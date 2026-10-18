# Artisan → Home Assistant: Implementation Spec

Spec for Claude Code. Build two deliverables:

1. `artisan-ha-bridge/`: a Python daemon on the Artisan laptop.
2. `ha-artisan/`: a HACS custom integration for Home Assistant.

Treat this file as the source of truth. Where it is silent, follow HA developer docs and current HA core conventions.

---

## 0. Instructions for the implementing agent

- Create two sibling directories, each its own git repo. HACS requires `custom_components/` at the repo root, so do not merge them into one repo.
- Work milestone by milestone (section 9). Do not start a milestone until the previous one's tests pass.
- Python 3.12+ for the bridge (3.11 minimum). The integration must match the Python version of current HA stable.
- Typing: full type hints, `mypy --strict` clean, `ruff` clean (format + lint).
- Async only. No blocking I/O on event loops. No threads except where a library forces one (zeroconf's async API avoids it).
- No new runtime dependencies in the integration (`requirements: []`). aiohttp ships with HA.
- Never log the token. Redact it in diagnostics.
- Ask before deviating from the wire protocol (section 3). Both sides must agree byte-for-byte on field names.

---

## 1. Architecture

```
┌──────────────── Roasting laptop ────────────────┐      ┌──────────── Home Assistant ────────────┐
│                                                  │      │                                         │
│  Artisan (unmodified)                            │      │  custom_components/artisan              │
│   ├─ WebLCDs server :8080 ──telemetry──┐         │      │   ├─ WS client (Bearer token)           │
│   └─ WebSocket client ─────events──────┤         │      │   ├─ sensors / binary_sensors / event   │
│      (event + slider actions)          ▼         │      │   ├─ device triggers                    │
│                          artisan-ha-bridge       │      │   └─ bus events artisan_event,          │
│                           ├─ ingest :8766 (lo) ──┼─LAN─▶│      artisan_roast_summary              │
│                           ├─ state machine, RoR  │ WS   │                │                        │
│                           ├─ HA server :8765     │      │                ▼                        │
│                           └─ zeroconf advert     │      │           Automations                   │
└──────────────────────────────────────────────────┘      └─────────────────────────────────────────┘
```

- **Telemetry:** the bridge connects as a client to Artisan's WebLCDs WebSocket `ws://127.0.0.1:8080/websocket` and receives BT, ET and the timer every sample.
- **Events and controls:** Artisan event actions (ON, START, CHARGE, DRY, FCs, FCe, SCs, SCe, DROP, COOL, OFF, RESET) and slider actions use *WebSocket Command* `send(...)`. Artisan acts as a client to the bridge ingest at `ws://127.0.0.1:8766/artisan`. Fallback: *Call Program* runs `artisan-ha-bridge emit ...`, which POSTs to `http://127.0.0.1:8766/emit`.
- **Bridge → HA:** the bridge runs an authenticated WebSocket server on the LAN (`:8765/v1`), advertised via zeroconf `_artisan-bridge._tcp.local.`. The HA integration is the client (`iot_class: local_push`).
- **Derived data:** the bridge owns the state machine, RoR, development time, DTR, phase and roast summary. Artisan supplies only raw values.

---

## 2. Artisan facts the design depends on (verified in Artisan source, `src/artisanlib/`)

| Fact | Source | Consequence |
|---|---|---|
| WebLCDs pushes `{"data":{"bt":"…","et":"…","time":"mm:ss"}}`, any key may be absent; alarm popups arrive as `{"alert":{"text","title"?,"timeout"?}}` | `canvas.py` `updateWebLCDs`, `weblcds.py` | Values are display strings. The none-symbol is `&nbsp;--` or `&nbsp;&nbsp;-.-` → null. Units follow Artisan's C/F mode. |
| WebLCDs default port 8080, WS path `/websocket`, min send interval 30 ms; last message replayed to new clients | `main.py` `WebLCDsPort`, `weblcds_websocket_path`; `weblcds.py` | Bridge defaults. |
| Event actions substitute `{BT}` `{ET}` `{t}` `{BTB}` `{ETB}` only for Serial, Call Program, Modbus, S7, Artisan Command, WebSocket Command | `main.py` `eventactionx` | Use WebSocket Command or Call Program. IO Command `publish()` gets no substitution. |
| `{t}` = seconds since CHARGE once charged, else since START; −1 values when not sampling | `eventactionx` | `tm` is roast time directly. Treat −1 as null. |
| WebSocket Command `send(<expr>)` → `eval` (300-char limit) → JSON, non-blocking. Client starts lazily on first send | `main.py` action 22, `wsport.py` `send` | The first message after a (re)connect may be dropped. Bridge must infer state from WebLCDs too. |
| Before eval, WebSocket Command replaces `_` with the last read result (when set) and `$` with button state | action 22 | Ingest keys and values must never contain `_` or `$`. |
| Artisan adds an `id` field (random int) and optionally a machine-id field to every WS request | `wsport.py` `send` | Ingest ignores unknown keys and sends no replies. |
| Slider commands go through `str.format(value)` for `{}` | `main.py` slider action | Literal braces break. Use `dict(...)` syntax, never `{...}`. |
| One action per event slot and per slider | Config > Events | Buttons: chain via *Multiple Event*. Sliders: cannot chain (Phase 2). |
| Simulator mode suppresses all actions except Call Program, Multiple Event, Artisan Command | `eventactionx` | Simulator testing uses the Call Program fallback. |
| Extra-device channels, PID SV/duty and RoR have no outbound path | — | Bridge computes RoR. Extra channels wait for Phase 2. |

---

## 3. Wire protocols

### 3.1 Ingest: Artisan → bridge (127.0.0.1 only, no auth)

Transports:
- `GET ws://127.0.0.1:8766/artisan`: Artisan's WebSocket client. Each text frame is one JSON object.
- `POST http://127.0.0.1:8766/emit`: JSON body, same schema, from the `emit` CLI. Reply `204` on success, `400` on schema error.

Schema (short keys, no `_` or `$`):

| Key | Type | Meaning |
|---|---|---|
| `k` | `"ev"` \| `"ctl"` | kind |
| `e` | str | event: `on` `off` `start` `reset` `charge` `dry` `fcs` `fce` `scs` `sce` `drop` `cool` |
| `bt`, `et` | float | `{BT}`, `{ET}`; −1 → null |
| `tm` | float | `{t}` seconds; −1 → null |
| `ch` | str | control channel, `[a-z0-9]{1,16}` (e.g. `heat`, `fan`, `drum`) |
| `v` | float | control value |

Examples (as received from Artisan):

```json
{"k":"ev","e":"charge","bt":185.2,"et":220.1,"tm":0.0,"id":48211}
{"k":"ctl","ch":"heat","v":70.0,"id":1093}
```

Validation: unknown `k` or `e` → log a warning and drop the message. Extra keys are ignored.

### 3.2 Bridge → HA (`ws://<laptop>:8765/v1`)

- Auth: header `Authorization: Bearer <token>`. Compare in constant time (`hmac.compare_digest`). Reject with HTTP 401 before upgrading.
- Framing: one JSON object per text frame. Every message carries `v` (int, currently 1), `type` (str) and `ts` (int, bridge epoch ms).
- Temperatures are in the bridge's configured `unit`. Numbers may be `null`.

| `type` | Fields | When |
|---|---|---|
| `hello` | `bridge_version`, `bridge_id` (stable UUID, persisted in config), `hostname`, `unit` (`"C"`/`"F"`), `protocol` (1) | first frame on connect |
| `snapshot` | `state` (object, see 3.3) | second frame on connect |
| `sample` | `tm`, `bt`, `et`, `bt_ror`, `et_ror` | per WebLCDs frame, throttled per client (default 1 s) |
| `event` | `event`, `tm`, `bt`, `et`, `roast_id` | per ingest event |
| `control` | `channel`, `value` | per ingest control |
| `state` | `phase`, `monitoring`, `recording`, `artisan_online`, `roast_id` | on any change |
| `alert` | `title`, `text` | WebLCDs alert |
| `summary` | see 3.4 | on DROP; re-sent on COOL with updated fields |
| `ping` | — | every 15 s |

The HA client closes and reconnects after 45 s without any frame. Versioning: additive fields are allowed within v1; breaking changes move to `/v2`.

### 3.3 `snapshot.state`

```json
{
  "phase": "development",
  "monitoring": true,
  "recording": true,
  "artisan_online": true,
  "roast_id": "2026-10-17T14:03:12Z",
  "tm": 512.0, "bt": 196.3, "et": 225.0, "bt_ror": 9.8, "et_ror": 4.1,
  "dev_time": 48.0, "dtr": 9.4,
  "controls": {"heat": 70.0, "fan": 40.0},
  "events": {"charge": {"tm": 0.0, "bt": 185.2}, "dry": {"tm": 245.0, "bt": 152.1}, "fcs": {"tm": 464.0, "bt": 194.0}},
  "last_event": {"event": "fcs", "ts": 1792252483254},
  "last_summary": null
}
```

### 3.4 `summary`

```json
{
  "v": 1, "type": "summary", "ts": 1792252483254,
  "roast_id": "2026-10-17T14:03:12Z",
  "final": false,
  "charge_bt": 185.2,
  "dry_tm": 245.0, "dry_bt": 152.1,
  "fcs_tm": 464.0, "fcs_bt": 194.0,
  "drop_tm": 620.0, "drop_bt": 208.4,
  "dev_time": 156.0, "dtr": 25.2,
  "cool_tm": null,
  "title": null, "beans": null, "weight_in_g": null, "weight_out_g": null
}
```

`final: true` on the COOL (or OFF after DROP) re-send. `.alog` enrichment fills `title`, `beans` and weights when `alog_watch` is enabled.

---

## 4. Bridge (`artisan-ha-bridge`)

### 4.1 Layout

```
artisan-ha-bridge/
  pyproject.toml          # hatchling; deps: aiohttp, zeroconf, platformdirs, pyyaml; extra [watch]: watchdog
  src/artisan_ha_bridge/
    __init__.py           # __version__
    __main__.py           # argparse CLI: run | init | emit | print-actions | doctor | replay
    config.py             # dataclass Config, load/save YAML, token + bridge_id generation
    weblcds_client.py
    ingest.py
    state.py
    ror.py
    ha_server.py
    discovery.py
    alog_watch.py
    protocol.py           # TypedDicts for all messages, builders, validation
    logging_setup.py
  service/
    org.artisan-ha-bridge.plist      # macOS LaunchAgent
    artisan-ha-bridge.service        # systemd --user
    artisan-ha-bridge.xml            # Windows Task Scheduler
  tests/
    fixtures/             # recorded WebLCDs frames + ingest sequences (JSONL with relative timestamps)
```

### 4.2 Config (`config.yaml` under `platformdirs.user_config_dir("artisan-ha-bridge")`)

```yaml
bridge_id: <uuid4, generated by init>
unit: C
weblcds: {host: 127.0.0.1, port: 8080, path: /websocket}
ingest: {port: 8766}
ha: {bind: 0.0.0.0, port: 8765, token: <secrets.token_urlsafe(32)>, sample_interval_s: 1.0}
ror_window_s: 30
offline_after_s: 10
autosave_dir: null
log: {level: INFO}
```

### 4.3 Components

**`weblcds_client`**
- Connect to `ws://{host}:{port}{path}`. On failure or close, retry every 2 s indefinitely.
- Parse: `html.unescape`, strip whitespace. Values matching `^-?[\d.]+$` → float; anything else (`--`, `-.-`) → null.
- `time` `"mm:ss"` → seconds (negative allowed, e.g. `-0:05` during charge delay).
- Forward `alert` to `state.on_alert`.
- Each frame calls `state.on_sample(bt, et, tm, now)`. Missing keys keep previous values.
- Liveness: no frame for `offline_after_s` while monitoring → `state.set_artisan_online(False)`. Socket closed → offline immediately.

**`ingest`**
- aiohttp server bound to `127.0.0.1:{ingest.port}` with routes `/artisan` (WS) and `/emit` (POST).
- Validate via `protocol.parse_ingest` and dispatch to `state.on_event` / `state.on_control`.

**`state`** (pure logic, no I/O, injectable clock; fully unit-tested)

Phases: `off`, `monitoring`, `recording`, `drying`, `maillard`, `development`, `cooling`, `finished`.

| From | Trigger | To | Side effects |
|---|---|---|---|
| `off` | `on` event, or a WebLCDs frame | `monitoring` | `monitoring=true`, `artisan_online=true` |
| `monitoring` | `start` | `recording` | `recording=true` |
| `monitoring` / `recording` | `charge` | `drying` | `recording=true`, new `roast_id` = UTC ISO of charge, reset events/RoR buffers |
| `drying` | `dry` | `maillard` | |
| `drying` / `maillard` | `fcs` | `development` | infer `maillard` if `dry` was skipped |
| `development` | `fce`, `scs`, `sce` | `development` | record only |
| `drying` / `maillard` / `development` | `drop` | `cooling` | emit `summary` (`final=false`) |
| `cooling` | `cool` | `finished` | emit `summary` (`final=true`) |
| `cooling` / `finished` | `off` | `off` | if not yet final, emit final `summary` |
| any | `off` | `off` | `monitoring=false`, `recording=false` |
| any | `reset` | `monitoring` if Artisan online, else `off` | clear the roast (not `last_summary`) |
| any non-off | artisan offline | unchanged phase | `artisan_online=false`; emit `state` |

- Duplicate events (same `e` within the same roast) overwrite the earlier record.
- Out-of-order events (e.g. `fcs` before `charge`) are recorded, log a warning, and do not change phase.
- `dev_time` = `tm − events.fcs.tm` while in development; frozen at drop.
- `dtr` = `dev_time / drop_tm × 100` (live: uses current `tm`). One decimal.
- `controls[ch] = v`.
- Every mutation produces one or more outbound messages through a listener callback. The state module never touches sockets.

**`ror`**
- Ring buffer of `(tm_or_monotonic, value)`. RoR = least-squares slope over the last `ror_window_s` × 60 → °/min.
- Needs at least 5 points spanning ≥ 50% of the window; otherwise null. Reset on `charge`. One decimal.
- Before charge, use monotonic time as the x-axis; after charge, use `tm`.

**`ha_server`**
- aiohttp server bound to `ha.bind:ha.port`, route `/v1`. Check auth before `ws.prepare`.
- On connect, send `hello` then `snapshot`.
- Per-client sample throttle: send the latest sample at most every `sample_interval_s`; never queue stale samples.
- Events, controls, state, alerts and summaries are never throttled.
- `ping` every 15 s. Remove a client on send failure. Use a bounded per-client queue (256); on overflow, drop the client.

**`discovery`**
- `zeroconf.asyncio.AsyncZeroconf`. Service `_artisan-bridge._tcp.local.`, name `Artisan Bridge (<hostname>)._artisan-bridge._tcp.local.`.
- TXT: `id=<bridge_id>`, `version=<ver>`, `path=/v1`, `protocol=1`.
- Choose the advertised address from the bind interface; if the bind is `0.0.0.0`, use all non-loopback IPv4 addresses.

**`alog_watch`** (optional, `[watch]` extra)
- Watch `autosave_dir` for new or modified `*.alog` files, debounced 2 s.
- Parse with `ast.literal_eval` (never `eval`).
- Extract `title`, `beans`, `weight` (`[in, out, unit]` → grams) and the `computed` dict when present.
- If `roast_id` matches the last summary by charge time (±60 s), send the enriched `summary`.

### 4.4 CLI

| Command | Behaviour |
|---|---|
| `run` | Default. Start all components; exit cleanly on SIGINT/SIGTERM. |
| `init [--force]` | Create config with `bridge_id` and token. Print the token and config path. Refuse to overwrite without `--force`. |
| `emit ev <event> <bt> <et> <tm>` / `emit ctl <channel> <value>` | POST to ingest. Timeout 1 s. Exit 0 even on failure (never block Artisan); log to stderr. |
| `print-actions [--fallback]` | Print the Artisan action table (section 6) using the configured ingest port. With `--fallback`, print absolute paths to this executable. |
| `doctor` | Check WebLCDs reachability, ingest bound, HA port bound, a recent HA client connection, and config sanity. Exit 1 on any failure. |
| `replay <file.jsonl> [--speed N]` | Feed recorded WebLCDs frames and ingest messages into `state` with the HA server running. For testing without a roaster. |

### 4.5 Services

Run in the user session (Artisan does):
- **macOS:** LaunchAgent with `RunAtLoad` and `KeepAlive`; `ProgramArguments` = pipx bin path + `run`.
- **Linux:** `systemd --user`, `Restart=always`, `RestartSec=5`.
- **Windows:** Task Scheduler "At log on", restart on failure every 1 min, hidden. Document the inbound firewall rule for TCP 8765 (`netsh advfirewall firewall add rule ...`).

---

## 5. HA integration (`ha-artisan`)

### 5.1 Layout

```
ha-artisan/
  hacs.json
  README.md
  LICENSE
  .github/workflows/validate.yml    # hassfest + hacs/action
  .github/workflows/tests.yml       # pytest
  requirements_test.txt             # pytest-homeassistant-custom-component
  custom_components/artisan/
    __init__.py
    manifest.json
    const.py
    models.py           # RoastState dataclass + apply(message) reducer
    client.py           # ArtisanBridgeClient
    config_flow.py
    entity.py
    sensor.py
    binary_sensor.py
    event.py
    device_trigger.py
    diagnostics.py
    strings.json
    translations/en.json
    icons.json
  tests/
    conftest.py         # fake bridge aiohttp server fixture
    test_config_flow.py
    test_client.py
    test_sensor.py
    test_event.py
    test_device_trigger.py
    test_diagnostics.py
```

### 5.2 `hacs.json`

```json
{"name": "Artisan Roaster Scope", "render_readme": true, "homeassistant": "<min HA version tested>"}
```

### 5.3 `manifest.json`

```json
{
  "domain": "artisan",
  "name": "Artisan Roaster Scope",
  "version": "0.1.0",
  "codeowners": ["@<owner>"],
  "config_flow": true,
  "dependencies": [],
  "documentation": "https://github.com/<owner>/ha-artisan",
  "issue_tracker": "https://github.com/<owner>/ha-artisan/issues",
  "integration_type": "device",
  "iot_class": "local_push",
  "requirements": [],
  "zeroconf": ["_artisan-bridge._tcp.local."]
}
```

### 5.4 Runtime

- `async_setup_entry`: create `ArtisanBridgeClient(hass, host, port, token)`, store `ArtisanRuntimeData(client, state)` in `entry.runtime_data`, start the client as a background task (`entry.async_create_background_task`), and forward platforms `[SENSOR, BINARY_SENSOR, EVENT]`. Do not block setup on the first connection; entities start unavailable.
- `client.py`:
  - `async_get_clientsession(hass)`; `ws_connect(url, headers={"Authorization": f"Bearer {token}"}, heartbeat=30)`.
  - Backoff 1, 2, 4 … 60 s with jitter; reset after a successful `hello`.
  - HTTP 401 → `entry.async_start_reauth(hass)` and stop retrying.
  - Receive loop: parse JSON → `models.apply(state, msg)` → `async_dispatcher_send(hass, signal(entry_id))`.
  - On `event` and `alert`: `hass.bus.async_fire("artisan_event", {device_id, event, tm, bt, et, roast_id, text?, title?})`.
  - On `summary`: `hass.bus.async_fire("artisan_roast_summary", {device_id, **summary})`.
  - On `hello`: update the device registry entry (`sw_version` = bridge version, `name` = `Artisan (<hostname>)`).
  - 45 s with no frame → close and reconnect.
- `entity.py`: `ArtisanEntity` with `_attr_has_entity_name = True`, `should_poll = False`, and `device_info` keyed by `bridge_id`. `available` = client connected (except the *Bridge connected* sensor itself). Subscribe in `async_added_to_hass` and call `async_write_ha_state` on signal.
- `unique_id` = `f"{bridge_id}_{key}"`.

### 5.5 Config flow

- **user:** `host`, `port` (default 8765), `token`. Validate by connecting and awaiting `hello` within 10 s. Errors: `cannot_connect`, `invalid_auth`, `unknown`. `async_set_unique_id(bridge_id)` + `_abort_if_unique_id_configured(updates={host, port})`.
- **zeroconf:** read `id` from TXT, set the unique ID, abort if configured (updating host/port). Show a confirm form asking for the token only.
- **reauth:** token only.
- **options:** `sample_interval_s` (1–10, client-side throttle of state writes) and `ror_precision` (0–2).

### 5.6 Entities

Temperature unit: `UnitOfTemperature.CELSIUS` or `FAHRENHEIT` from `hello.unit`.

| key | Platform | Name | device_class / unit / state_class | Value |
|---|---|---|---|---|
| `bt` | sensor | Bean temperature | temperature / °C·°F / measurement | `state.bt` |
| `et` | sensor | Environment temperature | temperature / °C·°F / measurement | `state.et` |
| `bt_ror` | sensor | BT rate of rise | — / `°C/min` or `°F/min` / measurement | `state.bt_ror` |
| `et_ror` | sensor | ET rate of rise | same | `state.et_ror` |
| `roast_time` | sensor | Roast time | duration / s / — | `state.tm` once charged, else null |
| `dev_time` | sensor | Development time | duration / s | `state.dev_time` |
| `dtr` | sensor | Development time ratio | — / % / measurement | `state.dtr` |
| `phase` | sensor | Phase | enum, options = 8 phases | `state.phase` |
| `control_<ch>` | sensor | `<Ch>` (e.g. Heat) | — / % / measurement | created dynamically on first `control` for a channel |
| `charge_bt` / `fcs_bt` / `drop_bt` | sensor | Charge / First crack / Drop temperature | temperature | last summary |
| `last_roast` | sensor | Last roast completed | timestamp | `ts` of the last final summary; attributes = summary fields |
| `connected` | binary_sensor | Bridge connected | connectivity; entity_category diagnostic | socket state |
| `artisan_online` | binary_sensor | Artisan running | running | `state.artisan_online` |
| `roasting` | binary_sensor | Roasting | running | phase in `drying`/`maillard`/`development` |
| `roast_event` | event | Roast event | event_types = 12 ingest events + `alert` | `_trigger_event(type, attrs)` |

Dynamic control sensors: platform setup registers a callback that adds an entity the first time a channel appears (in a snapshot or a `control` message). Restore them on restart from the entity registry.

### 5.7 Device triggers

- `device_trigger.py`: trigger types = the 12 event names + `alert`, implemented as an event trigger on `artisan_event` with `event_data: {device_id, event}`.
- Translations: e.g. `fcs` → "First crack started", `drop` → "Beans dropped", `cool` → "Cooling ended".

### 5.8 Diagnostics

Return the entry data with `token` redacted, the current `RoastState`, connection status and the last 20 messages (ring buffer in the client).

### 5.9 Recorder guidance (README)

A 15-minute roast at 1 s produces about 900 states per sensor. Document the `sample_interval_s` option and a `recorder: exclude: entity_globs:` example for the RoR sensors.

---

## 6. Artisan wire-up (README content + `print-actions` output)

1. **Config > Curves > UI tab:** enable **WebLCDs**, port 8080. Tick **Alarm Popups** to forward alarms.
2. **Config > Port > WebSocket tab:** host `127.0.0.1`, port `8766`, path `artisan`. If the roaster already uses the WebSocket device, use the Call Program column instead.
3. **Config > Events > Buttons:** for each slot, Action = **WebSocket Command** with the string below. If a slot already drives the roaster, move the original action to a hidden custom button, add a second hidden button carrying the WebSocket Command, and set the slot to **Multiple Event** for both.
4. **Config > Events > Sliders:** for sliders not already bound to roaster control, Action = **WebSocket Command** with the slider string.
5. Optional: **Config > Alarms** with a Pop Up action; these reach HA as `alert`.
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

On Windows, Call Program needs the absolute path to the pipx shim (`print-actions --fallback` prints it).

**Home Assistant:** HACS > Custom repositories > add the repo as Integration > install > restart. Then Settings > Devices & services: accept the discovered bridge (or add it manually with IP and 8765) and paste the token.

---

## 7. Example automation (README)

```yaml
alias: Roast finished
triggers:
  - trigger: event
    event_type: artisan_roast_summary
    event_data:
      final: false
actions:
  - action: notify.mobile_app_<phone>
    data:
      title: Roast dropped
      message: >
        {{ (trigger.event.data.drop_tm / 60) | round(1) }} min,
        drop BT {{ trigger.event.data.drop_bt }}°,
        DTR {{ trigger.event.data.dtr }}%
```

More README examples: first-crack TTS (device trigger `fcs`), cooling done (`cool`), exhaust plug on while *Roasting* plus 10 min after `finished`, overheat guard (BT above a limit while *Roasting*), lost telemetry (*Bridge connected* or *Artisan running* off for 30 s while *Roasting*), alarm forwarding (`alert`).

---

## 8. Phase 2 (out of scope for this build; do not implement)

An upstream Artisan PR adding a `/telemetry` WebSocket (`TelemetryServer(WebView)` in `weblcds.py`). It would be fed from:
- `canvas.py` `sample_processing`: all channels, extra devices, sliders, PID.
- `markCharge`, `markDryEnd`, `mark1Cstart`, `mark1Cend`, `mark2Cstart`, `mark2Cend`, `markDrop`, `markCoolEnd`, `EventRecordAction`.
- Slider handlers.
- `OnMonitor`, `OffMonitor`, `OnRecorder`, `OffRecorder`, `reset`.

Settings would sit in Config > Curves > UI. The bridge would add `telemetry_client.py` and prefer it when available. Keep the bridge's input layer behind an interface (`TelemetrySource`, `EventSource`) so Phase 2 slots in without touching `state`.

---

## 9. Milestones and definition of done

| # | Milestone | Done when |
|---|---|---|
| M1 | Bridge `protocol`, `state`, `ror` | Unit tests cover every transition row in 4.3, plus: dropped `on`, skipped `dry`, `reset` mid-roast, duplicate events, out-of-order events, −1 substitutions, RoR null/valid thresholds. 100% branch coverage on `state.py`. |
| M2 | `weblcds_client`, `ingest` | Tests against a fake WebLCDs server (aiohttp) and real HTTP/WS ingest clients; parsing tests for none-symbols, HTML entities, negative times, partial frames. |
| M3 | `ha_server`, `discovery`, CLI `init`/`emit`/`print-actions`/`replay` | Integration test: replay fixture → WS client receives `hello`, `snapshot`, throttled `sample`s, all events and two `summary` messages in order. 401 on a bad token. |
| M4 | `doctor`, `alog_watch`, service files, README | `doctor` passes on a dev machine with a fake WebLCDs server. A sample `.alog` fixture parses into an enriched summary. |
| M5 | HA integration: client, models, config flow, sensors, binary sensors | `pytest-homeassistant-custom-component` tests: user/zeroconf/reauth/options flows; entities update from a fake bridge; unavailable on disconnect; reconnect restores state from snapshot. |
| M6 | Event entity, device triggers, diagnostics, dynamic control sensors | Tests fire each event type; a device-trigger automation fires; diagnostics redact the token. |
| M7 | CI + release | `hassfest` and `hacs/action` pass; bridge `ruff` + `mypy --strict` + pytest pass in CI; tag `v0.1.0` in both repos. |
| M8 | Field test (manual, user) | Three real roasts; HA event times within 1 s of the `.alog`; BT matches the Artisan display. |

### Test fixture format (`tests/fixtures/*.jsonl`)

One object per line: `{"dt": <seconds since start>, "src": "weblcds" | "ingest", "msg": {...}}`. Provide at least:
- `full_roast.jsonl`: ON → START → CHARGE → DRY → FCs → DROP → COOL → OFF at 1 Hz.
- `dropped_on.jsonl`: no ON message.
- `reset_mid_roast.jsonl`.
- `artisan_crash.jsonl`: WebLCDs stops mid-roast.
