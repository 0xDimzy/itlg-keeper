"""The scheduler.

Design: one coroutine per claim feature, each sleeping until *its own* deadline
instead of sharing a ten-second tick. A long OTP login or a three-minute
human-like jitter can therefore never delay the other claims - the failure mode
that made the single-loop original miss its 24h window.

Each worker also survives its own failures: typed errors map onto a backoff
policy, and a crash is caught by the supervisor and restarted.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass

from .constants import RECOVERY_INTERVAL, RETRY_INTERVAL
from .errors import AuthError, KeeperError, RateLimited
from .runtime import AppContext
from .util import humanize, sleep_until

log = logging.getLogger("itlg_keeper.scheduler")


@dataclass(slots=True)
class TickResult:
    message: str | None = None
    due_at: float | None = None


class Worker:
    name = "worker"
    label = "Worker"
    #: Whether to apply human-like jitter before acting.
    jitter = True

    def __init__(self, ctx: AppContext, *, sleep=asyncio.sleep) -> None:
        self.ctx = ctx
        self._sleep = sleep
        self.failures = 0
        self.last_run: float | None = None
        self.last_message: str | None = None

    # -- contract ----------------------------------------------------------
    async def compute_due(self) -> float:
        raise NotImplementedError

    async def tick(self) -> TickResult:
        raise NotImplementedError

    # -- due-time memory ---------------------------------------------------
    @property
    def _hint_key(self) -> str:
        return f"due.{self.name}"

    async def next_due(self) -> float:
        now = time.time()
        hint = self.ctx.ledger.get(self._hint_key)
        if isinstance(hint, (int, float)) and hint > now:
            return float(hint)
        due = await self.compute_due()
        self._remember(due)
        return due

    def _remember(self, due: float | None) -> None:
        if due is not None:
            self.ctx.ledger.put(self._hint_key, float(due))

    # -- main loop ---------------------------------------------------------
    async def run(self, stop: asyncio.Event, paused: asyncio.Event) -> None:
        while not stop.is_set():
            if paused.is_set():
                if not await _wait(stop, 5.0):
                    return
                continue
            try:
                due = await self.next_due()
            except AuthError as exc:
                await self._on_auth_error(exc, stop)
                continue
            except KeeperError as exc:
                await self._backoff(stop, exc)
                continue

            wait = due - time.time()
            if wait > 0 and not await sleep_until(stop, due):
                return

            if self.jitter:
                delay = random.uniform(self.ctx.settings.runtime.jitter_min, self.ctx.settings.runtime.jitter_max)
                if delay > 0 and not await sleep_until(stop, time.time() + delay):
                    return

            await self._wait_if_paused(stop, paused)
            if stop.is_set():
                return

            try:
                result = await self.tick()
            except asyncio.CancelledError:
                raise
            except AuthError as exc:
                await self._on_auth_error(exc, stop)
                continue
            except RateLimited as exc:
                delay = exc.retry_after or 300.0
                log.warning("%s: rate limited, tunggu %.0fs", self.name, delay)
                self._remember(time.time() + delay)
                continue
            except KeeperError as exc:
                await self._backoff(stop, exc)
                continue
            except Exception as exc:
                log.exception("%s: error tak terduga", self.name)
                await self._report(f"⚠️ <b>{self.label}</b> error: {exc}")
                await self._backoff(stop, exc)
                continue

            self.failures = 0
            self.last_run = time.time()
            self.last_message = result.message
            if result.due_at is not None:
                self._remember(result.due_at)
            if result.message:
                await self._report(result.message)

    async def _wait_if_paused(self, stop: asyncio.Event, paused: asyncio.Event) -> None:
        while paused.is_set() and not stop.is_set():
            if not await _wait(stop, 3.0):
                return

    async def _backoff(self, stop: asyncio.Event, exc: Exception) -> None:
        self.failures += 1
        delay = min(60 * (2 ** min(self.failures - 1, 4)), 1800)
        log.warning("%s: %s (gagal %dx), coba lagi dalam %s", self.name, exc, self.failures, humanize(delay))
        if self.failures in {1, 3, 6}:
            await self._report(f"⚠️ <b>{self.label}</b> gagal: {exc}\nCoba lagi dalam {humanize(delay)}.")
        self._remember(time.time() + delay)
        await _wait(stop, delay)

    async def _on_auth_error(self, exc: Exception, stop: asyncio.Event) -> None:
        self.failures += 1
        log.warning("%s: masalah autentikasi: %s", self.name, exc)
        await self._report(f"🔐 <b>Login dibutuhkan</b>\n{exc}")
        await _wait(stop, 300)

    async def _report(self, message: str) -> None:
        try:
            await self.ctx.notifier.send(message)
        except Exception as exc:
            log.debug("gagal kirim notifikasi: %s", exc)

    def describe_due(self) -> tuple[float | None, str]:
        hint = self.ctx.ledger.get(self._hint_key)
        if isinstance(hint, (int, float)):
            return float(hint), humanize(float(hint) - time.time())
        return None, "-"


class MiningWorker(Worker):
    name = "mining"
    label = "Mining 4 jam"

    async def compute_due(self) -> float:
        gate = await self.ctx.mining.gate()
        if gate.due_at is None:
            return time.time() + RETRY_INTERVAL
        return gate.due_at

    async def tick(self) -> TickResult:
        outcome = await self.ctx.mining.claim()
        if outcome.claimed:
            amount = f"{outcome.amount:,.4f}" if outcome.amount is not None else "?"
            message = (
                f"⛏️ <b>Mining diklaim</b>\n"
                f"Hadiah: <b>+{amount} ITLG</b>\n"
                f"Saldo: {_fmt(outcome.balance_after)} ITLG\n"
                f"Frame berikutnya: {humanize((outcome.due_at or time.time()) - time.time())}"
            )
            return TickResult(message=message, due_at=outcome.due_at)
        if outcome.reason.startswith("sudah diklaim"):
            return TickResult(message=None, due_at=outcome.due_at)
        log.debug("mining: %s", outcome.reason)
        return TickResult(message=None, due_at=outcome.due_at)


class GroupWorker(Worker):
    name = "group"
    label = "Group mining 24 jam"

    async def compute_due(self) -> float:
        snapshot = await self.ctx.groups.snapshot()
        if snapshot.due_at is None:
            return time.time() + 3600
        return snapshot.due_at

    async def tick(self) -> TickResult:
        outcome = await self.ctx.groups.claim()
        if outcome.claimed:
            amount = f"{outcome.amount:,.4f}" if outcome.amount is not None else "?"
            message = (
                f"👥 <b>Group mining diklaim</b>\n"
                f"Hadiah: <b>+{amount} ITLG</b>\n"
                f"Saldo: {_fmt(outcome.balance_after)} ITLG\n"
                f"Berikutnya: {humanize((outcome.due_at or time.time()) - time.time())}"
            )
            return TickResult(message=message, due_at=outcome.due_at)
        return TickResult(message=None, due_at=outcome.due_at)


class RecoveryWorker(Worker):
    name = "recovery"
    label = "Pemulihan burn"

    async def compute_due(self) -> float:
        return time.time() + RECOVERY_INTERVAL

    async def tick(self) -> TickResult:
        outcome = await self.ctx.recovery.claim_all()
        if outcome.claimed:
            amount = f"{outcome.amount:,.4f}" if outcome.amount is not None else "?"
            message = (
                f"♻️ <b>Burn dipulihkan</b>\n"
                f"Jumlah: {outcome.claimed_count} transaksi\n"
                f"Nilai: <b>+{amount} ITLG</b>"
            )
            next_due = time.time() + RECOVERY_INTERVAL
            return TickResult(message=message, due_at=next_due)
        next_due = time.time() + (RETRY_INTERVAL if outcome.failures else RECOVERY_INTERVAL)
        return TickResult(message=None, due_at=next_due)


class Scheduler:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self.stop = asyncio.Event()
        self.paused = asyncio.Event()
        self.workers: list[Worker] = [
            MiningWorker(ctx),
            GroupWorker(ctx),
            RecoveryWorker(ctx),
        ]

    def pause(self) -> None:
        self.paused.set()

    def resume(self) -> None:
        self.paused.clear()

    @property
    def is_paused(self) -> bool:
        return self.paused.is_set()

    async def run(self) -> None:
        self.ctx.ledger.event("info", "scheduler", "scheduler dimulai")
        tasks = [asyncio.create_task(self._supervise(worker), name=worker.name) for worker in self.workers]
        try:
            await self.stop.wait()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self.ctx.ledger.event("info", "scheduler", "scheduler berhenti")

    async def _supervise(self, worker: Worker) -> None:
        while not self.stop.is_set():
            try:
                await worker.run(self.stop, self.paused)
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("worker %s berhenti tak terduga; restart 30s", worker.name)
                try:
                    await asyncio.wait_for(self.stop.wait(), timeout=30)
                    return
                except asyncio.TimeoutError:
                    continue

    def status_lines(self) -> list[tuple[str, str]]:
        rows: list[tuple[str, str]] = []
        for worker in self.workers:
            due, left = worker.describe_due()
            state = "jeda" if self.is_paused else ("siap" if due and due <= time.time() else "menunggu")
            rows.append((worker.label, f"{state} · {left}"))
        return rows


def _fmt(value: float | None) -> str:
    return f"{value:,.2f}" if value is not None else "?"


async def _wait(stop: asyncio.Event, seconds: float) -> bool:
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
        return False
    except asyncio.TimeoutError:
        return True
