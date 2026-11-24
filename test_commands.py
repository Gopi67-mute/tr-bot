#!/usr/bin/env python3
"""Telegram remote-control test suite.

Run with:   python -m unittest -v test_commands

Covers authorisation, duplicate suppression, every command in /help, the reset
confirmation workflow, and the rule that no response may ever contain a token,
a chat id or any other secret.

Nothing here touches the network: `tb.requests` is replaced by a scripted fake
for the Notifier tests, and the Bot tests drive `dispatch()` directly or
through a fake command source.
"""

import io
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import trading_bot as tb  # noqa: E402
from test_trading_bot import (FAKE_TOKEN, H4, HOUR, MIN, RULES,  # noqa: E402
                              FakeClient, FakeRequests, TempProject, candles,
                              cfg, signal_series_long)

TOKEN = FAKE_TOKEN           # synthetic; see test_trading_bot.FAKE_TOKEN
CHAT = "4242424242"          # not a real chat id


class QueueNotifier:
    """A notifier whose `commands()` serves scripted update dicts."""

    def __init__(self, queued=None):
        self.sent = []
        self.queued = list(queued or [])

    def send(self, text):
        self.sent.append(text)

    def commands(self):
        out, self.queued = self.queued, []
        return out

    def health_text(self):
        return "Telegram: fake"

    def joined(self):
        return "\n".join(self.sent)

    def last(self):
        return self.sent[-1] if self.sent else ""


def update(uid, cmd, args=None):
    return {"id": uid, "text": " ".join([cmd] + (args or [])),
            "cmd": cmd, "args": args or []}


class BotCase(TempProject):
    """A Bot with a fake exchange and a scripted command queue."""

    def build(self, conf=None, state=None, queued=None, price=3000.0,
              enabled=True, c1m=None, signal=False):
        conf = conf or cfg()
        state = {"paper_balance": 22.0} if state is None else state
        state.setdefault("paper_enabled", enabled)
        if signal:
            c1, c4 = signal_series_long()
            price = c1[-1]["c"]
        else:
            c1, c4 = candles(60, 0, HOUR, 3000.0), candles(60, 0, H4, 3000.0)
        client = FakeClient(c1=c1, c4=c4, price=price, c1m=c1m or [])
        n = QueueNotifier(queued)
        broker = tb.PaperBroker(client, conf, state)
        bot = tb.Bot(conf, client, broker, n, state, self.state_path)
        return bot, n, client

    def position(self, bot, side="LONG", entry=3000.0, stop=2970.0, qty=0.001):
        bot.s["position"] = {
            "symbol": "ETHUSDT", "side": side, "qty": qty, "entry": entry,
            "stop": stop, "initial_stop": stop,
            "target": tb.target_for(side, entry, stop, 2.0),
            "opened": tb.now_ms(), "checked": tb.now_ms(),
            "trade_id": "testtrade01", "planned_risk": 0.11,
            "strategy": tb.STRATEGY_NAME, "market_condition": "LONG bias; stop 1.00%",
            "entry_slippage": 0.0, "equity_before": 22.0}
        return bot.s["position"]


# -------------------------------------------------------- 1. authorisation

