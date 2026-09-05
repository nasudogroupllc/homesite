#!/usr/bin/env python3
"""
executor.py  -  Runs at 9:28 AM ET (and a couple of other times) each trading day.

It has three "modes". Cron/systemd runs them at different times:

    python executor.py            # 9:28 AM - the main morning routine
    python executor.py reconcile  # 9:45 AM - fix bracket legs after fills, log exits
    python executor.py cancel     # 3:45 PM - cancel entry orders that never filled

MORNING ROUTINE (default) does, in order:
    1. Update the account high-water mark on disk.
    2. Reconcile yesterday: log any positions that hit their stop/target.
    3. Time-stop: market-close any position >= 14 calendar days old.
    4. Drawdown halt: if equity is >8% below its high, place NO new trades.
    5. Otherwise place bracket orders for the top candidates, respecting all
       the risk limits (max positions, one-per-sector, position-size cap, etc.).

Everything is wrapped in try/except so an error alerts you on Telegram and,
critically, if a position ever ends up without protective bracket legs it is
closed immediately.
"""
import sys
import csv
import json
import math
import logging
import traceback
from datetime import datetime, date

import config
import journal
import notifier
import state

log = logging.getLogger("executor")


# ---------------------------------------------------------------- data helpers
def load_candidates():
    if not config.CANDIDATES_FILE.exists():
        return {"candidates": [], "flagged_earnings_unknown": []}
    with open(config.CANDIDATES_FILE, "r") as f:
        return json.load(f)


def sector_map():
    """symbol -> sector, from universe.csv."""
    m = {}
    with open(config.UNIVERSE_FILE, "r", newline="") as f:
        for r in csv.DictReader(f):
            sym = (r.get("symbol") or "").strip().upper()
            if sym:
                m[sym] = (r.get("sector") or "Unknown").strip()
    return m


def candidate_map():
    """symbol -> candidate dict (for atr14, prior_high, etc.)."""
    data = load_candidates()
    return {c["symbol"]: c for c in data.get("candidates", [])}


def journal_entry_date(symbol):
    """The entry date recorded in journal.csv for an OPEN position, or None."""
    import csv as _csv
    if not config.JOURNAL_FILE.exists():
        return None
    with open(config.JOURNAL_FILE, "r", newline="") as f:
        for r in reversed(list(_csv.DictReader(f))):
            if r["symbol"] == symbol and r["status"] == "OPEN":
                try:
                    return datetime.fromisoformat(r["date"]).date()
                except Exception:  # noqa: BLE001
                    return None
    return None


# ----------------------------------------------------------------- core steps
def enforce_time_stop(broker, cfg):
    """Market-close any position that is >= time_stop_days calendar days old."""
    limit = int(cfg["time_stop_days"])
    for p in broker.positions():
        sym = p["symbol"]
        entry_date = journal_entry_date(sym)
        if entry_date is None:
            continue  # no recorded entry date; leave it alone
        age = (date.today() - entry_date).days
        if age >= limit:
            price = p["current_price"] or p["avg_entry_price"]
            log.info("Time stop: %s is %d days old; closing.", sym, age)
            if broker.close_position(sym):
                journal.record_exit(sym, price, reason="TIME_STOP")
                notifier.send(
                    f"Time stop hit: closed {sym} after {age} days at ~${price:.2f}",
                    kind="timestop",
                )


