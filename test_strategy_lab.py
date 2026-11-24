#!/usr/bin/env python3
"""Tests for the research bench: indicators, rules, replay, costs and safety.

Run with:  python -m unittest test_strategy_lab

These tests are written against the two ways research goes wrong. The first is
look-ahead: an indicator or a timeframe alignment that reads a bar the market
had not printed yet, which turns a losing rule set into a winning one and is
invisible in the output. Several tests below therefore compute a value twice -
once on the full series and once on the series truncated just after the bar in
question - and assert the two agree. A function that peeks cannot pass that.

The second is a simulator that quietly flatters itself: filling at the stop
level through a gap, crediting a favourable gap on a target, resolving a bar
that touched both the stop and the target in the trade's favour, forgetting
funding, or letting two symbols each take a position when the engine permits
one. There is a test for each.

Nothing here touches the live engine's state, the journal, or the network.
"""

import copy
import json
import os
import unittest

import indicators as ind
import labdata
import labreplay as rp
import strategy_lab as lab
import trading_bot as tb


# ---------------------------------------------------------------- helpers

def bars(specs, start=0, step=900_000):
    """Build candles from (o,h,l,c) or (o,h,l,c,v) tuples."""
    out = []
    for k, s in enumerate(specs):
        o, h, l, c = s[:4]
        v = s[4] if len(s) > 4 else 1000.0
        t = start + k * step
        out.append({"t": t, "o": float(o), "h": float(h), "l": float(l),
                    "c": float(c), "v": float(v), "T": t + step - 1})
    return out


def ramp(n, lo=100.0, step=1.0, vol=1000.0, start=0, bar_ms=900_000, down=False,
         zigzag=True):
    """A trend that advances in a zig-zag, as a real one does.

    `zigzag=False` gives a perfectly monotonic ramp, which is useful for
    testing EMAs but is NOT a usable trend fixture for anything structural: a
    series whose every high exceeds the previous high contains no swing pivots
    at all, so `structure()` correctly reports None for it. Real trends pull
    back, which is what creates the higher lows the filter looks for - and
    what the strategy needs in order to have something to enter on.
    """
    out = []
    for k in range(n):
        base = lo + (-step * k if down else step * k)
        # A two-bar pullback every five bars. One bar is not enough: a swing
        # high needs `right` lower highs after it before it is confirmed, so a
        # single dip produces higher lows but no pivot highs at all, and
        # `structure()` then reports None on what looks like a clean trend.
        pull = 0.0
        if zigzag and k % 5 == 2:
            pull = step * 2.2
        elif zigzag and k % 5 == 3:
            pull = step * 2.8
        if down:
            pull = -pull
        px = base - pull
        hi = px + step * 0.6
        low = px - step * 0.6
        close = px + (step * 0.4 if not down else -step * 0.4)
        out.append((px, max(hi, close), min(low, close), close, vol))
    return bars(out, start=start, step=bar_ms)


def flat(n, px=100.0, wobble=0.2, start=0, bar_ms=900_000, vol=1000.0):
    """A sideways market: no net move, small alternating range."""
    out = []
    for k in range(n):
        up = k % 2 == 0
        c = px + (wobble if up else -wobble)
        out.append((px, px + wobble, px - wobble, c, vol))
    return bars(out, start=start, step=bar_ms)


def cfg(**over):
    c = {
        "paper_start_balance": 22.0, "risk_per_trade_pct": 0.5,
        # The absolute per-trade ceiling. sizing fails closed when it is
        # absent, so a hand-built cfg has to carry it just as load_config does.
        "max_risk_usdt_abs": 1.00,
        "max_daily_loss_pct": 2.0, "max_account_drawdown_pct": 5.0,
        "max_open_positions": 1, "max_trades_per_day": 3,
        "max_losses_per_day": 2, "loss_cooldown_min": 60,
        "monthly_drawdown_stop_pct": 6.0, "max_leverage": 5,
        "slippage_pct": 0.03, "taker_fee_pct": 0.05,
        "min_stop_pct": 0.4, "max_stop_pct": 3.0, "reward_risk": 2.0,
    }
    c.update(over)
    return c


RULES = {"step": 0.001, "min_qty": 0.001, "min_notional": 20.0, "tick": 0.01}


# ================================================================ indicators

class TestIndicatorCorrectness(unittest.TestCase):

    def test_ema_matches_the_engine(self):
        vals = [100 + (k * 7 % 13) for k in range(80)]
        self.assertEqual(ind.ema(vals, 20), tb.ema(vals, 20))

    def test_ema_is_none_before_it_is_warm(self):
        out = ind.ema([1.0] * 30, 20)
        self.assertTrue(all(v is None for v in out[:19]))
        self.assertIsNotNone(out[19])

    def test_sma_value(self):
        self.assertAlmostEqual(ind.sma([1, 2, 3, 4, 5], 5)[4], 3.0)

    def test_atr_on_a_constant_range(self):
        # Every bar: high-low = 2, and closes equal, so TR == 2 throughout.
        c = bars([(100, 101, 99, 100)] * 40)
        a = ind.atr(c, 14)
        self.assertIsNone(a[13 - 1] if len(a) > 12 else None)
        self.assertAlmostEqual(a[-1], 2.0, places=6)

    def test_atr_is_none_during_warmup(self):
        a = ind.atr(bars([(100, 101, 99, 100)] * 20), 14)
        self.assertTrue(all(v is None for v in a[:14]))
        self.assertIsNotNone(a[14])

    def test_adx_is_high_in_a_trend_and_low_in_chop(self):
        up, _, _ = ind.adx(ramp(120), 14)
        side, _, _ = ind.adx(flat(120), 14)
        self.assertGreater(up[-1], 40.0)
        self.assertLess(side[-1], 25.0)

    def test_adx_di_points_the_right_way(self):
        _, pdi, mdi = ind.adx(ramp(120), 14)
        self.assertGreater(pdi[-1], mdi[-1])
        _, pdi_d, mdi_d = ind.adx(ramp(120, lo=400.0, down=True), 14)
        self.assertGreater(mdi_d[-1], pdi_d[-1])

    def test_rsi_bounds_and_direction(self):
        up = ind.rsi([float(k) for k in range(1, 60)], 14)
        dn = ind.rsi([float(k) for k in range(60, 1, -1)], 14)
        self.assertGreater(up[-1], 95.0)
        self.assertLess(dn[-1], 5.0)
        for series in (up, dn):
            for v in series:
                if v is not None:
                    self.assertGreaterEqual(v, 0.0)
                    self.assertLessEqual(v, 100.0)

    def test_swing_low_and_high_use_the_window_inclusive(self):
        c = bars([(10, 12, 8, 11), (10, 15, 5, 11), (10, 13, 9, 11)])
        self.assertEqual(ind.swing_low(c, 2, 3), 5)
        self.assertEqual(ind.swing_high(c, 2, 3), 15)
        self.assertEqual(ind.swing_low(c, 2, 1), 9)


