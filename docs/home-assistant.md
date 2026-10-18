# Home Assistant integration

The `artisan` custom integration (`custom_components/artisan/`) connects Home Assistant to
`artisan-ha-bridge` over an authenticated WebSocket (`ws://<laptop>:8765/v1`). It is push-only
(`local_push`): the bridge sends every change and Home Assistant never polls.

Requires Home Assistant 2026.10 or later.

## Setup

1. Install through HACS (Custom repositories, category **Integration**) and restart Home Assistant.
2. Settings > Devices & services:
   - **Discovered:** the bridge advertises `_artisan-bridge._tcp.local.`, so it appears as
     *Artisan Bridge (&lt;hostname&gt;)*. Select **Add** and paste the token.
   - **Manual:** **Add integration** > *Artisan Roaster Scope*, then enter the host (IP or
     hostname of the roasting laptop), the port (default `8765`) and the token.
3. The token is `ha.token` in the bridge's `config.yaml`. `artisan-ha-bridge init` prints it.

The flow connects and waits up to 10 s for the bridge's `hello` before it creates the entry. One
entry is created per bridge, keyed by the bridge's `bridge_id`. If the bridge's IP or port
changes, discovery (or adding it manually again) updates the existing entry instead of adding a
second one.

If the bridge rejects the token later (for example after `artisan-ha-bridge init --force`), the
integration stops reconnecting and Home Assistant shows a reauthentication notification that asks
for the new token.

## Options

Settings > Devices & services > Artisan > **Configure**:

| Option | Range | Default | Effect |
|---|---|---|---|
| Sample interval (`sample_interval_s`) | 1–10 s | 1 s | Minimum time between state writes of the high-rate sensors: bean/environment temperature, both rates of rise, roast time, development time and DTR. The latest value is written when the interval ends. Snapshots, events, phase changes and availability are never delayed. |
| Rate of rise precision (`ror_precision`) | 0–2 | 1 | Decimals of the two rate-of-rise sensors (rounds the stored state and sets the display precision). |

Changing options reloads the entry.

## Entities

All entities belong to one device, *Artisan (&lt;hostname&gt;)*, whose firmware version is the bridge
version. Entity IDs below assume the hostname `roaster`. Everything except *Bridge connected* is
unavailable while the bridge is disconnected.

| Entity | Type | Unit | Value |
|---|---|---|---|
| `sensor.artisan_roaster_bean_temperature` | temperature, measurement | °C / °F | BT |
| `sensor.artisan_roaster_environment_temperature` | temperature, measurement | °C / °F | ET |
| `sensor.artisan_roaster_bt_rate_of_rise` | measurement | °C/min or °F/min | BT RoR (computed by the bridge) |
| `sensor.artisan_roaster_et_rate_of_rise` | measurement | °C/min or °F/min | ET RoR |
| `sensor.artisan_roaster_roast_time` | duration | s | Seconds since CHARGE; unknown before CHARGE |
| `sensor.artisan_roaster_development_time` | duration | s | Seconds since first crack; frozen at DROP |
| `sensor.artisan_roaster_development_time_ratio` | measurement | % | DTR |
| `sensor.artisan_roaster_phase` | enum | | `off`, `monitoring`, `recording`, `drying`, `maillard`, `development`, `cooling`, `finished` |
| `sensor.artisan_roaster_<channel>` | measurement | % | Last value of an Artisan slider (for example `heat`, `fan`). Created the first time the channel appears; kept across restarts. |
| `sensor.artisan_roaster_charge_temperature` | temperature | °C / °F | Charge BT from the last summary |
| `sensor.artisan_roaster_first_crack_temperature` | temperature | °C / °F | FCs BT from the last summary |
| `sensor.artisan_roaster_drop_temperature` | temperature | °C / °F | Drop BT from the last summary |
| `sensor.artisan_roaster_last_roast_completed` | timestamp | | Time of the last final summary; attributes hold all summary fields |
| `binary_sensor.artisan_roaster_bridge_connected` | connectivity, diagnostic | | WebSocket to the bridge is up |
| `binary_sensor.artisan_roaster_artisan_running` | running | | Artisan is sending data to the bridge |
| `binary_sensor.artisan_roaster_roasting` | running | | Phase is `drying`, `maillard` or `development` |
| `event.artisan_roaster_roast_event` | event | | Last roast event or alert (see below) |

Temperatures arrive in the bridge's unit (`unit` in `config.yaml`) and Home Assistant converts
them to your unit system like any temperature sensor. The rate-of-rise sensors are not converted;
their unit follows the bridge.

Control sensors are always reported in %. A channel you no longer use can be disabled from its
entity settings.

