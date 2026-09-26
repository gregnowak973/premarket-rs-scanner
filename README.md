# Premarket relative strength scanner

Ranks a watchlist by how each stock trades against SPY before the open and in the first minutes after it. The goal is to find the few names worth a small, defined-risk options position.

```
pip install -r requirements.txt
python premarket_rs.py                      # live scan (7:00–9:45 ET is the useful window)
python premarket_rs.py --watch 5 --news     # rescan every 5 min until 9:45, with headlines
python premarket_rs.py --date 2026-09-25 --asof 09:35   # replay a past morning
python premarket_rs.py --file watchlist.txt --csv out.csv   # same list the recorder uses
```

## Daily log and local website

`app.py` records the scan every trading morning into a local SQLite database (`data/scans.db`) and serves the history at http://127.0.0.1:8050.

**Setup (once):**
```
pip install -r requirements.txt
python app.py backfill    # rebuild the last ~30 days from Yahoo's 1-minute history (about a minute)
python app.py schedule    # run in the background from now on, then open the site
```

`schedule` sets up two things for your OS (macOS launchd, Windows Task Scheduler plus the Startup folder, Linux cron):
- **Recorder:** `python app.py record` runs every 5 minutes. On weekdays it takes snapshots at 08:00, 08:30, 09:00, 09:15, 09:29, 09:35, 09:45 and 10:00 ET, with headlines. Snapshot times are ET whatever your time zone.
- **Website:** starts when you log in and stays up at http://127.0.0.1:8050.

**Missed days are filled in automatically.** Each run checks the last ~30 days for any weekday that isn't recorded, whether the computer was off, asleep or offline. It rebuilds those days from Yahoo's 1-minute history, then adds rest-of-day outcomes once each session closes. Rebuilt days have prices and outcomes but no headlines, because Yahoo drops old news. Days older than ~30 days can't be recovered.

`python app.py unschedule` removes both jobs. Logs are in `data/record.log` and `data/site.log`. On macOS, keep the folder outside Documents/Desktop/Downloads, or allow Python access when macOS asks; otherwise the background job can't read it.

### If the website won't load

Run `python3 app.py status`. It shows whether the site is up, whether the background jobs are installed, and the latest log lines. Common causes on a Mac:
- **The folder is in Documents, Desktop, Downloads or iCloud Drive.** macOS won't let background jobs read those folders. `schedule` now refuses to install there and prints the command to move the folder.
- **You opened `localhost:8050`.** Use http://127.0.0.1:8050; some Macs send `localhost` to IPv6, where the site isn't listening.
- **To see errors directly**, run the site in the foreground: `python3 app.py serve --site-only`, then open http://127.0.0.1:8050.

Pages:
- **Home:** for each snapshot time, how the top 5 and bottom 5 did afterward, split by whether the top names had company news. Below that, one row per session.
- **Day:** the full strength and weakness tables for any snapshot time, with levels, headlines and outcomes.
- **Ticker:** every recorded session for one stock.

## Choosing the names

The scanner ranks the tickers in `watchlist.txt`: 41 liquid names chosen for tight option spreads and weekly expirations (index ETFs, mega-cap tech, high-beta tech, plus a few large caps from other sectors). SPY is always the benchmark. Edit the file whenever you like, for example adding a name the night before its earnings, and the next scan picks up the change.

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
- Yahoo news only reaches back a day or two, so `--news` works for live scans and very recent replays.
- Quotes are free and can lag by a few seconds. This is not investment advice.
