#!/usr/bin/env python3
"""Loss-protection tests added by the risk audit.

Run with:   python -m unittest -v test_loss_protection

These cover the controls the audit either added or found to be unverified.
Everything is offline: a fake exchange, a fake Telegram and a throwaway state
file. Many of these tests assert that NO trade happens, which is the point -
a risk control that merely warns is not a risk control.

Covers, in order:
  1. the 1.00 USDT absolute per-trade ceiling, and its interaction with the
     0.5% equity target (whichever is stricter must win)
  2. fees, slippage and the entry-fill correction
  3. quantity rounding and exchange minimums
  4. invalid, missing and non-finite inputs (the fail-closed rules)
  5. stop validity and the refusal to widen a stop
  6. account-level limits: daily, drawdown, monthly, one position, cooldown
  7. the emergency halt
  8. regression guards for the live paper account
"""

import json
import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import trading_bot as tb  # noqa: E402
from test_trading_bot import (HOUR, RULES, FakeClient, FakeNotifier,  # noqa: E402
                              TempProject, candles, cfg, signal_series_long)

ETH = RULES["ETHUSDT"]


def budget_of(equity, pct=0.5, abs_cap=1.00):
    """What the engine should allow: the stricter of the two caps."""
    return min(equity * pct / 100.0, abs_cap)


def real_loss(entry, stop, qty, fee_pct, slip_pct):
    """The planned loss of being stopped out, computed independently here.

    Deliberately not a call to cost_per_unit: a test that reuses the function
    under test cannot catch an error in it.
    """
    return (qty * abs(entry - stop)
            + qty * (entry + stop) * fee_pct / 100.0
            + qty * (entry + stop) * slip_pct / 100.0)


# ============================================ 1. the absolute per-trade cap

class TestAbsoluteRiskCeiling(TempProject):
    """A maximum planned loss of 1.00 USDT per position, inclusive of
    everything: stop distance, both fees and slippage on both sides."""

    def test_the_ceiling_is_one_usdt_by_default(self):
        self.assertEqual(tb.DEFAULTS["max_risk_usdt_abs"], 1.00)

    def test_the_shipped_config_carries_the_ceiling(self):
        c = tb.load_config(os.path.join(os.path.dirname(
            os.path.abspath(__file__)), "config.json"))
        self.assertEqual(c["max_risk_usdt_abs"], 1.00)

    def test_the_absolute_ceiling_overrides_a_looser_percentage(self):
        """Above 200 USDT of equity, 0.5% is looser than 1.00 USDT."""
        conf = cfg(risk_per_trade_pct=0.5, max_risk_usdt_abs=1.00)
        for equity in (201.0, 500.0, 1000.0, 10_000.0, 1_000_000.0):
            with self.subTest(equity=equity):
                b, which, _ = tb.risk_budget(equity, conf)
                self.assertEqual(which, "abs")
                self.assertAlmostEqual(b, 1.00, places=9)

    def test_the_percentage_stays_in_charge_on_a_small_account(self):
        """At 22 USDT the normal target is ~0.11 USDT and must still apply."""
        conf = cfg(risk_per_trade_pct=0.5, max_risk_usdt_abs=1.00)
        b, which, _ = tb.risk_budget(22.0, conf)
        self.assertEqual(which, "pct")
        self.assertAlmostEqual(b, 0.11, places=9)
        self.assertLess(b, 1.00)

    def test_the_stricter_cap_always_wins_across_the_whole_range(self):
        conf = cfg(risk_per_trade_pct=0.5, max_risk_usdt_abs=1.00)
        for equity in [5.0, 22.0, 50.0, 199.0, 200.0, 201.0, 400.0, 5000.0]:
            with self.subTest(equity=equity):
                b, _, _ = tb.risk_budget(equity, conf)
                self.assertAlmostEqual(b, budget_of(equity), places=9)

    def test_no_sized_position_can_ever_plan_to_lose_more_than_one_usdt(self):
        """The headline guarantee, swept over equity, price and stop width."""
        conf = cfg(risk_per_trade_pct=0.5, taker_fee_pct=0.05,
                   slippage_pct=0.03, max_risk_usdt_abs=1.00)
        for equity in (22.0, 100.0, 220.0, 1000.0, 25_000.0):
            for entry in (0.35, 150.0, 3000.0, 95_000.0):
                for stop_pct in (0.4, 0.9, 1.7, 3.0):
                    for side in (1, -1):
                        stop = entry * (1 - side * stop_pct / 100)
                        qty, risk, _ = tb.size_position(
                            equity, entry, stop, ETH, conf, fractional=True)
                        if qty <= 0:
                            continue
                        with self.subTest(equity=equity, entry=entry,
                                          stop_pct=stop_pct, side=side):
                            loss = real_loss(entry, stop, qty, 0.05, 0.03)
                            self.assertLessEqual(loss, 1.00 + 1e-9)
                            self.assertLessEqual(
                                loss, budget_of(equity) + 1e-9)
                            self.assertAlmostEqual(risk, loss, places=9)

    def test_the_risk_manager_reports_the_same_cap_the_sizer_uses(self):
        """One source of truth: a gate and the sizer cannot disagree."""
        conf = cfg(risk_per_trade_pct=0.5, max_risk_usdt_abs=1.00)
        r = tb.RiskManager(conf, {})
        for equity in (22.0, 150.0, 300.0, 2000.0):
            with self.subTest(equity=equity):
                self.assertAlmostEqual(r.max_risk_usdt(equity),
                                       tb.risk_budget(equity, conf)[0],
                                       places=12)
                self.assertAlmostEqual(r.max_risk_usdt(equity),
                                       budget_of(equity), places=9)

    def test_a_config_may_not_raise_the_ceiling_past_the_hard_cap(self):
        bad = cfg(max_risk_usdt_abs=tb.RISK_USDT_HARD_CAP + 0.01)
        with self.assertRaises(SystemExit):
            tb.validate_config(bad)

    def test_a_ceiling_of_zero_or_below_is_refused(self):
        for v in (0, -1, -0.5):
            with self.subTest(v=v):
                with self.assertRaises(SystemExit):
                    tb.validate_config(cfg(max_risk_usdt_abs=v))

    def test_even_an_accepted_ceiling_is_clamped_by_the_hard_cap(self):
        """The hard cap binds even if validation is bypassed entirely."""
        conf = cfg(risk_per_trade_pct=2.0, max_risk_usdt_abs=10_000.0)
        b, _, _ = tb.risk_budget(1_000_000.0, conf)
        self.assertLessEqual(b, tb.RISK_USDT_HARD_CAP + 1e-9)

    def test_the_risk_report_names_both_caps_and_which_one_binds(self):
        conf = cfg(risk_per_trade_pct=0.5, max_risk_usdt_abs=1.00)
        r = tb.RiskManager(conf, {"day_start_equity": 1000.0,
                                  "equity_peak": 1000.0})
        text = r.limits_text(1000.0)
        self.assertIn("1.00 USDT per trade", text)
        self.assertIn("absolute ceiling", text)
        small = tb.RiskManager(conf, {"day_start_equity": 22.0,
                                      "equity_peak": 22.0}).limits_text(22.0)
        self.assertIn("percentage target", small)


