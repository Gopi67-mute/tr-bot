#!/usr/bin/env python3
"""Research bench for a selective multi-timeframe pullback strategy.

Research-only. This module never places an order, never reads an account,
never writes paper state and is not imported by the live trading path. It
exists to answer one question honestly: does a more selective rule set have a
positive expectancy after costs, and is that answer stable out of sample?

What is deliberately shared with the engine
-------------------------------------------
Position sizing (`trading_bot.size_position`), the stop sanity check
(`check_stop`), the cost model (`cost_per_unit`, `adverse_fill`,
`stop_fill_price`, `trade_pnl`) and the entire risk engine
(`trading_bot.RiskManager`) are imported, not reimplemented. A limit therefore
cannot pass here and fail in the live loop, and a candidate strategy cannot
quietly flatter itself by sizing more generously than the engine would.

What is deliberately different from the engine's own backtest
-------------------------------------------------------------
  * Three timeframes. The 4h bar sets the trend, the 1h bar confirms momentum
    and the 15m bar triggers. The engine's backtest confirms and triggers on
    the same 1h bar, which forces stops to be a 1h swing wide.
  * Exits resolve on 15m bars rather than 1h bars, which is four times the
    path detail. Where a single 15m bar touches both the stop and the target,
    the trade is booked as a *loss* - the order of the high and the low inside
    a bar is not in OHLC data and never can be - and the count of such bars is
    reported, together with what the result would have been had every one of
    them resolved favourably. That pair of numbers brackets the true answer
    instead of picking the flattering end of it.
  * Funding is charged on every 8-hourly settlement inside a position's life.
    A trend-following perpetual strategy usually sits on the crowded side of
    the book, so leaving funding out is not neutral.

Look-ahead
----------
Every indicator is causal (see `indicators`). The higher-timeframe index for a
15m bar is the last bar that had *closed* by that bar's close time, via
`indicators.align_index`. A signal read on bar i fills at the open of bar i+1.
Swing levels come from pivots confirmed `right` bars earlier. Nothing in the
replay consults a bar the market had not yet printed.
"""

import os

import indicators as ind
import labdata
import trading_bot as tb

STRATEGY_NAME = "mtf-pullback-v1"

# ---------------------------------------------------------------- parameters
#
# These are the candidate's own knobs. Risk limits are NOT here - they live in
# config.json and are read through RiskManager, so research cannot loosen them.

LAB_DEFAULTS = {
    "trend_tf": "4h",
    "confirm_tf": "1h",
    "entry_tf": "15m",

    # ---- A. 4h higher-timeframe trend filter ----
    "t_ema_fast": 20,
    "t_ema_mid": 50,
    "t_ema_slow": 200,
    "t_require_stack": True,     # EMA20 > EMA50 > EMA200 (mirrored for short)
    "t_require_slow": True,      # refuse to trade before the 200 EMA is warm
    "t_adx_period": 14,
    "t_adx_min": 20.0,           # below this the 4h is not trending
    "t_require_structure": True,  # higher highs AND higher lows (mirrored)
    "t_piv_lr": 2,               # bars either side of a 4h swing pivot

    # ---- B. 1h confirmation ----
    "c_ema_fast": 20,
    "c_ema_mid": 50,
    "c_adx_period": 14,
    "c_adx_min": 18.0,
    "c_rsi_period": 14,
    "c_rsi_min": 45.0,           # momentum still with the trend
    "c_rsi_max": 72.0,           # ... but not an exhausted, extended push
    "c_require_close_above_mid": True,

    # ---- C. 15m entry trigger ----
    "e_ema": 20,
    "e_ema_mid": 50,
    "e_atr_period": 14,
    "pullback_atr": 0.75,        # the bar's low must reach within this x ATR of EMA20
    "confirm_break_prev": True,  # close beyond the previous bar's extreme
    "confirm_body": True,        # and close in the intended direction
    "vol_period": 20,
    "vol_mult": 1.0,             # confirmation bar volume vs its own 20-bar mean

    # ---- D. no-trade filters ----
    "max_ext_atr": 1.5,          # close this far from EMA20 is "already gone"
    "atr_pct_min": 0.10,         # dead market: fees dominate
    "atr_pct_max": 1.20,         # abnormal volatility on the 15m bar
    "funding_max_pct": 0.05,     # per 8h, against the position
    "room_mult": 1.0,            # clear space to structure, as a multiple of the target
    "c_piv_lr": 5,               # bars either side of a 1h support/resistance pivot
    "room_lookback": 120,        # how far back a 1h level still counts

    # ---- stop ----
    "swing_lb": 8,               # 15m bars of structural invalidation
    "stop_atr_buffer": 0.5,      # placed this far beyond the swing
    "stop_min_atr": 1.0,         # never tighter than this, whatever the swing says

    # ---- target / management ----
    "rr_net": 2.0,               # reward:risk AFTER fees and slippage
    "trail": "off",              # off | breakeven | atr
    "trail_trigger_r": 1.0,      # move the stop once this much R is banked
    "trail_atr": 2.0,            # atr mode: trail this many ATR behind the extreme
    "partial_at_r": 0.0,         # 0 = off; else take `partial_frac` off at this R
    "partial_frac": 0.5,
}


