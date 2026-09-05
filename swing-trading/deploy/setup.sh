#!/usr/bin/env bash
# =============================================================================
#  setup.sh - one-time installer. Run this ONCE on your VPS.
#
#  What it does:
#    1. Creates a private Python environment (.venv) inside the project.
#    2. Installs the required Python packages into it.
#    3. Makes the run scripts executable.
#    4. Generates a ready-to-use crontab file with the correct paths.
#    5. Checks that your .env secrets file exists.
#
#  It does NOT place any trades and does NOT turn on the schedule by itself -
#  it just prepares everything and tells you the final command to switch it on.
# =============================================================================
set -euo pipefail

PROJECT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT"
echo "Project folder: $PROJECT"

# --- 1 & 2: virtualenv + packages ---
if [ ! -d "$PROJECT/.venv" ]; then
    echo "Creating Python virtual environment..."
    python3 -m venv "$PROJECT/.venv"
fi
echo "Installing Python packages (this can take a minute)..."
"$PROJECT/.venv/bin/pip" install --quiet --upgrade pip
"$PROJECT/.venv/bin/pip" install --quiet -r "$PROJECT/requirements.txt"

# --- 3: make scripts executable ---
chmod +x "$PROJECT/run.sh" || true

# --- 4: generate crontab with real paths ---
sed "s#__PROJECT__#$PROJECT#g" "$PROJECT/deploy/crontab.txt" > "$PROJECT/deploy/crontab.generated"
echo "Generated schedule: $PROJECT/deploy/crontab.generated"

# --- 5: check secrets ---
ENVFILE="/home/trader/alpaca-trading/.env"
if [ -f "$ENVFILE" ]; then
    echo "Found secrets file: $ENVFILE"
else
    echo "WARNING: secrets file not found at $ENVFILE"
    echo "         Create it using .env.example as a guide."
fi

cat <<EOF

--------------------------------------------------------------------
SETUP COMPLETE.

Next steps (do these in order):

  1. Send yourself a test Telegram message:
       $PROJECT/run.sh notifier.py test

  2. Run the BACKTEST and look at the results BEFORE trading:
       $PROJECT/run.sh backtest.py
       (open $PROJECT/equity_curve.png to see the chart)

  3. Do one manual test scan + execution while watching the output:
       $PROJECT/run.sh scanner.py
       $PROJECT/run.sh executor.py morning

  4. When you are happy, TURN ON the automatic schedule:
       crontab "$PROJECT/deploy/crontab.generated"

     To check it is installed:   crontab -l
     To TURN IT OFF later:       crontab -r
--------------------------------------------------------------------
EOF