class TestNoLookAhead(unittest.TestCase):
    """The decisive property: a value at bar i must not change when the future
    arrives. Each test computes on the full series and on a truncation."""

    def setUp(self):
        self.c = ramp(60) + flat(40, px=160.0, start=60 * 900_000) + \
            ramp(60, lo=160.0, down=True, start=100 * 900_000)
        for k, b in enumerate(self.c):      # re-stamp a continuous timeline
            b["t"], b["T"] = k * 900_000, k * 900_000 + 899_999

    def _same_at(self, fn, i):
        full = fn(self.c)
        cut = fn(self.c[:i + 1])
        self.assertIsNotNone(full[i], "indicator not warm at the test index")
        self.assertAlmostEqual(full[i], cut[i], places=10)

    def test_atr_does_not_change_when_later_bars_arrive(self):
        self._same_at(lambda c: ind.atr(c, 14), 80)

    def test_adx_does_not_change_when_later_bars_arrive(self):
        self._same_at(lambda c: ind.adx(c, 14)[0], 90)

    def test_rsi_does_not_change_when_later_bars_arrive(self):
        self._same_at(lambda c: ind.rsi([x["c"] for x in c], 14), 80)

    def test_ema_does_not_change_when_later_bars_arrive(self):
        self._same_at(lambda c: ind.ema([x["c"] for x in c], 20), 80)

    def test_pivot_is_not_known_before_it_is_confirmed(self):
        # A pivot high at bar k is only knowable at k+right.
        c = bars([(10, 10, 9, 10), (10, 11, 9, 10), (10, 20, 9, 10),
                  (10, 11, 9, 10), (10, 10, 9, 10)])
        hi_early, _ = ind.pivots(c, 3, left=2, right=2)
        hi_late, _ = ind.pivots(c, 4, left=2, right=2)
        self.assertEqual(hi_early, [])
        self.assertEqual([v for _, v in hi_late], [20])

    def test_pivot_tracker_stamps_the_confirmation_bar(self):
        c = bars([(10, 10, 9, 10), (10, 11, 9, 10), (10, 20, 9, 10),
                  (10, 11, 9, 10), (10, 10, 9, 10)])
        pt = lab.PivotTracker(c, 2, 2)
        self.assertEqual(pt.highs, [(4, 20.0)])
        self.assertIsNone(pt.resistance(3, 15.0))      # not confirmed yet
        self.assertEqual(pt.resistance(4, 15.0), 20.0)  # confirmed now

    def test_align_index_never_picks_a_bar_that_had_not_closed(self):
        src = bars([(1, 1, 1, 1)] * 10, step=3_600_000)        # 1h
        dst = bars([(1, 1, 1, 1)] * 40, step=900_000)          # 15m
        idx = ind.align_index(src, [b["T"] for b in dst])
        for k, j in enumerate(idx):
            if j >= 0:
                self.assertLessEqual(src[j]["T"], dst[k]["T"])
            if j + 1 < len(src):
                self.assertGreater(src[j + 1]["T"], dst[k]["T"])

    def test_align_index_returns_minus_one_before_any_source_bar_closes(self):
        src = bars([(1, 1, 1, 1)] * 3, start=10_000_000, step=3_600_000)
        dst = bars([(1, 1, 1, 1)] * 3, start=0, step=900_000)
        self.assertEqual(ind.align_index(src, [b["T"] for b in dst]), [-1, -1, -1])

    def test_a_15m_bar_may_use_the_1h_bar_closing_at_the_same_instant(self):
        src = bars([(1, 1, 1, 1)] * 2, step=3_600_000)   # closes at 3599999
        dst = bars([(1, 1, 1, 1)] * 4, step=900_000)     # bar 3 closes 3599999
        self.assertEqual(ind.align_index(src, [b["T"] for b in dst]),
                         [-1, -1, -1, 0])


# ================================================================ structure

class TestStructure(unittest.TestCase):

    def test_higher_highs_and_higher_lows_reads_up(self):
        self.assertEqual(lab.PivotTracker(ramp(120), 2, 2).structure(119), "up")

    def test_lower_highs_and_lower_lows_reads_down(self):
        c = ramp(120, lo=400.0, down=True)
        self.assertEqual(lab.PivotTracker(c, 2, 2).structure(119), "down")

    def test_a_sideways_market_has_no_structure(self):
        self.assertIsNone(lab.PivotTracker(flat(120), 2, 2).structure(119))

    def test_structure_is_none_without_two_pivots_of_each_kind(self):
        self.assertIsNone(lab.PivotTracker(ramp(8), 2, 2).structure(7))


# ================================================================ the rules

class TestParams(unittest.TestCase):

    def test_unknown_parameter_is_refused(self):
        with self.assertRaises(KeyError):
            lab.params(no_such_knob=1)

    def test_known_parameter_overrides(self):
        self.assertEqual(lab.params(rr_net=3.0)["rr_net"], 3.0)

    def test_defaults_are_not_mutated_by_an_override(self):
        lab.params(rr_net=9.0)
        self.assertEqual(lab.LAB_DEFAULTS["rr_net"], 2.0)


class FakeSeries:
    """A Series-shaped object assembled by hand, for filter unit tests."""

    def __init__(self, p, trend=None, confirm=None, entry=None):
        self.t = trend if trend is not None else ramp(400, step=4.0, bar_ms=14_400_000)
        self.c = confirm if confirm is not None else ramp(400, bar_ms=3_600_000)
        self.e = entry if entry is not None else ramp(400)
        self.funding = []
        tc = [x["c"] for x in self.t]
        self.t_fast = ind.ema(tc, p["t_ema_fast"])
        self.t_mid = ind.ema(tc, p["t_ema_mid"])
        self.t_slow = ind.ema(tc, p["t_ema_slow"])
        self.t_adx = ind.adx(self.t, p["t_adx_period"])[0]
        self.t_atr = ind.atr(self.t, p["t_adx_period"])
        self.t_piv = lab.PivotTracker(self.t, p["t_piv_lr"], p["t_piv_lr"])
        self.t_struct = [self.t_piv.structure(i) for i in range(len(self.t))]
        self.t_atr_pct = [None if a is None else a / self.t[i]["c"] * 100
                          for i, a in enumerate(self.t_atr)]
        cc = [x["c"] for x in self.c]
        self.c_fast = ind.ema(cc, p["c_ema_fast"])
        self.c_mid = ind.ema(cc, p["c_ema_mid"])
        self.c_adx = ind.adx(self.c, p["c_adx_period"])[0]
        self.c_rsi = ind.rsi(cc, p["c_rsi_period"])
        self.c_piv = lab.PivotTracker(self.c, p["c_piv_lr"], p["c_piv_lr"])
        ec = [x["c"] for x in self.e]
        self.e_ema = ind.ema(ec, p["e_ema"])
        self.e_mid = ind.ema(ec, p["e_ema_mid"])
        self.e_atr = ind.atr(self.e, p["e_atr_period"])
        self.e_vol = ind.sma([x["v"] for x in self.e], p["vol_period"])
        self.ti = [len(self.t) - 1] * len(self.e)
        self.ci = [len(self.c) - 1] * len(self.e)


