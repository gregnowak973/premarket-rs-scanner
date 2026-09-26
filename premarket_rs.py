#!/usr/bin/env python3
"""
Premarket relative strength / weakness scanner.

Ranks a watchlist by how each stock is trading against the market (SPY) before
the open. It looks at two things:

  1. RS level: the gap from yesterday's close, minus the gap you'd expect from
     the stock's beta to SPY, scaled by the stock's normal daily volatility.
  2. RS trend: how much that excess move changed over the last N minutes of
     premarket. This catches the "turn", where a stock starts climbing while
     SPY fades (MSFT on 2026-09-25 did exactly this from about 8:30 AM).

Data comes from Yahoo Finance via yfinance, which is free and needs no key.
Yahoo reports zero volume for extended-hours bars, so this scanner can't
measure premarket relative volume.

Examples:
    python premarket_rs.py                          # live scan (or last session's premarket)
    python premarket_rs.py --watch 5                # rescan every 5 minutes
    python premarket_rs.py --date 2026-09-25        # replay a past morning + show outcome
    python premarket_rs.py --date 2026-09-25 --asof 08:45
    python premarket_rs.py --tickers MSFT,NVDA,AAPL --news
"""
from __future__ import annotations

import argparse
import datetime as dt
import functools
import sys
import time
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import yfinance as yf

NY = ZoneInfo("America/New_York")
BENCH = "SPY"

# Liquid names with daily/0DTE-style options and tight spreads.
DEFAULT_UNIVERSE = [
    "QQQ", "IWM", "DIA",
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AVGO", "AMD",
    "NFLX", "ORCL", "CRM", "ADBE", "INTC", "MU", "QCOM", "ARM", "SMCI", "PLTR",
    "COIN", "MSTR", "HOOD", "UBER", "SHOP", "SNOW", "PANW", "CRWD",
    "JPM", "BAC", "GS", "WMT", "COST", "HD", "LLY", "UNH", "XOM", "BA", "DIS",
]


# ---------------------------------------------------------------- data ----

def _split(df: pd.DataFrame, tickers: list[str]) -> dict[str, pd.DataFrame]:
    out = {}
    for t in tickers:
        try:
            sub = df[t].dropna(how="all")
        except KeyError:
            continue
        if not sub.empty:
            out[t] = sub
    return out


def fetch_intraday(tickers: list[str], start: dt.date | None = None,
                   end: dt.date | None = None, interval: str = "1m") -> dict[str, pd.DataFrame]:
    """Bars including pre/post market. Yahoo keeps 1-minute bars ~30 days (8 per request)
    and 5-minute bars ~60 days.

    Bars are labelled so a bar stamped t closes at t + 1 minute, whatever the interval, so
    last_at(t) never sees a price from after t + 1 minute (a 5-minute bar is stamped 4 minutes late).
    """
    when = dict(start=start, end=end) if start else dict(period="8d" if interval == "1m" else "60d")
    df = yf.download(tickers, interval=interval, prepost=True, progress=False,
                     group_by="ticker", threads=True, **when)
    df.index = df.index.tz_convert(NY) + (pd.Timedelta(interval) - pd.Timedelta("1min"))
    return _split(df, tickers)


def fetch_daily(tickers: list[str], period: str = "1y") -> dict[str, pd.DataFrame]:
    df = yf.download(tickers, period=period, interval="1d", progress=False,
                     group_by="ticker", threads=True, auto_adjust=False)
    return _split(df, tickers)