# ===================================== 2. fees, slippage and the entry fill

class TestFeesSlippageAndTheFill(TempProject):
    def test_the_cap_includes_fees_and_slippage_not_just_the_stop_move(self):
        """Price move alone must be strictly less than the whole budget."""
        conf = cfg(risk_per_trade_pct=0.5, taker_fee_pct=0.05,
                   slippage_pct=0.03, max_risk_usdt_abs=1.00)
        qty, risk, _ = tb.size_position(1000.0, 3000.0, 2970.0, ETH, conf,
                                        fractional=True)
        self.assertGreater(qty, 0)
        move_only = qty * 30.0
        self.assertLess(move_only, risk)
        self.assertAlmostEqual(risk, 1.00, places=2)

    def test_zero_fees_and_slippage_allow_a_bigger_position(self):
        free = cfg(taker_fee_pct=0.0, slippage_pct=0.0, max_risk_usdt_abs=1.00)
        costly = cfg(taker_fee_pct=0.05, slippage_pct=0.03,
                     max_risk_usdt_abs=1.00)
        q_free, _, _ = tb.size_position(1000.0, 3000.0, 2970.0, ETH, free,
                                        fractional=True)
        q_cost, _, _ = tb.size_position(1000.0, 3000.0, 2970.0, ETH, costly,
                                        fractional=True)
        self.assertGreater(q_free, q_cost)

    def test_sizing_uses_the_fill_price_so_the_real_stop_distance_is_used(self):
        """The audit's correction: entry slippage widens the stop distance.

        Sizing on the pre-slippage quote understates the distance the price
        has to travel to the stop, and therefore the loss. The engine now
        sizes on the projected fill, so the reported risk matches the loss
        measured from the fill the broker actually gives.
        """
        conf = cfg(risk_per_trade_pct=0.5, taker_fee_pct=0.05,
                   slippage_pct=0.03, max_risk_usdt_abs=1.00)
        quote, stop, side = 3000.0, 2988.0, "LONG"     # 0.4%, the tightest
        fill = tb.adverse_fill(side, quote, 0.03, entering=True)
        self.assertGreater(fill, quote)                 # a long pays up
        qty, risk, _ = tb.size_position(1000.0, fill, stop, ETH, conf,
                                        fractional=True)
        self.assertGreater(qty, 0)
        # measured from the fill, the loss is inside the cap
        self.assertLessEqual(real_loss(fill, stop, qty, 0.05, 0.03), 1.00 + 1e-9)
        # and sizing off the quote instead would have exceeded it
        q_naive, _, _ = tb.size_position(1000.0, quote, stop, ETH, conf,
                                         fractional=True)
        self.assertGreater(q_naive, qty)
        self.assertGreater(real_loss(fill, stop, q_naive, 0.05, 0.03), 1.00)

    def test_the_open_position_plans_no_more_than_the_cap_end_to_end(self):
        """Drive the real entry path and price the position that comes out."""
        conf = cfg(risk_per_trade_pct=0.5, max_risk_usdt_abs=1.00,
                   symbols=["ETHUSDT"])
        c1, c4 = signal_series_long()
        client = FakeClient(c1=c1, c4=c4, price=c1[-1]["c"])
        bot, _broker, _n = self.make_bot(
            client, state={"paper_balance": 1000.0}, conf=conf)
        bot.look_for_entry()
        p = bot.s.get("position")
        self.assertIsNotNone(p, "expected the fixture to produce an entry")
        loss = real_loss(p["entry"], p["stop"], p["qty"],
                         conf["taker_fee_pct"], conf["slippage_pct"])
        self.assertLessEqual(loss, 1.00 + 1e-9)
        self.assertLessEqual(p["planned_risk"], 1.00 + 1e-9)
        self.assertAlmostEqual(p["planned_risk"], loss, places=9)


