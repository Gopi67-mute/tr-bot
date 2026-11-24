#!/usr/bin/env python3
"""Test suite for trading_bot.py.

Run with:   python -m unittest -v test_trading_bot

Everything here is offline. The exchange is a fake object serving candles from
memory, Telegram is a fake `requests` module, and no credential, no real symbol
rule and no order ever leaves this file. Several tests assert the *absence* of
order placement, which is the point of the whole exercise.
"""

import csv
import io
import json
import math
import os
import random
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import trading_bot as tb  # noqa: E402

MIN = 60_000
HOUR = 3_600_000
H4 = 4 * HOUR

RULES = {
    "ETHUSDT": {"step": 0.001, "min_qty": 0.001, "min_notional": 5.0, "tick": 0.01},
    "SOLUSDT": {"step": 0.1, "min_qty": 0.1, "min_notional": 5.0, "tick": 0.001},
}


def cfg(**over):
    c = dict(tb.DEFAULTS)
    c.update(api_key="", api_secret="", telegram_token="", telegram_chat_id="")
    c.update(over)
    return c


def recent_t0(n, span):
    """A start time such that candle n-1 has only just closed.

    Live candle data is always a few seconds old, never decades. The engine
    refuses to trade on candles older than two intervals, so a fixture anchored
    at epoch 0 is - correctly - rejected as stale. Passing t0=0 to candles()
    below therefore means "anchor this series to now".
    """
    return (tb.now_ms() // span) * span - n * span


def candles(n, t0, span, px, step=0.0, spread=2.0):
    """A plain synthetic series: close moves by `step` each candle.

    t0=0 anchors the series so that its last candle has just closed.
    """
    t0 = recent_t0(n, span) if t0 == 0 else t0
    out = []
    for i in range(n):
        c = px + step * i
        out.append({"t": t0 + i * span, "o": c - spread / 2, "h": c + spread,
                    "l": c - spread, "c": c, "T": t0 + (i + 1) * span - 1})
    return out


class FakeClient:
    """Stand-in for tb.Binance. Public data only, served from memory.

    Anything that would be a signed call on the real client is simply absent, so
    a test that accidentally exercises an order path fails with AttributeError.
    """
    base = "fake://binance"
    public_only = True

    def __init__(self, c1=None, c4=None, c1m=None, price=3000.0, funding=0.0,
                 rules=None, fail=None):
        self.c1, self.c4, self.c1m = c1 or [], c4 or [], c1m or []
        self._price, self._funding = price, funding
        self._rules = rules or RULES
        self.fail = fail            # exception instance to raise from data calls
        self.kline_calls = []

    def _maybe_fail(self):
        if self.fail:
            raise self.fail

    def sync_time(self):
        self._maybe_fail()

    def rules(self, symbols):
        self._maybe_fail()
        return {s: self._rules[s] for s in symbols if s in self._rules}

    def price(self, symbol):
        self._maybe_fail()
        return self._price

    def funding_pct(self, symbol):
        self._maybe_fail()
        return self._funding

    def klines(self, symbol, interval, limit=500, start=None, end=None):
        self._maybe_fail()
        self.kline_calls.append((symbol, interval, limit, start, end))
        src = {"1m": self.c1m, "1h": self.c1, "4h": self.c4}.get(interval, [])
        if start is not None:
            src = [c for c in src if c["t"] >= start]
        if end is not None:
            src = [c for c in src if c["t"] <= end]
        return src[:limit]

    def klines_range(self, symbol, interval, start, end):
        self._maybe_fail()
        src = {"1m": self.c1m, "1h": self.c1, "4h": self.c4}.get(interval, [])
        return [c for c in src if start <= c["t"] < end]

    def closed_klines(self, symbol, interval, limit=500):
        self._maybe_fail()
        src = {"1m": self.c1m, "1h": self.c1, "4h": self.c4}.get(interval, [])
        return src[-limit:]


class FakeNotifier:
    def __init__(self, queued=None):
        self.sent = []
        self.queued = list(queued or [])

    def send(self, text):
        self.sent.append(text)

    def commands(self):
        out, self.queued = self.queued, []
        return out

    def joined(self):
        return "\n".join(self.sent)

    def last(self):
        return self.sent[-1] if self.sent else ""


class TempProject(unittest.TestCase):
    """Redirects every file the bot writes into a throwaway directory."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="trbot-test-")
        self._here, self._journal = tb.HERE, tb.JOURNAL_FILE
        tb.HERE = self.tmp
        tb.JOURNAL_FILE = os.path.join(self.tmp, "trades.csv")
        self.state_path = os.path.join(self.tmp, "bot_state_paper.json")

    def tearDown(self):
        tb.HERE, tb.JOURNAL_FILE = self._here, self._journal

    def make_bot(self, client, state=None, conf=None, notifier=None, enabled=True):
        """Build a Bot over a throwaway state file.

        `enabled` pre-arms paper trading in the state, because a fresh install
        deliberately starts in standby and almost every test here is about what
        happens once trading is switched on. TestStandby covers the default.
        """
        state = {} if state is None else state
        conf = conf or cfg()
        state.setdefault("paper_enabled", bool(enabled))
        n = notifier or FakeNotifier()
        broker = tb.PaperBroker(client, conf, state)
        bot = tb.Bot(conf, client, broker, n, state, self.state_path)
        return bot, broker, n


# --------------------------------------------------------------- 1. mode safety

class TestModeSafety(TempProject):
    """The single most important property: paper mode cannot reach an order."""

    def _run_main(self, mode_value, with_keys=False, extra=None):
        """Run main() with LiveBroker and the real client replaced by tripwires."""
        conf = {"mode": mode_value, "symbols": ["ETHUSDT"],
                "paper_start_balance": 22.0, "check_every_sec": 60}
        if with_keys:
            conf["api_key"] = "DUMMY_KEY_NOT_REAL"
            conf["api_secret"] = "DUMMY_SECRET_NOT_REAL"
        conf.update(extra or {})
        path = os.path.join(self.tmp, "cfg.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(conf, f)

        tripped = []

        class Tripwire(Exception):
            pass

        def live_broker(*a, **k):
            tripped.append("LiveBroker")
            raise Tripwire("LiveBroker was constructed")

        built = []

        def fake_binance(c, base=None, public_only=False):
            built.append({"base": base, "public_only": public_only})
            if not public_only:
                tripped.append("order-capable client")
                raise Tripwire("order-capable Binance client was constructed")
            return FakeClient(c1=candles(60, 0, HOUR, 3000.0),
                              c4=candles(60, 0, H4, 3000.0))

        old = (tb.LiveBroker, tb.Binance, tb.Notifier)
        tb.LiveBroker, tb.Binance, tb.Notifier = live_broker, fake_binance, \
            lambda c: FakeNotifier()
        argv, stdout = sys.argv, sys.stdout
        sys.argv = ["trading_bot.py", "--config", path, "--once"]
        sys.stdout = io.StringIO()          # keep backtest chatter out of the report
        try:
            tb.main()
            outcome = "completed"
        except Tripwire as e:
            outcome = f"TRIPPED: {e}"
        except SystemExit as e:
            outcome = f"SystemExit: {e}"
        finally:
            sys.argv, sys.stdout = argv, stdout
            tb.LiveBroker, tb.Binance, tb.Notifier = old
        return outcome, tripped, built

    def test_paper_mode_never_creates_a_live_broker(self):
        outcome, tripped, built = self._run_main("paper", with_keys=True)
        self.assertEqual(outcome, "completed", outcome)
        self.assertEqual(tripped, [], f"paper mode reached an order path: {tripped}")
        self.assertTrue(built and built[0]["public_only"],
                        "paper mode must build a read-only client")
        self.assertEqual(built[0]["base"], tb.LIVE_BASE,
                         "paper mode reads public data from the live endpoint")

    def test_backtest_mode_never_creates_a_live_broker(self):
        outcome, tripped, built = self._run_main("backtest", with_keys=True,
                                                 extra={"backtest_days": 1})
        self.assertEqual(tripped, [], f"backtest reached an order path: {tripped}")
        self.assertTrue(built and built[0]["public_only"])

    def test_unknown_modes_are_rejected_and_never_fall_through_to_live(self):
        """The repaired bug: anything but an exact mode used to reach LiveBroker."""
        for bad in ["Paper", "PAPER", "papper", "paper ", "", "sim", "LIVE",
                    None, 1, True, [], "backtest2"]:
            with self.subTest(mode=bad):
                outcome, tripped, _ = self._run_main(bad, with_keys=True)
                self.assertEqual(tripped, [],
                                 f"mode {bad!r} reached an order path: {tripped}")
                self.assertTrue(outcome.startswith("SystemExit"),
                                f"mode {bad!r} was not rejected: {outcome}")
                self.assertIn("mode must be exactly one of", outcome)

    def test_live_mode_still_requires_keys_and_explicit_consent(self):
        outcome, tripped, _ = self._run_main("live", with_keys=False)
        self.assertIn("needs api_key and api_secret", outcome)
        self.assertEqual(tripped, [])
        # real money needs the extra flag even with keys present
        outcome, tripped, _ = self._run_main(
            "live", with_keys=True, extra={"use_testnet": False})
        self.assertIn("i_understand_real_money", outcome)
        self.assertEqual(tripped, [], "real-money guard let a client through")

    def test_read_only_client_refuses_signed_and_non_get_requests(self):
        c = tb.Binance(cfg(api_key="K" * 20, api_secret="S" * 20),
                       base=tb.LIVE_BASE, public_only=True)
        self.assertEqual(c.key, "", "a read-only client must not hold the api key")
        self.assertEqual(c.secret, b"", "a read-only client must not hold the secret")
        for method, path, signed in [("POST", "/fapi/v1/order", True),
                                     ("POST", "/fapi/v1/order", False),
                                     ("GET", "/fapi/v3/balance", True),
                                     ("DELETE", "/fapi/v1/algoOpenOrders", True),
                                     ("POST", "/fapi/v1/algoOrder", True),
                                     ("POST", "/fapi/v1/leverage", True)]:
            with self.subTest(method=method, path=path, signed=signed):
                with self.assertRaises(tb.ReadOnlyClient):
                    c._req(method, path, signed=signed)

    def test_paper_broker_has_no_order_methods_at_all(self):
        broker = tb.PaperBroker(FakeClient(), cfg(), {})
        for attr in ["market", "close_all_order", "set_leverage", "ensure_isolated",
                     "cancel_algo_orders"]:
            self.assertFalse(hasattr(broker, attr),
                             f"PaperBroker exposes {attr}")

    def test_validate_config_rejects_unsafe_numbers(self):
        for bad in [{"risk_per_trade_pct": 0}, {"risk_per_trade_pct": -1},
                    {"risk_per_trade_pct": 5.0},          # above the hard cap
                    {"risk_per_trade_pct": 3.0, "max_daily_loss_pct": 2.0},
                    {"max_daily_loss_pct": 0}, {"max_daily_loss_pct": 101},
                    {"max_daily_loss_pct": 6.0, "max_account_drawdown_pct": 5.0},
                    {"max_account_drawdown_pct": 0},
                    {"max_open_positions": 2}, {"max_open_positions": 0},
                    {"max_trades_per_day": 0}, {"loss_cooldown_min": -1},
                    {"slippage_pct": -0.1}, {"command_poll_sec": 0},
                    {"paper_trading_enabled": "yes"},
                    {"paper_fractional_qty": 1},
                    {"reset_confirm_minutes": 0},
                    {"oos_fraction": 0}, {"oos_fraction": 0.95},
                    {"walk_forward_folds": 1},
                    {"max_leverage": 0}, {"reward_risk": 0},
                    {"max_stop_pct": 0.2, "min_stop_pct": 0.4},
                    {"monthly_drawdown_stop_pct": 0}, {"max_losses_per_day": 0},
                    {"symbols": []}, {"symbols": "ETHUSDT"},
                    {"paper_start_balance": 0}, {"check_every_sec": 1},
                    {"fast_ema": 50, "slow_ema": 20},
                    {"taker_fee_pct": "free"}]:
            with self.subTest(**bad):
                with self.assertRaises(SystemExit):
                    tb.validate_config(cfg(**bad))

    def test_a_config_using_the_removed_bump_key_is_refused(self):
        """Silently ignoring it would leave the user trusting a dead limit."""
        path = os.path.join(self.tmp, "old.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"mode": "paper", "max_risk_pct_when_bumped": 3.0}, f)
        with self.assertRaises(SystemExit) as ctx:
            tb.load_config(path)
        self.assertIn("max_risk_pct_when_bumped", str(ctx.exception))
        self.assertIn("no longer exist", str(ctx.exception))

    def test_validate_config_accepts_the_shipped_defaults(self):
        conf = cfg()
        self.assertIs(tb.validate_config(conf), conf)   # passes through unchanged
        for mode in tb.MODES:
            with self.subTest(mode=mode):
                self.assertIs(tb.validate_config(cfg(mode=mode)) is None, False)

    def test_validate_config_accepts_the_users_actual_config_file(self):
        """The shipped config.json must itself be valid."""
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
        if not os.path.exists(path):
            self.skipTest("no config.json in the project")
        tb.validate_config(tb.load_config(path))


# --------------------------------------------------------- 2. paper simulation

class TestPaperFills(TempProject):
    def setUp(self):
        super().setUp()
        # slippage is exercised in test_risk_controls.py; these tests assert
        # exact fill arithmetic, so it is switched off here deliberately.
        self.conf = cfg(reward_risk=2.0, taker_fee_pct=0.05, slippage_pct=0.0)
        self.m0 = 1_700_000_000_000 - 1_700_000_000_000 % MIN  # a minute boundary

    def pos(self, side="LONG", entry=3000.0, stop=2950.0, qty=0.01, offset_ms=0):
        opened = self.m0 + offset_ms
        return {"symbol": "ETHUSDT", "side": side, "qty": qty, "entry": entry,
                "stop": stop, "target": tb.target_for(side, entry, stop, 2.0),
                "opened": opened, "checked": opened}

    def broker(self, c1m, state=None):
        state = {} if state is None else state
        return tb.PaperBroker(FakeClient(c1m=c1m), self.conf, state), state

    def test_entry_price_and_target_are_set_from_the_fill(self):
        b, _ = self.broker([])
        p = b.open("ETHUSDT", "LONG", 0.01, 2950.0, 3000.0, RULES["ETHUSDT"])
        self.assertEqual(p["entry"], 3000.0)
        self.assertEqual(p["stop"], 2950.0)
        self.assertAlmostEqual(p["target"], 3100.0)   # 2 x the 50-wide risk
        p = b.open("ETHUSDT", "SHORT", 0.01, 3050.0, 3000.0, RULES["ETHUSDT"])
        self.assertAlmostEqual(p["target"], 2900.0)

    def test_open_position_is_left_alone_when_nothing_is_hit(self):
        c = [{"t": self.m0 + MIN, "o": 3000.0, "h": 3010.0, "l": 2990.0,
              "c": 3005.0, "T": self.m0 + 2 * MIN - 1}]
        b, st = self.broker(c)
        self.assertIsNone(b.check(self.pos()))
        self.assertEqual(st["paper_balance"], self.conf["paper_start_balance"])

    def test_long_stop_loss(self):
        c = [{"t": self.m0 + MIN, "o": 3000.0, "h": 3002.0, "l": 2940.0,
              "c": 2960.0, "T": self.m0 + 2 * MIN - 1}]
        b, st = self.broker(c)
        res = b.check(self.pos())
        self.assertEqual(res["reason"], "stop loss")
        self.assertEqual(res["exit"], 2950.0)
        expect_fee = (3000.0 + 2950.0) * 0.01 * 0.0005
        self.assertAlmostEqual(res["fees"], expect_fee)
        self.assertAlmostEqual(res["pnl"], -0.5 - expect_fee)
        self.assertAlmostEqual(st["paper_balance"], 22.0 - 0.5 - expect_fee)

    def test_long_take_profit(self):
        c = [{"t": self.m0 + MIN, "o": 3000.0, "h": 3110.0, "l": 2999.0,
              "c": 3105.0, "T": self.m0 + 2 * MIN - 1}]
        b, st = self.broker(c)
        res = b.check(self.pos())
        self.assertEqual(res["reason"], "take profit")
        self.assertEqual(res["exit"], 3100.0)
        self.assertAlmostEqual(res["pnl"], 1.0 - (3000.0 + 3100.0) * 0.01 * 0.0005)
        self.assertGreater(st["paper_balance"], 22.0)

    def test_short_stop_loss_and_take_profit(self):
        up = [{"t": self.m0 + MIN, "o": 3000.0, "h": 3060.0, "l": 2995.0,
               "c": 3055.0, "T": self.m0 + 2 * MIN - 1}]
        b, _ = self.broker(up)
        res = b.check(self.pos("SHORT", 3000.0, 3050.0))
        self.assertEqual((res["reason"], res["exit"]), ("stop loss", 3050.0))
        down = [{"t": self.m0 + MIN, "o": 3000.0, "h": 3001.0, "l": 2890.0,
                 "c": 2895.0, "T": self.m0 + 2 * MIN - 1}]
        b, _ = self.broker(down)
        res = b.check(self.pos("SHORT", 3000.0, 3050.0))
        self.assertEqual((res["reason"], res["exit"]), ("take profit", 2900.0))

    def test_stop_wins_when_one_candle_touches_both(self):
        c = [{"t": self.m0 + MIN, "o": 3000.0, "h": 3150.0, "l": 2900.0,
              "c": 3050.0, "T": self.m0 + 2 * MIN - 1}]
        b, _ = self.broker(c)
        for side, stop in [("LONG", 2950.0), ("SHORT", 3050.0)]:
            with self.subTest(side=side):
                res = b.check(self.pos(side, 3000.0, stop))
                self.assertEqual(res["reason"], "stop loss")
                self.assertLess(res["pnl"], 0)

    def test_gap_through_the_stop_fills_at_the_open_not_the_stop(self):
        c = [{"t": self.m0 + MIN, "o": 2900.0, "h": 2905.0, "l": 2880.0,
              "c": 2890.0, "T": self.m0 + 2 * MIN - 1}]
        b, _ = self.broker(c)
        res = b.check(self.pos())
        self.assertEqual(res["exit"], 2900.0, "a gapped stop must not fill at the stop")
        self.assertLess(res["pnl"], -0.5)
        # a favourable gap through the target is NOT credited
        c = [{"t": self.m0 + MIN, "o": 3200.0, "h": 3250.0, "l": 3190.0,
              "c": 3240.0, "T": self.m0 + 2 * MIN - 1}]
        b, _ = self.broker(c)
        self.assertEqual(b.check(self.pos())["exit"], 3100.0)

    # ------------------------------------------------ the entry-minute question

    def test_pre_entry_wick_in_the_entry_minute_does_not_trigger_a_stop(self):
        """The repaired bug: entering at :45 was judged on the whole :00 candle."""
        entry_minute = {"t": self.m0, "o": 3000.0, "h": 3010.0, "l": 2900.0,
                        "c": 3005.0, "T": self.m0 + MIN - 1}
        b, st = self.broker([entry_minute])
        res = b.check(self.pos(offset_ms=45_000))
        self.assertIsNone(res, "stopped out on price action from before the entry")
        self.assertEqual(st["paper_balance"], 22.0)

    def test_pre_entry_wick_in_the_entry_minute_does_not_trigger_a_target(self):
        entry_minute = {"t": self.m0, "o": 3000.0, "h": 3200.0, "l": 2995.0,
                        "c": 3005.0, "T": self.m0 + MIN - 1}
        b, st = self.broker([entry_minute])
        self.assertIsNone(b.check(self.pos(offset_ms=30_000)))
        self.assertEqual(st["paper_balance"], 22.0)

    def test_entry_exactly_on_the_minute_boundary_uses_that_whole_candle(self):
        """Then the candle really is post-entry, so it must count."""
        entry_minute = {"t": self.m0, "o": 3000.0, "h": 3010.0, "l": 2900.0,
                        "c": 3005.0, "T": self.m0 + MIN - 1}
        b, _ = self.broker([entry_minute])
        res = b.check(self.pos(offset_ms=0))
        self.assertIsNotNone(res, "a boundary entry must evaluate its own candle")
        self.assertEqual(res["reason"], "stop loss")

    def test_the_minute_after_entry_is_always_evaluated(self):
        series = [{"t": self.m0, "o": 3000.0, "h": 3010.0, "l": 2900.0,
                   "c": 3005.0, "T": self.m0 + MIN - 1},
                  {"t": self.m0 + MIN, "o": 3005.0, "h": 3008.0, "l": 2945.0,
                   "c": 2950.0, "T": self.m0 + 2 * MIN - 1}]
        b, _ = self.broker(series)
        res = b.check(self.pos(offset_ms=45_000))
        self.assertEqual(res["reason"], "stop loss")
        self.assertEqual(res["exit"], 2950.0)

    def test_first_post_entry_minute_boundary_maths(self):
        self.assertEqual(tb.PaperBroker.first_post_entry_minute(self.m0), self.m0)
        self.assertEqual(tb.PaperBroker.first_post_entry_minute(self.m0 + 1),
                         self.m0 + MIN)
        self.assertEqual(tb.PaperBroker.first_post_entry_minute(self.m0 + 59_999),
                         self.m0 + MIN)
        self.assertEqual(tb.PaperBroker.first_post_entry_minute(self.m0 + MIN),
                         self.m0 + MIN)

    def test_long_outage_pages_past_the_1500_candle_limit(self):
        """A >25h gap used to be skipped entirely, missing the stop inside it."""
        n = 3200
        series = candles(n, self.m0, MIN, 3000.0, step=0.0, spread=1.0)
        series[2000].update(l=2940.0, o=2995.0)          # stop hit deep in the gap
        client = FakeClient(c1m=series)
        b = tb.PaperBroker(client, self.conf, {})
        res = b.check(self.pos(offset_ms=0))
        self.assertIsNotNone(res, "the stop inside the outage was missed")
        self.assertEqual(res["reason"], "stop loss")
        self.assertTrue(any(call[3] is not None for call in client.kline_calls),
                        "candles must be fetched with an explicit startTime")

    def test_manual_close_uses_the_live_price(self):
        client = FakeClient(c1m=[], price=3040.0)
        b = tb.PaperBroker(client, self.conf, {})
        res = b.close_now(self.pos())
        self.assertEqual(res["reason"], "closed manually")
        self.assertEqual(res["exit"], 3040.0)
        self.assertAlmostEqual(res["pnl"],
                               0.4 - (3000.0 + 3040.0) * 0.01 * 0.0005)

    def test_paper_balance_starts_from_the_configured_amount(self):
        for amount in (22.0, 100.0, 7.5):
            st = {}
            tb.PaperBroker(FakeClient(), cfg(paper_start_balance=amount), st)
            self.assertEqual(st["paper_balance"], amount)
        # an existing balance is never overwritten by the config default
        st = {"paper_balance": 19.3}
        tb.PaperBroker(FakeClient(), cfg(paper_start_balance=22.0), st)
        self.assertEqual(st["paper_balance"], 19.3)


# ------------------------------------------------------- 3. fees and sizing

class TestFeesAndSizing(unittest.TestCase):
    def test_trade_pnl_charges_both_sides(self):
        pnl, fees = tb.trade_pnl("LONG", 100.0, 110.0, 2.0, 0.05)
        self.assertAlmostEqual(fees, (100 + 110) * 2 * 0.0005)
        self.assertAlmostEqual(pnl, 20.0 - fees)
        pnl, fees = tb.trade_pnl("SHORT", 100.0, 90.0, 2.0, 0.05)
        self.assertAlmostEqual(pnl, 20.0 - fees)
        pnl, fees = tb.trade_pnl("LONG", 100.0, 100.0, 2.0, 0.05)
        self.assertAlmostEqual(pnl, -fees, msg="a flat round trip still costs fees")

    def test_zero_fee_config_costs_nothing(self):
        pnl, fees = tb.trade_pnl("LONG", 100.0, 110.0, 2.0, 0.0)
        self.assertEqual((pnl, fees), (20.0, 0.0))

    def test_cost_per_unit_includes_the_round_trip(self):
        self.assertAlmostEqual(tb.cost_per_unit(3000.0, 2950.0, 0.05),
                               50.0 + (3000 + 2950) * 0.0005)
        self.assertAlmostEqual(tb.cost_per_unit(3000.0, 2950.0, 0.0), 50.0)
        # slippage is budgeted on both legs as well
        self.assertAlmostEqual(tb.cost_per_unit(3000.0, 2950.0, 0.05, 0.03),
                               50.0 + (3000 + 2950) * 0.0005 + (3000 + 2950) * 0.0003)

    def test_real_loss_at_the_stop_stays_inside_the_risk_budget(self):
        """Sizing budgets the real loss: price move + fees + slippage."""
        conf = cfg(risk_per_trade_pct=0.5, taker_fee_pct=0.05, slippage_pct=0.03)
        for entry, stop_pct in [(3000.0, 0.5), (3000.0, 1.0), (3000.0, 3.0),
                                (150.0, 0.5), (150.0, 2.0)]:
            for sym in RULES:
                for fractional in (False, True):
                    with self.subTest(entry=entry, stop_pct=stop_pct, sym=sym,
                                      fractional=fractional):
                        stop = entry * (1 - stop_pct / 100)
                        qty, risk, note = tb.size_position(
                            22.0, entry, stop, RULES[sym], conf, fractional=fractional)
                        if qty == 0:
                            continue
                        loss = (qty * abs(entry - stop)
                                + qty * (entry + stop) * 0.0005
                                + qty * (entry + stop) * 0.0003)
                        cap = conf["risk_per_trade_pct"] / 100 * 22.0
                        self.assertLessEqual(loss, cap + 1e-9,
                                             f"loss {loss:.4f} exceeds budget {cap:.4f}")
                        self.assertAlmostEqual(risk, loss, places=9,
                                               msg="reported risk must be the real loss")

    def test_risk_scales_with_the_configured_percentage(self):
        """Doubling risk_per_trade_pct doubles the planned risk.

        Equity is 100 here, not 1000: at 1000 both percentages would be
        clipped by the 1.00 USDT absolute ceiling and the percentage rule
        this test exists to check would be invisible. The clipping itself is
        covered by test_the_absolute_ceiling_overrides_a_looser_percentage.
        """
        conf1 = cfg(risk_per_trade_pct=0.5)
        conf2 = cfg(risk_per_trade_pct=1.0)
        q1, r1, _ = tb.size_position(100.0, 3000.0, 2970.0, RULES["ETHUSDT"], conf1)
        q2, r2, _ = tb.size_position(100.0, 3000.0, 2970.0, RULES["ETHUSDT"], conf2)
        self.assertAlmostEqual(r2 / r1, 2.0, places=1)
        self.assertGreater(q2, q1)

    def test_quantity_respects_the_step_size(self):
        conf = cfg(risk_per_trade_pct=1.5)
        for sym, entry, stop in [("SOLUSDT", 150.0, 147.0),
                                 ("ETHUSDT", 3000.0, 2940.0)]:
            with self.subTest(sym=sym):
                step = RULES[sym]["step"]
                qty, _, _ = tb.size_position(1000.0, entry, stop, RULES[sym], conf)
                self.assertGreater(qty, 0)
                # a valid quantity is unchanged by snapping it to the step again
                self.assertEqual(tb.floor_step(qty, step), qty)

    def test_step_rounding_helpers(self):
        self.assertEqual(tb.floor_step(1.2345, 0.001), 1.234)
        self.assertEqual(tb.ceil_step(1.2341, 0.001), 1.235)
        self.assertEqual(tb.floor_step(1.75, 0.1), 1.7)
        self.assertEqual(tb.ceil_step(1.71, 0.1), 1.8)
        self.assertEqual(tb.fmt_step(1.7, 0.001), "1.700")
        self.assertEqual(tb.fmt_step(1.7, 0.1), "1.7")

    def test_notional_cap_respects_max_leverage(self):
        conf = cfg(risk_per_trade_pct=2.0, max_leverage=5)
        qty, _, _ = tb.size_position(22.0, 3000.0, 2999.0, RULES["ETHUSDT"], conf,
                                     fractional=True)
        self.assertLessEqual(qty * 3000.0, 22.0 * 5 * 0.9 + 1e-9)

    def test_an_exchange_minimum_is_rejected_never_bumped(self):
        """The removed unsafe behaviour: size used to be raised to the minimum.

        A minimum that would cost more than the risk cap must reject the trade,
        not quietly risk more of the account.
        """
        conf = cfg(risk_per_trade_pct=0.5, taker_fee_pct=0.05, slippage_pct=0.03)
        rules = {"step": 0.001, "min_qty": 0.001, "min_notional": 20.0, "tick": 0.01}
        qty, risk, note = tb.size_position(22.0, 3000.0, 2970.0, rules, conf)
        self.assertEqual((qty, risk), (0.0, 0.0), f"a trade was sized anyway: {note}")
        self.assertIn("exchange minimum", note)
        self.assertIn("never raised", note)

    def test_the_bump_wording_is_gone_from_the_source(self):
        """Nothing may raise a quantity to reach a minimum, anywhere."""
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "trading_bot.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertNotIn("raised to exchange minimum", src)
        self.assertNotIn("max_risk_pct_when_bumped\"]", src)

    def test_a_minimum_that_breaks_the_cap_is_rejected_with_the_numbers(self):
        conf = cfg(risk_per_trade_pct=0.5)
        rules = {"step": 0.001, "min_qty": 0.001, "min_notional": 100.0, "tick": 0.01}
        qty, risk, note = tb.size_position(22.0, 3000.0, 2910.0, rules, conf)
        self.assertEqual((qty, risk), (0.0, 0.0))
        self.assertIn("rejected", note)
        self.assertIn("% of equity", note)

    def test_risk_never_exceeds_the_cap_across_a_sweep(self):
        """Whatever the rules, the planned risk stays inside the configured cap."""
        tight = {"step": 0.001, "min_qty": 0.001, "min_notional": 20.0, "tick": 0.01}
        for rules in (RULES["ETHUSDT"], RULES["SOLUSDT"], tight):
            for cap in (0.25, 0.5, 1.0, 2.0):
                conf = cfg(risk_per_trade_pct=cap)
                for stop_pct in [0.4, 0.8, 1.5, 2.0, 2.5, 3.0]:
                    for fractional in (False, True):
                        entry, bal = 3000.0, 22.0
                        stop = entry * (1 - stop_pct / 100)
                        qty, risk, _ = tb.size_position(bal, entry, stop, rules, conf,
                                                        fractional=fractional)
                        if qty == 0:
                            continue
                        with self.subTest(cap=cap, stop_pct=stop_pct,
                                          fractional=fractional):
                            self.assertLessEqual(risk / bal * 100, cap + 1e-9)

    def test_bad_inputs_are_refused(self):
        conf = cfg()
        self.assertEqual(tb.size_position(22.0, 3000.0, 3000.0, RULES["ETHUSDT"], conf)[0], 0)
        self.assertEqual(tb.size_position(0.0, 3000.0, 2950.0, RULES["ETHUSDT"], conf)[0], 0)
        self.assertEqual(tb.size_position(-5.0, 3000.0, 2950.0, RULES["ETHUSDT"], conf)[0], 0)

    def test_stop_validation_rules(self):
        conf = cfg(min_stop_pct=0.4, max_stop_pct=3.0)
        self.assertIsNone(tb.check_stop("LONG", 3000.0, 2970.0, conf))
        self.assertIn("already past", tb.check_stop("LONG", 3000.0, 3010.0, conf))
        self.assertIn("already past", tb.check_stop("SHORT", 3000.0, 2990.0, conf))
        self.assertIn("too tight", tb.check_stop("LONG", 3000.0, 2997.0, conf))
        self.assertIn("too wide", tb.check_stop("LONG", 3000.0, 2800.0, conf))

    def test_stop_fill_price_direction(self):
        self.assertEqual(tb.stop_fill_price("LONG", 2950.0, 2900.0), 2900.0)
        self.assertEqual(tb.stop_fill_price("LONG", 2950.0, 2990.0), 2950.0)
        self.assertEqual(tb.stop_fill_price("SHORT", 3050.0, 3100.0), 3100.0)
        self.assertEqual(tb.stop_fill_price("SHORT", 3050.0, 3010.0), 3050.0)


# ------------------------------------------------------- 4. circuit breakers

class TestCircuitBreakers(TempProject):
    def test_daily_loss_limit_stops_trading(self):
        bot, _, _ = self.make_bot(FakeClient(), conf=cfg(max_losses_per_day=2))
        bot.s["losses_today"] = 1
        self.assertIsNone(bot.blocked())
        bot.s["losses_today"] = 2
        self.assertIn("done for the day", bot.blocked())

    def test_losing_trades_count_towards_the_daily_limit(self):
        conf = cfg(max_losses_per_day=2, taker_fee_pct=0.05)
        bot, broker, n = self.make_bot(FakeClient(price=3000.0), conf=conf)
        for _ in range(2):
            bot.s["position"] = {"symbol": "ETHUSDT", "side": "LONG", "qty": 0.01,
                                 "entry": 3000.0, "stop": 2950.0, "target": 3100.0,
                                 "opened": tb.now_ms(), "checked": tb.now_ms()}
            bot._finish({"exit": 2950.0, "pnl": -0.53, "fees": 0.03,
                         "reason": "stop loss"})
        self.assertEqual(bot.s["losses_today"], 2)
        self.assertIsNone(bot.s["position"])
        self.assertIn("done for the day", bot.blocked())

    def test_winning_trades_do_not_count_as_losses(self):
        bot, _, _ = self.make_bot(FakeClient(price=3000.0))
        bot.s["position"] = {"symbol": "ETHUSDT", "side": "LONG", "qty": 0.01,
                             "entry": 3000.0, "stop": 2950.0, "target": 3100.0,
                             "opened": tb.now_ms(), "checked": tb.now_ms()}
        bot._finish({"exit": 3100.0, "pnl": 0.97, "fees": 0.03, "reason": "take profit"})
        self.assertEqual(bot.s["losses_today"], 0)

    def test_monthly_drawdown_stops_trading(self):
        # the account-drawdown limit is relaxed here so the monthly gate is
        # what is being measured; it has its own tests in test_risk_controls.
        conf = cfg(monthly_drawdown_stop_pct=6.0, max_account_drawdown_pct=90.0,
                   max_daily_loss_pct=90.0)
        state = {"paper_balance": 22.0}
        bot, broker, _ = self.make_bot(FakeClient(), state=state, conf=conf)
        bot.s["month_start"] = 22.0
        state["paper_balance"] = 20.75           # -5.7%, still inside
        self.assertIsNone(bot.blocked())
        state["paper_balance"] = 20.68           # -6.0%, at the floor
        self.assertIn("this month", bot.blocked())
        state["paper_balance"] = 15.00
        self.assertIn("this month", bot.blocked())

    def test_pause_and_resume_block_and_unblock(self):
        bot, _, n = self.make_bot(FakeClient(), notifier=FakeNotifier(["/pause"]))
        bot.handle_commands()
        self.assertTrue(bot.s["paused"])
        self.assertFalse(bot.s["paper_enabled"])
        self.assertIn("paper trading is OFF", bot.blocked())
        bot.n.queued = ["/resume"]
        bot.handle_commands()
        self.assertFalse(bot.s["paused"])
        self.assertTrue(bot.s["paper_enabled"])
        self.assertIsNone(bot.blocked())

    def test_day_and_month_rollover_reset_the_counters(self):
        bot, _, _ = self.make_bot(FakeClient())
        bot.s.update(day="1999-01-01", month="1999-01", losses_today=2,
                     month_start=50.0)
        bot._roll_periods()
        self.assertEqual(bot.s["losses_today"], 0)
        self.assertEqual(bot.s["day"], tb.day_key())
        self.assertEqual(bot.s["month_start"], 22.0)

    def test_state_missing_period_fields_does_not_crash(self):
        """An older or hand-edited state file used to KeyError in blocked()."""
        for broken in [{"paper_balance": 22.0, "day": tb.day_key()},
                       {"paper_balance": 22.0, "month": tb.month_key()},
                       {"paper_balance": 22.0, "day": tb.day_key(),
                        "month": tb.month_key()},
                       {"paper_balance": 22.0}]:
            with self.subTest(state=sorted(broken)):
                bot, _, _ = self.make_bot(FakeClient(), state=dict(broken))
                self.assertIsNone(bot.blocked())
                self.assertIn("Status", bot.status_text())

    def test_manual_close_command_closes_the_open_position(self):
        conf = cfg(taker_fee_pct=0.05)
        state = {"paper_balance": 22.0}
        bot, broker, n = self.make_bot(FakeClient(price=3040.0), state=state,
                                       conf=conf, notifier=FakeNotifier(["/close"]))
        bot.s["position"] = {"symbol": "ETHUSDT", "side": "LONG", "qty": 0.01,
                             "entry": 3000.0, "stop": 2950.0, "target": 3100.0,
                             "opened": tb.now_ms(), "checked": tb.now_ms()}
        bot.handle_commands()
        self.assertIsNone(bot.s["position"])
        self.assertGreater(state["paper_balance"], 22.0)
        self.assertIn("closed manually", n.joined())

    def test_close_command_with_no_position_says_so(self):
        bot, _, n = self.make_bot(FakeClient(), notifier=FakeNotifier(["/close"]))
        bot.handle_commands()
        self.assertIn("No open position", n.joined())

    def test_status_command_reports_without_crashing(self):
        client = FakeClient(c1=candles(60, 0, HOUR, 3000.0),
                            c4=candles(60, 0, H4, 3000.0), price=3000.0)
        bot, _, n = self.make_bot(client, notifier=FakeNotifier(["/status"]))
        bot.handle_commands()
        text = n.joined()
        self.assertIn("Balance 22.00 USDT", text)
        self.assertIn("trades 0/3", text)
        self.assertIn("No open position", text)

    def test_unknown_command_gets_the_help_text(self):
        bot, _, n = self.make_bot(FakeClient(), notifier=FakeNotifier(["/wipe"]))
        bot.handle_commands()
        self.assertIn("/status", n.joined())
        self.assertIn("Unknown command", n.joined())


# ---------------------------------------------------- 5. state and journal

class TestPersistence(TempProject):
    def test_state_survives_a_restart(self):
        state = {"paper_balance": 20.4, "losses_today": 1, "trades_today": 2,
                 "last_candle": {"ETHUSDT": 1234}, "paused": True,
                 "paper_enabled": True, "equity_peak": 23.5,
                 "day": tb.day_key(), "day_start_equity": 21.0,
                 "month": tb.month_key(), "month_start": 22.0,
                 "position": {"symbol": "ETHUSDT", "side": "LONG", "qty": 0.01,
                              "entry": 3000.0, "stop": 2950.0, "target": 3100.0,
                              "opened": 1_700_000_000_000, "checked": 1_700_000_000_000}}
        tb.save_state(state, self.state_path)
        back = tb.load_state(self.state_path)
        self.assertEqual(back, state)
        bot, broker, _ = self.make_bot(FakeClient(), state=back)
        self.assertEqual(broker.balance(), 20.4)
        self.assertEqual(bot.s["losses_today"], 1)
        self.assertEqual(bot.s["trades_today"], 2, "the day's trade count was reset")
        self.assertEqual(bot.s["day_start_equity"], 21.0,
                         "a restart must not re-baseline the day's equity")
        self.assertEqual(bot.s["equity_peak"], 23.5, "the equity peak was lost")
        self.assertTrue(bot.s["paused"])
        self.assertEqual(bot.s["position"]["entry"], 3000.0)
        self.assertEqual(bot.s["last_candle"]["ETHUSDT"], 1234)

    def test_restart_does_not_retake_the_same_candle(self):
        """The last processed candle is remembered, so no duplicate entry."""
        c1 = candles(60, 0, HOUR, 3000.0)
        client = FakeClient(c1=c1, c4=candles(60, 0, H4, 3000.0), price=3000.0)
        state = {"paper_balance": 22.0, "last_candle": {"ETHUSDT": c1[-1]["t"],
                                                        "SOLUSDT": c1[-1]["t"]}}
        tb.save_state(state, self.state_path)
        bot, _, n = self.make_bot(client, state=tb.load_state(self.state_path))
        bot.look_for_entry()
        self.assertIsNone(bot.s["position"])
        self.assertNotIn("Opened", n.joined())

    def test_corrupt_state_file_is_set_aside_not_fatal(self):
        for junk in ["{not json", "", "[1,2,3]", '"a string"', "null"]:
            with self.subTest(junk=junk[:12]):
                with open(self.state_path, "w", encoding="utf-8") as f:
                    f.write(junk)
                buf = io.StringIO()
                old = sys.stdout
                sys.stdout = buf
                try:
                    state = tb.load_state(self.state_path)
                finally:
                    sys.stdout = old
                self.assertEqual(state, {})
                if junk:
                    self.assertIn("could not be read", buf.getvalue())
                    self.assertTrue([p for p in os.listdir(self.tmp)
                                     if ".broken-" in p], "no .broken- copy kept")
                for p in os.listdir(self.tmp):
                    if ".broken-" in p:
                        os.unlink(os.path.join(self.tmp, p))

    def test_missing_state_file_starts_clean(self):
        self.assertEqual(tb.load_state(os.path.join(self.tmp, "nope.json")), {})

    def test_save_state_is_atomic_and_leaves_no_temp_file(self):
        tb.save_state({"a": 1}, self.state_path)
        self.assertFalse(os.path.exists(self.state_path + ".tmp"))
        with open(self.state_path, encoding="utf-8") as f:
            self.assertEqual(json.load(f), {"a": 1})

    def test_journal_writes_a_header_once_and_appends_rows(self):
        path = os.path.join(self.tmp, "trades.csv")
        row = {"symbol": "ETHUSDT", "side": "LONG", "pnl_usdt": -0.53}
        tb.journal(dict(row), path)
        tb.journal(dict(row, pnl_usdt=0.97), path)
        with open(path, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["pnl_usdt"], "-0.53")
        self.assertEqual(rows[1]["pnl_usdt"], "0.97")

    def test_journal_sets_aside_a_mismatched_header(self):
        path = os.path.join(self.tmp, "trades.csv")
        with open(path, "w", newline="", encoding="utf-8") as f:
            f.write("old,header\n1,2\n")
        buf, old = io.StringIO(), sys.stdout
        sys.stdout = buf
        try:
            tb.journal({"symbol": "ETHUSDT", "pnl_usdt": 1.0}, path)
        finally:
            sys.stdout = old
        with open(path, newline="", encoding="utf-8") as f:
            self.assertEqual(next(csv.reader(f)), ["symbol", "pnl_usdt"])
        self.assertTrue([p for p in os.listdir(self.tmp) if p.endswith(".old")])

    def test_finish_books_the_trade_even_if_notifying_fails(self):
        """The repaired bug: a failed send left the position open to be closed twice."""
        class Exploding(FakeNotifier):
            def send(self, text):
                raise RuntimeError("telegram is on fire")

        state = {"paper_balance": 21.47}
        bot, _, _ = self.make_bot(FakeClient(price=3000.0), state=state,
                                  notifier=Exploding())
        bot.s["position"] = {"symbol": "ETHUSDT", "side": "LONG", "qty": 0.01,
                             "entry": 3000.0, "stop": 2950.0, "target": 3100.0,
                             "opened": tb.now_ms(), "checked": tb.now_ms()}
        with self.assertRaises(RuntimeError):
            bot._finish({"exit": 2950.0, "pnl": -0.53, "fees": 0.03,
                         "reason": "stop loss"})
        self.assertIsNone(bot.s["position"], "position left open after a failed send")
        self.assertEqual(bot.s["losses_today"], 1)
        self.assertEqual(tb.load_state(self.state_path)["position"], None)
        with open(os.path.join(self.tmp, "trades.csv"), newline="",
                  encoding="utf-8") as f:
            self.assertEqual(len(list(csv.DictReader(f))), 1)

    def test_position_is_persisted_as_soon_as_it_opens(self):
        c1, c4 = signal_series_long()
        client = FakeClient(c1=c1, c4=c4, price=c1[-1]["c"], funding=0.0)
        bot, _, n = self.make_bot(client)
        bot.look_for_entry()
        self.assertIsNotNone(bot.s["position"], f"no entry taken: {n.joined()}")
        saved = tb.load_state(self.state_path)
        self.assertEqual(saved["position"]["entry"], bot.s["position"]["entry"])


# ------------------------------------------------------- 6. entry pipeline

def signal_series_long(n=140):
    """A 1h/4h pair that produces one LONG signal on the final closed candle."""
    c4 = candles(260, 0, H4, 1000.0, step=6.0, spread=3.0)      # close > 200 EMA
    c1 = candles(n, 0, HOUR, 2000.0, step=3.0, spread=4.0)      # fast EMA > slow EMA
    for _ in range(8):   # settle the last candle around its own 20 EMA
        f = tb.ema([x["c"] for x in c1], 20)[-1]
        c1[-1].update(o=f * 0.9985, l=f * 0.9955, c=f * 1.004, h=f * 1.006)
    return c1, c4


# The backtest walks the 4h series with an index driven by candle close times, so
# its 4h data must really start ~200 candles before the 1h window - exactly as
# run_backtest() fetches it with a warm-up period. Without that offset the 200
# EMA is never defined and the backtest takes no trades at all.
BT_BASE = 1_700_000_000_000 - 1_700_000_000_000 % H4
BT_WARMUP = 210 * H4


def _ohlc(n, t0, span, values, wick):
    """Candles whose open is the previous close, so the series is continuous."""
    return [{"t": t0 + i * span, "o": values[i], "c": values[i + 1],
             "h": max(values[i], values[i + 1]) + wick,
             "l": min(values[i], values[i + 1]) - wick,
             "T": t0 + (i + 1) * span - 1} for i in range(n)]


def backtest_series(n1=600, seed=3, noise=25.0, n4=400):
    """A choppy 1h uptrend under a clean 4h uptrend: many signals, mixed results.

    Deterministic - a fixed seed - so the assertions below are reproducible.
    """
    rnd = random.Random(seed)
    v1 = [2000.0 + 2.0 * i + 28.0 * math.sin(2 * math.pi * i / 22)
          + rnd.uniform(-noise, noise) for i in range(n1 + 1)]
    v4 = [1000.0 + 6.0 * i for i in range(n4 + 1)]
    return (_ohlc(n1, BT_BASE + BT_WARMUP, HOUR, v1, 8.0),
            _ohlc(n4, BT_BASE, H4, v4, 3.0))


class TestEntryPipeline(TempProject):
    def test_the_fixture_really_does_signal(self):
        c1, c4 = signal_series_long()
        closes1 = [x["c"] for x in c1]
        closes4 = [x["c"] for x in c4]
        bias = tb.trend_bias_at(closes4, tb.ema(closes4, 200), len(c4) - 1)
        self.assertEqual(bias, "LONG")
        sig = tb.signal_at(c1, tb.ema(closes1, 20), tb.ema(closes1, 50),
                           len(c1) - 1, bias, cfg())
        self.assertIsNotNone(sig)
        self.assertEqual(sig["side"], "LONG")

    def test_entry_opens_a_position_with_a_stop_and_a_target(self):
        c1, c4 = signal_series_long()
        px = c1[-1]["c"]
        bot, _, n = self.make_bot(FakeClient(c1=c1, c4=c4, price=px),
                                  conf=cfg(slippage_pct=0.0))
        bot.look_for_entry()
        p = bot.s["position"]
        self.assertIsNotNone(p, f"no entry: {n.joined()}")
        self.assertEqual(p["side"], "LONG")
        self.assertEqual(p["entry"], px)
        self.assertLess(p["stop"], p["entry"])
        self.assertGreater(p["target"], p["entry"])
        self.assertAlmostEqual(p["target"] - p["entry"],
                               2.0 * (p["entry"] - p["stop"]), places=6)
        self.assertIn("Opened LONG", n.joined())

    def test_adverse_funding_blocks_the_entry(self):
        c1, c4 = signal_series_long()
        client = FakeClient(c1=c1, c4=c4, price=c1[-1]["c"], funding=0.9)
        bot, _, n = self.make_bot(client, conf=cfg(max_adverse_funding_pct=0.05))
        bot.look_for_entry()
        self.assertIsNone(bot.s["position"])
        self.assertIn("funding", n.joined())

    def test_an_account_too_small_for_the_minimum_is_skipped_not_traded(self):
        c1, c4 = signal_series_long()
        rules = {"ETHUSDT": {"step": 0.001, "min_qty": 0.001,
                             "min_notional": 500.0, "tick": 0.01}}
        client = FakeClient(c1=c1, c4=c4, price=c1[-1]["c"], rules=rules)
        bot, _, n = self.make_bot(client, conf=cfg(symbols=["ETHUSDT"],
                                                   paper_fractional_qty=False))
        bot.look_for_entry()
        self.assertIsNone(bot.s["position"])
        self.assertIn("exchange minimum", n.joined())
        self.assertIn("never raised", n.joined())

    def test_a_tiny_account_is_simulated_fractionally_not_at_a_higher_risk(self):
        """Paper mode may go below the exchange minimum; it may not risk more."""
        c1, c4 = signal_series_long()
        rules = {"ETHUSDT": {"step": 0.001, "min_qty": 0.001,
                             "min_notional": 500.0, "tick": 0.01}}
        client = FakeClient(c1=c1, c4=c4, price=c1[-1]["c"], rules=rules)
        conf = cfg(symbols=["ETHUSDT"], paper_fractional_qty=True,
                   risk_per_trade_pct=0.5)
        bot, _, n = self.make_bot(client, conf=conf)
        bot.look_for_entry()
        p = bot.s["position"]
        self.assertIsNotNone(p, f"no entry: {n.joined()}")
        self.assertLess(p["qty"] * p["entry"], 500.0, "the notional was bumped up")
        self.assertLessEqual(p["planned_risk"], 22.0 * 0.005 + 1e-9)
        self.assertIn("below the exchange minimum", n.joined())

    def test_only_one_position_at_a_time(self):
        c1, c4 = signal_series_long()
        client = FakeClient(c1=c1, c4=c4, price=c1[-1]["c"], c1m=[])
        bot, _, _ = self.make_bot(client)
        bot.look_for_entry()
        first = bot.s["position"]
        self.assertIsNotNone(first)
        bot.s["last_candle"] = {}          # pretend a fresh candle arrived
        bot.step()
        self.assertEqual(bot.s["position"]["opened"], first["opened"],
                         "a second position was opened on top of the first")

    def test_no_signal_means_no_trade(self):
        flat1, flat4 = candles(140, 0, HOUR, 3000.0), candles(260, 0, H4, 3000.0)
        bot, _, n = self.make_bot(FakeClient(c1=flat1, c4=flat4, price=3000.0))
        bot.look_for_entry()
        self.assertIsNone(bot.s["position"])
        self.assertNotIn("Opened", n.joined())

    def test_signal_rules_reject_the_wrong_side_of_the_trend(self):
        conf = cfg()
        c1 = candles(80, 0, HOUR, 3000.0)
        fast, slow = tb.ema([x["c"] for x in c1], 20), tb.ema([x["c"] for x in c1], 50)
        self.assertIsNone(tb.signal_at(c1, fast, slow, len(c1) - 1, None, conf))
        self.assertIsNone(tb.signal_at(c1, fast, slow, 3, "LONG", conf))


# -------------------------------------------------------- 7. network failures

class TestNetworkFailures(TempProject):
    def test_sync_or_exit_reports_a_connection_failure_cleanly(self):
        class Dead:
            base = "https://fapi.binance.com"

            def sync_time(self):
                raise OSError("Connection to fapi.binance.com timed out")

        with self.assertRaises(SystemExit) as ctx:
            tb.sync_or_exit(Dead())
        self.assertIn("Could not reach", str(ctx.exception))

    def test_bot_construction_reports_a_rules_failure_cleanly(self):
        client = FakeClient(fail=OSError("network unreachable"))
        with self.assertRaises(SystemExit) as ctx:
            tb.Bot(cfg(), client, tb.PaperBroker(client, cfg(), {}),
                   FakeNotifier(), {}, self.state_path)
        self.assertIn("Could not load the exchange rules", str(ctx.exception))

    def test_the_loop_survives_a_data_failure_and_refuses_to_trade(self):
        client = FakeClient(c1=candles(60, 0, HOUR, 3000.0),
                            c4=candles(60, 0, H4, 3000.0), price=3000.0)
        bot, _, n = self.make_bot(client)
        client.fail = OSError("connection reset by peer")
        bot.run(once=True)                       # must not raise
        self.assertIsNone(bot.s["position"], "a trade was opened on dead data")
        self.assertIn("not trading on missing data", n.joined())

    def test_the_loop_survives_an_api_error_and_refuses_to_trade(self):
        client = FakeClient(c1=candles(60, 0, HOUR, 3000.0),
                            c4=candles(60, 0, H4, 3000.0), price=3000.0)
        bot, _, n = self.make_bot(client)
        client.fail = tb.ApiError(429, {"code": -1003, "msg": "Too many requests"})
        bot.run(once=True)
        self.assertIsNone(bot.s["position"])
        self.assertIn("not trading on missing data", n.joined())

    def test_an_unexpected_failure_inside_step_is_reported_not_fatal(self):
        client = FakeClient(c1=candles(60, 0, HOUR, 3000.0),
                            c4=candles(60, 0, H4, 3000.0), price=3000.0)
        bot, _, n = self.make_bot(client)

        def boom(*a, **k):
            raise RuntimeError("the state file caught fire")

        bot.risk.roll = boom
        bot.run(once=True)                       # must not raise
        self.assertIn("Error (#1)", n.joined())
        self.assertIn("caught fire", n.joined())
        self.assertEqual(bot.error_count, 1)

    def test_api_error_carries_status_and_code(self):
        e = tb.ApiError(400, {"code": -2019, "msg": "Margin is insufficient"})
        self.assertEqual((e.status, e.code), (400, -2019))
        self.assertIn("Margin is insufficient", str(e))

    def test_error_messages_never_leak_credentials(self):
        secret = "S3cr3tApiSecretValue1234567890"
        token = "123456789:AAEhBOweik6ad9r_AbCdEfGhIjKlMnOpQrS"
        leaky = (f"HTTPSConnectionPool(host='api.telegram.org'): url "
                 f"/bot{token}/sendMessage failed; signature=deadbeefcafe "
                 f"api_secret={secret}")
        out = tb.scrub(leaky, secret, token)
        self.assertNotIn(secret, out)
        self.assertNotIn(token, out)
        self.assertNotIn("deadbeefcafe", out)
        self.assertIn("***", out)


# ----------------------------------------------------------- 8. telegram

class FakeRequests:
    """Stands in for the `requests` module inside trading_bot."""

    class Session:
        """Binance's client builds one; it must never be used in a test."""

        def request(self, *a, **k):
            raise AssertionError("a test made a real HTTP request")

    class Resp:
        def __init__(self, status=200, payload=None, text=""):
            self.status_code, self._payload, self.text = status, payload, text

        def json(self):
            if self._payload is None:
                raise ValueError("no json")
            return self._payload

    def __init__(self, updates=None, post_status=200, post_payload=None, boom=None):
        self.updates = list(updates or [])
        self.post_status, self.post_payload = post_status, post_payload
        self.boom = boom
        self.posts, self.gets = [], []

    def post(self, url, json=None, timeout=10, **kw):
        self.posts.append({"url": url, "json": json})
        if self.boom:
            raise self.boom
        return self.Resp(self.post_status,
                         self.post_payload or {"ok": self.post_status < 400})

    def get(self, url, params=None, timeout=10, **kw):
        self.gets.append({"url": url, "params": params})
        if self.boom:
            raise self.boom
        ups, self.updates = self.updates, []
        return self.Resp(200, {"ok": True, "result": ups})


class TestTelegram(unittest.TestCase):
    TOKEN = "123456789:AAEhBOweik6ad9r_TESTTOKENvalue0000000"

    def setUp(self):
        self._requests = tb.requests
        self._stdout = sys.stdout
        sys.stdout = io.StringIO()

    def tearDown(self):
        tb.requests = self._requests
        self.out = sys.stdout.getvalue()
        sys.stdout = self._stdout

    def notifier(self, fake, chat="4242"):
        tb.requests = fake
        return tb.Notifier(cfg(telegram_token=self.TOKEN, telegram_chat_id=chat))

    @staticmethod
    def update(uid, chat_id, text, key="message"):
        return {"update_id": uid, key: {"chat": {"id": chat_id}, "text": text}}

    @staticmethod
    def names(commands):
        return [c["cmd"] for c in commands]

    def test_commands_from_the_configured_chat_are_accepted(self):
        fake = FakeRequests()
        n = self.notifier(fake)
        fake.updates = [self.update(1, 4242, "/status"),
                        self.update(2, "4242", "/pause"),
                        self.update(3, 4242, "/CLOSE@mybot extra words")]
        got = n.commands()
        self.assertEqual(self.names(got), ["/status", "/pause", "/close"])
        self.assertEqual([c["id"] for c in got], [1, 2, 3])
        self.assertEqual(got[2]["args"], ["extra", "words"])

    def test_commands_from_any_other_chat_are_refused(self):
        fake = FakeRequests()
        n = self.notifier(fake)
        fake.updates = [self.update(1, 9999, "/close"),
                        self.update(2, -1001234, "/pause"),
                        self.update(3, 42420, "/close"),
                        self.update(4, "", "/resume"),
                        self.update(5, None, "/status")]
        self.assertEqual(n.commands(), [],
                         "a command from an unauthorised chat was accepted")

    def test_a_refused_command_is_reported_not_swallowed(self):
        fake = FakeRequests()
        n = self.notifier(fake)
        fake.updates = [self.update(1, 9999, "/close")]
        n.commands()
        self.assertIn("refused 1 command", sys.stdout.getvalue())

    def test_mixed_authorised_and_unauthorised_updates(self):
        fake = FakeRequests()
        n = self.notifier(fake)
        fake.updates = [self.update(1, 9999, "/close"),
                        self.update(2, 4242, "/status"),
                        self.update(3, 1111, "/pause")]
        self.assertEqual(self.names(n.commands()), ["/status"])

    def test_non_command_and_contentless_messages_are_ignored(self):
        fake = FakeRequests()
        n = self.notifier(fake)
        fake.updates = [self.update(1, 4242, "hello there"),
                        {"update_id": 2, "message": {"chat": {"id": 4242}}},
                        {"update_id": 3, "channel_post": {"text": "/close"}},
                        {"update_id": 4}]
        self.assertEqual(n.commands(), [])

    def test_the_offset_advances_so_commands_are_not_replayed(self):
        fake = FakeRequests()
        n = self.notifier(fake)
        fake.updates = [self.update(7, 4242, "/status")]
        self.assertEqual(self.names(n.commands()), ["/status"])
        self.assertEqual(n.offset, 8)
        self.assertEqual(n.commands(), [], "the same command came back twice")

    def test_an_empty_chat_id_refuses_everything(self):
        fake = FakeRequests()
        n = self.notifier(fake, chat="")
        fake.updates = [self.update(1, 4242, "/close"),
                        self.update(2, "", "/close")]
        self.assertEqual(n.commands(), [])

    def test_a_rejected_message_is_reported(self):
        fake = FakeRequests(post_status=401,
                            post_payload={"ok": False, "description": "Unauthorized"})
        n = self.notifier(fake)
        n.send("hello")
        out = sys.stdout.getvalue()
        self.assertIn("Telegram rejected the message", out)
        self.assertIn("401", out)
        self.assertNotIn(self.TOKEN, out)

    def test_a_network_failure_on_send_is_reported_not_swallowed(self):
        fake = FakeRequests(boom=OSError(
            f"HTTPSConnectionPool: /bot{TestTelegram.TOKEN}/sendMessage failed"))
        n = self.notifier(fake)
        n.send("hello")                      # must not raise
        out = sys.stdout.getvalue()
        self.assertIn("telegram send failed", out)
        self.assertNotIn(self.TOKEN, out, "the bot token leaked into the log")

    def test_a_polling_failure_is_reported_and_returns_no_commands(self):
        fake = FakeRequests(boom=OSError("connection reset"))
        n = self.notifier(fake)
        self.assertEqual(n.commands(), [])
        self.assertIn("command polling failed", sys.stdout.getvalue())

    def test_repeated_failures_are_warned_about_only_once(self):
        fake = FakeRequests(boom=OSError("connection reset"))
        n = self.notifier(fake)
        for _ in range(5):
            n.send("x")
        self.assertEqual(sys.stdout.getvalue().count("telegram send failed"), 1)

    def test_a_placeholder_token_disables_telegram_with_a_warning(self):
        tb.requests = FakeRequests()
        n = tb.Notifier(cfg(telegram_token="PASTE_FROM_BOTFATHER",
                            telegram_chat_id="PASTE_YOUR_CHAT_ID"))
        self.assertEqual(n.token, "")
        self.assertIn("not a BotFather token", sys.stdout.getvalue())
        n.send("hello")
        self.assertEqual(tb.requests.posts, [], "a request went to a bogus token URL")

    def test_a_non_numeric_chat_id_is_warned_about(self):
        tb.requests = FakeRequests()
        tb.Notifier(cfg(telegram_token=self.TOKEN,
                        telegram_chat_id="PASTE_YOUR_CHAT_ID"))
        self.assertIn("not a numeric id", sys.stdout.getvalue())

    def test_no_token_means_console_only_and_no_requests(self):
        tb.requests = FakeRequests()
        n = tb.Notifier(cfg())
        n.send("console only")
        self.assertEqual(n.commands(), [])
        self.assertEqual(tb.requests.posts, [])
        self.assertEqual(tb.requests.gets, [])
        self.assertIn("console only", sys.stdout.getvalue())

    def test_startup_drops_commands_queued_while_the_bot_was_off(self):
        fake = FakeRequests(updates=[self.update(11, 4242, "/close")])
        n = self.notifier(fake)
        self.assertEqual(n.offset, 12)
        self.assertEqual(n.commands(), [])

    def test_send_posts_to_the_configured_chat(self):
        fake = FakeRequests()
        n = self.notifier(fake)
        n.send("a message")
        self.assertEqual(len(fake.posts), 1)
        self.assertEqual(fake.posts[0]["json"]["chat_id"], "4242")
        self.assertEqual(fake.posts[0]["json"]["text"], "a message")


# ------------------------------------------------------------- 9. backtest

class TestBacktest(unittest.TestCase):
    def setUp(self):
        self.c1, self.c4 = backtest_series()
        self.rules = RULES["ETHUSDT"]

    def test_the_fixture_produces_a_mix_of_wins_and_losses(self):
        r = tb.backtest_symbol(cfg(), self.c1, self.c4, self.rules)
        losses = [t for t in r["trades"] if t["pnl"] < 0]
        self.assertGreater(r["wins"], 0, "fixture produced no winning trades")
        self.assertGreater(len(losses), 0, "fixture produced no losing trades")

    def test_backtest_balances_add_up(self):
        r = tb.backtest_symbol(cfg(paper_start_balance=22.0), self.c1, self.c4,
                               self.rules)
        self.assertAlmostEqual(r["end"], r["start"] + sum(t["pnl"] for t in r["trades"]),
                               places=9)
        self.assertEqual(r["wins"], len([t for t in r["trades"] if t["pnl"] > 0]))
        self.assertGreaterEqual(r["max_dd"], 0.0)
        self.assertAlmostEqual(r["fees"], sum(t["fees"] for t in r["trades"]), places=9)
        self.assertGreater(r["fees"], 0.0, "a backtest with trades must pay fees")

    def test_entry_is_the_next_candle_open_so_there_is_no_look_ahead(self):
        """Every trade must enter at the open of the candle after its signal."""
        conf = cfg(slippage_pct=0.0)
        r = tb.backtest_symbol(conf, self.c1, self.c4, self.rules)
        opens = {tb.ist(c["t"]).strftime("%Y-%m-%d %H:%M"): c["o"] for c in self.c1}
        self.assertTrue(r["trades"], "fixture produced no trades at all")
        for t in r["trades"] + ([r["open"]] if r["open"] else []):
            self.assertIn(t["opened"], opens)
            self.assertAlmostEqual(t["entry"], opens[t["opened"]], places=9,
                                   msg="entry price is not the next candle's open")

    def test_slippage_moves_the_backtest_entry_against_the_trade(self):
        slipped = tb.backtest_symbol(cfg(slippage_pct=0.05), self.c1, self.c4,
                                     self.rules)
        clean = tb.backtest_symbol(cfg(slippage_pct=0.0), self.c1, self.c4, self.rules)
        opens = {tb.ist(c["t"]).strftime("%Y-%m-%d %H:%M"): c["o"] for c in self.c1}
        for t in slipped["trades"]:
            o = opens[t["opened"]]
            if t["side"] == "LONG":
                self.assertGreater(t["entry"], o)
            else:
                self.assertLess(t["entry"], o)
        self.assertGreater(sum(t["slippage"] for t in slipped["trades"]), 0.0)
        self.assertEqual(sum(t["slippage"] for t in clean["trades"]), 0.0)

    def test_a_trade_never_closes_before_it_opens(self):
        r = tb.backtest_symbol(cfg(), self.c1, self.c4, self.rules)
        for t in r["trades"]:
            self.assertGreaterEqual(t["closed"], t["opened"],
                                    f"{t['opened']} closed at {t['closed']}")

    def test_exits_are_only_ever_the_stop_or_the_target(self):
        r = tb.backtest_symbol(cfg(slippage_pct=0.0), self.c1, self.c4, self.rules)
        for t in r["trades"]:
            on_target = abs(t["exit"] - t["target"]) < 1e-9
            at_or_past_stop = (t["exit"] <= t["stop"] + 1e-9 if t["side"] == "LONG"
                               else t["exit"] >= t["stop"] - 1e-9)
            self.assertTrue(on_target or at_or_past_stop,
                            f"unexplained exit {t['exit']}")

    def test_slippage_only_ever_makes_an_exit_worse(self):
        r = tb.backtest_symbol(cfg(slippage_pct=0.05), self.c1, self.c4, self.rules)
        self.assertTrue(r["trades"])
        for t in r["trades"]:
            if t["exit_reason"] == "take profit":
                # a take profit fills slightly short of the target, never beyond
                if t["side"] == "LONG":
                    self.assertLess(t["exit"], t["target"] + 1e-9)
                else:
                    self.assertGreater(t["exit"], t["target"] - 1e-9)
            else:
                if t["side"] == "LONG":
                    self.assertLess(t["exit"], t["stop"] + 1e-9)
                else:
                    self.assertGreater(t["exit"], t["stop"] - 1e-9)

    def test_stop_distances_obey_the_configured_limits(self):
        conf = cfg(min_stop_pct=0.4, max_stop_pct=3.0)
        r = tb.backtest_symbol(conf, self.c1, self.c4, self.rules)
        for t in r["trades"]:
            pct = abs(t["entry"] - t["stop"]) / t["entry"] * 100
            self.assertGreaterEqual(pct, conf["min_stop_pct"] - 1e-9)
            self.assertLessEqual(pct, conf["max_stop_pct"] + 1e-9)

    def test_only_one_position_at_a_time_in_the_backtest(self):
        """Entries and exits must strictly alternate."""
        r = tb.backtest_symbol(cfg(), self.c1, self.c4, self.rules)
        for a, b in zip(r["trades"], r["trades"][1:]):
            self.assertLessEqual(a["closed"], b["opened"],
                                 "two backtest positions overlapped")

    def test_backtest_honours_the_daily_loss_limit(self):
        for limit in (1, 2):
            conf = cfg(max_losses_per_day=limit)
            r = tb.backtest_symbol(conf, self.c1, self.c4, self.rules)
            per_day = {}
            for t in r["trades"]:
                if t["pnl"] < 0:
                    day = t["closed"][:10]
                    per_day[day] = per_day.get(day, 0) + 1
            self.assertTrue(per_day, "no losses at all, so the limit is untested")
            for day, count in per_day.items():
                with self.subTest(limit=limit, day=day):
                    self.assertLessEqual(count, limit, f"{count} losses on {day}")

    def test_a_tighter_daily_limit_can_only_reduce_the_number_of_trades(self):
        loose = tb.backtest_symbol(cfg(max_losses_per_day=5), self.c1, self.c4,
                                   self.rules)
        tight = tb.backtest_symbol(cfg(max_losses_per_day=1), self.c1, self.c4,
                                   self.rules)
        self.assertLessEqual(len(tight["trades"]), len(loose["trades"]))

    def test_backtest_honours_the_monthly_drawdown_stop(self):
        """With a 0.1% monthly stop, the run must halt almost immediately."""
        strict = tb.backtest_symbol(cfg(monthly_drawdown_stop_pct=0.1),
                                    self.c1, self.c4, self.rules)
        loose = tb.backtest_symbol(cfg(monthly_drawdown_stop_pct=50.0),
                                   self.c1, self.c4, self.rules)
        self.assertLess(len(strict["trades"]), len(loose["trades"]),
                        "the monthly drawdown stop had no effect")

    def test_backtest_reports_a_position_still_open_at_the_end(self):
        c1, c4 = backtest_series(seed=7, noise=20.0)
        r = tb.backtest_symbol(cfg(), c1, c4, self.rules)
        self.assertIsNotNone(r["open"], "fixture was chosen to end mid-trade")
        for key in ("side", "entry", "stop", "target", "qty", "opened"):
            self.assertIn(key, r["open"])
        # the open trade must not be counted in the closed-trade figures
        self.assertAlmostEqual(r["end"], r["start"] + sum(t["pnl"] for t in r["trades"]),
                               places=9)

    def test_backtest_never_trades_without_a_trend(self):
        flat1 = candles(400, BT_BASE + BT_WARMUP, HOUR, 3000.0)
        flat4 = candles(400, BT_BASE, H4, 3000.0)
        r = tb.backtest_symbol(cfg(), flat1, flat4, self.rules)
        self.assertEqual(r["trades"], [])
        self.assertEqual(r["end"], r["start"])

    def test_losses_stay_inside_the_risk_budget_except_on_a_gap(self):
        conf = cfg(paper_start_balance=22.0, risk_per_trade_pct=1.5,
                   max_risk_pct_when_bumped=3.0)
        r = tb.backtest_symbol(conf, self.c1, self.c4, self.rules)
        for t in r["trades"]:
            if t["pnl"] >= 0:
                continue
            gapped = (t["exit"] < t["stop"] - 1e-9 if t["side"] == "LONG"
                      else t["exit"] > t["stop"] + 1e-9)
            if gapped:
                continue   # a gap through the stop is not a sizing failure
            with self.subTest(opened=t["opened"]):
                self.assertLessEqual(
                    -t["pnl"], conf["max_risk_pct_when_bumped"] / 100 * 22.0 + 1e-9,
                    "a non-gapped loss exceeded the risk cap")


# ------------------------------------------------- 10. output encoding

class TestOutputEncoding(unittest.TestCase):
    def test_say_does_not_raise_on_a_legacy_code_page(self):
        """The repaired crash: printing an emoji to cp1252 killed the bot."""
        buf = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict")
        old = sys.stdout
        sys.stdout = buf
        try:
            tb.say("✅ closed ❌ stop → exit ≈ ₹1,900")   # must not raise
            tb.say("plain ascii")
        finally:
            sys.stdout = old

    def test_plain_print_really_would_have_crashed(self):
        """Confirms the test above is testing something real."""
        buf = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict")
        old = sys.stdout
        sys.stdout = buf
        try:
            with self.assertRaises(UnicodeEncodeError):
                print("✅")
        finally:
            sys.stdout = old

    def test_notifier_send_does_not_raise_on_a_legacy_code_page(self):
        buf = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict")
        # keep the real module object: re-reading tb.requests after replacing it
        # hands back the fake, which used to leak into every later test
        old_out, old_requests = sys.stdout, tb.requests
        sys.stdout, tb.requests = buf, FakeRequests()
        try:
            tb.Notifier(cfg()).send("✅ Closed LONG ETHUSDT → ₹1,900")
        finally:
            sys.stdout, tb.requests = old_out, old_requests

    def test_source_file_is_valid_utf8_and_not_truncated(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "trading_bot.py")
        with open(path, "rb") as f:
            raw = f.read()
        text = raw.decode("utf-8")               # raises if not valid UTF-8
        compile(text, path, "exec")              # raises if syntactically broken
        self.assertIn('emoji = "✅" if res["pnl"] > 0', text)
        self.assertTrue(text.rstrip().endswith('keep their exchange stop-loss.")'))


if __name__ == "__main__":
    unittest.main(verbosity=2)
