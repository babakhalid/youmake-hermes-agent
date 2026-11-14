"""Youmake fork: computer-use permission for the ``hermes desktop`` browser.

"The computer" is the shared desktop Chrome that ``hermes desktop`` runs
(``browser.cdp_url`` + ``browser.youmake_desktop: true``).  Before any browser
tool drives it, the acting agent needs the user's permission, recorded by the
Youmake server in ``~/.hermes/desktop/permissions.json``.  Hermes only reads
that file, on every check (no caching).

Contract (source of truth): yomake_v2 docs/hermes-computer-permission.md.
The decision mirrors ``isComputerAllowed`` in yomake_v2
server/lib/computerPermission.js; the marker format is what its
``createPermissionMarkerScanner`` parses.

Hook points (kept to a few lines each so upstream merges stay easy):

* ``tools/registry.py`` ``dispatch``: :func:`gate_tool_call` for every
  ``browser_*`` tool (incl. ``browser_cdp`` / ``browser_dialog``), before the
  handler runs, so no session/supervisor attach happens when blocked and the
  model gets the marker as the whole tool result.
* ``tools/browser_tool.py`` ``_run_browser_command``: :func:`gate_browser_command`
  (defense in depth for internal callers that bypass the registry).

Not gated: no desktop mode, or the desktop endpoint is down and the browser
tool uses its hidden headless fallback (that browser is not the computer).
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse

from tools.youmake_identity import SLUG_RE, effective_identity

# Logged once per (run, reason) per turn to agent.log; Youmake reads this
# turn's slice of agent.log because it calls Hermes with stream: false.
marker_logger = logging.getLogger("youmake.computer_permission")
logger = logging.getLogger(__name__)

ALLOWED = "allowed"
DENIED = "denied"
ASK = "ask"

MODES = (ASK, ALLOWED, DENIED)
MARKER_KEY = "computer_permission_required"
DENIED_KEY = "computer_permission_denied"
MAX_REASON = 300
_NO_RUN_DEDUP_S = 300.0

ASK_SENTENCE = (
    "The user must allow computer use first. Stop using the browser, tell the "
    "user in one sentence what you want to do on the computer and why, and wait. "
    "Do not retry browser tools, and do not fetch the same page another way "
    "(web_extract, terminal curl, scripts): the user is being asked now."
)
DENIED_SENTENCE = (
    "Computer use is turned off for this agent. Do not use the browser; continue "
    "the task without the computer, or tell the user what you need."
)

# Last host each browser task navigated to (for "use the browser on <host>").
_last_host: Dict[str, str] = {}
# Dedup for markers outside a bound Youmake run: key -> monotonic time.
_logged_no_run: Dict[str, float] = {}
_lock = threading.Lock()


# ---------------------------------------------------------------------------
# permissions.json
# ---------------------------------------------------------------------------


def permission_file_path() -> Path:
    """``~/.hermes/desktop/permissions.json`` (root home, also from a profile)."""
    from hermes_constants import get_default_hermes_root

    return get_default_hermes_root() / "desktop" / "permissions.json"


def normalize_state(raw: Any, now: Optional[float] = None) -> Dict[str, Any]:
    """Same normalisation as the server's ``normalizePermissionState``."""
    now_s = int(time.time()) if now is None else now
    src = raw if isinstance(raw, dict) else {}
    agents: Dict[str, str] = {}
    src_agents = src.get("agents")
    if isinstance(src_agents, dict):
        for slug, v in src_agents.items():
            if isinstance(slug, str) and SLUG_RE.fullmatch(slug) and v in (ALLOWED, DENIED):
                agents[slug] = v
    once: Dict[str, int] = {}
    src_once = src.get("once")
    if isinstance(src_once, dict):
        for key, exp in src_once.items():
            if not isinstance(key, str) or len(key) > 200 or isinstance(exp, bool):
                continue
            try:
                n = float(exp)
            except (TypeError, ValueError):
                continue
            if n == n and n not in (float("inf"), float("-inf")) and n > now_s:
                once[key] = int(n)
    mode = src.get("mode")
    return {
        "version": 1,
        "mode": mode if mode in MODES else ASK,
        "agents": agents,
        "once": once,
        "updated_at": src.get("updated_at") if isinstance(src.get("updated_at"), str) else None,
    }


def read_state(path: Optional[Path] = None, now: Optional[float] = None) -> Tuple[Dict[str, Any], str]:
    """Read the file fresh.  Returns ``(state, status)``; status is ``ok``,
    ``missing`` or ``invalid``.  Missing/unreadable/invalid -> ``ask``, no grants."""
    p = path or permission_file_path()
    try:
        text = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return normalize_state(None, now), "missing"
    except Exception as exc:
        logger.debug("computer permission file unreadable (%s): %s", p, exc)
        return normalize_state(None, now), "invalid"
    try:
        data = json.loads(text)
    except ValueError:
        return normalize_state(None, now), "invalid"
    if not isinstance(data, dict):
        return normalize_state(None, now), "invalid"
    return normalize_state(data, now), "ok"


def decide(state: Dict[str, Any], agent: str, run_key: Optional[str], now: Optional[float] = None) -> str:
    """Contract section 2, in order."""
    now_s = time.time() if now is None else now
    s = normalize_state(state, now_s)
    if s["mode"] == DENIED:
        return DENIED
    per_agent = s["agents"].get(agent)
    if per_agent == DENIED:
        return DENIED
    if per_agent == ALLOWED:
        return ALLOWED
    if s["mode"] == ALLOWED:
        return ALLOWED
    if run_key and s["once"].get(run_key, 0) > now_s:
        return ALLOWED
    if s["once"].get(f"agent:{agent}", 0) > now_s:
        return ALLOWED
    return ASK