## Events

### `artisan_event`

Fired for every Artisan event button and every Artisan alarm popup.

| Field | Description |
|---|---|
| `device_id` | Home Assistant device ID of the bridge |
| `event` | `on`, `start`, `charge`, `dry`, `fcs`, `fce`, `scs`, `sce`, `drop`, `cool`, `off`, `reset` or `alert` |
| `tm` | Roast time in seconds (since CHARGE once charged, else since START); `null` when not sampling |
| `bt`, `et` | Temperatures at the event, in the bridge's unit; may be `null` |
| `roast_id` | ID of the current roast (UTC ISO time of CHARGE), or `null` |
| `title`, `text` | Alerts only: the popup's title (may be `null`) and text |

For alerts, `tm`, `bt`, `et` and `roast_id` are the latest known values when the alert arrives.

The `event.artisan_roaster_roast_event` entity fires with the same event types; its attributes are
`tm`, `bt`, `et`, `roast_id` (plus `title`, `text` for alerts). Reconnecting does not replay old
events.

### `artisan_roast_summary`

Fired at DROP with `final: false`, then again at COOL (or OFF after DROP) with `final: true`. When
the bridge's `.alog` enrichment is enabled, a further summary with `title`, `beans` and weights
filled in can follow.

| Field | Description |
|---|---|
| `device_id` | Home Assistant device ID of the bridge |
| `ts` | Bridge time of the summary, epoch milliseconds |
| `roast_id` | Roast ID |
| `final` | `false` at DROP, `true` once cooling ended |
| `charge_bt` | BT at CHARGE |
| `dry_tm`, `dry_bt` | Time and BT at DRY END |
| `fcs_tm`, `fcs_bt` | Time and BT at FC START |
| `drop_tm`, `drop_bt` | Time and BT at DROP |
| `dev_time` | Development time in seconds |
| `dtr` | Development time ratio in % |
| `cool_tm` | Time at COOL END, `null` until known |
| `title`, `beans`, `weight_in_g`, `weight_out_g` | From the `.alog` file, else `null` |

Times are seconds since CHARGE; temperatures are in the bridge's unit. Any field may be `null`.
New fields may be added in later bridge versions.

## Device triggers

In the automation editor, choose **Device** > *Artisan (&lt;hostname&gt;)*:

| Trigger | Event |
|---|---|
| Monitoring started (ON) | `on` |
| Recording started (START) | `start` |
| Beans charged | `charge` |
| Drying ended | `dry` |
| First crack started | `fcs` |
| First crack ended | `fce` |
| Second crack started | `scs` |
| Second crack ended | `sce` |
| Beans dropped | `drop` |
| Cooling ended | `cool` |
| Monitoring stopped (OFF) | `off` |
| Roast reset | `reset` |
| Artisan alert | `alert` |

A device trigger is an event trigger on `artisan_event` with `event_data: {device_id, event}`, so
`trigger.event.data` holds the fields above. In YAML, replace a device trigger by that event
trigger if you do not want to look up the device ID.

## Recorder

A 15-minute roast at 1 s produces about 900 states per high-rate sensor. To store less:

- Raise **Sample interval** (see Options). At 5 s a roast stores about 180 states per sensor.
- Exclude the noisiest sensors from the recorder. The rates of rise are the usual candidates:

```yaml
recorder:
  exclude:
    entity_globs:
      - sensor.artisan_*_rate_of_rise
```

Excluded sensors keep working in automations and on dashboards; they have no history or
statistics.

## Example automations

Replace `roaster` in entity IDs with your bridge's hostname, and `<phone>`, media players and
switches with your own.

### Roast finished

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

### First crack announcement (TTS)

```yaml
alias: First crack announcement
triggers:
  - trigger: device
    domain: artisan
    device_id: <bridge device id>
    type: fcs
actions:
  - action: tts.speak
    target:
      entity_id: tts.home_assistant_cloud
    data:
      media_player_entity_id: media_player.kitchen
      message: >
        First crack at {{ (trigger.event.data.tm / 60) | round(1) }} minutes,
        {{ trigger.event.data.bt | round(0) }} degrees.
```

### Cooling done

```yaml
alias: Cooling done
triggers:
  - trigger: event
    event_type: artisan_event
    event_data:
      event: cool
actions:
  - action: notify.mobile_app_<phone>
    data:
      message: Beans are cool. Empty the cooling tray.
```

### Exhaust fan while roasting, plus 10 minutes after

Turns a smart plug on when the roast starts and off 10 minutes after the roast has finished (or
Artisan was switched off).

