"""Tests for the Youmake ``hermes desktop`` module (pure-Python parts only).

No apt, systemd or Chromium is touched: every subprocess goes through a fake
runner.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from hermes_cli import youmake_desktop as yd

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes / systemd")


def _ctx(tmp_path: Path, *, wm: bool = True, novnc: str | None = "/usr/share/novnc") -> yd.UnitContext:
    profile = tmp_path / "desktop" / "chromium-profile"
    return yd.UnitContext(
        user="claw",
        group="claw",
        home="/home/claw",
        chrome_path="/opt/google/chrome/chrome",
        chrome_flags=yd.chrome_flags(profile, no_sandbox=False),
        fluxbox_bin="/usr/bin/fluxbox" if wm else None,
        novnc_dir=novnc,
        bin_dir=str(tmp_path / "desktop" / "bin"),
        profile_dir=str(profile),
        token_file=str(tmp_path / "desktop" / "vnc-tokens"),
    )


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------


class TestTokens:
    def test_generate_tokens_distinct_and_long(self):
        t = yd.generate_tokens()
        assert set(t) == {"view", "control"}
        assert t["view"] != t["control"]
        assert len(t["view"]) >= 40 and len(t["control"]) >= 40

    def test_token_file_maps_to_loopback_vnc_ports(self):
        content = yd.render_token_file({"view": "VVV", "control": "CCC"})
        assert "VVV: 127.0.0.1:5901" in content
        assert "CCC: 127.0.0.1:5902" in content

    @posix_only
    def test_token_file_is_0600(self, tmp_path):
        path = yd.write_token_file({"view": "a", "control": "b"}, tmp_path / "d" / "tokens")
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        # overwrite keeps the mode and leaves no temp files behind
        yd.write_token_file({"view": "c", "control": "d"}, path)
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert [p.name for p in path.parent.iterdir()] == ["tokens"]

    @posix_only
    def test_state_round_trip_private(self):
        yd.save_state({"tokens": {"view": "x", "control": "y"}})
        assert yd.load_state()["tokens"]["view"] == "x"
        assert stat.S_IMODE(yd.state_file_path().stat().st_mode) == 0o600

    def test_load_state_tolerates_garbage(self):
        yd.state_file_path().parent.mkdir(parents=True, exist_ok=True)
        yd.state_file_path().write_text("not json", encoding="utf-8")
        assert yd.load_state() == {}


# ---------------------------------------------------------------------------
# Unit rendering
# ---------------------------------------------------------------------------


class TestRenderUnits:
    def test_renders_all_units_without_leftover_placeholders(self, tmp_path):
        units = yd.render_units(_ctx(tmp_path))
        assert set(units) == {
            yd.TARGET_UNIT, yd.UNIT_XVFB, yd.UNIT_WM, yd.UNIT_CHROMIUM,
            yd.UNIT_VNC_VIEW, yd.UNIT_VNC_CONTROL, yd.UNIT_WEBSOCKIFY,
        }
        for name, text in units.items():
            assert "${" not in text, name
            assert "User=claw" in text or name == yd.TARGET_UNIT

    def test_nothing_listens_publicly(self, tmp_path):
        units = yd.render_units(_ctx(tmp_path))
        assert "-nolisten tcp" in units[yd.UNIT_XVFB]
        for name in (yd.UNIT_VNC_VIEW, yd.UNIT_VNC_CONTROL):
            assert " -localhost " in units[name]
        assert "127.0.0.1:6080" in units[yd.UNIT_WEBSOCKIFY]
        assert "--remote-debugging-address=127.0.0.1" in units[yd.UNIT_CHROMIUM]
        assert "0.0.0.0" not in "".join(units.values())

    def test_view_is_viewonly_and_control_is_not(self, tmp_path):
        units = yd.render_units(_ctx(tmp_path))
        assert "-viewonly" in units[yd.UNIT_VNC_VIEW]
        assert "-rfbport 5901" in units[yd.UNIT_VNC_VIEW]
        assert "-viewonly" not in units[yd.UNIT_VNC_CONTROL]
        assert "-rfbport 5902" in units[yd.UNIT_VNC_CONTROL]
        for name in (yd.UNIT_VNC_VIEW, yd.UNIT_VNC_CONTROL):
            assert "-shared -forever -ncache 0" in units[name]

    def test_target_does_not_pull_in_control_vnc(self, tmp_path):
        target = yd.render_units(_ctx(tmp_path))[yd.TARGET_UNIT]
        assert yd.UNIT_VNC_CONTROL not in target
        assert yd.UNIT_CHROMIUM in target and yd.UNIT_WEBSOCKIFY in target

    def test_chromium_flags_and_memory_limits(self, tmp_path):
        chromium = yd.render_units(_ctx(tmp_path))[yd.UNIT_CHROMIUM]
        assert "--remote-debugging-port=9222" in chromium
        assert "--user-data-dir=" + str(tmp_path / "desktop" / "chromium-profile") in chromium
        assert "--no-first-run" in chromium and "--no-default-browser-check" in chromium
        assert "Environment=DISPLAY=:1" in chromium
        assert "MemoryMax=" in chromium
        assert "--no-sandbox" not in chromium

    def test_without_wm(self, tmp_path):
        units = yd.render_units(_ctx(tmp_path, wm=False))
        assert yd.UNIT_WM not in units
        assert yd.UNIT_WM not in units[yd.TARGET_UNIT]
        assert yd.UNIT_WM not in units[yd.UNIT_CHROMIUM]

    def test_websockify_token_plugin_and_web(self, tmp_path):
        ws = yd.render_units(_ctx(tmp_path))[yd.UNIT_WEBSOCKIFY]
        assert "--token-plugin TokenFile" in ws
        assert "--token-source " + str(tmp_path / "desktop" / "vnc-tokens") in ws
        assert "--web /usr/share/novnc" in ws
        no_web = yd.render_units(_ctx(tmp_path, novnc=None))[yd.UNIT_WEBSOCKIFY]
        assert "--web" not in no_web

    def test_paths_with_spaces_are_quoted(self, tmp_path):
        ctx = _ctx(tmp_path)
        ctx.chrome_path = "/opt/My Chrome/chrome"
        chromium = yd.render_units(ctx)[yd.UNIT_CHROMIUM]
        assert 'ExecStart="/opt/My Chrome/chrome" ' in chromium

    def test_systemd_quote_escapes_specifiers(self):
        assert yd._systemd_quote("plain") == "plain"
        assert yd._systemd_quote("a%b") == '"a%%b"'
        assert yd._systemd_quote("a$b") == '"a$$b"'


# ---------------------------------------------------------------------------
# Chromium discovery
# ---------------------------------------------------------------------------


@posix_only
class TestFindChrome:
    def _exe(self, path: Path, content: bytes = b"\x7fELF") -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        path.chmod(0o755)
        return path

    def test_prefers_agent_browser_chrome_for_testing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(yd.shutil, "which", lambda name: None)
        monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)
        cft = self._exe(tmp_path / ".agent-browser" / "browsers" / "chrome-131.0" / "chrome-linux64" / "chrome")
        self._exe(tmp_path / ".cache" / "ms-playwright" / "chromium-1200" / "chrome-linux" / "chrome")
        assert yd.find_chrome(home=str(tmp_path)) == str(cft)

    def test_explicit_path_wins(self, tmp_path, monkeypatch):
        monkeypatch.setattr(yd.shutil, "which", lambda name: None)
        explicit = self._exe(tmp_path / "custom" / "chrome")
        self._exe(tmp_path / ".agent-browser" / "browsers" / "chrome-1" / "chrome-linux64" / "chrome")
        assert yd.find_chrome(home=str(tmp_path), extra=[str(explicit)]) == str(explicit)

    def test_skips_snap_wrapper(self, tmp_path, monkeypatch):
        wrapper = self._exe(tmp_path / "bin" / "chromium-browser", b"#!/bin/sh\nexec /snap/bin/chromium \"$@\"\n")
        monkeypatch.setattr(yd.shutil, "which", lambda name: str(wrapper) if name == "chromium-browser" else None)
        monkeypatch.setattr(yd.os.path, "isfile", lambda p: p == str(wrapper))
        assert yd.find_chrome(home=str(tmp_path / "nohome")) is None

    def test_no_sandbox_rules(self, monkeypatch):
        monkeypatch.setattr(yd.os, "geteuid", lambda: 0)
        assert yd.needs_no_sandbox("/opt/google/chrome/chrome") is True
        monkeypatch.setattr(yd.os, "geteuid", lambda: 1000)
        assert yd.needs_no_sandbox("/opt/google/chrome/chrome") is False


# ---------------------------------------------------------------------------
# Config wiring
# ---------------------------------------------------------------------------


def _config() -> dict:
    from hermes_cli.config import get_config_path

    p = get_config_path()
    return yaml.safe_load(p.read_text(encoding="utf-8")) if p.exists() else {}


class TestConfigOverride:
    def test_enable_preserves_other_settings(self):
        from hermes_cli.config import get_config_path

        get_config_path().write_text("model: foo\nbrowser:\n  command_timeout: 45\n", encoding="utf-8")
        yd.enable_cdp_override()
        cfg = _config()
        assert cfg["model"] == "foo"
        assert cfg["browser"]["command_timeout"] == 45
        assert cfg["browser"]["cdp_url"] == "http://127.0.0.1:9222"
        assert cfg["browser"]["youmake_desktop"] is True

    def test_enable_is_read_by_browser_tool(self, monkeypatch):
        import tools.browser_tool as bt

        monkeypatch.delenv("BROWSER_CDP_URL", raising=False)
        yd.enable_cdp_override()
        seen = {}
        monkeypatch.setattr(bt, "_resolve_cdp_override", lambda raw: seen.setdefault("raw", raw) and "ws://127.0.0.1:9222/devtools/browser/x")
        assert bt._get_cdp_override() == "ws://127.0.0.1:9222/devtools/browser/x"
        assert seen["raw"] == "http://127.0.0.1:9222"

    def test_disable_removes_only_our_override(self):
        yd.enable_cdp_override()
        assert yd.disable_cdp_override() is True
        assert "browser" not in _config()
        assert yd.disable_cdp_override() is False

    def test_disable_leaves_foreign_url(self):
        from hermes_cli.config import get_config_path

        get_config_path().write_text("browser:\n  cdp_url: ws://other:9333\n", encoding="utf-8")
        yd.disable_cdp_override()
        assert _config()["browser"]["cdp_url"] == "ws://other:9333"

    def test_disable_removes_matching_env_value(self):
        from hermes_cli.config import get_env_value, save_env_value

        save_env_value("BROWSER_CDP_URL", "http://127.0.0.1:9222")
        assert yd.disable_cdp_override() is True
        assert not get_env_value("BROWSER_CDP_URL")
        assert "BROWSER_CDP_URL" not in os.environ

    def test_conflicting_env_override(self, monkeypatch):
        monkeypatch.setenv("BROWSER_CDP_URL", "ws://elsewhere:1")
        assert yd.conflicting_env_override() == "ws://elsewhere:1"
        monkeypatch.setenv("BROWSER_CDP_URL", "http://127.0.0.1:9222/")
        assert yd.conflicting_env_override() is None


# ---------------------------------------------------------------------------
# Commands with a fake runner
# ---------------------------------------------------------------------------


class FakeRunner(yd.Runner):
    def __init__(self, active=None):
        self.calls = []
        self.is_root = False
        self.active = active or {}
        self._run = lambda *a, **k: SimpleNamespace(returncode=0, stdout="Chromium 131", stderr="")

    def run(self, cmd, *, check=False, timeout=120, env=None):
        self.calls.append(list(cmd))
        if cmd[:2] == ["systemctl", "is-active"]:
            return SimpleNamespace(returncode=0, stdout=self.active.get(cmd[2], "inactive"), stderr="")
        if cmd[:2] == ["systemctl", "show"]:
            return SimpleNamespace(returncode=0, stdout="104857600", stderr="")
        if cmd[0] == "dpkg-query":
            return SimpleNamespace(returncode=0, stdout="install ok installed", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")


def _ns(**kw):
    base = {"json": True, "yes": True, "no_wm": False, "chrome": "", "wait": 0.0, "state": "on"}
    base.update(kw)
    return argparse.Namespace(**base)


class TestCommands:
    def test_sudo_prefix(self):
        r = FakeRunner()
        r.systemctl("start", "x")
        assert r.calls[-1] == ["sudo", "-n", "systemctl", "start", "x"]
        r.is_root = True
        r.systemctl("start", "x")
        assert r.calls[-1] == ["systemctl", "start", "x"]

    def test_control_on_off(self, capsys):
        r = FakeRunner(active={yd.UNIT_VNC_CONTROL: "active"})
        assert yd.cmd_control(_ns(state="on"), r) == 0
        assert ["sudo", "-n", "systemctl", "start", yd.UNIT_VNC_CONTROL] in r.calls
        assert json.loads(capsys.readouterr().out)["control_enabled"] is True
        r.active = {}
        assert yd.cmd_control(_ns(state="off"), r) == 0
        assert ["sudo", "-n", "systemctl", "stop", yd.UNIT_VNC_CONTROL] in r.calls

    @posix_only
    def test_rotate_tokens(self, capsys):
        yd.save_state({"tokens": {"view": "old-v", "control": "old-c"}, "chrome_path": "/x"})
        r = FakeRunner(active={yd.UNIT_WEBSOCKIFY: "active"})
        assert yd.cmd_rotate_tokens(_ns(), r) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["view_token"] != "old-v"
        state = yd.load_state()
        assert state["tokens"]["view"] == out["view_token"]
        assert state["chrome_path"] == "/x"
        assert out["view_token"] in yd.token_file_path().read_text(encoding="utf-8")
        assert ["sudo", "-n", "systemctl", "restart", yd.UNIT_WEBSOCKIFY] in r.calls

    def test_status_json(self, capsys, monkeypatch):
        monkeypatch.setattr(yd, "cdp_version", lambda url=yd.CDP_URL, timeout=2.0: {"Browser": "Chrome/131"})
        active = {u: "active" for u in yd.ALL_SERVICES if u != yd.UNIT_VNC_CONTROL}
        assert yd.cmd_status(_ns(), FakeRunner(active=active)) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "running"
        assert out["cdp"]["browser"] == "Chrome/131"
        assert out["control_enabled"] is False
        assert out["memory"]["desktop_bytes"] == 5 * 104857600

    def test_status_reports_down_cdp(self, capsys, monkeypatch):
        monkeypatch.setattr(yd, "cdp_version", lambda url=yd.CDP_URL, timeout=2.0: None)
        assert yd.cmd_status(_ns(), FakeRunner()) == 1
        assert json.loads(capsys.readouterr().out)["status"] == "stopped"

    def test_disable(self, capsys):
        yd.enable_cdp_override()
        r = FakeRunner()
        assert yd.cmd_disable(_ns(), r) == 0
        assert ["sudo", "-n", "systemctl", "disable", "--now", yd.TARGET_UNIT] in r.calls
        assert json.loads(capsys.readouterr().out)["cdp_override_removed"] is True
        assert "browser" not in _config()

    @posix_only
    def test_setup_end_to_end_with_fakes(self, capsys, monkeypatch, tmp_path):
        monkeypatch.setattr(yd, "_preflight", lambda runner: None)
        monkeypatch.setattr(yd, "find_chrome", lambda extra=(): "/opt/google/chrome/chrome")
        monkeypatch.setattr(yd, "SYSTEMD_DIR", tmp_path / "systemd")
        monkeypatch.setattr(yd, "wait_for_cdp", lambda url, timeout: {"Browser": "Chrome/131.0"})
        monkeypatch.setattr(yd, "_current_user", lambda: {"user": "claw", "group": "claw", "home": "/home/claw"})
        r = FakeRunner()
        assert yd.cmd_setup(_ns(), r) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "running"
        assert out["websockify_port"] == 6080
        assert out["display_size"] == "1280x800x24"
        assert out["chromium_version"] == "Chrome/131.0"
        assert out["view_token"] and out["control_token"]
        installs = [c for c in r.calls if "install" in c and "-m" in c]
        assert len(installs) == 7
        assert ["sudo", "-n", "systemctl", "enable", yd.TARGET_UNIT] in r.calls
        assert (yd.bin_dir() / "wait-for-display.sh").exists()
        assert _config()["browser"]["cdp_url"] == yd.CDP_URL

        # Idempotent: a second run keeps the tokens.
        r2 = FakeRunner()
        assert yd.cmd_setup(_ns(), r2) == 0
        out2 = json.loads(capsys.readouterr().out)
        assert out2["view_token"] == out["view_token"]
        assert not any("apt-get" in c for call in r2.calls for c in call)

    def test_setup_refuses_missing_packages_without_yes(self, monkeypatch):
        monkeypatch.setattr(yd, "_preflight", lambda runner: None)
        monkeypatch.setattr(yd, "missing_packages", lambda runner, pkgs: ["x11vnc"])
        monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
        with pytest.raises(yd.DesktopError, match="--yes"):
            yd.cmd_setup(_ns(yes=False), FakeRunner())

    def test_cli_wiring(self):
        parser = argparse.ArgumentParser()
        yd.register_cli(parser)
        args = parser.parse_args(["control", "off", "--json"])
        assert args.state == "off" and args.json is True
        args = parser.parse_args(["setup", "--yes", "--no-wm"])
        assert args.yes and args.no_wm


def test_assets_present_for_packaging():
    names = {p.name for p in yd.ASSETS_DIR.iterdir()}
    assert "wait-for-display.sh" in names
    assert yd.TARGET_UNIT in names and yd.UNIT_CHROMIUM in names


def test_package_data_declares_assets():
    import tomllib

    root = Path(__file__).resolve().parents[2]
    data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    assert "desktop_assets/*" in data["tool"]["setuptools"]["package-data"]["hermes_cli"]
