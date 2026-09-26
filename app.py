#!/usr/bin/env python3
"""
Records the RS scanner every trading morning and serves the history on a local website.

    python app.py              # website at http://127.0.0.1:8050, records scans while it runs
    python app.py backfill     # load the last ~30 trading days from Yahoo (run once)
    python app.py backfill --deep   # also rebuild older days (~60 total) from 5-minute bars
    python app.py backfill --alpaca 12   # a year further back from Alpaca (free keys in .env)
    python app.py record       # record whatever is due right now, catch up missed days, then exit
    python app.py schedule     # run `record` every 5 minutes in the background (macOS, Windows, Linux)
    python app.py unschedule   # remove that background job
    python app.py status       # is the website up? recent log lines, for troubleshooting

While the site is running it takes a snapshot at each time in SNAPSHOTS on weekdays,
with headlines, and after 16:05 ET fills in what each stock did for the rest of the day.
Any session it missed (the computer was off or asleep) is rebuilt from Yahoo's
1-minute history the next time it runs, as long as it's within the last ~30 days.
Rebuilt snapshots have prices and outcomes but no headlines.
"""
from __future__ import annotations

import datetime as dt
import sqlite3
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

try:
    import flask  # noqa: F401  (used by web.py; checked here for a friendly message)
    import pandas as pd

    import premarket_rs as rs
except ModuleNotFoundError as e:
    sys.exit(f"Missing Python library: {e.name}. Run ./setup.sh (Mac/Linux) to install everything, "
             f"then use .venv/bin/python app.py ...")

SNAPSHOTS = ["08:00", "08:30", "09:00", "09:15", "09:29", "09:35", "09:45", "10:00"]
DEFAULT_VIEW = "09:35"
TREND_MIN = 45
TOP_N = 5
WATCHLIST = Path(__file__).with_name("watchlist.txt")


def load_tickers() -> list[str]:
    """watchlist.txt (one ticker per line, # for comments), re-read on every scan."""
    if not WATCHLIST.exists():
        return rs.DEFAULT_UNIVERSE
    names = [l.split("#")[0].strip().upper() for l in WATCHLIST.read_text().splitlines()]
    return list(dict.fromkeys(n for n in names if n)) or rs.DEFAULT_UNIVERSE


DB = Path(__file__).with_name("data") / "scans.db"
PORT = 8050


def version() -> str:
    """Commit this copy is running, shown in the page footer."""
    try:
        import subprocess
        return subprocess.run(["git", "log", "-1", "--format=%h %cd", "--date=format:%b %d %H:%M"],
                              cwd=Path(__file__).parent, capture_output=True, text=True).stdout.strip()
    except Exception:
        return ""


VERSION = version()

# scan() column -> database column
COLS = {
    "Last": "last", "Gap%": "gap", "Beta": "beta", "RS%": "rs", "RSz": "rsz",
    "Trend%": "trend", "Trendz": "trendz", "PMHi": "pm_hi", "PMLo": "pm_lo",
    "PrevHi": "prev_hi", "PrevLo": "prev_lo", "PrevCl": "prev_cl", "Score": "score",
    "Div": "div", "FwdRS%": "fwd_rs", "FwdHi%": "fwd_hi", "FwdLo%": "fwd_lo",
    "Fwd1h%": "fwd_1h", "FwdNoon%": "fwd_noon", "FwdClose%": "fwd_close",
    "News": "news", "NewsN": "news_n",
}
FWD = ["fwd_rs", "fwd_hi", "fwd_lo", "fwd_1h", "fwd_noon", "fwd_close"]
lock = threading.Lock()


# ------------------------------------------------------------- storage ----