def params(**over):
    p = dict(LAB_DEFAULTS)
    bad = set(over) - set(LAB_DEFAULTS)
    if bad:
        raise KeyError(f"unknown lab parameter(s): {sorted(bad)}")
    p.update(over)
    return p


# ---------------------------------------------------------------- pivots

class PivotTracker:
    """Confirmed swing levels, each stamped with the bar that confirmed it.

    A pivot high at bar k is only knowable at k + right. Storing that
    confirmation index and filtering on it is what stops the replay from
    placing a target under a high the market had not yet made.
    """

    def __init__(self, candles, left=2, right=2):
        self.right = right
        self.highs, self.lows = [], []
        n = len(candles)
        for k in range(left, n - right):
            h, l = candles[k]["h"], candles[k]["l"]
            if all(candles[k - j]["h"] < h for j in range(1, left + 1)) and \
               all(candles[k + j]["h"] < h for j in range(1, right + 1)):
                self.highs.append((k + right, h))
            if all(candles[k - j]["l"] > l for j in range(1, left + 1)) and \
               all(candles[k + j]["l"] > l for j in range(1, right + 1)):
                self.lows.append((k + right, l))

    @staticmethod
    def _recent(items, i, lookback, limit=40):
        out = []
        for confirmed, v in reversed(items):
            if confirmed > i:
                continue
            if confirmed < i - lookback:
                break
            out.append(v)
            if len(out) >= limit:
                break
        return out

    def resistance(self, i, price, lookback=120):
        above = [v for v in self._recent(self.highs, i, lookback) if v > price]
        return min(above) if above else None

    def support(self, i, price, lookback=120):
        below = [v for v in self._recent(self.lows, i, lookback) if v < price]
        return max(below) if below else None

    def structure(self, i, lookback=80):
        hi = self._recent(self.highs, i, lookback, limit=2)
        lo = self._recent(self.lows, i, lookback, limit=2)
        if len(hi) < 2 or len(lo) < 2:
            return None
        # _recent walks backwards, so hi[0] is the newer pivot.
        if hi[0] > hi[1] and lo[0] > lo[1]:
            return "up"
        if hi[0] < hi[1] and lo[0] < lo[1]:
            return "down"
        return None


# ---------------------------------------------------------------- series

