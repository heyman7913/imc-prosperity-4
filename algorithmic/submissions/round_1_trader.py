# ASH_COATED_OSMIUM: 3-layer mean-reversion MM around hard fair value 10000
# INTARIAN_PEPPER_ROOT: pure long accumulator on confirmed linear price drift
# Position limit: 80 per product

import json
import math
from datamodel import Order, TradingState, OrderDepth
from typing import Dict, List, Optional, Tuple

LIMIT = 80

ASH = "ASH_COATED_OSMIUM"
ASH_FAIR = 10_000        # anchor is rock-solid; ADF p<0.001, half-life ~15 ticks
ASH_DEV_THRESH = 6       # below this it's just spread noise, not a real dislocation
ASH_MAX_DEV_POS = 25     # reserve the remaining 55 units of capacity for passive MM

PEPPER = "INTARIAN_PEPPER_ROOT"
DRIFT = 0.001            # confirmed via OLS: R²>0.99, exactly 0.001/tick across all days


class Trader:

    def _load(self, raw: str) -> dict:
        defaults = {"pb": None, "wm": float(ASH_FAIR)}
        try:
            d = json.loads(raw) if raw else {}
            for k, v in defaults.items():
                d.setdefault(k, v)
            return d
        except Exception:
            return dict(defaults)

    def run(self, state: TradingState) -> tuple:
        result: Dict[str, List[Order]] = {}
        td = self._load(state.traderData)

        for product in state.order_depths:
            od = state.order_depths[product]
            pos = state.position.get(product, 0)

            if product == ASH:
                orders = self.trade_ash(od, pos, td)
            elif product == PEPPER:
                orders = self.trade_pepper(od, pos, state.timestamp, td)
            else:
                orders = []

            result[product] = self._clamp(orders, pos)

        return result, 0, json.dumps(td)

    def trade_ash(self, od: OrderDepth, pos: int, td: dict) -> List[Order]:
        orders: List[Order] = []
        if not od.buy_orders or not od.sell_orders:
            return orders

        best_bid = max(od.buy_orders.keys())
        best_ask = min(od.sell_orders.keys())
        simple_mid = (best_bid + best_ask) / 2.0
        deviation = simple_mid - ASH_FAIR

        # wall_mid uses the outermost quotes rather than best bid/ask.
        # It's a more stable reference because the outermost levels move less tick-to-tick.
        bid_wall = min(od.buy_orders.keys())
        ask_wall = max(od.sell_orders.keys())
        wall_mid = (bid_wall + ask_wall) / 2.0
        td["wm"] = wall_mid

        # pos_adj tracks what our position would be after all orders in this tick fill.
        # We check it before each new order to avoid blowing past limits mid-loop.
        pos_adj = pos

        abs_dev = abs(deviation)
        if abs_dev >= ASH_DEV_THRESH:
            if abs_dev >= 12:
                edge_limit = 5
            elif abs_dev >= 9:
                edge_limit = 3
            else:
                edge_limit = 1

            if deviation < 0 and pos_adj < ASH_MAX_DEV_POS:
                max_price = ASH_FAIR + edge_limit
                for ask_p in sorted(od.sell_orders.keys()):
                    if ask_p > max_price or pos_adj >= ASH_MAX_DEV_POS:
                        break
                    cap = min(ASH_MAX_DEV_POS - pos_adj, LIMIT - pos_adj)
                    if cap <= 0:
                        break
                    vol = min(abs(od.sell_orders[ask_p]), cap)
                    if vol > 0:
                        orders.append(Order(ASH, ask_p, vol))
                        pos_adj += vol

            elif deviation > 0 and pos_adj > -ASH_MAX_DEV_POS:
                min_price = ASH_FAIR - edge_limit
                for bid_p in sorted(od.buy_orders.keys(), reverse=True):
                    if bid_p < min_price or pos_adj <= -ASH_MAX_DEV_POS:
                        break
                    cap = min(ASH_MAX_DEV_POS + pos_adj, LIMIT + pos_adj)
                    if cap <= 0:
                        break
                    vol = min(od.buy_orders[bid_p], cap)
                    if vol > 0:
                        orders.append(Order(ASH, bid_p, -vol))
                        pos_adj -= vol

        for ask_p in sorted(od.sell_orders.keys()):
            if ask_p > wall_mid or pos_adj >= LIMIT:
                break
            avail = abs(od.sell_orders[ask_p])
            cap = LIMIT - pos_adj
            if ask_p <= wall_mid - 1:
                vol = min(avail, cap)
                if vol > 0:
                    orders.append(Order(ASH, ask_p, vol))
                    pos_adj += vol
            elif pos_adj < 0:
                vol = min(avail, -pos_adj, cap)
                if vol > 0:
                    orders.append(Order(ASH, ask_p, vol))
                    pos_adj += vol

        for bid_p in sorted(od.buy_orders.keys(), reverse=True):
            if bid_p < wall_mid or pos_adj <= -LIMIT:
                break
            avail = od.buy_orders[bid_p]
            cap = pos_adj + LIMIT
            if bid_p >= wall_mid + 1:
                vol = min(avail, cap)
                if vol > 0:
                    orders.append(Order(ASH, bid_p, -vol))
                    pos_adj -= vol
            elif pos_adj > 0:
                vol = min(avail, pos_adj, cap)
                if vol > 0:
                    orders.append(Order(ASH, bid_p, -vol))
                    pos_adj -= vol

        buy_cap = LIMIT - pos_adj
        sell_cap = LIMIT + pos_adj

        max_bid = math.ceil(wall_mid) - 1
        min_ask = math.floor(wall_mid) + 1

        # Penny-improve: find the best resting quote inside the wall and go one tick ahead.
        # If that quote only has 1 unit, match its price rather than improving (probably a stub quote).
        bid_price = bid_wall + 1
        for p in sorted(od.buy_orders.keys(), reverse=True):
            if p < wall_mid:
                bid_price = (p + 1) if od.buy_orders[p] > 1 else p
                break
        bid_price = min(bid_price, max_bid)

        ask_price = ask_wall - 1
        for p in sorted(od.sell_orders.keys()):
            if p > wall_mid:
                ask_price = (p - 1) if od.sell_orders[p] < -1 else p
                break
        ask_price = max(ask_price, min_ask)

        skew = 0
        if abs(pos_adj) > 20:
            skew = int(abs(pos_adj) / 20)
        if pos_adj > 20:
            bid_price -= skew
            ask_price -= skew
        elif pos_adj < -20:
            bid_price += skew
            ask_price += skew

        if bid_price >= ask_price:
            bid_price = ask_price - 1

        INNER_SIZE = 15
        MID_SIZE = 20
        MID_EDGE = 7
        OUTER_EDGE = 10
        mid_bid = ASH_FAIR - MID_EDGE
        mid_ask = ASH_FAIR + MID_EDGE
        outer_bid = ASH_FAIR - OUTER_EDGE
        outer_ask = ASH_FAIR + OUTER_EDGE

        if buy_cap > 0 and bid_price > 0:
            rem = buy_cap
            inner = min(INNER_SIZE, rem); rem -= inner
            orders.append(Order(ASH, bid_price, inner))
            if rem > 0 and mid_bid < bid_price:
                m = min(MID_SIZE, rem); rem -= m
                orders.append(Order(ASH, mid_bid, m))
            if rem > 0 and outer_bid < bid_price and outer_bid < mid_bid:
                orders.append(Order(ASH, outer_bid, rem))
            elif rem > 0:
                orders[-1] = Order(ASH, orders[-1].price, orders[-1].quantity + rem)

        if sell_cap > 0 and ask_price > 0:
            rem = sell_cap
            inner = min(INNER_SIZE, rem); rem -= inner
            orders.append(Order(ASH, ask_price, -inner))
            if rem > 0 and mid_ask > ask_price:
                m = min(MID_SIZE, rem); rem -= m
                orders.append(Order(ASH, mid_ask, -m))
            if rem > 0 and outer_ask > ask_price and outer_ask > mid_ask:
                orders.append(Order(ASH, outer_ask, -rem))
            elif rem > 0:
                orders[-1] = Order(ASH, orders[-1].price, orders[-1].quantity - rem)

        return orders

    def trade_pepper(self, od: OrderDepth, pos: int, ts: int, td: dict) -> List[Order]:
        orders: List[Order] = []

        base = td.get("pb")
        if base is None:
            if od.buy_orders and od.sell_orders:
                mid = (max(od.buy_orders.keys()) + min(od.sell_orders.keys())) / 2.0
                base = mid - ts * DRIFT
                td["pb"] = base
            else:
                return orders

        fv = base + ts * DRIFT
        buy_cap = LIMIT - pos

        if buy_cap <= 0:
            return orders

        if od.sell_orders:
            for ask_p in sorted(od.sell_orders.keys()):
                if buy_cap <= 0:
                    break
                vol = min(abs(od.sell_orders[ask_p]), buy_cap)
                orders.append(Order(PEPPER, ask_p, vol))
                buy_cap -= vol

        if buy_cap > 0 and od.buy_orders:
            best_bid = max(od.buy_orders.keys())
            bid_p = best_bid + 1
            bid_p = min(bid_p, int(fv) + 7)
            if od.sell_orders:
                best_ask = min(od.sell_orders.keys())
                bid_p = min(bid_p, best_ask - 1)
            if bid_p > 0:
                orders.append(Order(PEPPER, bid_p, buy_cap))

        return orders

    def _clamp(self, orders: List[Order], pos: int) -> List[Order]:
        max_buy = LIMIT - pos
        max_sell = LIMIT + pos
        total_buy = total_sell = 0
        valid = []
        for o in orders:
            if o.quantity > 0:
                can = min(o.quantity, max_buy - total_buy)
                if can > 0:
                    valid.append(Order(o.symbol, o.price, can))
                    total_buy += can
            elif o.quantity < 0:
                can = min(abs(o.quantity), max_sell - total_sell)
                if can > 0:
                    valid.append(Order(o.symbol, o.price, -can))
                    total_sell += can
        return valid
