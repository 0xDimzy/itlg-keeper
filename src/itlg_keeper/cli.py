"""Command line interface.

Ten commands, each doing one thing:

    init      write a starter config file
    login     obtain and store a session (OTP or selfie)
    run       start the daemon (scheduler + optional Telegram bot)
    status    one-shot dashboard
    claim     claim now, by kind
    devices   show or rotate the device fingerprint
    doctor    diagnose configuration, network and session
    export    dump the status model as JSON
    config    print the effective configuration with secrets masked
    prune     trim old ledger rows
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import sys
from datetime import datetime
from pathlib import Path

import typer
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

from . import __version__
from .config import CONFIG_FILENAME, Settings, generate_device, load_settings, masked_dump
from .errors import ApiError, AuthError, ConfigError, KeeperError, LoginUnavailable
from .runtime import AppContext
from .scheduler import Scheduler
from .ui import build_status, render_console, render_telegram
from .util import humanize, to_local

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="ITLG Keeper - penjaga klaim Interlink Labs (mining 4 jam, grup 24 jam, pemulihan burn).",
)
console = Console()
log = logging.getLogger("itlg_keeper")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(message)s",
        datefmt="%H:%M:%S",
        handlers=[RichHandler(console=console, rich_tracebacks=True, show_path=False, markup=False)],
        force=True,
    )
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _settings(config: Path | None, *, require_device: bool = False) -> Settings:
    return load_settings(config, require_device=require_device)


async def _open(ctx_config: Path | None, *, telegram: bool = True) -> AppContext:
    settings = _settings(ctx_config)
    return await AppContext.create(settings)


def _install_signals(loop: asyncio.AbstractEventLoop, stop: asyncio.Event) -> None:
    def request_stop(*_: object) -> None:
        loop.call_soon_threadsafe(stop.set)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, AttributeError, RuntimeError):
            signal.signal(sig, request_stop)


def _banner(settings: Settings) -> None:
    device = settings.device
    ident = f"{device.brand} {device.model} ({device.device_id})" if device else "belum ada"
    console.print(
        f"[bold cyan]ITLG Keeper[/] v{__version__}  "
        f"[dim]akun {settings.account.login_id} | perangkat {ident}[/]"
    )
    if settings.runtime.dry_run:
        console.print("[yellow]Mode DRY-RUN: tidak ada klaim yang benar-benar dikirim.[/]")
    if settings.source:
        console.print(f"[dim]konfigurasi: {settings.source}[/]")


# --------------------------------------------------------------------------- #
# init
# --------------------------------------------------------------------------- #
@app.command()
def init(
    path: Path = typer.Option(Path.cwd() / CONFIG_FILENAME, "--path", "-p", help="Lokasi file TOML."),
    force: bool = typer.Option(False, "--force", help="Timpa file yang sudah ada."),
) -> None:
    """Buat file konfigurasi awal (interaktif, secret tidak dicetak)."""
    target = path.expanduser()
    if target.exists() and not force:
        raise typer.BadParameter(f"{target} sudah ada; pakai --force untuk menimpa")

    login_id = typer.prompt("Login ID Interlink (angka)")
    passcode = typer.prompt("Passcode 6 digit", hide_input=True)
    email = typer.prompt("Email Gmail akun (opsional, Enter untuk lewati)", default="", show_default=False)
    imap_password = ""
    if email:
        imap_password = typer.prompt(
            "App Password Gmail 16 karakter (opsional)", hide_input=True, default="", show_default=False
        )
    face_photo = typer.prompt("Path foto selfie (opsional, Enter untuk lewati)", default="", show_default=False)
    tg_token = typer.prompt("Token bot Telegram (opsional)", default="", show_default=False)
    tg_chat = typer.prompt("Chat ID Telegram (opsional)", default="", show_default=False)

    device = generate_device()
    lines = [
        "# Konfigurasi ITLG Keeper. Jangan commit file ini.",
        "[account]",
        f'login_id = "{login_id}"',
        f'passcode = "{passcode}"',
    ]
    if email:
        lines.append(f'email = "{email}"')
    if imap_password:
        lines.append(f'imap_password = "{imap_password.replace(" ", "")}"')
    if face_photo:
        lines.append(f'face_photo = "{face_photo}"')
    lines += [
        "",
        "[telegram]",
        f'token = "{tg_token}"',
        f'chat_id = "{tg_chat}"',
        "",
        "[runtime]",
        'timezone = "Asia/Jakarta"',
        "jitter_min = 45",
        "jitter_max = 180",
        "dry_run = false",
        "",
        "[device]",
        f'device_id = "{device.device_id}"',
        f'brand = "{device.brand}"',
        f'model = "{device.model}"',
        f'user_agent = "{device.user_agent}"',
        "",
    ]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines), encoding="utf-8")
    with contextlib.suppress(OSError):  # Windows may not support POSIX modes
        os.chmod(target, 0o600)
    console.print(f"[green]Konfigurasi ditulis:[/] {target}")
    console.print("Langkah berikutnya: [bold]itlg-keeper login[/] lalu [bold]itlg-keeper run[/]")


# --------------------------------------------------------------------------- #
# login
# --------------------------------------------------------------------------- #
@app.command()
def login(
    config: Path | None = typer.Option(None, "--config", "-c", help="File konfigurasi TOML."),
    face: bool = typer.Option(False, "--face", help="Paksa login selfie."),
    photo: Path | None = typer.Option(None, "--photo", help="Path foto selfie."),
) -> None:
    """Ambil sesi baru (OTP email atau selfie) dan simpan tokennya."""

    async def _run() -> None:
        settings = _settings(config)
        setup_logging(settings.runtime.log_level)
        ctx = await AppContext.create(settings)
        try:
            _banner(ctx.settings)
            if face or photo:
                pair = await ctx.login.login_with_face(str(photo) if photo else None)
            elif ctx.settings.account.can_use_otp:
                pair = await ctx.login.login_with_otp()
            elif ctx.settings.account.can_use_face:
                pair = await ctx.login.login_with_face()
            else:
                raise LoginUnavailable(
                    "tidak ada metode login. Isi email + App Password Gmail, "
                    "atau set face_photo ke file selfie."
                )
            await ctx.tokens.store(pair)
            console.print("[green]Login berhasil.[/]")
            console.print(f"Token berlaku sampai: {to_local(pair.expires_at, ctx.settings.runtime.tz)}")
            console.print("Sekarang jalankan: [bold]itlg-keeper run[/]")
        finally:
            await ctx.aclose()

    _guard(_run)


# --------------------------------------------------------------------------- #
# run
# --------------------------------------------------------------------------- #
@app.command()
def run(
    config: Path | None = typer.Option(None, "--config", "-c", help="File konfigurasi TOML."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Jangan kirim klaim, hanya laporkan."),
    once: bool = typer.Option(False, "--once", help="Satu putaran lalu keluar (cocok untuk cron)."),
    no_telegram: bool = typer.Option(False, "--no-telegram", help="Matikan bot perintah Telegram."),
) -> None:
    """Jalankan daemon: tiga worker independen plus bot Telegram opsional."""

    async def _run() -> None:
        settings = _settings(config)
        if dry_run:
            settings = settings.model_copy(
                update={"runtime": settings.runtime.model_copy(update={"dry_run": True})}
            )
        setup_logging(settings.runtime.log_level)
        ctx = await AppContext.create(settings)
        scheduler = Scheduler(ctx)
        try:
            _banner(ctx.settings)
            stop = scheduler.stop
            loop = asyncio.get_running_loop()
            _install_signals(loop, stop)

            if once:
                for worker in scheduler.workers:
                    try:
                        result = await worker.tick()
                        console.print(f"[dim]{worker.label}:[/] {result.message or 'tidak ada aksi'}")
                    except KeeperError as exc:
                        console.print(f"[yellow]{worker.label}:[/] {exc}")
                return

            await ctx.notifier.send(
                f"🟢 <b>ITLG Keeper aktif</b>\nAkun <code>{ctx.settings.account.login_id}</code>"
                + ("\n🧪 dry-run" if ctx.settings.runtime.dry_run else "")
            )

            tasks = [asyncio.create_task(scheduler.run(), name="scheduler")]
            bot = None
            if ctx.settings.runtime.telegram_commands and ctx.settings.telegram.active and not no_telegram:
                from .notify import CommandBot

                bot = CommandBot(ctx.settings.telegram, _make_dispatcher(ctx, scheduler))
                try:
                    username = await bot.verify()
                    console.print(f"[dim]Bot Telegram: @{username}[/]")
                except Exception as exc:
                    console.print(f"[yellow]Bot Telegram tidak bisa dipakai: {exc}[/]")
                    await bot.aclose()
                    bot = None
                if bot is not None:
                    tasks.append(asyncio.create_task(bot.run(stop), name="telegram"))

            console.print("[green]Berjalan.[/] Ctrl+C untuk berhenti.")
            await stop.wait()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if bot is not None:
                await bot.aclose()
        finally:
            await ctx.aclose()

    _guard(_run)


def _make_dispatcher(ctx: AppContext, scheduler: Scheduler):
    from .notify import esc

    async def dispatch(text: str, chat_id: str) -> str | None:
        command = text.strip().split()[0].lower().lstrip("/")
        command = command.split("@")[0]

        if command in {"start", "help"}:
            return (
                "<b>ITLG Keeper</b>\n"
                "/status - ringkasan akun & jadwal\n"
                "/claim - klaim mining sekarang\n"
                "/group - klaim group mining\n"
                "/recover - pulihkan burn\n"
                "/pause - hentikan klaim otomatis\n"
                "/resume - lanjutkan klaim otomatis\n"
                "/dryrun - lihat status dry-run\n"
            )
        if command == "status":
            view = await build_status(ctx, scheduler)
            return render_telegram(view)
        if command in {"claim", "mining"}:
            outcome = await ctx.mining.claim()
            return _outcome_html(outcome)
        if command == "group":
            outcome = await ctx.groups.claim(force=True)
            return _outcome_html(outcome)
        if command in {"recover", "recovery"}:
            outcome = await ctx.recovery.claim_all()
            return f"♻️ {esc(outcome.message)}"
        if command == "pause":
            scheduler.pause()
            return "⏸️ Klaim otomatis dijeda."
        if command == "resume":
            scheduler.resume()
            return "▶️ Klaim otomatis dilanjutkan."
        if command == "dryrun":
            return "🧪 dry-run: " + ("aktif" if ctx.settings.runtime.dry_run else "nonaktif")
        return "Perintah tidak dikenal. Coba /help"

    return dispatch


def _outcome_html(outcome) -> str:
    from .notify import esc

    if outcome.claimed:
        amount = f"{outcome.amount:,.4f}" if outcome.amount is not None else "?"
        return f"✅ Klaim berhasil: +{amount} ITLG"
    return f"ℹ️ Tidak ada klaim: {esc(outcome.reason)}"


# --------------------------------------------------------------------------- #
# status / claim / devices
# --------------------------------------------------------------------------- #
@app.command()
def status(
    config: Path | None = typer.Option(None, "--config", "-c"),
    as_json: bool = typer.Option(False, "--json", help="Cetak JSON, bukan tabel."),
) -> None:
    """Tampilkan satu dashboard lalu keluar."""

    async def _run() -> None:
        settings = _settings(config)
        setup_logging("WARNING")
        ctx = await AppContext.create(settings)
        try:
            view = await build_status(ctx)
            if as_json:
                console.print_json(json.dumps(_view_dict(view), default=str))
            else:
                render_console(view)
        finally:
            await ctx.aclose()

    _guard(_run)


@app.command()
def claim(
    kind: str = typer.Option("mining", "--kind", "-k", help="mining | group | recovery | all"),
    force: bool = typer.Option(False, "--force", help="Klaim walau timer belum jatuh tempo."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Jangan kirim klaim."),
    config: Path | None = typer.Option(None, "--config", "-c"),
) -> None:
    """Klaim sekarang juga (tanpa menunggu timer)."""

    async def _run() -> None:
        settings = _settings(config)
        setup_logging(settings.runtime.log_level)
        ctx = await AppContext.create(settings)
        try:
            _banner(ctx.settings)
            kinds = ["mining", "group", "recovery"] if kind == "all" else [kind]
            for item in kinds:
                if item == "mining":
                    outcome = await ctx.mining.claim(force=force, dry_run=dry_run)
                    console.print(f"[bold]Mining:[/] {outcome.message}")
                elif item == "group":
                    outcome = await ctx.groups.claim(force=force, dry_run=dry_run)
                    console.print(f"[bold]Group:[/] {outcome.message}")
                elif item == "recovery":
                    result = await ctx.recovery.claim_all()
                    console.print(f"[bold]Recovery:[/] {result.message}")
                else:
                    raise typer.BadParameter(f"jenis klaim tidak dikenal: {item}")
        finally:
            await ctx.aclose()

    _guard(_run)


@app.command()
def devices(
    rotate: bool = typer.Option(False, "--rotate", help="Buat identitas perangkat baru (sesi lama batal)."),
    config: Path | None = typer.Option(None, "--config", "-c"),
) -> None:
    """Lihat atau ganti identitas perangkat yang dipakai API."""

    async def _run() -> None:
        settings = _settings(config)
        setup_logging("WARNING")
        ctx = await AppContext.create(settings)
        try:
            if rotate:
                device = generate_device()
                ctx.ledger.put("device", device.model_dump())
                ctx.tokens.clear()
                ctx.ledger.event("warning", "device", "identitas perangkat dirotasi; sesi dihapus")
                console.print(
                    f"[yellow]Identitas baru:[/] {device.brand} {device.model} · {device.device_id}"
                )
                console.print("Token lama dihapus. Jalankan [bold]itlg-keeper login[/] lagi.")
                return
            device = ctx.settings.device
            table = Table(title="Identitas perangkat", box=None)
            table.add_column("Field", style="dim")
            table.add_column("Nilai")
            table.add_row("device_id", device.device_id if device else "-")
            table.add_row("brand", device.brand if device else "-")
            table.add_row("model", device.model if device else "-")
            table.add_row("user_agent", device.user_agent if device else "-")
            table.add_row("database", str(ctx.settings.ledger_path))
            console.print(table)
        finally:
            await ctx.aclose()

    _guard(_run)


# --------------------------------------------------------------------------- #
# doctor / export / config / prune
# --------------------------------------------------------------------------- #
@app.command()
def doctor(
    config: Path | None = typer.Option(None, "--config", "-c"),
) -> None:
    """Periksa konfigurasi, jaringan, sesi, dan jam sistem."""

    async def _run() -> None:
        table = Table(title="Diagnosa", box=None, show_lines=False)
        table.add_column("Cek", style="dim")
        table.add_column("Hasil")
        failures = 0

        def ok(label: str, detail: str = "ok") -> None:
            table.add_row(label, f"[green]✓[/] {detail}")

        def bad(label: str, detail: str) -> None:
            nonlocal failures
            failures += 1
            table.add_row(label, f"[red]✗[/] {detail}")

        table.add_row("Python", f"{sys.version.split()[0]}")
        try:
            settings = _settings(config)
            ok("Konfigurasi", str(settings.source or "dari environment"))
        except ConfigError as exc:
            bad("Konfigurasi", str(exc))
            console.print(table)
            raise typer.Exit(2) from exc

        if settings.account.can_use_otp:
            ok("Login OTP", f"IMAP {settings.account.imap_host}")
        elif settings.account.can_use_face:
            ok("Login selfie", str(settings.account.face_photo))
        else:
            bad("Login", "tidak ada IMAP maupun foto selfie - sesi tidak bisa diperbarui")

        try:
            settings.runtime.data_dir.mkdir(parents=True, exist_ok=True)
            probe = settings.runtime.data_dir / ".write-test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            ok("Data dir", str(settings.runtime.data_dir))
        except OSError as exc:
            bad("Data dir", str(exc))

        ctx = await AppContext.create(settings)
        try:
            snapshot = ctx.tokens.snapshot()
            if snapshot["present"]:
                detail = (
                    f"berlaku {humanize(snapshot['seconds_left'])} lagi"
                    if snapshot["seconds_left"] > 0
                    else "kedaluwarsa (akan di-refresh)"
                )
                ok("Sesi", detail + (" · ada refresh token" if snapshot["has_refresh"] else ""))
            else:
                bad("Sesi", "belum login - jalankan `itlg-keeper login`")

            try:
                profile = await ctx.account.profile()
                ok("API", f"saldo {profile.balance if profile.balance is not None else '?'} ITLG")
                server_hint = _server_skew(profile.raw)
                if server_hint is not None:
                    ok("Jam", f"selisih {server_hint:+.0f}s dari server")
                else:
                    ok("Jam", "tidak bisa dibandingkan")
            except ApiError as exc:
                bad("API", str(exc))
            except Exception as exc:
                bad("API", f"tidak terjangkau: {exc}")

            if settings.telegram.active:
                from .notify import CommandBot

                bot = CommandBot(settings.telegram, lambda *_: None)  # type: ignore[arg-type]
                try:
                    username = await bot.verify()
                    ok("Telegram", f"@{username}")
                except Exception as exc:
                    bad("Telegram", str(exc))
                finally:
                    await bot.aclose()
            else:
                ok("Telegram", "tidak dikonfigurasi (opsional)")
        finally:
            await ctx.aclose()

        console.print(table)
        if failures:
            raise typer.Exit(1)

    _guard(_run)


@app.command()
def export(
    config: Path | None = typer.Option(None, "--config", "-c"),
    out: Path | None = typer.Option(None, "--out", "-o", help="Tulis ke file, bukan stdout."),
) -> None:
    """Ekspor status sebagai JSON (untuk monitoring eksternal)."""

    async def _run() -> None:
        settings = _settings(config)
        setup_logging("WARNING")
        ctx = await AppContext.create(settings)
        try:
            view = await build_status(ctx)
            payload = json.dumps(_view_dict(view), indent=2, default=str)
            if out:
                await asyncio.to_thread(out.write_text, payload, encoding="utf-8")
                console.print(f"[green]Ditulis:[/] {out}")
            else:
                console.print_json(payload)
        finally:
            await ctx.aclose()

    _guard(_run)


@app.command(name="config")
def show_config(
    config: Path | None = typer.Option(None, "--config", "-c"),
) -> None:
    """Cetak konfigurasi efektif (secret disamarkan)."""
    try:
        settings = _settings(config)
    except ConfigError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(2) from exc
    console.print_json(json.dumps(masked_dump(settings), indent=2, default=str))


@app.command()
def prune(
    days: int = typer.Option(30, "--days", help="Simpan riwayat selama N hari."),
    config: Path | None = typer.Option(None, "--config", "-c"),
) -> None:
    """Bersihkan riwayat lama dari database."""
    settings = _settings(config)
    from .ledger import Ledger

    with Ledger(settings.ledger_path) as ledger:
        removed = ledger.prune(days)
        ledger.vacuum()
    console.print(f"[green]{removed}[/] baris dihapus (lebih tua dari {days} hari).")


# --------------------------------------------------------------------------- #
# misc
# --------------------------------------------------------------------------- #
def _view_dict(view) -> dict:
    return {
        "username": view.username,
        "balance": view.balance,
        "daily_rate": view.daily_rate,
        "group_rate": view.group_rate,
        "referrals": view.referrals,
        "mining": {"claimable": view.mining_claimable, "due_at": view.mining_due},
        "group": {
            "claimable": view.group_claimable,
            "due_at": view.group_due,
            "pool": view.group_total,
        },
        "recovery": {"count": view.recovery_count, "total": view.recovery_total},
        "today": {"claims": view.claims_today, "amount": view.amount_today},
        "paused": view.paused,
        "dry_run": view.dry_run,
        "workers": [{"name": name, "state": state} for name, state in view.workers],
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }


def _server_skew(raw: dict) -> float | None:
    """The envelope carries a server timestamp; use it as a clock reference."""
    from .util import to_epoch_seconds

    for key in ("serverTime", "timestamp", "now"):
        epoch = to_epoch_seconds(raw.get(key))
        if epoch is not None:
            import time

            return epoch - time.time()
    return None


def _guard(coro) -> None:
    try:
        asyncio.run(coro())
    except KeyboardInterrupt:
        console.print("[dim]dihentikan[/]")
        raise typer.Exit(130) from None
    except (ConfigError, LoginUnavailable, AuthError) as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(2) from exc
    except KeeperError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1) from exc


def main() -> None:  # pragma: no cover - console-script shim
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
