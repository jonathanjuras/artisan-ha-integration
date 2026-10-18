# artisan-ha-bridge

A small daemon that runs next to [Artisan](https://artisan-scope.org/) on the roasting laptop and
streams the roast to Home Assistant: bean and environment temperature, rate of rise, roast
events (CHARGE, DRY END, FC START, DROP, ...), phase, development time, DTR, slider values,
alarms and a summary of every roast.

Artisan stays unmodified. The bridge reads telemetry from Artisan's WebLCDs WebSocket, receives
events and slider changes from Artisan event actions, computes RoR, phase and roast summary
itself, and serves everything to the Home Assistant `artisan` integration over an authenticated
WebSocket on the LAN. It advertises itself via zeroconf, so Home Assistant discovers it.

```
Artisan WebLCDs  ws://127.0.0.1:8080/websocket ──telemetry──┐
Artisan actions  ws://127.0.0.1:8766/artisan   ──events─────┤
                 (or `artisan-ha-bridge emit`, POST /emit)  ▼
                                                  artisan-ha-bridge ──ws://<laptop>:8765/v1──▶ Home Assistant
```

## Install

Python 3.11 or newer.

**Linux:** the installer from the rolling release sets up the bridge, config and systemd user
service in one step (see `SETUP.md` on the release):

```sh
curl -fsSL https://git.jur.as/dane/artisan-ha-integration/releases/download/latest/install.sh | bash
```

**macOS / Windows / manual:** [pipx](https://pipx.pypa.io/) keeps the bridge in its own
environment:

```sh
pipx install "artisan-ha-bridge[watch] @ git+https://git.jur.as/dane/artisan-ha-integration.git#subdirectory=bridge"
```

or, from the wheel inside the release's `artisan-ha-bridge.tar.gz`:
`pipx install "./artisan_ha_bridge-<version>-py3-none-any.whl[watch]"`.

The `[watch]` extra (watchdog) is only needed for `.alog` enrichment (see `autosave_dir`
below); leave it out if you do not use it.

## Quick start

```sh
artisan-ha-bridge init            # creates the config, prints the token and the config path
artisan-ha-bridge print-actions   # the strings to paste into Artisan (see below)
artisan-ha-bridge run             # start the bridge (also the default without a command)
artisan-ha-bridge doctor          # check that everything is wired up
```

Lost the token? `artisan-ha-bridge show-token` prints it again.

Then install the service for your OS (below) so the bridge starts with your session, and add the
integration in Home Assistant.

## Artisan wire-up

`artisan-ha-bridge print-actions` prints these steps with the ports from your config.

1. **Config > Curves > UI tab:** enable **WebLCDs**, port 8080. Tick **Alarm Popups** to forward
   alarms.
2. **Config > Port > WebSocket tab:** host `127.0.0.1`, port `8766`, path `artisan`. If the
   roaster already uses the WebSocket device, use the Call Program column instead.
3. **Config > Events > Buttons:** for each slot, Action = **WebSocket Command** with the string
   below. If a slot already drives the roaster, move the original action to a hidden custom
   button, add a second hidden button carrying the WebSocket Command, and set the slot to
   **Multiple Event** for both.
4. **Config > Events > Sliders:** for sliders not already bound to roaster control, Action =
   **WebSocket Command** with the slider string.
5. Optional: **Config > Alarms** with a Pop Up action; these reach Home Assistant as `alert`.
6. **Help > Save Settings.**

| Slot | WebSocket Command | Call Program fallback |
|---|---|---|
| ON | `send(dict(k='ev',e='on',bt={BT},et={ET},tm={t}))` | `artisan-ha-bridge emit ev on {BT} {ET} {t}` |
| START | `send(dict(k='ev',e='start',bt={BT},et={ET},tm={t}))` | `artisan-ha-bridge emit ev start {BT} {ET} {t}` |
| CHARGE | `send(dict(k='ev',e='charge',bt={BT},et={ET},tm={t}))` | `artisan-ha-bridge emit ev charge {BT} {ET} {t}` |
| DRY END | `send(dict(k='ev',e='dry',bt={BT},et={ET},tm={t}))` | `artisan-ha-bridge emit ev dry {BT} {ET} {t}` |
| FC START | `send(dict(k='ev',e='fcs',bt={BT},et={ET},tm={t}))` | `artisan-ha-bridge emit ev fcs {BT} {ET} {t}` |
| FC END | `send(dict(k='ev',e='fce',bt={BT},et={ET},tm={t}))` | `artisan-ha-bridge emit ev fce {BT} {ET} {t}` |
| SC START | `send(dict(k='ev',e='scs',bt={BT},et={ET},tm={t}))` | `artisan-ha-bridge emit ev scs {BT} {ET} {t}` |
| SC END | `send(dict(k='ev',e='sce',bt={BT},et={ET},tm={t}))` | `artisan-ha-bridge emit ev sce {BT} {ET} {t}` |
| DROP | `send(dict(k='ev',e='drop',bt={BT},et={ET},tm={t}))` | `artisan-ha-bridge emit ev drop {BT} {ET} {t}` |
| COOL END | `send(dict(k='ev',e='cool',bt={BT},et={ET},tm={t}))` | `artisan-ha-bridge emit ev cool {BT} {ET} {t}` |
| OFF | `send(dict(k='ev',e='off',bt={BT},et={ET},tm={t}))` | `artisan-ha-bridge emit ev off {BT} {ET} {t}` |
| RESET | `send(dict(k='ev',e='reset',bt={BT},et={ET},tm={t}))` | `artisan-ha-bridge emit ev reset {BT} {ET} {t}` |
| Heat slider | `send(dict(k='ctl',ch='heat',v={}))` | `artisan-ha-bridge emit ctl heat {}` |
| Fan slider | `send(dict(k='ctl',ch='fan',v={}))` | `artisan-ha-bridge emit ctl fan {}` |

Notes:

- Use the strings exactly as shown. Artisan replaces `_` and `$` in WebSocket Commands and runs
  slider strings through `str.format`, so the commands use `dict(...)` and never contain `_`,
  `$` or literal braces. Other slider channels work the same way: any name matching
  `[a-z0-9]{1,16}`, for example `drum`.
- Artisan's WebSocket client connects lazily, so the very first command after Artisan starts may
  be lost. The bridge also infers state from WebLCDs (the first frame means Artisan is
  monitoring), so a lost `ON` is harmless.
- **Simulator mode** suppresses WebSocket Commands; use the Call Program column to test with the
  simulator.
- **Windows:** Call Program needs the absolute path to the executable.
  `artisan-ha-bridge print-actions --fallback` prints the table with it. With a non-default
  config file, pass `--config PATH` to `print-actions` and the fallback commands include it.
- `emit` posts with a 1 s timeout and always exits 0, so a stopped bridge never blocks Artisan.
  `-1` (Artisan's "not sampling" value) and unparsable values are sent as null.

## Home Assistant

HACS > Custom repositories > add the repository as an **Integration** > install > restart Home
Assistant. Then Settings > Devices & services: accept the discovered **Artisan Bridge**, or add the
Artisan integration manually with the laptop's IP and port 8765, and paste the token from
`artisan-ha-bridge show-token`.

## Configuration

`init` writes `config.yaml` into the user config directory (readable by you only, since it holds
the token):

| OS | Default path |
|---|---|
| Linux | `~/.config/artisan-ha-bridge/config.yaml` |
| macOS | `~/Library/Application Support/artisan-ha-bridge/config.yaml` |
| Windows | `%LOCALAPPDATA%\artisan-ha-bridge\config.yaml` |

Every command accepts `--config PATH` to use another file.

```yaml
bridge_id: 0f6d...            # stable device identity for Home Assistant; generated by init
unit: C                       # C or F; must match Artisan's temperature mode
weblcds: {host: 127.0.0.1, port: 8080, path: /websocket}
ingest: {port: 8766}          # always bound to 127.0.0.1
ha:
  bind: 0.0.0.0               # interface for Home Assistant; also picks the advertised addresses
  port: 8765
  token: ...                  # secrets.token_urlsafe(32); never logged
  sample_interval_s: 1.0      # per-client sample throttle; events are never throttled
ror_window_s: 30              # least-squares RoR window
offline_after_s: 10           # no WebLCDs frame this long while monitoring = Artisan offline
autosave_dir: null            # Artisan autosave folder for .alog enrichment ([watch] extra)
log: {level: INFO}
```

`init --force` replaces the config with a new token but keeps the `bridge_id`, so Home Assistant
keeps the same device and only asks for the new token (reauthentication).

With `autosave_dir` set to Artisan's autosave folder, the bridge watches for saved `.alog`
profiles. When one matches the last roast (CHARGE within 60 s), the summary is re-sent with the
title, beans and green/roasted weight in grams, and Artisan's computed values fill any event the
bridge missed.

## Commands

| Command | What it does |
|---|---|
| `run` | Default. Start all components; exits cleanly on Ctrl+C / SIGTERM. `--no-discovery` skips zeroconf. |
| `init [--force]` | Create the config with a new `bridge_id` and token; print the token and config path. Refuses to overwrite without `--force`. |
| `show-token` | Print the Home Assistant token from the config. |
| `emit ev <event> <bt> <et> <tm>` / `emit ctl <channel> <value>` | Send one message to the running bridge (Call Program fallback). |
| `print-actions [--fallback]` | Print the Artisan wire-up steps and action table. `--fallback` uses the absolute executable path. |
| `doctor` | Check config sanity, WebLCDs reachability, the ingest and HA ports, and a recent Home Assistant connection. Exits 1 on any failure. |
| `replay <file.jsonl> [--speed N] [--exit] [--no-discovery]` | Play a recorded roast into the bridge with the HA server running, for testing without a roaster. Keeps serving after the end unless `--exit`. |

Try the integration without a roaster:

```sh
artisan-ha-bridge replay tests/fixtures/full_roast.jsonl --speed 10
```

## Run as a service

The bridge runs in your user session, like Artisan. The service files are in `service/`; they
assume pipx's default bin directory (`~/.local/bin`). Check yours with
`artisan-ha-bridge print-actions --fallback`.

### macOS (LaunchAgent)

```sh
sed "s/YOUR_USER/$(whoami)/g" service/org.artisan-ha-bridge.plist > ~/Library/LaunchAgents/org.artisan-ha-bridge.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/org.artisan-ha-bridge.plist
```

`RunAtLoad` starts it at login and `KeepAlive` restarts it. Logs go to
`~/Library/Logs/artisan-ha-bridge.log`. To stop:
`launchctl bootout gui/$(id -u)/org.artisan-ha-bridge`. When macOS asks whether Python may accept
incoming network connections, allow it; Home Assistant cannot connect otherwise.

### Linux (systemd user unit)

```sh
mkdir -p ~/.config/systemd/user
cp service/artisan-ha-bridge.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now artisan-ha-bridge
journalctl --user -u artisan-ha-bridge -f
```

The unit restarts the bridge 5 s after any exit (`Restart=always`, `RestartSec=5`). If a host
firewall is active, allow TCP 8765 (and UDP 5353 for discovery), for example
`sudo ufw allow 8765/tcp` or
`sudo firewall-cmd --permanent --add-port=8765/tcp && sudo firewall-cmd --reload`.

### Windows (Task Scheduler)

In PowerShell, as the user who runs Artisan:

```powershell
Register-ScheduledTask -TaskName "artisan-ha-bridge" -Xml (Get-Content -Raw .\service\artisan-ha-bridge.xml)
Start-ScheduledTask -TaskName "artisan-ha-bridge"
```

The task starts at log on, runs hidden, and restarts every minute after a failure. Allow Home
Assistant through the firewall (elevated prompt):

```bat
netsh advfirewall firewall add rule name="artisan-ha-bridge" dir=in action=allow protocol=TCP localport=8765 profile=private
```

If discovery does not work, also allow mDNS:
`netsh advfirewall firewall add rule name="artisan-ha-bridge mDNS" dir=in action=allow protocol=UDP localport=5353 profile=private`.
Make sure the network is set to **Private**.

## Troubleshooting

Run `artisan-ha-bridge doctor` first. It prints a checklist:

- **WebLCDs** fails: enable WebLCDs in Artisan (Config > Curves > UI) and check the port.
- **Ingest** fails: the bridge is not running, or another program uses port 8766.
- **HA server** fails: the bridge is not running, or `ha.port` is taken.
- **HA client** fails: Home Assistant has not connected in the last 15 minutes. Check the token
  (`show-token`), the IP address and the firewall rules above.

Home Assistant shows no events but temperatures update: the event actions are not reaching the
bridge. Check step 2 of the wire-up (WebSocket host, port and path), or switch to the Call Program
column.

## Protocol

The wire protocol (ingest schema, bridge-to-HA messages, summary fields) is specified in
`artisan-ha-implementation.md`, section 3, at the repository root. Every message to Home
Assistant carries `v` (1), `type` and `ts` (epoch ms). The ingest also serves `GET /status` on
127.0.0.1 for `doctor`; it never includes the token.

## Development

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/ruff format --check . && .venv/bin/ruff check . && .venv/bin/mypy && .venv/bin/pytest
```

Plain `pytest` enforces 100% branch coverage of `state.py`. When running a subset of the tests,
add `--no-cov`; for a whole-package report use
`pytest --cov=artisan_ha_bridge --cov-fail-under=0`.

The input layer sits behind the `TelemetrySource` / `EventSource` protocols in `sources.py`, so a
future Artisan telemetry source can replace WebLCDs and ingest without touching `state.py`.

Fixtures in `tests/fixtures/` are generated by `python tests/make_fixtures.py`. Format: one JSON
object per line, `{"dt": <seconds since start>, "src": "weblcds" | "ingest", "msg": {...}}`.