def news_items(ticker: str) -> list[dict]:
    """Yahoo's latest ~100 headlines for a ticker: time (UTC), title, link, publisher."""
    try:
        items = yf.Ticker(ticker).get_news(count=100) or []
    except Exception:
        return []
    out = []
    for it in items:
        c = it.get("content") or it
        ts = c.get("pubDate") or c.get("providerPublishTime")
        if isinstance(ts, (int, float)):
            ts = dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc)
        elif isinstance(ts, str):
            ts = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
        else:
            continue
        link = ((c.get("canonicalUrl") or {}).get("url") or (c.get("clickThroughUrl") or {}).get("url")
                or c.get("link") or "")
        publisher = (c.get("provider") or {}).get("displayName") or c.get("publisher") or ""
        out.append({"ts": ts, "title": c.get("title", ""), "link": link, "publisher": publisher})
    return out


@functools.lru_cache(maxsize=None)
def company_keys(ticker: str) -> tuple[str, ...]:
    """Words that mark a headline as being about this company: the ticker and its short name."""
    try:
        name = (yf.Ticker(ticker).info.get("shortName") or "").split()[0].strip(",.")
    except Exception:
        name = ""
    return tuple(k.lower() for k in (ticker, name) if k)


def is_company(ticker: str, title: str) -> bool:
    return any(k in title.lower() for k in company_keys(ticker))


def fetch_news(ticker: str, since: dt.datetime, until: dt.datetime) -> tuple[list[str], int]:
    """Headlines published in [since, until], company-specific first, and how many name the company."""
    heads = [n["title"] for n in news_items(ticker) if since <= n["ts"] <= until]
    named = [h for h in heads if is_company(ticker, h)]
    return named + [h for h in heads if h not in named], len(named)


# ------------------------------------------------------------- metrics ----

def beta_and_vol(daily: pd.DataFrame, bench: pd.DataFrame, before: dt.date,
                 lookback: int = 90) -> tuple[float, float]:
    """Beta to the benchmark and daily return stdev (in %), using data before `before`.

    Raw 60-90 day betas are noisy, so the estimate is shrunk halfway toward 1.
    """
    s = daily["Close"][daily.index.date < before].pct_change()
    b = bench["Close"][bench.index.date < before].pct_change()
    j = pd.concat([s, b], axis=1, keys=["s", "b"]).dropna().tail(lookback)
    if len(j) < 20:
        return 1.0, 2.0
    raw = float(np.cov(j.s, j.b)[0, 1] / np.var(j.b, ddof=1))
    return 0.5 * raw + 0.5, float(j.s.std() * 100)


def split_day(bars: pd.DataFrame, day: dt.date):
    """Return (previous regular session, all of `day` from 4:00 to 16:00)."""
    prior = bars[bars.index.date < day]
    if prior.empty:
        return None, None
    prev_day = prior.index[-1].date()
    prev_reg = prior[prior.index.date == prev_day].between_time("09:30", "15:59")
    today = bars[bars.index.date == day].between_time("04:00", "15:59")
    return prev_reg, today


def last_at(series: pd.Series, t: dt.datetime) -> float:
    s = series[series.index <= t].dropna()
    return float(s.iloc[-1]) if not s.empty else np.nan


