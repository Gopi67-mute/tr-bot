#!/usr/bin/env python3
"""Candle and funding-rate cache for strategy research.

Research-only. This module places no orders, reads no account, touches no
paper state and is never imported by the trading engine. It exists so a
backtest can be re-run a hundred times while the exchange is asked for each
candle exactly once.

Why a cache at all
------------------
Honest validation means replaying several years over several timeframes, and
each re-run of a candidate rule set needs the same bars as the last one. Paging
~100k fifteen-minute candles out of the REST API on every run would be slow,
would risk a rate-limit ban on the account's IP, and - worse for the research -
would quietly change the data under the comparison as new candles close. A
file cache fixes the window so two candidate strategies are judged on exactly
the same bars.

Storage format is one gzipped JSON file per (symbol, interval), holding a list
of {t,o,h,l,c,v,T} dicts sorted by open time. Funding is one file per symbol of
{fundingTime, rate_pct}. Files are written atomically (temp + replace) so an
interrupted download cannot leave a half-written cache behind that a later run
would trust.

Where the candles come from, and why this module pins the base URL
-------------------------------------------------------------------
Always `https://fapi.binance.com` - the real exchange - regardless of
`use_testnet`, and the pin is explicit rather than inherited.

`trading_bot.Binance.__init__` defaults its base to
`TESTNET_BASE if cfg["use_testnet"] else LIVE_BASE`, for *every* request,
market data included. The engine itself never relies on that default for data:
`main()` passes `base=LIVE_BASE` explicitly for backtest, research and paper
mode, so the running bot and `--mode research` have always read real candles.
The default is a trap for new callers, not a bug in the engine - and this
module was written by walking straight into it.

It is worth recording what that mistake looked like, because it is silent.
Candles pulled from `demo-fapi.binance.com` for ETHUSDT 15m show true ranges of
8% of price, volumes of tens of coins, and prices oscillating between two fixed
levels for hours. Nothing errors. A backtest on that data measures the demo
server's random number generator and reports an edge, or the absence of one,
with exactly the same confidence as a real run. Hence `public_client()`: no
caller of this module can inherit the default by forgetting an argument.

Market data needs no credentials, so reading it from the live host is a plain
public GET. Nothing here can place an order: the client is built with
`public_only=True`, which makes any signed or non-GET request raise before a
socket is opened. The testnet flag governs order placement, which is the only
thing it should ever have governed.
"""

import gzip
import json
import os
import time

import trading_bot as tb

CACHE_DIR = os.path.join(tb.HERE, "research_cache")
FUNDING_INTERVAL_MS = 8 * 3600_000


def public_client(cfg=None):
    """A read-only client pinned to the real exchange's public endpoints.

    `public_only=True` means no credentials are loaded and any signed request
    or non-GET method raises, so this object cannot place an order whatever it
    is asked to do. The base is pinned to the live host rather than left to
    `Binance`'s `use_testnet` default, for the reason in the module docstring:
    demo-fapi answers with synthetic candles instead of failing.
    """
    cfg = cfg or tb.load_config(os.path.join(tb.HERE, "config.json"))
    return tb.Binance(cfg, base=tb.LIVE_BASE, public_only=True)


def _path(name):
    return os.path.join(CACHE_DIR, name)


