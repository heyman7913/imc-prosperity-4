# VELVETFRUIT_EXTRACT + HYDROGEL_PACK: z-score mean-reversion MM
# VEV_4000-5500: European call options priced via Black-Scholes with implied TTE
# Portfolio delta hedged across all vouchers; zero-lottery bids on deep OTM strikes

import json
import math
from typing import Dict, List, Optional, Tuple

from datamodel import Order, OrderDepth, TradingState

VELVET = "VELVETFRUIT_EXTRACT"
UNDERLYING = VELVET
HYDROGEL = "HYDROGEL_PACK"

POSITION_LIMIT = 300
SHARED_DELTA_LIMIT = 1800  # total delta budget ~= 6 ATM calls; keeps aggregate directional exposure bounded
VELVET_ANCHOR = 5250       # conservative estimate; VELVET spent more time above this than below
VELVET_SIGMA = 12          # fitted OU sigma; gives half-life of ~15 ticks
YEAR_DAYS = 252.0
MARKET_SIGMA = 0.20        # flat vol assumption; empirical IV was close to 20% ATM

ZERO_LOTTERY_STRIKE_CUTOFF = 6000  # >6000 requires ~15% move in 5 days; free to bid at 0

VOUCHERS = {
    "VEV_4000": {
        "strike": 4000,
        "take_edge": 2,
        "quote_edge": 0,
        "inventory_skew": 4.0,
        "take_size": 16,
        "exit_take_size": 40,
        "exit_sweep_levels": 2,
        "quote_size": 150,
        "long_take_z": -1.6,
        "short_take_z": 2.1,
        "long_exit_z": 2.0,
        "short_exit_z": -0.6,
    },

    "VEV_4500": {
        "strike": 4500,
        "take_edge": 2,
        "quote_edge": 0,
        "inventory_skew": 4.0,
        "take_size": 40,
        "exit_take_size": 80,
        "exit_sweep_levels": 1,
        "quote_size": 100,
        "long_take_z": -1.5,
        "short_take_z": 2.0,
        "long_exit_z": 0.7,
        "short_exit_z": -0.7,
    },

    "VEV_5000": {
        "strike": 5000,
        "take_edge": 2,
        "quote_edge": 1,
        "inventory_skew": 4.0,
        "take_size": 40,
        "exit_take_size": 80,
        "exit_sweep_levels": 1,
        "quote_size": 70,
        "long_take_z": -1.5,
        "short_take_z": 2.0,
        "long_exit_z": 0.7,
        "short_exit_z": -0.7,
        "fair_mode": "bs",
    },

    "VEV_5100": {
        "strike": 5100,
        "take_edge": 6,
        "quote_edge": 3,
        "inventory_skew": 4.0,
        "take_size": 20,
        "exit_take_size": 80,
        "exit_sweep_levels": 1,
        "quote_size": 20,
        "long_take_z": -1.5,
        "short_take_z": 2.0,
        "long_exit_z": 0.7,
        "short_exit_z": -0.7,
        "fair_mode": "bs",
    },

    "VEV_5200": {
        "strike": 5200,
        "take_edge": 8,
        "quote_edge": 99,
        "inventory_skew": 4.0,
        "take_size": 60,
        "exit_take_size": 60,
        "exit_sweep_levels": 1,
        "quote_size": 0,
        "long_take_z": -1.5,
        "short_take_z": 2.0,
        "long_exit_z": 0.7,
        "short_exit_z": -0.7,
        "fair_mode": "bs",
    },

    "VEV_5300": {
        "strike": 5300,
        "take_edge": 8,
        "quote_edge": 99,
        "inventory_skew": 4.0,
        "take_size": 60,
        "exit_take_size": 60,
        "exit_sweep_levels": 1,
        "quote_size": 0,
        "long_take_z": -1.5,
        "short_take_z": 2.0,
        "long_exit_z": 0.7,
        "short_exit_z": -0.7,
        "fair_mode": "bs",
    },

    "VEV_5400": {
        "strike": 5400,
        "mode": "wing",
        "take_size": 6,
        "exit_take_size": 8,
        "long_take_z": -1.7,
        "short_take_z": 2.2,
        "long_exit_z": -0.4,
        "short_exit_z": 0.4,
        "long_entry_price": 15,
        "short_entry_price": 18,
        "long_exit_price": 18,
        "short_exit_price": 15,
    },

    "VEV_5500": {
        "strike": 5500,
        "mode": "wing",
        "take_size": 3,
        "exit_take_size": 5,
        "long_take_z": -1.9,
        "short_take_z": 2.3,
        "long_exit_z": -0.3,
        "short_exit_z": 0.3,
        "long_entry_price": 5,
        "short_entry_price": 7,
        "long_exit_price": 7,
        "short_exit_price": 5,
    },
}