def scan(tickers: list[str], day: dt.date, asof: dt.datetime, trend_min: int,
         intraday: dict, daily: dict, with_news: bool) -> pd.DataFrame:
    spy_prev, spy_day = split_day(intraday[BENCH], day)
    spy_pc = float(spy_prev["Close"].iloc[-1])
    t0 = asof - dt.timedelta(minutes=trend_min)
    spy_now = last_at(spy_day["Close"], asof)
    spy_then = last_at(spy_day["Close"], t0)
    if np.isnan(spy_then):
        spy_then = float(spy_day["Close"].iloc[0])
    spy_gap = (spy_now / spy_pc - 1) * 100
    spy_gap_then = (spy_then / spy_pc - 1) * 100
    spy_after = spy_day[spy_day.index > asof]

    rows = []
    for t in tickers:
        if t == BENCH or t not in intraday or t not in daily:
            continue
        prev_reg, today = split_day(intraday[t], day)
        if prev_reg is None or prev_reg.empty:
            continue
        seen = today[today.index <= asof]
        if seen.empty:
            continue
        pm = seen.between_time("04:00", "09:29")
        pc = float(prev_reg["Close"].iloc[-1])
        beta, sig = beta_and_vol(daily[t], daily[BENCH], day)

        now = last_at(seen["Close"], asof)
        then = last_at(seen["Close"], t0)
        if np.isnan(then):  # no print at t0, use the first bar of the day
            then = float(seen["Close"].iloc[0])
        gap = (now / pc - 1) * 100
        rs = gap - beta * spy_gap
        rs_then = (then / pc - 1) * 100 - beta * spy_gap_then
        trend = rs - rs_then
        # Scale trend by the volatility you'd expect over the trend window.
        trend_sig = sig * np.sqrt(trend_min / 390)

        row = {
            "Ticker": t,
            "Last": now,
            "Gap%": gap,
            "Beta": beta,
            "RS%": rs,
            "RSz": rs / sig,
            "Trend%": trend,
            "Trendz": trend / trend_sig,
            "PMHi": pm["High"].max() if not pm.empty else np.nan,
            "PMLo": pm["Low"].min() if not pm.empty else np.nan,
            "PrevHi": prev_reg["High"].max(),
            "PrevLo": prev_reg["Low"].min(),
            "PrevCl": pc,
        }
        row["Score"] = row["RSz"] + row["Trendz"]
        # Divergence: stock and SPY moved in opposite directions during the trend window.
        stock_move = (now / then - 1) * 100
        spy_move = (spy_now / spy_then - 1) * 100
        row["Div"] = ("UP vs SPY dn" if stock_move > 0.15 and spy_move < -0.05 else
                      "DN vs SPY up" if stock_move < -0.15 and spy_move > 0.05 else "")

        # Outcome, when replaying a past day: what happened after `asof`.
        after = today[today.index > asof].between_time("09:30", "15:59")
        if not after.empty and not spy_after.empty:
            fwd = (after["Close"].iloc[-1] / now - 1) * 100
            spy_fwd = (spy_after["Close"].iloc[-1] / spy_now - 1) * 100
            row["FwdRS%"] = fwd - beta * spy_fwd
            row["FwdHi%"] = (after["High"].max() / now - 1) * 100
            row["FwdLo%"] = (after["Low"].min() / now - 1) * 100
            # Price change from the scan price an hour later, at noon, and at the close.
            closes = today["Close"].dropna()
            for col, t in (("Fwd1h%", asof + dt.timedelta(hours=1)),
                           ("FwdNoon%", dt.datetime.combine(day, dt.time(12, 0), tzinfo=NY))):
                if asof < t <= closes.index[-1]:
                    row[col] = (last_at(closes, t) / now - 1) * 100
            row["FwdClose%"] = fwd
        rows.append(row)

    df = pd.DataFrame(rows).set_index("Ticker")
    if with_news and not df.empty:
        since = asof.astimezone(dt.timezone.utc) - dt.timedelta(hours=18)
        top = df["Score"].abs().sort_values(ascending=False).index[:12]
        df["News"] = ""
        df["NewsN"] = 0
        for t in top:
            heads, named = fetch_news(t, since, asof)
            if heads:
                df.loc[t, "News"] = heads[0]
                df.loc[t, "NewsN"] = named
    df.attrs.update(spy_gap=spy_gap, spy_trend=spy_gap - spy_gap_then)
    return df


# -------------------------------------------------------------- output ----

def fmt(df: pd.DataFrame) -> str:
    out = df.copy()
    if "News" in out:
        out["News"] = out["News"].str[:60]
    for c in out.columns:
        if out[c].dtype.kind == "f":
            digits = 1 if c.endswith("z") or c == "Score" else 2
            out[c] = out[c].map(lambda v: f"{v:+.{digits}f}" if c.endswith(("%", "z")) or c == "Score"
                                else f"{v:.2f}")
    return out.to_string()