def _write_atomic(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".part"
    with gzip.open(tmp, "wt", encoding="utf-8") as f:
        json.dump(payload, f, separators=(",", ":"))
    os.replace(tmp, path)


def _read(path):
    if not os.path.exists(path):
        return None
    try:
        with gzip.open(path, "rt", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------- candles

def _klines_range(client, symbol, interval, start, end, verbose=True):
    """Page the whole window. Volume is kept, which tb.Binance.klines drops."""
    out, cur, calls = {}, int(start), 0
    while cur < end:
        p = {"symbol": symbol, "interval": interval, "limit": 1500,
             "startTime": cur, "endTime": int(end)}
        raw = client._req("GET", "/fapi/v1/klines", p)
        calls += 1
        if not raw:
            break
        for r in raw:
            out[int(r[0])] = {"t": int(r[0]), "o": float(r[1]), "h": float(r[2]),
                              "l": float(r[3]), "c": float(r[4]), "v": float(r[5]),
                              "T": int(r[6])}
        if len(raw) < 1500:
            break
        cur = int(raw[-1][0]) + 1
        if verbose and calls % 20 == 0:
            tb.say(f"    {symbol} {interval}: {len(out)} candles ...")
        time.sleep(0.2)
    return [out[k] for k in sorted(out)]


def candles(client, symbol, interval, start_ms, end_ms, verbose=True):
    """Closed candles in [start_ms, end_ms), from cache when it covers them.

    A cached file is reused only if it spans the whole request; a short cache is
    re-downloaded rather than silently trimming the test window, because a
    backtest run on half the requested history looks identical to one run on all
    of it in every report that does not print its own date range.
    """
    path = _path(f"{symbol}-{interval}.json.gz")
    have = _read(path)
    if have:
        rows = have["rows"]
        if rows and rows[0]["t"] <= start_ms and have["end"] >= end_ms - tb.interval_ms(interval):
            return [c for c in rows if start_ms <= c["t"] and c["T"] < end_ms]
    if verbose:
        tb.say(f"  downloading {symbol} {interval} ...")
    try:
        rows = _klines_range(client, symbol, interval, start_ms, end_ms, verbose)
    except Exception as e:
        # A research run must not lose hours of work to a transient outage, but
        # it must also never quietly test a shorter window than it was asked
        # for. Falling back to the cache is allowed only when the cache starts
        # early enough, and the shortfall at the recent end is announced.
        if not have or not have["rows"] or have["rows"][0]["t"] > start_ms:
            raise
        short = (end_ms - have["end"]) / 86_400_000.0
        tb.say(f"  (exchange unreachable: {type(e).__name__}; using the cached "
               f"{symbol} {interval} series, which ends {short:.1f} day(s) short)")
        return [c for c in have["rows"] if start_ms <= c["t"] and c["T"] < end_ms]
    rows = [c for c in rows if c["T"] < end_ms]
    if rows:
        _write_atomic(path, {"symbol": symbol, "interval": interval,
                             "start": rows[0]["t"], "end": rows[-1]["T"],
                             "rows": rows})
    return rows


# ---------------------------------------------------------------- rules

# Used only if the exchange cannot be reached and nothing is cached yet. These
# are the live USDT-M futures filters as published by Binance; they are a
# fallback for reporting, not an authority. They affect *nothing* in a paper
# replay, because `size_position(..., fractional=True)` - which is what paper
# mode uses - ignores exchange minimums and rounds to PAPER_QTY_STEP instead.
# They matter only when a run is asked what a real, non-fractional order would
# have done, and that run says so in its own output.
FALLBACK_RULES = {
    "ETHUSDT": {"step": 0.001, "min_qty": 0.001, "min_notional": 20.0, "tick": 0.01},
    "SOLUSDT": {"step": 1.0, "min_qty": 1.0, "min_notional": 20.0, "tick": 0.001},
}


def rules(symbols, client=None, cfg=None, verbose=True):
    """Exchange filters, cached, with an explicit fallback when offline.

    A research run that dies because the exchange is briefly unreachable loses
    hours of work for data it already has on disk, so this degrades instead of
    raising - but it never invents a number silently: a fallback is announced.
    """
    path = _path("rules.json.gz")
    have = _read(path)
    if have and all(s in have for s in symbols):
        return {s: have[s] for s in symbols}
    try:
        got = (client or public_client(cfg)).rules(list(symbols))
    except Exception as e:
        missing = [s for s in symbols if s not in FALLBACK_RULES]
        if missing:
            raise
        if verbose:
            tb.say(f"  (exchange unreachable: {type(e).__name__}; using published "
                   f"fallback filters - paper sizing is fractional and ignores them)")
        return {s: dict(FALLBACK_RULES[s]) for s in symbols}
    merged = dict(have or {})
    merged.update(got)
    _write_atomic(path, merged)
    return got


# ---------------------------------------------------------------- funding

def funding(client, symbol, start_ms, end_ms, verbose=True):
    """Historical 8-hourly funding rates as [{'ms': int, 'pct': float}].

    Funding is a real, persistent cost for a strategy that holds perpetual
    positions through 8-hour settlements, and the engine's own backtest omits
    it. A trend-following strategy is usually on the crowded side of the book,
    so omitting it flatters the result.
    """
    path = _path(f"{symbol}-funding.json.gz")
    have = _read(path)
    if have and have["start"] <= start_ms and have["end"] >= end_ms - FUNDING_INTERVAL_MS:
        return [r for r in have["rows"] if start_ms <= r["ms"] < end_ms]
    if verbose:
        tb.say(f"  downloading {symbol} funding ...")
    out, cur = {}, int(start_ms)
    try:
        while cur < end_ms:
            raw = client._req("GET", "/fapi/v1/fundingRate",
                              {"symbol": symbol, "startTime": cur,
                               "endTime": int(end_ms), "limit": 1000})
            if not raw:
                break
            for r in raw:
                out[int(r["fundingTime"])] = float(r["fundingRate"]) * 100.0
            if len(raw) < 1000:
                break
            cur = int(raw[-1]["fundingTime"]) + 1
            time.sleep(0.2)
    except Exception as e:
        if not have or not have.get("rows"):
            raise
        # Degrade rather than lose the run - but say so loudly. A window with
        # no funding rows is scored as if funding were free, and a report that
        # does not know its funding is partial would understate the cost of
        # every trade in the uncovered stretch.
        if have["start"] > start_ms:
            tb.say(f"  !! {symbol} funding is INCOMPLETE: cached history starts "
                   f"{tb.ist(have['start']).strftime('%Y-%m-%d')}, window starts "
                   f"{tb.ist(start_ms).strftime('%Y-%m-%d')}. Trades before that "
                   f"date are scored with NO funding cost ({type(e).__name__}).")
        else:
            tb.say(f"  (exchange unreachable: {type(e).__name__}; using cached "
                   f"{symbol} funding)")
        return [r for r in have["rows"] if start_ms <= r["ms"] < end_ms]
    rows = [{"ms": k, "pct": out[k]} for k in sorted(out)]
    if rows:
        _write_atomic(path, {"symbol": symbol, "start": rows[0]["ms"],
                             "end": rows[-1]["ms"], "rows": rows})
    return rows


def funding_cost(rows, side, qty, entry_price, open_ms, close_ms):
    """USDT paid (positive) or received (negative) over a position's life.

    Binance charges funding only on settlements that fall strictly inside the
    holding period. A long pays when the rate is positive; a short receives it.
    Notional is approximated at the entry price, which is within a few percent
    of the mark at settlement for the stop distances this system uses and avoids
    pretending to a precision the data does not support.
    """
    notional = abs(qty) * entry_price
    total = 0.0
    for r in rows:
        if open_ms < r["ms"] <= close_ms:
            paid = r["pct"] / 100.0 * notional
            total += paid if side == "LONG" else -paid
    return total


# ---------------------------------------------------------------- bundle

def pinned_end(end_ms=None):
    """The fixed instant every research run treats as "now".

    Anchoring the window to `tb.now_ms()` looks harmless and is not. The window
    then moves forward on every run, so the cache never satisfies its own
    coverage check and quietly re-downloads; worse, two candidate strategies
    compared an hour apart are compared on different data, which is exactly
    what the cache exists to prevent. The first run writes the anchor and every
    later run reuses it, until it is deleted or an explicit `end_ms` is passed.
    """
    path = _path("window.json.gz")
    if end_ms is None:
        have = _read(path)
        if have and have.get("end_ms"):
            return int(have["end_ms"])
        end_ms = tb.now_ms()
    end_ms = int(end_ms) - int(end_ms) % 3_600_000
    _write_atomic(path, {"end_ms": end_ms,
                         "pinned_ist": tb.ist(end_ms).strftime("%Y-%m-%d %H:%M")})
    return end_ms


def load(symbols, intervals, days, end_ms=None, client=None, cfg=None,
         warmup_days=60, verbose=True):
    """Everything a research run needs: {symbol: {interval: rows, 'funding': rows}}.

    `warmup_days` of extra history is fetched *before* the window for every
    interval so the slowest indicator (a 200-period 4h EMA needs ~34 days) is
    warm on the first bar that can trade. Without it the first weeks of every
    test window would silently take no trades.
    """
    cfg = cfg or tb.load_config(os.path.join(tb.HERE, "config.json"))
    client = client or public_client(cfg)
    end = pinned_end(end_ms)
    start = end - int(days) * 86_400_000
    warm = int(warmup_days) * 86_400_000
    out = {}
    for sym in symbols:
        d = {"start": start, "end": end}
        for iv in intervals:
            d[iv] = candles(client, sym, iv, start - warm, end, verbose)
        d["funding"] = funding(client, sym, start - warm, end, verbose)
        out[sym] = d
        if verbose:
            tb.say(f"  {sym}: " + ", ".join(f"{iv}={len(d[iv])}" for iv in intervals)
                   + f", funding={len(d['funding'])}")
    return out