class Trader:

    VELVET_LIMIT = 200
    SPOT_VELVET_ANCHOR = 5_250
    SPOT_VELVET_SIGMA = 12
    VELVET_SHORT_Z = 2.0
    VELVET_LONG_Z = -0.6
    VELVET_MM_SIZE = 20
    VELVET_MM_MAX_POSITION = 175
    VELVET_UNWIND_SIZE = 10

    HYDROGEL_LIMIT = 200
    HYDROGEL_ANCHOR = 9_991
    HYDROGEL_SIGMA = 25
    HYDROGEL_SHORT_Z = 1.3
    HYDROGEL_LONG_Z = -2.0
    HYDROGEL_MM_SIZE = 10
    HYDROGEL_MM_MAX_POSITION = 80
    HYDROGEL_UNWIND_SIZE = 15

    def run(self, state: TradingState):
        result: Dict[str, List[Order]] = {
            product: [] for product in state.order_depths
        }

        saved_state = self._decode_state(state.traderData)

        if VELVET in state.order_depths:
            result[VELVET] = self._trade_velvet(
                state.order_depths[VELVET],
                state.position.get(VELVET, 0),
                saved_state,
            )

        if HYDROGEL in state.order_depths:
            result[HYDROGEL] = self._trade_hydrogel(
                state.order_depths[HYDROGEL],
                state.position.get(HYDROGEL, 0),
                saved_state,
            )

        voucher_orders = self._trade_vouchers(state, saved_state)
        for product, orders in voucher_orders.items():
            if product not in result:
                result[product] = []
            result[product].extend(orders)

        for product in state.order_depths:
            if self._is_zero_lottery_voucher(product):
                result.setdefault(product, [])
                result[product].extend(
                    self._orders_for_zero_lottery(
                        product=product,
                        position=state.position.get(product, 0),
                    )
                )

        return result, 0, self._encode_state(saved_state)

    def _trade_velvet(
        self,
        depth: OrderDepth,
        position: int,
        saved_state: Dict,
    ) -> List[Order]:
        if not depth.buy_orders or not depth.sell_orders:
            return []

        best_bid = max(depth.buy_orders)
        best_ask = min(depth.sell_orders)

        bid_vol = depth.buy_orders[best_bid]
        ask_vol = -depth.sell_orders[best_ask]

        mid = (best_bid + best_ask) / 2.0
        z = (mid - self.SPOT_VELVET_ANCHOR) / self.SPOT_VELVET_SIGMA

        saved_state["vz"] = round(z, 4)

        orders: List[Order] = []

        if z > self.VELVET_SHORT_Z:
            qty = min(position + self.VELVET_LIMIT, bid_vol)

            if qty > 0:
                return [Order(VELVET, best_bid, -qty)]

        if z < self.VELVET_LONG_Z:
            qty = min(self.VELVET_LIMIT - position, ask_vol)

            if qty > 0:
                return [Order(VELVET, best_ask, qty)]

        passive_bid = best_bid + 1
        passive_ask = best_ask - 1

        if passive_bid >= passive_ask:
            return orders

        if abs(position) < self.VELVET_MM_MAX_POSITION:
            bid_size, ask_size = self._velvet_mm_sizes(position)

            bid_size = min(bid_size, self.VELVET_LIMIT - position)
            ask_size = min(ask_size, self.VELVET_LIMIT + position)

            if bid_size > 0:
                orders.append(Order(VELVET, passive_bid, bid_size))

            if ask_size > 0:
                orders.append(Order(VELVET, passive_ask, -ask_size))

        elif position >= self.VELVET_MM_MAX_POSITION and z > -0.5:
            qty = min(
                self.VELVET_UNWIND_SIZE,
                position,
                self.VELVET_LIMIT + position,
            )

            if qty > 0:
                orders.append(Order(VELVET, passive_ask, -qty))

        elif position <= -self.VELVET_MM_MAX_POSITION and z < 0.5:
            qty = min(
                self.VELVET_UNWIND_SIZE,
                -position,
                self.VELVET_LIMIT - position,
            )

            if qty > 0:
                orders.append(Order(VELVET, passive_bid, qty))

        return orders

    def _velvet_mm_sizes(self, position: int) -> Tuple[int, int]:
        pos_ratio = position / self.VELVET_LIMIT

        bid_adj = 1.0 - 0.3 * max(0.0, pos_ratio * 3.0)
        ask_adj = 1.0 - 0.3 * max(0.0, -pos_ratio * 3.0)

        bid_size = max(1, int(self.VELVET_MM_SIZE * max(0.0, bid_adj)))
        ask_size = max(1, int(self.VELVET_MM_SIZE * max(0.0, ask_adj)))

        return bid_size, ask_size

    def _trade_hydrogel(
        self,
        order_depth: OrderDepth,
        position: int,
        saved_state: Dict,
    ) -> List[Order]:
        if not order_depth.buy_orders or not order_depth.sell_orders:
            return []

        best_bid = max(order_depth.buy_orders)
        best_ask = min(order_depth.sell_orders)

        bid_volume = order_depth.buy_orders[best_bid]
        ask_volume = -order_depth.sell_orders[best_ask]

        mid = (best_bid + best_ask) / 2.0
        z_score = (mid - self.HYDROGEL_ANCHOR) / self.HYDROGEL_SIGMA

        saved_state["hz"] = round(z_score, 4)

        orders: List[Order] = []

        if z_score > self.HYDROGEL_SHORT_Z:
            target_position = -self.HYDROGEL_LIMIT
            quantity = min(position - target_position, bid_volume)

            if quantity > 0:
                return [Order(HYDROGEL, best_bid, -quantity)]

        if z_score < self.HYDROGEL_LONG_Z:
            target_position = self.HYDROGEL_LIMIT
            quantity = min(target_position - position, ask_volume)

            if quantity > 0:
                return [Order(HYDROGEL, best_ask, quantity)]

        passive_bid = best_bid + 1
        passive_ask = best_ask - 1

        if passive_bid >= passive_ask:
            return orders

        if abs(position) < self.HYDROGEL_MM_MAX_POSITION:
            bid_size, ask_size = self._hydrogel_passive_sizes(position)

            bid_size = min(bid_size, self.HYDROGEL_LIMIT - position)
            ask_size = min(ask_size, self.HYDROGEL_LIMIT + position)

            if bid_size > 0:
                orders.append(Order(HYDROGEL, passive_bid, bid_size))

            if ask_size > 0:
                orders.append(Order(HYDROGEL, passive_ask, -ask_size))

        elif position >= self.HYDROGEL_MM_MAX_POSITION and z_score > -0.5:
            quantity = min(
                self.HYDROGEL_UNWIND_SIZE,
                position,
                self.HYDROGEL_LIMIT + position,
            )

            if quantity > 0:
                orders.append(Order(HYDROGEL, passive_ask, -quantity))

        elif position <= -self.HYDROGEL_MM_MAX_POSITION and z_score < 0.5:
            quantity = min(
                self.HYDROGEL_UNWIND_SIZE,
                -position,
                self.HYDROGEL_LIMIT - position,
            )

            if quantity > 0:
                orders.append(Order(HYDROGEL, passive_bid, quantity))

        return orders

    def _hydrogel_passive_sizes(self, position: int) -> Tuple[int, int]:
        pos_ratio = position / self.HYDROGEL_LIMIT

        bid_adjustment = 1.0 - 0.3 * max(0.0, pos_ratio * 3.0)
        ask_adjustment = 1.0 - 0.3 * max(0.0, -pos_ratio * 3.0)

        bid_size = max(
            1,
            int(self.HYDROGEL_MM_SIZE * max(0.0, bid_adjustment)),
        )
        ask_size = max(
            1,
            int(self.HYDROGEL_MM_SIZE * max(0.0, ask_adjustment)),
        )

        return bid_size, ask_size

    def _trade_vouchers(
        self,
        state: TradingState,
        saved: Dict,
    ) -> Dict[str, List[Order]]:
        result: Dict[str, List[Order]] = {}

        underlying_mid = self._mid_price(state.order_depths.get(UNDERLYING))
        if underlying_mid is None:
            return result

        z = (underlying_mid - VELVET_ANCHOR) / VELVET_SIGMA
        tte = self._implied_tte(state, underlying_mid)

        deltas = {
            product: self._bs_delta(
                underlying_mid,
                cfg["strike"],
                tte,
                MARKET_SIGMA,
            )
            for product, cfg in VOUCHERS.items()
        }

        # Total delta exposure across all voucher positions.
        # We use this to cap how many more contracts we can buy/sell before hitting SHARED_DELTA_LIMIT.
        shared_delta = sum(
            state.position.get(product, 0) * deltas[product]
            for product in VOUCHERS
        )

        saved["opt_z"] = round(z, 4)
        saved["opt_tte"] = round(tte, 6)
        saved["opt_delta"] = round(shared_delta, 2)

        for product, cfg in VOUCHERS.items():
            depth = state.order_depths.get(product)
            if depth is None:
                continue

            strike = cfg["strike"]
            position = state.position.get(product, 0)

            raw_delta = deltas[product]
            delta_for_capacity = max(raw_delta, 0.01)

            # How many more contracts of this strike fit within the shared delta budget.
            # Floored at 0.01 to avoid division by zero on deep OTM strikes with near-zero delta.
            shared_buy_capacity = max(
                0,
                int((SHARED_DELTA_LIMIT - shared_delta) / delta_for_capacity),
            )
            shared_sell_capacity = max(
                0,
                int((SHARED_DELTA_LIMIT + shared_delta) / delta_for_capacity),
            )

            if cfg.get("mode") == "wing":
                orders = self._orders_for_wing(
                    product,
                    depth,
                    position,
                    z,
                    cfg,
                    saved,
                    shared_buy_capacity,
                    shared_sell_capacity,
                )
                result[product] = orders
                shared_delta += sum(order.quantity for order in orders) * raw_delta
                continue

            if cfg.get("fair_mode") == "bs":
                fair = self._bs_call(underlying_mid, strike, tte, MARKET_SIGMA)
            else:
                # Deep ITM options: fair = intrinsic value. Time value is negligible so BS isn't needed.
                fair = max(0.0, underlying_mid - strike)

            # Skew the fair value toward the opposite side of our current position.
            # If long, lower our bid/ask to encourage selling; if short, raise them.
            reservation = fair - cfg["inventory_skew"] * position / POSITION_LIMIT

            orders = self._orders_for_voucher(
                product=product,
                depth=depth,
                reservation=reservation,
                position=position,
                z=z,
                cfg=cfg,
                saved=saved,
                shared_buy_capacity=shared_buy_capacity,
                shared_sell_capacity=shared_sell_capacity,
            )

            result[product] = orders
            shared_delta += sum(order.quantity for order in orders) * raw_delta

        return result

    def _orders_for_voucher(
        self,
        product: str,
        depth: OrderDepth,
        reservation: float,
        position: int,
        z: float,
        cfg: Dict,
        saved: Dict,
        shared_buy_capacity: int,
        shared_sell_capacity: int,
    ) -> List[Order]:
        best_bid = self._best_bid(depth)
        best_ask = self._best_ask(depth)

        if best_bid is None or best_ask is None:
            return []

        buy_capacity = min(POSITION_LIMIT - position, shared_buy_capacity)
        sell_capacity = min(POSITION_LIMIT + position, shared_sell_capacity)

        long_exit_z = cfg.get("long_exit_z")
        short_exit_z = cfg.get("short_exit_z")
        long_take_z = cfg.get("long_take_z")
        short_take_z = cfg.get("short_take_z")

        # Sticky exit flags: once the exit condition triggers, we keep unwinding even if
        # z-score temporarily reverses. Prevents getting stuck in a losing position
        # because of a single tick of noise at the threshold.
        short_exit_key = product + "_exit_short"
        long_exit_key = product + "_exit_long"

        if position >= 0:
            saved[short_exit_key] = False

        if position <= 0:
            saved[long_exit_key] = False

        if short_exit_z is not None and position < 0 and z <= short_exit_z:
            saved[short_exit_key] = True

        if long_exit_z is not None and position > 0 and z >= long_exit_z:
            saved[long_exit_key] = True

        if saved.get(short_exit_key) and position < 0:
            order = self._sweep_buy_order(
                product,
                depth,
                min(
                    cfg.get("exit_take_size", cfg["take_size"]),
                    -position,
                    buy_capacity,
                ),
                cfg.get("exit_sweep_levels", 1),
            )
            if order is not None:
                return [order]

        if saved.get(long_exit_key) and position > 0:
            order = self._sweep_sell_order(
                product,
                depth,
                min(
                    cfg.get("exit_take_size", cfg["take_size"]),
                    position,
                    sell_capacity,
                ),
                cfg.get("exit_sweep_levels", 1),
            )
            if order is not None:
                return [order]

        if long_take_z is not None and buy_capacity > 0 and z < long_take_z:
            quantity = min(
                cfg["take_size"],
                buy_capacity,
                -depth.sell_orders[best_ask],
            )

            if quantity > 0:
                return [Order(product, best_ask, quantity)]

        if short_take_z is not None and sell_capacity > 0 and z > short_take_z:
            quantity = min(
                cfg["take_size"],
                sell_capacity,
                depth.buy_orders[best_bid],
            )
            if quantity > 0:
                return [Order(product, best_bid, -quantity)]

        orders: List[Order] = []
        buy_used = 0
        sell_used = 0

        if buy_capacity > 0 and best_ask <= reservation - cfg["take_edge"]:
            quantity = min(
                cfg["take_size"],
                buy_capacity,
                -depth.sell_orders[best_ask],
            )
            if quantity > 0:
                orders.append(Order(product, best_ask, quantity))
                buy_used += quantity

        if sell_capacity > 0 and best_bid >= reservation + cfg["take_edge"]:
            quantity = min(
                cfg["take_size"],
                sell_capacity,
                depth.buy_orders[best_bid],
            )
            if quantity > 0:
                orders.append(Order(product, best_bid, -quantity))
                sell_used += quantity

        quote_edge = cfg["quote_edge"]

        passive_bid = min(best_bid + 1, int(reservation - quote_edge))
        passive_ask = max(best_ask - 1, int(reservation + quote_edge + 0.999999))

        if passive_bid >= passive_ask:
            return orders

        quote_size = cfg["quote_size"]

        buy_quantity = min(
            self._passive_size(position, side=1, quote_size=quote_size),
            buy_capacity - buy_used,
        )

        if buy_quantity > 0 and passive_bid > 0:
            orders.append(Order(product, passive_bid, buy_quantity))

        sell_quantity = min(
            self._passive_size(position, side=-1, quote_size=quote_size),
            sell_capacity - sell_used,
        )

        if sell_quantity > 0:
            orders.append(Order(product, passive_ask, -sell_quantity))

        return orders

    def _orders_for_wing(
        self,
        product: str,
        depth: OrderDepth,
        position: int,
        z: float,
        cfg: Dict,
        saved: Dict,
        shared_buy_capacity: int,
        shared_sell_capacity: int,
    ) -> List[Order]:
        best_bid = self._best_bid(depth)
        best_ask = self._best_ask(depth)
        if best_bid is None or best_ask is None:
            return []

        buy_capacity = min(POSITION_LIMIT - position, shared_buy_capacity)
        sell_capacity = min(POSITION_LIMIT + position, shared_sell_capacity)

        short_exit_key = product + "_exit_short"
        long_exit_key = product + "_exit_long"

        if position >= 0:
            saved[short_exit_key] = False
        if position <= 0:
            saved[long_exit_key] = False

        if position < 0 and (z <= cfg["short_exit_z"] or best_ask <= cfg["short_exit_price"]):
            saved[short_exit_key] = True
        if position > 0 and (z >= cfg["long_exit_z"] or best_bid >= cfg["long_exit_price"]):
            saved[long_exit_key] = True

        if saved.get(short_exit_key) and position < 0:
            quantity = min(cfg["exit_take_size"], -position, buy_capacity, -depth.sell_orders[best_ask])
            if quantity > 0:
                return [Order(product, best_ask, quantity)]

        if saved.get(long_exit_key) and position > 0:
            quantity = min(cfg["exit_take_size"], position, sell_capacity, depth.buy_orders[best_bid])
            if quantity > 0:
                return [Order(product, best_bid, -quantity)]

        if buy_capacity > 0 and z < cfg["long_take_z"] and best_ask <= cfg["long_entry_price"]:
            quantity = min(cfg["take_size"], buy_capacity, -depth.sell_orders[best_ask])
            if quantity > 0:
                return [Order(product, best_ask, quantity)]

        if sell_capacity > 0 and z > cfg["short_take_z"] and best_bid >= cfg["short_entry_price"]:
            quantity = min(cfg["take_size"], sell_capacity, depth.buy_orders[best_bid])
            if quantity > 0:
                return [Order(product, best_bid, -quantity)]

        return []

    def _orders_for_zero_lottery(
        self,
        product: str,
        position: int,
    ) -> List[Order]:
        capacity = POSITION_LIMIT - position
        if capacity <= 0:
            return []
        return [Order(product, 0, capacity)]

    def _is_zero_lottery_voucher(self, product: str) -> bool:
        if not product.startswith("VEV_"):
            return False
        try:
            strike = int(product.split("_", 1)[1])
        except Exception:
            return False
        return strike >= ZERO_LOTTERY_STRIKE_CUTOFF

    def _sweep_buy_order(
        self,
        product: str,
        depth: OrderDepth,
        max_quantity: int,
        levels: int,
    ) -> Optional[Order]:
        if max_quantity <= 0:
            return None

        quantity = 0
        limit_price = None

        for price in sorted(depth.sell_orders)[:levels]:
            available = -depth.sell_orders[price]
            if available <= 0:
                continue
            take = min(max_quantity - quantity, available)
            if take <= 0:
                break
            quantity += take
            limit_price = price
            if quantity >= max_quantity:
                break

        if quantity <= 0 or limit_price is None:
            return None

        return Order(product, limit_price, quantity)

    def _sweep_sell_order(
        self,
        product: str,
        depth: OrderDepth,
        max_quantity: int,
        levels: int,
    ) -> Optional[Order]:
        if max_quantity <= 0:
            return None
        quantity = 0
        limit_price = None
        for price in sorted(depth.buy_orders, reverse=True)[:levels]:
            available = depth.buy_orders[price]
            if available <= 0:
                continue
            take = min(max_quantity - quantity, available)
            if take <= 0:
                break
            quantity += take
            limit_price = price
            if quantity >= max_quantity:
                break

        if quantity <= 0 or limit_price is None:
            return None

        return Order(product, limit_price, -quantity)

    def _passive_size(self, position: int, side: int, quote_size: int) -> int:
        if quote_size <= 0:
            return 0
        if side > 0:
            if position >= 180:
                return 0
            if position >= 90:
                return quote_size // 2
            return quote_size
        if position <= -180:
            return 0
        if position <= -90:
            return quote_size // 2

        return quote_size

    def _implied_tte(self, state: TradingState, underlying_mid: float) -> float:
        # Bootstrap TTE from near-ATM options where time value is meaningful.
        # We take the median across multiple strikes to reduce noise from any single illiquid level.
        estimates: List[float] = []

        for strike in [5000, 5100, 5200, 5300]:
            mid = self._mid_price(state.order_depths.get(f"VEV_{strike}"))
            if mid is None:
                continue
            estimate = self._solve_tte(underlying_mid, strike, mid)
            if estimate is not None:
                estimates.append(estimate)
        if not estimates:
            return 5.0 / YEAR_DAYS

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
            # No meaningful time value to invert - skip this strike.
            return None
        low = 0.25 / YEAR_DAYS
        high = 12.0 / YEAR_DAYS
        if self._bs_call(underlying, strike, high, MARKET_SIGMA) < option_mid:
            return None

        # Binary search on TTE: find T such that BS(T) = market_price.
        for _ in range(20):
            mid = (low + high) / 2.0
            if self._bs_call(underlying, strike, mid, MARKET_SIGMA) < option_mid:
                low = mid
            else:
                high = mid
        return (low + high) / 2.0

    def _bs_call(
        self,
        underlying: float,
        strike: int,
        tte: float,
        sigma: float,
    ) -> float:
        intrinsic = max(0.0, underlying - strike)
        if tte <= 0.0 or sigma <= 0.0:
            return intrinsic
        d1 = (
            math.log(underlying / strike) + 0.5 * sigma * sigma * tte
        ) / (sigma * math.sqrt(tte))
        d2 = d1 - sigma * math.sqrt(tte)
        return underlying * self._normal_cdf(d1) - strike * self._normal_cdf(d2)

    def _bs_delta(
        self,
        underlying: float,
        strike: int,
        tte: float,
        sigma: float,
    ) -> float:
        if tte <= 0.0 or sigma <= 0.0:
            return 1.0 if underlying > strike else 0.0
        d1 = (
            math.log(underlying / strike) + 0.5 * sigma * sigma * tte
        ) / (sigma * math.sqrt(tte))
        return self._normal_cdf(d1)

    def _normal_cdf(self, value: float) -> float:
        return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))

    def _decode_state(self, trader_data: str) -> Dict:
        if not trader_data:
            return {}

        try:
            decoded = json.loads(trader_data)
        except Exception:
            return {}

        return decoded if isinstance(decoded, dict) else {}

    def _encode_state(self, saved_state: Dict) -> str:
        return json.dumps(saved_state, separators=(",", ":"))[:3800]

    def _mid_price(self, depth: Optional[OrderDepth]) -> Optional[float]:
        if depth is None:
            return None
        best_bid = self._best_bid(depth)
        best_ask = self._best_ask(depth)

        if best_bid is None or best_ask is None:
            return None

        return (best_bid + best_ask) / 2.0

    def _best_bid(self, depth: OrderDepth) -> Optional[int]:
        return max(depth.buy_orders) if depth.buy_orders else None

    def _best_ask(self, depth: OrderDepth) -> Optional[int]:
        return min(depth.sell_orders) if depth.sell_orders else None