def reconcile(broker, cfg):
    """
    Two jobs:
      A) New fills: record them, fix bracket legs to the REAL fill price, and
         if a filled position has NO protective legs, close it immediately.
      B) Exits: any previously-open journal trade that is no longer a position
         hit its stop or target - record the exit and notify.
    Safe to run multiple times a day.
    """
    positions = {p["symbol"]: p for p in broker.positions()}
    open_journal = journal.open_symbols()
    cmap = candidate_map()

    # ---- A) newly filled positions ----
    for sym, p in positions.items():
        if sym in open_journal:
            # already recorded; still make sure legs are correct
            _fix_legs(broker, cfg, sym, p, cmap, record=False)
            continue
        # A position we haven't journaled yet => it just filled.
        _fix_legs(broker, cfg, sym, p, cmap, record=True)

    # ---- B) exits (was open, now gone) ----
    for sym in open_journal:
        if sym in positions:
            continue
        exit_price = broker.recent_closed_sell_price(sym)
        reason = "EXIT"
        if exit_price is not None:
            # Decide target vs stop by comparing to the journaled levels.
            reason = _classify_exit(sym, exit_price)
        else:
            exit_price = 0.0
        journal.record_exit(sym, exit_price, reason=reason)
        icon = "target" if reason == "TARGET" else ("stop" if reason == "STOP" else "info")
        notifier.send(
            f"{sym} exited ({reason}) at ~${exit_price:.2f}", kind=icon
        )


def _classify_exit(symbol, exit_price):
    import csv as _csv
    if not config.JOURNAL_FILE.exists():
        return "EXIT"
    with open(config.JOURNAL_FILE, "r", newline="") as f:
        for r in reversed(list(_csv.DictReader(f))):
            if r["symbol"] == symbol and r["status"] == "OPEN":
                try:
                    target = float(r["target"])
                    stop = float(r["stop"])
                except (KeyError, ValueError):
                    return "EXIT"
                # Closer to target => TARGET, closer to stop => STOP.
                return "TARGET" if abs(exit_price - target) <= abs(exit_price - stop) else "STOP"
    return "EXIT"


def _fix_legs(broker, cfg, sym, position, cmap, record):
    """Recompute stop/target from the actual fill price and replace the bracket
    legs if they differ by more than the threshold. Close position if no legs."""
    entry_fill = position["avg_entry_price"]
    shares = position["qty"]

    cand = cmap.get(sym)
    if cand and cand.get("atr14"):
        stop_distance = float(cfg["atr_stop_multiple"]) * float(cand["atr14"])
    else:
        # No ATR on record (e.g. manual position): fall back to legs as-is.
        stop_distance = None

    legs = broker.child_sell_legs(sym)

    # SAFETY: a filled long position with no protective sell legs is dangerous.
    if not legs:
        log.error("SAFETY: %s has no protective legs! Closing immediately.", sym)
        broker.close_position(sym)
        notifier.send(
            f"SAFETY CLOSE: {sym} had no protective stop/target legs after fill. "
            f"Position closed to avoid unprotected risk.",
            kind="error",
        )
        return

    new_stop = new_target = None
    if stop_distance is not None:
        new_stop = entry_fill - stop_distance
        new_target = entry_fill + float(cfg["reward_multiple"]) * stop_distance
        threshold = float(cfg["leg_replace_threshold"])
        for leg in legs:
            try:
                if getattr(leg, "stop_price", None):  # stop-loss leg
                    cur = float(leg.stop_price)
                    if abs(cur - new_stop) > threshold:
                        broker.replace_order_price(leg.id, stop_price=new_stop)
                        log.info("Adjusted %s stop leg %.2f -> %.2f", sym, cur, new_stop)
                elif getattr(leg, "limit_price", None):  # take-profit leg
                    cur = float(leg.limit_price)
                    if abs(cur - new_target) > threshold:
                        broker.replace_order_price(leg.id, limit_price=new_target)
                        log.info("Adjusted %s target leg %.2f -> %.2f", sym, cur, new_target)
            except Exception as e:  # noqa: BLE001
                log.error("Could not adjust %s leg: %s", sym, e)

    if record:
        stop_for_journal = new_stop if new_stop is not None else entry_fill
        target_for_journal = new_target if new_target is not None else entry_fill
        r_risked = shares * (stop_distance if stop_distance else 0.0)
        journal.record_entry(sym, entry_fill, stop_for_journal,
                             target_for_journal, shares, r_risked)
        notifier.send(
            f"Order filled: bought {shares} {sym} @ ${entry_fill:.2f}\n"
            f"  stop ${stop_for_journal:.2f}  target ${target_for_journal:.2f}",
            kind="fill",
        )


