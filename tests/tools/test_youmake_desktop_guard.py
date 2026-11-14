"""Tests for the Youmake desktop CDP fallback + attach guard."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import tools.youmake_desktop_guard as guard

RAW = "http://127.0.0.1:9222"
WS = "ws://127.0.0.1:9222/devtools/browser/abc"


def _enable(flag: bool = True, cdp: str = RAW):
    from hermes_cli.config import get_config_path

    get_config_path().write_text(
        f"browser:\n  cdp_url: {cdp}\n  youmake_desktop: {'true' if flag else 'false'}\n",
        encoding="utf-8",
    )


class TestFallback:
    def test_noop_without_flag(self):
        _enable(flag=False)
        with patch.object(guard, "_endpoint_alive", return_value=False) as alive:
            assert guard.apply_fallback(RAW, RAW) == RAW
        alive.assert_not_called()

    def test_resolved_websocket_means_alive(self):
        _enable()
        with patch.object(guard, "_endpoint_alive") as alive:
            assert guard.apply_fallback(RAW, WS) == WS
        alive.assert_not_called()

    def test_dead_endpoint_falls_back_to_local(self, caplog):
        _enable()
        guard._last_warn = 0.0
        with patch.object(guard, "_endpoint_alive", return_value=False):
            assert guard.apply_fallback(RAW, RAW) == ""
        assert "not answering" in caplog.text

    def test_alive_raw_kept(self):
        _enable()
        with patch.object(guard, "_endpoint_alive", return_value=True):
            assert guard.apply_fallback(WS, WS) == WS

    def test_browser_tool_uses_local_when_desktop_down(self, monkeypatch):
        import tools.browser_tool as bt

        monkeypatch.delenv("BROWSER_CDP_URL", raising=False)
        _enable()
        # discovery fails -> _resolve_cdp_override returns the raw URL
        monkeypatch.setattr(bt, "_resolve_cdp_override", lambda raw: raw)
        with patch.object(guard, "_endpoint_alive", return_value=False):
            assert bt._get_cdp_override() == ""
            monkeypatch.setattr(bt, "_get_cloud_provider", lambda: None)
            assert bt._is_local_mode() is True

    def test_browser_tool_upstream_behaviour_without_flag(self, monkeypatch):
        import tools.browser_tool as bt

        monkeypatch.delenv("BROWSER_CDP_URL", raising=False)
        _enable(flag=False)
        monkeypatch.setattr(bt, "_resolve_cdp_override", lambda raw: raw)
        assert bt._get_cdp_override() == RAW

    def test_no_override_untouched(self, monkeypatch):
        import tools.browser_tool as bt

        monkeypatch.delenv("BROWSER_CDP_URL", raising=False)
        with patch.object(guard, "apply_fallback") as fb:
            assert bt._get_cdp_override() == ""
        fb.assert_not_called()


class TestAttachMismatch:
    def test_match(self):
        out = '{"success":true,"data":{"cdpUrl":"ws://127.0.0.1:9222/devtools/browser/abc"}}'
        assert guard.attach_mismatch(WS, out) is None

    def test_localhost_alias_matches(self):
        assert guard.attach_mismatch(WS, "ws://localhost:9222/devtools/browser/abc") is None

    def test_launched_own_browser(self):
        out = '{"success":true,"data":{"url":"ws://127.0.0.1:41233/devtools/browser/zzz"}}'
        assert guard.attach_mismatch(WS, out) == "ws://127.0.0.1:41233/devtools/browser/zzz"

    def test_same_port_other_instance(self):
        assert guard.attach_mismatch(WS, "ws://127.0.0.1:9222/devtools/browser/other")

    def test_inconclusive(self):
        assert guard.attach_mismatch(WS, '{"success":false,"error":"unknown command"}') is None
        assert guard.attach_mismatch(WS, "") is None

    def test_verify_attach_reports_error(self):
        proc = SimpleNamespace(stdout="ws://127.0.0.1:5555/devtools/browser/q", stderr="", returncode=0)
        with patch.object(guard.subprocess, "run", return_value=proc) as run:
            err = guard.verify_attach(["agent-browser"], WS, {})
        assert err and "its own browser" in err
        assert run.call_args[0][0] == ["agent-browser", "--cdp", WS, "--json", "get", "cdp-url"]

    def test_verify_attach_swallow_failures(self):
        with patch.object(guard.subprocess, "run", side_effect=OSError("nope")):
            assert guard.verify_attach(["agent-browser"], WS, {}) is None


class TestBrowserToolGuardHook:
    def _session(self, **extra):
        info = {"session_name": "cdp_x", "cdp_url": WS, "features": {"cdp_override": True}}
        info.update(extra)
        return info

    def test_mismatch_turns_success_into_error_once(self):
        import tools.browser_tool as bt

        _enable()
        session = self._session()
        with patch("tools.youmake_desktop_guard.verify_attach", return_value="boom") as v, \
                patch.object(bt, "cleanup_browser") as cleanup:
            res = bt._youmake_desktop_attach_guard("t1", session, "open", {"success": True}, ["ab"])
        assert res == {"success": False, "error": "boom"}
        cleanup.assert_called_once_with("t1")
        v.assert_called_once()

    def test_checked_only_once(self):
        import tools.browser_tool as bt

        _enable()
        session = self._session()
        with patch("tools.youmake_desktop_guard.verify_attach", return_value=None) as v:
            bt._youmake_desktop_attach_guard("t1", session, "open", {"success": True}, ["ab"])
            bt._youmake_desktop_attach_guard("t1", session, "snapshot", {"success": True}, ["ab"])
        assert v.call_count == 1

    def test_skipped_without_flag_or_for_local_sessions(self):
        import tools.browser_tool as bt

        _enable(flag=False)
        with patch("tools.youmake_desktop_guard.verify_attach") as v:
            bt._youmake_desktop_attach_guard("t", self._session(), "open", {"success": True}, ["ab"])
            _enable()
            local = {"session_name": "h_1", "cdp_url": None, "features": {"local": True}}
            bt._youmake_desktop_attach_guard("t", local, "open", {"success": True}, ["ab"])
            bt._youmake_desktop_attach_guard("t", self._session(), "open", {"success": False}, ["ab"])
        v.assert_not_called()
