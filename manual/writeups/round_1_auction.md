# Round 1 Manual - The Auction Was the Real Puzzle

## The challenge

Round 1 looked easy for about five minutes. The book showed offers below the Merchant Guild buyback price, so the first instinct was to sweep the cheap asks and lock in the spread. That was not the real problem. The real problem was the call auction rule.

Everything cleared at one price. The exchange picked the price with maximum traded volume, broke ties in favor of the higher price, and then used price-time priority. We also submitted last. That last detail mattered a lot. It meant a "cheap" order could still get zero fill if older bids were already ahead of us at the clearing price.

So this round was really about modeling the clearing mechanism, not just spotting visible arbitrage.

## Our thinking process

We started by writing the auction properly. For each candidate clearing price `c`, total executable demand was the existing bid volume at prices at least `c`, plus our own order if our limit price was at least `c`. Total executable supply was the existing ask volume at prices at most `c`. Traded volume was the minimum of the two.

Once we brute-forced the prices and quantities, the shape of the round became clear. The best order was never "buy all visible edge." The best order was "enter the queue just before the regime changes against us."

That is why this notebook searches integer quantities instead of stopping at obvious price levels.

## What we thought of placing at first

Our first instinct was the same one many teams probably had:

- `DRYLAND_FLAX`: buy the `40,000` offered at `28` and sell back at `30`
- `EMBER_MUSHROOM`: buy as much positive-edge supply as possible, especially the visible asks below the net buyback value

On paper that sounded great. In the actual mechanism it was fragile. In Flax, the old bids at `30`, `29`, and `28` were already enough to consume the available `28` supply before we ever got a turn. In Mushroom, a giant order pushed the clearing price up and left us behind older demand.

## What we ended up placing

We ended up with:

```text
DRYLAND_FLAX: BUY 9,999 @ 30
EMBER_MUSHROOM: BUY 19,999 @ 17
```

The notebook output matches this exactly:

- Best Flax order: `BUY 9,999 @ 30`, clearing at `29`, expected PnL `9,999`
- Best Mushroom order: `BUY 19,999 @ 17`, clearing at `16`, expected PnL `77,996.1`

## Where we did well

We understood the mechanism early enough to stop thinking like continuous-market traders.

That helped in two ways:

- In Flax, we accepted a worse clearing price because it bought us much more fill before the next jump.
- In Mushroom, we avoided the seductive oversized order that looked good on a screenshot of the book but performed much worse once queue priority and tie-breaking kicked in.

This was one of our cleaner manual rounds mathematically. The notebook and final answer line up well.

## Why we still could have lost

If we lost ground here, it would not have been because our arithmetic was wrong. It would have been because another team searched the same mechanism just as carefully and got to the same integer boundary logic.

This round did not reward vibes. It rewarded exact auction modeling.

Based on what we traced at the time, the strongest submissions were the ones that avoided the naive sweep and instead sat right below the clearing-price cliff, especially in Mushroom where oversizing destroyed a lot of value.

## Why the winner was the winner

Based on what we traced, the winning shape in Round 1 was simple: do not trade the visible book, trade the auction rule. The winner most likely understood that the edge lived at the quantity boundary where the clearing price changed, not at the posted ask itself.

That was the entire round.