def db() -> sqlite3.Connection:
    DB.parent.mkdir(exist_ok=True)
    con = sqlite3.connect(DB)
    cols = ", ".join(f"{c} {'TEXT' if c in ('div', 'news') else 'REAL'}" for c in COLS.values())
    con.executescript(f"""
        CREATE TABLE IF NOT EXISTS scans (
            day TEXT, asof TEXT, ticker TEXT, source TEXT, {cols},
            PRIMARY KEY (day, asof, ticker));
        CREATE TABLE IF NOT EXISTS market (
            day TEXT, asof TEXT, spy_gap REAL, spy_trend REAL, PRIMARY KEY (day, asof));
        CREATE TABLE IF NOT EXISTS done (day TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS news (
            ticker TEXT, ts TEXT, title TEXT, link TEXT, publisher TEXT, company INTEGER,
            PRIMARY KEY (ticker, title));
    """)
    have = {r[1] for r in con.execute("PRAGMA table_info(scans)")}
    added = [c for c in COLS.values() if c not in have]
    for c in added:
        con.execute(f"ALTER TABLE scans ADD COLUMN {c} {'TEXT' if c in ('div', 'news') else 'REAL'}")
    if added:  # new outcome columns: recompute outcomes for every day still in Yahoo's history
        con.execute("DELETE FROM done")
        con.execute("DELETE FROM state WHERE key='catch_up'")
        con.commit()
    return con


def save(con, day: dt.date, hhmm: str, df: pd.DataFrame, source: str) -> None:
    """Live rows overwrite everything but outcomes; replay rows only fill outcomes and gaps."""
    names = ["day", "asof", "ticker", "source"] + list(COLS.values())
    keep = FWD if source.startswith("replay") else [c for c in COLS.values() if c not in FWD] + ["source"]
    sql = (f"INSERT INTO scans ({', '.join(names)}) VALUES ({', '.join('?' * len(names))}) "
           f"ON CONFLICT(day, asof, ticker) DO UPDATE SET "
           + ", ".join(f"{c}=excluded.{c}" for c in keep))
    for t, r in df.iterrows():
        vals = [None if pd.isna(r.get(k)) else (r.get(k) if isinstance(r.get(k), str) else float(r.get(k)))
                for k in COLS]
        con.execute(sql, [day.isoformat(), hhmm, t, source] + vals)
    con.execute("INSERT OR REPLACE INTO market VALUES (?, ?, ?, ?)",
                (day.isoformat(), hhmm, df.attrs["spy_gap"], df.attrs["spy_trend"]))
    con.commit()


def load_market(start: dt.date | None = None):
    tickers = sorted(set(load_tickers()) | {rs.BENCH})
    if start is None:
        intraday = rs.fetch_intraday(tickers)
    else:  # stitch 7-day chunks together; Yahoo caps 1-minute requests at 8 days
        parts: dict[str, list] = {}
        today = dt.datetime.now(rs.NY).date()
        d = start
        while d <= today:
            for t, bars in rs.fetch_intraday(tickers, d, min(d + dt.timedelta(days=7), today + dt.timedelta(days=1))).items():
                parts.setdefault(t, []).append(bars)
            d += dt.timedelta(days=7)
        intraday = {t: pd.concat(p).pipe(lambda x: x[~x.index.duplicated()]).sort_index()
                    for t, p in parts.items()}
    return intraday, rs.fetch_daily(tickers)


# ------------------------------------------------------------ recording ----

def store_news(min_gap: dt.timedelta = dt.timedelta(minutes=25)) -> None:
    """Save Yahoo's latest headlines for every ticker. Yahoo drops old stories, so keeping
    them here is what lets the site show the news behind past sessions."""
    now = dt.datetime.now(dt.timezone.utc)
    with db() as con:
        last = con.execute("SELECT value FROM state WHERE key='news'").fetchone()
    if last and now - dt.datetime.fromisoformat(last[0]) < min_gap:
        return
    tickers = load_tickers()
    with ThreadPoolExecutor(max_workers=8) as pool:
        fetched = dict(zip(tickers, pool.map(rs.news_items, tickers)))
    with lock, db() as con:
        for t, items in fetched.items():
            con.executemany(
                "INSERT OR IGNORE INTO news VALUES (?, ?, ?, ?, ?, ?)",
                [(t, n["ts"].isoformat(), n["title"], n["link"], n["publisher"], int(rs.is_company(t, n["title"])))
                 for n in items])
        con.execute("INSERT OR REPLACE INTO state VALUES ('news', ?)", (now.isoformat(),))
    print(f"{now:%Y-%m-%d %H:%M}Z: saved headlines for {len(fetched)} tickers")


def at(day: dt.date, hhmm: str) -> dt.datetime:
    return dt.datetime.combine(day, dt.time.fromisoformat(hhmm), tzinfo=rs.NY)


