"""Youmake fork: computer-use permission (identity, decision, marker, gating).

Contract: yomake_v2 docs/hermes-computer-permission.md.  The regexes and the
marker expectations below are copied from yomake_v2
server/lib/computerPermission.js (nothing is imported from the server).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, patch

import pytest

import tools.youmake_computer_permission as perm
import tools.youmake_identity as ident

# --- fixtures copied from server/lib/computerPermission.js -----------------
SERVER_SLUG_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
SERVER_RUN_RE = re.compile(r"^youmake:[A-Za-z0-9-]{1,64}:[A-Za-z0-9_-]{1,64}:[A-Za-z0-9_-]{1,64}$")
SERVER_NEEDLE = '"computer_permission_required"'
SERVER_TOOL_LOG_RE = re.compile(r"agent\.tool_executor: tool (\w+) (completed)")

RUN = "youmake:3f1c2a9e-1111-2222-3333-444455556666:researcher:g2"
NOW = 1_800_000_000


def _server_extract(text: str):
    """Python port of createPermissionMarkerScanner + normalizeMarker."""
    out = []
    buf = text
    while True:
        key_at = buf.find(SERVER_NEEDLE)
        if key_at < 0:
            return out
        start = buf.rfind("{", 0, key_at)
        depth, in_str, end, i = 0, False, -1, start
        while i < len(buf):
            c = buf[i]
            if in_str:
                if c == "\\":
                    i += 1
                elif c == '"':
                    in_str = False
            elif c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
            i += 1
        assert end > 0, "marker object must be complete"
        obj = json.loads(buf[start:end])
        if obj.get("computer_permission_required") is True and SERVER_SLUG_RE.match(obj.get("agent", "")):
            out.append({
                "agent": obj["agent"],
                "reason": " ".join(str(obj.get("reason", "")).split())[:300],
                "run": obj["run"] if isinstance(obj.get("run"), str) and SERVER_RUN_RE.match(obj["run"]) else None,
            })
        buf = buf[end:]


@pytest.fixture(autouse=True)
def _clean_state():
    perm._last_host.clear()
    perm._logged_no_run.clear()
    token = ident.set_current_run(None)
    yield
    ident.reset_current_run(token)


def _write_perm(state) -> None:
    path = perm.permission_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(state if isinstance(state, str) else json.dumps(state), encoding="utf-8")


def _desktop(flag: bool = True) -> None:
    from hermes_cli.config import get_config_path

    get_config_path().write_text(
        f"browser:\n  cdp_url: http://127.0.0.1:9222\n  youmake_desktop: {'true' if flag else 'false'}\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


class TestIdentity:
    @pytest.mark.parametrize("raw,slug,lane", [
        (RUN, "researcher", "g2"),
        ("youmake:u1:writer:t0123456789ab", "writer", "t0123456789ab"),
        ("youmake:u1:lead_agent:synth", "lead_agent", "synth"),
    ])
    def test_valid(self, raw, slug, lane):
        run = ident.parse_youmake_user(raw)
        assert run is not None and run.raw == raw and run.slug == slug and run.lane == lane
        assert SERVER_RUN_RE.match(raw)

    @pytest.mark.parametrize("raw", [
        None, "", 42, "alice", "youmake:u1:researcher", "youmake:u1:researcher:g2:x",
        "youmake:u_1:researcher:g2",  # user id has no underscore
        "youmake:u1:re search:g2", "youmake:u1:researcher:g2\n", "Youmake:u1:a:g2",
        "youmake:" + "a" * 65 + ":x:g2", "youmake:u1::g2",
    ])
    def test_invalid(self, raw):
        assert ident.parse_youmake_user(raw) is None

    def test_effective_identity_default(self, monkeypatch):
        monkeypatch.delenv("HERMES_PROFILE", raising=False)
        assert ident.effective_identity() == ("hermes", None, None)

    def test_effective_identity_bound(self):
        ident.bind_request_user(RUN)
        slug, key, run = ident.effective_identity()
        assert (slug, key) == ("researcher", RUN) and run.lane == "g2"

    def test_kanban_profile_fallback(self, monkeypatch):
        monkeypatch.setenv("HERMES_PROFILE", "writer")
        assert ident.effective_identity()[:2] == ("writer", None)
        monkeypatch.setenv("HERMES_PROFILE", "bad name")
        assert ident.effective_identity()[:2] == ("hermes", None)

    def test_bind_invalid_clears_previous(self):
        ident.bind_request_user(RUN)
        ident.bind_request_user("not-youmake")
        assert ident.get_current_run() is None

    def test_plain_thread_pool_does_not_inherit_but_propagate_does(self):
        ident.bind_request_user(RUN)
        with ThreadPoolExecutor(max_workers=2) as pool:
            plain = pool.submit(ident.get_current_run).result()
            wrapped = pool.submit(ident.propagate(ident.get_current_run)).result()
            # nested hop (delegate batch worker -> per-child timeout executor)
            nested = pool.submit(ident.propagate(
                lambda: ThreadPoolExecutor(1).submit(ident.propagate(ident.get_current_run)).result()
            )).result()
        assert plain is None
        assert wrapped is not None and wrapped.raw == RUN
        assert nested is not None and nested.raw == RUN
        # the worker thread is reset afterwards
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(ident.get_current_run).result() is None

    def test_run_agent_executor_sees_request_run(self):
        """api_server._run_agent: identity reaches the agent thread (run_in_executor)."""
        from gateway.config import PlatformConfig
        from gateway.platforms.api_server import APIServerAdapter

        adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={}))
        seen = {}

        def _conv(**kw):
            seen["run"] = ident.get_current_run()
            seen["thread"] = threading.current_thread().name
            return {"final_response": "ok"}

        agent = MagicMock()
        agent.run_conversation.side_effect = _conv

        async def _go():
            ident.bind_request_user(RUN)
            with patch.object(adapter, "_create_agent", return_value=agent):
                await adapter._run_agent(user_message="hi", conversation_history=[], session_id="s1")

        asyncio.run(_go())
        assert seen["run"] is not None and seen["run"].raw == RUN

    def test_delegate_children_inherit(self):
        """delegate_task submits children through propagate()."""
        import inspect

        import tools.delegate_tool as dt

        src = inspect.getsource(dt)
        assert "_timeout_executor.submit(_ym_propagate(_run_with_thread_capture))" in src
        assert "_ym_propagate(_run_single_child)" in src


# ---------------------------------------------------------------------------
# Decision (contract section 2)
# ---------------------------------------------------------------------------


FUTURE = NOW + 600
PAST = NOW - 1


@pytest.mark.parametrize("state,agent,run,expected", [
    ({}, "researcher", RUN, "ask"),
    ({"mode": "ask"}, "researcher", None, "ask"),
    ({"mode": "allowed"}, "researcher", RUN, "allowed"),
    ({"mode": "denied"}, "researcher", RUN, "denied"),
    # mode denied beats a per-agent allow and grants
    ({"mode": "denied", "agents": {"researcher": "allowed"}, "once": {RUN: FUTURE}}, "researcher", RUN, "denied"),
    # per-agent denied beats mode allowed and grants
    ({"mode": "allowed", "agents": {"researcher": "denied"}}, "researcher", RUN, "denied"),
    ({"mode": "ask", "agents": {"researcher": "denied"}, "once": {RUN: FUTURE}}, "researcher", RUN, "denied"),
    # per-agent allowed
    ({"mode": "ask", "agents": {"researcher": "allowed"}}, "researcher", None, "allowed"),
    ({"mode": "ask", "agents": {"writer": "allowed"}}, "researcher", RUN, "ask"),
    # once grants
    ({"once": {RUN: FUTURE}}, "researcher", RUN, "allowed"),
    ({"once": {RUN: PAST}}, "researcher", RUN, "ask"),
    ({"once": {RUN: FUTURE}}, "researcher", None, "ask"),
    ({"once": {RUN: FUTURE}}, "researcher", "youmake:u1:researcher:g3", "ask"),
    ({"once": {"agent:researcher": FUTURE}}, "researcher", None, "allowed"),
    ({"once": {"agent:researcher": FUTURE}}, "writer", None, "ask"),
    ({"once": {"agent:researcher": str(FUTURE)}}, "researcher", None, "allowed"),
    ({"once": {"agent:researcher": "soon"}}, "researcher", None, "ask"),
    # junk is normalised away
    ({"mode": "yes", "agents": {"researcher": "maybe"}}, "researcher", RUN, "ask"),
])
def test_decision_table(state, agent, run, expected):
    assert perm.decide(state, agent, run, now=NOW) == expected


class TestFile:
    def test_missing_file_is_ask(self):
        state, status = perm.read_state()
        assert status == "missing" and state["mode"] == "ask" and state["once"] == {}

    @pytest.mark.parametrize("text", ["{not json", "[]", "null", '"allowed"', ""])
    def test_corrupt_file_is_ask(self, text):
        _write_perm(text)
        state, status = perm.read_state()
        assert status == "invalid" and state["mode"] == "ask"

    def test_reread_every_call(self):
        _write_perm({"mode": "allowed"})
        assert perm.read_state()[0]["mode"] == "allowed"
        _write_perm({"mode": "denied"})
        assert perm.read_state()[0]["mode"] == "denied"

    def test_path_is_root_home_desktop(self):
        from hermes_constants import get_default_hermes_root

        assert perm.permission_file_path() == get_default_hermes_root() / "desktop" / "permissions.json"


# ---------------------------------------------------------------------------
# Marker
# ---------------------------------------------------------------------------


class TestMarker:
    def test_exact_format(self):
        assert perm.marker_json("researcher", "open um6p.ma", RUN) == (
            '{"computer_permission_required": true, "agent": "researcher", '
            '"reason": "open um6p.ma", "run": "' + RUN + '"}'
        )

    def test_run_omitted_when_unknown(self):
        assert perm.marker_json("hermes", "use the computer", None) == (
            '{"computer_permission_required": true, "agent": "hermes", "reason": "use the computer"}'
        )

    def test_reason_collapsed_and_capped(self):
        obj = json.loads(perm.marker_json("a", "x  \n y" + "z" * 400, None))
        assert obj["reason"].startswith("x y") and len(obj["reason"]) == 300

    def test_server_parses_tool_result_and_log_line(self):
        _desktop()
        ident.bind_request_user(RUN)
        with patch.object(perm, "_desktop_active", return_value=True):
            result = perm.evaluate("navigate", "t1", "https://www.um6p.ma/fr/x?y=1")
        assert result.split("\n", 1)[0] == perm.marker_json("researcher", "open um6p.ma", RUN)
        assert _server_extract(result) == [{"agent": "researcher", "reason": "open um6p.ma", "run": RUN}]
        log_line = f"2026-11-14 10:00:00,000 WARNING youmake.computer_permission: {perm.marker_json('researcher', 'open um6p.ma', RUN)}"
        assert _server_extract(log_line) == [{"agent": "researcher", "reason": "open um6p.ma", "run": RUN}]
        # the server's activity-trace regex must not mistake it for a tool line
        assert not SERVER_TOOL_LOG_RE.search(log_line)


# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------


class TestGate:
    def test_no_desktop_mode_no_gate(self):
        _desktop(False)
        assert perm.gate_tool_call("browser_navigate", {"url": "https://a.com"}, "t") is None

    def test_ask_blocks_and_logs_once_per_reason(self, caplog):
        _desktop()
        ident.bind_request_user(RUN)
        caplog.set_level(logging.WARNING, logger="youmake.computer_permission")
        with patch.object(perm, "_desktop_active", return_value=True):
            r1 = perm.gate_tool_call("browser_navigate", {"url": "https://um6p.ma"}, "t")
            r2 = perm.gate_tool_call("browser_navigate", {"url": "https://um6p.ma/about"}, "t")
            r3 = perm.gate_tool_call("browser_click", {"ref": "@e1"}, "t")
            r4 = perm.gate_tool_call("browser_snapshot", {}, "t")
        assert all(r and r.startswith('{"computer_permission_required": true') for r in (r1, r2, r3, r4))
        assert "web_extract" in r1 and "wait" in r1
        records = [r.getMessage() for r in caplog.records if r.name == "youmake.computer_permission"]
        reasons = [json.loads(m)["reason"] for m in records]
        assert reasons == ["open um6p.ma", "use the browser on um6p.ma", "use the computer"]
        assert all(r.levelname == "WARNING" for r in caplog.records if r.name == "youmake.computer_permission")

    def test_denied_result_no_log(self, caplog):
        _desktop()
        _write_perm({"mode": "denied"})
        ident.bind_request_user(RUN)
        caplog.set_level(logging.WARNING, logger="youmake.computer_permission")
        with patch.object(perm, "_desktop_active", return_value=True):
            r = perm.gate_tool_call("browser_navigate", {"url": "https://um6p.ma"}, "t")
        assert r.split("\n", 1)[0] == '{"computer_permission_denied": true, "agent": "researcher"}'
        assert "computer_permission_required" not in r
        assert not [x for x in caplog.records if x.name == "youmake.computer_permission"]

    def test_allowed_passes_without_probing_desktop(self):
        _desktop()
        _write_perm({"mode": "ask", "once": {RUN: int(time.time()) + 600}})
        ident.bind_request_user(RUN)
        with patch.object(perm, "_desktop_active") as active:
            assert perm.gate_tool_call("browser_navigate", {"url": "https://um6p.ma"}, "t") is None
        active.assert_not_called()

    def test_headless_fallback_not_gated(self):
        _desktop()
        with patch.object(perm, "_desktop_active", return_value=False):
            assert perm.gate_tool_call("browser_navigate", {"url": "https://a.com"}, "t") is None

    def test_no_identity_uses_hermes(self, monkeypatch):
        monkeypatch.delenv("HERMES_PROFILE", raising=False)
        _desktop()
        with patch.object(perm, "_desktop_active", return_value=True):
            r = perm.gate_tool_call("browser_navigate", {"url": "https://a.com"}, "t")
        assert json.loads(r.split("\n", 1)[0]) == {
            "computer_permission_required": True, "agent": "hermes", "reason": "open a.com",
        }

    def test_registry_dispatch_returns_marker_without_running_handler(self):
        import tools.browser_tool  # noqa: F401  (registers browser_*)
        from tools.registry import registry

        _desktop()
        ident.bind_request_user(RUN)
        entry = registry.get_entry("browser_navigate")
        if entry is None:
            pytest.skip("browser tools not registered")
        with patch.object(perm, "_desktop_active", return_value=True), \
                patch.object(entry, "handler") as handler:
            out = registry.dispatch("browser_navigate", {"url": "https://um6p.ma"}, task_id="t")
        handler.assert_not_called()
        assert _server_extract(out) == [{"agent": "researcher", "reason": "open um6p.ma", "run": RUN}]

    def test_run_browser_command_does_not_spawn_when_blocked(self):
        import tools.browser_tool as bt

        _desktop()
        ident.bind_request_user(RUN)
        with patch.object(perm, "_desktop_active", return_value=True), \
                patch.object(bt.subprocess, "Popen") as popen, \
                patch.object(bt, "_get_session_info") as sess, \
                patch.object(bt, "_find_agent_browser", return_value="agent-browser"):
            result = bt._run_browser_command("t", "open", ["https://um6p.ma"])
        popen.assert_not_called()
        sess.assert_not_called()
        assert result["success"] is False
        assert _server_extract(result["error"])[0]["reason"] == "open um6p.ma"

    def test_close_never_gated(self):
        _desktop()
        with patch.object(perm, "_desktop_active", return_value=True):
            assert perm.gate_browser_command("t", "close", []) is None

    def test_cdp_tool_gated(self):
        _desktop()
        with patch.object(perm, "_desktop_active", return_value=True):
            r = perm.gate_tool_call("browser_cdp", {"method": "Page.navigate"}, "t")
        assert json.loads(r.split("\n", 1)[0])["reason"] == "use the computer"

    def test_cli_permission_help_and_json(self, capsys):
        import argparse

        from hermes_cli.youmake_desktop import register_cli

        parser = argparse.ArgumentParser(prog="hermes desktop")
        register_cli(parser)
        with pytest.raises(SystemExit) as exc:
            parser.parse_args(["permission", "--help"])
        assert exc.value.code == 0
        capsys.readouterr()
        _write_perm({"mode": "ask", "agents": {"researcher": "allowed"}})
        args = parser.parse_args(["permission", "check", "--run", RUN, "--json"])
        with pytest.raises(SystemExit) as exc:
            args.func(args)
        assert exc.value.code == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["agent"] == "researcher" and payload["decision"] == "allowed"
        assert payload["enforced"] is True and payload["file_status"] == "ok"