class TestTrendFilter(unittest.TestCase):

    def test_a_clean_uptrend_reads_long(self):
        p = lab.params(t_require_stack=True, t_require_structure=True)
        side, why = lab.trend_view(FakeSeries(p), p, 399)
        self.assertEqual(side, "LONG", why)

    def test_a_clean_downtrend_reads_short(self):
        p = lab.params(t_require_stack=True, t_require_structure=True)
        S = FakeSeries(p, trend=ramp(400, lo=2000.0, step=4.0,
                                     bar_ms=14_400_000, down=True))
        side, why = lab.trend_view(S, p, 399)
        self.assertEqual(side, "SHORT", why)

    def test_a_sideways_market_is_refused(self):
        p = lab.params()
        S = FakeSeries(p, trend=flat(400, px=100.0, bar_ms=14_400_000))
        side, why = lab.trend_view(S, p, 399)
        self.assertIsNone(side)
        self.assertTrue(why)

    def test_low_adx_is_refused_even_when_the_emas_are_aligned(self):
        p = lab.params(t_adx_min=99.0)
        side, why = lab.trend_view(FakeSeries(p), p, 399)
        self.assertIsNone(side)
        self.assertIn("ADX", why)

    def test_no_closed_higher_timeframe_bar_is_refused(self):
        p = lab.params()
        side, why = lab.trend_view(FakeSeries(p), p, -1)
        self.assertIsNone(side)

    def test_an_unwarm_200_ema_is_refused_when_required(self):
        p = lab.params(t_require_slow=True)
        S = FakeSeries(p, trend=ramp(60, step=4.0, bar_ms=14_400_000))
        side, why = lab.trend_view(S, p, 59)
        self.assertIsNone(side)
        self.assertIn("200", why)


class TestConfirmFilter(unittest.TestCase):

    def test_an_aligned_confirmation_passes(self):
        p = lab.params(c_adx_min=0.0, c_rsi_min=0.0, c_rsi_max=100.0)
        ok, why = lab.confirm_view(FakeSeries(p), p, 399, "LONG")
        self.assertTrue(ok, why)

    def test_a_conflicting_timeframe_is_refused(self):
        """4h says LONG, 1h is in a downtrend - the trade must be rejected."""
        p = lab.params(c_adx_min=0.0, c_rsi_min=0.0, c_rsi_max=100.0)
        S = FakeSeries(p, confirm=ramp(400, lo=2000.0, bar_ms=3_600_000, down=True))
        ok, why = lab.confirm_view(S, p, 399, "LONG")
        self.assertFalse(ok)
        self.assertIn("EMA", why)

    def test_an_exhausted_push_is_refused_by_the_rsi_ceiling(self):
        p = lab.params(c_adx_min=0.0, c_rsi_min=45.0, c_rsi_max=60.0)
        ok, why = lab.confirm_view(FakeSeries(p), p, 399, "LONG")
        self.assertFalse(ok)
        self.assertIn("RSI", why)

    def test_the_rsi_band_is_mirrored_for_a_short(self):
        """A short's band is 100-max .. 100-min, so a reading of r behaves for
        a SHORT exactly as 100-r does for a LONG."""
        S_up = FakeSeries(lab.params())
        S_dn = FakeSeries(lab.params(),
                          confirm=ramp(400, lo=2000.0, bar_ms=3_600_000, down=True))
        r_dn = S_dn.c_rsi[399]
        self.assertIsNotNone(r_dn)
        band = lab.params(c_adx_min=0.0, c_rsi_min=round(100 - r_dn) - 5,
                          c_rsi_max=round(100 - r_dn) + 5)
        ok, why = lab.confirm_view(S_dn, band, 399, "SHORT")
        self.assertTrue(ok, f"RSI {r_dn:.1f} should sit inside the mirrored band: {why}")

    def test_an_exhausted_downmove_is_refused_by_the_mirrored_floor(self):
        p = lab.params(c_adx_min=0.0, c_rsi_min=45.0, c_rsi_max=72.0)
        S = FakeSeries(p, confirm=ramp(400, lo=2000.0, bar_ms=3_600_000, down=True))
        ok, why = lab.confirm_view(S, p, 399, "SHORT")
        self.assertFalse(ok)
        self.assertIn("RSI", why)

    def test_weak_1h_adx_is_refused(self):
        p = lab.params(c_adx_min=99.0)
        ok, why = lab.confirm_view(FakeSeries(p), p, 399, "LONG")
        self.assertFalse(ok)
        self.assertIn("ADX", why)


class TestEntryTrigger(unittest.TestCase):
    """The 15m trigger, built bar by bar so each clause can be isolated."""

    def _build(self, shape, **over):
        """Append one bar shaped relative to the live EMA20 and ATR.

        `shape(ema, atr)` returns (open, high, low, close, volume). Building the
        bar from the indicator values rather than from hard-coded prices is what
        keeps these tests about the rule instead of about the fixture's price
        level - the earlier version placed a '124' bar under a 150 EMA and
        failed for a reason that had nothing to do with the clause under test.
        """
        p = lab.params(**over)
        base = ramp(60)
        probe = FakeSeries(p, entry=base)
        ema, atr = probe.e_ema[-1], probe.e_atr[-1]
        o, h, l, c, v = shape(ema, atr)
        e = base + [{"t": 0, "o": o, "h": h, "l": l, "c": c, "v": v, "T": 0}]
        for k, b in enumerate(e):
            b["t"], b["T"] = k * 900_000, k * 900_000 + 899_999
        return p, FakeSeries(p, entry=e), ema, atr

    @staticmethod
    def _pullback(ema, atr, vol=50_000.0):
        """Dips to the average, closes back up just above it."""
        return (ema - 0.2 * atr, ema + 0.5 * atr, ema - 0.6 * atr,
                ema + 0.3 * atr, vol)

    def test_a_pullback_with_confirmation_passes(self):
        p, S, _, _ = self._build(self._pullback, vol_mult=0.0,
                                 confirm_break_prev=False, max_ext_atr=99.0,
                                 pullback_atr=1.0)
        ok, why = lab.entry_trigger(S, p, len(S.e) - 1, "LONG")
        self.assertTrue(ok, why)

    def test_a_bar_that_never_reaches_the_zone_is_refused(self):
        p, S, _, _ = self._build(
            lambda ema, atr: (ema + 8 * atr, ema + 9 * atr, ema + 7 * atr,
                              ema + 8.5 * atr, 50_000.0),
            vol_mult=0.0, pullback_atr=0.1, max_ext_atr=99.0,
            confirm_break_prev=False)
        ok, why = lab.entry_trigger(S, p, len(S.e) - 1, "LONG")
        self.assertFalse(ok)
        self.assertIn("pullback zone", why)

    def test_a_red_bar_is_not_a_long_confirmation(self):
        p, S, _, _ = self._build(
            lambda ema, atr: (ema + 0.3 * atr, ema + 0.5 * atr, ema - 0.9 * atr,
                              ema - 0.8 * atr, 50_000.0),
            vol_mult=0.0, pullback_atr=1.0, max_ext_atr=99.0,
            confirm_break_prev=False)
        ok, why = lab.entry_trigger(S, p, len(S.e) - 1, "LONG")
        self.assertFalse(ok)
        self.assertIn("close up", why)

    def test_an_extended_close_is_refused(self):
        p, S, _, _ = self._build(
            lambda ema, atr: (ema - 0.2 * atr, ema + 5 * atr, ema - 0.5 * atr,
                              ema + 4.5 * atr, 50_000.0),
            vol_mult=0.0, pullback_atr=1.0, max_ext_atr=1.5,
            confirm_break_prev=False)
        ok, why = lab.entry_trigger(S, p, len(S.e) - 1, "LONG")
        self.assertFalse(ok)
        self.assertIn("ATR above", why)

    def test_thin_volume_is_refused(self):
        p, S, _, _ = self._build(lambda ema, atr: self._pullback(ema, atr, 1.0),
                                 vol_mult=1.0, pullback_atr=1.0,
                                 max_ext_atr=99.0, confirm_break_prev=False)
        ok, why = lab.entry_trigger(S, p, len(S.e) - 1, "LONG")
        self.assertFalse(ok)
        self.assertIn("volume", why)

    def test_a_close_that_does_not_clear_the_previous_bar_is_refused(self):
        p, S, _, _ = self._build(self._pullback, vol_mult=0.0,
                                 pullback_atr=1.0, max_ext_atr=99.0,
                                 confirm_break_prev=True)
        ok, why = lab.entry_trigger(S, p, len(S.e) - 1, "LONG")
        self.assertFalse(ok)
        self.assertIn("previous bar", why)

    def test_unwarm_indicators_are_refused_rather_than_treated_as_zero(self):
        p = lab.params()
        S = FakeSeries(p, entry=ramp(5))
        ok, why = lab.entry_trigger(S, p, 4, "LONG")
        self.assertFalse(ok)
        self.assertIn("warm", why)