```yaml
alias: Roaster exhaust fan
mode: restart
triggers:
  - trigger: state
    entity_id: binary_sensor.artisan_roaster_roasting
    to: "on"
    id: start
  - trigger: state
    entity_id: sensor.artisan_roaster_phase
    to:
      - finished
      - "off"
    for:
      minutes: 10
    id: stop
actions:
  - choose:
      - conditions:
          - condition: trigger
            id: start
        sequence:
          - action: switch.turn_on
            target:
              entity_id: switch.exhaust_plug
      - conditions:
          - condition: trigger
            id: stop
        sequence:
          - action: switch.turn_off
            target:
              entity_id: switch.exhaust_plug
```

### Overheat guard

The threshold is in your Home Assistant unit system.

```yaml
alias: Roaster overheat guard
triggers:
  - trigger: numeric_state
    entity_id: sensor.artisan_roaster_bean_temperature
    above: 230
conditions:
  - condition: state
    entity_id: binary_sensor.artisan_roaster_roasting
    state: "on"
actions:
  - action: notify.mobile_app_<phone>
    data:
      title: Roaster overheating
      message: BT is {{ states('sensor.artisan_roaster_bean_temperature') }}°.
      data:
        push:
          interruption-level: critical
```

### Lost telemetry during a roast

When the bridge connection drops, every roast entity becomes unavailable, so *Roasting* changing
from `on` to `unavailable` stands for a lost bridge. When only Artisan stops (crash, laptop
asleep), the bridge stays connected, *Roasting* keeps its last value and *Artisan running* turns
off.

```yaml
alias: Roast telemetry lost
triggers:
  - trigger: state
    entity_id: binary_sensor.artisan_roaster_roasting
    from: "on"
    to: unavailable
    for:
      seconds: 30
    id: bridge
  - trigger: state
    entity_id: binary_sensor.artisan_roaster_artisan_running
    to: "off"
    for:
      seconds: 30
    id: artisan
conditions:
  - condition: or
    conditions:
      - condition: trigger
        id: bridge
      - condition: state
        entity_id: binary_sensor.artisan_roaster_roasting
        state: "on"
actions:
  - action: notify.mobile_app_<phone>
    data:
      title: Roast telemetry lost
      message: >
        {{ 'Bridge unreachable' if trigger.id == 'bridge' else 'Artisan stopped responding' }}
        for 30 s during a roast.
```

### Alarm forwarding

Artisan alarms with a *Pop Up* action (Config > Alarms) reach Home Assistant as alerts.

```yaml
alias: Forward Artisan alarms
triggers:
  - trigger: event
    event_type: artisan_event
    event_data:
      event: alert
actions:
  - action: notify.mobile_app_<phone>
    data:
      title: "{{ trigger.event.data.title or 'Artisan alarm' }}"
      message: "{{ trigger.event.data.text }}"
```

## Diagnostics

Settings > Devices & services > Artisan > ⋮ > **Download diagnostics** returns:

- the entry data (with `token` redacted) and options,
- the connection status (connected, host, port, failed attempts since the last `hello`, messages
  received),
- the current roast state as Home Assistant sees it,
- the last 20 messages from the bridge (pings excluded).

## Troubleshooting

| Symptom | Check |
|---|---|
| *Failed to connect to the bridge* | The bridge runs (`artisan-ha-bridge doctor`), listens on the LAN (`ha.bind: 0.0.0.0`), and the laptop firewall allows inbound TCP 8765. From another machine, `curl -i http://<laptop>:8765/v1` must answer `401`. |
| *The bridge rejected the token* | Copy `ha.token` from the bridge's `config.yaml` again. Tokens change on `init --force`. |
| Bridge not discovered | mDNS does not cross VLANs/subnets without a reflector; add it manually. |
| All entities unavailable | *Bridge connected* is off: the laptop is asleep, the bridge stopped or the network dropped. The integration retries with backoff (1 s doubling to 60 s) and restores the full state from the bridge's snapshot when it reconnects. |
| Values frozen, *Artisan running* off | The bridge is up but Artisan is closed, not monitoring, or WebLCDs is disabled (Config > Curves > UI). |
| Events never fire | The Artisan event buttons lack the *WebSocket Command* actions (see the Artisan setup docs). Telemetry alone (WebLCDs) carries no events. |
| No control sensors | They appear on the first slider move after the slider has a *WebSocket Command* action. |
| Temperatures in the wrong unit | Sensors follow Home Assistant's unit system; event payloads and summaries use the bridge's `unit`. |

Debug logging:

```yaml
logger:
  logs:
    custom_components.artisan: debug
```

The token is never logged.
