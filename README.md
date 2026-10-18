# Artisan Roaster Scope for Home Assistant

Live coffee-roast telemetry and roast events from [Artisan](https://artisan-scope.org) in
Home Assistant: bean and environment temperature, rate of rise, roast phase, development time,
DTR, first crack and drop events, and a roast summary for automations.

Artisan is not modified. A small daemon on the roasting laptop, **artisan-ha-bridge**, reads
Artisan's WebLCDs telemetry and event actions, derives the roast state and serves it to this
integration over an authenticated local WebSocket (`local_push`, zeroconf discovery).

```
Artisan ──WebLCDs :8080──▶ artisan-ha-bridge ──WS :8765 (LAN, token)──▶ Home Assistant
        ──WebSocket :8766─▶ (127.0.0.1 only)
```

## Quick start

1. **Laptop (Linux, as the user running Artisan):**

   ```sh
   curl -fsSL https://git.jur.as/dane/artisan-ha-integration/releases/download/latest/install.sh | bash
   ```

   It installs the bridge as a systemd user service and prints the access token. Then run
   `artisan-ha-bridge print-actions` and enter the listed actions in Artisan.

2. **Home Assistant:** HACS > ⋮ > Custom repositories > add this repository's GitHub URL as
   **Integration** > download **Artisan Roaster Scope** > restart. Then accept the discovered
   bridge under Settings > Devices & services and paste the token.

The full guide, including the Artisan configuration table, firewall, manual install and
troubleshooting, is [docs/SETUP.md](docs/SETUP.md). A rendered copy ships with every release as
`SETUP.md`. The entity, event and automation reference is
[docs/home-assistant.md](docs/home-assistant.md).

## What you get

- Sensors: bean/environment temperature, BT/ET rate of rise, roast time, development time, DTR,
  phase, one sensor per Artisan slider channel (heat, fan, …), charge/first-crack/drop
  temperatures and *Last roast completed*.
- Binary sensors: *Bridge connected*, *Artisan running*, *Roasting*.
- An event entity and device triggers for every Artisan event (CHARGE, DRY END, FC START, DROP,
  COOL END, …) and alarm popups.
- Bus events `artisan_event` and `artisan_roast_summary` for automations.

## Repository layout

| Path | Contents |
|---|---|
| `custom_components/artisan/` | Home Assistant integration (installed by HACS) |
| `tests/` | Integration tests (`pytest-homeassistant-custom-component`) |
| `bridge/` | `artisan-ha-bridge` daemon: package, tests, service files, [README](bridge/README.md) |
| `install/install.sh` | Linux installer, published on the release with its download URL filled in |
| `docs/` | Setup guide and Home Assistant reference |
| `.forgejo/workflows/ci.yml` | Build, test and rolling `latest` release |
| `.github/workflows/validate.yml` | hassfest + HACS validation, runs on the GitHub mirror only |

## Development

Bridge (Python 3.11+):

```sh
cd bridge
python3 -m venv .venv && .venv/bin/pip install -e '.[dev,watch]'
.venv/bin/ruff format --check . && .venv/bin/ruff check . && .venv/bin/mypy && .venv/bin/pytest
```

Integration (Python 3.14, matching Home Assistant 2026.10):

```sh
python3.14 -m venv .venv && .venv/bin/pip install -r requirements_test.txt
.venv/bin/ruff format --check custom_components tests && .venv/bin/ruff check custom_components tests
.venv/bin/mypy custom_components && .venv/bin/pytest tests
```

Try the bridge without a roaster: `artisan-ha-bridge replay bridge/tests/fixtures/full_roast.jsonl --speed 10`.

## CI and releases

`.forgejo/workflows/ci.yml` runs on every push and pull request: bridge lint, `mypy --strict`
and tests (on Python 3.12 and 3.11), integration lint, typecheck, tests and hassfest. Each push
to `main` recreates the rolling **`latest`** release with stable asset URLs:

| Asset | Purpose |
|---|---|
| `install.sh` | Linux installer / upgrader / uninstaller |
| `artisan-ha-bridge.tar.gz` | Bridge wheel, installer, systemd/launchd/Task Scheduler files, README |
| `artisan-ha-bridge.service` | systemd --user unit |
| `artisan-integration.zip` | Integration for manual installs |
| `SETUP.md` | Rendered setup guide |

HACS only reads GitHub, so the HACS path needs a GitHub push mirror of this repository
(Forgejo: Settings > Repository > Mirror settings > Push mirror). Set the Forgejo variable
`GITHUB_MIRROR_URL` to the mirror's URL so the rendered `SETUP.md` points at it. The mirror has no
releases, so HACS tracks commits on the default branch.

## License

MIT, see [LICENSE](LICENSE).