class TestVolatilityFilter(unittest.TestCase):

    def test_a_dead_market_is_refused(self):
        p = lab.params(atr_pct_min=0.5)
        c = bars([(100.0, 100.01, 99.99, 100.0)] * 40)
        S = FakeSeries(p, entry=c)
        self.assertIn("dead market", lab.volatility_block(S, p, 39))

    def test_abnormal_volatility_is_refused(self):
        p = lab.params(atr_pct_max=0.5)
        c = bars([(100.0, 120.0, 80.0, 100.0)] * 40)
        S = FakeSeries(p, entry=c)
        self.assertIn("abnormal", lab.volatility_block(S, p, 39))

    def test_a_normal_market_passes(self):
        p = lab.params(atr_pct_min=0.1, atr_pct_max=5.0)
        c = bars([(100.0, 100.6, 99.4, 100.0)] * 40)
        S = FakeSeries(p, entry=c)
        self.assertIsNone(lab.volatility_block(S, p, 39))

    def test_an_unwarm_atr_is_refused(self):
        p = lab.params()
        S = FakeSeries(p, entry=ramp(5))
        self.assertIn("not warm", lab.volatility_block(S, p, 4))


# ================================================================ stop & target

class TestStopPlacement(unittest.TestCase):

    def test_a_long_stop_sits_below_the_swing_low(self):
        p = lab.params(swing_lb=8, stop_atr_buffer=0.5, stop_min_atr=0.0)
        c = ramp(40)
        S = FakeSeries(p, entry=c)
        stop = lab.stop_level(S, p, 39, "LONG")
        self.assertLess(stop, ind.swing_low(c, 39, 8))

    def test_a_short_stop_sits_above_the_swing_high(self):
        p = lab.params(swing_lb=8, stop_atr_buffer=0.5, stop_min_atr=0.0)
        c = ramp(40, lo=400.0, down=True)
        S = FakeSeries(p, entry=c)
        stop = lab.stop_level(S, p, 39, "SHORT")
        self.assertGreater(stop, ind.swing_high(c, 39, 8))

    def test_the_stop_is_never_tighter_than_the_atr_floor(self):
        """A swing an inch away must not become an inch-wide stop."""
        p = lab.params(swing_lb=2, stop_atr_buffer=0.0, stop_min_atr=2.0)
        c = bars([(100, 101, 99, 100)] * 40)
        S = FakeSeries(p, entry=c)
        stop = lab.stop_level(S, p, 39, "LONG")
        atr = S.e_atr[39]
        self.assertLessEqual(stop, c[39]["c"] - 2.0 * atr + 1e-9)

    def test_a_long_stop_is_always_below_the_reference_price(self):
        p = lab.params()
        S = FakeSeries(p, entry=ramp(40))
        self.assertLess(lab.stop_level(S, p, 39, "LONG"), S.e[39]["c"])

    def test_a_short_stop_is_always_above_the_reference_price(self):
        p = lab.params()
        S = FakeSeries(p, entry=ramp(40, lo=400.0, down=True))
        self.assertGreater(lab.stop_level(S, p, 39, "SHORT"), S.e[39]["c"])

    def test_no_stop_without_a_warm_atr(self):
        p = lab.params()
        self.assertIsNone(lab.stop_level(FakeSeries(p, entry=ramp(5)), p, 4, "LONG"))


class TestNetTarget(unittest.TestCase):
    """The target must bank rr x planned risk AFTER both fees and both
    slippage legs - which `trading_bot.target_for` does not do."""

    def _check(self, side, entry, stop, rr, fee=0.05, slip=0.03):
        tgt = lab.net_target(side, entry, stop, rr, fee, slip)
        qty = 3.0
        unit = tb.cost_per_unit(entry, stop, fee, slip)
        exit_fill = tb.adverse_fill(side, tgt, slip, entering=False)
        pnl, _ = tb.trade_pnl(side, entry, exit_fill, qty, fee)
        self.assertAlmostEqual(pnl, rr * qty * unit, places=8)

    def test_long_target_nets_the_requested_multiple(self):
        self._check("LONG", 100.0, 99.0, 2.0)

    def test_short_target_nets_the_requested_multiple(self):
        self._check("SHORT", 100.0, 101.0, 2.0)

    def test_it_holds_at_other_multiples(self):
        for rr in (1.0, 1.5, 2.5, 3.0):
            self._check("LONG", 2500.0, 2460.0, rr)
            self._check("SHORT", 2500.0, 2540.0, rr)

    def test_it_is_further_away_than_the_engines_cost_blind_target(self):
        naive = tb.target_for("LONG", 100.0, 99.0, 2.0)
        net = lab.net_target("LONG", 100.0, 99.0, 2.0, 0.05, 0.03)
        self.assertGreater(net, naive)

    def test_a_short_net_target_is_lower_than_the_cost_blind_one(self):
        naive = tb.target_for("SHORT", 100.0, 101.0, 2.0)
        net = lab.net_target("SHORT", 100.0, 101.0, 2.0, 0.05, 0.03)
        self.assertLess(net, naive)


