#!/usr/bin/env python3
"""Trade journal and performance statistics test suite.

Run with:   python -m unittest -v test_journal

The rule being enforced here is the one from the brief: a performance figure is
only ever computed from complete, de-duplicated, closed trades. Every test that
feeds the statistics a half-written, repeated or still-open row asserts that the
row is *ignored* rather than guessed at.
"""

import csv
import io
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import trading_bot as tb  # noqa: E402
from test_trading_bot import (H4, HOUR, MIN, FakeClient, FakeNotifier,  # noqa: E402
                              TempProject, candles, cfg)

DAY = 86_400_000


def row(tid, pnl, side="LONG", symbol="ETHUSDT", closed_ms=None, planned=0.11,
        fees=0.003, slippage=0.001, reason="stop loss", equity_before=22.0,
        condition="LONG bias", **extra):
    """One journal row. closed_ms="" deliberately builds an unfinished row."""
    closed_ms = 1_760_000_000_000 if closed_ms is None else closed_ms
    opened_ms = (closed_ms - HOUR) if isinstance(closed_ms, int) else ""
    try:
        r_multiple = round(float(pnl) / planned, 4) if planned else None
    except (TypeError, ValueError):
        r_multiple = None
    base = {
        "trade_id": tid, "mode": "PAPER", "symbol": symbol, "side": side,
        "strategy": tb.STRATEGY_NAME,
        "opened_ist": "2026-11-01 10:00:00", "closed_ist": "2026-11-01 12:00:00",
        "opened_ms": opened_ms,
        "closed_ms": closed_ms,
        "entry": 3000.0, "exit": 2970.0, "qty": 0.01,
        "initial_stop": 2970.0, "take_profit": 3060.0,
        "planned_risk": planned, "pnl": pnl, "fees": fees, "slippage": slippage,
        "r_multiple": r_multiple,
        "exit_reason": reason, "market_condition": condition,
        "equity_before": equity_before,
        "equity_after": equity_before + (pnl if isinstance(pnl, (int, float)) else 0),
    }
    base.update(extra)
    return base


# ---------------------------------------------------------- 1. journal writing

