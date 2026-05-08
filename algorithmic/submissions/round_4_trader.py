# Round 4: adds named-participant signal tracking (Mark 01/14/22/38/55/67)
# Soft regime detection from day-open price (low/mid/high) blends z-score thresholds
# EWMA short-term reversion overlay and mid-dip rebound state machine on VELVET

import json
import math
from typing import Dict, List, Optional

from datamodel import Order, OrderDepth, TradingState

UNDERLYING = "VELVETFRUIT_EXTRACT"
HYDROGEL   = "HYDROGEL_PACK"

VELVET_LIMIT            = 200
VELVET_MM_SIZE          = 20
VELVET_MM_MAX_POSITION  = 175
VELVET_UNWIND_SIZE      = 10

HYDROGEL_LIMIT            = 200
HYDROGEL_ANCHOR           = 9995
HYDROGEL_SIGMA            = 25
HYDROGEL_SHORT_Z          = 1.2
HYDROGEL_LONG_Z           = -1.8
HYDROGEL_MM_SIZE          = 10
HYDROGEL_MM_MAX_POSITION  = 80
HYDROGEL_UNWIND_SIZE      = 15

POSITION_LIMIT     = 300
SHARED_DELTA_LIMIT = 1800

VELVET_ANCHOR              = 5250
VELVET_SIGMA               = 12
VELVET_SHORT_Z             = 2.0   # base threshold; blended up/down per regime
VELVET_LONG_Z              = -0.6  # asymmetric: we're quicker to buy dips than sell rips
VELVET_EXTREME_OPEN_Z      = 2.5   # >2.5σ open triggers shifted anchor (price already elevated)
VELVET_EXTREME_ANCHOR_SHIFT = -15  # shift anchor down on extreme-high opens; less trigger-happy to short
VELVET_EXTREME_SIGMA_SHIFT  = 2
YEAR_DAYS                  = 252.0
DEFAULT_TTE_DAYS           = 4.0
MARKET_SIGMA               = 0.175 # tightened from R3's 0.20; better fit to observed ATM IV
OPTION_SPOT_STABILIZER_WEIGHT = 0.25  # blend ITM-implied spot with actual; smooths BS inputs
ELEVATED_OPEN_SIGMA_Z      = 0.5
ZERO_LOTTERY_PRODUCTS      = ["VEV_6000", "VEV_6500"]

# Named-participant TTLs (ticks): signals expire if the bot hasn't traded recently.
# 3000 ticks ≈ signal half-life from event study; 6000 for basket trades which have slower impact.
M01_MARK55_TTL       = 3000
M01_M22_BASKET_TTL   = 6000
M01_MARK55_MM_SIZE   = 30   # upsize passive quotes when Mark55 is active (informed flow = tighter MM)
M67_VELVET_TTL       = 3000
M67_VELVET_SHORT_Z_WIDEN = 1.0  # M67 buys VELVET directionally; widen short threshold by 1σ while active
M38_ACTIVITY_TTL     = 3000
M38_HYDROGEL_Z_WIDEN = 0.0  # M38 we track but don't widen on; net impact was negligible
M14_HYDROGEL_TTL     = 3000

VOUCHERS: Dict[str, Dict] = {
    "VEV_4000": {
        "strike": 4000,

        "take_edge": 8,   "quote_edge": 0,
        "inventory_skew": -14.0,
        "take_size": 40,  "exit_take_size": 60,  "exit_sweep_levels": 2,
        "quote_size": 150,
        "long_take_z": -2.4, "short_take_z": 2.0,
        "long_exit_z":  2.2, "short_exit_z": -0.7,
    },
    "VEV_4500": {
        "strike": 4500,
        "take_edge": 8,   "quote_edge": 0,
        "inventory_skew": -14.0,
        "take_size": 60,  "exit_take_size": 100, "exit_sweep_levels": 1,
        "quote_size": 120,
        "long_take_z": -1.5, "short_take_z": 2.0,
        "long_exit_z":  1.4, "short_exit_z": -0.7,
    },
    "VEV_5000": {
        "elevated_sigma_override": 0.18,
        "strike": 5000,
        "take_edge": 8,   "quote_edge": 1,
        "inventory_skew": -14.0,
        "take_size": 60,  "exit_take_size": 100, "exit_sweep_levels": 1,
        "quote_size": 100,
        "long_take_z": -0.8, "short_take_z": 2.0,
        "long_exit_z":  1.5, "short_exit_z": -0.7,
        "fair_mode": "bs",
    },
    "VEV_5100": {
        "elevated_sigma_override": 0.178,
        "strike": 5100,
        "take_edge": 8,   "quote_edge": 3,
        "inventory_skew": -14.0,
        "take_size": 30,  "exit_take_size": 100, "exit_sweep_levels": 1,
        "quote_size": 35,
        "long_take_z": -0.8, "short_take_z": 2.0,
        "long_exit_z":  1.5, "short_exit_z": -0.7,
        "fair_mode": "bs",
    },

    "VEV_5200": {
        "strike": 5200,
        "take_edge": 8,   "quote_edge": 99,
        "inventory_skew": -14.0,
        "take_size": 60,  "exit_take_size": 60,  "exit_sweep_levels": 1,
        "quote_size": 0,
        "long_take_z": -0.8, "short_take_z": 2.0,
        "long_exit_z":  1.5, "short_exit_z": -0.7,
        "fair_mode": "bs",
        "m01_catch_edge": 5, "m01_catch_size": 12, "m01_catch_max_position": 130,
    },
    "VEV_5300": {
        "strike": 5300,
        "take_edge": 8,   "quote_edge": 99,
        "inventory_skew": -14.0,
        "take_size": 60,  "exit_take_size": 60,  "exit_sweep_levels": 1,
        "quote_size": 0,
        "long_take_z": -0.8, "short_take_z": 2.0,
        "long_exit_z":  1.5, "short_exit_z": -0.7,
        "fair_mode": "bs",
        "m01_catch_edge": 5, "m01_catch_size": 12, "m01_catch_max_position": 130,
    },
    "VEV_5400": {
        "strike": 5400,
        "mode": "wing",

        "take_size": 300,  "exit_take_size": 300,
        "long_take_z": -0.5, "short_take_z": 2.0,
        "long_exit_z": 99.0, "short_exit_z": -99.0,
        "long_entry_price": 999, "short_entry_price": 0,
        "long_exit_price": 999,  "short_exit_price": -1,
        "m01_catch_bid": 5, "m01_catch_size": 64, "m01_catch_max_position": 80,
    },
    "VEV_5500": {
        "strike": 5500,
        "mode": "wing",
        "take_size": 300,  "exit_take_size": 300,
        "long_take_z": -1.5, "short_take_z": 2.0,
        "long_exit_z": 99.0, "short_exit_z": -99.0,
        "long_entry_price": 999, "short_entry_price": 0,
        "long_exit_price": 999,  "short_exit_price": -1,
        "m01_catch_bid": 1, "m01_catch_size": 80, "m01_catch_max_position": 90,
    },
}

