"""
state.py  -  Remembers things between runs by saving them to state.json.

Right now it stores the account "high-water mark" (the highest equity your
account has ever reached). The drawdown-halt rule uses it: if equity falls
more than the configured % below this high, the system stops opening new trades.
"""
import json
import logging

import config

log = logging.getLogger("state")


def _load():
    try:
        if config.STATE_FILE.exists():
            with open(config.STATE_FILE, "r") as f:
                return json.load(f)
    except Exception as e:  # noqa: BLE001
        log.warning("Could not read state.json (%s); starting fresh.", e)
    return {}


def _save(data):
    try:
        with open(config.STATE_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:  # noqa: BLE001
        log.error("Could not write state.json: %s", e)


def get_high_water_mark(default=0.0):
    return float(_load().get("high_water_mark", default))


def update_high_water_mark(current_equity):
    """Raise the high-water mark if current equity is a new record.
    Returns the (possibly updated) high-water mark."""
    data = _load()
    hwm = float(data.get("high_water_mark", 0.0))
    if current_equity > hwm:
        hwm = float(current_equity)
        data["high_water_mark"] = hwm
        _save(data)
        log.info("New high-water mark: $%.2f", hwm)
    return hwm


def drawdown_pct(current_equity):
    """How far (%) below the high-water mark we currently are. 0 if at/above."""
    hwm = get_high_water_mark(default=current_equity)
    if hwm <= 0:
        return 0.0
    dd = (hwm - current_equity) / hwm * 100.0
    return max(0.0, dd)