class Series:
    """Every precomputed series for one symbol, on all three timeframes.

    Precomputing once per symbol rather than per bar is what makes a three-year
    15m replay take seconds instead of an hour, and it also removes a whole
    class of mistake: an indicator computed inside the loop over a slice that
    accidentally includes bar i+1.
    """

    def __init__(self, data, p):
        self.t = data[p["trend_tf"]]
        self.c = data[p["confirm_tf"]]
        self.e = data[p["entry_tf"]]
        self.funding = data["funding"]

        tc = [x["c"] for x in self.t]
        self.t_fast = ind.ema(tc, p["t_ema_fast"])
        self.t_mid = ind.ema(tc, p["t_ema_mid"])
        self.t_slow = ind.ema(tc, p["t_ema_slow"])
        self.t_adx = ind.adx(self.t, p["t_adx_period"])[0]
        self.t_atr = ind.atr(self.t, p["t_adx_period"])
        lr = int(p["t_piv_lr"])
        self.t_piv = PivotTracker(self.t, lr, lr)
        self.t_struct = [self.t_piv.structure(i) for i in range(len(self.t))]

        cc = [x["c"] for x in self.c]
        self.c_fast = ind.ema(cc, p["c_ema_fast"])
        self.c_mid = ind.ema(cc, p["c_ema_mid"])
        self.c_adx = ind.adx(self.c, p["c_adx_period"])[0]
        self.c_rsi = ind.rsi(cc, p["c_rsi_period"])
        clr = int(p["c_piv_lr"])
        self.c_piv = PivotTracker(self.c, clr, clr)

        ec = [x["c"] for x in self.e]
        self.e_ema = ind.ema(ec, p["e_ema"])
        self.e_mid = ind.ema(ec, p["e_ema_mid"])
        self.e_atr = ind.atr(self.e, p["e_atr_period"])
        self.e_vol = ind.sma([x.get("v", 0.0) for x in self.e], p["vol_period"])

        closes = [x["T"] for x in self.e]
        self.ti = ind.align_index(self.t, closes)   # 15m bar -> last closed 4h bar
        self.ci = ind.align_index(self.c, closes)   # 15m bar -> last closed 1h bar

        # Causal volatility percentile on the 4h, for regime reporting only.
        self.t_atr_pct = [None] * len(self.t)
        for i, a in enumerate(self.t_atr):
            if a is not None and self.t[i]["c"] > 0:
                self.t_atr_pct[i] = a / self.t[i]["c"] * 100.0


# ---------------------------------------------------------------- the rules

def net_target(side, entry, stop, rr, fee_pct, slip_pct):
    """The price at which a win banks `rr` times the planned risk, net of costs.

    The engine's `target_for` returns entry + rr * stop_distance, which ignores
    two taker fees and two slippage allowances. On this account's numbers that
    quietly turns a nominal 2.0R win into roughly 1.8R, every time, while a
    loss is still a full 1R. This solves for the target that actually nets rr.

        LONG : net/qty = T(1-s)(1-f) - E(1+f)
        SHORT: net/qty = E(1-f) - T(1+s)(1+f)

    with s the slippage fraction, f the fee fraction and `unit` the per-unit
    planned loss the engine sized against.
    """
    unit = tb.cost_per_unit(entry, stop, fee_pct, slip_pct)
    f, s = fee_pct / 100.0, slip_pct / 100.0
    if side == "LONG":
        return (rr * unit + entry * (1 + f)) / ((1 - s) * (1 - f))
    return (entry * (1 - f) - rr * unit) / ((1 + s) * (1 + f))


def trend_view(S, p, ti):
    """What the 4h bar says: 'LONG', 'SHORT' or None, plus why not.

    Returning the reason, not just the verdict, is what lets the rejection
    tally below show which filter is doing the work - and therefore which ones
    are only decoration.
    """
    if ti < 0:
        return None, "no 4h bar closed yet"
    fast, mid, slow = S.t_fast[ti], S.t_mid[ti], S.t_slow[ti]
    adx, close = S.t_adx[ti], S.t[ti]["c"]
    if fast is None or mid is None:
        return None, "4h EMAs not warm"
    if p["t_require_slow"] and slow is None:
        return None, "4h 200 EMA not warm"
    if adx is None:
        return None, "4h ADX not warm"
    if adx < p["t_adx_min"]:
        return None, f"4h ADX {adx:.1f} < {p['t_adx_min']} (no trend)"
    up = fast > mid and close > mid
    dn = fast < mid and close < mid
    if p["t_require_stack"] and slow is not None:
        up = up and mid > slow
        dn = dn and mid < slow
    if not up and not dn:
        return None, "4h EMAs not aligned"
    side = "LONG" if up else "SHORT"
    if p["t_require_structure"]:
        st = S.t_struct[ti]
        want = "up" if side == "LONG" else "down"
        if st != want:
            return None, f"4h structure {st or 'unclear'} does not confirm {side}"
    return side, ""