class TestRoomAndFunding(unittest.TestCase):

    def test_room_filter_is_off_when_the_multiplier_is_zero(self):
        p = lab.params(room_mult=0.0)
        self.assertIsNone(lab.room_block(FakeSeries(p), p, 0, "LONG", 100, 110))

    def test_a_target_beyond_nearby_resistance_is_refused(self):
        p = lab.params(room_mult=1.0, c_piv_lr=2)
        c = bars([(10, 10, 9, 10), (10, 11, 9, 10), (10, 120, 9, 10),
                  (10, 11, 9, 10), (10, 10, 9, 10)] + [(10, 10, 9, 10)] * 10,
                 step=3_600_000)
        S = FakeSeries(p, confirm=c)
        S.ci = [len(c) - 1] * len(S.e)
        self.assertIn("resistance", lab.room_block(S, p, 0, "LONG", 100.0, 130.0))

    def test_funding_against_a_long_is_refused(self):
        p = lab.params(funding_max_pct=0.05)
        S = FakeSeries(p)
        S.funding = [{"ms": 0, "pct": 0.5}]
        S.e[0]["T"] = 10
        self.assertIn("funding", lab.funding_block(S, p, 0, "LONG"))

    def test_the_same_rate_is_a_credit_to_a_short(self):
        p = lab.params(funding_max_pct=0.05)
        S = FakeSeries(p)
        S.funding = [{"ms": 0, "pct": 0.5}]
        S.e[0]["T"] = 10
        self.assertIsNone(lab.funding_block(S, p, 0, "SHORT"))

    def test_no_funding_history_does_not_block(self):
        p = lab.params()
        S = FakeSeries(p)
        S.funding = []
        self.assertIsNone(lab.funding_block(S, p, 0, "LONG"))


# ================================================================ funding cost

class TestFundingCost(unittest.TestCase):

    ROWS = [{"ms": 1000, "pct": 0.01}, {"ms": 2000, "pct": 0.01},
            {"ms": 3000, "pct": -0.02}]

    def test_a_long_pays_a_positive_rate(self):
        v = labdata.funding_cost(self.ROWS[:1], "LONG", 2.0, 100.0, 0, 1500)
        self.assertAlmostEqual(v, 0.01 / 100 * 200.0)

    def test_a_short_receives_a_positive_rate(self):
        v = labdata.funding_cost(self.ROWS[:1], "SHORT", 2.0, 100.0, 0, 1500)
        self.assertAlmostEqual(v, -0.01 / 100 * 200.0)

    def test_only_settlements_strictly_inside_the_hold_are_charged(self):
        self.assertEqual(labdata.funding_cost(self.ROWS, "LONG", 1.0, 100.0,
                                              1000, 1000), 0.0)
        two = labdata.funding_cost(self.ROWS, "LONG", 1.0, 100.0, 999, 2000)
        self.assertAlmostEqual(two, 2 * 0.01 / 100 * 100.0)

    def test_a_negative_rate_credits_a_long(self):
        v = labdata.funding_cost(self.ROWS, "LONG", 1.0, 100.0, 2500, 3500)
        self.assertLess(v, 0)

    def test_a_trade_spanning_no_settlement_costs_nothing(self):
        self.assertEqual(labdata.funding_cost(self.ROWS, "LONG", 1.0, 100.0,
                                              1001, 1999), 0.0)


# ================================================================ the replay

def replay_series(entry, p, trend=None, confirm=None, funding=None):
    S = FakeSeries(p, trend=trend, confirm=confirm, entry=entry)
    S.funding = funding or []
    return S


class TestReplayMechanics(unittest.TestCase):
    """Fills, gaps, ambiguity and the one-position rule."""

    def setUp(self):
        self.p = lab.params()
        self.cfg = cfg()

    def _forced(self, e_bars, side="LONG", stop=None, target=None, **over):
        """Drive the replay with the signal stage stubbed out, so the exit
        mechanics can be tested on an exact, hand-built bar sequence."""
        p = lab.params(**over)
        S = replay_series(e_bars, p)
        c = dict(self.cfg)
        fired = {"n": 0}

        def fake_signal(cfg_, p_, S_, i, rules, bal, risk, fee, slip, frac, note):
            if fired["n"] or i != 0:
                return None
            fired["n"] = 1
            entry = tb.adverse_fill(side, S_.e[1]["o"], slip, entering=True)
            st = stop if stop is not None else entry * 0.99
            tg = target if target is not None else entry * 1.02
            qty, planned, _ = tb.size_position(bal, entry, st, RULES, cfg_,
                                               fractional=True)
            if qty <= 0:
                return None
            return {"side": side, "entry": entry, "stop": st, "target": tg,
                    "qty": qty, "planned_risk": planned,
                    "raw_entry": S_.e[1]["o"]}

        real, rp._signal = rp._signal, fake_signal
        try:
            return rp.replay(c, p, {"X": S}, {"X": RULES},
                             S.e[0]["T"], S.e[-1]["T"] + 1)
        finally:
            rp._signal = real

    def test_entry_fills_at_the_next_bar_open_with_adverse_slippage(self):
        e = bars([(100, 101, 99, 100), (100, 101, 99, 100)] + [(100, 101, 99, 100)] * 30)
        r = self._forced(e, stop=50.0, target=1e9)
        self.assertTrue(r["open"])
        pos = r["open"][0]
        self.assertAlmostEqual(pos["entry"], 100.0 * (1 + 0.03 / 100))

    def test_a_stop_gap_fills_at_the_open_not_the_stop_level(self):
        e = bars([(100, 101, 99, 100), (100, 100, 100, 100),
                  (80, 81, 79, 80)] + [(80, 81, 79, 80)] * 30)
        r = self._forced(e, stop=95.0, target=1e9)
        self.assertEqual(len(r["trades"]), 1)
        t = r["trades"][0]
        self.assertEqual(t["exit_reason"], "stop loss")
        self.assertAlmostEqual(t["raw_exit"], 80.0)   # the gap open, not 95

    def test_a_target_gap_still_fills_exactly_at_the_target(self):
        e = bars([(100, 101, 99, 100), (100, 100, 100, 100),
                  (130, 131, 129, 130)] + [(130, 131, 129, 130)] * 30)
        r = self._forced(e, stop=50.0, target=110.0)
        t = r["trades"][0]
        self.assertEqual(t["exit_reason"], "take profit")
        self.assertAlmostEqual(t["raw_exit"], 110.0)

    def test_a_bar_touching_both_is_booked_as_a_loss_and_counted(self):
        e = bars([(100, 101, 99, 100), (100, 100, 100, 100),
                  (100, 130, 70, 100)] + [(100, 101, 99, 100)] * 30)
        r = self._forced(e, stop=95.0, target=110.0)
        self.assertEqual(r["ambiguous_bars"], 1)
        self.assertEqual(r["trades"][0]["exit_reason"], "stop loss")
        self.assertGreater(r["ambiguous_gain"], 0.0)

    def test_funding_is_charged_across_a_settlement(self):
        p = lab.params()
        e = bars([(100, 101, 99, 100)] * 40)
        S = replay_series(e, p, funding=[{"ms": e[3]["t"], "pct": 0.1}])
        fired = {"n": 0}

        def fake_signal(cfg_, p_, S_, i, rules, bal, risk, fee, slip, frac, note):
            if fired["n"] or i != 0:
                return None
            fired["n"] = 1
            entry = tb.adverse_fill("LONG", S_.e[1]["o"], slip, entering=True)
            qty, planned, _ = tb.size_position(bal, entry, 95.0, RULES, cfg_,
                                               fractional=True)
            # Stop and target both far away, so the trade is still open when
            # the settlement at bar 3 passes and is only closed later.
            return {"side": "LONG", "entry": entry, "stop": 50.0,
                    "target": 1e9, "qty": qty, "planned_risk": planned,
                    "raw_entry": S_.e[1]["o"]}

        real, rp._signal = rp._signal, fake_signal
        try:
            r = rp.replay(cfg(), p, {"X": S}, {"X": RULES},
                          S.e[0]["T"], S.e[-1]["T"] + 1)
        finally:
            rp._signal = real
        self.assertEqual(len(r["open"]), 1, "the position should still be open")
        pos = r["open"][0]
        charged = labdata.funding_cost(S.funding, "LONG", pos["qty"],
                                       pos["entry"], pos["opened_ms"],
                                       S.e[-1]["T"])
        self.assertGreater(charged, 0.0, "a long must pay a positive funding rate")

    def test_a_closed_trade_carries_the_funding_it_paid(self):
        """The settlement must reach the booked P&L, not just the helper."""
        p = lab.params()
        quiet = [(100.0, 100.2, 99.8, 100.0)] * 12
        spike = [(100.0, 120.0, 99.8, 119.0)]
        e = bars(quiet + spike + quiet)
        S = replay_series(e, p, funding=[{"ms": e[4]["t"], "pct": 0.5}])
        fired = {"n": 0}

        def fake_signal(cfg_, p_, S_, i, rules, bal, risk, fee, slip, frac, note):
            if fired["n"] or i != 0:
                return None
            fired["n"] = 1
            entry = tb.adverse_fill("LONG", S_.e[1]["o"], slip, entering=True)
            qty, planned, _ = tb.size_position(bal, entry, 95.0, RULES, cfg_,
                                               fractional=True)
            return {"side": "LONG", "entry": entry, "stop": 95.0,
                    "target": 110.0, "qty": qty, "planned_risk": planned,
                    "raw_entry": S_.e[1]["o"]}

        real, rp._signal = rp._signal, fake_signal
        try:
            r = rp.replay(cfg(), p, {"X": S}, {"X": RULES},
                          e[0]["T"], e[-1]["T"] + 1)
        finally:
            rp._signal = real
        self.assertEqual(len(r["trades"]), 1)
        t = r["trades"][0]
        self.assertEqual(t["exit_reason"], "take profit")
        self.assertGreater(t["funding"], 0.0)
        # And the charge must be subtracted from the booked P&L, not merely
        # recorded beside it.
        gross, fees = tb.trade_pnl("LONG", t["entry"], t["exit"], t["qty"], 0.05)
        self.assertAlmostEqual(t["pnl"], gross - t["funding"], places=9)

    def test_never_more_than_max_open_positions_across_symbols(self):
        p = lab.params()
        e = bars([(100, 101, 99, 100)] * 60)
        S1, S2 = replay_series(e, p), replay_series(e, p)

        def always(cfg_, p_, S_, i, rules, bal, risk, fee, slip, frac, note):
            entry = tb.adverse_fill("LONG", S_.e[i + 1]["o"], slip, entering=True)
            qty, planned, _ = tb.size_position(bal, entry, 95.0, RULES, cfg_,
                                               fractional=True)
            if qty <= 0:
                return None
            return {"side": "LONG", "entry": entry, "stop": 95.0,
                    "target": 1e9, "qty": qty, "planned_risk": planned,
                    "raw_entry": S_.e[i + 1]["o"]}

        real, rp._signal = rp._signal, always
        try:
            r = rp.replay(cfg(max_open_positions=1), p, {"A": S1, "B": S2},
                          {"A": RULES, "B": RULES}, e[0]["T"], e[-1]["T"] + 1)
        finally:
            rp._signal = real
        self.assertEqual(len(r["open"]), 1)
        self.assertEqual(len(r["trades"]), 0)

    def test_an_empty_window_is_safe(self):
        p = lab.params()
        S = replay_series(bars([(100, 101, 99, 100)] * 5), p)
        r = rp.replay(cfg(), p, {"X": S}, {"X": RULES}, 10 ** 15, 10 ** 15 + 1)
        self.assertEqual(r["trades"], [])
        self.assertEqual(r["end"], r["start"])