def place_entries(broker, cfg, equity):
    """Place bracket orders for the top candidates, respecting all risk limits."""
    data = load_candidates()
    candidates = data.get("candidates", [])
    if not candidates:
        log.info("No candidates to trade today.")
        return

    smap = sector_map()
    positions = broker.positions()
    open_syms = broker.position_symbols() | broker.open_order_symbols()
    open_sectors = {smap.get(p["symbol"], "Unknown") for p in positions}
    slots_used = len(positions) + len(broker.open_order_symbols())
    max_pos = int(cfg["max_positions"])

    placed = []
    for c in sorted(candidates, key=lambda x: x.get("rank", 999)):
        if slots_used >= max_pos:
            log.info("Reached max positions (%d); stopping.", max_pos)
            break

        sym = c["symbol"]
        sector = c.get("sector") or smap.get(sym, "Unknown")

        if sym in open_syms:
            log.info("%s: already have a position/order; skipping.", sym)
            continue
        if int(cfg["max_sector_positions"]) <= _sector_count(open_sectors, sector):
            log.info("%s: sector '%s' already has a position; skipping.", sym, sector)
            continue

        entry_stop = float(c["prior_high"]) + float(cfg["entry_stop_buffer"])
        entry_limit = entry_stop * float(cfg["entry_limit_multiple"])
        stop_distance = float(cfg["atr_stop_multiple"]) * float(c["atr14"])
        if stop_distance <= 0:
            continue

        risk_dollars = equity * float(cfg["account_risk_pct"]) / 100.0
        shares = math.floor(risk_dollars / stop_distance)

        if shares < 1:
            log.info("%s: computed 0 shares; skipping.", sym)
            continue

        position_value = shares * entry_stop
        if position_value > float(cfg["max_position_size_pct"]) / 100.0 * equity:
            log.info("%s: position too large (%.0f > %.0f%% of equity); skipping.",
                     sym, position_value, cfg["max_position_size_pct"])
            continue

        take_profit = entry_stop + float(cfg["reward_multiple"]) * stop_distance
        stop_loss = entry_stop - stop_distance

        try:
            broker.submit_bracket(
                sym, shares, entry_stop, entry_limit, take_profit, stop_loss
            )
            placed.append(sym)
            slots_used += 1
            open_sectors.add(sector)
            open_syms.add(sym)
            log.info("Placed %s: %d sh, entry~%.2f, stop %.2f, target %.2f",
                     sym, shares, entry_stop, stop_loss, take_profit)
        except Exception as e:  # noqa: BLE001
            log.error("Failed to place order for %s: %s", sym, e)
            notifier.notify_error(f"placing order for {sym}", e)

    if placed:
        notifier.send(
            "Placed entry orders for: " + ", ".join(placed) +
            "\n(These are stop-limit entries; you'll get a message if/when they fill.)",
            kind="info",
        )
    else:
        log.info("No new entries placed today.")


def _sector_count(open_sectors, sector):
    # open_sectors is a set of sectors currently held; count is 1 if present.
    # (With max_sector_positions default 1 this is exactly the rule we want.)
    return 1 if sector in open_sectors else 0


