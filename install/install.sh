#!/usr/bin/env bash
# Installs (or upgrades) artisan-ha-bridge on a Linux laptop running Artisan, as a systemd --user
# service. Run it as the same desktop user that runs Artisan, never as root.
#
#   curl -fsSL __RELEASE_BASE_URL__/install.sh | bash
#   curl -fsSL __RELEASE_BASE_URL__/install.sh | bash -s -- --open-firewall
#
# Options:
#   --from FILE        Install from a local artisan-ha-bridge.tar.gz instead of downloading it.
#   --no-service       Install the CLI only; do not install or start the systemd --user service.
#   --open-firewall    Open TCP 8765 (the Home Assistant port) in ufw or firewalld (uses sudo).
#   --uninstall        Stop and remove the service, the CLI and its virtualenv (keeps the config).
#   --purge            With --uninstall, also delete the config (bridge id and token).
#   -h, --help         Show this help.
#
# Environment:
#   ARTISAN_HA_BRIDGE_BASE_URL   Release download base URL (default: the release this script came from).
#   PYTHON                       Python interpreter to build the virtualenv with (default: python3).

# Everything lives in main() so a truncated `curl | bash` download never runs half a script.
main() {
  set -euo pipefail

  local base_url="${ARTISAN_HA_BRIDGE_BASE_URL:-__RELEASE_BASE_URL__}"
  local python="${PYTHON:-python3}"
  local app="artisan-ha-bridge"
  local data_dir="${XDG_DATA_HOME:-$HOME/.local/share}/$app"
  local venv="$data_dir/venv"
  local bin_dir="$HOME/.local/bin"
  local config_dir="${XDG_CONFIG_HOME:-$HOME/.config}/$app"
  local unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
  local ha_port=8765

  local from="" service=1 firewall=0 uninstall=0 purge=0
  while [ $# -gt 0 ]; do
    case "$1" in
      --from) from="${2:?--from needs a file}"; shift 2 ;;
      --no-service) service=0; shift ;;
      --open-firewall) firewall=1; shift ;;
      --uninstall) uninstall=1; shift ;;
      --purge) purge=1; shift ;;
      -h | --help) usage; return 0 ;;
      *) die "unknown option: $1 (see --help)" ;;
    esac
  done

  [ "$(uname -s)" = Linux ] || die "this installer supports Linux only; see SETUP.md for macOS and Windows"
  # A system-wide root install would put the service outside the desktop session Artisan runs in.
  [ "$(id -u)" -ne 0 ] || die "run as the desktop user that runs Artisan, not as root"

  if [ "$uninstall" -eq 1 ]; then
    do_uninstall
    return 0
  fi

  check_python

  local work
  work="$(mktemp -d)"
  # shellcheck disable=SC2064 # expand now: $work is local and gone by the time EXIT fires
  trap "rm -rf '$work'" EXIT

  # Prefer, in order: --from, a bundle sitting next to this script (an extracted tarball), a download.
  local script_dir=""
  if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
    script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  fi
  local bundle
  if [ -n "$from" ]; then
    tar -xzf "$from" -C "$work"
    bundle="$work/$app"
  elif [ -n "$script_dir" ] && compgen -G "$script_dir/artisan_ha_bridge-*.whl" >/dev/null; then
    bundle="$script_dir"
  else
    case "$base_url" in
      __*) die "no release URL baked in; pass --from artisan-ha-bridge.tar.gz or set ARTISAN_HA_BRIDGE_BASE_URL" ;;
    esac
    info "Downloading $base_url/$app.tar.gz"
    download "$base_url/$app.tar.gz" "$work/$app.tar.gz"
    tar -xzf "$work/$app.tar.gz" -C "$work"
    bundle="$work/$app"
  fi

  local wheel
  wheel="$(compgen -G "$bundle/artisan_ha_bridge-*.whl" | sort -V | tail -n 1 || true)"
  [ -n "$wheel" ] || die "no artisan_ha_bridge wheel found in $bundle"

  info "Installing $(basename "$wheel") into $venv"
  mkdir -p "$data_dir" "$bin_dir"
  if [ ! -x "$venv/bin/python" ]; then
    "$python" -m venv "$venv"
  fi
  "$venv/bin/python" -m pip install --quiet --disable-pip-version-check --upgrade pip
  # The [watch] extra enables .alog enrichment (title, beans, weights) when autosave_dir is set.
  "$venv/bin/python" -m pip install --quiet --disable-pip-version-check --upgrade "${wheel}[watch]"
  # Same version number on a rebuilt rolling release would otherwise be skipped as "already satisfied".
  "$venv/bin/python" -m pip install --quiet --disable-pip-version-check --force-reinstall --no-deps "$wheel"
  ln -sf "$venv/bin/$app" "$bin_dir/$app"
  local exe="$bin_dir/$app"

  if [ -f "$config_dir/config.yaml" ]; then
    info "Keeping existing config $config_dir/config.yaml"
  else
    info "Creating config (bridge id and token)"
    "$exe" init >/dev/null
  fi

  if [ "$service" -eq 1 ]; then
    install_service "$bundle"
  fi

  if [ "$firewall" -eq 1 ]; then
    open_firewall
  fi

  case ":$PATH:" in
    *":$bin_dir:"*) ;;
    *) warn "$bin_dir is not on PATH; add it in your shell profile, or call $exe directly" ;;
  esac

  local token
  token="$("$exe" show-token 2>/dev/null || true)"
  cat <<EOF

