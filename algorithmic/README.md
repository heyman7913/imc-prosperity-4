Here is everything our team did for the algorithmic round of IMC Prosperity 4.

This folder covers our round-by-round strategy work: the executed notebooks, the actual submitted traders, and the price/trade datasets given to us by IMC. By Round 5 we were trading 50 products simultaneously, so the complexity scaled fast. This document exists so future Prosperity teams can follow the progression and understand the reasoning behind each major design decision.

Each round has one notebook, a post-round replay on the official data that came back from IMC after submission.

## Rounds

| Round | Notebook | Submission | Products | Core Strategy |
| --- | --- | --- | --- | --- |
| 1 | [`round_1.ipynb`](notebooks/round_1.ipynb) | [`round_1_trader.py`](submissions/round_1/round_1_trader.py) | ASH_COATED_OSMIUM, INTARIAN_PEPPER_ROOT | Mean-reversion MM on ASH (fair value 10000). Pure long accumulator on PEPPER following confirmed linear drift. |
| 2 | [`round_2.ipynb`](notebooks/round_2.ipynb) | [`round_2_trader.py`](submissions/round_2/round_2_trader.py) | ASH_COATED_OSMIUM, INTARIAN_PEPPER_ROOT | Upgraded ASH to hybrid micro-price signal-take + passive MM (EMA + book imbalance). Stop-loss and re-anchoring added to PEPPER. |
| 3 | [`round_3.ipynb`](notebooks/round_3.ipynb) | [`round_3_trader.py`](submissions/round_3/round_3_trader.py) | VELVETFRUIT_EXTRACT, HYDROGEL_PACK, VEV_4000 to VEV_5500 | Z-score mean-reversion on spot products. Call options priced via Black-Scholes, portfolio delta hedged. Zero-lottery bids on deep OTM strikes. |
| 4 | [`round_4.ipynb`](notebooks/round_4.ipynb) | [`round_4_trader.py`](submissions/round_4/round_4_trader.py) | VELVETFRUIT_EXTRACT, HYDROGEL_PACK, VEV options | Named-participant signal tracking (6 bots). Soft regime detection from day-open price. EWMA reversion overlay and mid-dip state machine on VELVET. |
| 5 | [`round_5.ipynb`](notebooks/round_5.ipynb) | [`round_5_trader.py`](submissions/round_5/round_5_trader.py) | 50 products across 10 families | Cross-sectional OLS fair value per product. Per-product price-shock classifiers (revert or momentum). Unconditional drift targets for select products. |

## Datasets

Each round folder under `datasets/` contains the official price and trade CSVs released by IMC. These are the inputs to all executed notebooks. Day numbering follows IMC's convention as released.

## Synthetic Data

Starting from Round 4, we began generating synthetic price and trade data using an LLM to stress-test our algorithms before submission. The idea was straightforward: the official datasets only cover a narrow slice of market conditions, and a strategy that looks clean on those days can fall apart under different spread dynamics, volatility regimes, or bot behavior. Generating synthetic scenarios let us probe for those failure modes before they showed up in scoring.

This turned out to be one of the more useful things we added to our workflow. Running the trader against conditions it had not seen, rather than the same few official days repeatedly, gave a much more honest picture of robustness. We caught several parameter choices that were clearly overfit to Round 3 data before they made it into the Round 4 submission.

We are not sharing the synthetic datasets, but the approach is worth replicating. Prompt the LLM with the statistical properties of the official data (spread distributions, mid-price dynamics, order book depth patterns) and ask it to generate plausible variations. Run your trader against those. If performance degrades badly on modest condition shifts, that is a signal to revisit your parameter choices before you submit.

One thing that matters: always visually sanity check the synthetic data against the real data before running anything. LLMs will sometimes produce nonsensical price paths or trade sizes that look nothing like the official datasets. If the data is absurd, the stress test is worthless. A visualizer (like the one we made) that lets you compare both side by side makes this a two-second check.

## Getting Started

If you are new to algorithmic trading and want a solid foundation before diving into the code, start here. This walkthrough by [chrispyroberts](https://github.com/chrispyroberts) from CMU Physics, who finished 7th globally and 1st in the US in Prosperity 3, is one of the most honest and practical walkthroughs of a competition like this:

**[IMC Prosperity 3 Full Walkthrough by chrispyroberts](https://www.youtube.com/watch?v=PI2lJ063sJ8)**

We watched this video before Round 1 and it shaped how we thought about product structure and strategy design throughout the competition. The way he frames the problem of figuring out what a product is doing before deciding how to trade it is exactly the right mental model for Prosperity. Highly recommended as a first stop.

## Our Advice

Start by visualizing the data and getting a feel for what the product is actually doing. Plot the price, look at how it moves, and ask basic questions: does it drift, does it mean-revert, does it correlate with something else in the same round? The right strategy usually becomes obvious once you can see the structure. ASH was stationary with a hard anchor. PEPPER was linear drift. VELVET had regime shifts. Round 5 had tight intra-category correlations. None of that required sophisticated modeling once it was clear in a chart.

Fit your model before you write your trader. If the model does not fit the data, the trader will not work, and you will not know why until you are debugging a live submission. Backtest on all available days, not just the most recent one, because day-to-day variance in these competitions is high. A strategy that looks great on one day may be curve-fit to its random seed. Look for parameters that hold up consistently across all days, not ones that spike on one and fall apart on others.

Do not tune parameters by hand if you can avoid it. Pick a metric you care about, enumerate a grid of plausible values, and let the backtester run through them. Parameters chosen via grid search generalize better than ones picked by eye. We did too much manual tuning in early rounds and it cost hours per round that could have gone into better analysis.

Simple strategies beat complex ones when position limits are tight. With a limit of 10 per product in Round 5, the upside of a more accurate model is capped. The downside of overfitting is not. Our best positions were clean and short. Three interpretable signals with conservative sizing will usually outperform a twelve-factor model that looked great on training data.

Stability is the goal, not peak performance. When comparing two parameter sets with similar final PnL, take the one with lower drawdown and more consistent behavior across days. A strategy that is reliably decent across all conditions will outscore one that is spectacular on two days and broken on the third.
