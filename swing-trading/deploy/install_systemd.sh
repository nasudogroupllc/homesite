#!/usr/bin/env bash
# =============================================================================
#  install_systemd.sh - OPTIONAL alternative to cron, using systemd timers.
#
#  Most people should just use cron (run deploy/setup.sh, then the crontab line
#  it prints). Use this ONLY if you prefer systemd. Run it with sudo:
#       sudo deploy/install_systemd.sh
#
#  It creates one service template and five timers, all in America/New_York time,
#  Monday-Friday. The code itself also skips market holidays.
# =============================================================================
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "Please run with sudo:  sudo deploy/install_systemd.sh"
    exit 1
fi

PROJECT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
USER_NAME="${SUDO_USER:-trader}"
SD=/etc/systemd/system

echo "Installing systemd units for project: $PROJECT (user: $USER_NAME)"

# --- dispatcher: maps a keyword to the real command ---
cat > "$PROJECT/deploy/task.sh" <<EOF
#!/usr/bin/env bash
set -euo pipefail
cd "$PROJECT"
case "\$1" in
  scan)      exec "$PROJECT/run.sh" scanner.py ;;
  morning)   exec "$PROJECT/run.sh" executor.py morning ;;
  reconcile) exec "$PROJECT/run.sh" executor.py reconcile ;;
  cancel)    exec "$PROJECT/run.sh" executor.py cancel ;;
  weekly)    exec "$PROJECT/run.sh" notifier.py weekly ;;
  *) echo "unknown task: \$1"; exit 1 ;;
esac
EOF
chmod +x "$PROJECT/deploy/task.sh"

# --- service template ---
cat > "$SD/swing@.service" <<EOF
[Unit]
Description=Swing trading task: %i

[Service]
Type=oneshot
User=$USER_NAME
ExecStart=$PROJECT/deploy/task.sh %i
EOF

# --- helper to write a timer ---
make_timer() {
    local name="$1" cal="$2"
    cat > "$SD/swing-$name.timer" <<EOF
[Unit]
Description=Swing trading timer: $name

[Timer]
OnCalendar=$cal
Persistent=false
Unit=swing@$name.service

[Install]
WantedBy=timers.target
EOF
}

make_timer morning   "Mon-Fri 09:28 America/New_York"
make_timer reconcile "Mon-Fri 09:45 America/New_York"
make_timer cancel    "Mon-Fri 15:45 America/New_York"
make_timer eod       "Mon-Fri 16:10 America/New_York"   # (uses reconcile)
make_timer scan      "Mon-Fri 17:00 America/New_York"
make_timer weekly    "Fri 17:05 America/New_York"

# The 'eod' timer should run reconcile too:
sed -i 's/Unit=swing@eod.service/Unit=swing@reconcile.service/' "$SD/swing-eod.timer"

systemctl daemon-reload
for t in morning reconcile cancel eod scan weekly; do
    systemctl enable --now "swing-$t.timer"
done

echo
echo "Done. Check the schedule with:  systemctl list-timers 'swing-*'"
echo "Turn it OFF with:  sudo systemctl disable --now 'swing-*.timer'"
