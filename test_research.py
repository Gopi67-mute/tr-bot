#!/usr/bin/env python3
"""Validation-reporting test suite for research.py.

Run with:   python -m unittest -v test_research

These tests do not assert that the strategy is any good - that is the whole
point of the module. They assert that the *reporting* is honest: that the
in-sample and out-of-sample windows do not overlap, that no value from after a
window leaks into it, that fees and slippage are charged, that the risk engine
is active during a replay, and that the report says plainly how little a small
sample can establish.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import research  # noqa: E402
import trading_bot as tb  # noqa: E402
from test_trading_bot import (BT_BASE, BT_WARMUP, H4, HOUR, RULES,  # noqa: E402
                              FakeClient, backtest_series, candles, cfg)


class TestWindows(unittest.TestCase):
    def setUp(self):
        self.c1, self.c4 = backtest_series()

    def test_the_split_divides_the_history_without_overlap(self):
        cut = research.split_index(self.c1, 0.3)
        is1 = [c for c in self.c1 if c["t"] < cut]
        oos1 = [c for c in self.c1 if c["t"] >= cut]
        self.assertEqual(len(is1) + len(oos1), len(self.c1))
        self.assertAlmostEqual(len(oos1) / len(self.c1), 0.3, places=1)
        self.assertLess(max(c["T"] for c in is1), min(c["t"] for c in oos1),
                        "the in-sample window overlaps the hold-out")

    def test_the_split_never_empties_either_side(self):
        for fraction in (0.01, 0.3, 0.89):
            cut = research.split_index(self.c1, fraction)
            self.assertTrue([c for c in self.c1 if c["t"] < cut])
            self.assertTrue([c for c in self.c1 if c["t"] >= cut])
        self.assertIsNone(research.split_index([], 0.3))

    def test_window_filters_to_closed_candles_only(self):
        lo, hi = self.c1[10]["t"], self.c1[20]["t"]
        got = research.window(self.c1, lo, hi)
        self.assertTrue(all(lo <= c["t"] and c["T"] < hi for c in got))

    def test_an_out_of_sample_replay_cannot_see_in_sample_equity(self):
        """Each window starts from the configured balance, not a carried one."""
        conf = cfg()
        cut = research.split_index(self.c1, 0.3)
        oos = [c for c in self.c1 if c["t"] >= cut]
        r, _ = research.evaluate(conf, oos, self.c4, RULES["ETHUSDT"], "ETHUSDT")
        self.assertEqual(r["start"], conf["paper_start_balance"])

    def test_a_window_too_short_to_warm_up_is_reported_not_faked(self):
        short = self.c1[:20]
        r, stats = research.evaluate(cfg(), short, self.c4, RULES["ETHUSDT"],
                                     "ETHUSDT")
        self.assertIsNone(r)
        self.assertIsNone(stats)

    def test_no_trade_uses_a_price_from_after_its_entry_candle(self):
        conf = cfg(slippage_pct=0.0)
        r, _ = research.evaluate(conf, self.c1, self.c4, RULES["ETHUSDT"], "ETHUSDT")
        opens = {c["t"]: c["o"] for c in self.c1}
        self.assertTrue(r["trades"])
        for t in r["trades"]:
            self.assertIn(t["opened_ms"], opens)
            self.assertAlmostEqual(t["entry"], opens[t["opened_ms"]], places=9)
            self.assertGreater(t["closed_ms"], t["opened_ms"],
                               "a trade closed before it opened")


class TestWalkForward(unittest.TestCase):
    def setUp(self):
        self.c1, self.c4 = backtest_series()

    def test_the_folds_are_consecutive_and_do_not_overlap(self):
        folds = research.walk_forward(cfg(), self.c1, self.c4, RULES["ETHUSDT"],
                                      "ETHUSDT", 4)
        self.assertEqual(len(folds), 4)
        for a, b in zip(folds, folds[1:]):
            self.assertLessEqual(a["to"], b["from"])

    def test_every_fold_starts_from_the_same_balance(self):
        folds = research.walk_forward(cfg(), self.c1, self.c4, RULES["ETHUSDT"],
                                      "ETHUSDT", 4)
        starts = {f["result"]["start"] for f in folds if f["result"]}
        self.assertEqual(starts, {cfg()["paper_start_balance"]},
                         "one fold compounded into the next")

    def test_too_few_candles_per_fold_returns_nothing(self):
        self.assertEqual(research.walk_forward(cfg(), self.c1[:50], self.c4,
                                               RULES["ETHUSDT"], "ETHUSDT", 4), [])
        self.assertEqual(research.walk_forward(cfg(), self.c1, self.c4,
                                               RULES["ETHUSDT"], "ETHUSDT", 1), [])


class TestCostsAndRisk(unittest.TestCase):
    def setUp(self):
        self.c1, self.c4 = backtest_series()

    def test_fees_and_slippage_are_charged_in_every_replay(self):
        r, stats = research.evaluate(cfg(taker_fee_pct=0.05, slippage_pct=0.03),
                                     self.c1, self.c4, RULES["ETHUSDT"], "ETHUSDT")
        self.assertGreater(stats["total_fees"], 0.0)
        self.assertGreater(stats["total_slippage"], 0.0)
        free, free_stats = research.evaluate(cfg(taker_fee_pct=0.0, slippage_pct=0.0),
                                             self.c1, self.c4, RULES["ETHUSDT"],
                                             "ETHUSDT")
        self.assertEqual(free_stats["total_fees"], 0.0)
        self.assertGreater(free_stats["net_pnl"], stats["net_pnl"],
                           "charging costs did not reduce the result")

    def test_the_replay_honours_the_trades_per_day_limit(self):
        for limit in (1, 3):
            conf = cfg(max_trades_per_day=limit, loss_cooldown_min=0)
            r, _ = research.evaluate(conf, self.c1, self.c4, RULES["ETHUSDT"],
                                     "ETHUSDT")
            per_day = {}
            for t in r["trades"]:
                day = t["opened_ist"][:10]
                per_day[day] = per_day.get(day, 0) + 1
            self.assertTrue(per_day)
            for day, count in per_day.items():
                with self.subTest(limit=limit, day=day):
                    self.assertLessEqual(count, limit, f"{count} entries on {day}")

    def test_the_replay_honours_the_half_percent_risk_cap(self):
        conf = cfg(risk_per_trade_pct=0.5, slippage_pct=0.03)
        r, _ = research.evaluate(conf, self.c1, self.c4, RULES["ETHUSDT"], "ETHUSDT")
        self.assertTrue(r["trades"])
        for t in r["trades"]:
            cap = t["equity_before"] * 0.005
            self.assertLessEqual(t["planned_risk"], cap + 1e-9)

    def test_a_non_gapped_loss_never_exceeds_the_planned_risk(self):
        conf = cfg(risk_per_trade_pct=0.5, slippage_pct=0.0)
        r, _ = research.evaluate(conf, self.c1, self.c4, RULES["ETHUSDT"], "ETHUSDT")
        for t in r["trades"]:
            if t["pnl"] >= 0:
                continue
            gapped = (t["exit"] < t["stop"] - 1e-9 if t["side"] == "LONG"
                      else t["exit"] > t["stop"] + 1e-9)
            if gapped:
                continue
            with self.subTest(opened=t["opened_ist"]):
                self.assertLessEqual(-t["pnl"], t["planned_risk"] + 1e-9)

    def test_a_drawdown_breach_halts_the_replay(self):
        conf = cfg(max_account_drawdown_pct=0.5, risk_per_trade_pct=0.5,
                   max_daily_loss_pct=0.5)
        r, _ = research.evaluate(conf, self.c1, self.c4, RULES["ETHUSDT"], "ETHUSDT")
        loose, _ = research.evaluate(cfg(max_account_drawdown_pct=50.0),
                                     self.c1, self.c4, RULES["ETHUSDT"], "ETHUSDT")
        self.assertLess(len(r["trades"]), len(loose["trades"]),
                        "a 0.5% drawdown limit had no effect on the replay")

    def test_a_flat_market_produces_no_trades_and_no_fees(self):
        flat1 = candles(400, BT_BASE + BT_WARMUP, HOUR, 3000.0)
        flat4 = candles(400, BT_BASE, H4, 3000.0)
        r, stats = research.evaluate(cfg(), flat1, flat4, RULES["ETHUSDT"],
                                     "ETHUSDT")
        self.assertEqual(r["trades"], [])
        self.assertEqual(stats["sample_size"], 0)
        self.assertEqual(r["end"], r["start"])


class TestReport(unittest.TestCase):
    def build(self, conf=None):
        conf = conf or cfg(symbols=["ETHUSDT"], backtest_days=90)
        c1, c4 = backtest_series()
        cut = research.split_index(c1, conf["oos_fraction"])
        is1 = [c for c in c1 if c["t"] < cut]
        oos1 = [c for c in c1 if c["t"] >= cut]
        data = {"ETHUSDT": {
            "full": research.evaluate(conf, c1, c4, RULES["ETHUSDT"], "ETHUSDT"),
            "is": research.evaluate(conf, is1, [c for c in c4 if c["T"] < cut],
                                    RULES["ETHUSDT"], "ETHUSDT"),
            "oos": research.evaluate(conf, oos1, c4, RULES["ETHUSDT"], "ETHUSDT"),
            "wf": research.walk_forward(conf, c1, c4, RULES["ETHUSDT"], "ETHUSDT",
                                        conf["walk_forward_folds"]),
            "is_from": "a", "is_to": "b", "oos_from": "c", "oos_to": "d"}}
        return conf, data

    def test_the_report_states_every_requested_metric(self):
        conf, data = self.build()
        text = "\n".join(research.report_lines(conf, data))
        for needle in ["STRATEGY VALIDATION REPORT", "In-sample", "Out-of-sample",
                       "Walk-forward", "win", "PF", "exp", "maxDD",
                       "Profit factor", "Expectancy", "Max drawdown", "Win rate",
                       "LONG", "SHORT", "fees", "slippage", "streaks",
                       "HOW TO READ THIS", "Funding is not modelled"]:
            self.assertIn(needle, text, f"the report never mentions {needle!r}")

    def test_the_report_warns_about_the_sample_size(self):
        conf, data = self.build()
        text = "\n".join(research.report_lines(conf, data))
        self.assertIn("cannot establish an edge", text)
        self.assertIn("Total closed trades", text)

    def test_the_report_says_nothing_is_optimised(self):
        conf, data = self.build()
        text = "\n".join(research.report_lines(conf, data))
        self.assertIn("No parameter is searched or optimised", text)
        self.assertIn("genuine hold-out", text)

    def test_the_report_warns_against_changing_the_strategy_on_one_loss(self):
        conf, data = self.build()
        text = "\n".join(research.report_lines(conf, data))
        self.assertIn("one losing trade", text)
        self.assertIn("compare it against this baseline", text)

    def test_the_report_states_the_costs_and_limits_it_used(self):
        conf, data = self.build()
        text = "\n".join(research.report_lines(conf, data))
        self.assertIn("0.5%/trade", text)
        self.assertIn("2.0%/day", text)
        self.assertIn("5.0% max drawdown", text)
        self.assertIn("0.05% taker fee per side", text)
        self.assertIn("paper-trading simulator", text)

    def test_an_unevaluated_symbol_is_reported_as_such(self):
        conf = cfg(symbols=["ETHUSDT"])
        text = "\n".join(research.report_lines(
            conf, {"ETHUSDT": {"error": "no candles returned for this window"}}))
        self.assertIn("Not evaluated", text)
        self.assertIn("no candles returned", text)

    def test_run_research_writes_a_report_file(self):
        import tempfile
        conf = cfg(symbols=["ETHUSDT"], backtest_days=30, walk_forward_folds=2)
        c1, c4 = backtest_series()
        client = FakeClient(c1=c1, c4=c4, price=c1[-1]["c"])
        tmp = tempfile.mkdtemp(prefix="trbot-research-")
        old_here, old_stdout = tb.HERE, sys.stdout
        tb.HERE = tmp
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
        try:
            data = research.run_research(conf, client)
        finally:
            sys.stdout.close()
            sys.stdout, tb.HERE = old_stdout, old_here
        path = os.path.join(tmp, "research_report.txt")
        self.assertTrue(os.path.exists(path), "no report file was written")
        with open(path, encoding="utf-8") as f:
            text = f.read()
        self.assertIn("STRATEGY VALIDATION REPORT", text)
        self.assertIn("ETHUSDT", text)
        self.assertIn("ETHUSDT", data)

    def test_research_mode_is_a_public_only_path(self):
        """A research run must be as incapable of ordering as a backtest."""
        self.assertIn("research", tb.MODES)
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "trading_bot.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertIn('if mode in ("backtest", "research", "paper"):', src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
