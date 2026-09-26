#!/usr/bin/env python3
"""
Records the RS scanner every trading morning and serves the history on a local website.

    python app.py              # website at http://127.0.0.1:8050, records scans while it runs
    python app.py backfill     # load the last ~30 trading days from Yahoo (run once)
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
import html
import sqlite3
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

try:
    import pandas as pd
    from flask import Flask, abort, render_template_string, request

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


def record_outcomes(days: list[dt.date], intraday, daily) -> None:
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
                save(con, day, hhmm, df, "replay")
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
                "top_nonews": top[top.news_n == 0].fwd_rs.mean(),
            })
        s = pd.DataFrame(days)
        out.append({"asof": hhmm, "days": len(s), "top": s.top.mean(), "bot": s.bot.mean(),
                    "spread": (s.top - s.bot).mean(), "hit": ((s.top - s.bot) > 0).mean() * 100,
                    "hits": int(((s.top - s.bot) > 0).sum()),
                    "top_news": s.top_news.mean(), "top_nonews": s.top_nonews.mean()})
    return pd.DataFrame(out)


BASE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ title }}</title>
<style>
:root{--bg:#f7f7f5;--card:#fff;--fg:#1d1d1f;--mute:#6e6e73;--line:#e3e3e0;--up:#0a7d3b;--dn:#c0392b;--acc:#2f5bd3}
@media (prefers-color-scheme:dark){:root{--bg:#141416;--card:#1d1d20;--fg:#ececef;--mute:#9a9aa1;--line:#2e2e33;--up:#3ecf7a;--dn:#ff6b5e;--acc:#7c9cff}}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 -apple-system,system-ui,Segoe UI,Roboto,sans-serif}
main{max-width:1440px;margin:0 auto;padding:20px 16px 60px}
a{color:var(--acc);text-decoration:none} a:hover{text-decoration:underline}
h1{font-size:22px;margin:0 0 4px} h2{font-size:16px;margin:28px 0 8px} .mute{color:var(--mute)}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:4px 0;overflow-x:auto}
table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}
th,td{padding:6px 8px;text-align:right;white-space:nowrap;border-bottom:1px solid var(--line)}
th{font-weight:600;color:var(--mute);font-size:12px} tr:last-child td{border-bottom:0}
td.l,th.l{text-align:left} td.news{white-space:normal;min-width:260px;text-align:left;color:var(--mute)}
.tog{min-width:64px;font:inherit;color:var(--acc);background:none;border:1px solid var(--line);border-radius:6px;padding:2px 8px;cursor:pointer;white-space:nowrap}
.tog[aria-expanded="true"]{background:var(--acc);color:#fff;border-color:var(--acc)}
tr.detail td{text-align:left;white-space:normal;background:var(--bg);padding:10px 14px 14px}
tr.detail p{margin:0 0 10px;max-width:900px} tr.detail ul{margin:4px 0 0;padding-left:18px} tr.detail li{margin:3px 0}
.cols{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:8px 28px}
.up{color:var(--up)} .dn{color:var(--dn)}
.pick td:first-child,.pick th:first-child{position:sticky;left:0;background:var(--card);z-index:1}
tr.detail td>*{position:sticky;left:14px;max-width:min(1100px,calc(100vw - 70px))}
tr.grp th{font-size:11px;text-transform:uppercase;letter-spacing:.04em;text-align:left;border-bottom:0;padding-bottom:0}
.hind{border-left:2px solid var(--line)} td.hind~td:not(:last-child),td.hind{background:color-mix(in srgb,var(--bg) 55%,transparent)}
.win{font-weight:600;background:color-mix(in srgb,var(--up) 14%,transparent)!important;border-radius:4px}
.lvl{display:inline-block;font-size:12px;padding:1px 6px;margin-left:3px;border-radius:999px;border:1px solid var(--line);color:var(--mute)}
.lvl.up{border-color:color-mix(in srgb,var(--up) 45%,transparent);color:var(--up)}
.lvl.dn{border-color:color-mix(in srgb,var(--dn) 45%,transparent);color:var(--dn)}
.badge{display:inline-block;min-width:18px;margin-left:4px;padding:0 5px;border-radius:999px;background:var(--line);color:var(--fg);font-size:12px}
.tog[aria-expanded="true"] .badge{background:#fff3;color:#fff}
.best td{font-weight:600}
details.rest>summary{cursor:pointer;color:var(--acc);margin:28px 0 8px;font-weight:600;font-size:16px}
details.rest td{opacity:.8}
details.how{margin:4px 0 0} details.how summary{cursor:pointer;color:var(--acc)} details.how p{margin:6px 0;max-width:900px}
@media(max-width:640px){.hide-sm,tr.grp{display:none} h1{font-size:20px} main{padding:16px 10px 48px}
 th{white-space:normal;vertical-align:bottom} th,td{padding:6px 5px} .tog{min-width:0;padding:2px 6px}}
.tabs{display:flex;gap:6px;flex-wrap:wrap;margin:12px 0} .tabs a{padding:4px 10px;border:1px solid var(--line);border-radius:999px;background:var(--card)}
.tabs a.on{background:var(--acc);color:#fff;border-color:var(--acc)}
nav{margin-bottom:16px}
</style></head><body><main>
<nav><a href="/">RS scanner log</a></nav>
{{ body|safe }}
</main>
<script>
document.addEventListener("click", e => {
  const b = e.target.closest(".tog"); if (!b) return;
  const row = b.closest("tr").nextElementSibling; row.hidden = !row.hidden;
  b.setAttribute("aria-expanded", String(!row.hidden));
});
</script></body></html>"""


