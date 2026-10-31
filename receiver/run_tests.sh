#!/usr/bin/env bash
# Runs all receiver tests; wraps in a private D-Bus session (if available) so the overlay bridge test runs too.
set -euo pipefail
cd "$(dirname "$0")"
PY=${PYTHON:-python3}
if command -v dbus-run-session >/dev/null; then exec dbus-run-session -- "$PY" -m pytest -v tests "$@"; fi
exec "$PY" -m pytest -v tests "$@"