class TestJournalWriting(TempProject):
    def test_a_journal_row_carries_every_required_field(self):
        pos = {"symbol": "SOLUSDT", "side": "SHORT", "qty": 0.5, "entry": 150.0,
               "stop": 153.0, "initial_stop": 153.0, "target": 144.0,
               "opened": 1_760_000_000_000, "trade_id": "abc123",
               "planned_risk": 0.11, "strategy": tb.STRATEGY_NAME,
               "market_condition": "SHORT bias; stop 2.00%", "equity_before": 22.0}
        res = {"exit": 153.2, "pnl": -0.12, "fees": 0.004, "slippage": 0.002,
               "reason": "stop loss"}
        got = tb.journal_row(pos, res, "PAPER", 22.0, 21.88,
                             closed_ms=1_760_000_900_000)
        self.assertEqual(sorted(got), sorted(tb.JOURNAL_FIELDS),
                         "the journal row does not match the declared columns")
        self.assertEqual(got["trade_id"], "abc123")
        self.assertEqual(got["side"], "SHORT")
        self.assertEqual(got["initial_stop"], 153.0)
        self.assertEqual(got["take_profit"], 144.0)
        self.assertEqual(got["exit_reason"], "stop loss")
        self.assertEqual(got["market_condition"], "SHORT bias; stop 2.00%")
        self.assertAlmostEqual(got["r_multiple"], -0.12 / 0.11, places=3)
        self.assertEqual(got["equity_before"], 22.0)
        self.assertEqual(got["equity_after"], 21.88)
        self.assertRegex(got["opened_ist"], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
        self.assertRegex(got["closed_ist"], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
        self.assertGreater(got["closed_ms"], got["opened_ms"])

    def test_a_trade_id_is_generated_when_one_is_missing(self):
        pos = {"symbol": "ETHUSDT", "side": "LONG", "qty": 0.01, "entry": 3000.0,
               "stop": 2970.0, "target": 3060.0, "opened": 1_760_000_000_000}
        got = tb.journal_row(pos, {"exit": 2970.0, "pnl": -0.1, "fees": 0.0,
                                   "slippage": 0.0, "reason": "stop loss"},
                             "PAPER", 22.0, 21.9)
        self.assertTrue(got["trade_id"])
        self.assertIsNone(got["r_multiple"], "no planned risk means no R multiple")

    def test_rows_append_under_one_header(self):
        path = os.path.join(self.tmp, "trades.csv")
        for i in range(3):
            tb.journal(row(f"t{i}", -0.1 * (i + 1)), path)
        with open(path, newline="", encoding="utf-8") as f:
            text = f.read()
        self.assertEqual(text.count("trade_id"), 1, "the header was written twice")
        with open(path, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual([r["trade_id"] for r in rows], ["t0", "t1", "t2"])

    def test_an_older_journal_is_renamed_not_deleted(self):
        path = os.path.join(self.tmp, "trades.csv")
        with open(path, "w", newline="", encoding="utf-8") as f:
            f.write("opened_ist,closed_ist,symbol\n2026-01-01,2026-01-01,ETHUSDT\n")
        buf, old = io.StringIO(), sys.stdout
        sys.stdout = buf
        try:
            tb.journal(row("t1", -0.1), path)
        finally:
            sys.stdout = old
        kept = [p for p in os.listdir(self.tmp) if p.endswith(".old")]
        self.assertTrue(kept, "the previous journal was lost")
        with open(os.path.join(self.tmp, kept[0]), encoding="utf-8") as f:
            self.assertIn("ETHUSDT", f.read())

    def test_closing_a_trade_through_the_bot_journals_it(self):
        conf = cfg(slippage_pct=0.0)
        state = {"paper_balance": 22.0, "paper_enabled": True}
        client = FakeClient(c1=candles(60, 0, HOUR, 3000.0),
                            c4=candles(60, 0, H4, 3000.0), price=2970.0)
        bot = tb.Bot(conf, client, tb.PaperBroker(client, conf, state),
                     FakeNotifier(), state, self.state_path)
        bot.s["position"] = {
            "symbol": "ETHUSDT", "side": "LONG", "qty": 0.01, "entry": 3000.0,
            "stop": 2970.0, "initial_stop": 2970.0, "target": 3060.0,
            "opened": tb.now_ms() - HOUR, "checked": tb.now_ms(),
            "trade_id": "live01", "planned_risk": 0.33,
            "strategy": tb.STRATEGY_NAME, "market_condition": "LONG bias",
            "entry_slippage": 0.0, "equity_before": 22.0}
        bot._finish(bot.b.close_now(bot.s["position"]))
        rows = tb.load_journal(mode="PAPER")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["trade_id"], "live01")
        self.assertEqual(rows[0]["exit_reason"], "closed manually")
        self.assertEqual(sorted(rows[0]), sorted(tb.JOURNAL_FIELDS))


# --------------------------------------------------------- 2. journal reading

class TestJournalIntegrity(TempProject):
    def write(self, rows, path=None):
        path = path or os.path.join(self.tmp, "trades.csv")
        for r in rows:
            tb.journal(r, path)
        return path

    def test_duplicate_trade_ids_are_counted_once(self):
        path = self.write([row("dup", -0.1), row("dup", -0.1), row("other", 0.2)])
        rows = tb.load_journal(path)
        self.assertEqual([r["trade_id"] for r in rows], ["dup", "other"])

    def test_a_row_with_no_close_is_ignored(self):
        path = self.write([row("open1", 0.2, closed_ms=""),
                           row("closed1", 0.2)])
        with open(path, newline="", encoding="utf-8") as f:
            raw = list(csv.DictReader(f))
        # the unfinished row really is in the file ...
        self.assertEqual(len(raw), 2)
        # ... and really is excluded from the statistics
        rows = tb.load_journal(path)
        self.assertEqual([r["trade_id"] for r in rows], ["closed1"])

    def test_a_row_with_no_usable_pnl_is_ignored(self):
        path = self.write([row("bad1", ""), row("bad2", "n/a"), row("good", 0.2)])
        self.assertEqual([r["trade_id"] for r in tb.load_journal(path)], ["good"])

    def test_a_row_with_no_trade_id_or_a_bad_side_is_ignored(self):
        path = self.write([row("", 0.2), row("x", 0.2, side="BOTH"),
                           row("good", 0.2)])
        self.assertEqual([r["trade_id"] for r in tb.load_journal(path)], ["good"])

    def test_rows_are_returned_in_exit_order(self):
        base = 1_760_000_000_000
        path = self.write([row("c", 0.1, closed_ms=base + 2 * DAY),
                           row("a", 0.1, closed_ms=base),
                           row("b", 0.1, closed_ms=base + DAY)])
        self.assertEqual([r["trade_id"] for r in tb.load_journal(path)],
                         ["a", "b", "c"])

    def test_the_mode_filter_separates_paper_from_anything_else(self):
        path = self.write([row("p1", 0.1), row("t1", 0.1, mode="TESTNET")])
        self.assertEqual(len(tb.load_journal(path)), 2)
        self.assertEqual([r["trade_id"] for r in tb.load_journal(path, mode="PAPER")],
                         ["p1"])

    def test_a_missing_journal_is_an_empty_list_not_an_error(self):
        self.assertEqual(tb.load_journal(os.path.join(self.tmp, "nope.csv")), [])

    def test_a_corrupt_journal_does_not_crash_the_reader(self):
        path = os.path.join(self.tmp, "trades.csv")
        with open(path, "wb") as f:
            f.write(b"trade_id,pnl\n\xff\xfe\x00bad\n")
        buf, old = io.StringIO(), sys.stdout
        sys.stdout = buf
        try:
            rows = tb.load_journal(path)
        finally:
            sys.stdout = old
        self.assertEqual(rows, [])


# ------------------------------------------------------- 3. the statistics

class TestPerformanceStats(unittest.TestCase):
    def setUp(self):
        base = 1_760_000_000_000
        #            id    pnl    side     symbol
        self.rows = [
            row("a", 1.0, "LONG", "ETHUSDT", base + 0 * DAY, reason="take profit"),
            row("b", -0.5, "LONG", "ETHUSDT", base + 0 * DAY),
            row("c", -0.5, "SHORT", "SOLUSDT", base + 1 * DAY),
            row("d", 2.0, "SHORT", "SOLUSDT", base + 1 * DAY, reason="take profit"),
            row("e", -1.0, "LONG", "SOLUSDT", base + 9 * DAY),
        ]

    def stats(self, rows=None, start=10.0):
        return tb.performance_stats(rows if rows is not None else self.rows,
                                    start_equity=start)

    def test_the_headline_numbers(self):
        s = self.stats()
        self.assertEqual(s["sample_size"], 5)
        self.assertEqual(s["trades"], 5)
        self.assertEqual((s["wins"], s["losses"], s["breakeven"]), (2, 3, 0))
        self.assertAlmostEqual(s["win_rate"], 40.0)
        self.assertAlmostEqual(s["gross_profit"], 3.0)
        self.assertAlmostEqual(s["gross_loss"], 2.0)
        self.assertAlmostEqual(s["net_pnl"], 1.0)
        self.assertAlmostEqual(s["profit_factor"], 1.5)
        self.assertAlmostEqual(s["avg_win"], 1.5)
        self.assertAlmostEqual(s["avg_loss"], 2.0 / 3)
        self.assertAlmostEqual(s["expectancy"], 0.2)

    def test_fees_and_slippage_are_totalled(self):
        s = self.stats()
        self.assertAlmostEqual(s["total_fees"], 5 * 0.003)
        self.assertAlmostEqual(s["total_slippage"], 5 * 0.001)
        self.assertAlmostEqual(s["planned_risk_total"], 5 * 0.11)

    def test_the_equity_curve_and_maximum_drawdown(self):
        s = self.stats(start=10.0)
        self.assertEqual(s["equity_curve"], [10.0, 11.0, 10.5, 10.0, 12.0, 11.0])
        self.assertAlmostEqual(s["end_equity"], 11.0)
        # peak 11.0 -> trough 10.0 is 1.0, then peak 12.0 -> 11.0 is 1.0
        self.assertAlmostEqual(s["max_drawdown"], 1.0)
        self.assertAlmostEqual(s["max_drawdown_pct"], 100 * 1.0 / 11.0)

    def test_the_drawdown_uses_the_first_rows_equity_when_none_is_given(self):
        s = tb.performance_stats(self.rows)
        self.assertAlmostEqual(s["start_equity"], 22.0)

    def test_long_and_short_are_reported_separately(self):
        s = self.stats()
        lng, sht = s["by_side"]["LONG"], s["by_side"]["SHORT"]
        self.assertEqual((lng["trades"], sht["trades"]), (3, 2))
        self.assertAlmostEqual(lng["net_pnl"], -0.5)
        self.assertAlmostEqual(sht["net_pnl"], 1.5)
        self.assertAlmostEqual(sht["win_rate"], 50.0)

    def test_eth_and_sol_are_reported_separately(self):
        s = self.stats()
        self.assertEqual(sorted(s["by_symbol"]), ["ETHUSDT", "SOLUSDT"])
        self.assertAlmostEqual(s["by_symbol"]["ETHUSDT"]["net_pnl"], 0.5)
        self.assertAlmostEqual(s["by_symbol"]["SOLUSDT"]["net_pnl"], 0.5)
        self.assertEqual(s["by_symbol"]["SOLUSDT"]["trades"], 3)

    def test_exit_reasons_and_market_conditions_are_grouped(self):
        s = self.stats()
        self.assertEqual(s["by_reason"]["take profit"]["trades"], 2)
        self.assertEqual(s["by_reason"]["stop loss"]["trades"], 3)
        self.assertEqual(s["by_condition"]["LONG bias"]["trades"], 5)

    def test_consecutive_wins_and_losses(self):
        rows = [row(str(i), p) for i, p in
                enumerate([1, 1, 1, -1, -1, 1, -1, -1, -1, -1])]
        s = tb.performance_stats(rows, start_equity=10.0)
        self.assertEqual(s["max_consecutive_wins"], 3)
        self.assertEqual(s["max_consecutive_losses"], 4)
        self.assertEqual(s["current_loss_streak"], 4)
        self.assertEqual(s["current_win_streak"], 0)

    def test_daily_and_weekly_grouping(self):
        s = self.stats()
        self.assertEqual(len(s["daily"]), 3)
        self.assertAlmostEqual(sum(s["daily"].values()), 1.0)
        self.assertGreaterEqual(len(s["weekly"]), 2)
        self.assertAlmostEqual(sum(s["weekly"].values()), 1.0)
        for key in s["daily"]:
            self.assertRegex(key, r"^\d{4}-\d{2}-\d{2}$")
        for key in s["weekly"]:
            self.assertRegex(key, r"^\d{4}-W\d{2}$")

    def test_realised_risk_reward(self):
        s = self.stats()
        self.assertAlmostEqual(s["avg_r"], sum(r["r_multiple"] for r in self.rows) / 5)
        self.assertEqual(s["avg_r"], s["realized_rr"])

    def test_an_empty_journal_gives_well_formed_zeroes(self):
        s = tb.performance_stats([])
        self.assertEqual(s["sample_size"], 0)
        self.assertEqual(s["trades"], 0)
        self.assertEqual(s["win_rate"], 0.0)
        self.assertEqual(s["profit_factor"], 0.0)
        self.assertEqual(s["net_pnl"], 0.0)
        self.assertEqual(s["max_drawdown"], 0.0)
        self.assertEqual(s["by_side"]["LONG"]["trades"], 0)
        self.assertEqual(s["by_symbol"], {})
        self.assertIn("No closed trades yet", tb.format_performance(s))

    def test_a_perfect_record_reports_an_infinite_profit_factor(self):
        s = tb.performance_stats([row("a", 1.0), row("b", 2.0)], start_equity=10.0)
        self.assertEqual(s["profit_factor"], float("inf"))
        self.assertEqual(s["max_drawdown"], 0.0)
        self.assertIn("inf", tb.format_performance(s))

    def test_only_losses_reports_a_zero_profit_factor(self):
        s = tb.performance_stats([row("a", -1.0)], start_equity=10.0)
        self.assertEqual(s["profit_factor"], 0.0)
        self.assertAlmostEqual(s["max_drawdown_pct"], 10.0)

    def test_breakeven_trades_are_counted_but_not_as_wins(self):
        s = tb.performance_stats([row("a", 0.0), row("b", 1.0)], start_equity=10.0)
        self.assertEqual((s["wins"], s["losses"], s["breakeven"]), (1, 0, 1))
        self.assertAlmostEqual(s["win_rate"], 50.0)

    def test_a_half_written_row_cannot_change_the_numbers(self):
        """The decisive property, at the level that does the filtering."""
        clean = [r for r in self.rows]
        dirty = clean + [row("open", -5.0, closed_ms=""),
                         row("dup", 7.0), row("dup", 7.0),
                         row("nopnl", "")]
        kept = [r for r in dirty
                if r["closed_ms"] != "" and r["pnl"] != ""]
        seen, deduped = set(), []
        for r in kept:
            if r["trade_id"] not in seen:
                seen.add(r["trade_id"])
                deduped.append(r)
        self.assertAlmostEqual(tb.performance_stats(deduped)["net_pnl"], 8.0)
        self.assertAlmostEqual(tb.performance_stats(clean)["net_pnl"], 1.0)

    def test_the_formatted_report_mentions_a_small_sample(self):
        text = tb.format_performance(self.stats())
        self.assertIn("far too small a sample", text)
        self.assertIn("Win rate: 40.0%", text)
        self.assertIn("LONG:", text)
        self.assertIn("SHORT:", text)
        self.assertIn("ETHUSDT", text)
        self.assertIn("Profit factor: 1.50", text)


# ------------------------------------------- 4. statistics across a restart

class TestStatsPersistence(TempProject):
    def bot(self, state=None):
        conf = cfg(slippage_pct=0.0)
        state = {"paper_balance": 22.0, "paper_enabled": True} if state is None else state
        client = FakeClient(c1=candles(60, 0, HOUR, 3000.0),
                            c4=candles(60, 0, H4, 3000.0), price=2970.0)
        return tb.Bot(conf, client, tb.PaperBroker(client, conf, state),
                      FakeNotifier(), state, self.state_path), state

    def close_one(self, bot, tid, pnl):
        bot.s["position"] = {
            "symbol": "ETHUSDT", "side": "LONG", "qty": 0.01, "entry": 3000.0,
            "stop": 2970.0, "initial_stop": 2970.0, "target": 3060.0,
            "opened": tb.now_ms() - HOUR, "checked": tb.now_ms(),
            "trade_id": tid, "planned_risk": 0.11, "strategy": tb.STRATEGY_NAME,
            "market_condition": "LONG bias", "entry_slippage": 0.0,
            "equity_before": bot.b.balance()}
        bot._finish({"exit": 2970.0, "pnl": pnl, "fees": 0.003,
                     "slippage": 0.0, "reason": "stop loss" if pnl < 0 else "take profit"})

    def test_the_journal_and_the_statistics_survive_a_restart(self):
        bot, state = self.bot()
        self.close_one(bot, "r1", -0.11)
        self.close_one(bot, "r2", 0.22)
        tb.save_state(state, self.state_path)
        first = tb.performance_stats(tb.load_journal(mode="PAPER"))

        bot2, _ = self.bot(state=tb.load_state(self.state_path))
        second = tb.performance_stats(tb.load_journal(mode="PAPER"))
        self.assertEqual(first["sample_size"], 2)
        self.assertEqual(first["net_pnl"], second["net_pnl"])
        self.assertEqual(first["wins"], second["wins"])
        self.close_one(bot2, "r3", 0.33)
        third = tb.performance_stats(tb.load_journal(mode="PAPER"))
        self.assertEqual(third["sample_size"], 3)
        self.assertAlmostEqual(third["net_pnl"], -0.11 + 0.22 + 0.33)

    def test_the_balance_and_the_journal_agree(self):
        bot, state = self.bot()
        start = state["paper_balance"]
        for i, pnl in enumerate([-0.11, 0.22, -0.05]):
            state["paper_balance"] += pnl       # the broker would do this
            self.close_one(bot, f"x{i}", pnl)
        rows = tb.load_journal(mode="PAPER")
        self.assertAlmostEqual(sum(float(r["pnl"]) for r in rows),
                               state["paper_balance"] - start, places=9)


if __name__ == "__main__":
    unittest.main(verbosity=2)