def pct(v, digits=2, unit=""):
    if v is None or pd.isna(v):
        return '<span class="mute">–</span>'
    v = round(float(v), digits) + 0.0  # no red "-0.00"
    return f'<span class="{"up" if v > 0 else "dn" if v < 0 else ""}">{v:+.{digits}f}{unit}</span>'


def pp(v):
    return pct(v, unit="%")


def page(title: str, body: str) -> str:
    return render_template_string(BASE, title=title, body=body)


@app.route("/")
def home():
    rows = query("SELECT day, asof, ticker, score, fwd_rs, news_n FROM scans")
    if not rows.empty:
        rows["news_n"] = company_news_counts(rows).fillna(rows.news_n).values
    if rows.empty:
        return page("RS scanner log", "<h1>No scans yet</h1><p>Run <code>python app.py backfill</code> "
                    "to load the last month, or leave this running on a weekday morning.</p>")
    stats = snapshot_stats(rows)
    h = ["<h1>Premarket RS scanner log</h1>",
         f'<p class="mute">{rows.day.nunique()} sessions recorded. For each snapshot time: how the '
         f"top {TOP_N} and bottom {TOP_N} by score did from then to the close, vs SPY (beta-adjusted).</p>",
         '<h2>Does the ranking work?</h2><div class="card"><table><tr><th class="l">Snapshot</th>'
         f'<th title="Top {TOP_N} minus bottom {TOP_N}, RS to the close, averaged over days">Spread</th>'
         "<th>Days spread &gt; 0</th>"
         f"<th>Top {TOP_N}</th><th>Bottom {TOP_N}</th><th class='hide-sm'>Top with company news</th>"
         "<th class='hide-sm'>Top without news</th><th class='hide-sm'>Days</th></tr>"]
    best = stats.sort_values(["spread", "hit"], ascending=False)["asof"].iloc[0] if not stats.empty else None
    mute = lambda v: f'<span class="mute">{v:+.2f}%</span>' if pd.notna(v) else pp(v)
    for _, s in stats.iterrows():
        h.append(f'<tr class="{"best" if s["asof"] == best else ""}"><td class="l">{s["asof"]}</td>'
                 f"<td>{pp(s.spread)}</td><td>{s.hits}/{s.days} ({s.hit:.0f}%)</td><td>{mute(s.top)}</td>"
                 f"<td>{mute(s.bot)}</td><td class='hide-sm'>{pp(s.top_news)}</td>"
                 f"<td class='hide-sm'>{pp(s.top_nonews)}</td><td class='hide-sm'>{s.days}</td></tr>")
    h.append(f'</table></div><p class="mute">Spread above zero means the strong names beat the weak ones '
             f"(RS vs SPY from the snapshot to the close). With {rows.day.nunique()} days, differences under "
             "about 0.3% are noise. News columns only cover days with saved headlines.</p>")

    view = rows[rows["asof"] == DEFAULT_VIEW]
    market = query("SELECT day, spy_gap FROM market WHERE asof=?", DEFAULT_VIEW).set_index("day")
    h.append(f'<h2>Sessions (ranking at {DEFAULT_VIEW})</h2><div class="card"><table><tr><th class="l">Day</th>'
             f"<th>SPY gap @{DEFAULT_VIEW}</th><th class='l'>Top 3</th><th class='l'>Bottom 3</th>"
             f"<th>Spread (top {TOP_N} − bottom {TOP_N})</th></tr>")
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


