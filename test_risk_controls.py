#!/usr/bin/env python3
"""Risk-control test suite for the paper-trading engine.

Run with:   python -m unittest -v test_risk_controls

Everything here is offline: a fake exchange serving candles from memory, a fake
Telegram, and a throwaway state file. Several tests assert the *absence* of a
trade, which is the point of the whole exercise - a risk control that only
warns is not a risk control.

Covers, in order:
  1. position sizing at the 0.5% cap, and rejection above it
  2. fees and slippage
  3. stop-loss execution, and the refusal to widen a stop
  4. the 2% daily loss lockout
  5. the 5% account drawdown halt
  6. the daily trade limit, the loss limit and the revenge-trade cooldown
  7. the single-position restriction
  8. failing closed on bad, stale or missing data
  9. persistence and restart recovery
 10. no real order can be reached from any of it
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import trading_bot as tb  # noqa: E402
from test_trading_bot import (H4, HOUR, MIN, RULES, FakeClient,  # noqa: E402
                              FakeNotifier, TempProject, candles, cfg,
                              signal_series_long)


def pos(side="LONG", entry=3000.0, stop=2970.0, qty=0.01, planned=0.11,
        symbol="ETHUSDT", opened=None):
    opened = tb.now_ms() if opened is None else opened
    return {"symbol": symbol, "side": side, "qty": qty, "entry": entry,
            "stop": stop, "initial_stop": stop,
            "target": tb.target_for(side, entry, stop, 2.0),
            "opened": opened, "checked": opened, "trade_id": tb.new_trade_id(),
            "planned_risk": planned, "strategy": tb.STRATEGY_NAME,
            "market_condition": "test", "entry_slippage": 0.0,
            "equity_before": 22.0}


# ------------------------------------------------------- 1. sizing at the cap

class TestSizingCap(TempProject):
    def test_planned_risk_is_exactly_the_half_percent_budget(self):
        """0.5% of equity, fees and slippage included, never more.

        Since the absolute USDT ceiling was added there are two caps and the
        stricter one applies, so the expected budget is the minimum of the
        two. At equity 1000 the 0.5% target (5.00 USDT) is looser than the
        1.00 USDT ceiling, and the ceiling is what must bind.
        """
        conf = cfg(risk_per_trade_pct=0.5, taker_fee_pct=0.05, slippage_pct=0.03,
                   max_risk_usdt_abs=1.00)
        for equity in (22.0, 100.0, 1000.0, 7.5):
            for stop_pct in (0.4, 0.75, 1.5, 3.0):
                entry = 3000.0
                stop = entry * (1 - stop_pct / 100)
                qty, risk, note = tb.size_position(equity, entry, stop,
                                                   RULES["ETHUSDT"], conf,
                                                   fractional=True)
                with self.subTest(equity=equity, stop_pct=stop_pct):
                    self.assertGreater(qty, 0, note)
                    budget = min(equity * 0.005, 1.00)
                    self.assertLessEqual(risk, budget + 1e-9)
                    # and it uses almost all of it: rounding is the only loss
                    self.assertGreater(risk, budget * 0.98)
                    # the percentage alone is never allowed to be the answer
                    # when it is the looser of the two
                    self.assertLessEqual(risk, 1.00 + 1e-9)

    def test_the_cap_cannot_be_exceeded_by_any_stop_distance(self):
        conf = cfg(risk_per_trade_pct=0.5)
        for stop_pct in [x / 100 for x in range(5, 400, 7)]:
            entry = 3000.0
            for side in (1, -1):
                stop = entry * (1 - side * stop_pct / 100)
                qty, risk, _ = tb.size_position(22.0, entry, stop, RULES["ETHUSDT"],
                                                conf, fractional=True)
                if qty == 0:
                    continue
                with self.subTest(stop_pct=stop_pct, side=side):
                    self.assertLessEqual(risk, 22.0 * 0.005 + 1e-9)

    def test_a_zero_or_inverted_stop_is_refused(self):
        conf = cfg()
        for entry, stop in [(3000.0, 3000.0), (3000.0, 0.0), (0.0, 10.0),
                            (-5.0, -6.0)]:
            with self.subTest(entry=entry, stop=stop):
                qty, risk, note = tb.size_position(22.0, entry, stop,
                                                   RULES["ETHUSDT"], conf)
                self.assertEqual((qty, risk), (0.0, 0.0), note)

    def test_an_empty_or_negative_account_is_refused(self):
        conf = cfg()
        for equity in (0.0, -1.0, -100.0):
            self.assertEqual(tb.size_position(equity, 3000.0, 2970.0,
                                              RULES["ETHUSDT"], conf)[0], 0.0)

    def test_a_dust_account_rejects_rather_than_rounding_up(self):
        """With a 1e-6 step even a tiny account sizes down, never up."""
        conf = cfg(risk_per_trade_pct=0.5)
        qty, risk, note = tb.size_position(0.0001, 3000.0, 2970.0,
                                           RULES["ETHUSDT"], conf, fractional=True)
        self.assertEqual(qty, 0.0, note)
        self.assertIn("rounds to zero", note)

    def test_risk_does_not_increase_after_losses(self):
        """No martingale: the same equity always produces the same size."""
        conf = cfg(risk_per_trade_pct=0.5)
        state = {"paper_balance": 22.0}
        risk = tb.RiskManager(conf, state)
        risk.roll(22.0, force=True)
        first = tb.size_position(22.0, 3000.0, 2970.0, RULES["ETHUSDT"], conf,
                                 fractional=True)
        for _ in range(3):
            risk.record_close(-0.11)
        again = tb.size_position(22.0, 3000.0, 2970.0, RULES["ETHUSDT"], conf,
                                 fractional=True)
        self.assertEqual(first[:2], again[:2],
                         "size changed after a run of losses")

    def test_risk_falls_with_equity_and_never_rises_to_recover(self):
        conf = cfg(risk_per_trade_pct=0.5)
        sizes = [tb.size_position(e, 3000.0, 2970.0, RULES["ETHUSDT"], conf,
                                  fractional=True)[1]
                 for e in (22.0, 21.0, 20.0, 19.0)]
        self.assertEqual(sizes, sorted(sizes, reverse=True),
                         "risk grew as the account shrank")
        self.assertAlmostEqual(sizes[0] / 22.0, sizes[-1] / 19.0, places=5)

    def test_the_bot_refuses_a_size_that_would_break_the_cap(self):
        """A tripwire: even if sizing were wrong, the entry path re-checks it."""
        c1, c4 = signal_series_long()
        client = FakeClient(c1=c1, c4=c4, price=c1[-1]["c"])
        case = _Case(self, client, conf=cfg(symbols=["ETHUSDT"]))
        bot, n = case.bot, case.n
        real = tb.size_position
        try:
            tb.size_position = lambda *a, **k: (999.0, 99.0, "deliberately oversized")
            bot.look_for_entry()
        finally:
            tb.size_position = real
        self.assertIsNone(bot.s["position"], "an oversized trade was opened")
        self.assertIn("against a cap of", n.joined())


class _Case:
    """A Bot wired to a fake client, with its own temp state file."""

    def __init__(self, test, client, conf=None, state=None, enabled=True):
        self.conf = conf or cfg()
        self.state = {} if state is None else state
        self.state.setdefault("paper_enabled", enabled)
        self.n = FakeNotifier()
        self.broker = tb.PaperBroker(client, self.conf, self.state)
        self.bot = tb.Bot(self.conf, client, self.broker, self.n, self.state,
                          test.state_path)


# --------------------------------------------------------- 2. fees & slippage

class TestFeesAndSlippage(unittest.TestCase):
    def test_slip_amount_and_direction(self):
        self.assertAlmostEqual(tb.slip_amount(3000.0, 0.03), 0.9)
        self.assertAlmostEqual(tb.slip_amount(3000.0, 0.0), 0.0)
        # entering long pays up, entering short gets less
        self.assertAlmostEqual(tb.adverse_fill("LONG", 3000.0, 0.03, True), 3000.9)
        self.assertAlmostEqual(tb.adverse_fill("SHORT", 3000.0, 0.03, True), 2999.1)
        # exiting is the mirror image, and still against us
        self.assertAlmostEqual(tb.adverse_fill("LONG", 3000.0, 0.03, False), 2999.1)
        self.assertAlmostEqual(tb.adverse_fill("SHORT", 3000.0, 0.03, False), 3000.9)

    def test_cost_per_unit_sums_move_fees_and_slippage(self):
        got = tb.cost_per_unit(3000.0, 2970.0, 0.05, 0.03)
        self.assertAlmostEqual(got, 30.0 + 5970 * 0.0005 + 5970 * 0.0003)

    def test_fees_are_charged_on_both_legs(self):
        pnl, fees = tb.trade_pnl("LONG", 100.0, 100.0, 2.0, 0.05)
        self.assertAlmostEqual(fees, (100 + 100) * 2 * 0.0005)
        self.assertAlmostEqual(pnl, -fees, msg="a flat round trip still costs fees")

    def test_paper_entry_fill_is_worsened_by_slippage(self):
        conf = cfg(slippage_pct=0.1, taker_fee_pct=0.05)
        b = tb.PaperBroker(FakeClient(), conf, {})
        long_p = b.open("ETHUSDT", "LONG", 0.01, 2970.0, 3000.0, RULES["ETHUSDT"])
        self.assertAlmostEqual(long_p["entry"], 3003.0)
        short_p = b.open("ETHUSDT", "SHORT", 0.01, 3030.0, 3000.0, RULES["ETHUSDT"])
        self.assertAlmostEqual(short_p["entry"], 2997.0)
        self.assertGreater(long_p["entry_slippage"], 0)

    def test_paper_exit_fill_is_worsened_by_slippage(self):
        conf = cfg(slippage_pct=0.1, taker_fee_pct=0.0)
        state = {"paper_balance": 22.0}
        b = tb.PaperBroker(FakeClient(price=3000.0), conf, state)
        p = pos(entry=3000.0, stop=2970.0, qty=0.01)
        p["entry_slippage"] = 0.0
        res = b.close_now(p)
        self.assertAlmostEqual(res["exit"], 2997.0)   # sold 0.1% lower
        self.assertAlmostEqual(res["slippage"], 3.0 * 0.01)
        self.assertLess(res["pnl"], 0.0)

    def test_a_take_profit_never_fills_better_than_the_target(self):
        conf = cfg(slippage_pct=0.1, taker_fee_pct=0.0)
        m0 = (tb.now_ms() // MIN) * MIN - 10 * MIN
        p = pos(entry=3000.0, stop=2970.0, opened=m0)
        c1m = [{"t": m0 + MIN, "o": 3000.0, "h": 3200.0, "l": 2999.0, "c": 3100.0,
                "T": m0 + 2 * MIN - 1}]
        b = tb.PaperBroker(FakeClient(c1m=c1m), conf, {})
        res = b.check(p)
        self.assertEqual(res["reason"], "take profit")
        self.assertLess(res["exit"], p["target"],
                        "a favourable gap must not be credited")

    def test_a_stop_on_the_wrong_side_of_the_fill_is_refused(self):
        """A position may never exist without a stop that can still fire."""
        b = tb.PaperBroker(FakeClient(), cfg(slippage_pct=0.0), {})
        for side, stop in [("LONG", 3010.0), ("LONG", 3000.0),
                           ("SHORT", 2990.0), ("SHORT", 3000.0)]:
            with self.subTest(side=side, stop=stop):
                with self.assertRaises(ValueError):
                    b.open("ETHUSDT", side, 0.01, stop, 3000.0, RULES["ETHUSDT"])

    def test_entry_slippage_always_moves_away_from_the_stop(self):
        """Paying up to get long cannot push the fill through a stop below it."""
        b = tb.PaperBroker(FakeClient(), cfg(slippage_pct=2.0), {})
        long_p = b.open("ETHUSDT", "LONG", 0.01, 2900.0, 3000.0, RULES["ETHUSDT"])
        self.assertGreater(long_p["entry"] - long_p["stop"], 100.0)
        short_p = b.open("ETHUSDT", "SHORT", 0.01, 3100.0, 3000.0, RULES["ETHUSDT"])
        self.assertGreater(short_p["stop"] - short_p["entry"], 100.0)

    def test_mark_to_market_includes_fees_and_exit_slippage(self):
        conf = cfg(slippage_pct=0.03, taker_fee_pct=0.05)
        b = tb.PaperBroker(FakeClient(), conf, {})
        p = pos(entry=3000.0, stop=2970.0, qty=0.01)
        flat = b.mark_to_market(p, 3000.0)
        self.assertLess(flat, 0.0, "a flat position still costs the round trip")


# -------------------------------------------------- 3. stop-loss execution

class TestStopExecution(TempProject):
    def setUp(self):
        super().setUp()
        self.m0 = (tb.now_ms() // MIN) * MIN - 20 * MIN

    def test_a_stop_hit_closes_the_position_and_journals_the_loss(self):
        conf = cfg(slippage_pct=0.0, taker_fee_pct=0.05)
        p = pos(entry=3000.0, stop=2970.0, qty=0.01, opened=self.m0)
        c1m = [{"t": self.m0 + MIN, "o": 2999.0, "h": 3001.0, "l": 2960.0,
                "c": 2965.0, "T": self.m0 + 2 * MIN - 1}]
        client = FakeClient(c1m=c1m, price=2965.0,
                            c1=candles(60, 0, HOUR, 3000.0),
                            c4=candles(60, 0, H4, 3000.0))
        case = _Case(self, client, conf=conf, state={"paper_balance": 22.0})
        case.bot.s["position"] = p
        case.bot.step()
        self.assertIsNone(case.bot.s["position"], "the stop did not close the trade")
        self.assertLess(case.state["paper_balance"], 22.0)
        rows = tb.load_journal(mode="PAPER")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["exit_reason"], "stop loss")
        self.assertEqual(float(rows[0]["initial_stop"]), 2970.0)
        self.assertLess(float(rows[0]["pnl"]), 0.0)

    def test_the_actual_loss_is_recorded_against_the_planned_risk(self):
        conf = cfg(slippage_pct=0.03, taker_fee_pct=0.05, risk_per_trade_pct=0.5)
        entry, stop = 3000.0, 2970.0
        qty, planned, _ = tb.size_position(22.0, entry, stop, RULES["ETHUSDT"],
                                           conf, fractional=True)
        p = pos(entry=entry, stop=stop, qty=qty, planned=planned, opened=self.m0)
        c1m = [{"t": self.m0 + MIN, "o": 2999.0, "h": 3000.0, "l": 2960.0,
                "c": 2965.0, "T": self.m0 + 2 * MIN - 1}]
        case = _Case(self, FakeClient(c1m=c1m, price=2965.0), conf=conf,
                     state={"paper_balance": 22.0})
        case.bot.s["position"] = p
        res = case.broker.check(p)
        case.bot._finish(res)
        row = tb.load_journal(mode="PAPER")[0]
        self.assertAlmostEqual(float(row["planned_risk"]), planned, places=5)
        # the planned risk budgeted an entry slippage leg that a stop fill at
        # the level does not spend, so the actual loss lands just inside it
        self.assertLessEqual(-float(row["pnl"]), planned + 1e-9)
        self.assertGreater(-float(row["pnl"]), planned * 0.8)
        self.assertAlmostEqual(float(row["r_multiple"]),
                               float(row["pnl"]) / planned, places=4)

    def test_a_gap_through_the_stop_is_recorded_as_the_larger_loss(self):
        conf = cfg(slippage_pct=0.0, taker_fee_pct=0.05)
        p = pos(entry=3000.0, stop=2970.0, qty=0.01, planned=0.315,
                opened=self.m0)
        c1m = [{"t": self.m0 + MIN, "o": 2900.0, "h": 2905.0, "l": 2890.0,
                "c": 2895.0, "T": self.m0 + 2 * MIN - 1}]
        b = tb.PaperBroker(FakeClient(c1m=c1m), conf, {"paper_balance": 22.0})
        res = b.check(p)
        self.assertEqual(res["exit"], 2900.0, "a gap must not fill at the stop")
        self.assertLess(res["pnl"], -p["planned_risk"],
                        "a gap loss should exceed the planned risk")

    def test_a_stop_may_be_tightened_but_never_widened(self):
        for side, better, worse in [("LONG", 2980.0, 2960.0),
                                    ("SHORT", 3020.0, 3040.0)]:
            with self.subTest(side=side):
                p = pos(side=side, entry=3000.0,
                        stop=2970.0 if side == "LONG" else 3030.0)
                original = p["stop"]
                self.assertFalse(tb.stop_is_widened(side, 3000.0, original, better))
                self.assertTrue(tb.stop_is_widened(side, 3000.0, original, worse))
                self.assertTrue(tb.tighten_stop(p, better))
                self.assertEqual(p["stop"], better)
                self.assertFalse(tb.tighten_stop(p, worse),
                                 "a widening request was accepted")
                self.assertEqual(p["stop"], better, "the stop was widened")

    def test_a_position_always_carries_a_stop(self):
        conf = cfg()
        b = tb.PaperBroker(FakeClient(), conf, {})
        p = b.open("ETHUSDT", "LONG", 0.01, 2970.0, 3000.0, RULES["ETHUSDT"])
        self.assertIn("stop", p)
        self.assertLess(p["stop"], p["entry"])
        self.assertEqual(p["stop"], p["initial_stop"])
        with self.assertRaises(ValueError):
            b.open("ETHUSDT", "LONG", 0.0, 2970.0, 3000.0, RULES["ETHUSDT"])


# ------------------------------------------------------- 4. daily loss lockout

class TestDailyLossLockout(TempProject):
    def armed(self, balance=22.0, day_start=22.0, peak=22.0, conf=None,
              price=3000.0, c1m=None):
        conf = conf or cfg(max_daily_loss_pct=2.0, max_account_drawdown_pct=50.0)
        state = {"paper_balance": balance, "day": tb.day_key(),
                 "day_start_equity": day_start, "equity_peak": peak,
                 "month": tb.month_key(), "month_start": day_start}
        client = FakeClient(c1=candles(60, 0, HOUR, 3000.0),
                            c4=candles(60, 0, H4, 3000.0), price=price,
                            c1m=c1m or [])
        return _Case(self, client, conf=conf, state=state)

    def test_the_limit_is_measured_on_realised_plus_unrealised(self):
        case = self.armed(balance=22.0, day_start=22.0)
        risk = case.bot.risk
        self.assertAlmostEqual(risk.daily_loss_pct(21.6), 100 * 0.4 / 22)
        self.assertIsNone(risk.breach(21.6))
        self.assertEqual(risk.breach(21.55)[0], "daily")   # -2.045%
        self.assertIn("daily limit", risk.breach(21.55)[1])

    def test_an_unrealised_loss_alone_can_trip_the_limit(self):
        """A 2% loss on an open trade locks the day; it does not wait for the exit."""
        case = self.armed(balance=22.0, day_start=22.0, price=2800.0)
        case.bot.s["position"] = pos(entry=3000.0, stop=2970.0, qty=0.0025)
        eq, err = case.bot.equity()
        self.assertIsNone(err)
        self.assertLess(eq, 22.0 * 0.98)
        case.bot.step()
        self.assertTrue(case.bot.risk.is_locked_today())
        self.assertIsNone(case.bot.s["position"], "the position was left open")
        self.assertFalse(case.bot.s["paper_enabled"])
        self.assertIn("daily limit", case.n.joined())

    def test_the_lockout_closes_blocks_and_notifies(self):
        case = self.armed(balance=21.5, day_start=22.0)
        case.bot.s["position"] = pos(entry=3000.0, stop=2970.0, qty=0.001)
        case.bot.step()
        self.assertTrue(case.bot.risk.is_locked_today())
        self.assertIsNone(case.bot.s["position"])
        self.assertFalse(case.bot.s["paper_enabled"])
        blocks = " ".join(case.bot.entry_blocks())
        self.assertIn("locked until the next trading day", blocks)
        joined = case.n.joined()
        self.assertIn("⛔", joined)
        self.assertIn("locked until the next trading day", joined)

    def test_the_lockout_is_not_announced_over_and_over(self):
        case = self.armed(balance=21.5, day_start=22.0)
        case.bot.step()
        first = len([m for m in case.n.sent if "daily limit" in m])
        for _ in range(3):
            case.bot.step()
        self.assertEqual(len([m for m in case.n.sent if "daily limit" in m]), first)

    def test_paper_on_cannot_unlock_the_day(self):
        case = self.armed(balance=21.5, day_start=22.0)
        case.bot.step()
        case.bot.dispatch("/paper_on", [])
        self.assertIn("locked until the next trading day",
                      " ".join(case.bot.entry_blocks()))

    def test_a_new_day_releases_the_lock_and_rebaselines(self):
        case = self.armed(balance=21.5, day_start=22.0)
        case.bot.step()
        self.assertTrue(case.bot.risk.is_locked_today())
        case.bot.risk.roll(21.5, ms=tb.now_ms() + 86400_000)   # next day
        self.assertFalse(case.bot.risk.is_locked_today())
        self.assertEqual(case.bot.s["day_start_equity"], 21.5)
        self.assertEqual(case.bot.s["trades_today"], 0)
        self.assertEqual(case.bot.s["losses_today"], 0)

    def test_a_restart_does_not_hand_back_a_fresh_daily_budget(self):
        case = self.armed(balance=21.5, day_start=22.0)
        case.bot.step()
        tb.save_state(case.state, self.state_path)
        again = _Case(self, FakeClient(price=3000.0),
                      conf=case.conf, state=tb.load_state(self.state_path))
        self.assertEqual(again.bot.s["day_start_equity"], 22.0)
        self.assertTrue(again.bot.risk.is_locked_today())

    def test_a_trade_that_will_not_fit_the_remaining_budget_is_refused(self):
        conf = cfg(max_daily_loss_pct=2.0, risk_per_trade_pct=0.5,
                   max_account_drawdown_pct=50.0)
        case = self.armed(balance=21.66, day_start=22.0, conf=conf)
        # 0.34 of the 0.44 budget is already gone; a 0.108 trade would not fit
        left = case.bot.risk.remaining_daily_loss(21.66)
        self.assertLess(left, case.bot.risk.max_risk_usdt(21.66))
        self.assertIn("left of today's loss budget",
                      " ".join(case.bot.entry_blocks()))


# ---------------------------------------------------- 5. drawdown halt

class TestDrawdownHalt(TempProject):
    def armed(self, balance, peak, conf=None, price=3000.0):
        conf = conf or cfg(max_account_drawdown_pct=5.0, max_daily_loss_pct=90.0,
                           monthly_drawdown_stop_pct=90.0)
        state = {"paper_balance": balance, "day": tb.day_key(),
                 "day_start_equity": balance, "equity_peak": peak,
                 "month": tb.month_key(), "month_start": balance}
        client = FakeClient(c1=candles(60, 0, HOUR, 3000.0),
                            c4=candles(60, 0, H4, 3000.0), price=price)
        return _Case(self, client, conf=conf, state=state)

    def test_the_equity_peak_only_ever_rises(self):
        risk = tb.RiskManager(cfg(), {})
        risk.roll(22.0, force=True)
        self.assertEqual(risk.observe(25.0), 25.0)
        self.assertEqual(risk.observe(20.0), 25.0)
        self.assertEqual(risk.observe(24.9), 25.0)
        self.assertAlmostEqual(risk.drawdown_pct(20.0), 20.0)
        self.assertEqual(risk.drawdown_pct(30.0), 0.0)

    def test_the_peak_survives_a_restart(self):
        case = self.armed(balance=22.0, peak=22.0)
        case.bot.risk.observe(30.0)
        tb.save_state(case.state, self.state_path)
        again = _Case(self, FakeClient(price=3000.0), conf=case.conf,
                      state=tb.load_state(self.state_path))
        self.assertEqual(again.bot.risk.peak(0.0), 30.0)

    def test_five_percent_drawdown_halts_the_engine(self):
        case = self.armed(balance=20.8, peak=22.0)      # -5.45%
        case.bot.s["position"] = pos(entry=3000.0, stop=2970.0, qty=0.001)
        case.bot.step()
        self.assertTrue(case.bot.risk.is_halted())
        self.assertIn("drawdown", case.bot.s["halt_reason"])
        self.assertIsNone(case.bot.s["position"], "the position was left open")
        self.assertFalse(case.bot.s["paper_enabled"])
        self.assertIn("🚨", case.n.joined())
        self.assertIn("HALTED", case.n.joined())

    def test_a_halt_blocks_entries_permanently_until_cleared(self):
        case = self.armed(balance=20.8, peak=22.0)
        case.bot.step()
        for _ in range(3):
            case.bot.step()
            self.assertTrue(case.bot.risk.is_halted())
        case.bot.dispatch("/paper_on", [])
        self.assertFalse(case.bot.s["paper_enabled"],
                         "/paper_on re-armed a halted engine")
        self.assertIn("Refused", case.n.joined())
        # even a new day does not clear it
        case.bot.risk.roll(20.8, ms=tb.now_ms() + 86400_000)
        self.assertTrue(case.bot.risk.is_halted())

    def test_clearing_a_halt_needs_an_explicit_confirmation(self):
        case = self.armed(balance=20.8, peak=22.0)
        case.bot.step()
        case.bot.dispatch("/clearhalt", [])
        self.assertTrue(case.bot.risk.is_halted(), "a bare /clearhalt lifted it")
        self.assertIn("/clearhalt CONFIRM", case.n.joined())
        case.bot.dispatch("/clearhalt", ["confirm"])
        self.assertFalse(case.bot.risk.is_halted())
        self.assertAlmostEqual(case.bot.s["equity_peak"], 20.8)
        self.assertFalse(case.bot.s["paper_enabled"],
                         "clearing a halt must not also re-arm trading")
        self.assertTrue(case.bot.s.get("halt_history"))

    def test_a_cleared_halt_does_not_immediately_re_fire(self):
        case = self.armed(balance=20.8, peak=22.0)
        case.bot.step()
        case.bot.dispatch("/clearhalt", ["CONFIRM"])
        case.bot.step()
        self.assertFalse(case.bot.risk.is_halted())

    def test_the_halt_survives_a_restart(self):
        case = self.armed(balance=20.8, peak=22.0)
        case.bot.step()
        tb.save_state(case.state, self.state_path)
        again = _Case(self, FakeClient(price=3000.0), conf=case.conf,
                      state=tb.load_state(self.state_path), enabled=True)
        self.assertTrue(again.bot.risk.is_halted())
        self.assertIn("HALTED", " ".join(again.bot.entry_blocks()))

    def test_manual_halt_closes_and_blocks(self):
        case = self.armed(balance=22.0, peak=22.0)
        case.bot.s["position"] = pos(entry=3000.0, stop=2970.0, qty=0.001)
        case.bot.dispatch("/halt", [])
        self.assertIsNone(case.bot.s["position"])
        self.assertTrue(case.bot.risk.is_halted())
        self.assertFalse(case.bot.s["paper_enabled"])
        rows = tb.load_journal(mode="PAPER")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["exit_reason"], "emergency halt")

    def test_halt_with_no_position_still_blocks(self):
        case = self.armed(balance=22.0, peak=22.0)
        case.bot.dispatch("/halt", [])
        self.assertTrue(case.bot.risk.is_halted())
        self.assertIn("No position was open", case.n.joined())


# ------------------------------------------- 6. trade counts and cooldown

class TestTradeLimits(TempProject):
    def armed(self, conf=None, **extra):
        conf = conf or cfg()
        state = {"paper_balance": 22.0, "day": tb.day_key(),
                 "day_start_equity": 22.0, "equity_peak": 22.0,
                 "month": tb.month_key(), "month_start": 22.0}
        state.update(extra)
        c1, c4 = signal_series_long()
        client = FakeClient(c1=c1, c4=c4, price=c1[-1]["c"])
        return _Case(self, client, conf=conf, state=state)

    def test_three_trades_a_day_is_the_limit(self):
        case = self.armed(trades_today=3)
        self.assertIn("3 of 3 trades already taken today",
                      " ".join(case.bot.entry_blocks()))
        case.bot.step()
        self.assertIsNone(case.bot.s["position"])

    def test_the_count_is_incremented_when_a_trade_opens(self):
        case = self.armed(conf=cfg(symbols=["ETHUSDT"]))
        self.assertEqual(case.bot.s["trades_today"], 0)
        case.bot.look_for_entry()
        self.assertIsNotNone(case.bot.s["position"], case.n.joined())
        self.assertEqual(case.bot.s["trades_today"], 1)

    def test_two_losses_a_day_stops_trading(self):
        case = self.armed(losses_today=2)
        self.assertIn("losses today", " ".join(case.bot.entry_blocks()))

    def test_a_loss_starts_a_cooldown_that_blocks_revenge_trades(self):
        case = self.armed(conf=cfg(loss_cooldown_min=60))
        case.bot.risk.record_close(-0.11)
        blocks = " ".join(case.bot.entry_blocks())
        self.assertIn("cooling off after a loss", blocks)
        self.assertIn("no revenge trades", blocks)
        # the cooldown expires on its own
        case.bot.s["last_loss_ms"] = tb.now_ms() - 61 * 60 * 1000
        self.assertNotIn("cooling off", " ".join(case.bot.entry_blocks()))

    def test_a_win_starts_no_cooldown(self):
        case = self.armed(conf=cfg(loss_cooldown_min=60))
        case.bot.risk.record_close(+0.22)
        self.assertEqual(case.bot.risk.cooldown_left_sec(), 0)
        self.assertNotIn("cooling off", " ".join(case.bot.entry_blocks()))

    def test_trades_left_today_is_reported(self):
        case = self.armed(trades_today=1)
        self.assertEqual(case.bot.risk.trades_left_today(), 2)
        text = case.bot.risk.limits_text(22.0)
        self.assertIn("Trades today: 1/3", text)
        self.assertIn("0.5%", text)
        self.assertIn("No averaging down", text)


# ----------------------------------------------- 7. one position at a time

class TestSinglePosition(TempProject):
    def test_a_second_entry_is_never_opened(self):
        c1, c4 = signal_series_long()
        client = FakeClient(c1=c1, c4=c4, price=c1[-1]["c"], c1m=[])
        case = _Case(self, client, conf=cfg(symbols=["ETHUSDT"]))
        case.bot.look_for_entry()
        first = case.bot.s["position"]
        self.assertIsNotNone(first, case.n.joined())
        case.bot.s["last_candle"] = {}
        case.bot.step()
        self.assertEqual(case.bot.s["position"]["trade_id"], first["trade_id"],
                         "a second position was opened on top of the first")
        self.assertIn("already in a", " ".join(case.bot.entry_blocks()))

    def test_no_averaging_down_on_a_losing_position(self):
        """A losing open trade blocks entries rather than adding to them."""
        c1, c4 = signal_series_long()
        client = FakeClient(c1=c1, c4=c4, price=c1[-1]["c"] * 0.99, c1m=[])
        case = _Case(self, client, conf=cfg(symbols=["ETHUSDT"]))
        case.bot.s["position"] = pos(entry=c1[-1]["c"], stop=c1[-1]["c"] * 0.98,
                                     qty=0.001)
        before = dict(case.bot.s["position"])
        case.bot.s["last_candle"] = {}
        case.bot.look_for_entry()
        self.assertEqual(case.bot.s["position"]["qty"], before["qty"],
                         "the position size grew - that is averaging down")

    def test_max_open_positions_above_one_is_refused_by_the_config(self):
        with self.assertRaises(SystemExit):
            tb.validate_config(cfg(max_open_positions=2))


# ------------------------------------------------- 8. failing closed on data

class TestFailClosed(TempProject):
    def armed(self, client, conf=None):
        state = {"paper_balance": 22.0, "day": tb.day_key(),
                 "day_start_equity": 22.0, "equity_peak": 22.0,
                 "month": tb.month_key(), "month_start": 22.0}
        return _Case(self, client, conf=conf or cfg(symbols=["ETHUSDT"]),
                     state=state)

    def test_stale_candles_block_the_entry(self):
        c1, c4 = signal_series_long()
        old = 10 * 3600_000
        c1 = [{**c, "t": c["t"] - old, "T": c["T"] - old} for c in c1]
        case = self.armed(FakeClient(c1=c1, c4=c4, price=c1[-1]["c"]))
        case.bot.look_for_entry()
        self.assertIsNone(case.bot.s["position"])
        self.assertIn("stale data", case.n.joined())

    def test_no_candles_at_all_blocks_the_entry(self):
        case = self.armed(FakeClient(c1=[], c4=[], price=3000.0))
        case.bot.look_for_entry()
        self.assertIsNone(case.bot.s["position"])

    def test_too_few_candles_block_the_entry(self):
        c1 = candles(10, 0, HOUR, 3000.0)
        case = self.armed(FakeClient(c1=c1, c4=candles(260, 0, H4, 3000.0),
                                     price=3000.0))
        self.assertIn("need", case.bot.data_problem("ETHUSDT"))

    def test_a_malformed_candle_blocks_the_entry(self):
        c1 = candles(60, 0, HOUR, 3000.0)
        c1[-1] = {**c1[-1], "h": 2000.0, "l": 3500.0}
        case = self.armed(FakeClient(c1=c1, c4=candles(260, 0, H4, 3000.0),
                                     price=3000.0))
        self.assertIn("malformed", case.bot.data_problem("ETHUSDT"))

    def test_a_ticker_that_disagrees_with_the_candles_blocks_the_entry(self):
        c1, c4 = signal_series_long()
        case = self.armed(FakeClient(c1=c1, c4=c4, price=c1[-1]["c"] * 3))
        case.bot.look_for_entry()
        self.assertIsNone(case.bot.s["position"])
        self.assertIn("disagrees", case.n.joined())

    def test_a_zero_or_negative_price_is_refused(self):
        case = self.armed(FakeClient(c1=candles(60, 0, HOUR, 3000.0),
                                     c4=candles(60, 0, H4, 3000.0), price=0.0))
        with self.assertRaises(ValueError):
            case.bot.price("ETHUSDT")

    def test_an_unvaluable_account_blocks_entries(self):
        """If the open position cannot be priced, no new risk is taken."""
        c1, c4 = signal_series_long()
        client = FakeClient(c1=c1, c4=c4, price=c1[-1]["c"])
        case = self.armed(client)
        case.bot.s["position"] = pos(entry=3000.0, stop=2970.0, qty=0.001)

        def dead(_symbol):
            raise OSError("ticker unavailable")

        client.price = dead
        eq, err = case.bot.equity()
        self.assertTrue(err)
        self.assertIn("cannot be valued", " ".join(case.bot.entry_blocks()))

    def test_a_drawdown_breach_is_not_evaluated_on_missing_data(self):
        """Fail closed, not fail loud: no equity means no action, and no trade."""
        client = FakeClient(c1=candles(60, 0, HOUR, 3000.0),
                            c4=candles(60, 0, H4, 3000.0), price=3000.0)
        case = self.armed(client)
        case.bot.s["position"] = pos(entry=3000.0, stop=2970.0, qty=0.001)

        def dead(_symbol):
            raise OSError("ticker unavailable")

        client.price = dead
        case.bot.step()                       # must not raise, must not halt
        self.assertFalse(case.bot.risk.is_halted())
        self.assertIsNotNone(case.bot.s["position"])


# --------------------------------------------- 9. persistence and recovery

class TestRecovery(TempProject):
    def test_an_open_position_is_restored_with_every_field(self):
        conf = cfg(symbols=["ETHUSDT"])
        c1, c4 = signal_series_long()
        client = FakeClient(c1=c1, c4=c4, price=c1[-1]["c"], c1m=[])
        case = _Case(self, client, conf=conf,
                     state={"paper_balance": 22.0, "day": tb.day_key(),
                            "day_start_equity": 22.0, "equity_peak": 22.0,
                            "month": tb.month_key(), "month_start": 22.0})
        case.bot.look_for_entry()
        original = dict(case.bot.s["position"])
        self.assertIsNotNone(original.get("trade_id"))
        again = _Case(self, client, conf=conf,
                      state=tb.load_state(self.state_path))
        restored = again.bot.s["position"]
        for key in ("trade_id", "side", "symbol", "entry", "stop", "initial_stop",
                    "target", "qty", "planned_risk", "opened", "market_condition"):
            self.assertEqual(restored[key], original[key], f"{key} was lost")

    def test_a_restart_does_not_duplicate_the_trade(self):
        conf = cfg(symbols=["ETHUSDT"])
        c1, c4 = signal_series_long()
        client = FakeClient(c1=c1, c4=c4, price=c1[-1]["c"], c1m=[])
        case = _Case(self, client, conf=conf,
                     state={"paper_balance": 22.0, "day": tb.day_key(),
                            "day_start_equity": 22.0, "equity_peak": 22.0,
                            "month": tb.month_key(), "month_start": 22.0})
        case.bot.look_for_entry()
        trade_id = case.bot.s["position"]["trade_id"]
        trades_today = case.bot.s["trades_today"]
        for _ in range(3):
            again = _Case(self, client, conf=conf,
                          state=tb.load_state(self.state_path))
            again.bot.step()
            tb.save_state(again.bot.s, self.state_path)
        final = tb.load_state(self.state_path)
        self.assertEqual(final["position"]["trade_id"], trade_id)
        self.assertEqual(final["trades_today"], trades_today,
                         "restarting counted the same trade again")
        self.assertEqual(len(tb.load_journal(mode="PAPER")), 0,
                         "an open trade was journalled")

    def test_a_closed_trade_is_journalled_exactly_once(self):
        conf = cfg(symbols=["ETHUSDT"], slippage_pct=0.0)
        m0 = (tb.now_ms() // MIN) * MIN - 20 * MIN
        c1m = [{"t": m0 + MIN, "o": 2999.0, "h": 3000.0, "l": 2960.0, "c": 2965.0,
                "T": m0 + 2 * MIN - 1}]
        client = FakeClient(c1=candles(60, 0, HOUR, 3000.0),
                            c4=candles(60, 0, H4, 3000.0), price=2965.0, c1m=c1m)
        case = _Case(self, client, conf=conf, state={"paper_balance": 22.0})
        case.bot.s["position"] = pos(entry=3000.0, stop=2970.0, qty=0.001,
                                     opened=m0)
        case.bot.step()
        tb.save_state(case.bot.s, self.state_path)
        for _ in range(3):
            again = _Case(self, client, conf=conf,
                          state=tb.load_state(self.state_path))
            again.bot.step()
            tb.save_state(again.bot.s, self.state_path)
        rows = tb.load_journal(mode="PAPER")
        self.assertEqual(len(rows), 1, f"{len(rows)} rows for one trade")

    def test_the_activation_flag_persists_across_a_restart(self):
        conf = cfg()
        client = FakeClient(c1=candles(60, 0, HOUR, 3000.0),
                            c4=candles(60, 0, H4, 3000.0), price=3000.0)
        case = _Case(self, client, conf=conf, state={"paper_balance": 22.0},
                     enabled=False)
        case.bot.dispatch("/paper_on", [])
        tb.save_state(case.bot.s, self.state_path)
        again = _Case(self, client, conf=conf, state=tb.load_state(self.state_path))
        self.assertTrue(again.bot.s["paper_enabled"])
        again.bot.dispatch("/paper_off", [])
        tb.save_state(again.bot.s, self.state_path)
        third = _Case(self, client, conf=conf, state=tb.load_state(self.state_path))
        self.assertFalse(third.bot.s["paper_enabled"])

    def test_a_fresh_install_starts_with_entries_off(self):
        client = FakeClient(c1=candles(60, 0, HOUR, 3000.0),
                            c4=candles(60, 0, H4, 3000.0), price=3000.0)
        conf = cfg()
        self.assertFalse(conf["paper_trading_enabled"],
                         "the shipped default must be OFF")
        broker = tb.PaperBroker(client, conf, {})
        state = {}
        bot = tb.Bot(conf, client, tb.PaperBroker(client, conf, state),
                     FakeNotifier(), state, self.state_path)
        self.assertFalse(bot.s["paper_enabled"])
        self.assertIn("paper trading is OFF", bot.blocked())
        del broker

    def test_the_engine_lock_refuses_a_second_live_instance(self):
        path = os.path.join(self.tmp, "engine.lock")
        tb.acquire_engine_lock(path)
        data = tb.read_engine_lock(path)
        self.assertEqual(data["pid"], os.getpid())
        # a lock held by another, still-beating process is refused
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"pid": os.getpid() + 1, "heartbeat": tb.time.time()}, f)
        with self.assertRaises(SystemExit) as ctx:
            tb.acquire_engine_lock(path)
        self.assertIn("already running", str(ctx.exception))
        # a stale one is taken over
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"pid": os.getpid() + 1,
                       "heartbeat": tb.time.time() - 10_000}, f)
        tb.acquire_engine_lock(path)
        self.assertEqual(tb.read_engine_lock(path)["pid"], os.getpid())
        tb.release_engine_lock(path)
        self.assertFalse(os.path.exists(path))


# ------------------------------------------------- 10. no real orders, ever

class TestNoRealOrders(TempProject):
    class Tripwire(FakeClient):
        """A client that fails loudly if anything order-shaped is called."""

        # every signed/order-placing call on the real client
        ORDER_METHODS = ("market", "close_all_order", "set_leverage",
                         "ensure_isolated", "cancel_algo_orders",
                         "open_algo_orders", "position_amt", "balance",
                         "hedge_mode")
        # the subset a broker must not expose (PaperBroker legitimately has
        # balance(), which reads the simulated state and talks to nothing)
        BROKER_FORBIDDEN = ("market", "close_all_order", "set_leverage",
                            "ensure_isolated", "cancel_algo_orders",
                            "open_algo_orders")

        def __getattr__(self, name):
            if name in self.ORDER_METHODS:
                raise AssertionError(f"the paper engine called {name}()")
            raise AttributeError(name)

    def test_a_whole_trade_cycle_never_touches_an_order_method(self):
        conf = cfg(symbols=["ETHUSDT"], slippage_pct=0.03)
        c1, c4 = signal_series_long()
        m0 = (tb.now_ms() // MIN) * MIN
        c1m = [{"t": m0, "o": c1[-1]["c"], "h": c1[-1]["c"] * 1.001,
                "l": c1[-1]["c"] * 0.95, "c": c1[-1]["c"] * 0.96,
                "T": m0 + MIN - 1}]
        client = self.Tripwire(c1=c1, c4=c4, price=c1[-1]["c"], c1m=c1m)
        case = _Case(self, client, conf=conf, state={"paper_balance": 22.0})
        case.bot.step()                      # may open
        case.bot.step()                      # may close on the 1m candle
        case.bot.dispatch("/status", [])
        case.bot.dispatch("/positions", [])
        case.bot.dispatch("/balance", [])
        case.bot.dispatch("/halt", [])
        self.assertNotIn("called", case.n.joined())

    def test_the_paper_broker_exposes_no_order_methods(self):
        broker = tb.PaperBroker(FakeClient(), cfg(), {})
        for attr in self.Tripwire.BROKER_FORBIDDEN:
            self.assertFalse(hasattr(broker, attr), f"PaperBroker exposes {attr}")

    def test_a_read_only_client_refuses_every_order_endpoint(self):
        c = tb.Binance(cfg(api_key="K" * 20, api_secret="S" * 20),
                       base=tb.LIVE_BASE, public_only=True)
        self.assertEqual((c.key, c.secret), ("", b""))
        for method, path, signed in [("POST", "/fapi/v1/order", True),
                                     ("POST", "/fapi/v1/algoOrder", True),
                                     ("DELETE", "/fapi/v1/algoOpenOrders", True),
                                     ("POST", "/fapi/v1/leverage", True),
                                     ("GET", "/fapi/v3/balance", True)]:
            with self.subTest(path=path):
                with self.assertRaises(tb.ReadOnlyClient):
                    c._req(method, path, signed=signed)

    def test_paper_mode_never_needs_or_holds_credentials(self):
        conf = cfg()
        self.assertEqual(conf["api_key"], "")
        self.assertEqual(conf["api_secret"], "")
        self.assertFalse(conf["i_understand_real_money"],
                         "the real-money consent flag must stay false")
        self.assertTrue(conf["use_testnet"])

    def test_a_remote_command_cannot_raise_the_risk_or_bypass_a_limit(self):
        """No command writes a risk setting; they only ever read or stop."""
        client = FakeClient(c1=candles(60, 0, HOUR, 3000.0),
                            c4=candles(60, 0, H4, 3000.0), price=3000.0)
        case = _Case(self, client, conf=cfg(), state={"paper_balance": 22.0})
        before = {k: case.conf[k] for k in
                  ("risk_per_trade_pct", "max_daily_loss_pct",
                   "max_account_drawdown_pct", "max_trades_per_day",
                   "max_losses_per_day", "max_leverage", "max_open_positions")}
        for cmd, args in [("/paper_on", []), ("/paper_off", []), ("/status", []),
                          ("/risk", []), ("/balance", []), ("/positions", []),
                          ("/performance", []), ("/history", []), ("/health", []),
                          ("/help", []), ("/resetpaper", []), ("/halt", []),
                          ("/clearhalt", ["CONFIRM"]),
                          ("/risk_per_trade_pct", ["99"]), ("/set", ["risk", "99"])]:
            case.bot.dispatch(cmd, args)
        after = {k: case.conf[k] for k in before}
        self.assertEqual(before, after, "a command changed a risk limit")


if __name__ == "__main__":
    unittest.main(verbosity=2)
