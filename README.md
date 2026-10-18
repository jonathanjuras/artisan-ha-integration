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
to `main` recreates the rolling **`latest`** release with stable asset URLs, and publishes the
integration to the GitHub mirror (below):

| Asset | Purpose |
|---|---|
| `install.sh` | Linux installer / upgrader / uninstaller |
| `artisan-ha-bridge.tar.gz` | Bridge wheel, installer, systemd/launchd/Task Scheduler files, README |
| `artisan-ha-bridge.service` | systemd --user unit |
| `artisan-integration.zip` | Integration for manual installs |
| `SETUP.md` | Rendered setup guide |

### GitHub mirror (HACS)

HACS only installs from GitHub, so the `github-mirror` job pushes every `main` build to a GitHub
repository and publishes it as a GitHub release that HACS picks up as an update.

- **Version:** `manifest.json` in this repo holds the base version (`0.1.0`). The mirror gets
  `<base>.<run number>` (e.g. `0.1.0.42`) in a generated `Release <version>` commit on top of the
  source commit, tagged `v<version>`. The source repo is never bumped. Raise the base version in
  source for real version changes.
- **History:** the mirror's `main` is force-pushed every build (source history + one release
  commit), so never commit to the mirror. Version tags keep older releases installable.
- **Setup (Forgejo repo > Settings > Actions):**

  | Kind | Name | Value |
  |---|---|---|
  | Variable | `MIRROR_REPOSITORY` | `<owner>/<repo>` on github.com, e.g. `dane/artisan-ha-integration` |
  | Secret | `MIRROR_TOKEN` | GitHub fine-grained PAT scoped to that repository only, with **Contents: Read and write** and **Workflows: Read and write** (the push includes `.github/workflows/`) |

  Create the GitHub repository empty (no README or license) first. Without the variable or the
  secret the job logs a warning and skips; nothing else fails. The rendered `SETUP.md` points HACS
  users at `https://github.com/<MIRROR_REPOSITORY>`.
- **Version ordering:** the run number only grows while the workflow keeps its name. If it is
  ever reset, raise the base version in `manifest.json` so new builds still sort above old ones.

## License

MIT, see [LICENSE](LICENSE).