def et(day: str, hhmm: str) -> pd.Timestamp:
    return pd.Timestamp(f"{day} {hhmm}", tz=rs.NY)


def prev_close(day: str) -> pd.Timestamp:
    """16:00 ET on the session before `day` (weekends skipped; holidays count as sessions)."""
    d = dt.date.fromisoformat(day) - dt.timedelta(days=1)
    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)
    return et(d.isoformat(), "16:00")


def load_news(tickers: list[str], start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    q = ",".join("?" * len(tickers))
    n = query(f"SELECT * FROM news WHERE ticker IN ({q}) AND ts >= ? AND ts <= ?", *tickers,
              start.tz_convert("UTC").isoformat(), end.tz_convert("UTC").isoformat())
    n["ts"] = pd.to_datetime(n.ts, utc=True, format="ISO8601").dt.tz_convert(rs.NY)
    return n.sort_values(["company", "ts"], ascending=[False, False])


def company_news_counts(rows: pd.DataFrame) -> pd.Series:
    """Company headlines between the previous close and each snapshot, per (day, asof, ticker)."""
    n = query("SELECT ticker, ts FROM news WHERE company=1")
    if n.empty:
        return pd.Series(float("nan"), index=rows.index)
    n["ts"] = pd.to_datetime(n.ts, utc=True, format="ISO8601")
    r = rows[["day", "asof", "ticker"]].reset_index()
    r["lo"] = r.day.map(lambda d: prev_close(d).tz_convert("UTC"))
    r["hi"] = [et(d, a).tz_convert("UTC") for d, a in zip(r.day, r["asof"])]
    m = r.merge(n, on="ticker")
    m = m[(m.ts >= m.lo) & (m.ts <= m.hi)]
    counts = m.groupby("index").size().reindex(r["index"]).fillna(0)
    # Days before any headline was saved have no news data at all, rather than no news.
    first = n.ts.min()
    counts[(r.set_index("index").hi < first).values] = float("nan")
    return counts


def why(r, before: pd.DataFrame) -> str:
    """Plain-English reasons behind a row's score."""
    bits = [f"Gap {r.gap:+.2f}% vs yesterday's close, {r.rs:+.2f}% beyond what SPY's move implies."]
    if abs(r.trendz) >= 1:
        bits.append(f"{'Gaining on' if r.trend > 0 else 'Losing to'} SPY fast: {r.trend:+.2f}% in the last {TREND_MIN} min.")
    elif abs(r.trendz) >= 0.3:
        bits.append(f"{'Gaining on' if r.trend > 0 else 'Slipping vs'} SPY: {r.trend:+.2f}% in the last {TREND_MIN} min.")
    else:
        bits.append(f"Flat vs SPY over the last {TREND_MIN} min.")
    if r["div"]:
        bits.append("Moving opposite to SPY: " + ("up while SPY dipped." if r["div"].startswith("UP") else "down while SPY rose."))
    lv = []
    if r["last"] > r.prev_hi: lv.append("above yesterday's high")
    if r["last"] < r.prev_lo: lv.append("below yesterday's low")
    if pd.notna(r.pm_hi) and r["asof"] >= "09:30":
        if r["last"] > r.pm_hi: lv.append("above the premarket high")
        if r["last"] < r.pm_lo: lv.append("below the premarket low")
    bits.append(("Price is " + " and ".join(lv) + ".") if lv else "Inside yesterday's range.")
    named = int(before.company.sum()) if not before.empty else 0
    if before.empty and r.news_n != r.news_n:  # no saved headlines and none recorded live
        bits.append("No headlines saved for this day.")
    else:
        bits.append(f"{named} company headline{'s' if named != 1 else ''} since yesterday's close"
                    + (f" (+{len(before) - named} sector/market)." if len(before) > named else "."))
    return " ".join(bits)


def level_chips(r) -> str:
    """Where the price sits against the levels a 0DTE entry is judged by."""
    chips = []
    if pd.notna(r.pm_hi) and r["last"] > r.pm_hi: chips.append(("up", "▲ PM hi"))
    if pd.notna(r.pm_lo) and r["last"] < r.pm_lo: chips.append(("dn", "▼ PM lo"))
    if r["last"] > r.prev_hi: chips.append(("up", "▲ Y hi"))
    if r["last"] < r.prev_lo: chips.append(("dn", "▼ Y lo"))
    tip = f"Premarket {r.pm_hi:.2f} / {r.pm_lo:.2f} · Yesterday {r.prev_hi:.2f} / {r.prev_lo:.2f}"
    body = "".join(f'<span class="lvl {c}">{t}</span>' for c, t in chips) or '<span class="lvl">inside</span>'
    return f'<span title="{tip}">{body}</span>'


def headline_list(n: pd.DataFrame, limit: int = 12) -> str:
    if n.empty:
        return '<p class="mute">None saved.</p>'
    items = []
    for _, x in n.head(limit).iterrows():
        title = html.escape(x.title)
        link = f'<a href="{html.escape(x.link)}" target="_blank" rel="noopener">{title}</a>' if x.link else title
        tag = "" if x.company else ' <span class="mute">(sector)</span>'
        items.append(f'<li><span class="mute">{x.ts:%a %H:%M}</span> {link}{tag} '
                     f'<span class="mute">· {html.escape(x.publisher or "")}</span></li>')
    more = f'<li class="mute">+{len(n) - limit} more</li>' if len(n) > limit else ""
    return f"<ul>{''.join(items)}{more}</ul>"


def ranking_table(d: pd.DataFrame, news: pd.DataFrame, first_col: str = "ticker", side: str = "") -> str:
    """side: "call" or "put" marks the RS-after cell when the stock moved the trade's way."""
    t = d["asof"].iloc[0] if not d.empty else ""
    head = [
        ("l", "Ticker" if first_col == "ticker" else "Day", ""),
        ("", "Score", "Combined z-score of RS and trend. ±1 is notable, ±3 is strong"),
        ("", "RS vs SPY %", "Gap from yesterday's close minus what SPY's gap implies for this stock (beta-adjusted)"),
        ("", f"{TREND_MIN}m vs SPY", f"Change in RS vs SPY over the last {TREND_MIN} minutes"),
        ("hide-sm", "Gap %", "Change from yesterday's close"),
        ("l hide-sm", "Level", "Price vs the premarket high/low and yesterday's high/low; hover for prices"),
        ("hide-sm", "Last", "Price at the snapshot"),
        ("hind hide-sm", "+1 hour", "Price change from the snapshot price one hour later"),
        ("hide-sm", "Noon", "Price change from the snapshot price to 12:00 ET"),
        ("hide-sm", "Close", "Price change from the snapshot price to the close"),
        ("", "RS after, to close", "Move to the close minus what SPY's move implies. Highlighted when it went the trade's way"),
        ("hide-sm", "Max up", "Biggest rise after the snapshot"),
        ("hide-sm", "Max down", "Biggest drop after the snapshot"),
        ("l", "", ""),
    ]
    cls = "pick" + (" " + side if side else "")
    h = [f'<div class="card"><table class="{cls}"><tr class="grp"><th colspan="7">At {t}</th>'
         f'<th colspan="6" class="hind">After {t} (hindsight)</th><th></th></tr><tr>'
         + "".join(f'<th class="{c}" title="{html.escape(tip)}">{lab}</th>' for c, lab, tip in head) + "</tr>"]
    for _, r in d.iterrows():
        key = (f'<a href="/ticker/{r.ticker}">{r.ticker}</a>' if first_col == "ticker"
               else f'<a href="/day/{r.day}?t={r["asof"]}">{r.day}</a>')
        mine = news[news.ticker == r.ticker] if not news.empty else news
        if not mine.empty:
            snap = et(r.day, r["asof"])
            mine = mine[mine.ts >= prev_close(r.day)]
            before, after = mine[mine.ts <= snap], mine[(mine.ts > snap) & (mine.ts <= et(r.day, "16:00"))]
        else:
            before = after = mine
        named = int(before.company.sum()) if not before.empty else 0
        badge = f'<span class="badge" title="company headlines before {r["asof"]}">{named}</span>' if not before.empty else ""
        detail = (f'<div><p>{why(r, before)}</p><div class="cols"><div><b>Headlines before {r["asof"]}</b>'
                  f'{headline_list(before)}</div><div><b>Later that day</b>{headline_list(after, 8)}</div></div></div>')
        won = pd.notna(r.fwd_rs) and ((side == "call" and r.fwd_rs > 0) or (side == "put" and r.fwd_rs < 0))
        rid = f"d-{r.ticker}-{r.day}-{r['asof'].replace(':', '')}"
        h.append(f'<tr><td class="l">{key}</td><td>{pct(r.score, 1)}</td><td>{pct(r.rs)}</td>'
                 f'<td>{pct(r.trend)}</td><td class="hide-sm">{pct(r.gap)}</td>'
                 f'<td class="l hide-sm">{level_chips(r)}</td><td class="hide-sm">{r["last"]:.2f}</td>'
                 f'<td class="hind hide-sm">{pct(r.fwd_1h)}</td><td class="hide-sm">{pct(r.fwd_noon)}</td>'
                 f'<td class="hide-sm">{pct(r.fwd_close)}</td><td class="{"win" if won else ""}">{pct(r.fwd_rs)}</td>'
                 f'<td class="hide-sm">{pct(r.fwd_hi)}</td><td class="hide-sm">{pct(r.fwd_lo)}</td>'
                 f'<td class="l"><button class="tog" aria-expanded="false" aria-controls="{rid}">Why{badge}</button></td></tr>'
                 f'<tr class="detail" id="{rid}" hidden><td colspan="14">{detail}</td></tr>')
    h.append("</table></div>")
    return "".join(h)


@app.route("/day/<day>")
def day_view(day):
    t = request.args.get("t", DEFAULT_VIEW)
    d = query("SELECT * FROM scans WHERE day=? AND asof=? ORDER BY score DESC", day, t)
    times = query("SELECT DISTINCT asof FROM scans WHERE day=? ORDER BY asof", day)["asof"]
    if times.empty or d.empty:
        abort(404)
    news = load_news(list(d.ticker), prev_close(day), et(day, "16:00"))
    m = query("SELECT spy_gap, spy_trend FROM market WHERE day=? AND asof=?", day, t)
    tabs = "".join(f'<a class="{"on" if x == t else ""}" href="?t={x}">{x}</a>' for x in times)
    spy = f"SPY {pp(m.spy_gap[0])} (last {TREND_MIN} min {pp(m.spy_trend[0])})" if not m.empty else ""
    src = "recorded live" if (d.source == "live").any() else "rebuilt from 1-minute history"
    n = min(10, len(d) // 2)
    strong, weak, rest = d.head(n), d.tail(n).iloc[::-1], d.iloc[n:len(d) - n]
    body = (f"<h1>{day}</h1><div class='tabs'>{tabs}</div><p class='mute'>{spy} · {src}</p>"
            f"<details class='how'><summary>How to read this</summary>"
            f"<p><b>At {t}</b>: what you could see at the snapshot. Score ranks the list; RS vs SPY is the gap "
            f"beyond what SPY's move implies for the stock; {TREND_MIN}m vs SPY shows whether it's gaining or "
            f"losing ground right now; Level shows whether price has cleared the premarket or yesterday's "
            f"high/low (hover for prices).</p><p><b>After {t}</b>: what happened next, from the {t} price. "
            f"RS after, to close is highlighted when the stock moved the trade's way (up vs SPY for calls, "
            f"down vs SPY for puts). Price colours always mean up/down, not win/loss. Hover any column name "
            f"for its definition. <i>Why</i> opens the reasons and the headlines.</p></details>"
            f"<h2>Relative strength (calls)</h2>{ranking_table(strong, news, side='call')}"
            f"<h2>Relative weakness (puts)</h2>{ranking_table(weak, news, side='put')}"
            f"<details class='rest'><summary>The rest of the watchlist ({len(rest)}), not picked at {t}</summary>"
            f"{ranking_table(rest, news)}</details>")
    return page(f"RS {day}", body)


@app.route("/ticker/<ticker>")
def ticker_view(ticker):
    t = request.args.get("t", DEFAULT_VIEW)
    ticker = ticker.upper()
    d = query("SELECT * FROM scans WHERE ticker=? AND asof=? ORDER BY day DESC", ticker, t)
    if d.empty:
        abort(404)
    news = load_news([ticker], prev_close(d.day.min()), et(d.day.max(), "16:00"))
    tabs = "".join(f'<a class="{"on" if x == t else ""}" href="?t={x}">{x}</a>' for x in SNAPSHOTS)
    body = (f"<h1>{ticker}</h1><p class='mute'>Every recorded session at {t}.</p>"
            f"<div class='tabs'>{tabs}</div>{ranking_table(d, news, first_col='day')}")
    return page(f"RS {ticker}", body)


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "serve"
    if cmd == "backfill":
        print("Loading the last ~30 days from Yahoo...")
        catch_up(dt.datetime.now(rs.NY), force=True)
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
    main()
