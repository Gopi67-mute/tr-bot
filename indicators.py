#!/usr/bin/env python3
"""Indicators for strategy research, all causal and all warm-up honest.

Research-only module. Places no orders and is never imported by the live
engine's trading path.

Two rules every function here obeys, because breaking either is how a backtest
invents an edge that does not exist:

  1. Value at index i uses candles 0..i and nothing later. There is no
     centring, no `shift(-1)`, no full-series normalisation.
  2. A bar whose indicator is not yet warm returns None, never 0.0 and never
     the first available value back-filled. A filter that reads 0.0 for "ADX
     not ready yet" would treat the first weeks of every window as a dead-calm
     market and either trade all of it or none of it, and both answers are
     wrong in a way that is invisible in the output.

Wilder's smoothing is used for ATR, ADX and RSI - the same definition
TradingView and most charting packages use - so a number here can be checked
against a chart by eye.
"""


def sma(values, period):
    """Simple moving average. None until `period` values exist."""
    out, total = [None] * len(values), 0.0
    for i, v in enumerate(values):
        total += v
        if i >= period:
            total -= values[i - period]
        if i >= period - 1:
            out[i] = total / period
    return out


def ema(values, period):
    """Exponential moving average seeded with the first `period` mean.

    Identical in behaviour to trading_bot.ema; duplicated so the research
    modules can be read, and tested, without the engine in scope.
    """
    out = [None] * len(values)
    if len(values) < period or period <= 0:
        return out
    k = 2.0 / (period + 1)
    prev = sum(values[:period]) / period
    out[period - 1] = prev
    for i in range(period, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def true_ranges(candles):
    """TR per bar. The first bar has no previous close, so it is None."""
    out = [None] * len(candles)
    for i in range(1, len(candles)):
        h, l, pc = candles[i]["h"], candles[i]["l"], candles[i - 1]["c"]
        out[i] = max(h - l, abs(h - pc), abs(l - pc))
    return out


def _wilder(values, period, first_index):
    """Wilder's recursive average over a list that may start with Nones.

    Seeded with the mean of the first `period` real values, then
    prev + (v - prev)/period. Returns a list aligned with `values`.
    """
    out = [None] * len(values)
    seed_end = first_index + period
    if seed_end > len(values):
        return out
    chunk = values[first_index:seed_end]
    if any(v is None for v in chunk):
        return out
    prev = sum(chunk) / period
    out[seed_end - 1] = prev
    for i in range(seed_end, len(values)):
        v = values[i]
        if v is None:
            return out
        prev = prev + (v - prev) / period
        out[i] = prev
    return out


def atr(candles, period=14):
    """Average true range, Wilder. None for the first `period` bars."""
    return _wilder(true_ranges(candles), period, 1)


def adx(candles, period=14):
    """(adx, plus_di, minus_di), Wilder.

    ADX measures how *directional* recent movement has been, not which way.
    It is the filter this research uses to refuse a sideways market, which is
    the condition the existing strategy has no defence against.

    Warm-up costs two passes of `period`: once to seed the DI smoothing and
    once to seed the ADX smoothing of DX, so the first usable ADX is at index
    2*period, matching standard implementations.
    """
    n = len(candles)
    tr, pdm, mdm = true_ranges(candles), [None] * n, [None] * n
    for i in range(1, n):
        up = candles[i]["h"] - candles[i - 1]["h"]
        dn = candles[i - 1]["l"] - candles[i]["l"]
        pdm[i] = up if (up > dn and up > 0) else 0.0
        mdm[i] = dn if (dn > up and dn > 0) else 0.0
    str_, spdm, smdm = (_wilder(tr, period, 1), _wilder(pdm, period, 1),
                        _wilder(mdm, period, 1))
    pdi, mdi, dx = [None] * n, [None] * n, [None] * n
    for i in range(n):
        if str_[i] is None or str_[i] <= 0:
            continue
        pdi[i] = 100.0 * spdm[i] / str_[i]
        mdi[i] = 100.0 * smdm[i] / str_[i]
        tot = pdi[i] + mdi[i]
        dx[i] = 0.0 if tot <= 0 else 100.0 * abs(pdi[i] - mdi[i]) / tot
    first = next((i for i, v in enumerate(dx) if v is not None), None)
    out = [None] * n if first is None else _wilder(dx, period, first)
    return out, pdi, mdi


def rsi(closes, period=14):
    """Wilder RSI. None until `period` changes have been seen."""
    n = len(closes)
    gains, losses = [None] * n, [None] * n
    for i in range(1, n):
        d = closes[i] - closes[i - 1]
        gains[i], losses[i] = max(d, 0.0), max(-d, 0.0)
    ag, al = _wilder(gains, period, 1), _wilder(losses, period, 1)
    out = [None] * n
    for i in range(n):
        if ag[i] is None:
            continue
        if al[i] == 0:
            out[i] = 100.0
        else:
            out[i] = 100.0 - 100.0 / (1 + ag[i] / al[i])
    return out


def swing_low(candles, i, lookback):
    """Lowest low over the `lookback` bars ending at i (inclusive)."""
    lo = max(0, i - lookback + 1)
    return min(c["l"] for c in candles[lo:i + 1])


def swing_high(candles, i, lookback):
    """Highest high over the `lookback` bars ending at i (inclusive)."""
    lo = max(0, i - lookback + 1)
    return max(c["h"] for c in candles[lo:i + 1])


def pivots(candles, i, left=2, right=2, lookback=60):
    """(pivot_highs, pivot_lows) confirmed on or before bar i.

    A pivot high at k needs `left` lower highs before it and `right` lower
    highs after it, so it is only *known* at k+right. Bars after i are never
    consulted: the scan stops at i-right, which is what keeps a swing level
    from being discovered before the market could have seen it.
    """
    hi, lo = [], []
    start = max(left, i - lookback)
    for k in range(start, i - right + 1):
        h, l = candles[k]["h"], candles[k]["l"]
        if all(candles[k - j]["h"] < h for j in range(1, left + 1)) and \
           all(candles[k + j]["h"] < h for j in range(1, right + 1)):
            hi.append((k, h))
        if all(candles[k - j]["l"] > l for j in range(1, left + 1)) and \
           all(candles[k + j]["l"] > l for j in range(1, right + 1)):
            lo.append((k, l))
    return hi, lo


def structure(candles, i, left=2, right=2, lookback=80):
    """'up', 'down' or None - the trend written in the swing points.

    'up' needs the last two confirmed pivot highs rising *and* the last two
    confirmed pivot lows rising (higher highs and higher lows); 'down' the
    mirror. Anything else - including not having two of each yet - is None,
    which the filters treat as "no structural trend", not as permission.
    """
    hi, lo = pivots(candles, i, left, right, lookback)
    if len(hi) < 2 or len(lo) < 2:
        return None
    hh = hi[-1][1] > hi[-2][1]
    hl = lo[-1][1] > lo[-2][1]
    lh = hi[-1][1] < hi[-2][1]
    ll = lo[-1][1] < lo[-2][1]
    if hh and hl:
        return "up"
    if lh and ll:
        return "down"
    return None


def next_resistance(candles, i, price, left=2, right=2, lookback=120):
    """Nearest confirmed pivot high strictly above `price`, or None."""
    hi, _ = pivots(candles, i, left, right, lookback)
    above = [h for _, h in hi if h > price]
    return min(above) if above else None


def next_support(candles, i, price, left=2, right=2, lookback=120):
    """Nearest confirmed pivot low strictly below `price`, or None."""
    _, lo = pivots(candles, i, left, right, lookback)
    below = [l for _, l in lo if l < price]
    return max(below) if below else None


def align_index(src, dst_close_ms):
    """For each close time in `dst_close_ms`, the last index of `src` closed by then.

    This is the whole multi-timeframe safety mechanism in one function. A 15m
    bar closing at 10:15:00 may consult the 1h bar that closed at 10:00:00 -
    and the one closing at 10:15 only if it really closed at or before then.
    Returns -1 where no source bar has closed yet.

    Both inputs must be ascending, which the exchange guarantees and the cache
    preserves.
    """
    out, j = [], -1
    for t in dst_close_ms:
        while j + 1 < len(src) and src[j + 1]["T"] <= t:
            j += 1
        out.append(j)
    return out