def resolve_day_and_asof(args, intraday) -> tuple[dt.date, dt.datetime]:
    bars = intraday[BENCH]
    if args.date:
        day = dt.date.fromisoformat(args.date)
    else:
        now = dt.datetime.now(NY)
        days = sorted(set(bars.index.date))
        day = now.date() if now.date() in days else days[-1]
    if args.asof:
        asof = dt.datetime.combine(day, dt.time.fromisoformat(args.asof), tzinfo=NY)
    elif not args.date and day == dt.datetime.now(NY).date():
        asof = min(dt.datetime.now(NY), dt.datetime.combine(day, dt.time(15, 59), tzinfo=NY))
    else:  # replaying, or no session yet today: use the last minute of premarket
        asof = dt.datetime.combine(day, dt.time(9, 29), tzinfo=NY)
    return day, asof


def run_once(args, tickers: list[str]) -> None:
    all_t = sorted(set(tickers) | {BENCH})
    intraday = fetch_intraday(all_t)
    if BENCH not in intraday:
        sys.exit("Couldn't get SPY data from Yahoo. Try again in a minute.")
    daily = fetch_daily(all_t)
    day, asof = resolve_day_and_asof(args, intraday)
    if day not in set(intraday[BENCH].index.date):
        sys.exit(f"No 1-minute data for {day}. Yahoo keeps about 30 days; this fetch covers the last 8.")

    df = scan(tickers, day, asof, args.trend, intraday, daily, args.news)
    if df.empty:
        print("No prints yet for this session.")
        return
    phase = "premarket" if asof.time() < dt.time(9, 30) else "regular session"
    print(f"\nRS scan ({phase})  |  {day}  as of {asof:%H:%M} ET  |  "
          f"SPY {df.attrs['spy_gap']:+.2f}% (last {args.trend}m {df.attrs['spy_trend']:+.2f}%)")
    print(f"Score = RSz (excess gap / daily vol) + Trendz (change in excess gap over the last {args.trend} min)\n")

    cols = [c for c in df.columns if c not in ("Beta", "PrevCl")]
    strong = df.sort_values("Score", ascending=False).head(args.top)
    weak = df.sort_values("Score").head(args.top)
    print(f"RELATIVE STRENGTH (call candidates)\n{fmt(strong[cols])}\n")
    print(f"RELATIVE WEAKNESS (put candidates)\n{fmt(weak[cols])}\n")
    if args.csv:
        df.sort_values("Score", ascending=False).to_csv(args.csv)
        print(f"Wrote {args.csv}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--tickers", help="comma-separated list (default: built-in liquid universe)")
    p.add_argument("--file", help="watchlist file, one ticker per line")
    p.add_argument("--date", help="replay a past session, YYYY-MM-DD (last ~30 days)")
    p.add_argument("--asof", help="time of day in ET, HH:MM (default: now, or 09:29 when replaying)")
    p.add_argument("--trend", type=int, default=45, help="trend window in minutes (default 45)")
    p.add_argument("--top", type=int, default=8, help="rows per table (default 8)")
    p.add_argument("--news", action="store_true", help="fetch headlines from the 18h before the scan for the top movers (Yahoo keeps roughly the last day or two)")
    p.add_argument("--watch", type=float, help="rescan every N minutes until 9:45 ET")
    p.add_argument("--csv", help="also write the full ranking to this CSV path")
    args = p.parse_args()

    if args.file:
        with open(args.file) as f:
            tickers = [l.strip().upper() for l in f if l.strip() and not l.startswith("#")]
    elif args.tickers:
        tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    else:
        tickers = DEFAULT_UNIVERSE

    if not args.watch:
        run_once(args, tickers)
        return
    while True:
        run_once(args, tickers)
        if dt.datetime.now(NY).time() >= dt.time(9, 45):
            break
        time.sleep(args.watch * 60)


if __name__ == "__main__":
    main()