# ================================= 3. rounding and exchange-minimum handling

class TestRoundingAndMinimums(TempProject):
    def test_rounding_can_only_reduce_the_risk(self):
        conf = cfg(risk_per_trade_pct=0.5, max_risk_usdt_abs=1.00)
        for step in (1e-6, 0.001, 0.01, 0.1, 1.0):
            rules = dict(ETH, step=step, min_qty=0.0, min_notional=0.0)
            qty, risk, note = tb.size_position(1000.0, 3000.0, 2970.0, rules,
                                               conf)
            if qty <= 0:
                continue
            with self.subTest(step=step):
                self.assertEqual(tb.floor_step(qty, step), qty)
                self.assertLessEqual(risk, 1.00 + 1e-9)

    def test_a_coarse_step_never_rounds_up_through_the_ceiling(self):
        """A step so coarse that one unit alone would breach the cap."""
        conf = cfg(risk_per_trade_pct=0.5, max_risk_usdt_abs=1.00)
        rules = {"step": 1.0, "min_qty": 1.0, "min_notional": 0.0, "tick": 0.01}
        qty, risk, note = tb.size_position(1000.0, 3000.0, 2970.0, rules, conf)
        self.assertEqual((qty, risk), (0.0, 0.0), note)

    def test_an_exchange_minimum_is_never_met_by_raising_the_risk(self):
        conf = cfg(risk_per_trade_pct=0.5, max_risk_usdt_abs=1.00)
        rules = {"step": 0.001, "min_qty": 0.001, "min_notional": 500.0,
                 "tick": 0.01}
        qty, risk, note = tb.size_position(1000.0, 3000.0, 2970.0, rules, conf)
        self.assertEqual((qty, risk), (0.0, 0.0), note)
        self.assertIn("never raised", note)

    def test_a_minimum_notional_cannot_push_risk_past_the_absolute_cap(self):
        """Sweep minimums: either rejected, or inside the 1.00 USDT cap."""
        conf = cfg(risk_per_trade_pct=0.5, max_risk_usdt_abs=1.00)
        for min_notional in (5.0, 20.0, 100.0, 200.0, 1000.0):
            for equity in (220.0, 1000.0, 5000.0):
                rules = {"step": 0.001, "min_qty": 0.001,
                         "min_notional": min_notional, "tick": 0.01}
                qty, risk, _ = tb.size_position(equity, 3000.0, 2970.0, rules,
                                                conf)
                with self.subTest(min_notional=min_notional, equity=equity):
                    if qty > 0:
                        self.assertGreaterEqual(qty * 3000.0, min_notional)
                        self.assertLessEqual(risk, 1.00 + 1e-9)

    def test_the_leverage_cap_only_ever_reduces_the_quantity(self):
        conf = cfg(risk_per_trade_pct=0.5, max_leverage=5,
                   max_risk_usdt_abs=1.00)
        qty, risk, _ = tb.size_position(1000.0, 3000.0, 2999.0, ETH, conf,
                                        fractional=True)
        if qty > 0:
            self.assertLessEqual(qty * 3000.0, 1000.0 * 5 * 0.9 + 1e-9)
            self.assertLessEqual(risk, 1.00 + 1e-9)

    def test_the_note_says_when_the_absolute_cap_set_the_size(self):
        conf = cfg(risk_per_trade_pct=0.5, max_risk_usdt_abs=1.00)
        _q, _r, note = tb.size_position(5000.0, 3000.0, 2970.0, ETH, conf,
                                        fractional=True)
        self.assertIn("absolute risk cap", note)


# ======================================= 4. invalid input and failing closed