# --------------------------------------------------------------------- modes
def morning_run():
    journal.setup_logging()
    import market
    if not market.is_trading_day():
        log.info("Not a trading day; skipping morning run.")
        return
    log.info("===== MORNING RUN %s =====", datetime.now().isoformat())
    import broker as broker_mod
    broker = broker_mod.Broker()
    cfg = config.CONFIG

    if not broker.paper:
        log.warning("NOTE: broker is pointed at a LIVE account.")

    equity = broker.equity()
    hwm = state.update_high_water_mark(equity)
    log.info("Equity: $%.2f  High-water mark: $%.2f", equity, hwm)

    # 1) Log any overnight exits from existing positions.
    reconcile(broker, cfg)

    # 2) Time-stop old positions.
    enforce_time_stop(broker, cfg)

    # 3) Drawdown halt.
    dd = state.drawdown_pct(equity)
    halt = float(cfg["drawdown_halt_pct"])
    if dd > halt:
        msg = (f"Drawdown halt: equity is {dd:.1f}% below the high-water mark "
               f"(limit {halt:.1f}%). NO new trades today.")
        log.warning(msg)
        notifier.send(msg, kind="warning")
        log.info("===== MORNING RUN END (halted) =====")
        return

    # 4) Market-regime filter: skip new entries if SPY is below its 200-day avg.
    if bool(cfg.get("market_regime_filter", False)) and not _market_is_healthy(cfg):
        msg = ("Market-regime filter: SPY is below its 200-day average "
               "(broad downtrend). No new trades today.")
        log.warning(msg)
        notifier.send(msg, kind="warning")
        log.info("===== MORNING RUN END (regime) =====")
        return

    # 5) Place new entries.
    place_entries(broker, cfg, equity)
    log.info("===== MORNING RUN END =====")


def _market_is_healthy(cfg):
    """True if SPY's latest close is above its 200-day simple moving average.
    On any data error we return True (fail open) so a data glitch doesn't
    silently stop all trading - the regime filter is a helper, not a gate."""
    try:
        import indicators
        from datafeed import DataFeed
        feed = DataFeed(notifier=notifier)
        df = feed.get_daily_bars(cfg["benchmark_symbol"])
        if df is None or len(df) < 205:
            return True
        sma200 = indicators.sma(df["close"], 200).iloc[-1]
        close = df["close"].iloc[-1]
        healthy = close > sma200
        log.info("Regime check: SPY close %.2f vs SMA200 %.2f -> %s",
                 close, sma200, "healthy" if healthy else "downtrend")
        return healthy
    except Exception as e:  # noqa: BLE001
        log.warning("Regime check failed (%s); allowing trades.", e)
        return True


def reconcile_run():
    journal.setup_logging()
    import market
    if not market.is_trading_day():
        log.info("Not a trading day; skipping reconcile.")
        return
    log.info("===== RECONCILE %s =====", datetime.now().isoformat())
    import broker as broker_mod
    broker = broker_mod.Broker()
    reconcile(broker, config.CONFIG)
    log.info("===== RECONCILE END =====")


def cancel_run():
    journal.setup_logging()
    import market
    if not market.is_trading_day():
        log.info("Not a trading day; skipping cancel sweep.")
        return
    log.info("===== CANCEL SWEEP %s =====", datetime.now().isoformat())
    import broker as broker_mod
    broker = broker_mod.Broker()
    # First catch any fills/exits, then cancel entries that never triggered.
    reconcile(broker, config.CONFIG)
    canceled = broker.cancel_unfilled_entries()
    if canceled:
        log.info("Canceled unfilled entries: %s", ", ".join(canceled))
        notifier.send("Canceled unfilled entry orders: " + ", ".join(canceled), kind="info")
    else:
        log.info("No unfilled entries to cancel.")
    log.info("===== CANCEL SWEEP END =====")


MODES = {"morning": morning_run, "reconcile": reconcile_run, "cancel": cancel_run}


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "morning"
    func = MODES.get(mode)
    if func is None:
        print("Usage: python executor.py [morning|reconcile|cancel]")
        sys.exit(1)
    try:
        func()
    except Exception as e:  # noqa: BLE001
        journal.setup_logging()
        log.error("EXECUTOR CRASHED (%s): %s\n%s", mode, e, traceback.format_exc())
        notifier.notify_error(f"executor ({mode})", e)
        raise
