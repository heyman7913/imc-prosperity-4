# Pre-Round 1 Learnings — IMC Prosperity 4

A reference covering core trading strategies, signal interpretation, and implementation patterns for IMC Prosperity. The goal isn't just what to do but why it works.

## Table of Contents

1. [Market Making](#1-market-making)
2. [End-of-Game PnL and Mark-to-Market](#2-end-of-game-pnl-and-mark-to-market)
3. [Order Tracking and Position Limits](#3-order-tracking-and-position-limits)
4. [Moving Fair Value Assets](#4-moving-fair-value-assets)
5. [Volatile and Mean-Reverting Assets](#5-volatile-and-mean-reverting-assets)
6. [Position Limit Considerations Across Products](#6-position-limit-considerations-across-products)
7. [Signals from trades.csv](#7-signals-from-tradescsv)
8. [Baskets and Relative Value Trading](#8-baskets-and-relative-value-trading)
9. [Z-Score Trading](#9-z-score-trading)
10. [Options](#10-options)
11. [Location Arbitrage](#11-location-arbitrage)
12. [Traders Round](#12-traders-round)

## 1. Market Making

Market making means simultaneously quoting a bid and an ask and profiting from the spread. You are not predicting direction. You are providing liquidity and capturing the difference between what buyers pay and what sellers receive.

### 1.1 Penny Jumping

Always be the best bid and best ask by quoting one tick tighter than the current top of book.

- Post bid at `best_bid + 1`
- Post ask at `best_ask - 1`

You are first in queue at the most competitive price. Trades route to you before anyone else. In Prosperity you are trading against deterministic bots that clear mechanically without discretion, so consistent queue priority translates directly into fills.

This works best on thick order books with large resting orders. Jumping one tick ahead of a large order gives you priority without meaningfully moving the market.

It breaks down on thin order books. If the best bid is a tiny order and you penny-jump it, you may inadvertently become the price setter and expose yourself to adverse selection. The market moves to you faster than you can react.

Against bots specifically, penny jumping is almost always correct. Bots do not adjust based on who is quoting, they simply take the best available price.

### 1.2 De-Risk Near Fair Value

When the market price is near your fair value estimate, reduce or flatten your position. Fair value is your anchor. When the market is near that anchor, your edge from directional exposure is minimal. Holding inventory when you have no directional conviction is pure risk, exposure to adverse moves with no compensating expected return.

In practice, define a band around fair value (e.g., ±1 tick). Within that band, either stop adding to your position or actively unwind existing inventory.

### 1.3 Take Mispriced Quotes

If you know the fair price is stable and you see a bid below fair or an ask above fair, take it immediately. This is pure arbitrage against a bot quoting incorrectly. The bot is not updating its quotes based on your fair value model, it is following a fixed rule. You are exploiting the gap between its rule and reality.

Only applies when fair value is genuinely stable. If you are uncertain about fair value, aggressive taking is risky.

### 1.4 Multi-Turn Thinking

Consider not just the current timestamp but what order flow you expect in the next turn.

Example: you are net long, there is a seller in the market right now at a low price, and you expect a buyer to arrive next turn.

The naive move is to sell your inventory now to reduce risk. The better move is to buy from the current seller at their low price (lowering your average entry cost), then post a higher sell to capture the incoming buyer's demand next turn. You end up selling at a higher net price than you would have by selling immediately, because you used the cheap seller to lower your cost basis before selling into demand.

When order flow is predictable, waiting one turn can improve expected execution significantly. Time entries and exits around anticipated flow, not just current state.

## 2. End-of-Game PnL and Mark-to-Market

At the end of each round, your unrealized position is marked to market. The exchange assigns a value to whatever inventory you are holding, even if you never traded it.

Your **realized PnL** comes from completed trades throughout the round. Your **unrealized PnL** comes from your remaining position, valued at the end-of-round mid price.

```
mid_price = (worst_bid + worst_ask) / 2
```

Note: this definition uses the *worst* bid and *worst* ask (the least competitive quotes on each side), not the best. This is counterintuitive and worth verifying against each round's specific rules.

If you end a round long and the mid price is above your average buy price, you profit on the remaining position even without selling. If mid price is below your buy cost, you take a loss. Your position at round end is not free to carry. Manage it actively toward the end of a round, or make sure your fair value model correctly anticipates where end-of-round mid will land.

## 3. Order Tracking and Position Limits

The exchange does not enforce pre-trade risk for you. Multiple orders can be submitted per timestamp, and the exchange only updates state at the end of each timestamp. This means you can accidentally submit more buy orders than your remaining capacity allows, and orders that breach your position limit will be rejected, sometimes silently.

Before submitting any new order, compute remaining capacity:

```python
remaining_buy_capacity  = position_limit - current_position - sum(outstanding_buy_orders)
remaining_sell_capacity = position_limit + current_position - sum(outstanding_sell_orders)
```

Only submit orders up to this capacity.

**Keep buy and sell logic separate.** Mixing them in a single block makes it easy to accidentally double-count or miscalculate net capacity.

**Separate logic per product.** Each product has its own position limit, order book, and fair value. Interleaving them in a single function is a common source of bugs.

**Keep `run()` clean.** Your top-level function should be a simple dispatcher:

```python
def run(state: TradingState):
    orders = {}
    orders[PRODUCT_A] = trade_product_a(state)
    orders[PRODUCT_B] = trade_product_b(state)
    return orders
```

If Product B is broken, you can comment it out in one line without touching Product A.

## 4. Moving Fair Value Assets

Some assets do not have a stable, externally known fair value. The price genuinely moves over time, and your job is to maintain an accurate internal estimate of where fair value currently is, then market-make around it.

Your quote midpoint tracks your internal fair value rather than a fixed reference:

```
bid = my_fair_value - half_spread
ask = my_fair_value + half_spread
```

Use rolling windows of recent prices to compute a smoothed fair value:

```python
fair_value = mean(last_N_mid_prices)
```

Common window sizes: 5, 10, 20 timestamps. Shorter windows are more responsive but noisier. Longer windows are smoother but lag behind genuine trends. Compute fair value at multiple window sizes and use the one that minimizes realized slippage empirically.

The current mid includes noise from large orders, transient imbalances, and informed trades. A rolling average filters that out and gives you a more stable anchor for quoting.

## 5. Volatile and Mean-Reverting Assets

Pure market making breaks down when volatility is high. MM generates many small wins and occasionally one large loss. If volatility is high enough, the large losses dominate.

### Trend Detection with EMA / SMA

Even mean-reverting assets have short-term trends. Use exponential or simple moving averages to identify the current drift direction and avoid trading against it.

- **SMA:** Equal weight to all points in the window. Stable but slow.
- **EMA:** More weight on recent prices. Faster to respond to new information.

If EMA is trending down, be more conservative on the long side. If EMA is trending up, be more conservative on the short side.

### Spike Detection and Mean Reversion

Many assets in Prosperity exhibit sudden sharp moves that immediately reverse, likely a characteristic of the bot-generated order flow. Track the rolling mean and standard deviation of price. A spike is a price that deviates from the mean by more than a threshold (e.g., 2-3 standard deviations). Trade the reversal:

- Price spikes up: short aggressively, expect reversion.
- Price spikes down: buy aggressively, expect reversion.

### Volatility Measurement

$$\sigma = \text{std}(r_t) \times \sqrt{T}$$

Where $r_t = \ln(P_t / P_{t-1})$ is the log return at each timestamp and $T$ is the number of timestamps in a period. Higher volatility means wider spread required to compensate for inventory risk. Quote wider or reduce size accordingly.

### Testing for Mean Reversion: ADF Test

The Augmented Dickey-Fuller test checks whether a time series has a unit root (random walk) or is stationary (mean-reverting).

```python
from statsmodels.tsa.stattools import adfuller
result = adfuller(price_series)
p_value = result[1]
```

Low p-value (< 0.05): reject the null hypothesis of a unit root, the series is statistically mean-reverting. High p-value: cannot reject the unit root, behaves more like a random walk, mean-reversion strategies are less reliable.

Run this on historical price data before committing to a mean-reversion strategy on any asset.

## 6. Position Limit Considerations Across Products

When trading related instruments (e.g., an option and its underlying), check whether their position limits differ. The binding constraint determines your actual edge. A strategy that looks profitable in isolation may be severely reduced once you account for the tighter limit on one leg.

Example: if the underlying has a limit of 300 and the option has a limit of 200, going long 200 on the option and trying to fully delta-hedge may run into capacity constraints on the underlying depending on your existing position.

Map out position limits for every product you trade. For any multi-leg strategy, compute the maximum achievable position on each leg simultaneously. Design around the tightest constraint.

## 7. Signals from `trades.csv`

The historical trade file reveals who is trading, when, and at what prices. That exposes bot behavior and potentially informed trader activity.

**Repeating patterns:** Bots in Prosperity often trade with a fixed logic based on rolling windows of a specific size. Identifying the window size lets you predict the bot's next action.

**Trades at unexpected prices:** If a participant is consistently buying above fair value or selling below it, they are either a badly calibrated bot (exploitable) or an informed trader who knows something about future price movement. Informed traders have alpha. If you see persistent flow in one direction at an unusual price, consider whether you should be trading in the same direction rather than against it.

Practical workflow:
1. Plot trade prices over time alongside your fair value estimate.
2. Flag trades that deviate significantly from fair value.
3. Check whether those deviations are followed by price moves in the same direction.
4. If yes, there is an informed signal. Incorporate it into your model.

## 8. Baskets and Relative Value Trading

A basket is a synthetic instrument composed of a weighted combination of underlying assets. Prosperity often includes both the basket and its components as tradeable instruments.

```
Premium = Market Price of Basket - Theoretical Price of Basket
Theoretical Price = weighted sum of component prices
```

Plot both on the same chart. You are not exposed to the basket's absolute level, you are exposed to the premium: the gap between what the basket trades at and what its components imply it should trade at.

| Strategy | What You Are Doing |
|---|---|
| Basket vs. components | Long/short the premium directly |
| Premium of individual components | Trade mispricings within the basket |
| Difference between two baskets | Trade relative premium spread |
| Underlying assets directly | Directional on individual components |

Only be exposed to what you intend to be exposed to. If you want to trade the premium, go long basket and short equivalent component weights (or vice versa). Your net directional exposure should be zero, only the spread remains.

If you have remaining position limit after sizing your primary strategy, use it to market-make. Basket-component spreads are often wide, and quoting both sides with a tight spread generates consistent edge at very low risk. Do not leave position capacity unused.

**Frankfurt Hedgehogs approach:** They tracked the highest and lowest observed trade prices for each component and used them as dynamic bounds for z-score calculations, rather than fixed sigma multiples. They also watched for informed trader signals within individual basket components, since a component trading aggressively in one direction can signal a future move in the basket itself.

## 9. Z-Score Trading

Z-score trading is a mean-reversion framework applicable to individual assets, baskets, or spreads between two correlated instruments.

Maintain a running estimate of the mean and standard deviation of the asset's price or spread:

$$z = \frac{x - \mu}{\sigma}$$

| Z-Score | Action |
|---|---|
| $z < -2$ | Buy (price is abnormally below mean, expect reversion upward) |
| $z > +2$ | Sell (price is abnormally above mean, expect reversion downward) |
| $\|z\| < 0.5$ | Exit / flatten (price has reverted to mean) |

Thresholds of ±2 are starting points. Tune them per asset using historical backtests.

Use a rolling window for mean and standard deviation, not an expanding window, unless the asset is stationary over very long horizons. Confirm mean-reversion behavior with an ADF test before applying. Tighter thresholds (±1) generate more trades but each at lower confidence. Wider thresholds (±3) generate fewer trades but higher expected edge per trade.

## 10. Options

### Fundamentals

A call option gives the holder the right (but not the obligation) to buy the underlying at a fixed strike price at expiry.

```
profit = max(0, market_price - strike_price) - premium_paid
```

You profit when the market price at expiry exceeds `strike + premium`.

Key inputs: strike price, current underlying price, time to expiry, implied volatility, realized volatility.

### The Greeks

| Greek | Measures | Detail |
|---|---|---|
| **Delta** | Sensitivity to underlying price | Delta of 0.5 means the option gains $0.50 for every $1 gain in the underlying. Ranges from 0 to 1 for calls. |
| **Gamma** | Rate of change of Delta | How fast Delta changes as the underlying moves. High near expiry — small underlying moves cause large Delta changes. |
| **Theta** | Time decay | How much value the option loses per unit of time. Options expire worthless if not in the money — Theta represents that erosion. |
| **Vega** | Sensitivity to implied volatility | How much option price changes for a 1% change in IV. High Vega means option price is very sensitive to volatility regime changes. |

### Implied Volatility and the Volatility Smile

Implied volatility (IV) is the volatility value that, when plugged into Black-Scholes, reproduces the observed market price of the option.

```
market_price -> Black-Scholes -> solve for sigma -> this is IV
```

In theory, IV should be constant across strikes. In practice it forms a smile or skew shape — options far in or out of the money often carry higher IV than at-the-money options. This shape reveals relative mispricings between options.

Practical use: back out IV from market price, compare across strikes to find the option that looks cheapest relative to its neighbors on the smile, use IV to reprice, then market-make around that fair price.

### Options Strategies

**Delta hedging:** Sum the deltas of all options in your portfolio to get total directional exposure. Take an offsetting position in the underlying to neutralize it.

```python
total_delta = sum(option_delta_i * quantity_i)
hedge_quantity = -total_delta  # in underlying units
```

**IV scalping + mean reversion (Frankfurt Hedgehogs approach):** Implied volatility tends to mean-revert. After a spike it tends to fall back toward its long-run average. When IV spikes well above its historical mean, sell options (collect premium, short Vega). When IV is depressed, buy options (long Vega, expecting reversion upward).

**Theta decay harvesting:** Sell options when Theta is high (near expiry, ATM) and collect the time value that erodes daily.

**Gamma scalping:** Buy options (long Gamma), then delta-hedge dynamically. Profit from large realized moves in the underlying that exceed what IV priced in.

Pick the Greek with the clearest signal and most predictable behavior for each asset.

## 11. Location Arbitrage

In some rounds, the same good can be obtained in one location and sold in another. Profit comes from the price differential minus all friction costs.

```
break_even_price = cost_of_good + storage_cost + transport_cost + fees
```

You only trade if `selling_price > break_even_price`.

**Prosperity 3 reference:** There is a hidden aggressive buyer who will always purchase at the right price regardless of quantity. Use a stockpile strategy: accumulate inventory so you can offer goods on every turn, eliminating idle turns where you are waiting for supply. Continuous trading is more profitable than batch trading.

Do not just price at break-even. Model the probability of fill as a function of price:

```
expected_profit = fill_probability(price) * margin(price)
```

Lower price means higher fill probability but lower margin per unit. Higher price means lower fill probability but higher margin. The optimal price is not necessarily the lowest valid price — it depends on the fill probability curve.

## 12. Traders Round

In the Traders Round you are competing against other participants rather than just bots.

| Type | Behavior | How to Think About Them |
|---|---|---|
| **Retail traders** | Noise traders, no edge | Provide liquidity to you as a market maker |
| **Market makers** | Quote both sides, profit from spread | Compete with you for queue position |
| **Informed traders** | Trade based on private information | Signal future price direction, watch their flow |

**Copy trading** (mimicking another trader's orders) is a taker strategy. You are reacting to someone else's orders and taking liquidity. It works when you believe the person you are copying has alpha.

**Market making** is a maker strategy. You post orders and wait for others to take them. You profit from the spread and order flow, not directional prediction.

These two make money in fundamentally different ways. You cannot copy a market maker's behavior and expect the same result. Their edge comes from being the liquidity provider, not the taker. Know which regime you are operating in before choosing your approach.

*Pre-Round 1 preparation notes, IMC Prosperity 4, 2026.*