class TestFailsClosedOnBadInput(TempProject):
    def test_a_non_finite_price_stop_or_equity_is_refused(self):
        """NaN compares False against every threshold, so it must be caught.

        Before the audit this returned (nan, nan) and every downstream check
        waved it through, producing a NaN-quantity position.
        """
        conf = cfg(max_risk_usdt_abs=1.00)
        nan, inf = float("nan"), float("inf")
        cases = [(nan, 2970.0, 22.0), (3000.0, nan, 22.0), (3000.0, 2970.0, nan),
                 (inf, 2970.0, 22.0), (3000.0, inf, 22.0), (3000.0, 2970.0, inf),
                 (-inf, 2970.0, 22.0), (3000.0, 2970.0, -inf)]
        for entry, stop, equity in cases:
            with self.subTest(entry=entry, stop=stop, equity=equity):
                qty, risk, note = tb.size_position(equity, entry, stop, ETH,
                                                   conf, fractional=True)
                self.assertEqual((qty, risk), (0.0, 0.0), note)
                self.assertTrue(math.isfinite(qty) and math.isfinite(risk))

    def test_is_finite_rejects_everything_that_is_not_a_real_number(self):
        self.assertTrue(tb.is_finite(0.0, 1, -3.5))
        for bad in (float("nan"), float("inf"), float("-inf"), None, "3000",
                    True, [], {}):
            with self.subTest(bad=bad):
                self.assertFalse(tb.is_finite(bad))

    def test_a_missing_absolute_cap_rejects_the_trade(self):
        """Fail closed: absent must not mean 'no absolute cap'."""
        conf = cfg(max_risk_usdt_abs=1.00)
        del conf["max_risk_usdt_abs"]
        qty, risk, note = tb.size_position(1000.0, 3000.0, 2970.0, ETH, conf,
                                           fractional=True)
        self.assertEqual((qty, risk), (0.0, 0.0), note)
        self.assertIn("max_risk_usdt_abs", note)

    def test_an_unusable_absolute_cap_rejects_the_trade(self):
        for bad in (None, "1.00", float("nan"), float("inf"), 0, -1):
            conf = cfg(max_risk_usdt_abs=bad)
            with self.subTest(bad=bad):
                qty, risk, note = tb.size_position(1000.0, 3000.0, 2970.0, ETH,
                                                   conf, fractional=True)
                self.assertEqual((qty, risk), (0.0, 0.0), note)

    def test_an_unusable_fee_or_slippage_setting_rejects_the_trade(self):
        for key in ("taker_fee_pct", "slippage_pct"):
            for bad in (None, "0.05", float("nan"), -1):
                conf = cfg(max_risk_usdt_abs=1.00, **{key: bad})
                with self.subTest(key=key, bad=bad):
                    qty, risk, note = tb.size_position(1000.0, 3000.0, 2970.0,
                                                       ETH, conf,
                                                       fractional=True)
                    self.assertEqual((qty, risk), (0.0, 0.0), note)

    def test_an_unusable_risk_percentage_rejects_the_trade(self):
        for bad in (None, "0.5", float("nan"), 0, -0.5):
            conf = cfg(max_risk_usdt_abs=1.00, risk_per_trade_pct=bad)
            with self.subTest(bad=bad):
                b, which, _ = tb.risk_budget(1000.0, conf)
                self.assertEqual((b, which), (0.0, "invalid"))

    def test_an_invalid_or_inverted_stop_is_refused(self):
        conf = cfg(max_risk_usdt_abs=1.00)
        for entry, stop in [(3000.0, 3000.0), (3000.0, 0.0), (0.0, 2970.0),
                            (-5.0, -6.0), (3000.0, -10.0)]:
            with self.subTest(entry=entry, stop=stop):
                qty, risk, note = tb.size_position(22.0, entry, stop, ETH,
                                                   conf)
                self.assertEqual((qty, risk), (0.0, 0.0), note)

    def test_a_zero_budget_blocks_entries_rather_than_permitting_them(self):
        conf = cfg(max_risk_usdt_abs=1.00)
        r = tb.RiskManager(conf, {"day_start_equity": 22.0, "equity_peak": 22.0})
        blocks = r.entry_blocks(float("nan"))
        self.assertTrue(blocks)
        self.assertTrue(any("finite" in b for b in blocks))

    def test_a_sizing_exception_is_a_rejection_not_a_pass(self):
        conf = cfg(max_risk_usdt_abs=1.00, symbols=["ETHUSDT"])
        c1, c4 = signal_series_long()
        client = FakeClient(c1=c1, c4=c4, price=c1[-1]["c"])
        bot, _broker, _n = self.make_bot(
            client, state={"paper_balance": 1000.0}, conf=conf)
        real = tb.size_position
        try:
            tb.size_position = lambda *a, **k: (_ for _ in ()).throw(
                RuntimeError("boom"))
            bot.look_for_entry()
        finally:
            tb.size_position = real
        self.assertIsNone(bot.s.get("position"),
                          "a sizing crash must not open a position")

    def test_stale_candles_block_an_entry(self):
        conf = cfg(max_risk_usdt_abs=1.00, symbols=["ETHUSDT"])
        old = candles(140, tb.now_ms() - 400 * HOUR, HOUR, 3000.0)
        client = FakeClient(c1=old, c4=candles(260, 0, 4 * HOUR, 3000.0),
                            price=3000.0)
        bot, _broker, _n = self.make_bot(
            client, state={"paper_balance": 1000.0}, conf=conf)
        self.assertIsNotNone(bot.data_problem("ETHUSDT"))
        bot.look_for_entry()
        self.assertIsNone(bot.s.get("position"))

    def test_missing_market_data_blocks_an_entry(self):
        conf = cfg(max_risk_usdt_abs=1.00, symbols=["ETHUSDT"])
        client = FakeClient(c1=[], c4=[], price=3000.0)
        bot, _broker, _n = self.make_bot(
            client, state={"paper_balance": 1000.0}, conf=conf)
        bot.look_for_entry()
        self.assertIsNone(bot.s.get("position"))


