# Round 2 Manual - The Math Was Easy, The Crowd Was Not

## The challenge

Round 2 asked us to split budget across `Research`, `Scale`, and `Speed`.

The first two were straightforward optimization variables:

```text
PnL = Research(x) * Scale(y) * SpeedMultiplier(z) - BudgetUsed
Research(x) = 200000 * log(1 + x) / log(101)
Scale(y) = 7y / 100
BudgetUsed = 500 * (x + y + z)
```

If Speed had been just another smooth multiplier, this would have been a standard optimization round.

But Speed was different. It was ranked against the rest of the field. So this round was really two problems at once:

1. Solve the deterministic split between Research and Scale.
2. Guess where the crowd would cluster on Speed.

The first part was math. The second part was game theory.

## Our thinking process

We first solved the deterministic frontier exactly for every possible Speed value. That gave us the best Research and Scale split conditional on each `z`.

The notebook shows the clean frontier:

- `Speed 0 -> Research 23, Scale 77, pre-speed gross 742,329.92`
- `Speed 13 -> Research 21, Scale 66, pre-speed gross 618,862.11`
- `Speed 42 -> Research 15, Scale 43, pre-speed gross 361,658.68`

Pure math strongly preferred lower Speed because it preserved more budget for productive spend.

That was the trap.

## What we thought of placing at first

Our first serious instinct was a low-to-mid Speed submission, especially something in the shape of:

```text
Research 21%, Scale 66%, Speed 13%
```

It looked beautiful on the deterministic frontier. If the Speed multiplier stayed healthy, this kind of book crushed the higher-Speed allocations.

But that logic assumed the crowd would not pile into higher focal-point values and push low-Speed choices into weak rank buckets. That was the part we underweighted at first.

## What we ended up placing

The corrected crowd-aware submission was:

```text
Research 15%, Scale 43%, Speed 42%
```

That is also what the notebook defends.

The key stress test from the notebook was this:

```text
Speed 42 at top multiplier beats Speed 13 whenever multiplier_13 < 0.526
Speed 13 at m=0.5 -> 259,431.06
Speed 42 at m=0.9 -> 275,492.82
```

That threshold made the whole round click for us. The low-Speed solution only wins if the crowd gives it enough rank protection. Once that disappears, the "inferior" deterministic answer becomes the better tournament answer.

## Where we did well

We eventually separated the round into the two right layers:

- optimize Research and Scale exactly
- treat Speed as a focal-point crowding game

That shift mattered. It stopped us from blindly trusting the prettiest spreadsheet row.

The notebook keeps both the tempting model and the corrected one on purpose, because the wrong model is exactly how strong teams get trapped in rounds like this.

## Why we lost ground

If we lost here, it was because we got to the crowding-game view later than we should have.

The low-Speed book was mathematically elegant, but too optimistic about field behavior. We should have started with the question "where will strong teams anchor?" before admiring the deterministic frontier.

In other words, we solved the calculator before we solved the tournament.

## Why the winner was the winner

Based on what we traced, the winner understood earlier than we did that ranked variables should not be treated like productive variables.

The winning shape here was not "maximize formula output." It was "buy enough rank insurance to survive the crowd." A focal Speed value in the stronger bucket, like the `42` shape we ended up respecting, makes much more sense once you think in Keynesian beauty contest terms instead of spreadsheet terms.

That was the lesson we carried forward: when the round has a crowd-dependent payoff, solve the crowd first and the clean math second.