class TestReplayRiskGates(unittest.TestCase):
    """The replay must be stopped by the same limits as the live engine."""

    def _always_long(self, stop_frac=0.99):
        def always(cfg_, p_, S_, i, rules, bal, risk, fee, slip, frac, note):
            if i + 1 >= len(S_.e):
                return None
            entry = tb.adverse_fill("LONG", S_.e[i + 1]["o"], slip, entering=True)
            st = entry * stop_frac
            qty, planned, _ = tb.size_position(bal, entry, st, RULES, cfg_,
                                               fractional=True)
            if qty <= 0:
                return None
            return {"side": "LONG", "entry": entry, "stop": st,
                    "target": entry * 1.5, "qty": qty, "planned_risk": planned,
                    "raw_entry": S_.e[i + 1]["o"]}
        return always

    def _losing_run(self, n=400):
        """Every trade stops out: price steps down relentlessly."""
        out = []
        px = 1000.0
        for _ in range(n):
            out.append((px, px * 1.0005, px * 0.97, px * 0.975))
            px *= 0.975
        return bars(out)

    def _run(self, c, bars_, p=None):
        p = p or lab.params()
        S = replay_series(bars_, p)
        real, rp._signal = rp._signal, self._always_long()
        try:
            return rp.replay(c, p, {"X": S}, {"X": RULES},
                             bars_[0]["T"], bars_[-1]["T"] + 1)
        finally:
            rp._signal = real

    def test_the_drawdown_halt_fires_and_stops_all_further_entries(self):
        r = self._run(cfg(max_trades_per_day=999, loss_cooldown_min=0,
                          max_losses_per_day=999), self._losing_run())
        self.assertTrue(r["halted"])
        self.assertIn("drawdown", r["halt_reason"])
        # Equity must not have fallen far past the limit after the halt.
        self.assertLess((r["start"] - r["end"]) / r["start"] * 100, 8.0)

    def test_max_trades_per_day_is_respected(self):
        c = cfg(max_trades_per_day=2, max_losses_per_day=999,
                loss_cooldown_min=0, max_account_drawdown_pct=1e9,
                max_daily_loss_pct=1e9, monthly_drawdown_stop_pct=1e9)
        day = 96          # 15m bars in a day
        r = self._run(c, self._losing_run(day * 3))
        by_day = {}
        for t in r["trades"]:
            by_day.setdefault(tb.day_key(t["opened_ms"]), []).append(t)
        for d, ts in by_day.items():
            self.assertLessEqual(len(ts), 2, f"{d} took {len(ts)} trades")

    def test_the_loss_cooldown_spaces_entries_out(self):
        c = cfg(loss_cooldown_min=60, max_trades_per_day=999,
                max_losses_per_day=999, max_account_drawdown_pct=1e9,
                max_daily_loss_pct=1e9, monthly_drawdown_stop_pct=1e9)
        r = self._run(c, self._losing_run(400))
        closes = sorted(t["closed_ms"] for t in r["trades"])
        opens = sorted(t["opened_ms"] for t in r["trades"])
        for o in opens[1:]:
            prior = [x for x in closes if x <= o]
            if prior:
                self.assertGreaterEqual((o - max(prior)) / 60000.0, 59.0)

    def test_max_losses_per_day_stops_the_day(self):
        c = cfg(max_losses_per_day=1, max_trades_per_day=999,
                loss_cooldown_min=0, max_account_drawdown_pct=1e9,
                max_daily_loss_pct=1e9, monthly_drawdown_stop_pct=1e9)
        r = self._run(c, self._losing_run(96 * 3))
        by_day = {}
        for t in r["trades"]:
            if t["pnl"] <= 0:
                by_day.setdefault(tb.day_key(t["closed_ms"]), 0)
                by_day[tb.day_key(t["closed_ms"])] += 1
        for d, n in by_day.items():
            self.assertLessEqual(n, 1, f"{d} booked {n} losses")

    def test_the_daily_loss_limit_locks_the_day(self):
        c = cfg(max_daily_loss_pct=1.0, max_trades_per_day=999,
                max_losses_per_day=999, loss_cooldown_min=0,
                max_account_drawdown_pct=1e9, monthly_drawdown_stop_pct=1e9)
        r = self._run(c, self._losing_run(96 * 2))
        for d in {tb.day_key(t["closed_ms"]) for t in r["trades"]}:
            day = [t for t in r["trades"] if tb.day_key(t["closed_ms"]) == d]
            start = day[0]["equity_before"]
            worst = min(t["equity_after"] for t in day)
            self.assertLess((start - worst) / start * 100, 3.0)

    def test_the_monthly_stop_blocks_entries(self):
        c = cfg(monthly_drawdown_stop_pct=1.0, max_trades_per_day=999,
                max_losses_per_day=999, loss_cooldown_min=0,
                max_account_drawdown_pct=1e9, max_daily_loss_pct=1e9)
        r = self._run(c, self._losing_run(96 * 10))
        self.assertLess((r["start"] - r["end"]) / r["start"] * 100, 4.0)

    def test_a_trade_is_rejected_when_sizing_returns_nothing(self):
        c = cfg(risk_per_trade_pct=0.0)
        r = self._run(c, self._losing_run(100))
        self.assertEqual(r["trades"], [])