# ================================================ 5. stop-loss enforcement

class TestStopEnforcement(TempProject):
    def test_every_opened_position_carries_a_stop_and_an_initial_stop(self):
        conf = cfg(max_risk_usdt_abs=1.00, symbols=["ETHUSDT"])
        c1, c4 = signal_series_long()
        client = FakeClient(c1=c1, c4=c4, price=c1[-1]["c"])
        bot, _broker, _n = self.make_bot(
            client, state={"paper_balance": 1000.0}, conf=conf)
        bot.look_for_entry()
        p = bot.s.get("position")
        self.assertIsNotNone(p)
        for key in ("stop", "initial_stop", "target"):
            self.assertIn(key, p)
            self.assertTrue(tb.is_finite(p[key]))
        self.assertLess(p["stop"], p["entry"])          # a LONG stop sits below

    def test_the_broker_refuses_to_open_a_position_past_its_stop(self):
        conf = cfg(max_risk_usdt_abs=1.00)
        broker = tb.PaperBroker(FakeClient(), conf, {"paper_balance": 22.0})
        with self.assertRaises(ValueError):
            broker.open("ETHUSDT", "LONG", 0.01, 3010.0, 3000.0, ETH)
        with self.assertRaises(ValueError):
            broker.open("ETHUSDT", "SHORT", 0.01, 2990.0, 3000.0, ETH)

    def test_a_stop_cannot_be_widened_in_either_direction(self):
        for side, entry, stop, wider, tighter in [
                ("LONG", 3000.0, 2970.0, 2950.0, 2985.0),
                ("SHORT", 3000.0, 3030.0, 3050.0, 3015.0)]:
            with self.subTest(side=side):
                self.assertTrue(tb.stop_is_widened(side, entry, stop, wider))
                self.assertFalse(tb.stop_is_widened(side, entry, stop, tighter))
                p = {"side": side, "entry": entry, "stop": stop}
                self.assertFalse(tb.tighten_stop(p, wider))
                self.assertEqual(p["stop"], stop, "the stop was widened")
                self.assertTrue(tb.tighten_stop(p, tighter))
                self.assertEqual(p["stop"], tighter)

    def test_widening_a_stop_is_refused_not_raised(self):
        """A refusal must not be an exception a caller could swallow."""
        p = {"side": "LONG", "entry": 3000.0, "stop": 2970.0}
        self.assertFalse(tb.tighten_stop(p, 2000.0))
        self.assertEqual(p["stop"], 2970.0)

    def test_a_gap_through_the_stop_fills_at_the_open_not_the_stop(self):
        self.assertEqual(tb.stop_fill_price("LONG", 2970.0, 2900.0), 2900.0)
        self.assertEqual(tb.stop_fill_price("SHORT", 3030.0, 3100.0), 3100.0)
        # and a candle that opened on the right side still fills at the stop
        self.assertEqual(tb.stop_fill_price("LONG", 2970.0, 2990.0), 2970.0)
        self.assertEqual(tb.stop_fill_price("SHORT", 3030.0, 3010.0), 3030.0)

    def test_a_gap_can_lose_more_than_the_planned_risk_and_is_not_hidden(self):
        """The simulator must book the real gap loss, not the capped one."""
        conf = cfg(taker_fee_pct=0.05, slippage_pct=0.03,
                   max_risk_usdt_abs=1.00)
        state = {"paper_balance": 1000.0}
        broker = tb.PaperBroker(FakeClient(), conf, state)
        p = broker.open("ETHUSDT", "LONG", 0.5, 2970.0, 3000.0, ETH,
                        meta={"planned_risk": 1.00})
        res = broker.book(p, tb.stop_fill_price("LONG", 2970.0, 2800.0),
                          "stop loss")
        self.assertLess(res["pnl"], -1.00,
                        "a gap loss must exceed the planned risk, not be clipped")

    def test_the_risk_report_does_not_claim_a_stop_guarantees_the_loss(self):
        conf = cfg(max_risk_usdt_abs=1.00)
        r = tb.RiskManager(conf, {"day_start_equity": 22.0, "equity_peak": 22.0})
        text = r.limits_text(22.0)
        self.assertIn("not the worst case", text)
        self.assertIn("gap", text)

    def test_stop_width_rules_are_direction_consistent(self):
        conf = cfg(min_stop_pct=0.4, max_stop_pct=3.0, max_risk_usdt_abs=1.00)
        self.assertIsNone(tb.check_stop("LONG", 3000.0, 2970.0, conf))
        self.assertIsNone(tb.check_stop("SHORT", 3000.0, 3030.0, conf))
        # a stop on the wrong side of the entry
        self.assertIsNotNone(tb.check_stop("LONG", 3000.0, 3030.0, conf))
        self.assertIsNotNone(tb.check_stop("SHORT", 3000.0, 2970.0, conf))
        # too tight and too wide
        self.assertIsNotNone(tb.check_stop("LONG", 3000.0, 2997.0, conf))
        self.assertIsNotNone(tb.check_stop("LONG", 3000.0, 2700.0, conf))


