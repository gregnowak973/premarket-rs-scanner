"""The local website: today's picks, a review of whether the ranking works, and per-stock history.

Pages
    /                 today's page on a trading morning, otherwise the review
    /day/today        today's ranking (refreshes itself as snapshots arrive)
    /day/<date>?t=    one session at one snapshot time
    /review?t=        does the ranking work, by snapshot time and by day
    /ticker/<T>       one stock across every recorded session
"""
from __future__ import annotations

import datetime as dt
import html
import math

import pandas as pd
from flask import Flask, abort, jsonify, redirect, render_template_string, request

import app as core
import premarket_rs as rs

SNAPSHOTS = core.SNAPSHOTS
TRADE_FROM = "09:35"          # first snapshot after the open; earlier ones are context only
DEFAULT_VIEW = core.DEFAULT_VIEW
TREND_MIN = core.TREND_MIN
TOP_N = core.TOP_N
CARDS = 2                     # pick cards per side
OUTCOME_SCALE = 3.0           # outcome bars span ±3%

web = Flask(__name__)


# ---------------------------------------------------------------- data ----

def query(sql: str, *args) -> pd.DataFrame:
    with core.db() as con:
        return pd.read_sql_query(sql, con, params=args)


def now_ny() -> dt.datetime:
    return dt.datetime.now(rs.NY)


def et(day: str, hhmm: str) -> pd.Timestamp:
    return pd.Timestamp(f"{day} {hhmm}", tz=rs.NY)


def prev_close(day: str) -> pd.Timestamp:
    """16:00 ET on the session before `day` (weekends skipped; holidays count as sessions)."""
    d = dt.date.fromisoformat(day) - dt.timedelta(days=1)
    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)
    return et(d.isoformat(), "16:00")


def all_days() -> list[str]:
    return list(query("SELECT DISTINCT day FROM scans ORDER BY day")["day"])


def times_for(day: str) -> list[str]:
    return list(query("SELECT DISTINCT asof FROM scans WHERE day=? ORDER BY asof", day)["asof"])


def is_live(day: str) -> bool:
    n = now_ny()
    return day == n.date().isoformat() and n.time() < dt.time(16, 5)


def next_snapshot(after: str) -> str | None:
    return next((s for s in SNAPSHOTS if s > after), None)


