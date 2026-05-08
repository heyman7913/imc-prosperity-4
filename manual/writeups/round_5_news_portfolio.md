# Round 5 Manual - Read the News, Then Size Like an Adult

## The challenge

Round 5 looked subjective on the surface because it was built around news articles. But the sizing rule underneath was very clean.

For a product with expected one-day move `m` and allocation `p`, the approximate net PnL was:

```text
PnL = 1,000,000 * (m * p/100 - (p/100)^2)
```

That means the unconstrained one-name optimum is:

```text
p* = 50 * abs(m)
```

So the round had two parts:

1. turn each article into a direction and a move prior
2. size positions while respecting the quadratic fee

The first part was judgment. The second part was math.

## Our thinking process

We read each article in layers:

- direct fundamental effect
- second-order or narrative effect
- how much might already be priced
- whether the crowd would probably overreact in the same direction

That gave us a ranked board of longs and shorts, not just a pile of headlines.

The notebook then converted those priors into allocations and compared multiple candidate books under simulation. That was important because being right on direction but too aggressive on size can still lower expected value once the quadratic fee bites.

## What we thought of placing at first

Our first impulse was a hotter book. The strongest negative stories were very tempting:

- Lava cake
- Ashes of the Phoenix
- Pyroflex cells

And the obvious positive stories were tempting too:

- Thermalite core
- Magma ink
- Volcanic incense

That kind of "rank the stories and swing hard" portfolio was not crazy. The notebook keeps a `rank1-shot` style comparison for exactly that reason. But it was too eager in names where the fee curve started punishing extra size.

## What we ended up placing

We chose this final book:

```text
Obsidian cutlery: SELL 5%
Pyroflex cells: SELL 9%
Thermalite core: BUY 7%
Lava cake: SELL 22%
Magma ink: BUY 5%
Scoria paste: BUY 4%
Ashes of the Phoenix: SELL 12%
Volcanic incense: BUY 5%
Sulfur reactor: BUY 5%
```

Total capital used was `74%`.

The notebook matches this book and gives the rough expected contribution table:

- Obsidian `-10.8%`, alloc `5%`
- Pyroflex `-18.7%`, alloc `9%`
- Thermalite `+14.5%`, alloc `7%`
- Lava `-45.2%`, alloc `22%`
- Magma `+10.6%`, alloc `5%`
- Scoria `+8.1%`, alloc `4%`
- Ashes `-24.2%`, alloc `12%`
- Volcanic `+10.2%`, alloc `5%`
- Sulfur `+10.2%`, alloc `5%`

The notebook also shows the final book had the best simulated mean and median among the candidate portfolios it compares:

```text
MC mean / median -> mean 93,314.9, median 93,426.8
rank1-shot       -> mean 93,065.4, median 93,157.3
```

## Where we did well

We did not confuse confidence in direction with permission to oversize.

That was the main win in this round. We let Lava be the biggest short because it deserved it, but we still kept most other names moderate because fees matter and article interpretation is noisy.

Leaving budget unused was part of that discipline, not a mistake. Once the fee curve turns against you, forced capital deployment is worse than sitting on cash.

## Why we lost ground

If we lost here, it was because one of two things happened:

- our move priors were off, especially on the more narrative names
- the crowd and realized returns leaned harder into momentum than our more measured sizing assumed

This round is where storytelling and market psychology interact most. A team that read the crowd better could beat a team with similar directional calls just by pressing the right names harder.

## Why the winner was the winner

Based on what we traced, the winner likely did not win by spraying capital everywhere. The winner probably identified the few stories that would both move fundamentally and attract the field, then sized those names aggressively while avoiding fee drag on the weaker ideas.

That is the Round 5 lesson we would give to future teams: read the article, read the crowd, then let the notebook tell you when to stop sizing.
