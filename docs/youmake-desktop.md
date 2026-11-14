# Youmake virtual desktop (`hermes desktop`)

Opt-in feature of the Youmake fork. It lets the Youmake web UI show the agent's
browser live over VNC and lets a human take control of it, while Hermes's
browser tool keeps driving the same browser through CDP.

Code: `hermes_cli/youmake_desktop.py` (CLI), `hermes_cli/desktop_assets/`
(systemd unit templates, shipped as package data), and
`tools/youmake_desktop_guard.py` (fallback + attach guard used by
`tools/browser_tool.py`).

## Architecture

```
                      Hetzner VM (user claw)                                 Youmake server
 +----------------------------------------------------------------+
 | Xvfb :1  1280x800x24  (unix socket only, -nolisten tcp)        |
 |   +-- fluxbox (optional, --no-wm to skip)                      |
 |   +-- Chromium (headful, one persistent instance)              |
 |   |     CDP 127.0.0.1:9222  <---- agent-browser --cdp (Hermes) |
 |   |     profile ~/.hermes/desktop/chromium-profile             |
 |   +-- x11vnc -viewonly   127.0.0.1:5901   (always on)          |
 |   +-- x11vnc (control)   127.0.0.1:5902   (takeover only)      |
 | websockify 127.0.0.1:6080  TokenFile ~/.hermes/desktop/vnc-tokens  <== SSH tunnel ==  noVNC client
 |   view token    -> 127.0.0.1:5901                              |
 |   control token -> 127.0.0.1:5902                              |
 |   --web /usr/share/novnc  (noVNC at /vnc.html)                 |
 +----------------------------------------------------------------+
```

systemd system units (rendered with `User=` = the Hermes user), grouped under
`youmake-desktop.target`:

| Unit | Purpose |
|---|---|
| `youmake-desktop-xvfb.service` | virtual display `:1` |
| `youmake-desktop-wm.service` | fluxbox (optional) |
| `youmake-desktop-chromium.service` | the persistent Chromium, `MemoryHigh=1200M`, `MemoryMax=1600M`, `OOMScoreAdjust=500` |
| `youmake-desktop-vnc-view.service` | view-only x11vnc |
| `youmake-desktop-vnc-control.service` | interactive x11vnc, not pulled in by the target |
| `youmake-desktop-websockify.service` | websockify + noVNC static files |

### Ports

| Port | Bind | What |
|---|---|---|
| 9222 | 127.0.0.1 | Chromium DevTools (CDP) |
| 5901 | 127.0.0.1 | x11vnc view-only |
| 5902 | 127.0.0.1 | x11vnc interactive (only while control is on) |
| 6080 | 127.0.0.1 | websockify / noVNC |
| X11 | unix socket `/tmp/.X11-unix/X1` | Xvfb (no TCP) |

### How Hermes uses it

`setup` writes `browser.cdp_url: http://127.0.0.1:9222` and
`browser.youmake_desktop: true` to `~/.hermes/config.yaml`. We use config.yaml
rather than `BROWSER_CDP_URL` in `.env` because `_get_cdp_override()` re-reads
config.yaml on every call (mtime-cached), so enabling or disabling applies to
the running `hermes-server.service` without a restart. A `BROWSER_CDP_URL`
env var still wins over config; `setup` warns if one points somewhere else.

`browser.youmake_desktop: true` turns on two small behaviours (no-ops otherwise,
so upstream behaviour is unchanged):

1. Fallback: if the desktop Chromium does not answer `/json/version` (desktop
   stopped, or Chromium restarting after hitting `MemoryMax`), the override is
   ignored and the browser tool uses its normal local headless browser. A
   warning is logged at most once a minute. Sessions created during the outage
   stay on the headless browser until they are cleaned up.
2. Attach guard: after the first successful command in a desktop session,
   Hermes runs `agent-browser --cdp <ws> --json get cdp-url` once. If that
   returns a different endpoint, agent-browser launched its own browser: the
   call fails, the session is closed, and the next call re-attaches.
   Inconclusive answers never block. In practice the mismatch should not
   happen: Hermes never passes `--session` with `--cdp` (agent-browser 0.13+
   silently launches a local browser when both are given) and every Hermes
   session gets a fresh agent-browser socket dir, so a new daemon either
   attaches or errors out.

### Choosing a Chromium

On Ubuntu 24.04 the `chromium` / `chromium-browser` apt packages are only
shims for the snap. The snap cannot write a profile under a hidden directory
such as `~/.hermes`, and it needs snapd, so it is never used. `setup` picks the
first match from this list:

