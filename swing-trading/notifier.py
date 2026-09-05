"""
notifier.py  -  Sends you Telegram messages and writes the weekly summary.

Other files call send() / notify_error() to alert you. You can also run this
file directly to test it or to trigger the Friday summary:

    python notifier.py test        # sends a "hello" message so you know it works
    python notifier.py weekly      # sends the weekly performance summary

It reuses your existing Telegram bot (token + chat id from your .env file).
If Telegram is not configured, messages are printed to the log instead of
crashing the system - trading must never stop just because a message failed.
"""
import sys
import logging

import requests

import config
import journal

log = logging.getLogger("notifier")

# Simple emoji prefixes so messages are easy to scan on your phone.
ICONS = {
    "info": "ℹ️",       # information
    "candidates": "\U0001F50D",   # magnifying glass
    "fill": "✅",             # green check
    "target": "\U0001F3AF",       # target hit
    "stop": "\U0001F6D1",         # stop hit
    "timestop": "⏱️",   # clock (time stop)
    "warning": "⚠️",    # warning
    "error": "\U0001F6A8",        # red siren
    "summary": "\U0001F4CA",      # bar chart
}


def send(message, kind="info"):
    """Send a Telegram message. Never raises - returns True/False."""
    token, chat_id = config.telegram_creds()
    icon = ICONS.get(kind, "")
    text = f"{icon} {message}".strip()

    # Always record it in the log too.
    log.info("TELEGRAM[%s]: %s", kind, message)

    if not token or not chat_id:
        log.warning("Telegram not configured; message not sent: %s", message)
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        resp = requests.post(
            url,
            data={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
        if resp.status_code != 200:
            log.error("Telegram send failed (%s): %s", resp.status_code, resp.text)
            return False
        return True
    except Exception as e:  # noqa: BLE001 - never let a message break trading
        log.error("Telegram send crashed: %s", e)
        return False


def notify_error(context, exc):
    """Send a red-alert error message. Used by the try/except wrappers."""
    msg = f"ERROR in {context}: {exc}"
    return send(msg, kind="error")


def notify_candidates(candidates, flagged=None):
    """Message sent by the scanner when candidates are found."""
    if not candidates:
        send("Scan complete: no candidates passed the filters tonight.", kind="candidates")
        return
    lines = [f"Scan found {len(candidates)} candidate(s):"]
    for c in candidates[:15]:
        lines.append(
            f"  {c['rank']}. {c['symbol']}  "
            f"price ${c['price']:.2f}  RS {c['rs_score']:+.1f}%"
        )
    if flagged:
        lines.append("")
        lines.append("Flagged (earnings unknown - NOT traded):")
        lines.append("  " + ", ".join(flagged))
    send("\n".join(lines), kind="candidates")


def weekly_summary():
    """Build and send the Friday performance summary from journal.csv."""
    try:
        stats = journal.weekly_stats()
    except Exception as e:  # noqa: BLE001
        notify_error("weekly_summary", e)
        return

    msg = (
        f"<b>Weekly Summary</b>\n"
        f"Trades closed this week: {stats['trades']}\n"
        f"Win rate: {stats['win_rate']:.0f}%\n"
        f"Total R: {stats['total_r']:+.2f}\n"
        f"Account equity: ${stats['equity']:,.2f}"
    )
    send(msg, kind="summary")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    cmd = sys.argv[1] if len(sys.argv) > 1 else "test"
    if cmd == "test":
        ok = send("Swing trading system: Telegram test message. If you see this, it works!", kind="info")
        print("Sent OK" if ok else "Send FAILED - check TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID in your .env")
    elif cmd == "weekly":
        weekly_summary()
        print("Weekly summary sent (or logged if Telegram not configured).")
    else:
        print("Usage: python notifier.py [test|weekly]")
