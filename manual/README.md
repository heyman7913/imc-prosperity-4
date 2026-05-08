Here is everything our team did for the manual round of IMC Prosperity 4.

This folder is meant to tell that story plainly for future Prosperity teams. IMC Prosperity 3 had already shown us that the manual side is never just "do the math" or just "read the room". The real game is usually both. So this folder keeps our actual round-by-round thinking, what we submitted, where we were sharp, and where we got punished.

## Rounds

| Round | Notebook | Writeup | What the task was |
| --- | --- | --- | --- |
| 1 | [`notebooks/round_1_manual_auction.ipynb`](notebooks/round_1_manual_auction.ipynb) | [`writeups/round_1_auction.md`](writeups/round_1_auction.md) | A one-shot auction where the whole game was understanding the clearing rule better than the crowd. |
| 2 | [`notebooks/round_2_invest_expand.ipynb`](notebooks/round_2_invest_expand.ipynb) | [`writeups/round_2_invest_expand.md`](writeups/round_2_invest_expand.md) | A budget allocation round where Research and Scale were math, but Speed was a crowd-ranking game. |
| 3 | [`notebooks/round_3_bio_pods.ipynb`](notebooks/round_3_bio_pods.ipynb) | [`writeups/round_3_bio_pods.md`](writeups/round_3_bio_pods.md) | A two-bid reserve-price auction with a nasty average-second-bid penalty. |
| 4 | [`notebooks/round_4_exotic_options.ipynb`](notebooks/round_4_exotic_options.ipynb) | [`writeups/round_4_exotic_options.md`](writeups/round_4_exotic_options.md) | A static options book where we had to price exotics correctly and only trade real mispricings. |
| 5 | [`notebooks/round_5_news_portfolio.ipynb`](notebooks/round_5_news_portfolio.ipynb) | [`writeups/round_5_news_portfolio.md`](writeups/round_5_news_portfolio.md) | A news-driven portfolio where the hard part was sizing under quadratic fees without getting baited by hype. |

## Our Advice

We understand AI is very powerful and is a good tool for deciding the manual trades. Even we used Claude and ChatGPT for figuring out our trades. But our suggestion would be to not use the LLMs as the final decisions. Always use `ipynb`s to do real math on the trades. And also try to use game theory or Keynesian beauty contest style thinking, understand Discord sentiment, predict where the crowd will herd, and then, based on the nature of the round, place your trades with or against the crowd.

One thing that is easy to overlook: always read the clearing rule carefully before forming any strategy. Every round has a mechanism, and the teams that do well are the ones who understood it precisely, not approximately. Round 1 was a clear example of this. Work through a simple numerical example before committing to any approach.
