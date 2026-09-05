#!/usr/bin/env bash
# run.sh - the single wrapper cron/systemd uses to launch any component.
# It makes sure we run from the project folder and use the project's virtualenv.
#
# Examples:
#   ./run.sh scanner.py
#   ./run.sh executor.py morning
#   ./run.sh executor.py cancel
#   ./run.sh notifier.py weekly
set -euo pipefail

# Folder this script lives in = the project folder.
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

# Use the virtualenv if it exists, otherwise fall back to system python3.
if [ -f "$DIR/.venv/bin/python" ]; then
    PY="$DIR/.venv/bin/python"
else
    PY="python3"
fi

exec "$PY" "$@"
