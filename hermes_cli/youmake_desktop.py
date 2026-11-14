"""Youmake fork: opt-in virtual desktop for a Hermes VM (``hermes desktop``).

Runs ONE persistent headful Chromium on an Xvfb display so a human can watch
the agent's browser live (view-only VNC) and take control (interactive VNC),
both proxied through a loopback-only websockify with token auth.  Hermes's
browser tool attaches to that Chromium through the existing CDP override
(``browser.cdp_url`` in config.yaml), so no browser-tool code path changes
when the desktop is not set up.

Layout (all loopback, nothing on a public interface)::

    Xvfb :1 (unix socket only)
      +-- fluxbox (optional)
      +-- chromium --remote-debugging-address=127.0.0.1 --remote-debugging-port=9222
      +-- x11vnc -viewonly  127.0.0.1:5901   (always on)
      +-- x11vnc            127.0.0.1:5902   (only during takeover)
    websockify 127.0.0.1:6080  --token-plugin TokenFile  (+ noVNC static files)

Everything is managed through systemd system units (the Hermes user has
passwordless sudo on Youmake VMs) grouped under ``youmake-desktop.target``.
Unit templates live in ``hermes_cli/desktop_assets`` and ship as package data.

Kept in its own module so upstream merges only touch a couple of lines in
``hermes_cli/main.py``.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import platform
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from string import Template
from typing import Any, Callable, Dict, List, Optional, Sequence

from hermes_constants import get_hermes_home

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ASSETS_DIR = Path(__file__).resolve().parent / "desktop_assets"

DISPLAY_NUM = 1
DISPLAY = f":{DISPLAY_NUM}"
SCREEN_WIDTH = 1280
SCREEN_HEIGHT = 800
SCREEN_DEPTH = 24
SCREEN = f"{SCREEN_WIDTH}x{SCREEN_HEIGHT}x{SCREEN_DEPTH}"

CDP_HOST = "127.0.0.1"
CDP_PORT = 9222
CDP_URL = f"http://{CDP_HOST}:{CDP_PORT}"

VNC_VIEW_PORT = 5901
VNC_CONTROL_PORT = 5902
WS_HOST = "127.0.0.1"
WS_PORT = 6080

CHROMIUM_MEMORY_HIGH = "1200M"
CHROMIUM_MEMORY_MAX = "1600M"

TARGET_UNIT = "youmake-desktop.target"
UNIT_XVFB = "youmake-desktop-xvfb.service"
UNIT_WM = "youmake-desktop-wm.service"
UNIT_CHROMIUM = "youmake-desktop-chromium.service"
UNIT_VNC_VIEW = "youmake-desktop-vnc-view.service"
UNIT_VNC_CONTROL = "youmake-desktop-vnc-control.service"
UNIT_WEBSOCKIFY = "youmake-desktop-websockify.service"
ALL_SERVICES = (UNIT_XVFB, UNIT_WM, UNIT_CHROMIUM, UNIT_VNC_VIEW, UNIT_VNC_CONTROL, UNIT_WEBSOCKIFY)

SYSTEMD_DIR = Path("/etc/systemd/system")

# Config keys (config.yaml) written by setup and removed by disable.
CONFIG_CDP_KEY = "cdp_url"
CONFIG_DESKTOP_FLAG = "youmake_desktop"

APT_PACKAGES_BASE = ("xvfb", "x11vnc", "websockify", "novnc", "fonts-liberation")
APT_PACKAGE_WM = "fluxbox"
NOVNC_DIRS = ("/usr/share/novnc",)

GOOGLE_CHROME_DEB_URL = "https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb"
GOOGLE_CHROME_PATH = "/opt/google/chrome/chrome"

# Chromium flags tuned for a 2 vCPU / 4 GB VM shared with the agent.
BASE_CHROME_FLAGS = (
    f"--remote-debugging-address={CDP_HOST}",
    f"--remote-debugging-port={CDP_PORT}",
    "--no-first-run",
    "--no-default-browser-check",
    "--password-store=basic",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--disable-background-networking",
    "--disable-component-update",
    "--disable-sync",
    "--disable-breakpad",
    "--disable-features=Translate,MediaRouter,OptimizationHints,InterestFeedContentSuggestions",
    "--renderer-process-limit=4",
    "--disk-cache-size=67108864",
    "--hide-crash-restore-bubble",
    "--window-position=0,0",
    f"--window-size={SCREEN_WIDTH},{SCREEN_HEIGHT}",
    "--start-maximized",
)


class DesktopError(RuntimeError):
    """User-facing failure of a desktop command."""


# ---------------------------------------------------------------------------
# Paths and state
# ---------------------------------------------------------------------------


def desktop_dir() -> Path:
    return get_hermes_home() / "desktop"


def token_file_path() -> Path:
    return desktop_dir() / "vnc-tokens"


def state_file_path() -> Path:
    return desktop_dir() / "state.json"


def bin_dir() -> Path:
    return desktop_dir() / "bin"


def profile_dir() -> Path:
    return desktop_dir() / "chromium-profile"


def _write_private(path: Path, content: str, mode: int = 0o600) -> None:
    """Atomically write ``content`` to ``path`` with ``mode`` (never world-readable)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        os.chmod(path, mode)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_state() -> Dict[str, Any]:
    try:
        data = json.loads(state_file_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(state: Dict[str, Any]) -> None:
    _write_private(state_file_path(), json.dumps(state, indent=2, sort_keys=True) + "\n")


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------


def generate_tokens() -> Dict[str, str]:
    """Return fresh, distinct, URL-safe view/control tokens."""
    view = secrets.token_urlsafe(32)
    control = secrets.token_urlsafe(32)
    while control == view:  # astronomically unlikely, but cheap to guarantee
        control = secrets.token_urlsafe(32)
    return {"view": view, "control": control}


def render_token_file(tokens: Dict[str, str]) -> str:
    """websockify TokenFile format: ``<token>: <host>:<port>`` per line."""
    return (
        "# Managed by `hermes desktop` -- websockify TokenFile targets.\n"
        f"{tokens['view']}: 127.0.0.1:{VNC_VIEW_PORT}\n"
        f"{tokens['control']}: 127.0.0.1:{VNC_CONTROL_PORT}\n"
    )


def write_token_file(tokens: Dict[str, str], path: Optional[Path] = None) -> Path:
    path = path or token_file_path()
    _write_private(path, render_token_file(tokens))
    return path


# ---------------------------------------------------------------------------
# Chromium discovery
# ---------------------------------------------------------------------------


def _is_snap_wrapper(path: str) -> bool:
    """True for snap-packaged Chromium (or Ubuntu's apt shim that execs the snap).

    Snap confinement cannot write a profile under a hidden directory like
    ``~/.hermes`` and drags in snapd, so we never use it.
    """
    try:
        real = os.path.realpath(path)
    except OSError:
        return False
    if real.startswith("/snap/") or path.startswith("/snap/"):
        return True
    try:
        with open(real, "rb") as fh:
            head = fh.read(4096)
    except OSError:
        return False
    return head.startswith(b"#!") and b"snap" in head


def find_chrome(home: Optional[str] = None, extra: Sequence[str] = ()) -> Optional[str]:
    """Pick the most reliable non-snap Chromium-family binary on this machine.

    Order (first hit wins):
      1. ``extra`` (explicit ``--chrome`` / ``HERMES_DESKTOP_CHROME``)
      2. Chrome for Testing downloaded by ``agent-browser install``
         (``~/.agent-browser/browsers/*``) -- same build agent-browser targets
      3. Playwright's Chromium cache (``~/.cache/ms-playwright/chromium-*``)
      4. System Google Chrome / non-snap Chromium
    """
    home = home or os.path.expanduser("~")
    candidates: List[str] = [p for p in extra if p]

    for pattern in (
        os.path.join(home, ".agent-browser", "browsers", "*", "chrome-linux*", "chrome"),
        os.path.join(home, ".agent-browser", "browsers", "*", "chrome"),
    ):
        candidates.extend(sorted(glob.glob(pattern), reverse=True))

    pw_root = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "").strip()
    pw_roots = [pw_root] if pw_root and pw_root != "0" else []
    pw_roots.append(os.path.join(home, ".cache", "ms-playwright"))
    for root in pw_roots:
        candidates.extend(sorted(glob.glob(os.path.join(root, "chromium-*", "chrome-linux*", "chrome")), reverse=True))

    for name in ("google-chrome-stable", "google-chrome", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            candidates.append(found)
    candidates.extend(["/opt/google/chrome/chrome", "/usr/lib/chromium/chromium"])

    for cand in candidates:
        if os.path.isfile(cand) and os.access(cand, os.X_OK) and not _is_snap_wrapper(cand):
            return cand
    return None


def needs_no_sandbox(chrome_path: str) -> bool:
    """Mirror tools/browser_tool.py: root, or Ubuntu 23.10+ userns restriction.

    Google Chrome's .deb ships an AppArmor profile that re-allows user
    namespaces for /opt/google/chrome, so it keeps its sandbox there.
    """
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        return True
    if chrome_path.startswith("/opt/google/chrome"):
        return False
    try:
        with open("/proc/sys/kernel/apparmor_restrict_unprivileged_userns", encoding="utf-8") as fh:
            return fh.read().strip() == "1"
    except OSError:
        return False


def chrome_flags(profile: Path, no_sandbox: bool) -> List[str]:
    flags = list(BASE_CHROME_FLAGS)
    flags.insert(2, f"--user-data-dir={profile}")
    if no_sandbox:
        flags.append("--no-sandbox")
    return flags


def chrome_version(chrome_path: str, runner: Callable[..., Any] = subprocess.run) -> str:
    try:
        out = runner([chrome_path, "--version"], capture_output=True, text=True, timeout=20)
        return (out.stdout or "").strip()
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Unit rendering
# ---------------------------------------------------------------------------


def _systemd_quote(arg: str) -> str:
    """Quote one ExecStart argument for systemd (only when needed)."""
    if arg and all(c not in arg for c in ' \t"\'\\;$%'):
        return arg
    escaped = arg.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$")
    return f'"{escaped}"'


@dataclass
class UnitContext:
    user: str
    group: str
    home: str
    chrome_path: str
    chrome_flags: List[str]
    xvfb_bin: str = "/usr/bin/Xvfb"
    x11vnc_bin: str = "/usr/bin/x11vnc"
    websockify_bin: str = "/usr/bin/websockify"
    fluxbox_bin: Optional[str] = "/usr/bin/fluxbox"
    novnc_dir: Optional[str] = "/usr/share/novnc"
    bin_dir: str = ""
    profile_dir: str = ""
    token_file: str = ""
    extra: Dict[str, str] = field(default_factory=dict)


def _template(name: str) -> Template:
    return Template((ASSETS_DIR / name).read_text(encoding="utf-8"))


def render_units(ctx: UnitContext) -> Dict[str, str]:
    """Render every unit file. Returns ``{unit_name: content}``.

    ``Template.substitute`` raises on any missing placeholder, so a typo in
    a template fails loudly in tests instead of producing a broken unit.
    """
    with_wm = bool(ctx.fluxbox_bin)
    wants = [UNIT_XVFB] + ([UNIT_WM] if with_wm else []) + [UNIT_CHROMIUM, UNIT_VNC_VIEW, UNIT_WEBSOCKIFY]
    values = {
        "user": ctx.user,
        "group": ctx.group,
        "home": ctx.home,
        "display": DISPLAY,
        "display_num": str(DISPLAY_NUM),
        "screen": SCREEN,
        "cdp_port": str(CDP_PORT),
        "vnc_view_port": str(VNC_VIEW_PORT),
        "vnc_control_port": str(VNC_CONTROL_PORT),
        "ws_host": WS_HOST,
        "ws_port": str(WS_PORT),
        "xvfb_bin": ctx.xvfb_bin,
        "x11vnc_bin": ctx.x11vnc_bin,
        "websockify_bin": ctx.websockify_bin,
        "fluxbox_bin": ctx.fluxbox_bin or "",
        "wait_display": f"{_systemd_quote(os.path.join(ctx.bin_dir, 'wait-for-display.sh'))} {DISPLAY_NUM}",
        "stale_locks": " ".join(
            _systemd_quote(os.path.join(ctx.profile_dir, n))
            for n in ("SingletonLock", "SingletonSocket", "SingletonCookie")
        ),
        "token_file": _systemd_quote(ctx.token_file),
        "web_args": f"--web {_systemd_quote(ctx.novnc_dir)} " if ctx.novnc_dir else "",
        "chrome_cmd": " ".join(_systemd_quote(a) for a in [ctx.chrome_path, *ctx.chrome_flags]),
        "chromium_after": UNIT_WM if with_wm else "",
        "memory_high": CHROMIUM_MEMORY_HIGH,
        "memory_max": CHROMIUM_MEMORY_MAX,
        "target_wants": " ".join(wants),
    }
    values.update(ctx.extra)

    names = [TARGET_UNIT, UNIT_XVFB, UNIT_CHROMIUM, UNIT_VNC_VIEW, UNIT_VNC_CONTROL, UNIT_WEBSOCKIFY]
    if with_wm:
        names.insert(2, UNIT_WM)
    return {name: _template(name).substitute(values) for name in names}


# ---------------------------------------------------------------------------
# Config (CDP override) wiring
# ---------------------------------------------------------------------------


def _write_raw_config(cfg: Dict[str, Any]) -> None:
    from hermes_cli.config import ensure_hermes_home, get_config_path
    from utils import atomic_yaml_write

    ensure_hermes_home()
    atomic_yaml_write(get_config_path(), cfg, sort_keys=False)


def enable_cdp_override(url: str = CDP_URL) -> None:
    """Point Hermes's browser tool at the desktop Chromium.

    Written to ``browser.cdp_url`` in config.yaml rather than
    ``BROWSER_CDP_URL`` in .env: ``tools/browser_tool._get_cdp_override``
    re-reads config.yaml on every call (mtime-cached), so the change applies
    to the running hermes-server without a restart, and ``disable`` takes
    effect just as live.  ``browser.youmake_desktop`` enables the fork's
    fallback + attach guard (tools/youmake_desktop_guard.py).
    """
    from hermes_cli.config import read_raw_config

    cfg = read_raw_config()
    browser = cfg.get("browser")
    if not isinstance(browser, dict):
        browser = {}
    browser[CONFIG_CDP_KEY] = url
    browser[CONFIG_DESKTOP_FLAG] = True
    cfg["browser"] = browser
    _write_raw_config(cfg)


def disable_cdp_override(url: str = CDP_URL) -> bool:
    """Remove the desktop CDP override. Leaves a user-set different URL alone.

    Returns True when anything was removed.
    """
    from hermes_cli.config import read_raw_config

    changed = False
    cfg = read_raw_config()
    browser = cfg.get("browser")
    if isinstance(browser, dict):
        if str(browser.get(CONFIG_CDP_KEY) or "").rstrip("/") == url.rstrip("/"):
            browser.pop(CONFIG_CDP_KEY, None)
            changed = True
        if CONFIG_DESKTOP_FLAG in browser:
            browser.pop(CONFIG_DESKTOP_FLAG, None)
            changed = True
        if changed:
            if browser:
                cfg["browser"] = browser
            else:
                cfg.pop("browser", None)
            _write_raw_config(cfg)

    try:
        from hermes_cli.config import get_env_value, remove_env_value

        if str(get_env_value("BROWSER_CDP_URL") or "").rstrip("/") == url.rstrip("/"):
            remove_env_value("BROWSER_CDP_URL")
            changed = True
    except Exception:
        pass
    return changed


def conflicting_env_override(url: str = CDP_URL) -> Optional[str]:
    """BROWSER_CDP_URL in the environment wins over config.yaml; flag a mismatch."""
    env = os.environ.get("BROWSER_CDP_URL", "").strip()
    if env and env.rstrip("/") != url.rstrip("/"):
        return env
    return None


# ---------------------------------------------------------------------------
# System helpers (all subprocess access goes through ``Runner``)
# ---------------------------------------------------------------------------


class Runner:
    """Thin subprocess wrapper so tests can swap in a fake."""

    def __init__(self, run: Callable[..., Any] = subprocess.run):
        self._run = run
        self.is_root = hasattr(os, "geteuid") and os.geteuid() == 0

    def run(self, cmd: List[str], *, check: bool = False, timeout: int = 120, env: Optional[dict] = None):
        proc = self._run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
        if check and proc.returncode != 0:
            msg = (proc.stderr or proc.stdout or "").strip()
            raise DesktopError(f"command failed ({proc.returncode}): {shlex.join(cmd)}\n{msg[-800:]}")
        return proc

    def sudo(self, cmd: List[str], **kw):
        return self.run(cmd if self.is_root else ["sudo", "-n", *cmd], **kw)

    def systemctl(self, *args: str, check: bool = False):
        return self.sudo(["systemctl", *args], check=check)

    def unit_active(self, unit: str) -> str:
        proc = self.run(["systemctl", "is-active", unit])
        return (proc.stdout or "").strip() or "unknown"

    def unit_memory(self, unit: str) -> Optional[int]:
        proc = self.run(["systemctl", "show", "-p", "MemoryCurrent", "--value", unit])
        raw = (proc.stdout or "").strip()
        return int(raw) if raw.isdigit() else None


def _preflight(runner: Runner) -> None:
    if platform.system() != "Linux":
        raise DesktopError("hermes desktop only runs on Linux VMs (systemd + apt).")
    if not shutil.which("systemctl"):
        raise DesktopError("systemctl not found: hermes desktop needs systemd.")
    if not runner.is_root and runner.run(["sudo", "-n", "true"]).returncode != 0:
        raise DesktopError("passwordless sudo is required (sudo -n true failed).")


def missing_packages(runner: Runner, packages: Sequence[str]) -> List[str]:
    missing = []
    for pkg in packages:
        proc = runner.run(["dpkg-query", "-W", "-f=${Status}", pkg])
        if proc.returncode != 0 or "install ok installed" not in (proc.stdout or ""):
            missing.append(pkg)
    return missing


def apt_install(runner: Runner, packages: Sequence[str]) -> None:
    if not packages:
        return
    env = {**os.environ, "DEBIAN_FRONTEND": "noninteractive"}
    # Wait for unattended-upgrades instead of failing on the dpkg lock.
    apt = ["env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "-o", "DPkg::Lock::Timeout=600"]
    # A VM can carry a half-finished dpkg run (an interrupted cloud-init or
    # unattended upgrade); apt refuses to install anything until it is finished.
    runner.sudo(["env", "DEBIAN_FRONTEND=noninteractive", "dpkg", "--configure", "-a"], check=False, timeout=900, env=env)
    runner.sudo([*apt, "update", "-q"], check=True, timeout=600, env=env)
    runner.sudo([*apt, "install", "-y", "-q", "--no-install-recommends", *packages], check=True, timeout=1200, env=env)


def install_google_chrome(runner: Runner) -> str:
    """Last resort: Google Chrome stable .deb (amd64, apt-managed, not a snap)."""
    if platform.machine() not in ("x86_64", "amd64"):
        raise DesktopError(
            "No usable Chromium found and Google Chrome has no arm64 .deb. "
            "Run `agent-browser install --with-deps` (Chrome for Testing) and re-run setup, "
            "or pass --chrome /path/to/chrome."
        )
    import urllib.request

    with tempfile.TemporaryDirectory() as tmp:
        deb = os.path.join(tmp, "google-chrome-stable.deb")
        urllib.request.urlretrieve(GOOGLE_CHROME_DEB_URL, deb)  # noqa: S310 - fixed https URL
        os.chmod(tmp, 0o755)
        os.chmod(deb, 0o644)
        apt_install(runner, [deb])
    path = GOOGLE_CHROME_PATH
    if not os.path.isfile(path):
        raise DesktopError("google-chrome-stable installed but /opt/google/chrome/chrome is missing.")
    return path


def install_unit_files(runner: Runner, units: Dict[str, str]) -> List[str]:
    """Write changed units into /etc/systemd/system. Returns changed unit names."""
    changed = []
    with tempfile.TemporaryDirectory() as tmp:
        for name, content in units.items():
            dest = SYSTEMD_DIR / name
            try:
                if dest.read_text(encoding="utf-8") == content:
                    continue
            except OSError:
                pass
            src = Path(tmp) / name
            src.write_text(content, encoding="utf-8")
            runner.sudo(["install", "-m", "0644", "-o", "root", "-g", "root", str(src), str(dest)], check=True)
            changed.append(name)
    return changed


def install_scripts() -> None:
    """Copy helper scripts to ~/.hermes/desktop/bin (stable across pipx upgrades)."""
    target = bin_dir()
    target.mkdir(parents=True, exist_ok=True)
    src = ASSETS_DIR / "wait-for-display.sh"
    dest = target / src.name
    dest.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    os.chmod(dest, 0o755)


def cdp_version(url: str = CDP_URL, timeout: float = 2.0) -> Optional[Dict[str, Any]]:
    import urllib.request

    try:
        with urllib.request.urlopen(url.rstrip("/") + "/json/version", timeout=timeout) as resp:  # noqa: S310
            data = json.loads(resp.read().decode("utf-8"))
            return data if isinstance(data, dict) else None
    except Exception:
        return None


def wait_for_cdp(url: str = CDP_URL, timeout: float = 30.0, sleep: Callable[[float], None] = time.sleep) -> Optional[Dict[str, Any]]:
    deadline = time.monotonic() + timeout
    while True:
        info = cdp_version(url)
        if info is not None or time.monotonic() >= deadline:
            return info
        sleep(1.0)


def _meminfo() -> Dict[str, int]:
    out: Dict[str, int] = {}
    try:
        with open("/proc/meminfo", encoding="utf-8") as fh:
            for line in fh:
                key, _, rest = line.partition(":")
                parts = rest.split()
                if parts and parts[0].isdigit():
                    out[key] = int(parts[0]) * 1024
    except OSError:
        pass
    return out


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def _current_user() -> Dict[str, str]:
    import grp  # POSIX-only; imported lazily so the module loads anywhere
    import pwd

    pw = pwd.getpwuid(os.getuid())
    try:
        group = grp.getgrgid(pw.pw_gid).gr_name
    except KeyError:
        group = str(pw.pw_gid)
    return {"user": pw.pw_name, "group": group, "home": pw.pw_dir}


def build_unit_context(chrome_path: str, *, with_wm: bool, novnc_dir: Optional[str]) -> UnitContext:
    who = _current_user()
    return UnitContext(
        user=who["user"],
        group=who["group"],
        home=who["home"],
        chrome_path=chrome_path,
        chrome_flags=chrome_flags(profile_dir(), needs_no_sandbox(chrome_path)),
        xvfb_bin=shutil.which("Xvfb") or "/usr/bin/Xvfb",
        x11vnc_bin=shutil.which("x11vnc") or "/usr/bin/x11vnc",
        websockify_bin=shutil.which("websockify") or "/usr/bin/websockify",
        fluxbox_bin=(shutil.which("fluxbox") or "/usr/bin/fluxbox") if with_wm else None,
        novnc_dir=novnc_dir,
        bin_dir=str(bin_dir()),
        profile_dir=str(profile_dir()),
        token_file=str(token_file_path()),
    )


def _status_payload(runner: Runner) -> Dict[str, Any]:
    state = load_state()
    services = [UNIT_XVFB] + ([UNIT_WM] if state.get("window_manager", True) else []) + [
        UNIT_CHROMIUM, UNIT_VNC_VIEW, UNIT_VNC_CONTROL, UNIT_WEBSOCKIFY,
    ]
    units = {}
    total_mem = 0
    for unit in services:
        active = runner.unit_active(unit)
        mem = runner.unit_memory(unit) if active == "active" else None
        if mem:
            total_mem += mem
        units[unit] = {"active": active, "memory_bytes": mem}
    info = cdp_version()
    required = [u for u in services if u != UNIT_VNC_CONTROL]
    healthy = info is not None and all(units[u]["active"] == "active" for u in required)
    meminfo = _meminfo()
    return {
        "status": "running" if healthy else ("stopped" if info is None else "degraded"),
        "configured": bool(state),
        "units": units,
        "control_enabled": units[UNIT_VNC_CONTROL]["active"] == "active",
        "cdp": {"url": CDP_URL, "ok": info is not None, "browser": (info or {}).get("Browser")},
        "memory": {
            "desktop_bytes": total_mem,
            "system_total_bytes": meminfo.get("MemTotal"),
            "system_available_bytes": meminfo.get("MemAvailable"),
        },
        "websockify": {"host": WS_HOST, "port": WS_PORT},
        "display": DISPLAY,
        "display_size": SCREEN,
    }


def _emit(payload: Dict[str, Any], as_json: bool, lines: Sequence[str]) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        for line in lines:
            print(line)


def cmd_setup(args: argparse.Namespace, runner: Optional[Runner] = None) -> int:
    runner = runner or Runner()
    log = (lambda *_a: None) if args.json else (lambda msg: print(msg, file=sys.stderr))
    _preflight(runner)

    with_wm = not args.no_wm
    packages = list(APT_PACKAGES_BASE) + ([APT_PACKAGE_WM] if with_wm else [])
    missing = missing_packages(runner, packages)
    if missing:
        if not args.yes:
            if not sys.stdin.isatty():
                raise DesktopError(f"missing packages {missing}; re-run with --yes to install them.")
            answer = input(f"Install {' '.join(missing)} with apt? [y/N] ").strip().lower()
            if answer not in ("y", "yes"):
                raise DesktopError("aborted: packages not installed.")
        log(f"Installing: {' '.join(missing)}")
        apt_install(runner, missing)

    explicit = [p for p in (args.chrome, os.environ.get("HERMES_DESKTOP_CHROME", "")) if p]
    chrome = find_chrome(extra=explicit) if explicit else None
    # The desktop browser opens arbitrary sites, so prefer Google Chrome: its
    # .deb ships an AppArmor profile that keeps the sandbox on Ubuntu 24.04,
    # where Chrome for Testing has to run with --no-sandbox.
    if not chrome and os.path.isfile(GOOGLE_CHROME_PATH):
        chrome = GOOGLE_CHROME_PATH
    if not chrome and platform.machine() in ("x86_64", "amd64") and (args.yes or sys.stdin.isatty()):
        log("Installing Google Chrome stable (.deb) so the browser keeps its sandbox.")
        chrome = install_google_chrome(runner)
    if not chrome:
        chrome = find_chrome()
    if not chrome:
        if not args.yes and not sys.stdin.isatty():
            raise DesktopError("no Chromium found; re-run with --yes to install Google Chrome stable.")
        log("No non-snap Chromium found; installing Google Chrome stable (.deb).")
        chrome = install_google_chrome(runner)
    log(f"Chromium: {chrome}")

    novnc = next((d for d in NOVNC_DIRS if os.path.isfile(os.path.join(d, "vnc.html"))), None)

    for d in (desktop_dir(), profile_dir()):
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(d, 0o700)
    install_scripts()

    state = load_state()
    tokens = state.get("tokens") if isinstance(state.get("tokens"), dict) else None
    if not tokens or not tokens.get("view") or not tokens.get("control"):
        tokens = generate_tokens()
    write_token_file(tokens)

    units = render_units(build_unit_context(chrome, with_wm=with_wm, novnc_dir=novnc))
    changed = install_unit_files(runner, units)
    if not with_wm:
        runner.systemctl("disable", "--now", UNIT_WM)
    runner.systemctl("daemon-reload", check=True)
    runner.systemctl("enable", TARGET_UNIT, check=True)
    if changed and runner.unit_active(UNIT_CHROMIUM) == "active":
        runner.systemctl("restart", TARGET_UNIT, check=True)
    else:
        runner.systemctl("start", TARGET_UNIT, check=True)

    enable_cdp_override(CDP_URL)
    info = wait_for_cdp(CDP_URL, timeout=args.wait)
    version = (info or {}).get("Browser") or chrome_version(chrome, runner._run)

    state.update({
        "tokens": tokens,
        "chrome_path": chrome,
        "chrome_version": version,
        "window_manager": with_wm,
        "novnc_dir": novnc,
        "updated_at": int(time.time()),
    })
    save_state(state)

    env_conflict = conflicting_env_override(CDP_URL)
    payload = {
        "status": "running" if info else "degraded",
        "websockify_host": WS_HOST,
        "websockify_port": WS_PORT,
        "novnc_path": "/vnc.html" if novnc else None,
        "view_token": tokens["view"],
        "control_token": tokens["control"],
        "display": DISPLAY,
        "display_size": SCREEN,
        "cdp_url": CDP_URL,
        "chromium_path": chrome,
        "chromium_version": version,
        "units_changed": changed,
        "warnings": ([f"BROWSER_CDP_URL={env_conflict} in the environment overrides browser.cdp_url"] if env_conflict else []),
    }
    _emit(payload, args.json, [
        f"Desktop {payload['status']}: display {DISPLAY} ({SCREEN}), Chromium {version or '?'}",
        f"websockify: {WS_HOST}:{WS_PORT} (noVNC at /vnc.html)" if novnc else f"websockify: {WS_HOST}:{WS_PORT}",
        f"view token:    {tokens['view']}",
        f"control token: {tokens['control']}",
        f"browser.cdp_url = {CDP_URL}",
        *payload["warnings"],
    ])
    return 0 if info else 1


def cmd_status(args: argparse.Namespace, runner: Optional[Runner] = None) -> int:
    runner = runner or Runner()
    payload = _status_payload(runner)
    lines = [f"Desktop: {payload['status']}  CDP: {'ok' if payload['cdp']['ok'] else 'down'} ({payload['cdp']['browser'] or '-'})"]
    for unit, info in payload["units"].items():
        mem = info["memory_bytes"]
        lines.append(f"  {unit:40s} {info['active']:10s} {f'{mem / 1048576:.0f} MiB' if mem else ''}")
    lines.append(f"  desktop memory: {payload['memory']['desktop_bytes'] / 1048576:.0f} MiB")
    _emit(payload, args.json, lines)
    return 0 if payload["status"] == "running" else 1


def cmd_start(args: argparse.Namespace, runner: Optional[Runner] = None) -> int:
    runner = runner or Runner()
    runner.systemctl("start", TARGET_UNIT, check=True)
    ok = wait_for_cdp(CDP_URL, timeout=getattr(args, "wait", 30.0)) is not None
    _emit({"status": "running" if ok else "degraded"}, args.json, [f"Desktop started (CDP {'ok' if ok else 'not answering yet'})."])
    return 0 if ok else 1


def cmd_stop(args: argparse.Namespace, runner: Optional[Runner] = None) -> int:
    runner = runner or Runner()
    runner.systemctl("stop", UNIT_VNC_CONTROL)
    runner.systemctl("stop", TARGET_UNIT, *ALL_SERVICES, check=True)
    _emit({"status": "stopped"}, args.json, ["Desktop stopped. The browser tool falls back to its headless browser while it is down."])
    return 0


def cmd_control(args: argparse.Namespace, runner: Optional[Runner] = None) -> int:
    runner = runner or Runner()
    if args.state == "on":
        runner.systemctl("start", UNIT_VNC_CONTROL, check=True)
    else:
        runner.systemctl("stop", UNIT_VNC_CONTROL, check=True)
    active = runner.unit_active(UNIT_VNC_CONTROL) == "active"
    _emit({"control_enabled": active}, args.json, [f"Interactive VNC {'on' if active else 'off'}."])
    return 0 if active == (args.state == "on") else 1


def cmd_disable(args: argparse.Namespace, runner: Optional[Runner] = None) -> int:
    runner = runner or Runner()
    removed = disable_cdp_override(CDP_URL)
    runner.systemctl("disable", "--now", TARGET_UNIT)
    runner.systemctl("stop", *ALL_SERVICES)
    _emit({"status": "disabled", "cdp_override_removed": removed}, args.json, [
        "Desktop disabled; Hermes is back on its normal headless browser.",
        "Unit files and the Chromium profile are kept; `hermes desktop setup` re-enables.",
    ])
    return 0


def cmd_rotate_tokens(args: argparse.Namespace, runner: Optional[Runner] = None) -> int:
    runner = runner or Runner()
    state = load_state()
    tokens = generate_tokens()
    write_token_file(tokens)
    state["tokens"] = tokens
    state["tokens_rotated_at"] = int(time.time())
    save_state(state)
    # Drop live sessions authenticated with the old tokens. websockify re-reads
    # the token file per connection, so a restart is only for existing ones.
    if runner.unit_active(UNIT_WEBSOCKIFY) == "active":
        runner.systemctl("restart", UNIT_WEBSOCKIFY)
    _emit({"view_token": tokens["view"], "control_token": tokens["control"]}, args.json, [
        f"view token:    {tokens['view']}",
        f"control token: {tokens['control']}",
    ])
    return 0


def cmd_permission(args: argparse.Namespace) -> int:
    """Show the computer-use permission file and the decision for an agent/run.

    Read-only: the Youmake server owns ``~/.hermes/desktop/permissions.json``.
    Its presence also tells Youmake that this runtime enforces the permission.
    """
    from tools.youmake_computer_permission import describe
    from tools.youmake_identity import SLUG_RE, parse_youmake_user

    run = args.run or None
    agent = args.agent or None
    if run:
        parsed = parse_youmake_user(run)
        if parsed is None:
            raise DesktopError(f"invalid --run {run!r} (expected youmake:<userId>:<slug>:<lane>)")
        agent = agent or parsed.slug
    if agent is not None and not SLUG_RE.fullmatch(agent):
        raise DesktopError(f"invalid --agent {agent!r}")
    payload = describe(agent or "hermes", run)
    state = payload["state"]
    lines = [
        f"file: {payload['file']} ({payload['file_status']})",
        f"desktop mode: {'on' if payload['desktop_mode'] else 'off'}",
        f"mode: {state['mode']}",
        f"agents: {json.dumps(state['agents'])}",
        f"active one-time grants: {len(state['once'])}",
        f"decision for {payload['agent']}{' / ' + run if run else ''}: {payload['decision']}",
    ]
    _emit(payload, args.json, lines)
    return 0


# ---------------------------------------------------------------------------
# argparse wiring
# ---------------------------------------------------------------------------


def _wrap(fn: Callable[[argparse.Namespace], int]) -> Callable[[argparse.Namespace], None]:
    def _run(args: argparse.Namespace) -> None:
        try:
            code = fn(args)
        except DesktopError as exc:
            if getattr(args, "json", False):
                print(json.dumps({"status": "error", "error": str(exc)}))
            else:
                print(f"hermes desktop: {exc}", file=sys.stderr)
            code = 2
        sys.exit(code)

    return _run


def register_cli(parent: argparse.ArgumentParser) -> None:
    """Attach ``desktop`` subcommands to the parser main.py created."""
    parent.set_defaults(func=lambda a: parent.print_help())
    subs = parent.add_subparsers(dest="desktop_command")

    def _json(p: argparse.ArgumentParser) -> None:
        p.add_argument("--json", action="store_true", help="Machine-readable output")

    p = subs.add_parser("setup", help="Install and start the virtual desktop (idempotent)")
    p.add_argument("--yes", "-y", action="store_true", help="Install missing apt packages without prompting")
    p.add_argument("--no-wm", action="store_true", help="Skip the fluxbox window manager")
    p.add_argument("--chrome", default="", help="Explicit Chromium/Chrome binary to run")
    p.add_argument("--wait", type=float, default=30.0, help="Seconds to wait for CDP after start")
    _json(p)
    p.set_defaults(func=_wrap(cmd_setup))

    p = subs.add_parser("status", help="Unit health, CDP reachability, memory")
    _json(p)
    p.set_defaults(func=_wrap(cmd_status))

    p = subs.add_parser("start", help="Start the desktop units")
    p.add_argument("--wait", type=float, default=30.0)
    _json(p)
    p.set_defaults(func=_wrap(cmd_start))

    p = subs.add_parser("stop", help="Stop the desktop units")
    _json(p)
    p.set_defaults(func=_wrap(cmd_stop))

    p = subs.add_parser("control", help="Start/stop the interactive (takeover) VNC")
    p.add_argument("state", choices=["on", "off"])
    _json(p)
    p.set_defaults(func=_wrap(cmd_control))

    p = subs.add_parser("disable", help="Stop the desktop and remove the CDP override")
    _json(p)
    p.set_defaults(func=_wrap(cmd_disable))

    p = subs.add_parser("rotate-tokens", help="Issue new view/control websockify tokens")
    _json(p)
    p.set_defaults(func=_wrap(cmd_rotate_tokens))

    p = subs.add_parser(
        "permission",
        help="Show the computer-use permission and the decision for an agent/run (read-only)",
    )
    p.add_argument("action", nargs="?", choices=["show", "check"], default="show",
                   help="show (default) or check; both print the same payload")
    p.add_argument("--agent", default="", help="Agent slug (default: from --run, else hermes)")
    p.add_argument("--run", default="", help="Run key youmake:<userId>:<slug>:<lane>")
    _json(p)
    p.set_defaults(func=_wrap(cmd_permission))