class TestNonFractionalSizingRejectsSmallAccounts(unittest.TestCase):
    """What a real (non-paper) order would do on a $22 account."""

    def test_the_exchange_minimum_is_never_met_by_raising_risk(self):
        qty, risk, note = tb.size_position(22.0, 2500.0, 2475.0, RULES,
                                           cfg(), fractional=False)
        self.assertEqual(qty, 0.0)
        self.assertEqual(risk, 0.0)
        self.assertIn("never raised", note)

    def test_paper_fractional_sizing_keeps_the_risk_at_the_cap(self):
        qty, risk, note = tb.size_position(22.0, 2500.0, 2475.0, RULES,
                                           cfg(), fractional=True)
        self.assertGreater(qty, 0.0)
        self.assertLessEqual(risk, 22.0 * 0.5 / 100 + 1e-9)
        self.assertIn("below the exchange minimum", note)


class TestTrailingStop(unittest.TestCase):

    def test_breakeven_trail_moves_the_stop_only_after_the_trigger(self):
        p = lab.params(trail="breakeven", trail_trigger_r=1.0)
        S = replay_series(bars([(100, 101, 99, 100)] * 40), p)
        pos = {"side": "LONG", "entry": 100.0, "initial_stop": 99.0,
               "stop": 99.0, "best": 100.0}
        rp._advance_stop(p, S, pos, 5, {"h": 100.5, "l": 100.0})
        self.assertEqual(pos["stop"], 99.0)          # only 0.5R, no move
        rp._advance_stop(p, S, pos, 5, {"h": 101.5, "l": 100.0})
        self.assertEqual(pos["stop"], 100.0)         # 1.5R, moved to entry

    def test_a_trail_can_never_widen_the_stop(self):
        p = lab.params(trail="atr", trail_trigger_r=0.0, trail_atr=50.0)
        S = replay_series(bars([(100, 101, 99, 100)] * 40), p)
        pos = {"side": "LONG", "entry": 100.0, "initial_stop": 99.0,
               "stop": 99.5, "best": 100.0}
        rp._advance_stop(p, S, pos, 20, {"h": 101.0, "l": 100.0})
        self.assertGreaterEqual(pos["stop"], 99.5)

    def test_trail_off_never_touches_the_stop(self):
        p = lab.params(trail="off")
        S = replay_series(bars([(100, 101, 99, 100)] * 40), p)
        pos = {"side": "LONG", "entry": 100.0, "initial_stop": 99.0,
               "stop": 99.0, "best": 100.0}
        rp._advance_stop(p, S, pos, 20, {"h": 200.0, "l": 100.0})
        self.assertEqual(pos["stop"], 99.0)


# ================================================================ statistics

class TestStats(unittest.TestCase):

    def rows(self, pnls):
        out = []
        for k, v in enumerate(pnls):
            out.append({"pnl": v, "planned_risk": 1.0, "fees": 0.0,
                        "slippage": 0.0, "funding": 0.0, "hold_hours": 1.0,
                        "closed_ms": k * 1000, "side": "LONG", "symbol": "X"})
        return out

    def test_profit_factor_and_expectancy(self):
        st = rp.stats(self.rows([2.0, -1.0, 2.0, -1.0]), 10.0)
        self.assertAlmostEqual(st["profit_factor"], 2.0)
        self.assertAlmostEqual(st["expectancy"], 0.5)
        self.assertAlmostEqual(st["win_rate"], 50.0)

    def test_consecutive_losses(self):
        st = rp.stats(self.rows([-1, -1, -1, 2, -1, -1]), 10.0)
        self.assertEqual(st["max_consec_losses"], 3)
        self.assertEqual(st["max_consec_wins"], 1)

    def test_max_drawdown_is_measured_from_the_running_peak(self):
        st = rp.stats(self.rows([5.0, -2.0, -1.0]), 10.0)
        self.assertAlmostEqual(st["max_drawdown"], 3.0)

    def test_an_empty_trade_list_is_safe(self):
        st = rp.stats([], 10.0)
        self.assertEqual(st["trades"], 0)
        self.assertEqual(st["net_pnl"], 0.0)
        self.assertIsNone(st["profit_factor"])

    def test_sharpe_is_withheld_on_a_small_sample(self):
        self.assertIsNone(rp.stats(self.rows([1.0, -1.0] * 5), 10.0)["sharpe"])

    def test_sharpe_is_reported_once_the_sample_is_large_enough(self):
        self.assertIsNotNone(rp.stats(self.rows([1.0, -1.0] * 40), 10.0)["sharpe"])

    def test_a_zero_loss_book_has_no_profit_factor_rather_than_a_crash(self):
        self.assertIsNone(rp.stats(self.rows([1.0, 2.0]), 10.0)["profit_factor"])


class TestWindows(unittest.TestCase):

    def test_folds_tile_the_window_without_gaps(self):
        f = rp.folds(0, 1000, 4)
        self.assertEqual(f[0][0], 0)
        self.assertEqual(f[-1][1], 1000)
        for a, b in zip(f, f[1:]):
            self.assertEqual(a[1], b[0])

    def test_the_split_holds_back_the_later_part(self):
        (a0, a1), (b0, b1) = rp.splits(0, 1000, 0.3)
        self.assertEqual(a1, b0)
        self.assertAlmostEqual(b1 - b0, 300, delta=1)


