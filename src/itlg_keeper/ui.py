"""Presentation: one status model, two renderers (terminal and Telegram)."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .errors import AuthError
from .runtime import AppContext
from .util import humanize, to_local

console = Console()


@dataclass(slots=True)
class StatusView:
    username: str | None = None
    balance: float | None = None
    daily_rate: float | None = None
    group_rate: float | None = None
    referrals: int = 0
    mining_claimable: bool = False
    mining_due: float | None = None
    group_total: float | None = None
    group_claimable: bool = False
    group_due: float | None = None
    recovery_total: float | None = None
    recovery_count: int = 0
    claims_today: int = 0
    amount_today: float = 0.0
    workers: list[tuple[str, str]] = field(default_factory=list)
    history: list[dict[str, Any]] = field(default_factory=list)
    paused: bool = False
    dry_run: bool = False
    tz: Any = None
    auth_error: str | None = None


async def build_status(ctx: AppContext, scheduler=None) -> StatusView:
    view = StatusView(tz=ctx.settings.runtime.tz, dry_run=ctx.settings.runtime.dry_run)
    errors: list[str] = []

    try:
        profile = await ctx.account.profile()
        view.username = profile.username
        view.balance = profile.balance
        view.daily_rate = profile.daily_rate
        view.group_rate = profile.group_rate
        view.referrals = profile.referrals
    except AuthError as exc:
        # One clear message beats four identical failures from four services.
        view.auth_error = str(exc)
    except Exception as exc:
        errors.append(f"profil: {exc}")

    if view.auth_error is None:
        try:
            gate = await ctx.mining.gate()
            view.mining_claimable = gate.claimable
            view.mining_due = gate.due_at
        except Exception as exc:
            errors.append(f"mining: {exc}")

        try:
            snapshot = await ctx.groups.snapshot()
            view.group_total = snapshot.total_reward
            view.group_claimable = snapshot.can_claim
            view.group_due = snapshot.due_at
        except Exception as exc:
            errors.append(f"grup: {exc}")

        try:
            recovery = await ctx.recovery.snapshot()
            view.recovery_total = recovery.total
            view.recovery_count = len(recovery.claimable)
        except Exception as exc:
            errors.append(f"recovery: {exc}")

    start_of_day = _start_of_day(view.tz)
    totals = ctx.ledger.claim_totals(start_of_day)
    view.claims_today = int(totals["count"])
    view.amount_today = float(totals["amount"])

    if scheduler is not None:
        view.workers = scheduler.status_lines()
        view.paused = scheduler.is_paused
    else:
        view.workers = _hint_lines(ctx)

    view.history = [
        {
            "kind": record.kind,
            "amount": record.amount,
            "status": record.status,
            "at": record.created_at,
        }
        for record in ctx.ledger.recent_claims(limit=5)
    ]
    if errors:
        view.history.insert(0, {"kind": "error", "status": "; ".join(errors), "at": time.time()})
    return view


def _hint_lines(ctx: AppContext) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for name, label in (("mining", "Mining 4 jam"), ("group", "Group 24 jam"), ("recovery", "Pemulihan")):
        hint = ctx.ledger.get(f"due.{name}")
        if isinstance(hint, (int, float)):
            rows.append((label, humanize(float(hint) - time.time())))
        else:
            rows.append((label, "belum dijadwalkan"))
    return rows


def render_console(view: StatusView) -> None:
    header = Table.grid(padding=(0, 2))
    header.add_column(justify="right", style="dim")
    header.add_column()
    header.add_row("Akun", view.username or "-")
    header.add_row("Saldo", f"[bold cyan]{_num(view.balance)}[/] ITLG")
    header.add_row(
        "Laju",
        f"harian {_num(view.daily_rate)} | grup {_num(view.group_rate)} | referral {view.referrals}",
    )
    if view.dry_run:
        header.add_row("Mode", "[yellow]DRY-RUN (tidak ada klaim dikirim)[/]")
    if view.paused:
        header.add_row("Status", "[yellow]dijeda[/]")

    if view.auth_error:
        console.print(
            Group(
                Panel(header, title="[bold]ITLG Keeper[/]", border_style="cyan"),
                Panel(
                    f"[yellow]{view.auth_error}[/]\n\n"
                    "[dim]Jalankan: itlg-keeper login[/]",
                    title="Perlu login",
                    border_style="yellow",
                ),
            )
        )
        return

    mining = Text()
    mining.append("frame 4 jam: ", style="dim")
    if view.mining_claimable:
        mining.append("siap diklaim", style="bold green")
    else:
        mining.append(_when(view.mining_due))
    mining.append(f"  ({to_local(view.mining_due, view.tz)})\n", style="dim")
    mining.append("group 24 jam: ", style="dim")
    if view.group_claimable:
        mining.append("siap diklaim", style="bold green")
    else:
        mining.append(_when(view.group_due))
    mining.append(f"  (pool {_num(view.group_total)})\n", style="dim")
    mining.append("pemulihan: ", style="dim")
    mining.append(
        f"{view.recovery_count} burn | {_num(view.recovery_total)} ITLG"
        if view.recovery_count
        else "tidak ada",
        style="bold green" if view.recovery_count else "",
    )

    today = Table(box=None, padding=(0, 1), show_header=False)
    today.add_column(style="dim", justify="right")
    today.add_column()
    today.add_row("Hari ini", f"{view.claims_today} klaim | +{_num(view.amount_today)} ITLG")
    for row in view.workers:
        today.add_row(*row)

    history = Table(box=None, padding=(0, 1), show_header=False)
    history.add_column(style="dim")
    history.add_column()
    history.add_column(style="dim")
    for item in view.history:
        history.add_row(
            to_local(item.get("at"), view.tz),
            f"{item.get('kind')} | {item.get('status')}",
            _num(item.get("amount")) if item.get("amount") is not None else "",
        )

    body = Group(
        Panel(header, title="[bold]ITLG Keeper[/]", border_style="cyan"),
        Panel(mining, title="Jadwal", border_style="blue"),
        Panel(today, title="Ringkasan", border_style="blue"),
        Panel(
            history if view.history else Text("belum ada riwayat", style="dim"),
            title="Aktivitas",
            border_style="blue",
        ),
    )
    console.print(body)


def _when(due: float | None) -> str:
    if due is None:
        return "menunggu data"
    return f"buka dalam {humanize(due - time.time())}"


def render_telegram(view: StatusView) -> str:
    import html

    def e(value: Any) -> str:
        return html.escape(str(value))

    def countdown(epoch: float | None) -> str:
        if not epoch:
            return "-"
        remaining = epoch - time.time()
        return "sekarang" if remaining <= 0 else humanize(remaining)

    lines = [
        "<b>📊 ITLG Keeper</b>",
        f"Akun: <code>{e(view.username or '-')}</code>",
        f"Saldo: <b>{_num(view.balance)}</b> ITLG",
        "",
        f"⛏️ 4 jam: {'<b>siap</b>' if view.mining_claimable else countdown(view.mining_due)}",
        f"👥 24 jam: {'<b>siap</b>' if view.group_claimable else countdown(view.group_due)}",
        f"♻️ recovery: {view.recovery_count} burn · {_num(view.recovery_total)}",
        "",
        f"Hari ini: {view.claims_today} klaim · +{_num(view.amount_today)} ITLG",
    ]
    if view.paused:
        lines.append("⏸️ <i>scheduler dijeda</i>")
    if view.dry_run:
        lines.append("🧪 <i>dry-run aktif</i>")
    recent = [h for h in view.history if h.get("kind") != "error"][:3]
    if recent:
        lines.append("")
        for item in recent:
            lines.append(
                f"• {to_local(item.get('at'), view.tz)} — {e(item.get('kind'))} "
                f"{'+' + _num(item.get('amount')) if item.get('amount') is not None else ''}"
            )
    return "\n".join(lines)


def _num(value: float | None) -> str:
    if value is None:
        return "?"
    if abs(value) >= 1000:
        return f"{value:,.2f}"
    return f"{value:,.4f}".rstrip("0").rstrip(".")


def _start_of_day(tz) -> float:
    import datetime as _dt

    now = _dt.datetime.now(tz)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.timestamp()
