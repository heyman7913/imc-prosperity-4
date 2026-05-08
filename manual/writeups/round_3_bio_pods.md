# Round 3 Manual - The Pretty Answer Was Not the Submission Answer

## The challenge

Round 3 gave us a two-bid auction for Bio-Pods.

Each counterparty had a reserve price drawn uniformly from the discrete grid `670, 675, ..., 920`, and every pod we bought would be sold the next day at `920`. We could submit two bids.

The rules made the round subtle:

- the first bid bought if it was strictly above the reserve
- if the first bid failed, the second bid could still buy
- but the second bid was benchmarked against the global average second bid
- if our second bid was at or below that average, the payoff on that second tranche got penalized by

```text
((920 - avg_b2) / (920 - b2))^3
```

So this was not just an EV problem. It was an EV problem with a crowd-sensitive landmine attached to the second bid.

## Our thinking process

We first solved the clean version with no penalty. That gave the textbook optimum:

```text
751 / 836
```

The notebook confirms it:

```text
no-penalty best: b1=751 b2=836 expected=84.3333
```

That result also taught us an important mechanical detail: because the reserve condition was strict inequality, integer bids one tick above reserve levels were usually best. Bidding exactly on the grid wasted value.

Then we moved to the real problem: what if the field average second bid drifted upward?

That produced a best-response map by average-second-bid regime, and that map is what actually mattered.

## What we thought of placing at first

Our first clean answer was the elegant one:

```text
Lowest bid 751
Highest bid 836
```

It was the highest no-penalty EV and looked mathematically complete.

But it was also fragile. If the average second bid landed even modestly above `836`, the second leg started getting punished. After Round 2, we were much less willing to trust a fragile optimum in a crowd-dependent setting.

## What we ended up placing

We chose the hedge:

```text
Lowest bid 756
Highest bid 846
```

The notebook supports that choice directly. Around the plausible average-second-bid region, the best-response shelves move like this:

```text
avg 837..841 -> 751 / 841
avg 842..846 -> 756 / 846
avg 847..851 -> 756 / 851
```

Our final pair sat in the middle of the regime we thought was most plausible. It gave up only a little clean EV while buying much better protection against the penalty.

## Where we did well

We did not get hypnotized by the prettiest no-penalty answer.

That was the main win. The notebook shows the cost of hedging was small:

- `751 / 836 -> 84.3333`
- `756 / 846 -> 84.0000`

Giving up `0.3333` of expected value per counterparty to protect against an average-bid cliff was a very reasonable trade.

## Why we lost ground

If we lost here, it was because our estimate of the crowd average was still too conservative.

The whole round lived or died on that forecast. If the field average second bid really drifted much higher, then stronger shading such as the `766 / 871` region would beat our hedge. The notebook says that clearly too. We just did not think the crowd would go that far.

So the mistake, if there was one, was not mathematical. It was a crowd prior.

## Why the winner was the winner

Based on what we traced, the winner won by getting the average-second-bid game more right than the rest of the field.

This round was never about finding the single best static bid pair in a vacuum. It was about asking where everyone else would anchor, then choosing the pair that survived that average. The winner either forecast a higher field average than we did, or was simply more willing to pay up for protection.

For us, the lasting lesson was simple: in manual rounds with a crowd-triggered penalty, the robust answer is often better than the clean answer.
