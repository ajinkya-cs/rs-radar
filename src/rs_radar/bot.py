"""Background signal bot.

``start()`` launches ``python -m rs_radar bot run`` as a detached process, so the bot keeps running
after the MCP client (or terminal) that started it exits. The process scans every ``interval_s``,
delivers at most one alert per market per cycle (the highest-confidence one), applies a per-symbol
cooldown, and records a heartbeat so ``status()`` can tell a healthy bot from a hung one.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from rs_radar.config import Settings, build_scanners
from rs_radar.errors import RsRadarError
from rs_radar.http import HttpClient
from rs_radar.models import Market
from rs_radar.notify import desktop, webhook
from rs_radar.scanner import Scanner
from rs_radar.signals import Signal
from rs_radar.storage import read_json, write_json
from rs_radar.watchlist import Watchlist

log = logging.getLogger("rs_radar.bot")

Scope = Literal["universe", "watchlist", "both"]
MAX_ALERT_LOG_BYTES = 1_000_000
MAX_LOG_BYTES = 5_000_000


class BotError(RsRadarError):
    pass


@dataclass(frozen=True, slots=True)
class BotConfig:
    markets: tuple[Market, ...] = (Market.CRYPTO,)
    min_confidence: int = 85
    scope: Scope = "universe"
    universe_size: int = 200
    interval_s: int = 60
    cooldown_s: int = 7200
    desktop_notifications: bool = True

    def __post_init__(self) -> None:
        if not self.markets:
            raise ValueError("choose at least one market")
        if not 0 <= self.min_confidence <= 100:
            raise ValueError("min_confidence must be between 0 and 100")
        if self.interval_s < 30:
            raise ValueError("interval_s must be at least 30 seconds (public APIs rate-limit)")

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["markets"] = [m.value for m in self.markets]
        return data

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> BotConfig:
        fields = dict(data)
        fields["markets"] = tuple(Market(m) for m in fields.get("markets", ["crypto"]))
        return cls(**fields)


@dataclass(frozen=True, slots=True)
class Alert:
    ts: float
    market: str
    symbol: str
    confidence: int
    entry: float
    max_entry: float
    stop: float
    take_profit_1: float
    take_profit_2: float
    risk_pct: float
    reasons: tuple[str, ...] = field(default_factory=tuple)

    @classmethod
    def from_signal(cls, sig: Signal, now: float) -> Alert:
        p = sig.plan
        return cls(
            ts=now,
            market=sig.instrument.market.value,
            symbol=sig.instrument.symbol,
            confidence=sig.confidence,
            entry=p.entry,
            max_entry=p.max_entry,
            stop=p.stop,
            take_profit_1=p.take_profit_1,
            take_profit_2=p.take_profit_2,
            risk_pct=p.risk_pct,
            reasons=sig.reasons,
        )

    @property
    def title(self) -> str:
        return f"LONG {self.symbol} · {self.confidence}/100"

    @property
    def subtitle(self) -> str:
        return (
            f"Entry {fmt_price(self.entry)}  SL {fmt_price(self.stop)}  "
            f"TP {fmt_price(self.take_profit_1)} / {fmt_price(self.take_profit_2)}"
        )

    @property
    def text(self) -> str:
        return f"{self.title}\n{self.subtitle}\n{', '.join(self.reasons)}"


@dataclass(frozen=True, slots=True)
class BotPaths:
    root: Path

    @property
    def config(self) -> Path:
        return self.root / "config.json"

    @property
    def pid(self) -> Path:
        return self.root / "bot.pid"

    @property
    def heartbeat(self) -> Path:
        return self.root / "heartbeat.json"

    @property
    def alerts(self) -> Path:
        return self.root / "alerts.jsonl"

    @property
    def log(self) -> Path:
        return self.root / "bot.log"


def fmt_price(x: float) -> str:
    if x >= 100:
        return f"{x:,.2f}"
    if x >= 1:
        return f"{x:.4f}"
    return f"{x:.6g}"


def select_new(
    signals: Sequence[Signal], last_alerted: Mapping[str, float], now: float, cooldown_s: float
) -> Signal | None:
    """The best signal whose symbol is out of cooldown (signals arrive sorted by confidence)."""
    for sig in signals:
        key = f"{sig.instrument.market.value}:{sig.instrument.symbol}"
        if now - last_alerted.get(key, float("-inf")) >= cooldown_s:
            return sig
    return None


class Bot:
    """One bot process: owns the scan loop, cooldown state, alert log and heartbeat."""

    def __init__(
        self,
        config: BotConfig,
        settings: Settings,
        scanners: Mapping[Market, Scanner],
        *,
        http: HttpClient | None = None,
        notifier: Callable[[str, str, str], bool] = desktop,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.config = config
        self.settings = settings
        self.paths = BotPaths(settings.bot_dir)
        self.scanners = scanners
        self.watchlist = Watchlist(settings.watchlist_path)
        self._http = http
        self._notifier = notifier
        self._clock = clock
        self._last_alerted: dict[str, float] = {}
        for alert in read_alerts(self.paths, limit=500):
            key = f"{alert['market']}:{alert['symbol']}"
            self._last_alerted[key] = max(self._last_alerted.get(key, 0.0), float(alert["ts"]))

    async def cycle(self) -> list[Alert]:
        """Scan each configured market once. Returns the alerts delivered."""
        alerts, report = [], {}
        for market in self.config.markets:
            try:
                alert, report[market.value] = await self._scan_market(market)
            except Exception as exc:  # keep the loop alive; surface the error in the heartbeat
                log.exception("scan failed for %s", market.value)
                report[market.value] = {"error": str(exc) or type(exc).__name__}
                continue
            if alert is not None:
                await self._deliver(alert)
                alerts.append(alert)
        write_json(
            self.paths.heartbeat,
            {"ts": self._clock(), "pid": os.getpid(), "markets": report, "config": self.config.to_json()},
        )
        return alerts

    async def run_forever(self, stop: asyncio.Event) -> None:
        log.info("bot started: %s", self.config.to_json())
        while not stop.is_set():
            started = self._clock()
            await self.cycle()
            delay = max(1.0, self.config.interval_s - (self._clock() - started))
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=delay)
        log.info("bot stopped")

    async def _scan_market(self, market: Market) -> tuple[Alert | None, dict[str, Any]]:
        scanner = self.scanners[market]
        if not await scanner.is_live():
            return None, {"live": False, "note": "market closed or data stale; skipped"}
        watch = self.watchlist.get(market)
        scope = self.config.scope
        if scope == "watchlist" and not watch:
            return None, {"live": True, "note": "watchlist is empty"}
        snap = await scanner.snapshot(
            size=self.config.universe_size,
            symbols=watch if scope == "watchlist" else None,
            include=watch if scope == "both" else (),
        )
        signals = await scanner.signals(snap, min_confidence=self.config.min_confidence)
        best = select_new(signals, self._last_alerted, self._clock(), self.config.cooldown_s)
        report = {"live": snap.live, "scanned": len(snap.rows), "signals": len(signals)}
        if best is None:
            return None, report
        self._last_alerted[f"{market.value}:{best.instrument.symbol}"] = self._clock()
        return Alert.from_signal(best, self._clock()), report

    async def _deliver(self, alert: Alert) -> None:
        log.info("ALERT %s", alert.text.replace("\n", " | "))
        append_alert(self.paths, alert)
        if self.config.desktop_notifications:
            self._notifier(alert.title, alert.subtitle, ", ".join(alert.reasons))
        if self.settings.webhook_url and self._http is not None:
            await webhook(self._http, self.settings.webhook_url, alert.text)


def append_alert(paths: BotPaths, alert: Alert) -> None:
    paths.root.mkdir(parents=True, exist_ok=True)
    if paths.alerts.exists() and paths.alerts.stat().st_size > MAX_ALERT_LOG_BYTES:
        keep = paths.alerts.read_text(encoding="utf-8").splitlines()[-500:]
        paths.alerts.write_text("\n".join(keep) + "\n", encoding="utf-8")
    with paths.alerts.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(asdict(alert)) + "\n")


def read_alerts(paths: BotPaths, limit: int = 20) -> list[dict[str, Any]]:
    try:
        lines = paths.alerts.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    out = []
    for line in lines[-limit:]:
        with contextlib.suppress(json.JSONDecodeError):
            out.append(json.loads(line))
    return out


# --- process management -----------------------------------------------------------------------


def pid_alive(pid: int) -> bool:
    if sys.platform == "win32":  # os.kill(pid, 0) would terminate the process on Windows
        return _windows_pid_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _windows_pid_alive(pid: int) -> bool:  # pragma: no cover - exercised on Windows only
    import ctypes  # noqa: PLC0415 - Windows-only

    handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # type: ignore[attr-defined]
    if not handle:
        return False
    ctypes.windll.kernel32.CloseHandle(handle)  # type: ignore[attr-defined]
    return True


def running_pid(paths: BotPaths) -> int | None:
    try:
        pid = int(paths.pid.read_text().strip())
    except (FileNotFoundError, ValueError):
        return None
    return pid if pid_alive(pid) else None


def start(settings: Settings, config: BotConfig) -> int:
    """Launch the bot as a detached background process and return its PID."""
    paths = BotPaths(settings.bot_dir)
    if (pid := running_pid(paths)) is not None:
        raise BotError(f"bot is already running (pid {pid}); stop it first to change settings")
    paths.root.mkdir(parents=True, exist_ok=True)
    write_json(paths.config, config.to_json())
    if paths.log.exists() and paths.log.stat().st_size > MAX_LOG_BYTES:
        paths.log.replace(paths.log.with_suffix(".log.1"))

    kwargs: dict[str, Any] = {}
    if sys.platform == "win32":  # pragma: no cover
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    with paths.log.open("ab") as log_file:
        proc = subprocess.Popen(
            [sys.executable, "-m", "rs_radar", "bot", "run"],
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            env={**os.environ, "RS_RADAR_HOME": str(settings.home)},
            **kwargs,
        )
    paths.pid.write_text(str(proc.pid))
    return proc.pid


def stop(settings: Settings, *, timeout_s: float = 10.0) -> bool:
    """Stop the bot. Returns False when it wasn't running."""
    paths = BotPaths(settings.bot_dir)
    pid = running_pid(paths)
    if pid is None:
        paths.pid.unlink(missing_ok=True)
        return False
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline and pid_alive(pid):
        time.sleep(0.1)
    paths.pid.unlink(missing_ok=True)
    return True


