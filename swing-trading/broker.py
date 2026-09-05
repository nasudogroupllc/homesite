"""
broker.py  -  Everything that talks to Alpaca (your broker).

By default it points at Alpaca's PAPER (fake-money) endpoint, taken from
ALPACA_BASE_URL in your .env. It will refuse to do anything unusual with a
live account unless the base URL is deliberately the live one.

This module places bracket orders, reads positions/orders, replaces bracket
legs, and closes positions. It imports the Alpaca library lazily so the rest
of the system (scanner, backtest) can run even if alpaca-py is not installed.
"""
import logging

import config

log = logging.getLogger("broker")


class Broker:
    def __init__(self):
        from alpaca.trading.client import TradingClient

        key, secret, base = config.alpaca_keys()
        self.paper = "paper" in base.lower()
        # TradingClient(paper=True) uses the paper endpoint automatically.
        self.client = TradingClient(key, secret, paper=self.paper)
        log.info("Broker connected (%s trading).", "PAPER" if self.paper else "LIVE")

    # ---------------------------------------------------------------- account
    def account(self):
        return self.client.get_account()

    def equity(self):
        return float(self.account().equity)

    def buying_power(self):
        return float(self.account().buying_power)

    # -------------------------------------------------------------- positions
    def positions(self):
        """Return list of dicts: symbol, qty, avg_entry_price, market_value, unrealized."""
        out = []
        for p in self.client.get_all_positions():
            out.append({
                "symbol": p.symbol,
                "qty": int(float(p.qty)),
                "avg_entry_price": float(p.avg_entry_price),
                "market_value": float(p.market_value),
                "current_price": float(p.current_price) if p.current_price else None,
            })
        return out

    def position_symbols(self):
        return {p["symbol"] for p in self.positions()}

    # ----------------------------------------------------------------- orders
    def open_orders(self):
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus, OrderSide  # noqa: F401

        req = GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=500, nested=True)
        return self.client.get_orders(req)

    def open_order_symbols(self):
        return {o.symbol for o in self.open_orders()}

    def has_exposure(self, symbol):
        """True if we already have a position OR an open order for this symbol."""
        return symbol in self.position_symbols() or symbol in self.open_order_symbols()

    # ----------------------------------------------------------- placing orders
    def submit_bracket(self, symbol, qty, entry_stop, entry_limit,
                       take_profit, stop_loss):
        """
        Place a BUY stop-limit ENTRY with an attached bracket:
          - entry:       stop-limit, time_in_force = DAY
          - take_profit: limit sell (GTC, auto-managed by Alpaca)
          - stop_loss:   stop sell  (GTC, auto-managed by Alpaca)
        Returns the submitted order object.
        """
        from alpaca.trading.requests import (
            StopLimitOrderRequest, TakeProfitRequest, StopLossRequest,
        )
        from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass

        req = StopLimitOrderRequest(
            symbol=symbol,
            qty=int(qty),
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
            stop_price=_round2(entry_stop),
            limit_price=_round2(entry_limit),
            order_class=OrderClass.BRACKET,
            take_profit=TakeProfitRequest(limit_price=_round2(take_profit)),
            stop_loss=StopLossRequest(stop_price=_round2(stop_loss)),
        )
        order = self.client.submit_order(req)
        log.info("Submitted bracket %s x%s entry_stop=%.2f tp=%.2f sl=%.2f",
                 symbol, qty, entry_stop, take_profit, stop_loss)
        return order

    def get_order(self, order_id):
        return self.client.get_order_by_id(order_id)

    def child_sell_legs(self, symbol):
        """After a bracket entry fills, its two exit legs become open SELL
        orders. Return them as a list (take-profit limit + stop-loss stop)."""
        legs = []
        for o in self.open_orders():
            if o.symbol == symbol and str(o.side).lower().endswith("sell"):
                legs.append(o)
        return legs

    def replace_order_price(self, order_id, limit_price=None, stop_price=None):
        from alpaca.trading.requests import ReplaceOrderRequest
        req = ReplaceOrderRequest(
            limit_price=_round2(limit_price) if limit_price is not None else None,
            stop_price=_round2(stop_price) if stop_price is not None else None,
        )
        return self.client.replace_order_by_id(order_id, req)

    # ---------------------------------------------------------------- closing
    def cancel_symbol_orders(self, symbol):
        for o in self.open_orders():
            if o.symbol == symbol:
                try:
                    self.client.cancel_order_by_id(o.id)
                except Exception as e:  # noqa: BLE001
                    log.warning("Could not cancel order %s: %s", o.id, e)

    def cancel_unfilled_entries(self):
        """Cancel every open BUY (entry) order that has not filled.
        Used by the 3:45 PM sweep. Returns the list of symbols canceled."""
        canceled = []
        for o in self.open_orders():
            if str(o.side).lower().endswith("buy"):
                try:
                    self.client.cancel_order_by_id(o.id)
                    canceled.append(o.symbol)
                except Exception as e:  # noqa: BLE001
                    log.warning("Could not cancel entry %s: %s", o.symbol, e)
        return canceled

    def recent_closed_sell_price(self, symbol):
        """Find the fill price of the most recent CLOSED sell order for a
        symbol (used to record the exit price when a stop/target hits).
        Returns a float price or None."""
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus
        try:
            req = GetOrdersRequest(
                status=QueryOrderStatus.CLOSED, symbols=[symbol], limit=50, nested=False,
            )
            orders = self.client.get_orders(req)
        except Exception as e:  # noqa: BLE001
            log.warning("Could not read closed orders for %s: %s", symbol, e)
            return None
        for o in orders:
            if str(o.side).lower().endswith("sell") and o.filled_avg_price:
                return float(o.filled_avg_price)
        return None

    def close_position(self, symbol):
        """Cancel any orders for the symbol, then market-close the position."""
        self.cancel_symbol_orders(symbol)
        try:
            self.client.close_position(symbol)
            log.info("Closed position %s", symbol)
            return True
        except Exception as e:  # noqa: BLE001
            log.error("Failed to close position %s: %s", symbol, e)
            return False


def _round2(x):
    """Round a price to 2 decimals (whole cents), which Alpaca requires."""
    return round(float(x) + 1e-9, 2)
