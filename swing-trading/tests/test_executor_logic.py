"""Offline tests for executor logic using a fake broker (no Alpaca needed)."""
import os, sys, json
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["SWING_ENV_FILE"] = "/nonexistent"

import config, journal, state, executor, notifier

# Silence Telegram
notifier.send = lambda *a, **k: True

# Use temp data files so we don't clobber real ones
import pathlib, tempfile
tmp = pathlib.Path(tempfile.mkdtemp())
config.JOURNAL_FILE = tmp / "journal.csv"
config.STATE_FILE = tmp / "state.json"
config.CANDIDATES_FILE = tmp / "candidates.json"
journal.config.JOURNAL_FILE = config.JOURNAL_FILE
state.config.STATE_FILE = config.STATE_FILE
executor.config.CANDIDATES_FILE = config.CANDIDATES_FILE

cfg = config.CONFIG


class FakeLeg:
    def __init__(self, id, stop_price=None, limit_price=None, side="sell"):
        self.id, self.stop_price, self.limit_price, self.side = id, stop_price, limit_price, side


class FakeBroker:
    def __init__(self):
        self.paper = True
        self._positions = []
        self._orders = []       # open orders (symbols)
        self.submitted = []
        self.closed = []
        self.replaced = []
        self.legs = {}
    def equity(self): return 100000.0
    def positions(self): return list(self._positions)
    def position_symbols(self): return {p["symbol"] for p in self._positions}
    def open_order_symbols(self): return set(self._orders)
    def submit_bracket(self, sym, qty, es, el, tp, sl):
        self.submitted.append(dict(symbol=sym, qty=qty, entry=es, tp=tp, sl=sl)); return object()
    def child_sell_legs(self, sym): return self.legs.get(sym, [])
    def replace_order_price(self, oid, limit_price=None, stop_price=None):
        self.replaced.append((oid, limit_price, stop_price))
    def close_position(self, sym): self.closed.append(sym); return True
    def recent_closed_sell_price(self, sym): return None
    def cancel_unfilled_entries(self): return []


def test_position_sizing_and_limits():
    # Two candidates, same sector -> only one should be placed (sector cap = 1)
    # ATR chosen so position value stays under the 30% cap:
    # risk $1000 / (1.5*3.0=4.5) = 222 shares; 222*100.05 = $22,211 < $30,000.
    cand = {"candidates": [
        {"symbol": "AAA", "sector": "Tech", "price": 100, "prior_high": 100, "atr14": 3.0, "rs_score": 5, "rank": 1},
        {"symbol": "BBB", "sector": "Tech", "price": 100, "prior_high": 100, "atr14": 3.0, "rs_score": 4, "rank": 2},
        {"symbol": "CCC", "sector": "Energy", "price": 100, "prior_high": 100, "atr14": 3.0, "rs_score": 3, "rank": 3},
    ]}
    # sectors also come from universe.csv; override sector_map to our test sectors
    executor.sector_map = lambda: {"AAA": "Tech", "BBB": "Tech", "CCC": "Energy"}
    json.dump(cand, open(config.CANDIDATES_FILE, "w"))

    b = FakeBroker()
    executor.place_entries(b, cfg, equity=100000.0)
    placed = [s["symbol"] for s in b.submitted]
    assert "AAA" in placed, placed
    assert "BBB" not in placed, "sector cap failed"   # same sector as AAA
    assert "CCC" in placed, placed
    # Sizing: risk = 1% of 100k = $1000; stop dist = 1.5*3 = 4.5 -> shares = 222
    aaa = next(s for s in b.submitted if s["symbol"] == "AAA")
    assert aaa["qty"] == 222, aaa["qty"]
    # stop = entry - 4.5, target = entry + 9  (entry = 100 + 0.05 buffer = 100.05)
    assert abs(aaa["sl"] - (100.05 - 4.5)) < 0.001
    assert abs(aaa["tp"] - (100.05 + 9)) < 0.001
    print("PASS: sizing, sector cap, bracket levels")


def test_position_size_cap():
    # A huge ATR making shares small is fine; test the 30% cap rejects oversize.
    # entry 100, atr tiny -> shares huge -> value > 30% -> skip
    cand = {"candidates": [
        {"symbol": "ZZZ", "sector": "Tech", "price": 100, "prior_high": 100, "atr14": 0.05, "rs_score": 5, "rank": 1},
    ]}
    executor.sector_map = lambda: {"ZZZ": "Tech"}
    json.dump(cand, open(config.CANDIDATES_FILE, "w"))
    b = FakeBroker()
    executor.place_entries(b, cfg, equity=100000.0)
    assert not b.submitted, "30% position-size cap failed to reject"
    print("PASS: 30% position-size cap")


def test_time_stop():
    journal.record_entry("OLD", 100, 97, 106, 10, 30.0)
    # Backdate the entry to 20 days ago
    rows = list(open(config.JOURNAL_FILE))
    rows[1] = rows[1].replace(date.today().isoformat(),
                              (date.today() - timedelta(days=20)).isoformat())
    open(config.JOURNAL_FILE, "w").writelines(rows)
    b = FakeBroker()
    b._positions = [{"symbol": "OLD", "qty": 10, "avg_entry_price": 100, "current_price": 104, "market_value": 1040}]
    executor.enforce_time_stop(b, cfg)
    assert "OLD" in b.closed, "time stop did not close old position"
    assert "OLD" not in journal.open_symbols(), "journal not updated after time stop"
    print("PASS: 14-day time stop")


def test_safety_close_no_legs():
    # A filled position with NO protective legs must be closed immediately.
    json.dump({"candidates": [{"symbol": "NAK", "atr14": 2.0}]}, open(config.CANDIDATES_FILE, "w"))
    b = FakeBroker()
    b._positions = [{"symbol": "NAK", "qty": 5, "avg_entry_price": 50, "current_price": 50, "market_value": 250}]
    b.legs = {"NAK": []}   # no legs!
    executor.reconcile(b, cfg)
    assert "NAK" in b.closed, "unprotected position not safety-closed"
    print("PASS: safety close when bracket legs missing")


def test_state_hwm_drawdown():
    state.update_high_water_mark(100000)
    state.update_high_water_mark(120000)
    assert state.get_high_water_mark() == 120000
    dd = state.drawdown_pct(110400)   # 8% below 120k
    assert abs(dd - 8.0) < 0.01, dd
    print("PASS: high-water mark + drawdown %")


if __name__ == "__main__":
    test_position_sizing_and_limits()
    test_position_size_cap()
    test_time_stop()
    test_safety_close_no_legs()
    test_state_hwm_drawdown()
    print("\nALL EXECUTOR LOGIC TESTS PASSED")
