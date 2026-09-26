"""Historical 1-minute bars from Alpaca's market data API, for backtests longer than Yahoo allows.

Needs a free Alpaca account. Put the keys in a `.env` file next to this one (git ignores it):

    ALPACA_KEY_ID=PK...
    ALPACA_SECRET_KEY=...

or set them as environment variables. Bars come from the SIP feed (all US exchanges),
including extended hours, labelled by their start minute like Yahoo's 1-minute bars.
"""
from __future__ import annotations

import datetime as dt
import os
import time
from pathlib import Path

import pandas as pd
import requests

URL = "https://data.alpaca.markets/v2/stocks/bars"
ENV = Path(__file__).with_name(".env")
NY = "America/New_York"


def credentials() -> tuple[str, str] | None:
    vals = {}
    if ENV.exists():
        for line in ENV.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                vals[k.strip()] = v.strip().strip('"').strip("'")
    key = os.environ.get("ALPACA_KEY_ID") or vals.get("ALPACA_KEY_ID")
    secret = os.environ.get("ALPACA_SECRET_KEY") or vals.get("ALPACA_SECRET_KEY")
    return (key, secret) if key and secret else None


def _get(session: requests.Session, params: dict) -> dict:
    for attempt in range(6):
        r = session.get(URL, params=params, timeout=60)
        if r.status_code == 429:  # free plan: 200 requests a minute
            time.sleep(int(r.headers.get("Retry-After", 0)) or 2 ** attempt)
            continue
        if r.status_code in (401, 403):
            raise SystemExit("Alpaca rejected the keys (check ALPACA_KEY_ID and ALPACA_SECRET_KEY in .env).")
        r.raise_for_status()
        return r.json()
    raise RuntimeError("Alpaca kept rate-limiting; try again in a minute.")


def fetch_bars(tickers: list[str], start: dt.date, end: dt.date, feed: str = "sip") -> dict[str, pd.DataFrame]:
    """1-minute bars from 04:00 ET on `start` through 20:00 ET on `end`, per ticker."""
    creds = credentials()
    if not creds:
        raise SystemExit(f"No Alpaca keys found. Add ALPACA_KEY_ID and ALPACA_SECRET_KEY to {ENV}.")
    s = requests.Session()
    s.headers.update({"APCA-API-KEY-ID": creds[0], "APCA-API-SECRET-KEY": creds[1]})
    t0 = pd.Timestamp(f"{start} 04:00", tz=NY).tz_convert("UTC")
    t1 = min(pd.Timestamp(f"{end} 20:00", tz=NY).tz_convert("UTC"),
             pd.Timestamp.now(tz="UTC") - pd.Timedelta(minutes=16))  # free plan: no SIP data from the last 15 min
    rows: dict[str, list] = {}
    for i in range(0, len(tickers), 20):  # a handful of symbols per request keeps pages balanced
        params = {"symbols": ",".join(tickers[i:i + 20]), "timeframe": "1Min", "start": t0.isoformat(),
                  "end": t1.isoformat(), "limit": 10000, "adjustment": "split", "feed": feed, "sort": "asc"}
        while True:
            data = _get(s, params)
            for sym, bars in (data.get("bars") or {}).items():
                rows.setdefault(sym, []).extend(bars)
            token = data.get("next_page_token")
            if not token:
                break
            params["page_token"] = token
    out = {}
    for sym, bars in rows.items():
        df = pd.DataFrame(bars)
        df.index = pd.to_datetime(df.pop("t"), utc=True).dt.tz_convert(NY)
        df = df.rename(columns={"o": "Open", "h": "High", "l": "Low", "c": "Close", "v": "Volume"})
        out[sym] = df[["Open", "High", "Low", "Close", "Volume"]].astype(float).sort_index()
    return out