# ---------------------------------------------------------------------------
# Marker
# ---------------------------------------------------------------------------


def _clean_reason(reason: str) -> str:
    return " ".join(str(reason or "").split())[:MAX_REASON] or "use the computer"


def marker_json(agent: str, reason: str, run_key: Optional[str]) -> str:
    """Exactly the contract object: key order, json.dumps default separators."""
    obj: Dict[str, Any] = {MARKER_KEY: True, "agent": agent, "reason": _clean_reason(reason)}
    if run_key:
        obj["run"] = run_key
    return json.dumps(obj)


def denied_json(agent: str) -> str:
    return json.dumps({DENIED_KEY: True, "agent": agent})


def _host(url: Any) -> str:
    try:
        host = (urlparse(str(url or "").strip()).hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def _task_key(task_id: Optional[str]) -> str:
    return str(task_id or "default").split("::", 1)[0]


def _reason(kind: str, task_id: Optional[str], url: Any = None) -> str:
    key = _task_key(task_id)
    if kind == "navigate":
        host = _host(url)
        if host:
            with _lock:
                _last_host[key] = host
            return f"open {host}"
        return "use the computer"
    if kind == "interact":
        with _lock:
            host = _last_host.get(key)
        return f"use the browser on {host}" if host else "use the computer"
    return "use the computer"


_TOOL_KIND = {
    "browser_navigate": "navigate",
    "browser_click": "interact",
    "browser_type": "interact",
    "browser_press": "interact",
    "browser_scroll": "interact",
}
_COMMAND_KIND = {"open": "navigate", "click": "interact", "fill": "interact", "press": "interact", "scroll": "interact"}


def _log_marker_once(marker: str, run_key: Optional[str], run, reason: str) -> None:
    key = f"{run_key or ''}\n{reason}"
    if run is not None:
        with _lock:
            if key in run.emitted:
                return
            run.emitted.add(key)
    else:
        now = time.monotonic()
        with _lock:
            last = _logged_no_run.get(key)
            if last is not None and now - last < _NO_RUN_DEDUP_S:
                return
            _logged_no_run[key] = now
    marker_logger.warning("%s", marker)


# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------


def _desktop_mode() -> bool:
    from tools.youmake_desktop_guard import desktop_mode_enabled

    return desktop_mode_enabled()


def _desktop_active() -> bool:
    """True when browser tools would drive the desktop (not the headless fallback)."""
    try:
        from tools.browser_tool import _get_cdp_override

        return bool(_get_cdp_override())
    except Exception as exc:
        logger.debug("computer permission: CDP override check failed: %s", exc)
        return True  # unknown: assume the desktop, refuse rather than drive it


def evaluate(kind: str, task_id: Optional[str], url: Any = None) -> Optional[str]:
    """Return the blocking tool result, or None when the call may proceed."""
    if not _desktop_mode():
        return None
    reason = _reason(kind, task_id, url)
    agent, run_key, run = effective_identity()
    state, _status = read_state()
    decision = decide(state, agent, run_key)
    if decision == ALLOWED:
        return None
    if not _desktop_active():
        return None
    if decision == DENIED:
        return f"{denied_json(agent)}\n{DENIED_SENTENCE}"
    marker = marker_json(agent, reason, run_key)
    _log_marker_once(marker, run_key, run, _clean_reason(reason))
    return f"{marker}\n{ASK_SENTENCE}"


def gate_tool_call(name: str, args: Any, task_id: Optional[str] = None) -> Optional[str]:
    """Registry hook for ``browser_*`` tools.  Never raises."""
    if not str(name).startswith("browser_"):
        return None
    try:
        url = args.get("url") if isinstance(args, dict) else None
        return evaluate(_TOOL_KIND.get(name, "other"), task_id, url)
    except Exception as exc:
        logger.warning("computer permission check failed for %s: %s", name, exc)
        return None


def gate_browser_command(task_id: Optional[str], command: str, args: Any) -> Optional[Dict[str, Any]]:
    """``_run_browser_command`` hook.  Returns a failed result dict when blocked."""
    if command == "close":  # session cleanup never touches the page
        return None
    try:
        url = args[0] if command == "open" and args else None
        blocked = evaluate(_COMMAND_KIND.get(command, "other"), task_id, url)
    except Exception as exc:
        logger.warning("computer permission check failed for %s: %s", command, exc)
        return None
    if blocked is None:
        return None
    return {"success": False, "error": blocked, "youmake_computer_permission": True}


def describe(agent: Optional[str] = None, run_key: Optional[str] = None) -> Dict[str, Any]:
    """Debug payload for ``hermes desktop permission --json``."""
    path = permission_file_path()
    state, status = read_state(path)
    if agent is None:
        agent, ident_run, _ = effective_identity()
        run_key = run_key or ident_run
    try:
        desktop = _desktop_mode()
    except Exception:
        desktop = False
    return {
        "enforced": True,
        "file": str(path),
        "file_status": status,
        "desktop_mode": desktop,
        "state": state,
        "agent": agent,
        "run": run_key,
        "decision": decide(state, agent, run_key),
    }