def load_news(tickers: list[str], start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    q = ",".join("?" * len(tickers))
    n = query(f"SELECT * FROM news WHERE ticker IN ({q}) AND ts >= ? AND ts <= ?", *tickers,
              start.tz_convert("UTC").isoformat(), end.tz_convert("UTC").isoformat())
    n["ts"] = pd.to_datetime(n.ts, utc=True, format="ISO8601").dt.tz_convert(rs.NY)
    return n.sort_values(["company", "ts"], ascending=[False, False])


def news_start() -> pd.Timestamp | None:
    first = query("SELECT MIN(ts) AS ts FROM news")["ts"].iloc[0]
    return pd.Timestamp(first).tz_convert(rs.NY) if first else None


def news_coverage() -> dict[str, pd.Timestamp]:
    """Earliest saved headline per ticker. Yahoo keeps ~100 stories, which is two days for a
    busy name and weeks for a quiet one, so 'no headlines' only means 'no news' after this."""
    c = query("SELECT ticker, MIN(ts) AS ts FROM news GROUP BY ticker")
    return {t: pd.Timestamp(ts).tz_convert(rs.NY) for t, ts in zip(c.ticker, c.ts)}


def covered(cov: dict, ticker: str, day: str) -> bool:
    return ticker in cov and cov[ticker] <= prev_close(day)


def company_news_counts(rows: pd.DataFrame) -> pd.Series:
    """Company headlines between the previous close and each snapshot, per row; NaN = no data."""
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
    first = n.groupby("ticker").ts.min()
    counts[(r.lo.values < r.ticker.map(first).values) | r.ticker.map(first).isna().values] = float("nan")
    return pd.Series(counts.values, index=rows.index)


def day_spreads(rows: pd.DataFrame) -> pd.DataFrame:
    """Per (day, snapshot): top-N minus bottom-N RS to the close."""
    out = []
    for (day, hhmm), d in rows.dropna(subset=["fwd_rs"]).groupby(["day", "asof"]):
        top, bot = d.nlargest(TOP_N, "score"), d.nsmallest(TOP_N, "score")
        out.append({"day": day, "asof": hhmm, "top": top.fwd_rs.mean(), "bot": bot.fwd_rs.mean(),
                    "spread": top.fwd_rs.mean() - bot.fwd_rs.mean(),
                    "top_news": top[top.news_n > 0].fwd_rs.mean(), "n_news": int((top.news_n > 0).sum()),
                    "top_nonews": top[top.news_n == 0].fwd_rs.mean(), "n_nonews": int((top.news_n == 0).sum())})
    return pd.DataFrame(out)


def snapshot_stats(ds: pd.DataFrame) -> pd.DataFrame:
    out = []
    for hhmm, s in ds.groupby("asof"):
        n = len(s)
        se = s.spread.std(ddof=1) / math.sqrt(n) if n > 1 else float("nan")
        out.append({"asof": hhmm, "days": n, "spread": s.spread.mean(), "se": se,
                    "hits": int((s.spread > 0).sum()), "top": s.top.mean(), "bot": s.bot.mean(),
                    "top_news": s.top_news.mean(), "n_news": int(s.n_news.sum()),
                    "top_nonews": s.top_nonews.mean(), "n_nonews": int(s.n_nonews.sum())})
    return pd.DataFrame(out).sort_values("asof") if out else pd.DataFrame()


def review_rows() -> pd.DataFrame:
    rows = query("SELECT day, asof, ticker, score, fwd_rs, news_n FROM scans")
    if not rows.empty:
        rows["news_n"] = company_news_counts(rows).fillna(rows.news_n).values
    return rows


# ------------------------------------------------------------ formatting ----

def esc(s) -> str:
    return html.escape(str(s))


def num(v, digits: int = 2, unit: str = "%", color: bool = False) -> str:
    """A signed number in text ink; exact zero muted; optional direction colour."""
    if v is None or pd.isna(v):
        return '<span class="mute">–</span>'
    v = round(float(v), digits) + 0.0
    if v == 0:
        return f'<span class="mute">{0:.{digits}f}{unit}</span>'
    cls = (" pos" if v > 0 else " neg") if color else ""
    return f'<span class="n{cls}">{v:+.{digits}f}{unit}</span>'


def bar(v, scale: float, cls: str = "", width: int = 64) -> str:
    """Diverging bar centred on zero. cls 'dir' colours by sign, otherwise neutral ink."""
    if v is None or pd.isna(v):
        return f'<span class="bar" style="width:{width}px"></span>'
    frac = max(-1.0, min(1.0, float(v) / scale)) / 2
    left, w = (50, frac * 100) if frac >= 0 else (50 + frac * 100, -frac * 100)
    tone = ("pos" if v > 0 else "neg") if cls == "dir" else "ink"
    return (f'<span class="bar" style="width:{width}px"><i class="{tone}" '
            f'style="left:{left:.1f}%;width:{w:.1f}%"></i></span>')


def result(v, side: str) -> str:
    """✓/✗ for whether the move went the trade's way (calls: beat SPY; puts: lagged SPY)."""
    if side not in ("call", "put") or v is None or pd.isna(v):
        return ""
    won = v > 0 if side == "call" else v < 0
    return ('<span class="res win" title="Went the trade\'s way">✓ won</span>' if won
            else '<span class="res loss" title="Went against the trade">✗ lost</span>')


def short_day(day: str) -> str:
    return f"{dt.date.fromisoformat(day):%a %b %-d}"


def long_day(day: str) -> str:
    return f"{dt.date.fromisoformat(day):%A, %B %-d, %Y}"


# ---------------------------------------------------------- explanations ----

def levels(r) -> list[tuple[str, str, float]]:
    """(tone, label, level price) for each level the price has cleared."""
    out = []
    if pd.notna(r.pm_hi) and r["last"] > r.pm_hi: out.append(("pos", "PM hi", r.pm_hi))
    if r["last"] > r.prev_hi: out.append(("pos", "Y hi", r.prev_hi))
    if pd.notna(r.pm_lo) and r["last"] < r.pm_lo: out.append(("neg", "PM lo", r.pm_lo))
    if r["last"] < r.prev_lo: out.append(("neg", "Y lo", r.prev_lo))
    return out


def level_chips(r) -> str:
    tip = f"Premarket {r.pm_hi:.2f} / {r.pm_lo:.2f} · Yesterday {r.prev_hi:.2f} / {r.prev_lo:.2f}"
    chips = "".join(f'<span class="lvl {t}">{"▲" if t == "pos" else "▼"} {lab}</span>' for t, lab, _ in levels(r))
    return f'<span title="{tip}">{chips or "<span class=lvl>inside</span>"}</span>'


def level_sentence(r) -> str:
    """Levels as prices, e.g. '510.70 · above PM hi 500.94 (+1.9%) · above Y hi 498.88 (+2.4%)'."""
    parts = [f"<b>{r['last']:.2f}</b>"]
    lv = levels(r)
    for tone, lab, px in lv:
        parts.append(f"{'above' if tone == 'pos' else 'below'} {lab} {px:.2f} ({(r['last'] / px - 1) * 100:+.1f}%)")
    if not lv:
        parts.append(f"inside PM {r.pm_lo:.2f}–{r.pm_hi:.2f} and Y {r.prev_lo:.2f}–{r.prev_hi:.2f}")
    return " · ".join(parts)


def why(r, before: pd.DataFrame, has_news_data: bool) -> str:
    bits = [f"Gap {r.gap:+.2f}% from yesterday's close, {r.rs:+.2f}% beyond what SPY's move implies."]
    if abs(r.trendz) >= 1:
        bits.append(f"{'Gaining on' if r.trend > 0 else 'Losing to'} SPY fast: {r.trend:+.2f}% in the last {TREND_MIN} min.")
    elif abs(r.trendz) >= 0.3:
        bits.append(f"{'Gaining on' if r.trend > 0 else 'Slipping vs'} SPY: {r.trend:+.2f}% in the last {TREND_MIN} min.")
    else:
        bits.append(f"Flat vs SPY over the last {TREND_MIN} min.")
    if r["div"]:
        bits.append("Moving opposite to SPY: " + ("up while SPY dipped." if r["div"].startswith("UP") else "down while SPY rose."))
    if not has_news_data:
        bits.append("No headlines saved for this day.")
    else:
        named = int(before.company.sum()) if not before.empty else 0
        bits.append(f"{named} company headline{'s' if named != 1 else ''} since yesterday's close.")
    return " ".join(bits)


def headline_items(n: pd.DataFrame, limit: int) -> str:
    items = []
    for _, x in n.head(limit).iterrows():
        title = esc(x.title)
        link = f'<a href="{esc(x.link)}" target="_blank" rel="noopener">{title}</a>' if x.link else title
        items.append(f'<li><span class="mute">{x.ts:%a %H:%M}</span> {link} '
                     f'<span class="mute">· {esc(x.publisher or "")}</span></li>')
    return "".join(items)


def headlines(n: pd.DataFrame, more_href: str, limit: int = 5) -> str:
    if n.empty:
        return '<p class="mute">None saved.</p>'
    company, sector = n[n.company == 1], n[n.company == 0]
    out = f"<ul>{headline_items(company, limit)}</ul>" if not company.empty else '<p class="mute">No company headlines.</p>'
    if len(company) > limit:
        out += f'<p class="mute"><a href="{more_href}">All {len(company)} company headlines →</a></p>'
    if not sector.empty:
        out += (f'<details class="sub"><summary>{len(sector)} sector / market headline'
                f'{"s" if len(sector) != 1 else ""}</summary><ul>{headline_items(sector, limit)}</ul></details>')
    return out


# ---------------------------------------------------------------- layout ----

BASE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ title }}</title>
<style>
:root{--bg:#f6f6f4;--card:#fff;--fg:#1c1c1e;--mute:#6b6b70;--line:#e2e2df;--acc:#2956cc;
  --pos:#1f63c6;--neg:#c25400;--win:#137a3a;--loss:#b3261e;--tint:#eef2fb}
@media (prefers-color-scheme:dark){:root{--bg:#131315;--card:#1c1c1f;--fg:#ececef;--mute:#9b9ba2;--line:#2f2f35;
  --acc:#86a6ff;--pos:#6ea8ff;--neg:#ff9a52;--win:#4cc27a;--loss:#ff7a70;--tint:#1f2433}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 -apple-system,system-ui,Segoe UI,Roboto,sans-serif}
a{color:var(--acc);text-decoration:none} a:hover{text-decoration:underline}
main{max-width:1180px;margin:0 auto;padding:16px 16px 56px}
h1{font-size:21px;margin:4px 0 2px} h2{font-size:15px;margin:26px 0 8px} .mute{color:var(--mute)}
.n,td,.num{font-variant-numeric:tabular-nums} .pos{color:var(--pos)} .neg{color:var(--neg)}
/* header */
header.top{position:sticky;top:0;z-index:10;background:color-mix(in srgb,var(--bg) 92%,transparent);
  backdrop-filter:blur(6px);border-bottom:1px solid var(--line)}
.bar1,.bar2{max-width:1180px;margin:0 auto;padding:8px 16px;display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.bar2{padding-top:0;overflow-x:auto;flex-wrap:nowrap}
.brand{font-weight:700;color:var(--fg)} .navlink{padding:3px 10px;border-radius:999px}
.navlink.on{background:var(--acc);color:#fff}
.stepper{display:flex;align-items:center;gap:2px} .stepper a,.stepper span{padding:3px 8px;border-radius:6px}
.stepper .cur{font-weight:600;border:1px solid var(--line);background:var(--card)}
.spacer{flex:1}
input.jump{width:120px;padding:4px 8px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--fg);font:inherit}
details.help{position:relative} details.help summary{list-style:none;cursor:pointer;width:26px;height:26px;border-radius:50%;
  border:1px solid var(--line);display:grid;place-items:center;background:var(--card)}
details.help .panel{position:absolute;right:0;top:32px;width:min(420px,90vw);background:var(--card);border:1px solid var(--line);
  border-radius:10px;padding:12px 14px;box-shadow:0 8px 24px #0002;font-size:13px}
.pills{display:flex;gap:4px;align-items:center;white-space:nowrap}
.pills .grp{font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:var(--mute);margin:0 2px 0 8px}
.pills a,.pills span.off{padding:3px 9px;border:1px solid var(--line);border-radius:999px;background:var(--card);font-size:13px}
.pills .ctx a{opacity:.75} .pills a.on{background:var(--acc);color:#fff;border-color:var(--acc);opacity:1}
.pills span.off{color:var(--mute);opacity:.5}
.pills .sep{width:1px;height:18px;background:var(--line);margin:0 6px}
.days{display:flex;gap:6px;overflow-x:auto;margin:10px 0 6px;padding-bottom:2px}
.days a{flex:none;padding:4px 10px;border-radius:8px;border:1px solid var(--line);background:var(--card);font-size:13px}
.days a.on{background:var(--acc);color:#fff;border-color:var(--acc);font-weight:600}
/* cards */
.card{background:var(--card);border:1px solid var(--line);border-radius:12px}
.banner{padding:10px 14px;border-radius:10px;background:var(--tint);margin:12px 0}
.verdict{padding:14px 16px;margin:10px 0 4px;font-size:15px} .verdict b{font-size:16px}
.picks{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:10px;margin:10px 0}
.pick{padding:12px 14px;display:flex;flex-direction:column;gap:5px}
.pick .hd{display:flex;align-items:baseline;gap:8px} .pick .tk{font-size:19px;font-weight:700}
.pick .side{font-size:11px;text-transform:uppercase;letter-spacing:.05em;padding:1px 7px;border-radius:999px;border:1px solid}
.side.call{color:var(--pos);border-color:var(--pos)} .side.put{color:var(--neg);border-color:var(--neg)}
.pick .kv{display:flex;gap:2px 14px;flex-wrap:wrap;font-size:13px} .pick .kv span b{font-size:15px}
.pick .lv,.pick .hl{font-size:13px} .pick .hl{color:var(--mute)}
.tag{font-size:12px;padding:1px 7px;border-radius:6px;background:var(--tint)}
.tag.warn{background:color-mix(in srgb,var(--loss) 14%,transparent)}
/* tables */
.tbl{overflow-x:auto} table{border-collapse:collapse;width:100%}
th,td{padding:7px 8px;text-align:right;white-space:nowrap;border-bottom:1px solid var(--line)}
th{font-weight:600;color:var(--mute);font-size:12px;vertical-align:bottom} tr:last-child td{border-bottom:0}
.l{text-align:left!important} td.tk a{font-weight:600}
table.rank td:first-child,table.rank th:first-child{position:sticky;left:0;background:var(--card);z-index:1}
tr.hl td{background:var(--tint)} tr.hl td:first-child{box-shadow:inset 3px 0 var(--acc)}
tr.won td:first-child{box-shadow:inset 3px 0 var(--win)} tr.lost td:first-child{box-shadow:inset 3px 0 var(--loss)}
.bar{display:inline-block;position:relative;height:10px;vertical-align:middle;margin-right:6px;
  background:linear-gradient(var(--line),var(--line)) center/1px 100% no-repeat}
.bar i{position:absolute;top:1px;bottom:1px;border-radius:2px} .bar i.ink{background:color-mix(in srgb,var(--fg) 45%,transparent)}
.bar i.pos{background:var(--pos)} .bar i.neg{background:var(--neg)}
.res{font-size:12px;font-weight:600;white-space:nowrap} .res.win{color:var(--win)} .res.loss{color:var(--loss)}
.lvl{display:inline-block;font-size:12px;padding:0 6px;margin-right:3px;border-radius:999px;border:1px solid var(--line);color:var(--mute)}
.lvl.pos{color:var(--pos);border-color:color-mix(in srgb,var(--pos) 45%,transparent)}
.lvl.neg{color:var(--neg);border-color:color-mix(in srgb,var(--neg) 45%,transparent)}
.tog{font:inherit;font-size:13px;color:var(--acc);background:none;border:1px solid var(--line);border-radius:6px;padding:2px 8px;cursor:pointer}
.tog[aria-expanded=true]{background:var(--acc);color:#fff;border-color:var(--acc)}
.badge{display:inline-block;min-width:16px;margin-left:4px;padding:0 5px;border-radius:999px;background:var(--line);color:var(--fg);font-size:11px}
.badge.none{opacity:.5}
tr.detail>td{white-space:normal;text-align:left;background:var(--bg);padding:12px 14px}
tr.detail>td>div{position:sticky;left:14px;max-width:min(1100px,calc(100vw - 70px))}
tr.detail p{margin:0 0 8px} tr.detail ul{margin:4px 0 6px;padding-left:18px} tr.detail li{margin:3px 0}
.facts{display:flex;flex-wrap:wrap;gap:4px 18px;margin:0 0 10px;font-size:13px} .facts span{white-space:nowrap}
.cols{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:4px 28px}
details.sub summary,details.later summary{cursor:pointer;color:var(--acc);font-size:13px;margin:4px 0}
.chips{display:flex;gap:6px;flex-wrap:wrap;margin:14px 0 4px}
.chips button{font:inherit;font-size:13px;padding:3px 10px;border-radius:999px;border:1px solid var(--line);background:var(--card);color:var(--fg);cursor:pointer}
.chips button[aria-pressed=true]{background:var(--fg);color:var(--bg);border-color:var(--fg)}
details.rest>summary{cursor:pointer;color:var(--mute);margin:22px 0 8px}
.legend{font-size:12px;color:var(--mute);margin:4px 0 8px}
/* charts */
.chart{padding:10px 12px;max-width:640px} .chart svg{display:block;width:100%}
svg text{fill:var(--fg);font:12px -apple-system,system-ui,sans-serif} svg .mute{fill:var(--mute)}
.heat{display:grid;gap:2px;align-items:center;font-size:12px}
.heat a{display:block;height:16px;border-radius:2px} .heat .lab{color:var(--mute);padding-right:6px;white-space:nowrap}
footer{margin-top:36px;font-size:12px;color:var(--mute)}
kbd{font:11px ui-monospace,monospace;border:1px solid var(--line);border-radius:4px;padding:0 4px;background:var(--bg)}
@media(max-width:640px){main{padding:12px 10px 48px} .hide-sm{display:none} th{white-space:normal}
  th,td{padding:6px 5px} input.jump{width:84px} .stepper .cur{font-size:13px} h1{font-size:19px}}
</style></head><body data-page="{{ page }}" data-prev="{{ prev or '' }}" data-next="{{ next or '' }}"
 data-tprev="{{ tprev or '' }}" data-tnext="{{ tnext or '' }}" data-live="{{ live or '' }}">
<header class="top"><div class="bar1">
  <a class="brand" href="/review">RS Scanner</a>
  <a class="navlink {{ 'on' if page=='today' else '' }}" href="/day/today">Today</a>
  <a class="navlink {{ 'on' if page=='review' else '' }}" href="/review">Review</a>
  {{ stepper|safe }}
  <span class="spacer"></span>
  <form action="/go" method="get"><input class="jump" name="q" list="tickers" placeholder="Ticker  /" aria-label="Go to ticker" autocomplete="off">
    <datalist id="tickers">{% for t in tickers %}<option value="{{ t }}">{% endfor %}</datalist></form>
  <details class="help"><summary aria-label="Help">?</summary><div class="panel">
    <p><b>Score</b> ranks the list: how far the stock is ahead of (or behind) what SPY's move implies, plus whether that gap is growing, scaled by the stock's normal volatility. ±1 is notable, ±3 strong.</p>
    <p><b>RS vs SPY</b>: move from yesterday's close beyond what SPY implies (beta-adjusted). <b>{{ trend }}m</b>: change in that over the last {{ trend }} minutes.</p>
    <p><b>Levels</b>: whether price has cleared the premarket (PM) or yesterday's (Y) high or low.</p>
    <p><b>RS → close</b>: what happened after the snapshot, vs SPY. <b>✓ won</b> means it moved the trade's way (calls up vs SPY, puts down). Blue/orange always mean up/down vs SPY, not good/bad.</p>
    <p><b>Premarket snapshots</b> (before 09:30) are context only. In the log they haven't predicted the day; 09:35 onward has. See Review.</p>
    <p class="mute">Keys: <kbd>←</kbd><kbd>→</kbd> day · <kbd>[</kbd><kbd>]</kbd> snapshot · <kbd>/</kbd> ticker · <kbd>t</kbd> today · <kbd>r</kbd> review · <kbd>Esc</kbd> close rows</p>
  </div></details>
</div>{% if pills %}<div class="bar2">{{ pills|safe }}</div>{% endif %}</header>
<main>
{{ body|safe }}
<footer>{{ footer|safe }} · Version {{ version }}</footer>
</main>
<script>
const $ = (s, e = document) => e.querySelector(s), $$ = (s, e = document) => [...e.querySelectorAll(s)];
function openRow(b, open) {
  const row = document.getElementById(b.getAttribute("aria-controls")); if (!row) return;
  row.hidden = !open; b.setAttribute("aria-expanded", String(open));
}
document.addEventListener("click", e => {
  const b = e.target.closest(".tog"); if (b) {
    const open = b.getAttribute("aria-expanded") !== "true"; openRow(b, open);
    history.replaceState(null, "", open ? "#" + b.dataset.key : location.pathname + location.search);
    return;
  }
  const c = e.target.closest(".chips button"); if (c) {
    $$(".chips button").forEach(x => x.setAttribute("aria-pressed", String(x === c)));
    const f = c.dataset.f;
    $$("tr[data-row]").forEach(tr => {
      const show = !f || tr.dataset[f] === "1"; tr.hidden = !show;
      const d = tr.nextElementSibling; if (d && d.classList.contains("detail") && !show) d.hidden = true;
    });
  }
});
// Reopen the row named in the URL hash (#MSFT) so links and Back land on it.
function openHash() {
  if (!location.hash) return;
  const b = $(`.tog[data-key="${CSS.escape(decodeURIComponent(location.hash.slice(1)))}"]`);
  if (b) { const tr = b.closest("tr"); tr.hidden = false;
    const det = b.closest("details"); if (det) det.open = true;
    openRow(b, true); tr.scrollIntoView({block: "center"}); }
}
openHash(); window.addEventListener("hashchange", openHash);
const on = $(".days a.on"); if (on) on.scrollIntoView({inline: "center", block: "nearest"});
document.addEventListener("keydown", e => {
  if (e.target.closest("input,textarea,select") || e.metaKey || e.ctrlKey || e.altKey) return;
  const d = document.body.dataset, go = u => u && (location.href = u);
  if (e.key === "ArrowLeft") go(d.prev); else if (e.key === "ArrowRight") go(d.next);
  else if (e.key === "[") go(d.tprev); else if (e.key === "]") go(d.tnext);
  else if (e.key === "/") { e.preventDefault(); $("input.jump").focus(); }
  else if (e.key === "t") go("/day/today"); else if (e.key === "r") go("/review");
  else if (e.key === "Escape") $$(".tog[aria-expanded=true]").forEach(b => openRow(b, false));
});
// Live page: check every 30s for a new snapshot and reload onto it.
if (document.body.dataset.live) {
  const [day, shown, following] = document.body.dataset.live.split("|");
  setInterval(async () => {
    try {
      const s = await (await fetch(`/api/latest?day=${day}`)).json();
      if (s.latest && s.latest !== shown) {
        if (following === "1") location.href = `/day/today${location.hash}`;
        else { const n = $("#newsnap"); if (n) { n.hidden = false; n.querySelector("b").textContent = s.latest; } }
      }
    } catch (_) {}
  }, 30000);
}
const cd = $("[data-countdown]");
if (cd) { const tgt = new Date(cd.dataset.countdown).getTime();
  const tick = () => { const s = Math.max(0, Math.round((tgt - Date.now()) / 1000));
    cd.textContent = s ? `in ${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}` : "any moment"; };
  tick(); setInterval(tick, 1000); }
</script></body></html>"""


def page(title: str, body: str, *, page: str = "", pills: str = "", stepper: str = "", footer: str = "",
         prev: str = "", next: str = "", tprev: str = "", tnext: str = "", live: str = "") -> str:
    return render_template_string(
        BASE, title=title, body=body, page=page, pills=pills, stepper=stepper, footer=footer,
        prev=prev, next=next, tprev=tprev, tnext=tnext, live=live, version=core.VERSION,
        tickers=core.load_tickers(), trend=TREND_MIN)


def snapshot_pills(href, available: list[str], current: str | None) -> str:
    """Snapshot times, grouped into premarket context and tradeable; missing ones greyed out."""
    def pill(s):
        if s not in available:
            return f'<span class="off" title="Not recorded">{s}</span>'
        return f'<a class="{"on" if s == current else ""}" href="{href(s)}">{s}</a>'
    ctx = "".join(pill(s) for s in SNAPSHOTS if s < TRADE_FROM)
    trade = "".join(pill(s) for s in SNAPSHOTS if s >= TRADE_FROM)
    return (f'<div class="pills"><span class="grp">Premarket · context</span><span class="pills ctx">{ctx}</span>'
            f'<span class="sep"></span><span class="grp">Tradeable</span>{trade}</div>')


def neighbours(items: list[str], cur: str) -> tuple[str | None, str | None]:
    if cur not in items:
        return None, None
    i = items.index(cur)
    return (items[i - 1] if i > 0 else None), (items[i + 1] if i < len(items) - 1 else None)


# ------------------------------------------------------------------ home ----

@web.route("/")
def home():
    today = now_ny().date().isoformat()
    if is_live(today) and dt.date.fromisoformat(today).weekday() < 5:
        return redirect("/day/today")
    return redirect("/review")


@web.route("/go")
def go():
    q = (request.args.get("q") or "").strip().upper()
    return redirect(f"/ticker/{q}" if q else "/review")


@web.route("/api/latest")
def api_latest():
    t = times_for(request.args.get("day", ""))
    return jsonify(latest=t[-1] if t else None)


# ---------------------------------------------------------------- review ----

def dot_plot(stats: pd.DataFrame) -> str:
    """Spread per snapshot with ±1 standard error, noise band, and hit counts."""
    if stats.empty:
        return ""
    lo = min(-1.0, (stats.spread - 2 * stats.se.fillna(0)).min() * 1.1)
    hi = max(1.0, (stats.spread + 2 * stats.se.fillna(0)).max() * 1.1)
    W, L, R, top, rh = 480, 78, 62, 26, 28
    H = top + rh * len(stats) + 18
    x = lambda v: L + (v - lo) / (hi - lo) * (W - L - R)
    out = [f'<svg viewBox="0 0 {W} {H}" width="100%" role="img" aria-label="Spread by snapshot time">',
           f'<line x1="{x(0):.1f}" x2="{x(0):.1f}" y1="{top - 10}" y2="{top + rh * len(stats) - 4}" stroke="var(--fg)" opacity=".45"/>']
    for tick in sorted({round(lo), 0, round(hi)} | {v for v in (-2, -1, 1, 2) if lo < v < hi}):
        out.append(f'<text x="{x(tick):.1f}" y="{H - 2}" text-anchor="middle" class="mute">{tick:+d}%</text>')
    out.append(f'<text x="{x(0):.1f}" y="12" text-anchor="middle" class="mute">strong − weak, RS to close</text>')
    out.append(f'<text x="{W - R + 8}" y="12" class="mute">won</text>')
    for i, s in enumerate(stats.itertuples()):
        y = top + i * rh + 6
        ctx = s.asof < TRADE_FROM
        clear = pd.notna(s.se) and s.spread - 2 * s.se > 0
        col = "var(--acc)" if clear else "var(--fg)"
        out.append(f'<a href="/review?t={s.asof}"><text x="8" y="{y + 4}" class="{"mute" if ctx else ""}">'
                   f'{s.asof}{" pre" if ctx else ""}</text></a>')
        if pd.notna(s.se):
            out.append(f'<line x1="{x(s.spread - 2 * s.se):.1f}" x2="{x(s.spread + 2 * s.se):.1f}" y1="{y}" y2="{y}" '
                       f'stroke="{col}" stroke-width="2" opacity=".6"/>')
        out.append(f'<circle cx="{x(s.spread):.1f}" cy="{y}" r="{5.5 if clear else 4.5}" fill="{col}"'
                   f'{"" if not ctx else " opacity=.55"}><title>{s.asof}: {s.spread:+.2f}%, likely range {s.spread - 2 * s.se:+.2f} to {s.spread + 2 * s.se:+.2f}</title></circle>')
        out.append(f'<text x="{x(s.spread):.1f}" y="{y - 8}" text-anchor="middle" class="mute" font-size="11">{s.spread:+.2f}</text>')
        out.append(f'<text x="{W - R + 8}" y="{y + 4}" class="{"" if clear else "mute"}">{s.hits}/{s.days}</text>')
    out.append("</svg>")
    return "".join(out)


def heatmap(ds: pd.DataFrame, current: str) -> str:
    """Days × snapshots, each cell that day's spread (blue: strong beat weak; orange: the reverse)."""
    if ds.empty:
        return ""
    days = sorted(ds.day.unique())
    cells = [f'<div class="heat" style="grid-template-columns:auto repeat({len(days)},minmax(8px,1fr))">']
    for s in SNAPSHOTS:
        g = ds[ds["asof"] == s].set_index("day")
        if g.empty:
            continue
        cells.append(f'<span class="lab">{s}</span>')
        for d in days:
            v = g.spread.get(d)
            if v is None or pd.isna(v):
                cells.append("<span></span>")
                continue
            a = min(abs(v) / 2, 1) * 85 + 8
            color = f"color-mix(in srgb,var(--{'pos' if v > 0 else 'neg'}) {a:.0f}%,var(--card))"
            ring = ";outline:2px solid var(--fg)" if s == current else ""
            cells.append(f'<a href="/day/{d}?t={s}" style="background:{color}{ring}" '
                         f'title="{short_day(d)} {s}: {v:+.2f}%"></a>')
    cells.append('<span></span>' + "".join(
        f'<span class="mute" style="font-size:10px;text-align:center">{dt.date.fromisoformat(d):%-d}</span>' for d in days))
    cells.append("</div>")
    return "".join(cells)


def verdict(stats: pd.DataFrame, n_days: int) -> str:
    pre, trade = stats[stats["asof"] < TRADE_FROM], stats[stats["asof"] >= TRADE_FROM]
    if trade.empty:
        return ""
    clear = trade[(trade.spread - 2 * trade.se) > 0]
    t_bits = ", ".join(f"{s.hits}/{s.days} days at {s.asof}" for s in trade.itertuples())
    p_rng = f"{pre.hits.min()}–{pre.hits.max()} of {pre.days.max()}" if not pre.empty else "n/a"
    head = ("Act from 09:35, not premarket." if not clear.empty and (pre.spread - 2 * pre.se).max() <= 0
            else "No snapshot time clearly works yet." if clear.empty else "Several snapshot times look positive.")
    return (f'<div class="card verdict"><b>{head}</b><br>Strong names beat weak ones on {t_bits}; '
            f"premarket snapshots {p_rng}. Each line is the likely range for that time (±2 standard errors, "
            f"{n_days} days); blue dots are the times whose whole range is above zero. "
            f"Clear so far: {', '.join(clear['asof']) or 'none'}.</div>")


@web.route("/review")
def review():
    t = request.args.get("t", DEFAULT_VIEW)
    rows = review_rows()
    if rows.empty:
        return page("RS Scanner", "<h1>No sessions yet</h1><p>Run <code>./setup.sh</code> to load the last month.</p>",
                    page="review")
    ds = day_spreads(rows)
    stats = snapshot_stats(ds)
    n_days = rows.day.nunique()
    viewed = request.args.get("day", "")
    body = [f"<h1>Does the ranking work?</h1>{verdict(stats, n_days)}",
            f'<div class="card chart" style="margin-top:10px">{dot_plot(stats)}</div>',
            f"<h2>Every session, every snapshot</h2>"
            f'<p class="legend">Each square is one day: <span class="pos">blue</span> = the top {TOP_N} beat the '
            f'bottom {TOP_N} to the close, <span class="neg">orange</span> = the reverse, darker = bigger. '
            f"Click a square to open that day.</p>"
            f'<div class="card" style="padding:12px">{heatmap(ds, t)}</div>']

    # Sessions list for one snapshot time
    view = rows[rows["asof"] == t]
    market = query("SELECT day, spy_gap FROM market WHERE asof=?", t).set_index("day")
    tabs = snapshot_pills(lambda s: f"/review?t={s}#sessions", list(stats["asof"]), t)
    body.append(f'<h2 id="sessions">Sessions at {t}</h2>{tabs}'
                '<div class="card tbl" style="margin-top:8px"><table><tr><th class="l">Day</th>'
                f'<th class="l" title="Top {TOP_N} minus bottom {TOP_N}, RS to the close">Strong − weak</th>'
                "<th class='l'>Top 3 (calls) · Bottom 3 (puts)</th><th class='hide-sm'>SPY gap</th></tr>")
    for day in sorted(view.day.unique(), reverse=True):
        d = view[view.day == day]
        top, bot = d.nlargest(3, "score"), d.nsmallest(3, "score")
        spread = d.nlargest(TOP_N, "score").fwd_rs.mean() - d.nsmallest(TOP_N, "score").fwd_rs.mean()
        mark = lambda r, side: ('<span class="res win">✓</span>' if (r.fwd_rs > 0) == (side == "call") else
                                '<span class="res loss">✗</span>') if pd.notna(r.fwd_rs) else ""
        names = lambda g, side: " ".join(f'<a href="/ticker/{r.ticker}?day={day}&t={t}">{r.ticker}</a>{mark(r, side)}'
                                         for r in g.itertuples())
        body.append(f'<tr id="d-{day}" class="{"hl" if day == viewed else ""}">'
                    f'<td class="l"><a href="/day/{day}?t={t}">{short_day(day)}</a></td>'
                    f'<td class="l">{bar(spread, 3, "dir", 80)}{num(spread)}</td>'
                    f'<td class="l">▲ {names(top, "call")}<br>▼ {names(bot, "put")}</td>'
                    f'<td class="hide-sm mute">{num(market.spy_gap.get(day))}</td></tr>')
    body.append("</table></div>")

    # Full numbers, including the news split, for the curious
    tr = "".join(
        f'<tr><td class="l">{s.asof}</td><td>{num(s.spread)}</td><td>±{s.se:.2f}</td><td>{s.hits}/{s.days}</td>'
        f"<td>{num(s.top)}</td><td>{num(s.bot)}</td><td>{num(s.top_news)} <span class=mute>n={s.n_news}</span></td>"
        f"<td>{num(s.top_nonews)} <span class=mute>n={s.n_nonews}</span></td></tr>" for s in stats.itertuples())
    body.append(
        "<details style='margin-top:22px'><summary class='mute' style='cursor:pointer'>All the numbers</summary>"
        '<div class="card tbl" style="margin-top:8px"><table><tr><th class="l">Snapshot</th><th>Spread</th>'
        f"<th>± s.e.</th><th>Days won</th><th>Top {TOP_N}</th><th>Bottom {TOP_N}</th>"
        f"<th>Top {TOP_N} with company news</th><th>…without</th></tr>{tr}</table></div>"
        "<p class='legend'>RS to the close, vs SPY. 'With company news' = at least one headline naming the "
        "company since the previous close; big companies have one most days, so this barely separates "
        "anything yet. n = stock-days.</p></details>")
    return page("Review · RS Scanner", "".join(body), page="review",
                footer=f"{n_days} sessions · headlines saved since "
                       f"{news_start():%b %-d}" if news_start() is not None else f"{n_days} sessions")


# ------------------------------------------------------------------- day ----

def persistence(day: str, t: str) -> dict[str, str]:
    """For picks at t (after 09:35): did they stay in the top/bottom N at every tradeable snapshot so far?"""
    if t <= TRADE_FROM:
        return {}
    d = query("SELECT asof, ticker, score FROM scans WHERE day=? AND asof>=? AND asof<=?", day, TRADE_FROM, t)
    tops = {a: set(g.nlargest(TOP_N, "score").ticker) for a, g in d.groupby("asof")}
    bots = {a: set(g.nsmallest(TOP_N, "score").ticker) for a, g in d.groupby("asof")}
    out = {}
    for sets, side in ((tops, "call"), (bots, "put")):
        times = sorted(sets)
        for tk in sets.get(t, set()):
            held = [a for a in times if tk in sets[a]]
            out[f"{side}:{tk}"] = ("held" if held == times else f"new since {held[0]}" if held[0] != times[0]
                                   else "in and out")
    return out


def pick_card(r, side: str, before: pd.DataFrame, has_news: bool, pers: str, hindsight: bool, t: str) -> str:
    arrow = "↑" if r.trend > 0 else "↓"
    company = before[before.company == 1] if not before.empty else before
    if not has_news:
        hl = "No headlines saved for this day."
    elif company.empty:
        hl = "No company headlines since yesterday's close."
    else:
        x = company.sort_values("ts", ascending=False).iloc[0]
        hl = (f'{x.ts:%H:%M} <a href="{esc(x.link)}" target="_blank" rel="noopener">{esc(x.title)}</a>'
              f' <span class="mute">({len(company)} company headline{"s" if len(company) != 1 else ""})</span>')
    tag = ""
    if pers == "held":
        tag = f'<span class="tag">Held top {TOP_N} since {TRADE_FROM}</span>'
    elif pers:
        tag = f'<span class="tag warn">{"New" if pers.startswith("new") else "Unsteady"}: {pers}</span>'
    out = ""
    if hindsight and pd.notna(r.fwd_rs):
        out = (f'<div class="kv">{result(r.fwd_rs, side)} <span>RS → close {num(r.fwd_rs)}</span>'
               f'<span>+1h {num(r.fwd_1h)}</span><span>max up {num(r.fwd_hi)} / down {num(r.fwd_lo)}</span></div>')
    return (f'<div class="card pick"><div class="hd"><a class="tk" href="/ticker/{r.ticker}?day={r.day}&t={t}">'
            f'{r.ticker}</a><span class="side {side}">{side}</span>{tag}</div>'
            f'<div class="kv"><span>Score <b>{r.score:+.1f}</b></span><span>RS vs SPY <b>{r.rs:+.2f}%</b></span>'
            f'<span>{TREND_MIN}m {arrow} {r.trend:+.2f}%</span></div>'
            f'<div class="lv">{level_sentence(r)}</div><div class="hl">{hl}</div>{out}</div>')


def rank_table(d: pd.DataFrame, news: pd.DataFrame, has_news, t: str, side: str, hindsight: bool,
               pers: dict, score_scale: float, first: str = "ticker", viewed: str = "") -> str:
    head = [("l", "Ticker" if first == "ticker" else "Day", ""),
            ("l", "Score", "±1 notable, ±3 strong"),
            ("", "RS vs SPY", "Move from yesterday's close beyond what SPY's move implies"),
            ("", f"{TREND_MIN}m", f"Change in RS vs SPY over the last {TREND_MIN} minutes"),
            ("l", "Levels", "Cleared the premarket (PM) or yesterday's (Y) high/low; hover for prices")]
    if hindsight:
        head += [("l", "RS → close", "After the snapshot, vs SPY; bar spans ±3%"), ("l", "", "")]
    head += [("l", "", "")]
    news_ok = has_news if callable(has_news) else (lambda _day, _tk: has_news)
    h = ['<div class="card tbl"><table class="rank"><tr>' +
         "".join(f'<th class="{c}" title="{esc(tip)}">{lab}</th>' for c, lab, tip in head) + "</tr>"]
    for r in d.itertuples():
        r = d.loc[r.Index]
        mine = news[news.ticker == r.ticker] if not news.empty else news
        if not mine.empty:
            snap = et(r.day, r["asof"])
            mine = mine[mine.ts >= prev_close(r.day)]
            before, after = mine[mine.ts <= snap], mine[(mine.ts > snap) & (mine.ts <= et(r.day, "16:00"))]
        else:
            before = after = mine
        n_company = int(before.company.sum()) if not before.empty else 0
        row_side = side or ("call" if r.score > 0 else "put")
        key = r.ticker if first == "ticker" else r.day
        rid = f"row-{key}-{r['asof'].replace(':', '')}"
        clean = any(tn == ("pos" if row_side == "call" else "neg") for tn, _, _ in levels(r))
        p = pers.get(f"{row_side}:{r.ticker}", "")
        won = hindsight and pd.notna(r.fwd_rs) and side and ((r.fwd_rs > 0) == (side == "call"))
        cls = " ".join(c for c in ("hl" if key == viewed else "",
                                    ("won" if won else "lost") if hindsight and side and pd.notna(r.fwd_rs) else "") if c)
        label = (f'<a href="/ticker/{r.ticker}?day={r.day}&t={t}">{r.ticker}</a>' if first == "ticker"
                 else f'<a href="/day/{r.day}?t={t}#{r.ticker}">{short_day(r.day)}</a>')
        row_news = news_ok(r.day, r.ticker)
        badge = (f'<span class="badge">{n_company}</span>' if row_news
                 else '<span class="badge none" title="No headlines saved for this day">–</span>')
        cells = [f'<td class="l tk">{label}</td>',
                 f'<td class="l">{bar(r.score, score_scale)}{num(r.score, 1, "")}</td>',
                 f"<td>{num(r.rs)}</td><td>{num(r.trend)}</td>",
                 f'<td class="l">{level_chips(r)}</td>']
        if hindsight:
            cells += [f'<td class="l">{bar(r.fwd_rs, OUTCOME_SCALE, "dir", 72)}{num(r.fwd_rs)}</td>',
                      f'<td class="l">{result(r.fwd_rs, side)}</td>']
        cells.append(f'<td class="l"><button class="tog" aria-expanded="false" aria-controls="{rid}" '
                     f'data-key="{key}">Why{badge}</button></td>')
        facts = (f'<div class="facts"><span>Last <b>{r["last"]:.2f}</b></span><span>Gap {num(r.gap)}</span>'
                 f"<span>PM hi/lo {r.pm_hi:.2f} / {r.pm_lo:.2f}</span><span>Y hi/lo {r.prev_hi:.2f} / {r.prev_lo:.2f}</span>")
        if hindsight:
            facts += (f"<span>+1h {num(r.fwd_1h)}</span><span>Noon {num(r.fwd_noon)}</span>"
                      f"<span>Close {num(r.fwd_close)}</span><span>Max up {num(r.fwd_hi)} · down {num(r.fwd_lo)}</span>")
        facts += (f'<span>{("Held since " + TRADE_FROM) if p == "held" else p}</span>' if p else "") + "</div>"
        more = f"/ticker/{r.ticker}?day={r.day}&t={t}"
        detail = (f"<div><p>{why(r, before, row_news)}</p>{facts}"
                  f'<div class="cols"><div><b>Headlines before {r["asof"]}</b>{headlines(before, more)}</div>'
                  + (f'<div><details class="later"><summary>Later that day ({len(after)})</summary>'
                     f"{headlines(after, more)}</details></div>" if hindsight else "")
                  + f'</div><p class="mute" style="margin-top:8px"><a href="{more}">{r.ticker} on every day →</a></p></div>')
        h.append(f'<tr class="{cls}" data-row="1" data-news="{int(n_company > 0)}" data-clean="{int(clean)}" '
                 f'data-held="{int(p == "held")}">{"".join(cells)}</tr>'
                 f'<tr class="detail" id="{rid}" hidden><td colspan="{len(head)}">{detail}</td></tr>')
    h.append("</table></div>")
    return "".join(h)


def waiting_page(today: str) -> str:
    n = now_ny()
    nxt = next((s for s in SNAPSHOTS if s > n.strftime("%H:%M")), None)
    if n.weekday() >= 5 or nxt is None:
        return redirect("/review")
    when = et(today, nxt)
    body = (f"<h1>{long_day(today)}</h1><div class='banner'>No snapshot yet today. The first one is at "
            f"<b>{nxt} ET</b> (<span data-countdown='{when.isoformat()}'></span>); this page updates itself.</div>"
            "<p class='mute'>If nothing appears by then, check that the recorder is running: "
            "<code>.venv/bin/python app.py status</code>.</p>")
    return page("Today · RS Scanner", body, page="today", live=f"{today}||1")


@web.route("/day/<day>")
def day_view(day):
    following = day == "today"
    if following:
        day = now_ny().date().isoformat()
    times = times_for(day)
    if not times:
        if following:
            return waiting_page(day)
        abort(404)
    live = is_live(day)
    t = request.args.get("t") or (times[-1] if live else DEFAULT_VIEW if DEFAULT_VIEW in times else times[-1])
    if t not in times:
        t = max([s for s in times if s <= t] or times[:1])
    if request.args.get("t"):
        following = False
    d = query("SELECT * FROM scans WHERE day=? AND asof=? ORDER BY score DESC", day, t)
    news = load_news(list(d.ticker), prev_close(day), et(day, "16:00"))
    cov = news_coverage()
    has_news = lambda day_, tk: covered(cov, tk, day_)
    hindsight = not live and d.fwd_rs.notna().any()
    m = query("SELECT spy_gap, spy_trend FROM market WHERE day=? AND asof=?", day, t)
    pers = persistence(day, t)
    scale = max(3.0, d.score.abs().max())

    days = all_days()
    p_day, n_day = neighbours(days, day)
    p_t, n_t = neighbours(times, t)
    href = lambda s: f"/day/{day}?t={s}"
    stepper = (f'<span class="stepper">{"<a href=/day/" + p_day + "?t=" + t + " title=Previous day>‹</a>" if p_day else ""}'
               f'<span class="cur">{short_day(day)}</span>'
               f'{"<a href=/day/" + n_day + "?t=" + t + " title=Next day>›</a>" if n_day else ""}</span>')
    strip = "".join(f'<a class="{"on" if x == day else ""}" href="/day/{x}?t={t}">{short_day(x)}</a>'
                    for x in days[max(0, days.index(day) - 6):days.index(day) + 7])

    spy = f"SPY {num(m.spy_gap[0], color=True)} · last {TREND_MIN}m {num(m.spy_trend[0], color=True)}" if not m.empty else ""
    status = ""
    if live:
        nxt = next_snapshot(times[-1])
        status = (f' · <b>live</b>, updated {times[-1]}' + (f", next {nxt}" if nxt else "")
                  + " · outcomes fill in after the close")
    body = [f'<div class="days">{strip}</div><h1>{long_day(day)} · {t}</h1><p class="mute">{spy}{status}</p>',
            '<div id="newsnap" class="banner" hidden>New snapshot <b></b> is in. '
            '<a href="/day/today">Show it →</a></div>']
    if t < TRADE_FROM:
        st = snapshot_stats(day_spreads(review_rows()))
        s = st[st["asof"] == t]
        record = f" In the log, strong beat weak from {t} on only {s.hits.iloc[0]}/{s.days.iloc[0]} days." if not s.empty else ""
        count = (f" First tradeable ranking at {TRADE_FROM} (<span data-countdown='{et(day, TRADE_FROM).isoformat()}'></span>)."
                 if live else "")
        body.append(f'<div class="banner"><b>Premarket: context only, don\'t act on this ranking yet.</b>{record}{count}</div>')

    strong, weak = d.head(TOP_N), d.tail(TOP_N).iloc[::-1]
    rest = d.iloc[TOP_N:len(d) - TOP_N]
    news_for = lambda tk: news[news.ticker == tk] if not news.empty else news
    cards = []
    for g, side in ((strong, "call"), (weak, "put")):
        for r in g.head(CARDS).itertuples():
            r = g.loc[r.Index]
            b = news_for(r.ticker)
            b = b[(b.ts >= prev_close(day)) & (b.ts <= et(day, t))] if not b.empty else b
            cards.append(pick_card(r, side, b, has_news(day, r.ticker), pers.get(f"{side}:{r.ticker}", ""), hindsight, t))
    body.append(f'<h2>{"Watch list (premarket)" if t < TRADE_FROM else "Top picks"}</h2><div class="picks">{"".join(cards)}</div>')

    body.append('<div class="chips" role="group" aria-label="Filter"><button aria-pressed="true" data-f="">All</button>'
                '<button aria-pressed="false" data-f="news">Company news</button>'
                '<button aria-pressed="false" data-f="clean">Cleared a level</button>'
                + ('<button aria-pressed="false" data-f="held">Held since 09:35</button>' if t > TRADE_FROM else "")
                + "</div>")
    legend = ("Bars: score (grey, same scale in both tables) and RS → close (blue up / orange down vs SPY, ±3%). "
              "✓ = moved the trade's way." if hindsight else "Bars show the score on one scale for both tables.")
    body.append(f'<p class="legend">{legend}</p>')
    args = dict(news=news, has_news=has_news, t=t, hindsight=hindsight, pers=pers, score_scale=scale)
    body.append(f"<h2>Strongest {TOP_N} · calls</h2>{rank_table(strong, side='call', **args)}")
    body.append(f"<h2>Weakest {TOP_N} · puts</h2>{rank_table(weak, side='put', **args)}")
    body.append(f'<details class="rest"><summary>The other {len(rest)} on the watchlist</summary>'
                f"{rank_table(rest, side='', **args)}</details>")

    src = ("recorded live" if (d.source == "live").any() else
           "rebuilt from 5-minute history (a little coarser than 1-minute days)" if (d.source == "replay-5m").any()
           else "rebuilt from 1-minute history")
    return page(f"{short_day(day)} {t} · RS Scanner", "".join(body), page="today" if day == now_ny().date().isoformat() else "day",
                pills=snapshot_pills(href, times, t), stepper=stepper, footer=f"This day was {src}",
                prev=f"/day/{p_day}?t={t}" if p_day else "", next=f"/day/{n_day}?t={t}" if n_day else "",
                tprev=href(p_t) if p_t else "", tnext=href(n_t) if n_t else "",
                live=f"{day}|{times[-1]}|{int(following)}" if live else "")


# ---------------------------------------------------------------- ticker ----

def scatter(d: pd.DataFrame, viewed: str) -> str:
    """Score at the snapshot vs RS to the close, one dot per day; follow-through quadrants tinted."""
    d = d.dropna(subset=["fwd_rs"])
    if d.empty:
        return ""
    W, H, P = 560, 260, 34
    xs = max(3.0, d.score.abs().max() * 1.1)
    ys = max(2.0, d.fwd_rs.abs().max() * 1.1)
    x = lambda v: P + (v + xs) / (2 * xs) * (W - 2 * P)
    y = lambda v: H - P - (v + ys) / (2 * ys) * (H - 2 * P)
    out = [f'<svg viewBox="0 0 {W} {H}" width="100%" role="img" aria-label="Score vs outcome">',
           f'<rect x="{x(0)}" y="{y(ys)}" width="{x(xs) - x(0)}" height="{y(0) - y(ys)}" fill="var(--pos)" opacity=".07"/>',
           f'<rect x="{x(-xs)}" y="{y(0)}" width="{x(0) - x(-xs)}" height="{y(-ys) - y(0)}" fill="var(--neg)" opacity=".07"/>',
           f'<line x1="{x(-xs)}" x2="{x(xs)}" y1="{y(0)}" y2="{y(0)}" stroke="var(--fg)" opacity=".35"/>',
           f'<line x1="{x(0)}" x2="{x(0)}" y1="{y(ys)}" y2="{y(-ys)}" stroke="var(--fg)" opacity=".35"/>',
           f'<text x="{W - P}" y="{y(ys) + 14}" text-anchor="end" class="mute">strong, and kept beating SPY</text>',
           f'<text x="{P + 2}" y="{y(-ys) - 6}" class="mute">weak, and kept lagging</text>',
           f'<text x="{W / 2}" y="{H - 6}" text-anchor="middle" class="mute">score at the snapshot →</text>',
           f'<text x="10" y="{H / 2}" transform="rotate(-90 10 {H / 2})" text-anchor="middle" class="mute">RS → close</text>']
    for r in d.itertuples():
        me = r.day == viewed
        out.append(f'<a href="/day/{r.day}?t={r.asof}#{r.ticker}"><circle cx="{x(r.score):.1f}" cy="{y(r.fwd_rs):.1f}" '
                   f'r="{6 if me else 4}" fill="{"var(--acc)" if me else "var(--fg)"}" opacity="{1 if me else .6}">'
                   f"<title>{short_day(r.day)}: score {r.score:+.1f}, RS → close {r.fwd_rs:+.2f}%</title></circle></a>")
    out.append("</svg>")
    return "".join(out)


@web.route("/ticker/<ticker>")
def ticker_view(ticker):
    ticker = ticker.upper()
    t = request.args.get("t", DEFAULT_VIEW)
    viewed = request.args.get("day", "")
    d = query("SELECT * FROM scans WHERE ticker=? AND asof=? ORDER BY day DESC", ticker, t)
    if d.empty:
        body = f"<h1>{esc(ticker)}</h1><p>No recorded sessions for {esc(ticker)} at {esc(t)}.</p>"
        return page(f"{ticker} · RS Scanner", body)
    everyone = query("SELECT day, ticker, score FROM scans WHERE asof=?", t)
    tops = {day: set(g.nlargest(TOP_N, "score").ticker) for day, g in everyone.groupby("day")}
    bots = {day: set(g.nsmallest(TOP_N, "score").ticker) for day, g in everyone.groupby("day")}
    d["side"] = ["call" if ticker in tops.get(x, ()) else "put" if ticker in bots.get(x, ()) else "" for x in d.day]

    lines = []
    for side, word in (("call", f"top {TOP_N}"), ("put", f"bottom {TOP_N}")):
        g = d[(d.side == side) & d.fwd_rs.notna()]
        if len(g):
            won = int(((g.fwd_rs > 0) if side == "call" else (g.fwd_rs < 0)).sum())
            lines.append(f"{word} on {len(g)} day{'s' if len(g) != 1 else ''}: moved the trade's way on "
                         f"<b>{won}/{len(g)}</b>, average RS → close {num(g.fwd_rs.mean())}")
    followed = d.dropna(subset=["fwd_rs"])
    ft = int((((followed.score > 0) & (followed.fwd_rs > 0)) | ((followed.score < 0) & (followed.fwd_rs < 0))).sum())
    summary = (f"<p>At {t}, {ticker} was " + "; ".join(lines) + ".</p>" if lines else
               f"<p>{ticker} was never in the top or bottom {TOP_N} at {t}.</p>")
    summary += f'<p class="mute">Followed through (score and later move the same direction) on {ft}/{len(followed)} days.</p>'

    cov = news_coverage()
    news = load_news([ticker], prev_close(d.day.min()), et(d.day.max(), "16:00"))
    crumb = (f'<p class="mute"><a href="/day/{viewed}?t={t}#{ticker}">‹ {short_day(viewed)} at {t}</a></p>'
             if viewed else "")
    scale = max(3.0, d.score.abs().max())
    rows_html = []
    for side in ("call", "put", ""):
        g = d[d.side == side]
        if g.empty:
            continue
        title = {"call": f"Days in the top {TOP_N}", "put": f"Days in the bottom {TOP_N}", "": "Other days"}[side]
        has_news = lambda day_, tk: covered(cov, tk, day_)
        rows_html.append(f"<h2>{title}</h2>" + rank_table(
            g, news, has_news, t, side, g.fwd_rs.notna().any(), {}, scale, first="day", viewed=viewed))
    body = (f"{crumb}<h1>{ticker}</h1>{summary}"
            f'<div class="card chart">{scatter(d, viewed)}</div>'
            + "".join(rows_html))
    href = lambda s: f"/ticker/{ticker}?t={s}" + (f"&day={viewed}" if viewed else "")
    times = sorted(query("SELECT DISTINCT asof FROM scans WHERE ticker=?", ticker)["asof"])
    p_t, n_t = neighbours(times, t)
    return page(f"{ticker} · RS Scanner", body, page="ticker", pills=snapshot_pills(href, times, t),
                tprev=href(p_t) if p_t else "", tnext=href(n_t) if n_t else "",
                footer=(f"Headlines for {ticker} saved since {cov[ticker]:%b %-d}; earlier days show – instead of a count"
                        if ticker in cov else "No headlines saved for this ticker yet"))