def record_live(day: dt.date, hhmm: str) -> None:
    intraday, daily = load_market()
    if rs.BENCH not in intraday or day not in set(intraday[rs.BENCH].index.date):
        print(f"{day} {hhmm}: no session today (holiday?), skipping")
        return
    store_news()
    df = rs.scan(load_tickers(), day, at(day, hhmm), TREND_MIN, intraday, daily, with_news=True)
    with lock, db() as con:
        save(con, day, hhmm, df, "live")
    print(f"{day} {hhmm}: recorded {len(df)} tickers")


def record_outcomes(days: list[dt.date], intraday, daily, source: str = "replay") -> None:
    """Replay every snapshot time for finished sessions and fill in what happened next."""
    now = dt.datetime.now(rs.NY)
    for day in days:
        if day == now.date() and now.time() < dt.time(16, 5):
            continue
        for hhmm in SNAPSHOTS:
            try:
                df = rs.scan(load_tickers(), day, at(day, hhmm), TREND_MIN, intraday, daily, with_news=False)
            except Exception:  # first day in the data has no prior session
                break
            if df.empty:
                continue
            with lock, db() as con:
                save(con, day, hhmm, df, source)
        with lock, db() as con:
            con.execute("INSERT OR REPLACE INTO done VALUES (?)", (day.isoformat(),))
        print(f"{day}: outcomes recorded")


def trading_days(intraday) -> list[dt.date]:
    return sorted(set(intraday[rs.BENCH].index.date))


def catch_up(now: dt.datetime, force: bool = False) -> None:
    """Fill in every finished session from the last ~30 days that isn't recorded yet.

    Covers days the computer was off or asleep, and today's outcomes after the close.
    Tries at most once an hour unless forced, so a holiday doesn't trigger a fetch every run.
    """
    last_done = now.date() if now.time() >= dt.time(16, 5) else now.date() - dt.timedelta(days=1)
    oldest = now.date() - dt.timedelta(days=29)  # Yahoo keeps 1-minute bars for about 30 days
    with db() as con:
        done = {r[0] for r in con.execute("SELECT day FROM done")}
        tried = con.execute("SELECT value FROM state WHERE key='catch_up'").fetchone()
    missing = [d for d in (oldest + dt.timedelta(days=i) for i in range((last_done - oldest).days + 1))
               if d.weekday() < 5 and d.isoformat() not in done]
    store_news(dt.timedelta(minutes=25) if missing or force else dt.timedelta(hours=3))
    if not missing:
        return
    if not force and tried and now - dt.datetime.fromisoformat(tried[0]) < dt.timedelta(hours=1):
        return
    with db() as con:
        con.execute("INSERT OR REPLACE INTO state VALUES ('catch_up', ?)", (now.isoformat(),))
    print(f"{now:%Y-%m-%d %H:%M}: catching up {len(missing)} weekday(s) from {missing[0]}")
    intraday, daily = load_market(max(oldest, missing[0] - dt.timedelta(days=5)))
    if rs.BENCH not in intraday:
        print("No data from Yahoo; will retry later")
        return
    sessions = trading_days(intraday)
    record_outcomes([d for d in sessions if d in missing], intraday, daily)
    # Weekdays with no session (holidays) count as done once Yahoo has data past them.
    with db() as con:
        for d in missing:
            if d < sessions[-1]:
                con.execute("INSERT OR IGNORE INTO done VALUES (?)", (d.isoformat(),))


def backfill_deep() -> None:
    """Sessions older than the 1-minute history, rebuilt from Yahoo's 5-minute bars (~60 days).

    Scores use 5-minute prices, so they're a little coarser than the 1-minute days; the
    site labels these days. Days already recorded are left alone.
    """
    tickers = sorted(set(load_tickers()) | {rs.BENCH})
    print("Loading ~60 days of 5-minute bars from Yahoo...")
    intraday = rs.fetch_intraday(tickers, interval="5m")
    daily = rs.fetch_daily(tickers)
    with db() as con:
        have = {r[0] for r in con.execute("SELECT DISTINCT day FROM scans")}
    days = [d for d in trading_days(intraday) if d.isoformat() not in have]
    print(f"{len(days)} older session(s) to rebuild")
    record_outcomes(days, intraday, daily, source="replay-5m")


