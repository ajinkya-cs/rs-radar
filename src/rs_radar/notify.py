"""Alert delivery: native desktop notifications and an optional webhook."""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys

from rs_radar.errors import DataSourceError
from rs_radar.http import HttpClient

log = logging.getLogger(__name__)

# Text is passed as argv, never interpolated into AppleScript source, so symbols can't inject code.
_APPLESCRIPT = (
    "on run argv\n"
    "display notification (item 1 of argv) with title (item 2 of argv) "
    'subtitle (item 3 of argv) sound name "Glass"\n'
    "end run"
)


def desktop(title: str, subtitle: str, body: str) -> bool:
    """Show a native notification. Returns False when the platform has no supported notifier."""
    if sys.platform == "darwin":
        cmd = ["osascript", "-e", _APPLESCRIPT, body, title, subtitle]
    elif shutil.which("notify-send"):
        cmd = ["notify-send", "--app-name=rs-radar", title, f"{subtitle}\n{body}"]
    else:
        return False
    try:
        subprocess.run(cmd, check=True, timeout=10, capture_output=True)
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("desktop notification failed: %s", exc)
        return False
    return True


async def webhook(http: HttpClient, url: str, text: str) -> bool:
    """POST an alert to a Discord- or Slack-compatible incoming webhook."""
    try:
        await http.post_json(url, {"content": text, "text": text})
    except DataSourceError as exc:
        log.warning("webhook delivery failed: %s", exc)
        return False
    return True
