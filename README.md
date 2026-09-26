# Premarket relative strength scanner

Ranks a watchlist by how each stock trades against SPY before the open and in the first minutes after it. The goal is to find the few names worth a small, defined-risk options position.

```
pip install -r requirements.txt
python premarket_rs.py                      # live scan (7:00–9:45 ET is the useful window)
python premarket_rs.py --watch 5 --news     # rescan every 5 min until 9:45, with headlines
python premarket_rs.py --date 2026-09-25 --asof 09:35   # replay a past morning
python premarket_rs.py --file watchlist.txt --csv out.csv
```

## Columns

| Column | Meaning |
|---|---|
| Gap% | Change from yesterday's close |
| RS% | Gap minus what the stock's beta to SPY predicts (beta is shrunk toward 1) |
| RSz | RS% divided by the stock's normal daily move, so a 1% excess move in WMT counts more than in TSLA |
| TrendNNm% | How much RS% changed over the last NN minutes (default 45). This catches the turn |
| Trendz | Trend scaled by the volatility expected over that window |
| Score | RSz + Trendz. The top is strength (calls); the bottom is weakness (puts) |
| Div | The stock and SPY moved in opposite directions during the trend window |
| PMHi/PMLo, PrevHi/PrevLo | Levels to trade against: premarket range and yesterday's range |
| FwdRS%, FwdHi%, FwdLo% | Replay only: what happened after the scan time (RS to the close, best and worst move) |
| News | With `--news`: headline count from the 18 hours before the scan, and the latest title |

## What the replays show

- **MSFT on 2026-09-25:** before the open it ranked only about 10th of 41. Its gap was small, and it was lagging SPY until about 8:30, then turned. At **9:35 it ranked #2** (+2.3% RS) and went on to gain another +1.7% at best. The catalyst plus the turn was the real signal.
- **Across 7 sessions (Sep 17–25):** the 9:29 premarket score did **not** predict the rest of the day. Premarket leaders often faded. The 9:35 score did better: the top 5 beat the bottom 5 on 5 of 7 days. A week of data proves nothing, so treat the score as a way to find candidates, not as a trade signal.

A workflow that fits: build the list premarket, and treat names with news as a separate group. Then trade only names that **confirm RS in the first 5–15 minutes** against a level (PMHi, PrevHi).

## Limits

- Yahoo reports zero volume for extended-hours bars, so there is no premarket relative-volume filter. A paid feed such as Polygon or Alpaca SIP would add it.
- 1-minute history goes back about 30 days, and each fetch covers the last 8 sessions.
- Yahoo news only keeps recent articles, so `--news` is only meaningful when scanning live.
- Quotes are free and can lag by a few seconds. This is not investment advice.