# ============================================== 6. account-level risk limits

class TestAccountLevelLimits(TempProject):
    def conf(self, **over):
        return cfg(max_risk_usdt_abs=1.00, **over)

    def test_the_approved_limits_are_the_shipped_defaults(self):
        d = tb.DEFAULTS
        self.assertEqual(d["max_daily_loss_pct"], 2.0)
        self.assertEqual(d["max_account_drawdown_pct"], 5.0)
        self.assertEqual(d["monthly_drawdown_stop_pct"], 6.0)
        self.assertEqual(d["max_open_positions"], 1)
        self.assertEqual(d["max_trades_per_day"], 3)
        self.assertEqual(d["max_losses_per_day"], 2)
        self.assertEqual(d["loss_cooldown_min"], 60)
        self.assertEqual(d["risk_per_trade_pct"], 0.5)
        self.assertEqual(d["max_risk_usdt_abs"], 1.00)

    def test_the_daily_loss_limit_breaches_at_two_percent(self):
        r = tb.RiskManager(self.conf(), {"day_start_equity": 100.0,
                                         "equity_peak": 100.0})
        self.assertIsNone(r.breach(98.5))
        kind, msg = r.breach(98.0)
        self.assertEqual(kind, "daily")
        self.assertIn("2.0% daily limit", msg)

    def test_the_drawdown_limit_breaches_at_five_percent(self):
        r = tb.RiskManager(self.conf(), {"day_start_equity": 100.0,
                                         "equity_peak": 100.0})
        self.assertEqual(r.breach(95.0)[0], "drawdown")
        # drawdown is reported ahead of the daily loss: it is the harder stop
        self.assertEqual(r.breach(90.0)[0], "drawdown")

    def test_the_monthly_limit_blocks_entries_at_six_percent(self):
        r = tb.RiskManager(self.conf(), {"month_start": 100.0,
                                         "day_start_equity": 94.0,
                                         "equity_peak": 94.0})
        self.assertTrue(any("this month" in b for b in r.entry_blocks(94.0)))
        self.assertFalse(any("this month" in b for b in r.entry_blocks(94.5)))

    def test_only_one_position_is_allowed_across_all_symbols(self):
        conf = self.conf(symbols=["ETHUSDT", "SOLUSDT"])
        c1, c4 = signal_series_long()
        client = FakeClient(c1=c1, c4=c4, price=c1[-1]["c"])
        bot, _broker, _n = self.make_bot(
            client, state={"paper_balance": 1000.0}, conf=conf)
        bot.s["position"] = {"symbol": "SOLUSDT", "side": "LONG", "qty": 0.1,
                             "entry": 150.0, "stop": 148.0,
                             "initial_stop": 148.0, "target": 154.0,
                             "opened": tb.now_ms(), "checked": tb.now_ms(),
                             "trade_id": "held", "planned_risk": 0.11,
                             "strategy": tb.STRATEGY_NAME,
                             "market_condition": "", "entry_slippage": 0.0,
                             "equity_before": 1000.0}
        blocks = bot.entry_blocks()
        self.assertTrue(any("already in a" in b for b in blocks))
        bot.look_for_entry()
        self.assertEqual(bot.s["position"]["trade_id"], "held",
                         "a second symbol opened while one was already held")

    def test_three_entries_a_day_is_the_limit(self):
        r = tb.RiskManager(self.conf(), {"day_start_equity": 1000.0,
                                         "equity_peak": 1000.0,
                                         "trades_today": 3})
        self.assertTrue(any("trades already taken today" in b
                            for b in r.entry_blocks(1000.0)))
        self.assertEqual(r.trades_left_today(), 0)

    def test_the_loss_cooldown_blocks_a_revenge_trade(self):
        now = tb.now_ms()
        r = tb.RiskManager(self.conf(loss_cooldown_min=60),
                           {"day_start_equity": 1000.0, "equity_peak": 1000.0,
                            "last_loss_ms": now - 10 * 60_000})
        self.assertTrue(any("cooling off" in b
                            for b in r.entry_blocks(1000.0, ms=now)))
        self.assertGreater(r.cooldown_left_sec(now), 0)
        later = now + 61 * 60_000
        self.assertEqual(r.cooldown_left_sec(later), 0)

    def test_two_losses_end_the_trading_day(self):
        r = tb.RiskManager(self.conf(), {"day_start_equity": 1000.0,
                                         "equity_peak": 1000.0,
                                         "losses_today": 2})
        self.assertTrue(any("done for the day" in b
                            for b in r.entry_blocks(1000.0)))

    def test_a_locked_day_blocks_entries_until_the_next_day(self):
        r = tb.RiskManager(self.conf(), {"day_start_equity": 1000.0,
                                         "equity_peak": 1000.0})
        r.lock_day()
        self.assertTrue(r.is_locked_today())
        self.assertTrue(any("locked until the next trading day" in b
                            for b in r.entry_blocks(1000.0)))

    def test_a_restart_does_not_hand_back_a_fresh_daily_budget(self):
        s = {"day": tb.day_key(), "day_start_equity": 100.0,
             "equity_peak": 100.0, "trades_today": 2, "losses_today": 1,
             "realized_today": -1.5}
        r = tb.RiskManager(self.conf(), s)
        r.roll(98.0)
        self.assertEqual(s["day_start_equity"], 100.0)
        self.assertEqual(s["trades_today"], 2)
        self.assertEqual(s["losses_today"], 1)

    def test_the_equity_peak_never_moves_down(self):
        s = {"equity_peak": 100.0}
        r = tb.RiskManager(self.conf(), s)
        r.observe(80.0)
        self.assertEqual(s["equity_peak"], 100.0)
        r.observe(120.0)
        self.assertEqual(s["equity_peak"], 120.0)

    def test_a_trade_that_does_not_fit_the_day_budget_is_blocked(self):
        """2% of the day's start, less what is already lost."""
        r = tb.RiskManager(self.conf(), {"day_start_equity": 1000.0,
                                         "equity_peak": 1000.0})
        # 2% of 1000 = 20 USDT; after losing 19.5 only 0.5 is left, and the
        # 1.00 USDT cap no longer fits inside it
        blocks = r.entry_blocks(980.5)
        self.assertTrue(any("left of today's loss budget" in b for b in blocks))

    def test_no_risk_limit_in_the_shipped_config_was_loosened(self):
        here = os.path.dirname(os.path.abspath(__file__))
        c = tb.load_config(os.path.join(here, "config.json"))
        self.assertLessEqual(c["risk_per_trade_pct"], 0.5)
        self.assertLessEqual(c["max_risk_usdt_abs"], 1.00)
        self.assertLessEqual(c["max_daily_loss_pct"], 2.0)
        self.assertLessEqual(c["max_account_drawdown_pct"], 5.0)
        self.assertLessEqual(c["monthly_drawdown_stop_pct"], 6.0)
        self.assertEqual(c["max_open_positions"], 1)
        self.assertLessEqual(c["max_trades_per_day"], 3)
        self.assertGreaterEqual(c["loss_cooldown_min"], 60)