def status(settings: Settings, *, alerts: int = 5) -> dict[str, Any]:
    paths = BotPaths(settings.bot_dir)
    pid = running_pid(paths)
    heartbeat = read_json(paths.heartbeat, None)
    config = read_json(paths.config, None)
    interval = (config or {}).get("interval_s", 60)
    age = time.time() - heartbeat["ts"] if heartbeat else None
    return {
        "running": pid is not None,
        "pid": pid,
        "healthy": pid is not None and age is not None and age < 3 * interval + 60,
        "last_cycle_seconds_ago": round(age) if age is not None else None,
        "last_cycle": heartbeat["markets"] if heartbeat else None,
        "config": config,
        "recent_alerts": read_alerts(paths, alerts),
        "log_file": str(paths.log),
    }


def run(settings: Settings) -> None:
    """Entry point of the detached process (``rs-radar bot run``)."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    paths = BotPaths(settings.bot_dir)
    config = BotConfig.from_json(read_json(paths.config, {}))
    paths.pid.parent.mkdir(parents=True, exist_ok=True)
    paths.pid.write_text(str(os.getpid()))

    async def main() -> None:
        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(NotImplementedError):  # Windows
                loop.add_signal_handler(sig, stop_event.set)
        async with HttpClient() as http:
            bot = Bot(config, settings, build_scanners(settings, http), http=http)
            await bot.run_forever(stop_event)

    try:
        asyncio.run(main())
    finally:
        if running_pid(paths) in (None, os.getpid()):
            paths.pid.unlink(missing_ok=True)
