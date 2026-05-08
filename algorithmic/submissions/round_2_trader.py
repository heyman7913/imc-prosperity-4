# ASH_COATED_OSMIUM: hybrid signal-take + passive MM (micro-price + EMA + imbalance)
# INTARIAN_PEPPER_ROOT: drift accumulator with stop-loss and re-anchoring
# Position limit: 80 per product

import json
import math
from typing import Dict, List, Tuple

try:
    from datamodel import Order, TradingState, OrderDepth
except ImportError:
    pass

ASH    = "ASH_COATED_OSMIUM"
PEPPER = "INTARIAN_PEPPER_ROOT"

class Trader:

    LIMIT = 80
    MAF_BID = 4000

    MICRO_ANCHOR   = 0.70    # 70% weight on slow EMA; prevents single-tick book noise from dominating
    IMB_COEF       = 2.0     # imbalance adds up to ±2 ticks of signal; 3 was too jumpy
    BASE_TAKE_EDGE = 0.7     # scales with realized vol, so naturally wider in fast markets
    SIGNAL_MAX_POS = 80
    POS_SHIFT_BASE = 0.03    # inventory penalty per unit; grows signal's effective edge when loaded
    VOL_NORM       = 5.0
    TAKE_EDGE_SCALE = True

    ASH_MAKE_EDGE_INNER = 5
    ASH_MAKE_EDGE_OUTER = 7
    ASH_WALL_FRAC = 0.15     # 15% of passive capacity goes to the wall level for deep fills
    ASH_WALL_MIN  = 5

    EMA_ALPHA_MEAN = 0.0001  # ~7000-tick half-life; slow enough to treat as the day's drift anchor
    EMA_ALPHA_VAR  = 0.005   # tracks realized vol on a ~200-tick window for dynamic edge sizing

    DRIFT      = 0.001
    PEP_OVER   = 8           # buy up to FV+8; covers typical ask spread without overpaying
    PEP_STOP   = 25          # never triggered in competition, but correct to have
    PEP_ANCHOR = 50          # re-anchor if model diverges >50 from mid (day boundary protection)

    def bid(self, *a, **kw) -> int:
        return self.MAF_BID

    def _load_state(self, raw: str) -> dict:
        defaults = {
            "ash_prev_micro": None,
            "ash_ema_mean": 10000.0,
            "ash_ema_var":  25.0,
            "pb": None,
            "pb_last_ts": -1,
            "pb_init": False,
        }
        try:
            d = json.loads(raw) if raw else {}
            for k, v in defaults.items():
                d.setdefault(k, v)
            return d
        except Exception:
            return dict(defaults)

    @staticmethod
    def _book_signal(od: 'OrderDepth') -> Tuple[float, float, float]:
        best_bid = max(od.buy_orders.keys())
        best_ask = min(od.sell_orders.keys())
        bv = od.buy_orders[best_bid]
        av = abs(od.sell_orders[best_ask])
        total = bv + av
        if total > 0:
            micro = (best_bid * av + best_ask * bv) / total
            imb   = (bv - av) / total
        else:
            micro = (best_bid + best_ask) / 2.0
            imb   = 0.0
        wall_mid = (min(od.buy_orders.keys()) + max(od.sell_orders.keys())) / 2.0
        return micro, imb, wall_mid

    def _update_ema(self, price: float, sd: dict) -> Tuple[float, float]:
        m = (1 - self.EMA_ALPHA_MEAN) * sd["ash_ema_mean"] + self.EMA_ALPHA_MEAN * price
        v = (1 - self.EMA_ALPHA_VAR)  * sd["ash_ema_var"]  + self.EMA_ALPHA_VAR  * (price - m) ** 2
        sd["ash_ema_mean"] = m
        sd["ash_ema_var"]  = v
        return m, math.sqrt(max(v, 0.25))

    def trade_ash(self, od: 'OrderDepth', pos: int, sd: dict) -> List['Order']:
        orders: List[Order] = []
        if not od.buy_orders or not od.sell_orders:
            return orders

        micro, imb, wall_mid = self._book_signal(od)

        # AR(1) smoothing reduces bid-ask bounce in the micro-price signal.
        prev = sd["ash_prev_micro"]
        if prev is None:
            prev = micro
        sd["ash_prev_micro"] = micro
        smoothed = 0.5 * (micro + prev)

        simple_mid = (max(od.buy_orders.keys()) + min(od.sell_orders.keys())) / 2.0
        ema_mean, ema_std = self._update_ema(simple_mid, sd)

        vol_ratio     = ema_std / self.VOL_NORM
        dynamic_shift = self.POS_SHIFT_BASE * vol_ratio
        dynamic_edge  = (self.BASE_TAKE_EDGE * vol_ratio
                         if self.TAKE_EDGE_SCALE else self.BASE_TAKE_EDGE)
        dynamic_edge  = max(0.5, min(2.5, dynamic_edge))

        # signal_fair = blend of smoothed micro-price (short-term) + EMA mean (long-term anchor)
        #               + order book pressure (imbalance) - inventory penalty
        signal_fair = ((1 - self.MICRO_ANCHOR) * smoothed
                       + self.MICRO_ANCHOR * ema_mean
                       + self.IMB_COEF * imb
                       - dynamic_shift * pos)

        pos_adj = pos

        if pos_adj < self.SIGNAL_MAX_POS:
            lim_buy = signal_fair - dynamic_edge
            for ap in sorted(od.sell_orders.keys()):
                if ap >= lim_buy or pos_adj >= self.SIGNAL_MAX_POS:
                    break
                cap = min(self.SIGNAL_MAX_POS - pos_adj, self.LIMIT - pos_adj)
                if cap <= 0:
                    break
                vol = min(abs(od.sell_orders[ap]), cap)
                if vol > 0:
                    orders.append(Order(ASH, ap, vol))
                    pos_adj += vol

        if pos_adj > -self.SIGNAL_MAX_POS:
            lim_sell = signal_fair + dynamic_edge
            for bp in sorted(od.buy_orders.keys(), reverse=True):
                if bp <= lim_sell or pos_adj <= -self.SIGNAL_MAX_POS:
                    break
                cap = min(self.SIGNAL_MAX_POS + pos_adj, self.LIMIT + pos_adj)
                if cap <= 0:
                    break
                vol = min(od.buy_orders[bp], cap)
                if vol > 0:
                    orders.append(Order(ASH, bp, -vol))
                    pos_adj -= vol

        buy_cap  = self.LIMIT - pos_adj
        sell_cap = self.LIMIT + pos_adj

        bid_wall = min(od.buy_orders.keys())
        ask_wall = max(od.sell_orders.keys())
        max_bid  = math.ceil(wall_mid) - 1
        min_ask  = math.floor(wall_mid) + 1

        bid_p = bid_wall + 1
        for p in sorted(od.buy_orders.keys(), reverse=True):
            if p < wall_mid:
                bid_p = (p + 1) if od.buy_orders[p] > 1 else p
                break
        bid_p = min(bid_p, max_bid)

        ask_p = ask_wall - 1
        for p in sorted(od.sell_orders.keys()):
            if p > wall_mid:
                ask_p = (p - 1) if od.sell_orders[p] < -1 else p
                break
        ask_p = max(ask_p, min_ask)

        if bid_p >= ask_p:
            bid_p = ask_p - 1

        if buy_cap > 0 and bid_p > 0:
            wb = int(buy_cap * self.ASH_WALL_FRAC)
            ib = buy_cap - wb
            if ib > 0:
                orders.append(Order(ASH, bid_p, ib))
            if wb >= self.ASH_WALL_MIN and bid_wall < bid_p:
                orders.append(Order(ASH, bid_wall, wb))
            elif wb > 0:
                orders.append(Order(ASH, bid_p, wb))

        if sell_cap > 0 and ask_p > 0:
            ws  = int(sell_cap * self.ASH_WALL_FRAC)
            iss = sell_cap - ws
            if iss > 0:
                orders.append(Order(ASH, ask_p, -iss))
            if ws >= self.ASH_WALL_MIN and ask_wall > ask_p:
                orders.append(Order(ASH, ask_wall, -ws))
            elif ws > 0:
                orders.append(Order(ASH, ask_p, -ws))

        return orders

    def trade_pepper(self, od: 'OrderDepth', pos: int, timestamp: int, sd: dict) -> List['Order']:
        orders: List[Order] = []

        last_ts = sd["pb_last_ts"]
        if sd["pb_init"] and last_ts > 0 and timestamp < last_ts - 50_000:
            sd["pb_init"] = False
        sd["pb_last_ts"] = timestamp

        if not sd["pb_init"]:
            if od.buy_orders and od.sell_orders:
                mid = (max(od.buy_orders.keys()) + min(od.sell_orders.keys())) / 2.0
                sd["pb"] = mid - self.DRIFT * timestamp
                sd["pb_init"] = True
            return orders

        fv = sd["pb"] + self.DRIFT * timestamp

        if od.buy_orders and od.sell_orders:
            mid = (max(od.buy_orders.keys()) + min(od.sell_orders.keys())) / 2.0
            if abs(mid - fv) > self.PEP_ANCHOR:
                sd["pb"] = mid - self.DRIFT * timestamp
                fv = mid
            if pos > 0 and mid < fv - self.PEP_STOP:
                remaining = pos
                for bp in sorted(od.buy_orders.keys(), reverse=True):
                    if remaining <= 0:
                        break
                    vol = min(od.buy_orders[bp], remaining)
                    orders.append(Order(PEPPER, bp, -vol))
                    remaining -= vol
                return orders

        buy_cap = self.LIMIT - pos
        if buy_cap <= 0:
            return orders

        if od.sell_orders:
            for ap in sorted(od.sell_orders.keys()):
                if buy_cap <= 0 or ap > fv + self.PEP_OVER:
                    break
                vol = min(abs(od.sell_orders[ap]), buy_cap)
                orders.append(Order(PEPPER, ap, vol))
                buy_cap -= vol

        if buy_cap > 0 and od.buy_orders and od.sell_orders:
            bb = max(od.buy_orders.keys())
            ba = min(od.sell_orders.keys())
            bp = min(bb + 1, ba - 1, int(fv) + 6)
            if bp > 0:
                orders.append(Order(PEPPER, bp, buy_cap))

        return orders

    def _clamp(self, orders: List['Order'], pos: int) -> List['Order']:
        mb = self.LIMIT - pos
        ms = self.LIMIT + pos
        tb = ts = 0
        valid = []
        for o in orders:
            if o.quantity > 0:
                can = min(o.quantity, mb - tb)
                if can > 0:
                    valid.append(Order(o.symbol, o.price, can))
                    tb += can
            elif o.quantity < 0:
                can = min(abs(o.quantity), ms - ts)
                if can > 0:
                    valid.append(Order(o.symbol, o.price, -can))
                    ts += can
        return valid

    def run(self, state: 'TradingState'):
        sd = self._load_state(state.traderData)
        result: Dict[str, List[Order]] = {}
        for product, od in state.order_depths.items():
            pos = int(state.position.get(product, 0))
            if product == ASH:
                result[product] = self._clamp(self.trade_ash(od, pos, sd), pos)
            elif product == PEPPER:
                result[product] = self._clamp(self.trade_pepper(od, pos, state.timestamp, sd), pos)
            else:
                result[product] = []
        return result, 0, json.dumps(sd)
