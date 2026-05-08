# Round 4 Manual - Price the Product, Then Trade Only the Edge

## The challenge

Round 4 was a static options pricing round.

There was no crowd ranking, no intraday hedging decision, and no reason to get cute. We just had to value each listed product under the official model and then compare fair value to the executable bid or ask.

The official setup was:

```text
S0 = 50
volatility = 251%
risk-neutral drift = 0
4 steps per trading day
252 trading days per year
contract size = 3000
```

So the whole round reduced to:

```text
fair value > ask -> buy
fair value < bid -> sell
otherwise -> no trade
```

The hard part was not the rule. The hard part was pricing the exotics correctly under the exact rules IMC gave.

## Our thinking process

We split the products into natural buckets:

- vanilla calls and puts: Black-Scholes under the stated GBM
- binary put: risk-neutral terminal probability
- chooser: static decomposition
- knock-out put: discrete-step Monte Carlo, because monitoring was on the simulation grid

That separation cleaned the round up immediately. Once we stopped trying to force one mental model onto every product, the book became much more defensible.

## What we thought of placing at first

Our first instinct was slightly too eager. Like many teams, we were tempted to buy anything that looked cosmetically cheap.

That is dangerous in these rounds. A product can look exotic, complicated, and underloved and still be fairly priced or even expensive. The chooser was the best example of that. It felt like a product people would want to own. Under the actual decomposition, it was a sell.

## What we ended up placing

We ended up with this book:

```text
AC_60_C: SELL 50
AC_50_P_2: BUY 50
AC_50_C_2: BUY 50
AC_50_CO: SELL 50
AC_40_BP: SELL 50
AC_45_KO: BUY 500
```

Everything else was `no trade`.

The notebook outputs match that exactly and prices the key edges as:

- `AC_60_C` fair `8.7918` vs bid `8.80` -> small sell
- `AC_50_P_2` fair `9.8707` vs ask `9.75` -> buy
- `AC_50_C_2` fair `9.8707` vs ask `9.75` -> buy
- `AC_50_CO` fair `21.8977` vs bid `22.20` -> sell
- `AC_40_BP` fair `4.7679` vs bid `5.00` -> sell
- `AC_45_KO` fair `0.2056` vs ask `0.175` -> buy

Total expected PnL from the notebook is about `163,438.8`.

## Where we did well

We got the structure of the round right.

That showed up in three places:

- we did not force trades in products that were basically fair after spread
- we decomposed the chooser instead of treating it like a mystery box
- we valued the knock-out put on the discrete monitoring grid instead of using the wrong continuous-barrier shortcut

That last part mattered a lot. The knock-out was one of the cleaner edges in the round precisely because many people were likely to model it incorrectly.

## Why we lost ground

If we lost here, it was not because the framework was bad. It would have been because some teams estimated the exotics even more precisely, especially the barrier product, or were more confident about pushing size only where the edge was truly clean.

This round rewarded precision and discipline. It punished overtrading.

## Why the winner was the winner

Based on what we traced, the winner probably did two things better than average:

1. priced the exotics under the exact official mechanics, especially the chooser and knock-out
2. stayed disciplined enough to trade only the real mispricings

Round 4 did not need a heroic theory. It needed correct pricing and the patience to leave fair products alone. That is usually what wins these static mispricing rounds.