# ==================================================== 7. the emergency halt

class TestEmergencyHalt(TempProject):
    def build(self, balance=1000.0, **over):
        conf = cfg(max_risk_usdt_abs=1.00, symbols=["ETHUSDT"], **over)
        c1, c4 = candles(140, 0, HOUR, 3000.0), candles(260, 0, 4 * HOUR, 3000.0)
        client = FakeClient(c1=c1, c4=c4, price=3000.0)
        n = FakeNotifier()
        bot, _broker, _n = self.make_bot(
            client, state={"paper_balance": balance}, conf=conf, notifier=n)
        return bot, n

    def test_halt_closes_an_open_position_and_journals_it(self):
        bot, _ = self.build()
        broker = bot.b
        p = broker.open("ETHUSDT", "LONG", 0.01, 2970.0, 3000.0, ETH,
                        meta={"planned_risk": 0.11, "equity_before": 1000.0})
        bot.s["position"] = p
        bot.emergency("manual /halt from Telegram", kind="manual")
        self.assertIsNone(bot.s.get("position"), "the position was not closed")
        rows = tb.load_journal(tb.JOURNAL_FILE)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["exit_reason"], "emergency halt")

    def test_halt_blocks_new_entries_and_switches_paper_off(self):
        bot, _ = self.build()
        bot.emergency("manual /halt from Telegram", kind="manual")
        self.assertTrue(bot.s["halted"])
        self.assertTrue(bot.s["paused"])
        self.assertFalse(bot.s["paper_enabled"])
        blocks = bot.entry_blocks()
        self.assertTrue(any("HALTED" in b for b in blocks))

    def test_a_halted_engine_takes_no_entry_even_on_a_valid_signal(self):
        conf = cfg(max_risk_usdt_abs=1.00, symbols=["ETHUSDT"])
        c1, c4 = signal_series_long()
        client = FakeClient(c1=c1, c4=c4, price=c1[-1]["c"])
        bot, _broker, _n = self.make_bot(
            client, state={"paper_balance": 1000.0}, conf=conf)
        bot.risk.halt("audit test")
        bot.look_for_entry()
        self.assertIsNone(bot.s.get("position"))

    def test_paper_on_is_refused_while_halted(self):
        bot, n = self.build()
        bot.emergency("manual /halt from Telegram", kind="manual")
        bot.dispatch("/paper_on", [])
        self.assertFalse(bot.s["paper_enabled"],
                         "/paper_on re-armed entries on a halted engine")
        self.assertIn("halted", n.last().lower())

    def test_clearing_a_halt_needs_an_explicit_confirmation(self):
        bot, n = self.build()
        bot.emergency("manual /halt from Telegram", kind="manual")
        bot.dispatch("/clearhalt", [])
        self.assertTrue(bot.s["halted"], "a bare /clearhalt lifted the halt")
        self.assertIn("CONFIRM", n.last())
        bot.dispatch("/clearhalt", ["CONFIRM"])
        self.assertFalse(bot.s["halted"])

    def test_clearing_a_halt_leaves_entries_off(self):
        """Re-arming is a separate, deliberate decision."""
        bot, _ = self.build()
        bot.emergency("manual /halt from Telegram", kind="manual")
        bot.dispatch("/clearhalt", ["CONFIRM"])
        self.assertFalse(bot.s["halted"])
        self.assertFalse(bot.s["paper_enabled"], "entries came back by themselves")
        self.assertTrue(bot.s["paused"])

    def test_halt_preserves_the_balance_and_the_journal(self):
        bot, _ = self.build(balance=1234.5)
        tb.journal({"trade_id": "old", "mode": "PAPER", "symbol": "ETHUSDT",
                    "side": "LONG", "pnl": 1.0})
        before = len(tb.load_journal(tb.JOURNAL_FILE))
        bot.emergency("manual /halt from Telegram", kind="manual")
        self.assertEqual(bot.s["paper_balance"], 1234.5)
        self.assertEqual(len(tb.load_journal(tb.JOURNAL_FILE)), before)

    def test_a_daily_breach_locks_the_day_rather_than_halting(self):
        bot, _ = self.build()
        bot.emergency("daily limit", kind="daily")
        self.assertTrue(bot.risk.is_locked_today())
        self.assertFalse(bot.s["halted"])
        self.assertFalse(bot.s["paper_enabled"])

    def test_a_drawdown_breach_halts_until_a_human_clears_it(self):
        bot, _ = self.build()
        bot.emergency("drawdown limit", kind="drawdown")
        self.assertTrue(bot.s["halted"])

    def test_resetpaper_is_refused_while_a_position_is_open(self):
        bot, n = self.build()
        bot.s["position"] = {"symbol": "ETHUSDT", "side": "LONG", "qty": 0.01,
                             "entry": 3000.0, "stop": 2970.0}
        bot.dispatch("/resetpaper", ["CONFIRM"])
        self.assertIsNotNone(bot.s["position"])
        self.assertIn("Refusing to reset", n.last())


