#!/usr/bin/env python3
"""Responsiveness and concurrency test suite.

Run with:   python -m unittest -v test_performance

The question every test here asks is: can a Telegram command be answered while
the trading engine is stuck waiting on Binance? A `BlockingClient` is used to
make that concrete - it parks inside a market-data call until the test releases
it, which is exactly what a connect timeout looks like from the inside.

Timing assertions are deliberately loose (a second or more of headroom) so they
measure the architecture rather than the speed of the machine running them.
"""

import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import trading_bot as tb  # noqa: E402
from test_trading_bot import (FAKE_TOKEN, H4, HOUR, MIN, RULES,  # noqa: E402
                              FakeClient, FakeNotifier, TempProject, candles,
                              cfg, signal_series_long)

# A command must finish well inside this. The target is a second; the assertion
# allows more so a slow CI box does not fail a correct implementation.
FAST_MS = 1500


class BlockingClient(FakeClient):
    """A fake exchange that can be made to hang, like a real connect timeout."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.release = threading.Event()
        self.entered = threading.Event()
        self.block_price = False
        self.block_klines = False
        self.calls = {"price": 0, "klines": 0, "closed_klines": 0}

    def _maybe_block(self, which):
        if which == "price" and self.block_price:
            self.entered.set()
            self.release.wait(30)
        if which == "klines" and self.block_klines:
            self.entered.set()
            self.release.wait(30)

    def price(self, symbol):
        self.calls["price"] += 1
        self._maybe_block("price")
        return super().price(symbol)

    def klines(self, symbol, interval, limit=500, start=None, end=None):
        self.calls["klines"] += 1
        self._maybe_block("klines")
        return super().klines(symbol, interval, limit, start, end)

    def closed_klines(self, symbol, interval, limit=500):
        self.calls["closed_klines"] += 1
        self._maybe_block("klines")
        return super().closed_klines(symbol, interval, limit)


class Timed(TempProject):
    """A Bot behind a MarketData cache, as main() builds it."""

    def build(self, conf=None, state=None, client=None, enabled=True,
              c1m=None, signal=False, warm=True):
        conf = conf or cfg()
        state = {"paper_balance": 22.0} if state is None else state
        state.setdefault("paper_enabled", enabled)
        if signal:
            c1, c4 = signal_series_long()
            price = c1[-1]["c"]
        else:
            c1, c4 = candles(60, 0, HOUR, 3000.0), candles(60, 0, H4, 3000.0)
            price = 3000.0
        raw = client or BlockingClient(c1=c1, c4=c4, price=price, c1m=c1m or [])
        md = tb.MarketData(raw, conf)
        n = FakeNotifier()
        bot = tb.Bot(conf, md, tb.PaperBroker(md, conf, state), n, state,
                     self.state_path)
        if warm:
            # the engine fills the cache on its first cycle; do the same here
            for sym in conf["symbols"]:
                try:
                    md.fresh_price(sym)
                    md.closed_klines(sym, conf["entry_interval"], 300)
                    md.closed_klines(sym, conf["trend_interval"], 300)
                except Exception:
                    pass
        self.addCleanup(bot.shutdown)
        return bot, n, raw, md

    def position(self, bot, side="SHORT", entry=3000.0, stop=3030.0, qty=0.001,
                 opened=None):
        opened = (tb.now_ms() - 2 * 86_400_000) if opened is None else opened
        bot.s["position"] = {
            "symbol": "ETHUSDT", "side": side, "qty": qty, "entry": entry,
            "stop": stop, "initial_stop": stop,
            "target": tb.target_for(side, entry, stop, 2.0),
            "opened": opened, "checked": opened, "trade_id": "perftrade1",
            "planned_risk": 0.11, "strategy": tb.STRATEGY_NAME,
            "market_condition": "test", "entry_slippage": 0.0,
            "equity_before": 22.0}
        return bot.s["position"]

    @staticmethod
    def timed(fn, *a, **k):
        t0 = time.perf_counter()
        out = fn(*a, **k)
        return out, (time.perf_counter() - t0) * 1000


# ------------------------------------------- 1. commands during a stuck cycle

class TestCommandsDuringSlowMarketData(Timed):
    def test_risk_answers_while_the_engine_is_stuck_on_market_data(self):
        """The reported symptom: /risk not responding during a trading cycle."""
        bot, n, raw, _ = self.build()
        self.position(bot)
        raw.block_klines = True
        cycle = threading.Thread(target=bot.step, daemon=True)
        cycle.start()
        self.assertTrue(raw.entered.wait(5), "the engine never reached the exchange")
        # the cycle is now parked inside a market-data call
        _, ms = self.timed(bot.dispatch, "/risk", [])
        self.assertLess(ms, FAST_MS, f"/risk took {ms:.0f}ms while the engine was busy")
        self.assertIn("Risk limits", n.joined())
        raw.release.set()
        cycle.join(timeout=15)
        self.assertFalse(cycle.is_alive(), "the trading cycle never finished")

    def test_every_read_only_command_answers_while_the_engine_is_stuck(self):
        bot, n, raw, _ = self.build()
        self.position(bot)
        raw.block_klines = True
        cycle = threading.Thread(target=bot.step, daemon=True)
        cycle.start()
        self.assertTrue(raw.entered.wait(5))
        slow = {}
        for cmd in ["/help", "/status", "/risk", "/balance", "/positions",
                    "/performance", "/history", "/health"]:
            _, ms = self.timed(bot.dispatch, cmd, [])
            if ms >= FAST_MS:
                slow[cmd] = ms
        self.assertEqual(slow, {}, f"commands blocked by the engine: {slow}")
        raw.release.set()
        cycle.join(timeout=15)

    def test_status_does_not_wait_for_a_full_trading_cycle(self):
        """/status reads the cache; it never starts a cycle of its own."""
        bot, n, raw, _ = self.build()
        before = dict(raw.calls)
        _, ms = self.timed(bot.dispatch, "/status", [])
        self.assertLess(ms, FAST_MS)
        self.assertEqual(raw.calls, before,
                         "/status made a market-data request of its own")
        self.assertEqual(bot.loops, 0, "/status ran a trading cycle")
        self.assertIn("Status", n.last())

    def test_status_is_served_entirely_from_cache(self):
        bot, n, raw, _ = self.build()
        raw.block_price = True
        raw.block_klines = True        # any fetch would now hang for 30s
        _, ms = self.timed(bot.dispatch, "/status", [])
        self.assertLess(ms, FAST_MS, "/status tried to fetch something")
        self.assertIn("ETHUSDT", n.last())

    def test_commands_remain_responsive_during_a_binance_timeout(self):
        """The engine starts healthy, then the exchange goes away."""
        bot, n, raw, _ = self.build(warm=False)
        raw.fail = OSError("Connection to fapi.binance.com timed out")
        for cmd in ["/help", "/status", "/risk", "/balance", "/health",
                    "/performance"]:
            _, ms = self.timed(bot.dispatch, cmd, [])
            self.assertLess(ms, FAST_MS, f"{cmd} took {ms:.0f}ms with a dead exchange")
        self.assertIn("no recent quote", n.joined())

    def test_halt_is_processed_promptly_while_the_engine_is_stuck(self):
        bot, n, raw, md = self.build()
        self.position(bot)
        raw.block_klines = True        # the cycle parks on the 1m scan
        cycle = threading.Thread(target=bot.step, daemon=True)
        cycle.start()
        self.assertTrue(raw.entered.wait(5))
        _, ms = self.timed(bot.dispatch, "/halt", [])
        self.assertLess(ms, FAST_MS, f"/halt took {ms:.0f}ms")
        self.assertTrue(bot.risk.is_halted())
        self.assertFalse(bot.s["paper_enabled"])
        self.assertIsNone(bot.s["position"], "/halt did not close the position")
        raw.release.set()
        cycle.join(timeout=15)
        # and the cycle must not resurrect or re-book the closed trade
        self.assertIsNone(bot.s["position"])
        self.assertEqual(len(tb.load_journal(mode="PAPER")), 1,
                         "the trade was booked twice")

    def test_paper_off_is_immediate_and_needs_no_market_data(self):
        bot, n, raw, _ = self.build()
        raw.block_price = True
        raw.block_klines = True
        _, ms = self.timed(bot.dispatch, "/paper_off", [])
        self.assertLess(ms, 250, f"/paper_off took {ms:.0f}ms")
        self.assertFalse(bot.s["paper_enabled"])
        self.assertIn("Paper trading OFF", n.last())

    def test_paper_on_does_not_block_on_market_data(self):
        bot, n, raw, _ = self.build(enabled=False)
        raw.block_price = True
        raw.block_klines = True
        _, ms = self.timed(bot.dispatch, "/paper_on", [])
        self.assertLess(ms, FAST_MS, f"/paper_on took {ms:.0f}ms")
        self.assertTrue(bot.s["paper_enabled"])


# ----------------------------------------------- 2. locking and concurrency

class TestLocking(Timed):
    def test_the_state_lock_is_never_held_across_a_market_data_call(self):
        """If it were, every command would queue behind the exchange."""
        bot, n, raw, _ = self.build()
        self.position(bot)
        raw.block_klines = True
        cycle = threading.Thread(target=bot.step, daemon=True)
        cycle.start()
        self.assertTrue(raw.entered.wait(5))
        got = bot.state_lock.acquire(timeout=2)
        if got:
            bot.state_lock.release()
        self.assertTrue(got, "the state lock was held during a network call")
        raw.release.set()
        cycle.join(timeout=15)

    def test_no_deadlock_when_commands_and_a_cycle_interleave(self):
        bot, n, raw, _ = self.build(signal=True, conf=cfg(symbols=["ETHUSDT"]))
        errors = []

        def spam():
            try:
                for _ in range(25):
                    for cmd in ["/status", "/risk", "/balance", "/positions",
                                "/paper_off", "/paper_on", "/health"]:
                        bot.dispatch(cmd, [])
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=spam, daemon=True) for _ in range(3)]
        cycles = threading.Thread(
            target=lambda: [bot.step() for _ in range(5)], daemon=True)
        for t in threads:
            t.start()
        cycles.start()
        for t in threads + [cycles]:
            t.join(timeout=30)
            self.assertFalse(t.is_alive(), "a thread deadlocked")
        self.assertEqual(errors, [])

    def test_several_commands_run_safely_one_after_another(self):
        bot, n, _, _ = self.build()
        for i in range(30):
            bot.run_command({"id": i, "cmd": "/status", "args": []})
        self.assertEqual(bot.cmd_count, 30)
        self.assertEqual(len(n.sent), 30)

    def test_a_position_cannot_be_booked_twice_by_two_threads(self):
        """The close path re-checks the trade id under the lock."""
        m0 = (tb.now_ms() // MIN) * MIN - 20 * MIN
        c1m = [{"t": m0 + MIN, "o": 3001.0, "h": 3040.0, "l": 3000.0, "c": 3035.0,
                "T": m0 + 2 * MIN - 1}]
        bot, n, raw, _ = self.build(conf=cfg(slippage_pct=0.0), c1m=c1m)
        self.position(bot, opened=m0)
        done = []

        def close_it():
            try:
                bot.dispatch("/close", [])
                done.append("cmd")
            except Exception as e:
                done.append(e)

        ts = [threading.Thread(target=close_it, daemon=True) for _ in range(4)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(timeout=10)
        self.assertIsNone(bot.s["position"])
        self.assertEqual(len(tb.load_journal(mode="PAPER")), 1,
                         "the same trade was journalled more than once")


# ------------------------------------------- 3. one poller, never two

class TestSinglePoller(Timed):
    def test_the_worker_is_the_only_poller_once_it_runs(self):
        bot, n, _, _ = self.build()
        polls = {"n": 0}
        real = bot.n.commands

        def counting():
            polls["n"] += 1
            return real()

        bot.n.commands = counting
        bot.start_command_worker()
        self.assertTrue(bot.worker_polls)
        time.sleep(0.2)
        before = polls["n"]
        bot.step()                       # must not poll as well
        bot.step()
        self.assertEqual(polls["n"], before,
                         "the trading loop polled Telegram behind the worker")
        bot.stop_command_worker()
        self.assertFalse(bot.worker_polls)
        # with the worker gone the loop takes polling back over
        before = polls["n"]
        bot.step()
        self.assertEqual(polls["n"], before + 1)

    def test_starting_the_worker_twice_creates_one_thread(self):
        bot, _, _, _ = self.build()
        first = bot.start_command_worker()
        second = bot.start_command_worker()
        self.assertIs(first, second)
        names = [t.name for t in threading.enumerate()
                 if t.name == "telegram-commands"]
        self.assertEqual(len(names), 1, f"{len(names)} pollers running")
        bot.stop_command_worker()

    def test_the_notifier_refuses_two_concurrent_pollers(self):
        """getUpdates advances a shared offset; only one caller may be inside."""
        import test_trading_bot as T
        fake = T.FakeRequests(updates=[])
        tb_requests = tb.requests
        tb.requests = fake
        try:
            n = tb.Notifier(cfg(telegram_token="123456789:AA" + "x" * 30,
                                telegram_chat_id="4242"))
            n.io_lock.acquire()           # pretend another poller is inside
            try:
                self.assertEqual(n.commands(), [],
                                 "a second poller was allowed in")
            finally:
                n.io_lock.release()
            fake.updates = [{"update_id": 1,
                             "message": {"chat": {"id": 4242}, "text": "/status"}}]
            self.assertEqual([c["cmd"] for c in n.commands()], ["/status"])
        finally:
            tb.requests = tb_requests

    def test_a_worker_failure_does_not_stop_the_engine(self):
        bot, n, _, _ = self.build()
        bot.n.commands = lambda: (_ for _ in ()).throw(OSError("telegram down"))
        bot.start_command_worker()
        time.sleep(0.3)
        self.assertTrue(bot.worker_polls, "the worker died on an error")
        bot.step()                       # the engine is unaffected
        self.assertEqual(bot.loops, 1)
        bot.stop_command_worker()


# --------------------------------------------- 4. the market-data cache

class TestMarketDataCache(unittest.TestCase):
    def md(self, conf=None, **kw):
        conf = conf or cfg()
        raw = BlockingClient(c1=candles(60, 0, HOUR, 3000.0),
                             c4=candles(60, 0, H4, 3000.0), price=3000.0, **kw)
        return tb.MarketData(raw, conf), raw

    def test_a_price_is_reused_inside_the_cache_window(self):
        md, raw = self.md(conf=cfg(price_cache_sec=30))
        self.assertEqual(md.price("ETHUSDT"), 3000.0)
        for _ in range(5):
            md.price("ETHUSDT")
        self.assertEqual(raw.calls["price"], 1, "the ticker was fetched repeatedly")

    def test_a_price_older_than_the_window_is_refetched(self):
        md, raw = self.md(conf=cfg(price_cache_sec=0))
        md.price("ETHUSDT")
        md.price("ETHUSDT")
        self.assertEqual(raw.calls["price"], 2)

    def test_cached_prices_expire_against_the_age_asked_for(self):
        md, raw = self.md()
        md.fresh_price("ETHUSDT")
        px, age = md.price_cached("ETHUSDT", 60)
        self.assertEqual(px, 3000.0)
        self.assertLess(age, 1000)
        md._px["ETHUSDT"] = (3000.0, tb.now_ms() - 120_000)   # 2 minutes old
        self.assertEqual(md.price_cached("ETHUSDT", 60), (None, None))
        self.assertEqual(md.price_cached("ETHUSDT", 600)[0], 3000.0)
        self.assertGreater(md.price_age_ms("ETHUSDT"), 100_000)

    def test_an_unseen_symbol_has_no_cached_price(self):
        md, _ = self.md()
        self.assertEqual(md.price_cached("DOGEUSDT", 60), (None, None))

    def test_fresh_price_always_goes_to_the_exchange(self):
        md, raw = self.md(conf=cfg(price_cache_sec=9999))
        md.price("ETHUSDT")
        md.fresh_price("ETHUSDT")
        md.fresh_price("ETHUSDT")
        self.assertEqual(raw.calls["price"], 3,
                         "fresh_price served a cached quote")

    def test_candles_are_cached_until_the_next_one_is_due(self):
        md, raw = self.md()
        md.closed_klines("ETHUSDT", "1h", 300)
        for _ in range(10):
            md.closed_klines("ETHUSDT", "1h", 300)
        self.assertEqual(raw.calls["closed_klines"], 1,
                         "1h candles were downloaded more than once an hour")

    def test_candles_are_refetched_once_a_new_one_has_closed(self):
        md, raw = self.md()
        got = md.closed_klines("ETHUSDT", "1h", 300)
        # pretend the newest candle closed over an hour ago
        aged = [dict(c, t=c["t"] - 2 * HOUR, T=c["T"] - 2 * HOUR) for c in got]
        md._kl[("ETHUSDT", "1h")] = (aged, 300, tb.now_ms())
        self.assertEqual(md.klines_cached("ETHUSDT", "1h"), (None, None))
        md.closed_klines("ETHUSDT", "1h", 300)
        self.assertEqual(raw.calls["closed_klines"], 2)

    def test_an_overdue_cache_entry_is_still_available_for_a_report(self):
        md, _ = self.md()
        got = md.closed_klines("ETHUSDT", "1h", 300)
        aged = [dict(c, t=c["t"] - 5 * HOUR, T=c["T"] - 5 * HOUR) for c in got]
        md._kl[("ETHUSDT", "1h")] = (aged, 300, tb.now_ms())
        self.assertEqual(md.klines_cached("ETHUSDT", "1h"), (None, None))
        shown, age = md.klines_cached("ETHUSDT", "1h", allow_overdue=True)
        self.assertEqual(len(shown), len(aged))
        self.assertIsNotNone(age)

    def test_a_bigger_limit_than_was_cached_is_refetched(self):
        md, raw = self.md()
        md.closed_klines("ETHUSDT", "1h", 100)
        md.closed_klines("ETHUSDT", "1h", 300)
        self.assertEqual(raw.calls["closed_klines"], 2)

    def test_the_1m_scan_is_never_cached(self):
        md, raw = self.md()
        for _ in range(3):
            md.klines("ETHUSDT", "1m", 1500, 0, None)
        self.assertEqual(raw.calls["klines"], 3)

    def test_funding_is_cached_for_its_window(self):
        conf = cfg(funding_cache_sec=30)
        raw = BlockingClient(price=3000.0, funding=0.01)
        md = tb.MarketData(raw, conf)
        calls = {"n": 0}
        real = raw.funding_pct

        def counting(sym):
            calls["n"] += 1
            return real(sym)

        raw.funding_pct = counting
        for _ in range(4):
            md.funding_pct("ETHUSDT")
        self.assertEqual(calls["n"], 1)
        self.assertEqual(md.funding_pct("ETHUSDT", 0), 0.01)
        self.assertEqual(calls["n"], 2)

    def test_exchange_rules_are_fetched_once(self):
        md, raw = self.md()
        calls = {"n": 0}
        real = raw.rules

        def counting(symbols):
            calls["n"] += 1
            return real(symbols)

        raw.rules = counting
        md.rules(["ETHUSDT", "SOLUSDT"])
        md.rules(["ETHUSDT"])
        md.rules(["ETHUSDT", "SOLUSDT"])
        self.assertEqual(calls["n"], 1)

    def test_invalidate_clears_what_it_is_told_to(self):
        md, raw = self.md()
        md.fresh_price("ETHUSDT")
        md.fresh_price("SOLUSDT")
        md.invalidate("ETHUSDT")
        self.assertEqual(md.price_cached("ETHUSDT", 60), (None, None))
        self.assertIsNotNone(md.price_cached("SOLUSDT", 60)[0])
        md.invalidate()
        self.assertEqual(md.price_cached("SOLUSDT", 60), (None, None))

    def test_stats_report_reuse(self):
        md, _ = self.md(conf=cfg(price_cache_sec=60))
        md.price("ETHUSDT")
        md.price("ETHUSDT")
        st = md.stats()
        self.assertEqual(st["misses"], 1)
        self.assertGreaterEqual(st["hits"], 1)
        self.assertGreater(st["hit_rate"], 0)


# ----------------------------- 5. cached data can never open a position

class TestCacheCannotRelaxSafety(Timed):
    def test_an_entry_always_fetches_a_fresh_price(self):
        bot, n, raw, md = self.build(signal=True, conf=cfg(
            symbols=["ETHUSDT"], price_cache_sec=9999))
        before = raw.calls["price"]
        bot.look_for_entry()
        self.assertIsNotNone(bot.s["position"], n.joined())
        self.assertGreater(raw.calls["price"], before,
                           "a position was opened on a cached price")

    def test_a_stale_cached_price_cannot_be_used_for_an_entry(self):
        conf = cfg(symbols=["ETHUSDT"], price_cache_sec=9999)
        bot, n, raw, md = self.build(signal=True, conf=conf)
        # plant a wildly wrong, very old quote in the cache
        md._px["ETHUSDT"] = (1.0, tb.now_ms() - 3_600_000)
        bot.look_for_entry()
        p = bot.s["position"]
        self.assertIsNotNone(p, n.joined())
        self.assertGreater(p["entry"], 100.0,
                           "the entry used the stale cached price")

    def test_stale_candles_still_block_an_entry_even_when_cached(self):
        conf = cfg(symbols=["ETHUSDT"])
        c1, c4 = signal_series_long()
        old = 10 * HOUR
        c1 = [{**c, "t": c["t"] - old, "T": c["T"] - old} for c in c1]
        raw = BlockingClient(c1=c1, c4=c4, price=c1[-1]["c"])
        md = tb.MarketData(raw, conf)
        state = {"paper_balance": 22.0, "paper_enabled": True}
        bot = tb.Bot(conf, md, tb.PaperBroker(md, conf, state), FakeNotifier(),
                     state, self.state_path)
        self.addCleanup(bot.shutdown)
        md.closed_klines("ETHUSDT", "1h", 300)      # warm the cache with them
        bot.look_for_entry()
        self.assertIsNone(bot.s["position"])
        self.assertIn("stale data", bot.n.joined())

    def test_risk_checks_still_fire_with_the_cache_in_place(self):
        conf = cfg(max_account_drawdown_pct=5.0, max_daily_loss_pct=90.0,
                   monthly_drawdown_stop_pct=90.0)
        state = {"paper_balance": 20.8, "day": tb.day_key(),
                 "day_start_equity": 20.8, "equity_peak": 22.0,
                 "month": tb.month_key(), "month_start": 20.8,
                 "paper_enabled": True}
        bot, n, raw, md = self.build(conf=conf, state=state)
        bot.step()
        self.assertTrue(bot.risk.is_halted(), "the drawdown halt stopped firing")
        self.assertIn("drawdown", bot.s["halt_reason"])

    def test_the_daily_limit_still_fires_on_an_unrealised_loss(self):
        conf = cfg(max_daily_loss_pct=2.0, max_account_drawdown_pct=50.0,
                   slippage_pct=0.0)
        state = {"paper_balance": 22.0, "day": tb.day_key(),
                 "day_start_equity": 22.0, "equity_peak": 22.0,
                 "month": tb.month_key(), "month_start": 22.0,
                 "paper_enabled": True}
        raw = BlockingClient(c1=candles(60, 0, HOUR, 3000.0),
                             c4=candles(60, 0, H4, 3000.0), price=2800.0, c1m=[])
        bot, n, _, _ = self.build(conf=conf, state=state, client=raw)
        self.position(bot, side="LONG", entry=3000.0, stop=2970.0, qty=0.0025,
                      opened=tb.now_ms() - 60_000)
        bot.step()
        self.assertTrue(bot.risk.is_locked_today())
        self.assertIsNone(bot.s["position"])

    def test_a_stop_is_still_enforced_with_the_phased_cycle(self):
        m0 = (tb.now_ms() // MIN) * MIN - 20 * MIN
        c1m = [{"t": m0 + MIN, "o": 2999.0, "h": 3000.0, "l": 2960.0, "c": 2965.0,
                "T": m0 + 2 * MIN - 1}]
        bot, n, _, _ = self.build(conf=cfg(slippage_pct=0.0), c1m=c1m)
        self.position(bot, side="LONG", entry=3000.0, stop=2970.0, qty=0.001,
                      opened=m0)
        bot.step()
        self.assertIsNone(bot.s["position"], "the stop was not enforced")
        rows = tb.load_journal(mode="PAPER")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["exit_reason"], "stop loss")

    def test_paper_off_mid_sizing_stops_the_entry(self):
        """The race /paper_off must win: no new risk after it is sent."""
        bot, n, raw, _ = self.build(signal=True, conf=cfg(symbols=["ETHUSDT"]))
        real_size = tb.size_position

        def switch_off_then_size(*a, **k):
            bot.dispatch("/paper_off", [])      # arrives while we size
            return real_size(*a, **k)

        tb.size_position = switch_off_then_size
        try:
            bot.look_for_entry()
        finally:
            tb.size_position = real_size
        self.assertIsNone(bot.s["position"], "a trade opened after /paper_off")
        self.assertIn("switched off while it was being sized", n.joined())

    def test_a_halt_mid_sizing_stops_the_entry(self):
        bot, n, raw, _ = self.build(signal=True, conf=cfg(symbols=["ETHUSDT"]))
        real_size = tb.size_position

        def halt_then_size(*a, **k):
            bot.risk.halt("test halt")
            return real_size(*a, **k)

        tb.size_position = halt_then_size
        try:
            bot.look_for_entry()
        finally:
            tb.size_position = real_size
        self.assertIsNone(bot.s["position"], "a trade opened after a halt")

    def test_entries_are_not_switched_on_by_any_of_this(self):
        state = {"paper_balance": 22.0}
        conf = cfg()
        raw = BlockingClient(c1=candles(60, 0, HOUR, 3000.0),
                             c4=candles(60, 0, H4, 3000.0), price=3000.0)
        md = tb.MarketData(raw, conf)
        bot = tb.Bot(conf, md, tb.PaperBroker(md, conf, state), FakeNotifier(),
                     state, self.state_path)
        self.addCleanup(bot.shutdown)
        bot.step()
        self.assertFalse(bot.s["paper_enabled"])
        self.assertIsNone(bot.s["position"])


# ------------------------------- 6. the incremental position scan

class TestIncrementalScan(Timed):
    def test_the_scan_resumes_instead_of_restarting_from_the_entry(self):
        """The real bottleneck: a 2-day-old trade rescanned ~2,900 candles a cycle."""
        m0 = (tb.now_ms() // MIN) * MIN - 2000 * MIN
        series = candles(2000, m0, MIN, 3000.0, spread=1.0)
        bot, n, raw, _ = self.build(conf=cfg(slippage_pct=0.0), c1m=series)
        pos = self.position(bot, side="SHORT", entry=3000.0, stop=3100.0,
                            qty=0.001, opened=m0)
        bot.step()
        first = raw.calls["klines"]
        self.assertGreaterEqual(first, 1)
        self.assertGreater(bot.s["position"]["scanned_to"], m0,
                           "the scan cursor did not advance")
        # a second cycle must not download the whole history again
        raw.calls["klines"] = 0
        bot.step()
        self.assertLessEqual(raw.calls["klines"], 1,
                             "the position scan started over from the entry")
        self.assertEqual(tb.PaperBroker.scan_from(bot.s["position"]),
                         bot.s["position"]["scanned_to"])

    def test_a_position_with_no_cursor_falls_back_to_the_entry(self):
        m0 = (tb.now_ms() // MIN) * MIN - 30 * MIN
        pos = {"opened": m0}
        self.assertEqual(tb.PaperBroker.scan_from(pos),
                         tb.PaperBroker.first_post_entry_minute(m0))
        pos["scanned_to"] = m0 + 10 * MIN
        self.assertEqual(tb.PaperBroker.scan_from(pos), m0 + 10 * MIN)

    def test_the_cursor_never_skips_a_candle_that_is_still_forming(self):
        now = tb.now_ms()
        m0 = (now // MIN) * MIN - 3 * MIN
        closed = [{"t": m0, "o": 3000.0, "h": 3001.0, "l": 2999.0, "c": 3000.0,
                   "T": m0 + MIN - 1},
                  {"t": m0 + MIN, "o": 3000.0, "h": 3001.0, "l": 2999.0,
                   "c": 3000.0, "T": m0 + 2 * MIN - 1}]
        forming = [{"t": m0 + 2 * MIN, "o": 3000.0, "h": 3001.0, "l": 2999.0,
                    "c": 3000.0, "T": now + 30_000}]
        bot, n, raw, _ = self.build(c1m=closed + forming)
        self.position(bot, side="SHORT", entry=3000.0, stop=3100.0, opened=m0)
        plan = bot.b.evaluate(bot.s["position"])
        self.assertEqual(plan["scanned_to"], m0 + 2 * MIN,
                         "the unfinished candle was marked as already scanned")

    def test_a_stop_inside_the_resumed_window_is_still_found(self):
        m0 = (tb.now_ms() // MIN) * MIN - 100 * MIN
        series = candles(100, m0, MIN, 3000.0, spread=1.0)
        series[60].update(h=3120.0)          # stop hit 60 minutes in
        bot, n, raw, _ = self.build(conf=cfg(slippage_pct=0.0), c1m=series)
        self.position(bot, side="SHORT", entry=3000.0, stop=3100.0, qty=0.001,
                      opened=m0)
        bot.s["position"]["scanned_to"] = m0 + 30 * MIN   # resume mid-way
        bot.step()
        self.assertIsNone(bot.s["position"], "the stop in the window was missed")
        self.assertEqual(tb.load_journal(mode="PAPER")[0]["exit_reason"], "stop loss")

    def test_evaluate_changes_nothing(self):
        """It runs outside the lock, so it must be free of side effects."""
        m0 = (tb.now_ms() // MIN) * MIN - 20 * MIN
        c1m = [{"t": m0 + MIN, "o": 2999.0, "h": 3000.0, "l": 2960.0, "c": 2965.0,
                "T": m0 + 2 * MIN - 1}]
        bot, n, _, _ = self.build(conf=cfg(slippage_pct=0.0), c1m=c1m)
        pos = self.position(bot, side="LONG", entry=3000.0, stop=2970.0,
                            qty=0.001, opened=m0)
        before = dict(pos)
        balance = bot.b.balance()
        plan = bot.b.evaluate(pos)
        self.assertEqual(plan["reason"], "stop loss")
        self.assertEqual(dict(pos), before, "evaluate() mutated the position")
        self.assertEqual(bot.b.balance(), balance, "evaluate() moved the balance")
        # book() is the half that changes things
        res = bot.b.book(pos, plan["exit"], plan["reason"])
        self.assertLess(bot.b.balance(), balance)
        self.assertEqual(res["reason"], "stop loss")


# --------------------------------------- 7. bounded timeouts and retries

class TestBoundedNetworkBudget(unittest.TestCase):
    def test_the_client_uses_separate_connect_and_read_timeouts(self):
        c = tb.Binance(cfg(http_connect_timeout_sec=3, http_read_timeout_sec=7),
                       base=tb.LIVE_BASE, public_only=True)
        self.assertEqual(c.timeout, (3.0, 7.0))
        self.assertEqual(c.tries, cfg()["http_retries"])

    def test_a_hanging_endpoint_is_given_up_on_within_a_bounded_time(self):
        conf = cfg(http_connect_timeout_sec=0.05, http_read_timeout_sec=0.05,
                   http_retries=2)
        c = tb.Binance(conf, base=tb.LIVE_BASE, public_only=True)
        attempts = {"n": 0}

        class Hang:
            def request(self, *a, **k):
                attempts["n"] += 1
                raise TimeoutError("connect timed out")

        c.session = Hang()
        t0 = time.perf_counter()
        with self.assertRaises(TimeoutError):
            c._req("GET", "/fapi/v1/ticker/price", {"symbol": "ETHUSDT"})
        ms = (time.perf_counter() - t0) * 1000
        self.assertEqual(attempts["n"], 2, "the retry budget was not honoured")
        self.assertLess(ms, 3000, f"a dead endpoint cost {ms:.0f}ms")

    def test_a_timeout_is_counted_for_health(self):
        perf = tb.Perf()
        conf = cfg(http_retries=1)
        c = tb.Binance(conf, base=tb.LIVE_BASE, public_only=True, perf=perf)

        class Hang:
            def request(self, *a, **k):
                raise TimeoutError("connect timed out")

        c.session = Hang()
        with self.assertRaises(TimeoutError):
            c._req("GET", "/fapi/v1/time")
        self.assertEqual(perf.snapshot()[1]["api_timeouts"], 1)

    def test_a_retry_after_header_cannot_park_the_engine(self):
        conf = cfg(http_retries=2)
        c = tb.Binance(conf, base=tb.LIVE_BASE, public_only=True)
        slept = []
        real_sleep = tb.time.sleep
        tb.time.sleep = lambda s: slept.append(s)

        class Limited:
            def __init__(self):
                self.n = 0

            def request(self, *a, **k):
                self.n += 1

                class R:
                    status_code = 429
                    headers = {"Retry-After": "600"}
                    text = "{}"

                    @staticmethod
                    def json():
                        return {"code": -1003, "msg": "too many"}
                return R()

        c.session = Limited()
        try:
            with self.assertRaises(tb.ApiError):
                c._req("GET", "/fapi/v1/klines")
        finally:
            tb.time.sleep = real_sleep
        self.assertTrue(slept)
        self.assertLessEqual(max(slept), 5, f"slept for {max(slept)}s on a 429")

    def test_a_post_is_still_never_retried(self):
        c = tb.Binance(cfg(), base=tb.LIVE_BASE, public_only=False)
        attempts = {"n": 0}

        class Hang:
            def request(self, *a, **k):
                attempts["n"] += 1
                raise TimeoutError("timed out")

        c.session = Hang()
        with self.assertRaises(TimeoutError):
            c._req("POST", "/fapi/v1/order", {"symbol": "ETHUSDT"}, signed=True)
        self.assertEqual(attempts["n"], 1, "an order request was retried")

    def test_the_telegram_timeout_comes_from_the_config(self):
        import test_trading_bot as T
        old = tb.requests
        tb.requests = T.FakeRequests()
        try:
            n = tb.Notifier(cfg(telegram_timeout_sec=4.5))
            self.assertEqual(n.timeout, 4.5)
        finally:
            tb.requests = old


# ------------------------------------------------ 8. the timing report

class TestPerfReporting(Timed):
    def test_operations_are_timed_and_averaged(self):
        perf = tb.Perf({"cmd": 10})
        for ms in (5.0, 15.0, 25.0):
            perf.record("cmd /status", ms)
        ops, counters = perf.snapshot()
        o = ops["cmd /status"]
        self.assertEqual(o["n"], 3)
        self.assertEqual(o["max_ms"], 25.0)
        self.assertEqual(o["last_ms"], 25.0)
        self.assertEqual(o["slow"], 2, "slow operations were not flagged")
        self.assertEqual(perf.slowest(), ("cmd /status", 25.0))

    def test_the_timer_context_manager_records(self):
        perf = tb.Perf()
        with perf.timer("http /test") as t:
            time.sleep(0.01)
        self.assertGreater(t.ms, 5)
        self.assertEqual(perf.snapshot()[0]["http /test"]["n"], 1)

    def test_counters_are_reported(self):
        perf = tb.Perf()
        perf.bump("api_timeouts", 3)
        perf.bump("telegram_errors")
        text = "\n".join(perf.report_lines())
        self.assertIn("API timeouts: 3", text)
        self.assertIn("Telegram errors: 1", text)

    def test_health_reports_command_and_cycle_timings(self):
        bot, n, _, _ = self.build()
        bot.dispatch("/risk", [])
        bot.run_command({"id": 1, "cmd": "/status", "args": []})
        bot.step()
        bot.dispatch("/health", [])
        text = n.last()
        self.assertIn("Last trading cycle", text)
        self.assertIn("cmd /status", text)
        self.assertIn("API timeouts", text)
        self.assertIn("Market-data cache", text)
        self.assertRegex(text, r"\d+ cycles")

    def test_end_to_end_latency_is_measured_when_telegram_dates_it(self):
        bot, n, _, _ = self.build()
        sent = tb.now_ms() - 4000
        bot.run_command({"id": 7, "cmd": "/status", "args": [],
                         "sent_ms": sent, "received_ms": tb.now_ms()})
        ops, _ = bot.perf.snapshot()
        self.assertIn("cmd end-to-end latency", ops)
        self.assertGreater(ops["cmd end-to-end latency"]["last_ms"], 3000)

    def test_the_health_report_contains_no_secrets(self):
        conf = cfg(telegram_token=FAKE_TOKEN,
                   telegram_chat_id="999888777",
                   api_key="KEY1234567890", api_secret="SECRET1234567890")
        bot, n, _, _ = self.build(conf=conf)
        bot.step()
        bot.dispatch("/health", [])
        for secret in (FAKE_TOKEN.split(":", 1)[1], "999888777",
                       "KEY1234567890", "SECRET1234567890"):
            self.assertNotIn(secret, n.last())

    def test_a_read_only_command_does_not_rewrite_the_state_file(self):
        bot, n, _, _ = self.build()
        tb.save_state(bot.s, self.state_path)
        before = os.stat(self.state_path).st_mtime_ns
        time.sleep(0.01)
        for cmd in ["/status", "/risk", "/balance", "/history", "/performance"]:
            bot.run_command({"id": hash(cmd) % 10000, "cmd": cmd, "args": []})
        self.assertEqual(os.stat(self.state_path).st_mtime_ns, before,
                         "a read-only command rewrote the state file")
        bot.run_command({"id": 999, "cmd": "/paper_off", "args": []})
        self.assertNotEqual(os.stat(self.state_path).st_mtime_ns, before)


# ------------------------------------------------ 9. the journal cache

class TestJournalCache(TempProject):
    def test_the_journal_is_only_reparsed_when_it_changes(self):
        path = os.path.join(self.tmp, "trades.csv")
        import test_journal as TJ
        tb.journal(TJ.row("a", 1.0), path)
        first = tb.load_journal(path)
        parses = {"n": 0}
        real = tb.csv.DictReader

        class Counting(real):
            def __init__(self, *a, **k):
                parses["n"] += 1
                super().__init__(*a, **k)

        tb.csv.DictReader = Counting
        try:
            for _ in range(5):
                tb.load_journal(path)
            self.assertEqual(parses["n"], 0, "the journal was re-parsed")
            tb.journal(TJ.row("b", -1.0), path)
            again = tb.load_journal(path)
            self.assertEqual(parses["n"], 1, "a changed journal was not re-read")
        finally:
            tb.csv.DictReader = real
        self.assertEqual(len(first), 1)
        self.assertEqual(len(again), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
