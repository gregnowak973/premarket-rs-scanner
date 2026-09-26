#!/usr/bin/env python3
"""
Records the RS scanner every trading morning and serves the history on a local website.

    python app.py              # website at http://localhost:8050, records scans while it runs
    python app.py backfill     # load the last ~30 trading days from Yahoo (run once)
    python app.py record       # record whatever is due right now, then exit (for cron/Task Scheduler)

While the site is running it takes a snapshot at each time in SNAPSHOTS on weekdays,
with headlines, and after 16:05 ET fills in what each stock did for the rest of the day.
Snapshots it missed (the app wasn't running) are filled in from Yahoo's 1-minute
history at the end of the day, without headlines.
"""
from __future__ import annotations

import datetime as dt
import sqlite3
import sys
import threading
import time
import traceback
from pathlib import Path

import pandas as pd
from flask import Flask, abort, render_template_string, request

import premarket_rs as rs

SNAPSHOTS = ["08:00", "08:30", "09:00", "09:15", "09:29", "09:35", "09:45", "10:00"]
DEFAULT_VIEW = "09:35"
TREND_MIN = 45
TOP_N = 5
TICKERS = rs.DEFAULT_UNIVERSE
DB = Path(__file__).with_name("data") / "scans.db"
PORT = 8050

# scan() column -> database column
COLS = {
    "Last": "last", "Gap%": "gap", "Beta": "beta", "RS%": "rs", "RSz": "rsz",
    "Trend%": "trend", "Trendz": "trendz", "PMHi": "pm_hi", "PMLo": "pm_lo",
    "PrevHi": "prev_hi", "PrevLo": "prev_lo", "PrevCl": "prev_cl", "Score": "score",
    "Div": "div", "FwdRS%": "fwd_rs", "FwdHi%": "fwd_hi", "FwdLo%": "fwd_lo",
    "News": "news", "NewsN": "news_n",
}
FWD = ["fwd_rs", "fwd_hi", "fwd_lo"]
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
    """)
    return con


def save(con, day: dt.date, hhmm: str, df: pd.DataFrame, source: str) -> None:
    """Live rows overwrite everything but outcomes; replay rows only fill outcomes and gaps."""
    names = ["day", "asof", "ticker", "source"] + list(COLS.values())
    keep = FWD if source == "replay" else [c for c in COLS.values() if c not in FWD] + ["source"]
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
    tickers = sorted(set(TICKERS) | {rs.BENCH})
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

def at(day: dt.date, hhmm: str) -> dt.datetime:
    return dt.datetime.combine(day, dt.time.fromisoformat(hhmm), tzinfo=rs.NY)


def record_live(day: dt.date, hhmm: str) -> None:
    intraday, daily = load_market()
    if rs.BENCH not in intraday or day not in set(intraday[rs.BENCH].index.date):
        print(f"{day} {hhmm}: no session today (holiday?), skipping")
        return
    df = rs.scan(TICKERS, day, at(day, hhmm), TREND_MIN, intraday, daily, with_news=True)
    with lock, db() as con:
        save(con, day, hhmm, df, "live")
    print(f"{day} {hhmm}: recorded {len(df)} tickers")


def record_outcomes(days: list[dt.date], intraday, daily) -> None:
    """Replay every snapshot time for finished sessions and fill in what happened next."""
    now = dt.datetime.now(rs.NY)
    for day in days:
        if day == now.date() and now.time() < dt.time(16, 5):
            continue
        for hhmm in SNAPSHOTS:
            try:
                df = rs.scan(TICKERS, day, at(day, hhmm), TREND_MIN, intraday, daily, with_news=False)
            except Exception:  # first day in the data has no prior session
                break
            if df.empty:
                continue
            with lock, db() as con:
                save(con, day, hhmm, df, "replay")
        with lock, db() as con:
            con.execute("INSERT OR REPLACE INTO done VALUES (?)", (day.isoformat(),))
        print(f"{day}: outcomes recorded")


def trading_days(intraday) -> list[dt.date]:
    return sorted(set(intraday[rs.BENCH].index.date))


def backfill() -> None:
    start = dt.datetime.now(rs.NY).date() - dt.timedelta(days=29)
    print(f"Loading 1-minute bars since {start}...")
    intraday, daily = load_market(start)
    record_outcomes(trading_days(intraday), intraday, daily)


def record_due(now: dt.datetime, taken: set) -> None:
    """Take any snapshot due in the last 10 minutes, and outcomes after the close."""
    if now.weekday() >= 5:
        return
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
    if now.time() >= dt.time(16, 5):
        with db() as con:
            finished = con.execute("SELECT 1 FROM done WHERE day=?", (now.date().isoformat(),)).fetchone()
        if not finished:
            intraday, daily = load_market()
            record_outcomes([d for d in trading_days(intraday) if d >= now.date() - dt.timedelta(days=5)],
                            intraday, daily)


def scheduler() -> None:
    taken: set = set()
    while True:
        try:
            record_due(dt.datetime.now(rs.NY), taken)
        except Exception:
            traceback.print_exc()
        time.sleep(30)


# -------------------------------------------------------------- website ----

app = Flask(__name__)


def query(sql: str, *args) -> pd.DataFrame:
    with db() as con:
        return pd.read_sql_query(sql, con, params=args)


def snapshot_stats(rows: pd.DataFrame) -> pd.DataFrame:
    """Per snapshot time: how the top and bottom of the ranking did afterwards."""
    out = []
    for hhmm, g in rows.dropna(subset=["fwd_rs"]).groupby("asof"):
        days = []
        for day, d in g.groupby("day"):
            top, bot = d.nlargest(TOP_N, "score"), d.nsmallest(TOP_N, "score")
            days.append({
                "top": top.fwd_rs.mean(), "bot": bot.fwd_rs.mean(),
                "top_news": top[top.news_n > 0].fwd_rs.mean(),
                "top_nonews": top[top.news_n.fillna(0) == 0].fwd_rs.mean() if top.news_n.notna().any() else float("nan"),
            })
        s = pd.DataFrame(days)
        out.append({"asof": hhmm, "days": len(s), "top": s.top.mean(), "bot": s.bot.mean(),
                    "spread": (s.top - s.bot).mean(), "hit": ((s.top - s.bot) > 0).mean() * 100,
                    "top_news": s.top_news.mean(), "top_nonews": s.top_nonews.mean()})
    return pd.DataFrame(out)


BASE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ title }}</title>
<style>
:root{--bg:#f7f7f5;--card:#fff;--fg:#1d1d1f;--mute:#6e6e73;--line:#e3e3e0;--up:#0a7d3b;--dn:#c0392b;--acc:#2f5bd3}
@media (prefers-color-scheme:dark){:root{--bg:#141416;--card:#1d1d20;--fg:#ececef;--mute:#9a9aa1;--line:#2e2e33;--up:#3ecf7a;--dn:#ff6b5e;--acc:#7c9cff}}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 -apple-system,system-ui,Segoe UI,Roboto,sans-serif}
main{max-width:1200px;margin:0 auto;padding:20px 16px 60px}
a{color:var(--acc);text-decoration:none} a:hover{text-decoration:underline}
h1{font-size:22px;margin:0 0 4px} h2{font-size:16px;margin:28px 0 8px} .mute{color:var(--mute)}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:4px 0;overflow-x:auto}
table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}
th,td{padding:6px 10px;text-align:right;white-space:nowrap;border-bottom:1px solid var(--line)}
th{font-weight:600;color:var(--mute);font-size:12px} tr:last-child td{border-bottom:0}
td.l,th.l{text-align:left} td.news{white-space:normal;min-width:260px;text-align:left;color:var(--mute)}
.up{color:var(--up)} .dn{color:var(--dn)}
.tabs{display:flex;gap:6px;flex-wrap:wrap;margin:12px 0} .tabs a{padding:4px 10px;border:1px solid var(--line);border-radius:999px;background:var(--card)}
.tabs a.on{background:var(--acc);color:#fff;border-color:var(--acc)}
nav{margin-bottom:16px}
</style></head><body><main>
<nav><a href="/">RS scanner log</a></nav>
{{ body|safe }}
</main></body></html>"""