class TestAuthorisation(unittest.TestCase):
    def setUp(self):
        self._requests, self._stdout = tb.requests, sys.stdout
        sys.stdout = io.StringIO()

    def tearDown(self):
        tb.requests = self._requests
        self.out = sys.stdout.getvalue()
        sys.stdout = self._stdout

    def notifier(self, fake, chat=CHAT):
        tb.requests = fake
        return tb.Notifier(cfg(telegram_token=TOKEN, telegram_chat_id=chat))

    @staticmethod
    def msg(uid, chat_id, text, key="message"):
        return {"update_id": uid, key: {"chat": {"id": chat_id}, "text": text}}

    def test_only_the_configured_chat_id_is_obeyed(self):
        fake = FakeRequests()
        n = self.notifier(fake)
        fake.updates = [self.msg(1, CHAT, "/paper_on"),
                        self.msg(2, int(CHAT), "/halt"),
                        self.msg(3, "9999999", "/paper_on"),
                        self.msg(4, -1001234567, "/halt"),
                        self.msg(5, CHAT + "0", "/resetpaper"),
                        self.msg(6, "", "/status"),
                        self.msg(7, None, "/status")]
        got = n.commands()
        self.assertEqual([c["cmd"] for c in got], ["/paper_on", "/halt"])

    def test_an_unauthorised_command_gets_no_reply(self):
        """Silence, not a refusal: a stranger learns nothing about the bot."""
        fake = FakeRequests()
        n = self.notifier(fake)
        fake.updates = [self.msg(1, "9999999", "/status")]
        self.assertEqual(n.commands(), [])
        self.assertEqual(fake.posts, [], "the bot replied to an unauthorised chat")
        self.assertEqual(n.refused, 1)

    def test_an_empty_chat_id_refuses_everything(self):
        fake = FakeRequests()
        n = self.notifier(fake, chat="")
        fake.updates = [self.msg(1, CHAT, "/halt"), self.msg(2, "", "/halt")]
        self.assertEqual(n.commands(), [])

    def test_the_offset_prevents_a_replay_within_a_run(self):
        fake = FakeRequests()
        n = self.notifier(fake)
        fake.updates = [self.msg(11, CHAT, "/status")]
        self.assertEqual(len(n.commands()), 1)
        self.assertEqual(n.offset, 12)
        self.assertEqual(n.commands(), [])

    def test_commands_queued_while_the_engine_was_off_are_dropped(self):
        """Otherwise a /paper_on sent overnight would arm the bot unattended."""
        fake = FakeRequests(updates=[self.msg(5, CHAT, "/paper_on")])
        n = self.notifier(fake)
        self.assertEqual(n.offset, 6)
        self.assertEqual(n.commands(), [])

    def test_parse_command_normalises_case_and_bot_suffixes(self):
        self.assertEqual(tb.parse_command("/Paper_On@mybot yes"),
                         ("/paper_on", ["yes"]))
        self.assertEqual(tb.parse_command("/STATUS"), ("/status", []))
        self.assertIsNone(tb.parse_command("hello"))
        self.assertIsNone(tb.parse_command(""))
        self.assertIsNone(tb.parse_command(None))

    def test_no_secret_appears_in_a_health_report(self):
        fake = FakeRequests()
        n = self.notifier(fake)
        n.send("hello")
        text = n.health_text()
        self.assertNotIn(TOKEN, text)
        self.assertNotIn(TOKEN.split(":")[1], text)
        self.assertNotIn(CHAT, text)
        self.assertIn("chat id configured", text)


# ------------------------------------------------- 2. duplicate suppression

class TestDuplicates(BotCase):
    def test_the_same_update_id_is_acted_on_once(self):
        bot, n, _ = self.build(queued=[update(1, "/paper_off"),
                                       update(1, "/paper_off")])
        bot.handle_commands()
        self.assertEqual(len([m for m in n.sent if "Paper trading OFF" in m]), 1)

    def test_a_repeat_after_a_restart_is_ignored(self):
        bot, n, client = self.build(queued=[update(7, "/halt")])
        bot.handle_commands()
        self.assertTrue(bot.risk.is_halted())
        halts = len([m for m in n.sent if "manual /halt" in m])
        tb.save_state(bot.s, self.state_path)
        # the same update is redelivered to a freshly started engine
        bot2, n2, _ = self.build(state=tb.load_state(self.state_path),
                                 queued=[update(7, "/halt")])
        bot2.handle_commands()
        self.assertEqual(len([m for m in n2.sent if "manual /halt" in m]), 0,
                         "a command was executed twice across a restart")
        self.assertEqual(halts, 1)

    def test_the_seen_list_is_bounded(self):
        bot, _, _ = self.build()
        for i in range(500):
            bot._seen(i)
        self.assertLessEqual(len(bot.s["seen_updates"]), 300)
        self.assertIn(499, bot.s["seen_updates"])

    def test_a_command_without_an_id_is_still_run(self):
        """The plain-string path used by the older fake notifier."""
        bot, n, _ = self.build(queued=["/paper_off", "/paper_off"])
        bot.handle_commands()
        self.assertEqual(len([m for m in n.sent if "Paper trading OFF" in m]), 2)

    def test_a_failing_command_does_not_stop_the_rest(self):
        bot, n, _ = self.build(queued=[update(1, "/positions"),
                                       update(2, "/paper_off")])
        bot.b.mark_to_market = lambda *a, **k: 1 / 0
        self.position(bot)
        bot.handle_commands()
        self.assertIn("Paper trading OFF", n.joined())


# ------------------------------------------------------ 3. standby control

