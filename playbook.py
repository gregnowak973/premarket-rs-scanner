"""The trading playbook the backtest pointed to, in one place so the site and the review agree.

    Mon/Wed  the top-scored mega cap, only if its score is >= 2.5 (a clear outlier) and it's
             above its premarket high at 09:35
    Fri      the #1 name on the full list, if it's above its premarket high at 09:35
    Tue/Thu  no trade

Exits (0DTE calls, simulated from the stock's 5-minute path): stop at -0.5% in the stock
(option ~-50%); at +1% (~+100%) sell half and move the stop to entry; sell the rest at +2%
(~+200%) or at noon. About 5% of premium is lost to the spread on every trade.
"""
from __future__ import annotations

import datetime as dt

import pandas as pd

MEGA = {"AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AVGO", "AMD", "INTC"}
OUTLIER = 2.5
ENTRY = "09:35"
EXIT = "12:00"
STOP, HALF, RUNNER = 0.5, 1.0, 2.0      # stock moves, %
SPREAD = 0.05
RULE_NAMES = {"mega": "Mega-cap outlier", "full": "Friday full-list pick"}


def rule_for(day: str) -> str | None:
    return {0: "mega", 2: "mega", 4: "full"}.get(dt.date.fromisoformat(day).weekday())


def is_outlier(r) -> bool:
    return r.ticker in MEGA and r.score >= OUTLIER


def pool(rows: pd.DataFrame, rule: str) -> pd.DataFrame:
    """The names the rule chooses from."""
    return rows[rows.ticker.isin(MEGA)] if rule == "mega" else rows


def signal(rows: pd.DataFrame, day: str) -> tuple[pd.Series | None, str | None, str]:
    """(the trade, rule, explanation) from the 09:35 rows of `day`."""
    rule = rule_for(day)
    if rule is None:
        return None, None, "Tuesday and Thursday: no playbook trade (the edge didn't hold on these days)."
    cand = pool(rows, rule).sort_values("score", ascending=False)
    if cand.empty:
        return None, rule, "No data."
    top = cand.iloc[0]
    above = top["last"] > top.pm_hi
    if rule == "mega" and top.score < OUTLIER:
        return None, rule, (f"No mega cap reached a score of {OUTLIER} (best: {top.ticker} {top.score:+.2f}). "
                            "No trade today.")
    if not above:
        return None, rule, (f"{top.ticker} ranked first (score {top.score:+.1f}) but was below its premarket high "
                            f"({top['last']:.2f} vs {top.pm_hi:.2f}). No trade today.")
    return top, rule, ""


def plan(r) -> dict:
    px = r["last"]
    return {"entry": px, "stop": px * (1 - STOP / 100), "half": px * (1 + HALF / 100),
            "runner": px * (1 + RUNNER / 100), "exit": EXIT}


def simulate(path: str | None) -> tuple[float | None, str]:
    """Simulated option return (fraction of premium, after spread) and how the trade ended."""
    if not isinstance(path, str) or not path:
        return None, ""
    p = [float(v) for v in path.split(",")]
    end = (12 * 60 - (9 * 60 + 35)) // 5
    half = False
    for v in p[1:end + 1]:
        if not half:
            if v <= -STOP:
                return -0.50 - SPREAD, "stopped out"
            if v >= HALF:
                half = True
        else:
            if v >= RUNNER:
                return 0.5 * 1.0 + 0.5 * 2.0 - SPREAD, "half at +100%, runner at +200%"
            if v <= 0.0:
                return 0.5 * 1.0 + 0.5 * -0.20 - SPREAD, "half at +100%, runner back to entry"
    last = p[min(end, len(p) - 1)]
    rest = max(last - 0.30, -1.0)
    if half:
        return 0.5 * 1.0 + 0.5 * rest - SPREAD, "half at +100%, runner out at noon"
    return rest - SPREAD, "neither by noon, out at noon"