def backfill_alpaca(months: int = 12) -> None:
    """Sessions older than what's recorded, from Alpaca's 1-minute bars, a month at a time.

    Uses today's watchlist for every past day, so a stock that listed or fell off the
    list since then is judged as if you'd been watching it (survivorship).
    """
    import alpaca_data
    tickers = sorted(set(load_tickers()) | {rs.BENCH})
    today = dt.datetime.now(rs.NY).date()
    oldest = today - dt.timedelta(days=int(months * 30.5))
    with db() as con:
        have = {r[0] for r in con.execute("SELECT DISTINCT day FROM scans")}
    end = (dt.date.fromisoformat(min(have)) - dt.timedelta(days=1)) if have else today
    print("Daily history for betas...")
    daily = rs.fetch_daily(tickers, period="2y" if months <= 12 else "5y")
    while end > oldest:
        start = max(oldest, end - dt.timedelta(days=30))
        print(f"Alpaca 1-minute bars {start} → {end} ...", flush=True)
        # a week of extra history so the first day has a previous session
        bars = alpaca_data.fetch_bars(tickers, start - dt.timedelta(days=7), end)
        if rs.BENCH not in bars:
            print("No SPY bars returned; stopping.")
            break
        days = [d for d in trading_days(bars) if start <= d <= end and d.isoformat() not in have]
        record_outcomes(days, bars, daily, source="replay-alpaca")
        end = start - dt.timedelta(days=1)


def record_due(now: dt.datetime, taken: set) -> None:
    """Take any snapshot due in the last 10 minutes, then catch up on finished sessions."""
    if now.weekday() < 5:
        for hhmm in SNAPSHOTS:
            due = at(now.date(), hhmm)
            if (now.date(), hhmm) in taken or not due <= now < due + dt.timedelta(minutes=10):
                continue
            taken.add((now.date(), hhmm))
            with db() as con:
                have = con.execute("SELECT 1 FROM scans WHERE day=? AND asof=? AND source='live'",
                                    (now.date().isoformat(), hhmm)).fetchone()
            if not have:
                record_live(now.date(), hhmm)
    catch_up(now)


def scheduler() -> None:
    taken: set = set()
    while True:
        try:
            record_due(dt.datetime.now(rs.NY), taken)
        except Exception:
            traceback.print_exc()
        time.sleep(30)


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "serve"
    if cmd == "backfill":
        print("Loading the last ~30 days from Yahoo...")
        catch_up(dt.datetime.now(rs.NY), force=True)
        if "--deep" in sys.argv:
            backfill_deep()
        if "--alpaca" in sys.argv:
            i = sys.argv.index("--alpaca")
            months = int(sys.argv[i + 1]) if len(sys.argv) > i + 1 and sys.argv[i + 1].isdigit() else 12
            backfill_alpaca(months)
    elif cmd == "record":
        DB.parent.mkdir(exist_ok=True)
        with open(DB.with_name("record.log"), "a") as log:  # scheduled runs have no console
            sys.stdout = sys.stderr = log
            try:
                record_due(dt.datetime.now(rs.NY), set())
            except Exception:
                traceback.print_exc()
    elif cmd in ("schedule", "unschedule", "status"):
        import scheduling
        {"schedule": scheduling.install, "unschedule": scheduling.remove, "status": scheduling.status}[cmd]()
    elif cmd == "serve":
        from web import web as app
        if sys.stdout is None:  # started hidden (pythonw), so log to a file
            DB.parent.mkdir(exist_ok=True)
            sys.stdout = sys.stderr = open(DB.with_name("site.log"), "a", buffering=1)
        if "--site-only" not in sys.argv:  # the scheduled `record` job does the recording instead
            threading.Thread(target=scheduler, daemon=True).start()
            print(f"Recording {', '.join(SNAPSHOTS)} ET on weekdays")
        print(f"RS scanner log: http://127.0.0.1:{PORT}")
        app.run(host="127.0.0.1", port=PORT)
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    sys.modules.setdefault("app", sys.modules[__name__])  # so `import app` in web.py reuses this module
    main()