def pct(v, digits=2, unit=""):
    if v is None or pd.isna(v):
        return '<span class="mute">–</span>'
    return f'<span class="{"up" if v > 0 else "dn" if v < 0 else ""}">{v:+.{digits}f}{unit}</span>'


def pp(v):
    return pct(v, unit="%")


def page(title: str, body: str) -> str:
    return render_template_string(BASE, title=title, body=body)


@app.route("/")
def home():
    rows = query("SELECT day, asof, ticker, score, fwd_rs, news_n FROM scans")
    if rows.empty:
        return page("RS scanner log", "<h1>No scans yet</h1><p>Run <code>python app.py backfill</code> "
                    "to load the last month, or leave this running on a weekday morning.</p>")
    stats = snapshot_stats(rows)
    h = ["<h1>Premarket RS scanner log</h1>",
         f'<p class="mute">{rows.day.nunique()} sessions recorded. For each snapshot time: how the '
         f"top {TOP_N} and bottom {TOP_N} by score did from then to the close, vs SPY (beta-adjusted).</p>",
         '<h2>Does the ranking work?</h2><div class="card"><table><tr><th class="l">Snapshot</th><th>Days</th>'
         f"<th>Top {TOP_N}</th><th>Bottom {TOP_N}</th><th>Spread</th><th>Days spread &gt; 0</th>"
         "<th>Top with company news</th><th>Top without news</th></tr>"]
    for _, s in stats.iterrows():
        h.append(f'<tr><td class="l">{s["asof"]}</td><td>{s.days}</td><td>{pp(s.top)}</td><td>{pp(s.bot)}</td>'
                 f"<td>{pp(s.spread)}</td><td>{s.hit:.0f}%</td><td>{pp(s.top_news)}</td><td>{pp(s.top_nonews)}</td></tr>")
    h.append('</table></div><p class="mute">Spread above zero means the strong names beat the weak ones. '
             "News columns only cover snapshots recorded live, since Yahoo drops old headlines.</p>")

    view = rows[rows["asof"] == DEFAULT_VIEW]
    market = query("SELECT day, spy_gap FROM market WHERE asof=?", DEFAULT_VIEW).set_index("day")
    h.append(f'<h2>Sessions (ranking at {DEFAULT_VIEW})</h2><div class="card"><table><tr><th class="l">Day</th>'
             "<th>SPY</th><th class='l'>Strongest</th><th class='l'>Weakest</th><th>Spread</th></tr>")
    for day in sorted(rows.day.unique(), reverse=True):
        d = view[view.day == day]
        top, bot = d.nlargest(3, "score"), d.nsmallest(3, "score")
        spread = d.nlargest(TOP_N, "score").fwd_rs.mean() - d.nsmallest(TOP_N, "score").fwd_rs.mean()
        link = lambda g: " ".join(f'<a href="/ticker/{t}">{t}</a>' for t in g.ticker)
        spy = market.spy_gap.get(day)
        h.append(f'<tr><td class="l"><a href="/day/{day}">{day}</a></td><td>{pp(spy)}</td>'
                 f'<td class="l">{link(top)}</td><td class="l">{link(bot)}</td><td>{pp(spread)}</td></tr>')
    h.append("</table></div>")
    return page("RS scanner log", "".join(h))