def confirm_view(S, p, ci, side):
    """Does the 1h bar agree? Returns (ok, reason)."""
    if ci < 0:
        return False, "no 1h bar closed yet"
    fast, mid = S.c_fast[ci], S.c_mid[ci]
    adx, r, close = S.c_adx[ci], S.c_rsi[ci], S.c[ci]["c"]
    if fast is None or mid is None or adx is None or r is None:
        return False, "1h indicators not warm"
    if adx < p["c_adx_min"]:
        return False, f"1h ADX {adx:.1f} < {p['c_adx_min']}"
    if side == "LONG":
        if not fast > mid:
            return False, "1h EMA20 not above EMA50"
        if p["c_require_close_above_mid"] and close <= mid:
            return False, "1h close below EMA50"
        if not (p["c_rsi_min"] <= r <= p["c_rsi_max"]):
            return False, (f"1h RSI {r:.0f} outside {p['c_rsi_min']:.0f}-"
                           f"{p['c_rsi_max']:.0f} (weak or exhausted)")
    else:
        if not fast < mid:
            return False, "1h EMA20 not below EMA50"
        if p["c_require_close_above_mid"] and close >= mid:
            return False, "1h close above EMA50"
        lo, hi = 100 - p["c_rsi_max"], 100 - p["c_rsi_min"]
        if not (lo <= r <= hi):
            return False, (f"1h RSI {r:.0f} outside {lo:.0f}-{hi:.0f} "
                           f"(weak or exhausted)")
    return True, ""


def entry_trigger(S, p, i, side):
    """The 15m trigger. Returns (ok, reason).

    Four things must be true at once, and each one rules out a different way of
    being wrong:

      pullback   - the bar actually came back to the moving average, so we are
                   not buying an extension that has already run
      confirmation - it closed back in the trend's direction and beyond the
                   previous bar's extreme, so the pullback is over rather than
                   still falling
      not extended - the close is not already miles from the average, which is
                   what turns a 2R target into a fantasy
      volume     - the turn had participation behind it
    """
    e, ema20, atr = S.e[i], S.e_ema[i], S.e_atr[i]
    if ema20 is None or atr is None or atr <= 0:
        return False, "15m indicators not warm"
    prev = S.e[i - 1] if i > 0 else None
    if prev is None:
        return False, "no previous 15m bar"
    tol = p["pullback_atr"] * atr
    if side == "LONG":
        if not e["l"] <= ema20 + tol:
            return False, "15m low never reached the EMA20 pullback zone"
        if p["confirm_body"] and not (e["c"] > e["o"] and e["c"] > ema20):
            return False, "15m bar did not close up and above the EMA20"
        if p["confirm_break_prev"] and not e["c"] > prev["h"]:
            return False, "15m close did not clear the previous bar's high"
        if (e["c"] - ema20) > p["max_ext_atr"] * atr:
            return False, f"15m close is {(e['c'] - ema20) / atr:.1f} ATR above the EMA20"
    else:
        if not e["h"] >= ema20 - tol:
            return False, "15m high never reached the EMA20 pullback zone"
        if p["confirm_body"] and not (e["c"] < e["o"] and e["c"] < ema20):
            return False, "15m bar did not close down and below the EMA20"
        if p["confirm_break_prev"] and not e["c"] < prev["l"]:
            return False, "15m close did not clear the previous bar's low"
        if (ema20 - e["c"]) > p["max_ext_atr"] * atr:
            return False, f"15m close is {(ema20 - e['c']) / atr:.1f} ATR below the EMA20"
    vmean = S.e_vol[i]
    if p["vol_mult"] > 0 and vmean:
        if e.get("v", 0.0) < p["vol_mult"] * vmean:
            return False, (f"15m volume {e.get('v', 0):.0f} below "
                           f"{p['vol_mult']:.2f}x its 20-bar mean {vmean:.0f}")
    return True, ""