artisan-ha-bridge $("$venv/bin/python" -c 'import artisan_ha_bridge as m; print(m.__version__)') installed.

  Executable : $exe
  Config     : $config_dir/config.yaml
  HA port    : TCP $ha_port on $(hostname)
  Token      : ${token:-<run: $app show-token>}

Next steps:
  1. Configure Artisan (WebLCDs, WebSocket port, event and slider actions):
       $app print-actions
  2. In Home Assistant: Settings > Devices & services > add "Artisan Roaster Scope"
     (or accept the discovered bridge) and paste the token above.
  3. Check everything:   $app doctor
     Service logs:       journalctl --user -u $app -f
EOF
}

usage() {
  cat <<'EOF'
Usage: install.sh [--from FILE] [--no-service] [--open-firewall] [--uninstall [--purge]]

  --from FILE        Install from a local artisan-ha-bridge.tar.gz instead of downloading it.
  --no-service       Install the CLI only; do not install or start the systemd --user service.
  --open-firewall    Open TCP 8765 (the Home Assistant port) in ufw or firewalld (uses sudo).
  --uninstall        Stop and remove the service, the CLI and its virtualenv (keeps the config).
  --purge            With --uninstall, also delete the config (bridge id and token).
EOF
}

check_python() {
  command -v "$python" >/dev/null 2>&1 || die "$python not found; install Python 3.11 or newer"
  "$python" -c 'import sys; sys.exit(sys.version_info < (3, 11))' ||
    die "$("$python" -V 2>&1) is too old; Python 3.11 or newer is required"
  # Debian/Ubuntu split venv/ensurepip into a separate package.
  "$python" -c 'import venv, ensurepip' 2>/dev/null ||
    die "Python venv support missing; install it (Debian/Ubuntu: sudo apt install python3-venv)"
}

download() {
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL -o "$2" "$1"
  elif command -v wget >/dev/null 2>&1; then
    wget -q -O "$2" "$1"
  else
    die "curl or wget is required"
  fi
}

install_service() {
  local bundle="$1"
  if ! command -v systemctl >/dev/null 2>&1 || ! systemctl --user show-environment >/dev/null 2>&1; then
    warn "systemd --user is not available; start the bridge manually with: $app run"
    return 0
  fi
  [ -f "$bundle/$app.service" ] || die "service unit missing from bundle"
  mkdir -p "$unit_dir"
  install -m 0644 "$bundle/$app.service" "$unit_dir/$app.service"
  systemctl --user daemon-reload
  systemctl --user enable "$app.service" >/dev/null
  # restart (not start) so an upgrade picks up the new code.
  systemctl --user restart "$app.service"
  sleep 2
  if systemctl --user is-active --quiet "$app.service"; then
    info "Service $app is running (starts automatically at login)"
  else
    warn "service failed to start; see: journalctl --user -u $app -e"
  fi
}

open_firewall() {
  if command -v ufw >/dev/null 2>&1 && sudo ufw status 2>/dev/null | grep -q "Status: active"; then
    sudo ufw allow "$ha_port/tcp" comment "artisan-ha-bridge"
    info "Opened TCP $ha_port in ufw"
  elif command -v firewall-cmd >/dev/null 2>&1 && sudo firewall-cmd --state >/dev/null 2>&1; then
    sudo firewall-cmd --permanent --add-port="$ha_port/tcp"
    sudo firewall-cmd --reload
    info "Opened TCP $ha_port in firewalld"
  else
    info "No active ufw/firewalld found; nothing to open"
  fi
}

do_uninstall() {
  if command -v systemctl >/dev/null 2>&1 && [ -f "$unit_dir/$app.service" ]; then
    systemctl --user disable --now "$app.service" 2>/dev/null || true
    rm -f "$unit_dir/$app.service"
    systemctl --user daemon-reload 2>/dev/null || true
    info "Removed service"
  fi
  rm -f "$bin_dir/$app"
  rm -rf "$data_dir"
  info "Removed $data_dir and $bin_dir/$app"
  if [ "$purge" -eq 1 ]; then
    rm -rf "$config_dir"
    info "Removed config $config_dir"
  else
    info "Kept config $config_dir (use --purge to remove it)"
  fi
}

info() { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mwarning:\033[0m %s\n' "$*" >&2; }
die() {
  printf '\033[1;31merror:\033[0m %s\n' "$*" >&2
  exit 1
}

main "$@"