def ranking_table(d: pd.DataFrame, first_col: str = "ticker") -> str:
    h = ['<div class="card"><table><tr>'
         f'<th class="l">{"Ticker" if first_col == "ticker" else "Day"}</th><th>Last</th><th>Gap%</th><th>RS%</th>'
         "<th>Trend%</th><th>Score</th><th>PM hi / lo</th><th>Prev hi / lo</th><th class='l'>Div</th>"
         "<th>Fwd RS%</th><th>Fwd best%</th><th>Fwd worst%</th><th class='l'>News</th></tr>"]
    for _, r in d.iterrows():
        key = (f'<a href="/ticker/{r.ticker}">{r.ticker}</a>' if first_col == "ticker"
               else f'<a href="/day/{r.day}?t={r["asof"]}">{r.day}</a>')
        news = "" if not isinstance(r.news, str) or not r.news else f"({int(r.news_n or 0)}) {r.news}"
        h.append(f'<tr><td class="l">{key}</td><td>{r["last"]:.2f}</td><td>{pct(r.gap)}</td><td>{pct(r.rs)}</td>'
                 f"<td>{pct(r.trend)}</td><td>{pct(r.score, 1)}</td>"
                 f"<td>{r.pm_hi:.2f} / {r.pm_lo:.2f}</td><td>{r.prev_hi:.2f} / {r.prev_lo:.2f}</td>"
                 f'<td class="l">{r["div"] or ""}</td><td>{pct(r.fwd_rs)}</td><td>{pct(r.fwd_hi)}</td>'
                 f'<td>{pct(r.fwd_lo)}</td><td class="news">{news}</td></tr>')
    h.append("</table></div>")
    return "".join(h)