class TestStandby(BotCase):
    def test_paper_on_arms_entries(self):
        bot, n, _ = self.build(enabled=False, queued=[update(1, "/paper_on")])
        bot.handle_commands()
        self.assertTrue(bot.s["paper_enabled"])
        self.assertFalse(bot.s["paused"])
        self.assertIn("Paper trading ON", n.joined())

    def test_paper_off_blocks_entries_but_keeps_managing_a_position(self):
        m0 = (tb.now_ms() // MIN) * MIN - 20 * MIN
        c1m = [{"t": m0 + MIN, "o": 2999.0, "h": 3000.0, "l": 2960.0, "c": 2965.0,
                "T": m0 + 2 * MIN - 1}]
        bot, n, _ = self.build(conf=cfg(slippage_pct=0.0), c1m=c1m, price=2965.0)
        p = self.position(bot)
        p["opened"] = m0
        bot.dispatch("/paper_off", [])
        self.assertFalse(bot.s["paper_enabled"])
        self.assertIsNotNone(bot.s["position"], "/paper_off closed the position")
        self.assertIn("keeps its stop", n.joined())
        # the open position is still taken to its exit by the loop
        bot.step()
        self.assertIsNone(bot.s["position"], "the stop was not managed")
        self.assertEqual(tb.load_journal(mode="PAPER")[0]["exit_reason"], "stop loss")

    def test_paper_off_then_on_does_not_reopen_the_same_candle(self):
        bot, n, _ = self.build(signal=True, conf=cfg(symbols=["ETHUSDT"]))
        bot.look_for_entry()
        first = bot.s["position"]
        self.assertIsNotNone(first, n.joined())
        bot.dispatch("/paper_off", [])
        bot.dispatch("/paper_on", [])
        bot.look_for_entry()
        self.assertEqual(bot.s["position"]["trade_id"], first["trade_id"])

    def test_halt_closes_and_blocks(self):
        bot, n, _ = self.build()
        self.position(bot)
        bot.dispatch("/halt", [])
        self.assertIsNone(bot.s["position"])
        self.assertTrue(bot.risk.is_halted())
        self.assertFalse(bot.s["paper_enabled"])
        self.assertIn("🚨", n.joined())

    def test_paper_on_is_refused_while_halted(self):
        bot, n, _ = self.build()
        bot.dispatch("/halt", [])
        bot.dispatch("/paper_on", [])
        self.assertFalse(bot.s["paper_enabled"])
        self.assertIn("Refused", n.last())
        self.assertIn("/clearhalt CONFIRM", n.last())

    def test_the_pause_and_resume_aliases_still_work(self):
        bot, _, _ = self.build(enabled=True)
        bot.dispatch("/pause", [])
        self.assertFalse(bot.s["paper_enabled"])
        bot.dispatch("/resume", [])
        self.assertTrue(bot.s["paper_enabled"])

    def test_close_closes_only_when_there_is_a_position(self):
        bot, n, _ = self.build(price=2990.0, conf=cfg(slippage_pct=0.0))
        bot.dispatch("/close", [])
        self.assertIn("No open position", n.last())
        self.position(bot)
        bot.dispatch("/close", [])
        self.assertIsNone(bot.s["position"])
        self.assertEqual(tb.load_journal(mode="PAPER")[0]["exit_reason"],
                         "closed manually")


# -------------------------------------------------- 4. the reset workflow

class TestResetWorkflow(BotCase):
    def journal_a_trade(self, bot):
        self.position(bot)
        bot._finish({"exit": 2970.0, "pnl": -0.11, "fees": 0.003,
                     "slippage": 0.001, "reason": "stop loss"})

    def test_a_bare_resetpaper_only_asks(self):
        bot, n, _ = self.build(state={"paper_balance": 19.0})
        bot.dispatch("/resetpaper", [])
        self.assertEqual(bot.s["paper_balance"], 19.0, "the balance was reset")
        self.assertIn("/resetpaper CONFIRM", n.last())
        self.assertIsNotNone(bot.s["pending_reset"])

    def test_confirming_resets_the_virtual_account(self):
        bot, n, _ = self.build(state={"paper_balance": 19.0, "equity_peak": 25.0,
                                      "trades_today": 3, "losses_today": 2})
        bot.dispatch("/resetpaper", [])
        bot.dispatch("/resetpaper", ["CONFIRM"])
        self.assertEqual(bot.s["paper_balance"], 22.0)
        self.assertEqual(bot.s["equity_peak"], 22.0)
        self.assertEqual(bot.s["trades_today"], 0)
        self.assertEqual(bot.s["losses_today"], 0)
        self.assertFalse(bot.s["paper_enabled"], "a reset must not arm trading")
        self.assertTrue(bot.s["resets"])
        self.assertIn("reset to 22.00", n.last())

    def test_a_reset_never_deletes_the_journal(self):
        bot, n, _ = self.build()
        self.journal_a_trade(bot)
        before = tb.load_journal(mode="PAPER")
        self.assertEqual(len(before), 1)
        bot.dispatch("/resetpaper", [])
        bot.dispatch("/resetpaper", ["CONFIRM"])
        after = tb.load_journal(mode="PAPER")
        self.assertEqual([r["trade_id"] for r in after],
                         [r["trade_id"] for r in before],
                         "the permanent trade journal was lost")
        self.assertTrue(os.path.exists(tb.JOURNAL_FILE))

    def test_performance_still_counts_pre_reset_trades(self):
        bot, n, _ = self.build()
        self.journal_a_trade(bot)
        bot.dispatch("/resetpaper", [])
        bot.dispatch("/resetpaper", ["CONFIRM"])
        bot.dispatch("/performance", [])
        self.assertIn("Closed trades: 1", n.last())

    def test_a_reset_is_refused_while_a_position_is_open(self):
        bot, n, _ = self.build()
        self.position(bot)
        bot.dispatch("/resetpaper", ["CONFIRM"])
        self.assertIsNotNone(bot.s["position"])
        self.assertEqual(bot.s["paper_balance"], 22.0)
        self.assertIn("Refusing to reset", n.last())

    def test_a_stale_confirmation_is_refused(self):
        bot, n, _ = self.build(state={"paper_balance": 19.0})
        bot.dispatch("/resetpaper", [])
        bot.s["pending_reset"] = {"asked_ms": tb.now_ms() - 10 * 60_000}
        bot.dispatch("/resetpaper", ["CONFIRM"])
        self.assertEqual(bot.s["paper_balance"], 19.0)
        self.assertIn("expired", n.last())

    def test_a_confirmation_out_of_the_blue_is_refused(self):
        bot, n, _ = self.build(state={"paper_balance": 19.0})
        bot.dispatch("/resetpaper", ["CONFIRM"])
        self.assertEqual(bot.s["paper_balance"], 19.0)
        self.assertIn("expired", n.last())

    def test_a_reset_clears_a_halt_but_leaves_trading_off(self):
        bot, n, _ = self.build()
        bot.dispatch("/halt", [])
        bot.dispatch("/resetpaper", [])
        bot.dispatch("/resetpaper", ["CONFIRM"])
        self.assertFalse(bot.risk.is_halted())
        self.assertFalse(bot.s["paper_enabled"])


# --------------------------------------------------- 5. every command replies

class TestEveryCommand(BotCase):
    COMMANDS = ["/help", "/status", "/paper_on", "/paper_off", "/positions",
                "/performance", "/balance", "/history", "/risk", "/health",
                "/halt", "/clearhalt", "/resetpaper", "/close", "/nonsense"]

    def test_every_documented_command_answers(self):
        for cmd in self.COMMANDS:
            with self.subTest(cmd=cmd):
                bot, n, _ = self.build()
                bot.dispatch(cmd, [])
                self.assertTrue(n.sent, f"{cmd} produced no reply")
                self.assertGreater(len(n.last()), 10, f"{cmd} said almost nothing")

    def test_help_lists_every_command(self):
        bot, n, _ = self.build()
        bot.dispatch("/help", [])
        for cmd in ["/status", "/paper_on", "/paper_off", "/positions",
                    "/performance", "/balance", "/history", "/risk", "/halt",
                    "/resetpaper", "/health"]:
            self.assertIn(cmd, n.last(), f"{cmd} is missing from /help")
        self.assertIn("PAPER", n.last())

    def test_status_reports_mode_market_uptime_and_risk(self):
        bot, n, _ = self.build()
        bot.dispatch("/status", [])
        text = n.last()
        self.assertIn("PAPER", text)
        self.assertIn("Entries: ON", text)
        self.assertIn("Uptime", text)
        self.assertIn("ETHUSDT", text)
        self.assertIn("drawdown", text)
        self.assertIn("trades 0/3", text)

    def test_positions_shows_the_full_picture(self):
        bot, n, _ = self.build(price=3020.0)
        self.position(bot)
        bot.dispatch("/positions", [])
        text = n.last()
        for needle in ["LONG", "ETHUSDT", "Entry", "Stop", "target",
                       "Planned risk", "unrealised", "testtrade01"]:
            self.assertIn(needle, text)

    def test_positions_with_nothing_open(self):
        bot, n, _ = self.build()
        bot.dispatch("/positions", [])
        self.assertIn("No open position", n.last())

    def test_balance_reports_balance_equity_available_and_drawdown(self):
        bot, n, _ = self.build()
        bot.s["equity_peak"] = 25.0
        bot.dispatch("/balance", [])
        text = n.last()
        self.assertIn("Balance: 22.0000 USDT", text)
        self.assertIn("Equity", text)
        self.assertIn("Available", text)
        self.assertIn("Peak equity: 25.0000", text)
        self.assertIn("drawdown 12.00%", text)

    def test_risk_lists_every_limit_and_the_remaining_allowance(self):
        bot, n, _ = self.build()
        bot.dispatch("/risk", [])
        text = n.last()
        for needle in ["Risk per trade: 0.5%", "Daily loss limit: 2.0%",
                       "Max account drawdown: 5.0%", "Trades today: 0/3",
                       "left to risk", "Mandatory stop-loss",
                       "no martingale", "slippage allowance"]:
            self.assertIn(needle, text)

    def test_performance_with_an_empty_journal_says_so(self):
        bot, n, _ = self.build()
        bot.dispatch("/performance", [])
        self.assertIn("No closed trades yet", n.last())

    def test_history_with_an_empty_journal_says_so(self):
        bot, n, _ = self.build()
        bot.dispatch("/history", [])
        self.assertIn("No completed trades", n.last())

    def test_history_lists_recent_trades(self):
        bot, n, _ = self.build()
        for i in range(3):
            self.position(bot)
            bot.s["position"]["trade_id"] = f"trade{i}"
            bot._finish({"exit": 3100.0, "pnl": 0.2, "fees": 0.003,
                         "slippage": 0.001, "reason": "take profit"})
        bot.dispatch("/history", [])
        self.assertIn("take profit", n.last())
        self.assertEqual(n.last().count("✅"), 3)

    def test_health_covers_data_telegram_and_the_engine(self):
        bot, n, _ = self.build()
        bot.dispatch("/health", [])
        text = n.last()
        for needle in ["Engine:", "Market data", "ETHUSDT: price", "candles OK",
                       "Telegram", "State file", "Journal", "Risk engine",
                       "Last trading cycle", "API timeouts", "Command worker"]:
            self.assertIn(needle, text)

    def test_health_still_answers_when_the_exchange_is_unreachable(self):
        """A health report is exactly what you want when nothing else works."""
        bot, n, client = self.build()
        client.fail = OSError("connection reset")
        bot.dispatch("/health", [])
        text = n.last()
        self.assertIn("Engine:", text)
        self.assertIn("none fetched yet", text)
        self.assertIn("no recent quote", text)


# ------------------------------------------------------- 6. no secrets leak

class TestNoSecretsLeak(BotCase):
    def test_no_response_contains_a_token_or_a_chat_id(self):
        conf = cfg(telegram_token=TOKEN, telegram_chat_id=CHAT,
                   api_key="REALKEY1234567890", api_secret="REALSECRET1234567890")
        bot, n, client = self.build(conf=conf)
        self.position(bot)
        for cmd in TestEveryCommand.COMMANDS + ["/clearhalt", "/resetpaper"]:
            bot.dispatch(cmd, ["CONFIRM"])
        client.fail = OSError(f"HTTPSConnectionPool /bot{TOKEN}/sendMessage failed; "
                              f"api_secret=REALSECRET1234567890")
        bot.dispatch("/health", [])
        bot.dispatch("/status", [])
        blob = n.joined()
        for secret in (TOKEN, TOKEN.split(":")[1], "REALKEY1234567890",
                       "REALSECRET1234567890"):
            self.assertNotIn(secret, blob, "a secret reached a Telegram reply")

    def test_an_exception_inside_a_command_is_scrubbed(self):
        conf = cfg(telegram_token=TOKEN, api_secret="REALSECRET1234567890")
        bot, n, _ = self.build(conf=conf, queued=[update(1, "/positions")])

        def boom(*a, **k):
            raise RuntimeError("failed with api_secret=REALSECRET1234567890")

        bot.position_text = boom
        bot.handle_commands()
        self.assertIn("failed", n.joined())
        self.assertNotIn("REALSECRET1234567890", n.joined())


if __name__ == "__main__":
    unittest.main(verbosity=2)
