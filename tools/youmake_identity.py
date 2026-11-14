"""Youmake fork: which Youmake agent/run a Hermes turn belongs to.

Youmake sends the OpenAI ``user`` field on every ``/v1/chat/completions``
call as ``youmake:<userId>:<slug>:<lane>`` (lane: ``g<gen>`` for a chat,
``t<taskId[0:12]>`` for a delegated teammate task, ``synth`` for the lead
synthesis).  The API server binds it to :data:`current_youmake_run` for the
duration of the agent run; tools read it to know which agent is acting
(computer-use permission, see tools/youmake_computer_permission.py).

ContextVars do not cross ``loop.run_in_executor`` or plain
``ThreadPoolExecutor.submit``; :func:`propagate` captures the current run and
rebinds it in the worker thread (used by api_server ``_run_agent`` and the
delegate_task child threads).  Only this one variable is carried, so upstream
contextvar behaviour is unchanged.

Contract: yomake_v2 docs/hermes-computer-permission.md section 3.
"""

from __future__ import annotations

import contextvars
import functools
import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Set, Tuple

RUN_RE = re.compile(r"^youmake:[A-Za-z0-9-]{1,64}:[A-Za-z0-9_-]{1,64}:[A-Za-z0-9_-]{1,64}$")
SLUG_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
DEFAULT_AGENT = "hermes"


@dataclass(eq=False)
class YoumakeRun:
    raw: str
    user_id: str
    slug: str
    lane: str
    # Permission markers already logged during this run (one per reason).
    emitted: Set[str] = field(default_factory=set, repr=False)


current_youmake_run: contextvars.ContextVar[Optional[YoumakeRun]] = contextvars.ContextVar(
    "current_youmake_run", default=None
)


def parse_youmake_user(value: Any) -> Optional[YoumakeRun]:
    """Parse the request ``user`` field; None when absent or not a Youmake run key."""
    if not isinstance(value, str) or not RUN_RE.fullmatch(value):
        return None
    _, user_id, slug, lane = value.split(":")
    return YoumakeRun(raw=value, user_id=user_id, slug=slug, lane=lane)


def get_current_run() -> Optional[YoumakeRun]:
    return current_youmake_run.get()


def set_current_run(run: Optional[YoumakeRun]) -> contextvars.Token:
    return current_youmake_run.set(run)


def reset_current_run(token: contextvars.Token) -> None:
    try:
        current_youmake_run.reset(token)
    except ValueError:
        # Token from another context (should not happen); leave it be.
        current_youmake_run.set(None)


def bind_request_user(value: Any) -> contextvars.Token:
    """Bind the run for this request; ALWAYS sets (None when invalid) so a
    keep-alive connection never leaks the previous request's identity."""
    return set_current_run(parse_youmake_user(value))


def propagate(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap ``fn`` so it runs with the caller's current run bound (thread hops)."""
    run = get_current_run()

    @functools.wraps(fn)
    def _wrapped(*args: Any, **kwargs: Any) -> Any:
        token = set_current_run(run)
        try:
            return fn(*args, **kwargs)
        finally:
            reset_current_run(token)

    return _wrapped


def effective_identity() -> Tuple[str, Optional[str], Optional[YoumakeRun]]:
    """``(agent_slug, run_key, run)`` for the code currently executing.

    Bound run -> its slug and the whole ``user`` string.  Otherwise a kanban
    worker process (HERMES_PROFILE = assignee profile) -> that slug, no run.
    Otherwise ``hermes`` with no run key (the contract's default).
    """
    run = get_current_run()
    if run is not None:
        return run.slug, run.raw, run
    profile = (os.environ.get("HERMES_PROFILE") or "").strip()
    if profile and SLUG_RE.fullmatch(profile):
        return profile, None, None
    return DEFAULT_AGENT, None, None