# ================================== 8. regression guards on the live account

class TestTheLivePaperAccountIsPreserved(unittest.TestCase):
    """Guards on the real files in the project directory, not a temp copy."""

    HERE = os.path.dirname(os.path.abspath(__file__))

    def state(self):
        path = os.path.join(self.HERE, "bot_state_paper.json")
        if not os.path.exists(path):
            self.skipTest("no live paper state on this machine")
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    def test_the_account_is_flat(self):
        self.assertIsNone(self.state().get("position"))

    def test_entries_are_off(self):
        s = self.state()
        self.assertFalse(s.get("paper_enabled"))
        self.assertTrue(s.get("paused"))

    def test_the_emergency_halt_is_active(self):
        s = self.state()
        self.assertTrue(s.get("halted"))
        self.assertTrue(s.get("halt_reason"))

    def test_the_config_is_paper_only_with_real_money_refused(self):
        c = tb.load_config(os.path.join(self.HERE, "config.json"))
        self.assertEqual(c["mode"], "paper")
        self.assertFalse(c["i_understand_real_money"])
        self.assertFalse(c["paper_trading_enabled"])

    def test_the_closed_eth_trade_is_still_on_record(self):
        rows = tb.load_journal(os.path.join(self.HERE, "trades.csv"))
        eth = [r for r in rows if r.get("symbol") == "ETHUSDT"]
        self.assertTrue(eth, "the ETH trade has gone from the journal")
        self.assertEqual(eth[0]["side"], "SHORT")
        self.assertEqual(eth[0]["mode"], "PAPER")

    def test_the_effective_cap_on_this_account_is_the_percentage_target(self):
        """At ~22 USDT the 0.5% target (~0.11) is stricter than 1.00."""
        c = tb.load_config(os.path.join(self.HERE, "config.json"))
        equity = float(self.state().get("paper_balance"))
        budget, which, _ = tb.risk_budget(equity, c)
        self.assertEqual(which, "pct")
        self.assertLess(budget, 1.00)
        self.assertAlmostEqual(budget, equity * 0.005, places=9)


if __name__ == "__main__":
    unittest.main(verbosity=2)