class Trader:

    def run(self, state: TradingState):
        result: Dict[str, List[Order]] = {p: [] for p in state.order_depths}
        saved = self._decode_state(state.traderData)
        now   = int(getattr(state, "timestamp", 0) or 0)

        self._update_signal_state(state, saved, now)

        if UNDERLYING in state.order_depths:
            result[UNDERLYING] = self._trade_velvet(
                state.order_depths[UNDERLYING],
                state.position.get(UNDERLYING, 0),
                saved, now,
            )

        if HYDROGEL in state.order_depths:
            result[HYDROGEL] = self._trade_hydrogel(
                state.order_depths[HYDROGEL],
                state.position.get(HYDROGEL, 0),
                saved, now,
            )

        underlying_mid = self._mid_price(state.order_depths.get(UNDERLYING))
        if underlying_mid is None:
            return result, 0, self._encode_state(saved)

        self._record_velvet_open(saved, underlying_mid)
        z = self._velvet_z(underlying_mid, saved)

        opt_mid = self._option_underlying_mid(state, underlying_mid)
        tte     = self._implied_tte(state, opt_mid)

        elevated_open = self._velvet_elevated_open(saved, underlying_mid)

        def sigma_for(cfg: Dict) -> float:
            if elevated_open and "elevated_sigma_override" in cfg:
                return cfg["elevated_sigma_override"]
            return MARKET_SIGMA

        deltas = {
            p: self._bs_delta(opt_mid, cfg["strike"], tte, sigma_for(cfg))
            for p, cfg in VOUCHERS.items()
        }

        underlying_delta = state.position.get(UNDERLYING, 0) + sum(
            o.quantity for o in result.get(UNDERLYING, [])
        )
        shared_delta = sum(
            state.position.get(p, 0) * deltas[p] for p in VOUCHERS
        ) + underlying_delta

        for prod, cfg in VOUCHERS.items():
            depth = state.order_depths.get(prod)
            if depth is None:
                continue

            position = state.position.get(prod, 0)
            delta    = deltas[prod]
            dcap     = max(delta, 0.01)
            sbc      = max(0, int((SHARED_DELTA_LIMIT - shared_delta) / dcap))
            ssc      = max(0, int((SHARED_DELTA_LIMIT + shared_delta) / dcap))

            if cfg.get("mode") == "wing":
                orders = self._orders_for_wing(
                    prod, depth, position, z, cfg, saved, sbc, ssc, now,
                )
            else:
                if cfg.get("fair_mode") == "bs":
                    fair = self._bs_call(opt_mid, cfg["strike"], tte, sigma_for(cfg))
                else:
                    fair = max(0.0, opt_mid - cfg["strike"])
                reservation = fair - cfg["inventory_skew"] * position / POSITION_LIMIT

                orders = self._orders_for_voucher(
                    prod, depth, reservation, position, z, cfg, saved, sbc, ssc, now,
                )

            result[prod]  = orders
            net           = sum(o.quantity for o in orders)
            shared_delta += net * delta

        for prod in ZERO_LOTTERY_PRODUCTS:
            if prod in state.order_depths:
                pos   = state.position.get(prod, 0)
                depth = state.order_depths[prod]
                result[prod] = self._orders_for_zero_lottery(prod, depth, pos)

        return result, 0, self._encode_state(saved)

    def _update_signal_state(
        self,
        state: TradingState,
        saved: Dict,
        now: int,
    ) -> None:
        mt = getattr(state, "market_trades", {}) or {}
        basket_products: set = set()
        basket_qty = 0
        basket_ts  = None

        for prod, trades in mt.items():
            for tr in trades:
                buyer  = getattr(tr, "buyer",     "")
                seller = getattr(tr, "seller",    "")
                qty    = int(getattr(tr, "quantity", 0) or 0)
                ts     = int(getattr(tr, "timestamp", now) or now)

                if prod == UNDERLYING and (buyer == "Mark 55" or seller == "Mark 55"):
                    saved["m01_m55_ts"] = max(saved.get("m01_m55_ts", -(10**12)), ts)

                if prod == UNDERLYING:
                    if buyer == "Mark 55" and seller == "Mark 14":
                        saved["m55_buy_m14_ts"] = max(saved.get("m55_buy_m14_ts", -(10**12)), ts)
                    if buyer == "Mark 14" and seller == "Mark 55":
                        saved["m14_buy_m55_ts"] = max(saved.get("m14_buy_m55_ts", -(10**12)), ts)

                if prod == UNDERLYING and buyer == "Mark 67":
                    saved["m67_velvet_buy_ts"] = max(saved.get("m67_velvet_buy_ts", -(10**12)), ts)

                if prod == HYDROGEL and (buyer == "Mark 38" or seller == "Mark 38"):
                    saved["m38_ts"] = max(saved.get("m38_ts", -(10**12)), ts)
                    if buyer == "Mark 38":
                        saved["m38_hydrogel_buy_ts"] = max(saved.get("m38_hydrogel_buy_ts", -(10**12)), ts)
                    if seller == "Mark 38":
                        saved["m38_hydrogel_sell_ts"] = max(saved.get("m38_hydrogel_sell_ts", -(10**12)), ts)

                if prod == HYDROGEL and (buyer == "Mark 14" or seller == "Mark 14"):
                    if buyer == "Mark 14":
                        saved["m14_hydrogel_buy_ts"]  = max(saved.get("m14_hydrogel_buy_ts",  -(10**12)), ts)
                    if seller == "Mark 14":
                        saved["m14_hydrogel_sell_ts"] = max(saved.get("m14_hydrogel_sell_ts", -(10**12)), ts)

                if (
                    isinstance(prod, str)
                    and prod.startswith("VEV_")
                    and buyer == "Mark 01"
                    and seller == "Mark 22"
                ):
                    basket_products.add(prod)
                    basket_qty += qty
                    basket_ts   = ts if basket_ts is None else max(basket_ts, ts)

        # Mark 01 buying multiple voucher strikes from Mark 22 in the same tick window
        # is a strong signal that Mark 01 is taking a directional position on VELVET.
        # We require at least 3 different strikes or 10 total contracts to filter noise.
        if basket_ts is not None and (len(basket_products) >= 3 or basket_qty >= 10):
            saved["m01_m22_basket_ts"] = max(saved.get("m01_m22_basket_ts", -(10**12)), basket_ts)

    def _recent(self, saved: Dict, key: str, now: int, ttl: int) -> bool:
        ts = saved.get(key)
        return isinstance(ts, (int, float)) and 0 <= now - ts <= ttl

    def _record_velvet_open(self, saved: Dict, mid: float) -> None:
        if "open_mid" not in saved:
            saved["open_mid"] = mid

    def _velvet_z(self, mid: float, saved: Dict) -> float:
        # On days where VELVET opened extremely high, shift the anchor down and widen sigma.
        # This prevents over-eagerness to short a day that's just genuinely elevated.
        open_mid     = saved.get("open_mid", mid)
        extreme_open = open_mid > VELVET_ANCHOR + VELVET_EXTREME_OPEN_Z * VELVET_SIGMA
        anchor = VELVET_ANCHOR + (VELVET_EXTREME_ANCHOR_SHIFT if extreme_open else 0)
        sigma  = VELVET_SIGMA  + (VELVET_EXTREME_SIGMA_SHIFT  if extreme_open else 0)
        return (mid - anchor) / sigma

    def _velvet_elevated_open(self, saved: Dict, fallback_mid: Optional[float] = None) -> bool:
        open_mid = saved.get("open_mid", fallback_mid)
        return (
            isinstance(open_mid, (int, float))
            and open_mid > VELVET_ANCHOR + ELEVATED_OPEN_SIGMA_Z * VELVET_SIGMA
        )

    def _option_underlying_mid(
        self,
        state: TradingState,
        actual_mid: float,
    ) -> float:
        # Blend the actual spot mid with an implied spot derived from deep ITM option prices.
        # ITM options with negligible time value satisfy: option_mid ~= spot - strike,
        # so spot ~= option_mid + strike. Averaging these gives a more stable input for BS pricing.
        estimates = [actual_mid]
        for prod, K in [("VEV_4000", 4000), ("VEV_4500", 4500)]:
            m = self._mid_price(state.order_depths.get(prod))
            if m is not None:
                estimates.append(m + K)
        estimates.sort()
        median = estimates[len(estimates) // 2]
        return (
            (1.0 - OPTION_SPOT_STABILIZER_WEIGHT) * actual_mid
            + OPTION_SPOT_STABILIZER_WEIGHT * median
        )

    def _trade_velvet(
        self,
        depth:    OrderDepth,
        position: int,
        saved:    Dict,
        now:      int,
    ) -> List[Order]:
        if not depth.buy_orders or not depth.sell_orders:
            return []

        bb   = max(depth.buy_orders)
        ba   = min(depth.sell_orders)
        bvol = depth.buy_orders[bb]
        avol = -depth.sell_orders[ba]
        mid  = (bb + ba) / 2.0

        self._record_velvet_open(saved, mid)
        z = self._velvet_z(mid, saved)
        saved["vz"] = round(z, 4)

        short_z = VELVET_SHORT_Z + (
            M67_VELVET_SHORT_Z_WIDEN
            if self._recent(saved, "m67_velvet_buy_ts", now, M67_VELVET_TTL)
            else 0.0
        )

        if z > short_z:
            qty = min(position + VELVET_LIMIT, bvol)
            if qty > 0:
                return [Order(UNDERLYING, bb, -qty)]

        if z < VELVET_LONG_Z:
            qty = min(VELVET_LIMIT - position, avol)
            if qty > 0:
                return [Order(UNDERLYING, ba, qty)]

        pb = bb + 1
        pa = ba - 1
        if pb >= pa:
            return []

        orders: List[Order] = []
        if abs(position) < VELVET_MM_MAX_POSITION:

            base = (
                M01_MARK55_MM_SIZE
                if self._recent(saved, "m01_m55_ts", now, M01_MARK55_TTL)
                else VELVET_MM_SIZE
            )
            bs, as_ = self._velvet_mm_sizes(position, base)

            if self._recent(saved, "m67_velvet_buy_ts", now, M67_VELVET_TTL) and z < 1.8:
                bs = min(bs * 2, 60)
                as_ = max(1, as_ // 2)

            m55_buy_m14 = self._recent(saved, "m55_buy_m14_ts", now, 6000)
            m14_buy_m55 = self._recent(saved, "m14_buy_m55_ts", now, 6000)
            if m55_buy_m14 and not m14_buy_m55:
                bs = min(bs * 2, 50)
                as_ = max(1, as_ // 2)
            elif m14_buy_m55 and not m55_buy_m14:
                bs = max(1, bs // 2)
                as_ = min(as_ * 2, 50)

            bs  = min(bs,  VELVET_LIMIT - position)
            as_ = min(as_, VELVET_LIMIT + position)
            if bs  > 0: orders.append(Order(UNDERLYING, pb,  bs))
            if as_ > 0: orders.append(Order(UNDERLYING, pa, -as_))

        elif position >=  VELVET_MM_MAX_POSITION and z > -0.5:
            q = min(VELVET_UNWIND_SIZE, position, VELVET_LIMIT + position)
            if q > 0: orders.append(Order(UNDERLYING, pa, -q))

        elif position <= -VELVET_MM_MAX_POSITION and z < 0.5:
            q = min(VELVET_UNWIND_SIZE, -position, VELVET_LIMIT - position)
            if q > 0: orders.append(Order(UNDERLYING, pb, q))

        return orders

    def _velvet_mm_sizes(self, position: int, base: int = VELVET_MM_SIZE):
        r  = position / VELVET_LIMIT
        ba = max(0.0, 1.0 - 0.3 * max(0.0,  r * 3.0))
        aa = max(0.0, 1.0 - 0.3 * max(0.0, -r * 3.0))
        return (
            max(1, int(base * ba)),
            max(1, int(base * aa)),
        )

    def _trade_hydrogel(
        self,
        depth:    OrderDepth,
        position: int,
        saved:    Dict,
        now:      int,
    ) -> List[Order]:
        if not depth.buy_orders or not depth.sell_orders:
            return []

        bb   = max(depth.buy_orders)
        ba   = min(depth.sell_orders)
        bvol = depth.buy_orders[bb]
        avol = -depth.sell_orders[ba]
        mid  = (bb + ba) / 2.0
        z    = (mid - HYDROGEL_ANCHOR) / HYDROGEL_SIGMA
        saved["hz"] = round(z, 4)

        m38 = self._recent(saved, "m38_ts", now, M38_ACTIVITY_TTL)
        short_z = HYDROGEL_SHORT_Z + (M38_HYDROGEL_Z_WIDEN if m38 else 0.0)
        long_z  = HYDROGEL_LONG_Z  - (M38_HYDROGEL_Z_WIDEN if m38 else 0.0)

        if z > short_z:
            qty = min(position + HYDROGEL_LIMIT, bvol)
            if qty > 0:
                return [Order(HYDROGEL, bb, -qty)]

        if z < long_z:
            qty = min(HYDROGEL_LIMIT - position, avol)
            if qty > 0:
                return [Order(HYDROGEL, ba, qty)]

        pb = bb + 1
        pa = ba - 1
        if pb >= pa:
            return []

        orders: List[Order] = []
        if abs(position) < HYDROGEL_MM_MAX_POSITION:
            bs, as_ = self._hydrogel_passive_sizes(position)

            m38_buy  = self._recent(saved, "m38_hydrogel_buy_ts",  now, M38_ACTIVITY_TTL)
            m38_sell = self._recent(saved, "m38_hydrogel_sell_ts", now, M38_ACTIVITY_TTL)
            if m38_buy and not m38_sell:

                bs  = max(1, bs  // 2)
                as_ = as_ * 2
            elif m38_sell and not m38_buy:

                bs  = bs  * 2
                as_ = max(1, as_ // 2)

            m14_buy  = self._recent(saved, "m14_hydrogel_buy_ts",  now, M14_HYDROGEL_TTL)
            m14_sell = self._recent(saved, "m14_hydrogel_sell_ts", now, M14_HYDROGEL_TTL)
            if m14_buy and not m14_sell:

                bs  = min(bs * 2, HYDROGEL_MM_SIZE * 3)
                as_ = max(1, as_ // 2)
            elif m14_sell and not m14_buy:
                bs  = max(1, bs  // 2)
                as_ = min(as_ * 2, HYDROGEL_MM_SIZE * 3)

            bs  = min(bs,  HYDROGEL_LIMIT - position)
            as_ = min(as_, HYDROGEL_LIMIT + position)
            if bs  > 0: orders.append(Order(HYDROGEL, pb,  bs))
            if as_ > 0: orders.append(Order(HYDROGEL, pa, -as_))

        elif position >=  HYDROGEL_MM_MAX_POSITION and z > -0.5:
            q = min(HYDROGEL_UNWIND_SIZE, position, HYDROGEL_LIMIT + position)
            if q > 0: orders.append(Order(HYDROGEL, pa, -q))

        elif position <= -HYDROGEL_MM_MAX_POSITION and z < 0.5:
            q = min(HYDROGEL_UNWIND_SIZE, -position, HYDROGEL_LIMIT - position)
            if q > 0: orders.append(Order(HYDROGEL, pb, q))

        return orders

    def _hydrogel_passive_sizes(self, position: int):
        r  = position / HYDROGEL_LIMIT
        ba = max(0.0, 1.0 - 0.3 * max(0.0,  r * 3.0))
        aa = max(0.0, 1.0 - 0.3 * max(0.0, -r * 3.0))
        return (
            max(1, int(HYDROGEL_MM_SIZE * ba)),
            max(1, int(HYDROGEL_MM_SIZE * aa)),
        )

    def _orders_for_voucher(
        self,
        prod:     str,
        depth:    OrderDepth,
        rsv:      float,
        position: int,
        z:        float,
        cfg:      Dict,
        saved:    Dict,
        sbc:      int,
        ssc:      int,
        now:      int,
    ) -> List[Order]:
        bb = self._best_bid(depth)
        ba = self._best_ask(depth)
        if bb is None or ba is None:
            return []

        buy_cap  = min(POSITION_LIMIT - position, sbc)
        sell_cap = min(POSITION_LIMIT + position, ssc)

        lez = cfg.get("long_exit_z")
        sez = cfg.get("short_exit_z")
        ltz = cfg["long_take_z"]
        stz = cfg["short_take_z"]

        sk = prod + "_es"
        lk = prod + "_el"

        if position >= 0: saved[sk] = False
        if position <= 0: saved[lk] = False
        if sez is not None and position < 0 and z <= sez: saved[sk] = True
        if lez is not None and position > 0 and z >= lez: saved[lk] = True

        if saved.get(sk) and position < 0:
            o = self._sweep_buy_order(
                prod, depth,
                min(cfg.get("exit_take_size", cfg["take_size"]), -position, buy_cap),
                cfg.get("exit_sweep_levels", 1),
            )
            if o: return [o]

        if saved.get(lk) and position > 0:
            o = self._sweep_sell_order(
                prod, depth,
                min(cfg.get("exit_take_size", cfg["take_size"]), position, sell_cap),
                cfg.get("exit_sweep_levels", 1),
            )
            if o: return [o]

        if ltz is not None and buy_cap > 0 and z < ltz:
            q = min(cfg["take_size"], buy_cap, -depth.sell_orders[ba])
            if q > 0: return [Order(prod, ba, q)]

        if stz is not None and sell_cap > 0 and z > stz:
            q = min(cfg["take_size"], sell_cap, depth.buy_orders[bb])
            if q > 0: return [Order(prod, bb, -q)]

        orders: List[Order] = []
        bu = su = 0

        te = cfg["take_edge"]
        if buy_cap > 0 and ba <= rsv - te:
            q = min(cfg["take_size"], buy_cap, -depth.sell_orders[ba])
            if q > 0:
                orders.append(Order(prod, ba, q))
                bu += q

        if sell_cap > 0 and bb >= rsv + te:
            q = min(cfg["take_size"], sell_cap, depth.buy_orders[bb])
            if q > 0:
                orders.append(Order(prod, bb, -q))
                su += q

        qe = cfg["quote_edge"]
        qs = cfg["quote_size"]
        if qs > 0:
            pb = min(bb + 1, int(rsv - qe))
            pa = max(ba - 1, int(rsv + qe + 0.9999))
            if pb < pa:
                bq = min(self._passive_size(position,  1, qs), buy_cap  - bu)
                sq = min(self._passive_size(position, -1, qs), sell_cap - su)
                if bq > 0 and pb > 0: orders.append(Order(prod, pb,  bq))
                if sq > 0:            orders.append(Order(prod, pa, -sq))
                bu += max(0, bq)
                su += max(0, sq)

        catch = self._m01_voucher_catch(
            prod, depth, rsv, position, cfg, saved,
            max(0, buy_cap - bu), now,
        )
        if catch: orders.append(catch)

        return orders

    def _orders_for_wing(
        self,
        prod:     str,
        depth:    OrderDepth,
        position: int,
        z:        float,
        cfg:      Dict,
        saved:    Dict,
        sbc:      int,
        ssc:      int,
        now:      int,
    ) -> List[Order]:
        bb = self._best_bid(depth)
        ba = self._best_ask(depth)
        if bb is None or ba is None:
            return []

        buy_cap  = min(POSITION_LIMIT - position, sbc)
        sell_cap = min(POSITION_LIMIT + position, ssc)

        sk = prod + "_es"
        lk = prod + "_el"

        if position >= 0: saved[sk] = False
        if position <= 0: saved[lk] = False

        if position < 0 and (z <= cfg["short_exit_z"] or ba <= cfg["short_exit_price"]):
            saved[sk] = True
        if position > 0 and (z >= cfg["long_exit_z"]  or bb >= cfg["long_exit_price"]):
            saved[lk] = True

        if saved.get(sk) and position < 0:
            q = min(cfg["exit_take_size"], -position, buy_cap, -depth.sell_orders[ba])
            if q > 0: return [Order(prod, ba, q)]

        if saved.get(lk) and position > 0:
            q = min(cfg["exit_take_size"], position, sell_cap, depth.buy_orders[bb])
            if q > 0: return [Order(prod, bb, -q)]

        if buy_cap  > 0 and z < cfg["long_take_z"]  and ba <= cfg["long_entry_price"]:
            q = min(cfg["take_size"], buy_cap,  -depth.sell_orders[ba])
            if q > 0: return [Order(prod, ba, q)]

        if sell_cap > 0 and z > cfg["short_take_z"] and bb >= cfg["short_entry_price"]:
            q = min(cfg["take_size"], sell_cap, depth.buy_orders[bb])
            if q > 0: return [Order(prod, bb, -q)]

        catch = self._m01_wing_catch(prod, depth, position, cfg, saved, buy_cap, now)
        return [catch] if catch else []

    def _m01_voucher_catch(
        self,
        prod:      str,
        depth:     OrderDepth,
        rsv:       float,
        position:  int,
        cfg:       Dict,
        saved:     Dict,
        buy_cap:   int,
        now:       int,
    ) -> Optional[Order]:
        if buy_cap <= 0 or "m01_catch_edge" not in cfg:
            return None
        if not self._recent(saved, "m01_m22_basket_ts", now, M01_M22_BASKET_TTL):
            return None
        bb = self._best_bid(depth)
        ba = self._best_ask(depth)
        if bb is None or ba is None:
            return None
        max_pos = cfg.get("m01_catch_max_position", POSITION_LIMIT)
        if position >= max_pos:
            return None
        lp = min(bb + 1, ba - 1, int(rsv - cfg["m01_catch_edge"]))
        if lp <= bb or lp <= 0:
            return None
        q = min(cfg["m01_catch_size"], buy_cap, max_pos - position)
        return Order(prod, lp, q) if q > 0 else None

    def _m01_wing_catch(
        self,
        prod:     str,
        depth:    OrderDepth,
        position: int,
        cfg:      Dict,
        saved:    Dict,
        buy_cap:  int,
        now:      int,
    ) -> Optional[Order]:
        if buy_cap <= 0 or "m01_catch_bid" not in cfg:
            return None
        if not self._recent(saved, "m01_m22_basket_ts", now, M01_M22_BASKET_TTL):
            return None
        bb = self._best_bid(depth)
        ba = self._best_ask(depth)
        if bb is None or ba is None:
            return None
        max_pos = cfg.get("m01_catch_max_position", POSITION_LIMIT)
        if position >= max_pos:
            return None
        lp = min(cfg["m01_catch_bid"], ba - 1)
        if lp <= bb or lp <= 0:
            return None
        q = min(cfg["m01_catch_size"], buy_cap, max_pos - position)
        return Order(prod, lp, q) if q > 0 else None

    def _orders_for_zero_lottery(
        self,
        prod:     str,
        depth:    OrderDepth,
        position: int,
    ) -> List[Order]:
        cap = POSITION_LIMIT - position
        if cap <= 0:
            return []

        return [Order(prod, 0, cap)]

    def _implied_tte(self, state: TradingState, underlying_mid: float) -> float:
        estimates: List[float] = []
        for strike in [5000, 5100, 5200, 5300]:
            mid = self._mid_price(state.order_depths.get(f"VEV_{strike}"))
            if mid is None:
                continue
            est = self._solve_tte(underlying_mid, strike, mid)
            if est is not None:
                estimates.append(est)
        if not estimates:
            return DEFAULT_TTE_DAYS / YEAR_DAYS
        estimates.sort()
        return estimates[len(estimates) // 2]

    def _solve_tte(
        self,
        underlying: float,
        strike: int,
        option_mid: float,
    ) -> Optional[float]:
        intrinsic = max(0.0, underlying - strike)
        if option_mid <= intrinsic + 0.05:
            return None
        low  = 0.25 / YEAR_DAYS
        high = 12.0 / YEAR_DAYS
        if self._bs_call(underlying, strike, high, MARKET_SIGMA) < option_mid:
            return None
        for _ in range(20):
            mid = (low + high) / 2.0
            if self._bs_call(underlying, strike, mid, MARKET_SIGMA) < option_mid:
                low = mid
            else:
                high = mid
        return (low + high) / 2.0

    def _sweep_buy_order(
        self, prod: str, depth: OrderDepth, mx: int, levels: int,
    ) -> Optional[Order]:
        if mx <= 0: return None
        qty = lp = 0
        for price in sorted(depth.sell_orders)[:levels]:
            avail = -depth.sell_orders[price]
            if avail <= 0: continue
            take = min(mx - qty, avail)
            if take <= 0: break
            qty += take; lp = price
            if qty >= mx: break
        return Order(prod, lp, qty) if qty > 0 and lp else None

    def _sweep_sell_order(
        self, prod: str, depth: OrderDepth, mx: int, levels: int,
    ) -> Optional[Order]:
        if mx <= 0: return None
        qty = lp = 0
        for price in sorted(depth.buy_orders, reverse=True)[:levels]:
            avail = depth.buy_orders[price]
            if avail <= 0: continue
            take = min(mx - qty, avail)
            if take <= 0: break
            qty += take; lp = price
            if qty >= mx: break
        return Order(prod, lp, -qty) if qty > 0 and lp else None

    def _passive_size(self, position: int, side: int, qs: int) -> int:
        if side > 0:
            if position >= 180: return 0
            if position >=  90: return qs // 2
        else:
            if position <= -180: return 0
            if position <=  -90: return qs // 2
        return qs

    def _bs_call(self, S: float, K: int, T: float, sigma: float) -> float:
        intrinsic = max(0.0, S - K)
        if T <= 0.0 or sigma <= 0.0: return intrinsic
        sqT = math.sqrt(T)
        d1  = (math.log(S / K) + 0.5 * sigma * sigma * T) / (sigma * sqT)
        d2  = d1 - sigma * sqT
        return S * self._ncdf(d1) - K * self._ncdf(d2)

    def _bs_delta(self, S: float, K: int, T: float, sigma: float) -> float:
        if T <= 0.0 or sigma <= 0.0: return 1.0 if S > K else 0.0
        d1 = (math.log(S / K) + 0.5 * sigma * sigma * T) / (sigma * math.sqrt(T))
        return self._ncdf(d1)

    def _ncdf(self, x: float) -> float:
        return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

    def _mid_price(self, depth: Optional[OrderDepth]) -> Optional[float]:
        if depth is None: return None
        bb = self._best_bid(depth); ba = self._best_ask(depth)
        if bb is None or ba is None: return None
        return (bb + ba) / 2.0

    def _best_bid(self, d: OrderDepth) -> Optional[int]:
        return max(d.buy_orders)  if d.buy_orders  else None

    def _best_ask(self, d: OrderDepth) -> Optional[int]:
        return min(d.sell_orders) if d.sell_orders else None

    def _decode_state(self, raw: str) -> Dict:
        if not raw: return {}
        try:
            d = json.loads(raw)
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}

    def _encode_state(self, saved: Dict) -> str:
        try:
            return json.dumps(saved, separators=(",", ":"))
        except Exception:
            return "{}"

SOFT_OPEN_CENTERS = {"low": 5245.0, "mid": 5267.5, "high": 5295.5}
SOFT_OPEN_WIDTH = 8.0

def _soft_open_mid(self, saved):
    om = saved.get("open_mid", None)
    return float(om) if isinstance(om, (int, float)) else float(VELVET_ANCHOR)

def _soft_regime_weights(self, saved):
    om = self._soft_open_mid(saved)

    key = round(om, 4)
    cached_key = saved.get("_soft_w_key")
    cached = saved.get("_soft_w")
    if cached_key == key and isinstance(cached, dict):
        return cached

    raw = {}
    for name, center in SOFT_OPEN_CENTERS.items():
        z = (om - center) / SOFT_OPEN_WIDTH
        raw[name] = math.exp(-0.5 * z * z)
    s = sum(raw.values()) or 1.0
    weights = {k: v / s for k, v in raw.items()}
    saved["_soft_w_key"] = key
    saved["_soft_w"] = weights
    return weights

Trader._soft_open_mid = _soft_open_mid
Trader._soft_regime_weights = _soft_regime_weights

SPOT_CFG = {
    "low":  {"vel_long": -1.0, "vel_short": 1.35, "hyd_long": -2.2, "hyd_short": 1.2},
    "mid":  {"vel_long": -3.0, "vel_short": 2.5, "hyd_long": -2.8, "hyd_short": 1.2},
    "high": {"vel_long": -0.8, "vel_short": 2.0, "hyd_long": -0.5, "hyd_short": 1.2},
}

VOUCHER_REGIME_CFG = {
    "low": {
        "VEV_4000": {"long_take_z": -1.0, "short_take_z": 1.8, "long_exit_z": 2.8, "short_exit_z": -1.5, "take_size": 30, "exit_take_size": 60},
        "VEV_4500": {"long_take_z": -1.0, "short_take_z": 1.8, "long_exit_z": 1.5, "short_exit_z": -0.7, "take_size": 30, "exit_take_size": 60},
        "VEV_5000": {"long_take_z": -1.0, "short_take_z": 1.2, "long_exit_z": 1.8, "short_exit_z": -0.7, "take_size": 30, "exit_take_size": 60},
        "VEV_5100": {"long_take_z": -1.0, "short_take_z": 1.2, "long_exit_z": 1.8, "short_exit_z": -2.0, "take_size": 30, "exit_take_size": 100},
        "VEV_5200": {"long_take_z": -1.0, "short_take_z": 1.4, "long_exit_z": 1.5, "short_exit_z": -0.7, "take_size": 30, "exit_take_size": 60},
        "VEV_5300": {"long_take_z": -1.0, "short_take_z": 1.3, "long_exit_z": 1.8, "short_exit_z": -1.5, "take_size": 30, "exit_take_size": 60},
    },
    "mid": {
        "VEV_4000": {"long_take_z": -2.8, "short_take_z": 2.1, "long_exit_z": 2.8, "short_exit_z": -5.0, "take_size": 30, "exit_take_size": 60},
        "VEV_4500": {"long_take_z": -2.8, "short_take_z": 2.0, "long_exit_z": 2.8, "short_exit_z": -5.0, "take_size": 30, "exit_take_size": 60},
        "VEV_5000": {"long_take_z": -3.0, "short_take_z": 2.2, "long_exit_z": 2.8, "short_exit_z": -5.0, "take_size": 30, "exit_take_size": 60},
        "VEV_5100": {"long_take_z": -3.0, "short_take_z": 2.3, "long_exit_z": 2.8, "short_exit_z": -5.0, "take_size": 30, "exit_take_size": 60},
        "VEV_5200": {"long_take_z": -3.0, "short_take_z": 2.3, "long_exit_z": 2.8, "short_exit_z": -5.0, "take_size": 30, "exit_take_size": 60},
        "VEV_5300": {"long_take_z": -3.0, "short_take_z": 2.3, "long_exit_z": 2.8, "short_exit_z": -5.0, "take_size": 30, "exit_take_size": 60},
    },
    "high": {
        "VEV_4000": {"long_take_z": -2.8, "short_take_z": 2.8, "long_exit_z": 99.0, "short_exit_z": -2.0, "take_size": 30, "exit_take_size": 60},
        "VEV_4500": {"long_take_z": -2.8, "short_take_z": 2.8, "long_exit_z": 99.0, "short_exit_z": -2.0, "take_size": 30, "exit_take_size": 60},
        "VEV_5000": {"long_take_z": -0.8, "short_take_z": 1.8, "long_exit_z": 99.0, "short_exit_z": -0.5, "take_size": 60, "exit_take_size": 100},
        "VEV_5100": {"long_take_z": -0.8, "short_take_z": 2.0, "long_exit_z": 99.0, "short_exit_z": -0.7, "take_size": 60, "exit_take_size": 100},
        "VEV_5200": {"long_take_z": -0.8, "short_take_z": 2.0, "long_exit_z": 99.0, "short_exit_z": -0.7, "take_size": 60, "exit_take_size": 60},
        "VEV_5300": {"long_take_z": -0.8, "short_take_z": 2.0, "long_exit_z": 99.0, "short_exit_z": -0.7, "take_size": 30, "exit_take_size": 60},
    },
}

WING_REGIME_CFG = {
    "low": {
        "VEV_5400": {"long_take_z": -1.0, "short_take_z": 1.2, "take_size": 30, "exit_take_size": 60},
        "VEV_5500": {"long_take_z": -1.0, "short_take_z": 1.2, "take_size": 30, "exit_take_size": 60},
    },
    "mid": {
        "VEV_5400": {"long_take_z": -3.0, "short_take_z": 2.2, "take_size": 30, "exit_take_size": 60},
        "VEV_5500": {"long_take_z": -3.0, "short_take_z": 2.2, "take_size": 30, "exit_take_size": 60},
    },
    "high": {
        "VEV_5400": {"long_take_z": -0.5, "short_take_z": 2.0, "take_size": 30, "exit_take_size": 60},
        "VEV_5500": {"long_take_z": -2.5, "short_take_z": 2.8, "take_size": 30, "exit_take_size": 60},
    },
}

def _blend_number(weights, values):
    return sum(weights.get(k, 0.0) * float(v) for k, v in values.items())

def _blend_cfg(weights, table, prod=None):
    keys = set()
    selected = {}
    for regime in ("low", "mid", "high"):
        if prod is None:
            cfg = table.get(regime, {})
        else:
            cfg = table.get(regime, {}).get(prod, {})
        selected[regime] = cfg
        keys.update(cfg.keys())

    out = {}
    for key in keys:
        vals = {r: cfg[key] for r, cfg in selected.items()
                if key in cfg and isinstance(cfg[key], (int, float))}
        if not vals:
            continue
        val = _blend_number(weights, vals)
        if key in ("take_size", "exit_take_size", "exit_sweep_levels"):
            val = max(1, int(round(val)))
        out[key] = val
    return out

# Wrap the base methods to inject regime-blended thresholds at call time.
# We mutate the module globals momentarily (inside try/finally) so the original
# method sees the blended values without needing to be rewritten.
_orig_trade_velvet_soft = Trader._trade_velvet
def _trade_velvet_soft(self, depth, position, saved, now):
    cfg = _blend_cfg(self._soft_regime_weights(saved), SPOT_CFG)
    g = globals()
    old_l, old_s = g["VELVET_LONG_Z"], g["VELVET_SHORT_Z"]
    g["VELVET_LONG_Z"], g["VELVET_SHORT_Z"] = cfg.get("vel_long", old_l), cfg.get("vel_short", old_s)
    try:
        return _orig_trade_velvet_soft(self, depth, position, saved, now)
    finally:
        g["VELVET_LONG_Z"], g["VELVET_SHORT_Z"] = old_l, old_s
Trader._trade_velvet = _trade_velvet_soft

_orig_trade_hydrogel_soft = Trader._trade_hydrogel
def _trade_hydrogel_soft(self, depth, position, saved, now):
    cfg = _blend_cfg(self._soft_regime_weights(saved), SPOT_CFG)
    g = globals()
    old_l, old_s = g["HYDROGEL_LONG_Z"], g["HYDROGEL_SHORT_Z"]
    g["HYDROGEL_LONG_Z"], g["HYDROGEL_SHORT_Z"] = cfg.get("hyd_long", old_l), cfg.get("hyd_short", old_s)
    try:
        return _orig_trade_hydrogel_soft(self, depth, position, saved, now)
    finally:
        g["HYDROGEL_LONG_Z"], g["HYDROGEL_SHORT_Z"] = old_l, old_s
Trader._trade_hydrogel = _trade_hydrogel_soft

_orig_orders_for_voucher_soft = Trader._orders_for_voucher

_orig_orders_for_wing_soft = Trader._orders_for_wing
def _orders_for_wing_soft(self, prod, depth, position, z, cfg, saved, sbc, ssc, now):
    patch = _blend_cfg(self._soft_regime_weights(saved), WING_REGIME_CFG, prod)
    if patch:
        cfg = dict(cfg)
        cfg.update(patch)
    return _orig_orders_for_wing_soft(self, prod, depth, position, z, cfg, saved, sbc, ssc, now)
Trader._orders_for_wing = _orders_for_wing_soft

_orig_trade_velvet_spot_filter = Trader._trade_velvet
def _trade_velvet_spot_filter(self, depth, position, saved, now):
    orders = _orig_trade_velvet_spot_filter(self, depth, position, saved, now)
    bb = self._best_bid(depth)
    ba = self._best_ask(depth)
    out = []
    for o in orders:
        if o.quantity < 0 and bb is not None and o.price > bb:
            continue
        if o.quantity > 0 and ba is not None and o.price < ba:
            continue
        out.append(o)
    return out
Trader._trade_velvet = _trade_velvet_spot_filter

_orig_trade_hydrogel_spot_filter = Trader._trade_hydrogel
def _trade_hydrogel_spot_filter(self, depth, position, saved, now):
    orders = _orig_trade_hydrogel_spot_filter(self, depth, position, saved, now)
    bb = self._best_bid(depth)
    return [o for o in orders if not (o.quantity < 0 and bb is not None and o.price > bb)]
Trader._trade_hydrogel = _trade_hydrogel_spot_filter

def _dom_regime_state(self, saved):
    w = self._soft_regime_weights(saved)
    return max(w, key=w.get)

def _update_mid_min_z_state(saved, z):
    old = saved.get("mid_min_z_state")
    if not isinstance(old, (int, float)) or z < old:
        saved["mid_min_z_state"] = float(z)

def _maybe_set_mid_rebound_state(saved, z):
    mn = saved.get("mid_min_z_state")
    if isinstance(mn, (int, float)) and mn <= -3.4 and (z - mn) >= 0.8:
        saved["mid_post_dip_rebound_state"] = True

def _mid_rebound_state_active(saved, z):
    return bool(saved.get("mid_post_dip_rebound_state")) and z < 99.0

_orig_trade_velvet_state_notime = Trader._trade_velvet
def _trade_velvet_state_notime(self, depth, position, saved, now):
    bb = self._best_bid(depth)
    ba = self._best_ask(depth)
    if bb is not None and ba is not None:
        regime = _dom_regime_state(self, saved)
        z = self._velvet_z((bb + ba) / 2.0, saved)
        if regime == "mid":
            _update_mid_min_z_state(saved, z)
            _maybe_set_mid_rebound_state(saved, z)
            if _mid_rebound_state_active(saved, z):
                old_s = SPOT_CFG["mid"].get("vel_short")
                SPOT_CFG["mid"]["vel_short"] = 99.0
                try:
                    return _orig_trade_velvet_state_notime(self, depth, position, saved, now)
                finally:
                    SPOT_CFG["mid"]["vel_short"] = old_s
        elif regime == "low":
            old = saved.get("low_min_z_state")
            if not isinstance(old, (int, float)) or z < old:
                saved["low_min_z_state"] = float(z)
            mn = saved.get("low_min_z_state")
            if isinstance(mn, (int, float)) and mn <= -3.5 and z <= 0.3:
                old_l = SPOT_CFG["low"].get("vel_long")
                SPOT_CFG["low"]["vel_long"] = -0.5
                try:
                    return _orig_trade_velvet_state_notime(self, depth, position, saved, now)
                finally:
                    SPOT_CFG["low"]["vel_long"] = old_l
    return _orig_trade_velvet_state_notime(self, depth, position, saved, now)
Trader._trade_velvet = _trade_velvet_state_notime

def _orders_for_voucher_state_notime_hold_long(self, prod, depth, rsv, position, z, cfg, saved, sbc, ssc, now):
    patch = _blend_cfg(self._soft_regime_weights(saved), VOUCHER_REGIME_CFG, prod)
    if patch:
        cfg = dict(cfg)
        cfg.update(patch)

    if _dom_regime_state(self, saved) == "mid":
        _update_mid_min_z_state(saved, z)
        _maybe_set_mid_rebound_state(saved, z)
        if _mid_rebound_state_active(saved, z) and prod in ("VEV_4000", "VEV_4500", "VEV_5000", "VEV_5100", "VEV_5200", "VEV_5300"):
            cfg = dict(cfg)
            cfg["short_take_z"] = 99.0
            if position < 0 and prod in ("VEV_5000", "VEV_5100", "VEV_5200", "VEV_5300"):
                cfg["short_exit_z"] = 99.0
                cfg["exit_take_size"] = 300
                cfg["exit_sweep_levels"] = 3
            if position > 0 and prod in ("VEV_5000", "VEV_5100", "VEV_5200", "VEV_5300"):
                cfg["long_exit_z"] = 99.0
    return _orig_orders_for_voucher_soft(self, prod, depth, rsv, position, z, cfg, saved, sbc, ssc, now)
Trader._orders_for_voucher = _orders_for_voucher_state_notime_hold_long

HYDROGEL_MM_SIZE = 25
HYDROGEL_MM_MAX_POSITION = 200

_ORIG_HYDROGEL_ENTRY_AFTER_SOFT = _orig_trade_hydrogel_soft

def _hydrogel_high_open_sweep(self, depth, position, saved, now):
    if not depth.buy_orders or not depth.sell_orders:
        return []

    bb = max(depth.buy_orders)
    ba = min(depth.sell_orders)
    bvol = depth.buy_orders[bb]
    avol = -depth.sell_orders[ba]
    mid = (bb + ba) / 2.0
    z = (mid - HYDROGEL_ANCHOR) / HYDROGEL_SIGMA
    saved["hz"] = round(z, 4)

    m38 = self._recent(saved, "m38_ts", now, M38_ACTIVITY_TTL)
    short_z = HYDROGEL_SHORT_Z + (M38_HYDROGEL_Z_WIDEN if m38 else 0.0)
    long_z = HYDROGEL_LONG_Z - (M38_HYDROGEL_Z_WIDEN if m38 else 0.0)
    levels = 3 if self._soft_open_mid(saved) > 5280.0 else 1

    if z > short_z:
        if levels > 1:
            o = self._sweep_sell_order(HYDROGEL, depth, position + HYDROGEL_LIMIT, levels)
            if o:
                return [o]
        else:
            qty = min(position + HYDROGEL_LIMIT, bvol)
            if qty > 0:
                return [Order(HYDROGEL, bb, -qty)]

    if z < long_z:
        if levels > 1:
            o = self._sweep_buy_order(HYDROGEL, depth, HYDROGEL_LIMIT - position, levels)
            if o:
                return [o]
        else:
            qty = min(HYDROGEL_LIMIT - position, avol)
            if qty > 0:
                return [Order(HYDROGEL, ba, qty)]

    return _ORIG_HYDROGEL_ENTRY_AFTER_SOFT(self, depth, position, saved, now)

_orig_trade_hydrogel_soft = _hydrogel_high_open_sweep

EWMA_DEEP_LOW_OPEN_Z = -1.0
_ORIG_VELVET_ENTRY_AFTER_SOFT = _orig_trade_velvet_soft

def _deep_low_open_for_ewma(saved):
    open_mid = saved.get("open_mid", None)
    if not isinstance(open_mid, (int, float)):
        return False
    return (float(open_mid) - VELVET_ANCHOR) / VELVET_SIGMA < EWMA_DEEP_LOW_OPEN_Z

def _velvet_with_guarded_ewma_overlay(self, depth, position, saved, now):
    if not depth.buy_orders or not depth.sell_orders:
        return []

    bb = max(depth.buy_orders)
    ba = min(depth.sell_orders)
    mid = (bb + ba) / 2.0
    self._record_velvet_open(saved, mid)

    if _deep_low_open_for_ewma(saved):
        return _ORIG_VELVET_ENTRY_AFTER_SOFT(self, depth, position, saved, now)

    window = 500.0 if self._soft_open_mid(saved) < 5280.0 else 300.0
    alpha = 2.0 / (window + 1.0)
    old_mean = saved.get("vel_ewm", mid)
    old_var = saved.get("vel_ewv", 1.0)
    diff = mid - old_mean
    new_mean = old_mean + alpha * diff
    new_var = (1.0 - alpha) * (old_var + alpha * diff * diff)
    saved["vel_ewm"] = new_mean
    saved["vel_ewv"] = new_var

    local_z = (mid - new_mean) / max(1.0, math.sqrt(new_var))
    if local_z > 3.0:
        qty = min(position + VELVET_LIMIT, depth.buy_orders[bb])
        if qty > 0:
            return [Order(UNDERLYING, bb, -qty)]
    if local_z < -3.0:
        qty = min(VELVET_LIMIT - position, -depth.sell_orders[ba])
        if qty > 0:
            return [Order(UNDERLYING, ba, qty)]

    return _ORIG_VELVET_ENTRY_AFTER_SOFT(self, depth, position, saved, now)

_orig_trade_velvet_soft = _velvet_with_guarded_ewma_overlay
