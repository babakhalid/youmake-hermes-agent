"""Youmake fork: resilience + attach guard for the ``hermes desktop`` CDP override.

Both behaviours are gated on ``browser.youmake_desktop: true`` in config.yaml
(written by ``hermes desktop setup``), so upstream behaviour is unchanged when
no override is set, and also when an override is set by other means
(``/browser connect``, a hand-written ``browser.cdp_url``).

1. Fallback: if the desktop Chromium does not answer (desktop stopped, unit
   restarting after an OOM kill), ``_get_cdp_override`` returns "" so the
   browser tool uses its normal local headless launcher instead of failing
   every tool call.  Logged as a warning (rate-limited).

2. Attach guard: Hermes passes ``--cdp <ws-url>`` to agent-browser without
   ``--session`` (agent-browser >= 0.13 silently launches its own browser
   when both are given) and uses a fresh socket dir per Hermes session, so a
   fresh daemon must attach or fail.  As defense in depth, after the first
   successful command of a desktop session we ask agent-browser which CDP
   endpoint it is driving (``get cdp-url``).  If it reports a different
   endpoint, it launched a browser of its own: we fail the call and drop the
   session rather than letting the agent work in an invisible browser.
"""

from __future__ import annotations

import logging
import re
import subprocess
import time
from typing import Dict, List, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

CONFIG_FLAG = "youmake_desktop"
_WARN_INTERVAL_S = 60.0
_last_warn = 0.0

_WS_URL_RE = re.compile(r"wss?://[^\s\"'<>]+")


def desktop_mode_enabled() -> bool:
    try:
        from hermes_cli.config import read_raw_config

        browser = read_raw_config().get("browser", {})
        return isinstance(browser, dict) and bool(browser.get(CONFIG_FLAG))
    except Exception:
        return False


def _endpoint_alive(url: str) -> bool:
    try:
        from hermes_cli.browser_connect import is_browser_debug_ready

        return is_browser_debug_ready(url, timeout=1.0)
    except Exception:
        return False


def apply_fallback(raw: str, resolved: str) -> str:
    """Return ``resolved``, or "" when the desktop endpoint is down.

    ``tools.browser_tool._resolve_cdp_override`` turns an HTTP discovery URL
    into ``ws://.../devtools/browser/<id>`` only when ``/json/version``
    answered; when it did that, the endpoint is up and we skip a second probe.
    """
    global _last_warn
    if not raw or not resolved or not desktop_mode_enabled():
        return resolved
    if resolved != raw and "/devtools/browser/" in resolved:
        return resolved
    if _endpoint_alive(resolved):
        return resolved
    now = time.monotonic()
    if now - _last_warn >= _WARN_INTERVAL_S:
        _last_warn = now
        logger.warning(
            "Desktop Chromium at %s is not answering; using the local headless "
            "browser instead (run `hermes desktop status` / `hermes desktop start`).",
            raw,
        )
    return ""


def _endpoint_key(url: str) -> Optional[str]:
    try:
        p = urlparse(url)
        host = (p.hostname or "").lower()
        if host in ("localhost", "::1"):
            host = "127.0.0.1"
        return f"{host}:{p.port}" if host and p.port else None
    except ValueError:
        return None


def attach_mismatch(expected: str, reported_output: str) -> Optional[str]:
    """Compare agent-browser's reported CDP endpoint with the desktop one.

    Returns the foreign URL when agent-browser is driving a different browser,
    None when it matches or the output is inconclusive (no ws URL found).
    """
    found = _WS_URL_RE.findall(reported_output or "")
    if not found:
        return None
    exp_key = _endpoint_key(expected)
    exp_id = expected.rsplit("/devtools/browser/", 1)[-1] if "/devtools/browser/" in expected else None
    for url in found:
        key = _endpoint_key(url)
        if exp_key and key == exp_key:
            if exp_id and "/devtools/browser/" in url and url.rsplit("/devtools/browser/", 1)[-1] != exp_id:
                return url  # same port, different browser instance
            return None
    return found[0]


def verify_attach(cmd_prefix: List[str], cdp_url: str, env: Dict[str, str], timeout: int = 15) -> Optional[str]:
    """Ask agent-browser which endpoint it is attached to.

    Returns an error string on a confirmed mismatch, else None.  Any failure
    to get an answer is treated as inconclusive (logged, not fatal), so an
    agent-browser version without ``get cdp-url`` never blocks the tool.
    """
    cmd = [*cmd_prefix, "--cdp", cdp_url, "--json", "get", "cdp-url"]
    try:
        # The daemon is already running (spawned by the command we are
        # verifying), so this client exits promptly and pipes are safe.
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            stdin=subprocess.DEVNULL, env=env,
        )
    except Exception as exc:
        logger.debug("desktop attach check inconclusive: %s", exc)
        return None
    foreign = attach_mismatch(cdp_url, f"{proc.stdout}\n{proc.stderr}")
    if foreign:
        return (
            "agent-browser is driving its own browser (%s) instead of the desktop "
            "Chromium (%s); refusing so the agent does not work in an invisible "
            "browser. Check `hermes desktop status`." % (foreign, cdp_url)
        )
    return None
