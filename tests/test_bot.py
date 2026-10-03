from __future__ import annotations

import os
from pathlib import Path

import pytest

from rs_radar import bot
from rs_radar.bot import Bot, BotConfig, BotPaths, read_alerts
from rs_radar.config import Settings
from rs_radar.models import Market
from rs_radar.scanner import Scanner
from rs_radar.storage import read_json
from rs_radar.watchlist import Watchlist
from tests.fakes import NOW, FakeProvider

pytestmark = pytest.mark.anyio


class Notifications:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    def __call__(self, title: str, subtitle: str, body: str) -> bool:
        self.sent.append((title, subtitle, body))
        return True


def make_bot(tmp_path: Path, *, now: float = NOW, **config: object) -> tuple[Bot, Notifications]:
    settings = Settings(home=tmp_path)
    notes = Notifications()
    cfg = BotConfig(**{"min_confidence": 50, **config})  # type: ignore[arg-type]
    scanners = {Market.CRYPTO: Scanner(FakeProvider(), clock=lambda: now)}
    return Bot(cfg, settings, scanners, notifier=notes, clock=lambda: now), notes


async def test_cycle_alerts_once_then_respects_cooldown(tmp_path: Path) -> None:
    b, notes = make_bot(tmp_path)

    first = await b.cycle()
    assert [a.symbol for a in first] == ["SPIKE"]
    assert notes.sent[0][0].startswith("LONG SPIKE")
    assert "SL" in notes.sent[0][1]

    assert await b.cycle() == []  # same signal, still in cooldown
    assert len(notes.sent) == 1

    heartbeat = read_json(BotPaths(tmp_path / "bot").heartbeat, {})
    assert heartbeat["pid"] == os.getpid()
    assert heartbeat["markets"]["crypto"]["signals"] == 1


async def test_cooldown_survives_restart(tmp_path: Path) -> None:
    b, _ = make_bot(tmp_path)
    await b.cycle()
    restarted, notes = make_bot(tmp_path)
    assert await restarted.cycle() == []
    assert notes.sent == []
    assert read_alerts(BotPaths(tmp_path / "bot"))[0]["symbol"] == "SPIKE"


async def test_stale_market_is_skipped(tmp_path: Path) -> None:
    b, _ = make_bot(tmp_path, now=NOW + 7200)
    assert await b.cycle() == []
    report = read_json(BotPaths(tmp_path / "bot").heartbeat, {})["markets"]["crypto"]
    assert report["live"] is False


async def test_watchlist_scope(tmp_path: Path) -> None:
    b, _ = make_bot(tmp_path, scope="watchlist")
    assert await b.cycle() == []  # empty watchlist
    Watchlist(Settings(home=tmp_path).watchlist_path).add(Market.CRYPTO, ["quiet"])
    assert await b.cycle() == []  # QUIET never signals
    Watchlist(Settings(home=tmp_path).watchlist_path).add(Market.CRYPTO, ["spike"])
    assert [a.symbol for a in await b.cycle()] == ["SPIKE"]


async def test_scan_errors_are_reported_not_raised(tmp_path: Path) -> None:
    b, _ = make_bot(tmp_path, markets=(Market.CRYPTO, Market.STOCKS))  # no stocks scanner wired
    await b.cycle()
    report = read_json(BotPaths(tmp_path / "bot").heartbeat, {})["markets"]
    assert "error" in report["stocks"]
    assert "signals" in report["crypto"]


def test_config_validation_and_round_trip() -> None:
    cfg = BotConfig(markets=(Market.CRYPTO, Market.STOCKS), min_confidence=90, scope="both")
    assert BotConfig.from_json(cfg.to_json()) == cfg
    with pytest.raises(ValueError, match="interval"):
        BotConfig(interval_s=5)
    with pytest.raises(ValueError, match="min_confidence"):
        BotConfig(min_confidence=150)
    with pytest.raises(ValueError, match="market"):
        BotConfig(markets=())


def test_process_guards(tmp_path: Path) -> None:
    settings = Settings(home=tmp_path)
    paths = BotPaths(settings.bot_dir)
    assert bot.stop(settings) is False
    assert bot.status(settings)["running"] is False

    paths.root.mkdir(parents=True)
    paths.pid.write_text(str(os.getpid()))  # pretend this test process is the bot
    assert bot.running_pid(paths) == os.getpid()
    with pytest.raises(bot.BotError, match="already running"):
        bot.start(settings, BotConfig())

    paths.pid.write_text("999999999")  # stale pid file from a crashed bot
    assert bot.running_pid(paths) is None


@pytest.mark.parametrize(
    ("price", "expected"), [(85123.456, "85,123.46"), (1.23456, "1.2346"), (0.0001234, "0.0001234")]
)
def test_fmt_price(price: float, expected: str) -> None:
    assert bot.fmt_price(price) == expected