def stop_level(S, p, i, side):
    """Structural invalidation, widened to respect volatility.

    The stop sits beyond the recent 15m swing - the level that says the
    pullback was not a pullback at all - and then, if that is closer than
    `stop_min_atr` ATR, it is pushed out to that floor. A stop inside the
    market's own noise is not a tight stop, it is a donation: it converts
    ordinary wiggle into a realised loss. Pushing it out costs position size,
    never risk, because size is solved from the stop distance.
    """
    atr = S.e_atr[i]
    if atr is None or atr <= 0:
        return None
    buf = p["stop_atr_buffer"] * atr
    ref = S.e[i]["c"]
    if side == "LONG":
        swing = ind.swing_low(S.e, i, p["swing_lb"])
        stop = min(swing - buf, ref - p["stop_min_atr"] * atr)
    else:
        swing = ind.swing_high(S.e, i, p["swing_lb"])
        stop = max(swing + buf, ref + p["stop_min_atr"] * atr)
    return stop


def volatility_block(S, p, i):
    """Reject the bar outright on abnormal or absent volatility."""
    atr, close = S.e_atr[i], S.e[i]["c"]
    if atr is None or close <= 0:
        return "15m ATR not warm"
    pct = atr / close * 100.0
    if pct < p["atr_pct_min"]:
        return f"15m ATR {pct:.2f}% below {p['atr_pct_min']}% - dead market, fees dominate"
    if pct > p["atr_pct_max"]:
        return f"15m ATR {pct:.2f}% above {p['atr_pct_max']}% - abnormal volatility"
    return None


def room_block(S, p, i, side, entry, target):
    """Refuse a trade whose target sits beyond the next structural barrier.

    Measured on the 1h pivots, which is where the levels a 2R move has to get
    through actually live. `room_mult` scales how much of the distance to the
    barrier the target is allowed to use.
    """
    if p["room_mult"] <= 0:
        return None
    ci = S.ci[i]
    if ci < 0:
        return "no 1h bar for structure"
    need = abs(target - entry) * p["room_mult"]
    lb = p["room_lookback"]
    if side == "LONG":
        level = S.c_piv.resistance(ci, entry, lb)
        if level is not None and level - entry < need:
            return (f"only {(level - entry) / abs(target - entry) * 100:.0f}% of the "
                    f"way to resistance {level:.4f} before the target")
    else:
        level = S.c_piv.support(ci, entry, lb)
        if level is not None and entry - level < need:
            return (f"only {(entry - level) / abs(target - entry) * 100:.0f}% of the "
                    f"way to support {level:.4f} before the target")
    return None


def funding_block(S, p, i, side):
    """The most recent settled funding rate, judged against the position."""
    ms = S.e[i]["T"]
    rate = None
    for r in S.funding:
        if r["ms"] <= ms:
            rate = r["pct"]
        else:
            break
    if rate is None:
        return None
    against = rate if side == "LONG" else -rate
    if against > p["funding_max_pct"]:
        return f"funding {rate:+.4f}% per 8h is against a {side}"
    return None


def regime_at(S, i):
    """A label for the market the trade was taken in. Reporting only."""
    ti = S.ti[i]
    if ti < 0:
        return "unknown"
    adx, pct = S.t_adx[ti], S.t_atr_pct[ti]
    strength = "unknown" if adx is None else (
        "strong" if adx >= 30 else "moderate" if adx >= 20 else "weak")
    vol = "unknown" if pct is None else (
        "high-vol" if pct > 2.5 else "low-vol" if pct < 1.0 else "mid-vol")
    slow, close = S.t_slow[ti], S.t[ti]["c"]
    direction = "unknown" if slow is None else ("bull" if close > slow else "bear")
    return f"{direction}/{strength}/{vol}"