@app.route("/day/<day>")
def day_view(day):
    t = request.args.get("t", DEFAULT_VIEW)
    d = query("SELECT * FROM scans WHERE day=? AND asof=?", day, t)
    times = query("SELECT DISTINCT asof FROM scans WHERE day=? ORDER BY asof", day)["asof"]
    if times.empty:
        abort(404)
    m = query("SELECT spy_gap, spy_trend FROM market WHERE day=? AND asof=?", day, t)
    tabs = "".join(f'<a class="{"on" if x == t else ""}" href="?t={x}">{x}</a>' for x in times)
    spy = f"SPY {pp(m.spy_gap[0])} (last {TREND_MIN} min {pp(m.spy_trend[0])})" if not m.empty else ""
    src = "recorded live" if (d.source == "live").any() else "rebuilt from 1-minute history"
    body = (f"<h1>{day}</h1><p class='mute'>{spy} · {src}. Fwd columns: what happened after {t} "
            f"(RS to the close vs SPY, best and worst move from the {t} price).</p><div class='tabs'>{tabs}</div>"
            f"<h2>Relative strength (calls)</h2>{ranking_table(d.nlargest(10, 'score'))}"
            f"<h2>Relative weakness (puts)</h2>{ranking_table(d.nsmallest(10, 'score'))}")
    return page(f"RS {day}", body)


@app.route("/ticker/<ticker>")
def ticker_view(ticker):
    t = request.args.get("t", DEFAULT_VIEW)
    d = query("SELECT * FROM scans WHERE ticker=? AND asof=? ORDER BY day DESC", ticker.upper(), t)
    if d.empty:
        abort(404)
    tabs = "".join(f'<a class="{"on" if x == t else ""}" href="?t={x}">{x}</a>' for x in SNAPSHOTS)
    body = (f"<h1>{ticker.upper()}</h1><p class='mute'>Every recorded session at {t}.</p>"
            f"<div class='tabs'>{tabs}</div>{ranking_table(d, first_col='day')}")
    return page(f"RS {ticker.upper()}", body)


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "serve"
    if cmd == "backfill":
        backfill()
    elif cmd == "record":
        record_due(dt.datetime.now(rs.NY), set())
    elif cmd == "serve":
        threading.Thread(target=scheduler, daemon=True).start()
        print(f"RS scanner log: http://localhost:{PORT}  (recording {', '.join(SNAPSHOTS)} ET on weekdays)")
        app.run(host="127.0.0.1", port=PORT)
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
