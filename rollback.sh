#!/usr/bin/env bash
set -euo pipefail
HERE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
USER_BUS="/run/user/$(id -u)/bus"
if [[ -z "${XDG_RUNTIME_DIR:-}" && -S "$USER_BUS" ]]; then export XDG_RUNTIME_DIR="${USER_BUS%/bus}"; fi
if [[ -z "${DBUS_SESSION_BUS_ADDRESS:-}" && -S "$USER_BUS" ]]; then export DBUS_SESSION_BUS_ADDRESS="unix:path=$USER_BUS"; fi
if [[ "${1:-}" == "list-backups" ]]; then
  shift
  exec python3 "$HERE/lib/install.py" list-backups "$@"
fi
exec python3 "$HERE/lib/install.py" rollback "$@"