# ================================================================ safety

class TestResearchCannotTrade(unittest.TestCase):
    """The research path must be structurally incapable of placing an order."""

    def test_the_public_client_refuses_a_signed_request(self):
        c = labdata.public_client(cfg(use_testnet=True, api_key="", api_secret=""))
        with self.assertRaises(tb.ReadOnlyClient):
            c._req("GET", "/fapi/v2/balance", signed=True)

    def test_the_public_client_refuses_any_non_get(self):
        c = labdata.public_client(cfg(use_testnet=True, api_key="", api_secret=""))
        with self.assertRaises(tb.ReadOnlyClient):
            c._req("POST", "/fapi/v1/order", {"symbol": "ETHUSDT"})

    def test_the_public_client_holds_no_credentials(self):
        c = labdata.public_client(cfg(use_testnet=True, api_key="KEY",
                                      api_secret="SECRET"))
        self.assertEqual(c.key, "")
        self.assertEqual(c.secret, b"")

    def test_research_reads_the_live_host_not_the_demo_one(self):
        """The demo host answers with synthetic candles instead of failing, so
        a research client must pin the base rather than inherit the default."""
        c = labdata.public_client(cfg(use_testnet=True))
        self.assertEqual(c.base, tb.LIVE_BASE)
        self.assertNotIn("demo", c.base)

    def test_the_engine_also_pins_the_live_host_for_paper_and_research(self):
        """Guards the line in main() that makes paper/research read real data."""
        src = open(os.path.join(tb.HERE, "trading_bot.py"), encoding="utf-8").read()
        self.assertIn("client = Binance(cfg, base=LIVE_BASE, public_only=True)", src)

    def test_the_bare_constructor_still_follows_use_testnet(self):
        """Documents the trap: this default is why public_client() exists."""
        base = dict(api_key="", api_secret="")
        self.assertEqual(tb.Binance(cfg(use_testnet=True, **base)).base,
                         tb.TESTNET_BASE)
        self.assertEqual(tb.Binance(cfg(use_testnet=False, **base)).base,
                         tb.LIVE_BASE)

    def test_the_testnet_flag_still_does_not_leak_into_research(self):
        for flag in (True, False):
            self.assertEqual(labdata.public_client(cfg(use_testnet=flag)).base,
                             tb.LIVE_BASE)


class TestLiveStateIsUntouched(unittest.TestCase):
    """Importing and running research must not disturb the running engine."""

    PATHS = [os.path.join(tb.HERE, "bot_state_paper.json"),
             os.path.join(tb.HERE, "config.json"),
             os.path.join(tb.HERE, "trades.csv")]

    def test_research_modules_do_not_write_engine_state(self):
        before = {p: (os.path.exists(p),
                      os.path.getmtime(p) if os.path.exists(p) else None)
                  for p in self.PATHS}
        p = lab.params()
        S = replay_series(bars([(100, 101, 99, 100)] * 60), p)
        rp.replay(cfg(), p, {"X": S}, {"X": RULES},
                  S.e[0]["T"], S.e[-1]["T"] + 1)
        after = {q: (os.path.exists(q),
                     os.path.getmtime(q) if os.path.exists(q) else None)
                 for q in self.PATHS}
        self.assertEqual(before, after)

    def test_the_account_is_flat_and_the_eth_trade_is_in_the_journal(self):
        """The legacy ETH short is closed, and its record is permanent.

        This replaces an earlier guard that asserted the ETH position was
        still OPEN in the state file. A manual /halt closed it on
        2026-11-20, which is the intended behaviour of a halt, so the old
        assertion described a state the bot is no longer supposed to be in.
        The invariant that actually matters now is the pair below: the
        account is flat, and the closed trade was not lost in the process.
        """
        path = os.path.join(tb.HERE, "bot_state_paper.json")
        if not os.path.exists(path):
            self.skipTest("no live paper state on this machine")
        with open(path, encoding="utf-8") as f:
            s = json.load(f)
        self.assertIsNone(s.get("position"),
                          "the account must stay flat for this audit")
        rows = tb.load_journal(os.path.join(tb.HERE, "trades.csv"))
        eth = [r for r in rows if r.get("symbol") == "ETHUSDT"]
        self.assertTrue(eth, "the closed ETH trade has gone missing from the journal")
        self.assertEqual(eth[0]["side"], "SHORT")
        self.assertEqual(eth[0]["exit_reason"], "emergency halt")

    def test_the_engine_is_still_halted(self):
        path = os.path.join(tb.HERE, "bot_state_paper.json")
        if not os.path.exists(path):
            self.skipTest("no live paper state on this machine")
        with open(path, encoding="utf-8") as f:
            s = json.load(f)
        self.assertTrue(s.get("halted"), "the emergency halt must stay ACTIVE")
        self.assertTrue(s.get("paused"), "the engine must stay paused")

    def test_paper_entries_are_still_switched_off(self):
        path = os.path.join(tb.HERE, "bot_state_paper.json")
        if not os.path.exists(path):
            self.skipTest("no live paper state on this machine")
        with open(path, encoding="utf-8") as f:
            s = json.load(f)
        self.assertFalse(s.get("paper_enabled"),
                         "paper entries must stay OFF for this task")

    def test_the_config_is_still_in_paper_mode_with_live_trading_refused(self):
        c = tb.load_config(os.path.join(tb.HERE, "config.json"))
        self.assertEqual(c["mode"], "paper")
        self.assertFalse(c["i_understand_real_money"])
        self.assertFalse(c["paper_trading_enabled"])

    def test_the_approved_risk_limits_are_unchanged(self):
        c = tb.load_config(os.path.join(tb.HERE, "config.json"))
        self.assertEqual(c["risk_per_trade_pct"], 0.5)
        self.assertEqual(c["max_daily_loss_pct"], 2.0)
        self.assertEqual(c["max_account_drawdown_pct"], 5.0)
        self.assertEqual(c["monthly_drawdown_stop_pct"], 6.0)
        self.assertEqual(c["max_open_positions"], 1)
        self.assertEqual(c["max_trades_per_day"], 3)
        self.assertEqual(c["loss_cooldown_min"], 60)
        self.assertEqual(c["taker_fee_pct"], 0.05)
        self.assertEqual(c["slippage_pct"], 0.03)


class TestDiagnosticConfigIsNeverTheRealOne(unittest.TestCase):

    def test_lifting_the_halts_does_not_mutate_the_caller(self):
        import labreport
        real = cfg()
        d = labreport.diagnostic_cfg(real)
        self.assertEqual(real["max_account_drawdown_pct"], 5.0)
        self.assertGreater(d["max_account_drawdown_pct"], 1e6)

    def test_the_candidate_never_raises_risk_per_trade(self):
        import labreport
        d = labreport.diagnostic_cfg(cfg())
        self.assertEqual(d["risk_per_trade_pct"], 0.5)
        self.assertEqual(d["max_open_positions"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