1. `--chrome PATH` or `HERMES_DESKTOP_CHROME`
2. Chrome for Testing already downloaded by `agent-browser install`
   (`~/.agent-browser/browsers/*/chrome-linux*/chrome`). This is the same build
   agent-browser is tested against, and it is usually already on disk.
3. Playwright's Chromium (`~/.cache/ms-playwright/chromium-*`)
4. System Google Chrome or a non-snap Chromium
5. If none is found: install the Google Chrome stable `.deb` from
   dl.google.com. This is apt-managed and needs amd64; CX23 is amd64. On arm64,
   setup asks you to run `agent-browser install --with-deps`.

`--no-sandbox` is added only when running as root, or when Ubuntu's
`apparmor_restrict_unprivileged_userns=1` applies to a binary outside
`/opt/google/chrome` (Google Chrome's .deb ships its own AppArmor profile).
This is the same rule `tools/browser_tool.py` uses.

## Security model

- Nothing listens on a public interface. X11 uses a unix socket only. VNC,
  CDP and websockify are bound to 127.0.0.1.
- The Youmake server reaches websockify through an SSH tunnel to
  `127.0.0.1:6080` (for example `ssh -L <local>:127.0.0.1:6080 claw@vm`) and
  serves noVNC with `?path=websockify?token=<token>`. The view token can only
  reach the view-only VNC. The control token reaches the interactive VNC, and
  only while `hermes desktop control on` is active.
- Tokens are 256-bit `secrets.token_urlsafe(32)` values, stored in
  `~/.hermes/desktop/vnc-tokens` and `state.json` (both 0600, directory 0700).
  `rotate-tokens` swaps them. websockify's TokenFile plugin re-reads the file
  on every connection, and websockify is restarted to drop existing sessions.
- The VNC servers use `-nopw`. Any process on the VM running as any user can
  connect to 127.0.0.1:5901/5902 or CDP 9222. That is acceptable on a
  single-tenant VM where the agent already has the user's full shell. Do not
  enable this on a shared host.
- CDP on 9222 gives full browser control (cookies included) to local
  processes. It is never tunnelled to the Youmake server.

## Memory cost (CX23: 2 vCPU / 4 GB)

Rough, unverified on the VM:

| Component | RSS |
|---|---|
| Xvfb (4 MB framebuffer) | 20-40 MB |
| fluxbox | ~10 MB |
| x11vnc view (+ control while on) | 15-30 MB each |
| websockify (Python) | ~25 MB |
| Chromium idle, one tab | 250-400 MB |
| Chromium on a heavy page | 600 MB to 1.2 GB (throttled at 1200M, killed and restarted at 1600M) |

Baseline overhead is about 350-500 MB. When the desktop is up, the agent uses
this Chromium instead of launching its own headless one, so the net cost over
today's headless browser is roughly +100-200 MB plus the headful rendering
overhead. `hermes desktop status` reports live per-unit memory from systemd.
Given the known OOM issues when the agent runs heavy CLIs, the Chromium cgroup
cap and `OOMScoreAdjust=500` make sure Chromium dies before Hermes does.

## Usage

All commands accept `--json`.

```bash
hermes desktop setup --yes --json   # idempotent: apt packages, units, tokens, config.yaml, start
hermes desktop status --json        # unit health, CDP /json/version, memory
hermes desktop control on           # start the interactive VNC (takeover)
hermes desktop control off
hermes desktop rotate-tokens --json
hermes desktop stop                 # browser tool falls back to headless while stopped
hermes desktop start
hermes desktop disable              # stop + disable units, remove the CDP override
```

`setup --json` returns `status`, `websockify_host`, `websockify_port`,
`novnc_path`, `view_token`, `control_token`, `display`, `display_size`,
`cdp_url`, `chromium_path`, `chromium_version`, `units_changed`, and
`warnings`. The exit code is non-zero when CDP did not come up within `--wait`
seconds (default 30).

Other options: `--no-wm` skips fluxbox and `--chrome PATH` forces a binary.
Run the commands as the Hermes user (`claw`), which needs passwordless sudo.
Units are written to `/etc/systemd/system` through `sudo -n install`.

`disable` keeps the unit files and the Chromium profile (logins persist).
`setup` re-enables everything with the same tokens.

## Not verified

None of this has been run on a real VM yet. The unit tests mock every
subprocess. Check these on the first real run:

- websockify 0.10 (Ubuntu 24.04) TokenFile behaviour
- the Chrome for Testing glob path
- the output format of `agent-browser get cdp-url`
- the memory figures above
