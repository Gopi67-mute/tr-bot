#!/usr/bin/env python3
"""Test suite for telegram_setup.py and the Telegram diagnostics in trading_bot.py.

Run with:   python -m unittest -v test_telegram_setup

Entirely offline. `FakeTelegram` stands in for the HTTP layer, so no request
ever leaves this process, no real token exists and no Binance code path - no
client, no broker, no order - is constructed anywhere in this file.
"""

import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import telegram_setup as ts  # noqa: E402
import trading_bot as tb  # noqa: E402

TOKEN = "123456789:AAEhBOweik6ad9r_TESTTOKENvalue0000000"
BAD_SHAPE = "PASTE_FROM_BOTFATHER"


class Resp:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code, self._payload, self.text = status, payload, text

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class FakeTelegram:
    """A scripted api.telegram.org. Queues one reply per method name."""

    def __init__(self, **replies):
        # each value is a list of Resp / Exception, consumed in order
        self.replies = {k: list(v) for k, v in replies.items()}
        self.calls = []

    def _key(self, url):
        return url.rsplit("/", 1)[-1]

    def get(self, url, params=None, timeout=20, **kw):
        method = self._key(url)
        self.calls.append({"method": method, "url": url, "params": params or {},
                           "timeout": timeout})
        queue = self.replies.get(method)
        if not queue:
            return Resp(200, {"ok": True, "result": []})
        reply = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(reply, Exception):
            raise reply
        return reply

    post = get

    def params_for(self, method):
        return [c["params"] for c in self.calls if c["method"] == method]


def ok(result):
    return Resp(200, {"ok": True, "result": result})


def err(status, description):
    return Resp(status, {"ok": False, "description": description})


ME = {"id": 123456789, "is_bot": True, "first_name": "PaperBot",
      "username": "my_paper_bot"}


def update(uid, chat_id, text="/start", chat_type="private", first_name="Palani"):
    return {"update_id": uid,
            "message": {"chat": {"id": chat_id, "type": chat_type,
                                 "first_name": first_name},
                        "text": text}}


class Quiet(unittest.TestCase):
    """Captures stdout so assertions can be made about what was printed."""

    def setUp(self):
        self._stdout = sys.stdout
        sys.stdout = io.StringIO()
        self.lines = []

    def tearDown(self):
        self.out = sys.stdout.getvalue()
        sys.stdout = self._stdout

    def say(self, text):
        self.lines.append(str(text))

    @property
    def printed(self):
        return "\n".join(self.lines) + sys.stdout.getvalue()


# ------------------------------------------------------- 1. token hygiene

class TestTokenHygiene(unittest.TestCase):

    def test_a_good_token_has_no_problem(self):
        self.assertIsNone(ts.token_problem(TOKEN))

    def test_the_placeholder_is_rejected(self):
        self.assertIn("placeholder", ts.token_problem(BAD_SHAPE))

    def test_an_empty_token_is_rejected(self):
        self.assertIsNotNone(ts.token_problem(""))
        self.assertIsNotNone(ts.token_problem(None))

    def test_a_token_without_a_colon_is_explained(self):
        self.assertIn("no ':'", ts.token_problem("1234567890abcdefghijklmnop"))

    def test_a_truncated_secret_is_explained(self):
        self.assertIn("truncated", ts.token_problem("123456789:AAEhBOweik"))

    def test_a_non_numeric_bot_id_is_explained(self):
        self.assertIn("numeric bot id",
                      ts.token_problem("notanid:AAEhBOweik6ad9r_TESTTOKENvalue0000000"))

    def test_illegal_characters_are_explained(self):
        bad = "123456789:AAEhBOweik6ad9r TESTTOKEN!!!value00000"
        self.assertIsNotNone(ts.token_problem(bad))

    def test_clean_token_strips_quotes_and_whitespace(self):
        self.assertEqual(ts.clean_token(f'  "{TOKEN}" \n'), TOKEN)

    def test_clean_token_accepts_a_pasted_api_url(self):
        self.assertEqual(
            ts.clean_token(f"https://api.telegram.org/bot{TOKEN}/getMe"), TOKEN)

    def test_clean_token_strips_a_bot_prefix(self):
        self.assertEqual(ts.clean_token(f"bot{TOKEN}"), TOKEN)

    def test_mask_token_never_shows_the_secret(self):
        masked = ts.mask_token(TOKEN)
        self.assertNotIn(TOKEN.split(":", 1)[1], masked)
        self.assertIn("123456789", masked)        # the bot id is not a secret

    def test_mask_token_survives_junk(self):
        self.assertEqual(ts.mask_token(""), "(empty)")
        self.assertEqual(ts.mask_token("nocolon"), "***")

    def test_chat_id_problems(self):
        self.assertIsNone(ts.chat_id_problem("4242"))
        self.assertIsNone(ts.chat_id_problem("-1001234567890"))
        self.assertIsNotNone(ts.chat_id_problem("PASTE_YOUR_CHAT_ID"))
        self.assertIsNotNone(ts.chat_id_problem(""))


# ------------------------------------------------------- 2. getMe validation

class TestValidateToken(unittest.TestCase):

    def test_a_good_token_returns_the_bot_identity(self):
        fake = FakeTelegram(getMe=[ok(ME)])
        me = ts.validate_token(TOKEN, http=fake)
        self.assertEqual(me["username"], "my_paper_bot")
        self.assertEqual(fake.calls[0]["method"], "getMe")

    def test_a_malformed_token_is_rejected_without_any_request(self):
        fake = FakeTelegram()
        with self.assertRaises(ts.SetupError) as cm:
            ts.validate_token(BAD_SHAPE, http=fake)
        self.assertEqual(fake.calls, [], "a bogus token was sent to Telegram")
        self.assertIn("404", str(cm.exception),
                      "the 404 cause should be explained up front")

    def test_404_is_explained_as_a_wrong_token(self):
        fake = FakeTelegram(getMe=[err(404, "Not Found")])
        with self.assertRaises(ts.SetupError) as cm:
            ts.validate_token(TOKEN, http=fake)
        msg = str(cm.exception)
        self.assertIn("404", msg)
        self.assertIn("URL path", msg)
        self.assertIn("placeholder", msg)

    def test_401_is_explained_as_a_revoked_token(self):
        fake = FakeTelegram(getMe=[err(401, "Unauthorized")])
        with self.assertRaises(ts.SetupError) as cm:
            ts.validate_token(TOKEN, http=fake)
        msg = str(cm.exception)
        self.assertIn("401", msg)
        self.assertIn("BotFather", msg)

    def test_429_is_explained_as_rate_limiting(self):
        fake = FakeTelegram(getMe=[err(429, "Too Many Requests")])
        with self.assertRaises(ts.SetupError) as cm:
            ts.validate_token(TOKEN, http=fake)
        self.assertIn("429", str(cm.exception))

    def test_a_server_error_is_not_blamed_on_the_config(self):
        fake = FakeTelegram(getMe=[err(503, "Service Unavailable")])
        with self.assertRaises(ts.SetupError) as cm:
            ts.validate_token(TOKEN, http=fake)
        self.assertIn("not a configuration problem", str(cm.exception))

    def test_a_timeout_is_explained_as_a_blocked_network(self):
        class Timeout(Exception):
            pass
        fake = FakeTelegram(getMe=[Timeout("Read timed out after 20s")])
        with self.assertRaises(ts.SetupError) as cm:
            ts.validate_token(TOKEN, http=fake)
        msg = str(cm.exception)
        self.assertIn("did not answer in time", msg)
        self.assertIn("VPN", msg)

    def test_a_dns_failure_is_explained(self):
        fake = FakeTelegram(getMe=[OSError(
            "Failed to resolve 'api.telegram.org' ([Errno 11001] getaddrinfo failed)")])
        with self.assertRaises(ts.SetupError) as cm:
            ts.validate_token(TOKEN, http=fake)
        self.assertIn("could not be made at all", str(cm.exception))

    def test_an_error_never_leaks_the_token(self):
        leak = OSError(f"HTTPSConnectionPool: /bot{TOKEN}/getMe failed")
        fake = FakeTelegram(getMe=[leak])
        with self.assertRaises(ts.SetupError) as cm:
            ts.validate_token(TOKEN, http=fake)
        self.assertNotIn(TOKEN, str(cm.exception), "the token leaked into an error")

    def test_a_non_json_error_body_is_still_reported(self):
        fake = FakeTelegram(getMe=[Resp(404, None, "<html>404 Not Found</html>")])
        with self.assertRaises(ts.SetupError) as cm:
            ts.validate_token(TOKEN, http=fake)
        self.assertIn("404", str(cm.exception))

    def test_ok_false_with_a_200_is_still_a_failure(self):
        fake = FakeTelegram(getMe=[Resp(200, {"ok": False,
                                              "description": "weird"})])
        with self.assertRaises(ts.SetupError):
            ts.validate_token(TOKEN, http=fake)


# ------------------------------------------------------- 3. chat id discovery

class TestChatId(Quiet):

    def test_the_private_chat_id_is_found(self):
        fake = FakeTelegram(getUpdates=[ok([]), ok([update(7, 4242)])])
        found = ts.wait_for_chat_id(TOKEN, username="my_paper_bot", wait_sec=30,
                                    poll=0, http=fake, sleep=lambda s: None,
                                    out=self.say)
        self.assertEqual(found["chat_id"], "4242")
        self.assertEqual(found["name"], "Palani")

    def test_a_negative_group_id_is_not_accepted(self):
        clock = iter([0, 1, 2, 99]).__next__
        fake = FakeTelegram(getUpdates=[ok([]),
                                        ok([update(7, -100123, chat_type="group")])])
        with self.assertRaises(ts.SetupError):
            ts.wait_for_chat_id(TOKEN, wait_sec=5, poll=0, http=fake,
                                clock=clock, sleep=lambda s: None, out=self.say)
        self.assertIn("group chat", self.printed)

    def test_stale_updates_are_drained_and_reused(self):
        fake = FakeTelegram(getUpdates=[ok([update(3, 999)])])
        found = ts.wait_for_chat_id(TOKEN, wait_sec=5, poll=0, http=fake,
                                    sleep=lambda s: None, out=self.say)
        self.assertEqual(found["chat_id"], "999")
        self.assertEqual(len(fake.calls), 1, "it should not poll again")

    def test_the_offset_advances_so_updates_are_not_re_read(self):
        fake = FakeTelegram(getUpdates=[ok([update(3, 999, text="hi",
                                                   chat_type="channel")]),
                                        ok([update(4, 4242)])])
        ts.wait_for_chat_id(TOKEN, wait_sec=30, poll=0, http=fake,
                            sleep=lambda s: None, out=self.say)
        offsets = [p.get("offset") for p in fake.params_for("getUpdates")]
        self.assertEqual(offsets[0], None)
        self.assertEqual(offsets[1], 4)

    def test_a_timeout_waiting_for_start_explains_how_to_retry(self):
        clock = iter([0, 1, 2, 3, 100]).__next__
        fake = FakeTelegram(getUpdates=[ok([])])
        with self.assertRaises(ts.SetupError) as cm:
            ts.wait_for_chat_id(TOKEN, username="my_paper_bot", wait_sec=5,
                                poll=0, http=fake, clock=clock,
                                sleep=lambda s: None, out=self.say)
        msg = str(cm.exception)
        self.assertIn("t.me/my_paper_bot", msg)
        self.assertIn("--wait", msg)

    def test_a_409_conflict_is_explained(self):
        fake = FakeTelegram(getUpdates=[err(409, "Conflict: terminated by "
                                                 "other getUpdates request")])
        with self.assertRaises(ts.SetupError) as cm:
            ts.wait_for_chat_id(TOKEN, wait_sec=5, poll=0, http=fake,
                                sleep=lambda s: None, out=self.say)
        self.assertIn("409", str(cm.exception))
        self.assertIn("webhook", str(cm.exception))

    def test_find_private_chat_ignores_junk_updates(self):
        self.assertIsNone(ts.find_private_chat([{}, None, {"message": {}},
                                                {"message": {"chat": {}}}]))

    def test_find_private_chat_takes_the_newest(self):
        found = ts.find_private_chat([update(1, 111), update(2, 222)])
        self.assertEqual(found["chat_id"], "222")


# ------------------------------------------------------- 4. test message

class TestSendMessage(unittest.TestCase):

    def test_a_test_message_is_sent_to_the_right_chat(self):
        fake = FakeTelegram(sendMessage=[ok({"message_id": 1})])
        ts.send_message(TOKEN, "4242", "hello", http=fake)
        params = fake.params_for("sendMessage")[0]
        self.assertEqual(params["chat_id"], "4242")
        self.assertEqual(params["text"], "hello")

    def test_a_bad_chat_id_is_refused_without_a_request(self):
        fake = FakeTelegram()
        with self.assertRaises(ts.SetupError):
            ts.send_message(TOKEN, "PASTE_YOUR_CHAT_ID", "hello", http=fake)
        self.assertEqual(fake.calls, [])

    def test_a_400_is_explained_as_a_chat_that_never_started_the_bot(self):
        fake = FakeTelegram(sendMessage=[err(400, "Bad Request: chat not found")])
        with self.assertRaises(ts.SetupError) as cm:
            ts.send_message(TOKEN, "4242", "hello", http=fake)
        self.assertIn("/start", str(cm.exception))

    def test_a_403_is_explained_as_blocked(self):
        fake = FakeTelegram(sendMessage=[err(403, "Forbidden: bot was blocked")])
        with self.assertRaises(ts.SetupError) as cm:
            ts.send_message(TOKEN, "4242", "hello", http=fake)
        self.assertIn("blocked", str(cm.exception))

    def test_a_long_message_is_truncated_to_the_api_limit(self):
        fake = FakeTelegram(sendMessage=[ok({"message_id": 1})])
        ts.send_message(TOKEN, "4242", "x" * 5000, http=fake)
        self.assertEqual(len(fake.params_for("sendMessage")[0]["text"]), 4096)


# ------------------------------------------------------- 5. saving config

class TestSaveCredentials(unittest.TestCase):

    EXISTING = {
        "mode": "paper",
        "symbols": ["ETHUSDT", "SOLUSDT"],
        "telegram_token": "PASTE_FROM_BOTFATHER",
        "telegram_chat_id": "PASTE_YOUR_CHAT_ID",
        "api_key": "KEEP_THIS_BINANCE_KEY",
        "api_secret": "KEEP_THIS_BINANCE_SECRET",
        "use_testnet": True,
        "i_understand_real_money": False,
        "risk_per_trade_pct": 1.5,
        "max_leverage": 5,
    }

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "config.json")
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.EXISTING, f, indent=2)

    def saved(self):
        with open(self.path, encoding="utf-8") as f:
            return json.load(f)

    def test_the_two_telegram_keys_are_written(self):
        ts.save_credentials(TOKEN, "4242", path=self.path)
        data = self.saved()
        self.assertEqual(data["telegram_token"], TOKEN)
        self.assertEqual(data["telegram_chat_id"], "4242")

    def test_binance_credentials_are_not_touched(self):
        ts.save_credentials(TOKEN, "4242", path=self.path)
        data = self.saved()
        self.assertEqual(data["api_key"], "KEEP_THIS_BINANCE_KEY")
        self.assertEqual(data["api_secret"], "KEEP_THIS_BINANCE_SECRET")

    def test_every_other_setting_survives_unchanged(self):
        ts.save_credentials(TOKEN, "4242", path=self.path)
        data = self.saved()
        for key, value in self.EXISTING.items():
            if key.startswith("telegram_"):
                continue
            self.assertEqual(data[key], value, f"{key} was modified")

    def test_the_chat_id_is_stored_as_a_string(self):
        ts.save_credentials(TOKEN, 4242, path=self.path)
        self.assertIsInstance(self.saved()["telegram_chat_id"], str)

    def test_paper_mode_is_never_promoted_to_live(self):
        ts.save_credentials(TOKEN, "4242", path=self.path)
        data = self.saved()
        self.assertEqual(data["mode"], "paper")
        self.assertFalse(data["i_understand_real_money"])

    def test_the_previous_config_is_backed_up(self):
        _, backup, _ = ts.save_credentials(TOKEN, "4242", path=self.path)
        self.assertTrue(os.path.exists(backup))
        with open(backup, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["telegram_token"], "PASTE_FROM_BOTFATHER")

    def test_a_missing_config_is_created(self):
        fresh = os.path.join(self.dir, "new.json")
        _, backup, mode = ts.save_credentials(TOKEN, "4242", path=fresh)
        self.assertIsNone(backup)
        self.assertEqual(mode, "paper")
        with open(fresh, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["telegram_chat_id"], "4242")

    def test_no_temp_file_is_left_behind(self):
        ts.save_credentials(TOKEN, "4242", path=self.path)
        self.assertFalse(os.path.exists(self.path + ".tmp"))

    def test_a_corrupt_config_is_refused_rather_than_overwritten(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{not json")
        with self.assertRaises(ts.SetupError):
            ts.save_credentials(TOKEN, "4242", path=self.path)
        with open(self.path, encoding="utf-8") as f:
            self.assertEqual(f.read(), "{not json", "the bad file was clobbered")

    def test_a_json_list_is_refused(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("[1, 2, 3]")
        with self.assertRaises(ts.SetupError):
            ts.save_credentials(TOKEN, "4242", path=self.path)

    def test_the_saved_file_is_loadable_by_the_bot(self):
        ts.save_credentials(TOKEN, "4242", path=self.path)
        cfg = tb.load_config(self.path)
        self.assertEqual(cfg["telegram_token"], TOKEN)
        self.assertEqual(cfg["telegram_chat_id"], "4242")
        self.assertEqual(cfg["mode"], "paper")
        tb.validate_config(cfg)              # must not raise


# ------------------------------------------------------- 6. gitignore

class TestGitignore(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def read(self):
        with open(os.path.join(self.dir, ".gitignore"), encoding="utf-8") as f:
            return f.read()

    def test_a_gitignore_is_created_with_config_json(self):
        added = ts.ensure_gitignore(self.dir)
        self.assertIn("config.json", added)
        self.assertIn("config.json", self.read().splitlines())

    def test_state_and_journal_files_are_ignored_too(self):
        ts.ensure_gitignore(self.dir)
        lines = self.read().splitlines()
        self.assertIn("bot_state_*.json", lines)
        self.assertIn("trades.csv", lines)
        self.assertIn("config.json.bak", lines)

    def test_existing_content_is_preserved(self):
        path = os.path.join(self.dir, ".gitignore")
        with open(path, "w", encoding="utf-8") as f:
            f.write("# mine\nnotes.txt\n")
        ts.ensure_gitignore(self.dir)
        body = self.read()
        self.assertIn("notes.txt", body)
        self.assertIn("config.json", body)

    def test_entries_are_not_duplicated(self):
        ts.ensure_gitignore(self.dir)
        self.assertEqual(ts.ensure_gitignore(self.dir), [])
        self.assertEqual(self.read().count("\nconfig.json\n"), 1)

    def test_the_example_config_is_still_committable(self):
        ts.ensure_gitignore(self.dir)
        self.assertNotIn("config.example.json", self.read().splitlines())


# ------------------------------------------------------- 7. token input

class TestReadToken(Quiet):

    def setUp(self):
        super().setUp()
        self._env = os.environ.pop("TELEGRAM_TOKEN", None)

    def tearDown(self):
        if self._env is not None:
            os.environ["TELEGRAM_TOKEN"] = self._env
        else:
            os.environ.pop("TELEGRAM_TOKEN", None)
        super().tearDown()

    def test_the_prompt_is_hidden_and_the_token_is_never_printed(self):
        asked = []

        def ask(prompt):
            asked.append(prompt)
            return TOKEN

        got = ts.read_token(out=self.say, ask=ask)
        self.assertEqual(got, TOKEN)
        self.assertEqual(len(asked), 1)
        self.assertIn("Nothing is echoed", asked[0])
        self.assertNotIn(TOKEN, self.printed, "the token was echoed")

    def test_getpass_is_the_default_reader(self):
        import getpass
        self.assertIs(ts.read_token.__defaults__[-1], getpass.getpass)

    def test_the_environment_variable_is_used_when_present(self):
        os.environ["TELEGRAM_TOKEN"] = TOKEN

        def ask(prompt):
            raise AssertionError("should not prompt when the env var is set")

        self.assertEqual(ts.read_token(out=self.say, ask=ask), TOKEN)
        self.assertNotIn(TOKEN, self.printed)

    def test_an_empty_entry_explains_the_env_var_fallback(self):
        with self.assertRaises(ts.SetupError) as cm:
            ts.read_token(out=self.say, ask=lambda p: "")
        self.assertIn("TELEGRAM_TOKEN", str(cm.exception))

    def test_a_cancelled_prompt_is_a_clean_failure(self):
        def ask(prompt):
            raise EOFError()
        with self.assertRaises(ts.SetupError):
            ts.read_token(out=self.say, ask=ask)


# ------------------------------------------------------- 8. end to end run

class TestRun(Quiet):

    def setUp(self):
        super().setUp()
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "config.json")
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"mode": "paper", "api_key": "KEEP", "api_secret": "KEEP",
                       "telegram_token": "PASTE_FROM_BOTFATHER",
                       "telegram_chat_id": "PASTE_YOUR_CHAT_ID"}, f, indent=2)
        self._env = os.environ.pop("TELEGRAM_TOKEN", None)

    def tearDown(self):
        # restore *or remove*: a test that sets the variable must not leak it
        # into the next one, which previously made load_config() read a stale
        # placeholder token out of the environment.
        if self._env is not None:
            os.environ["TELEGRAM_TOKEN"] = self._env
        else:
            os.environ.pop("TELEGRAM_TOKEN", None)
        super().tearDown()

    def contents(self):
        with open(self.path, encoding="utf-8") as f:
            return f.read()

    def happy_fake(self):
        return FakeTelegram(getMe=[ok(ME)],
                            getUpdates=[ok([update(5, 4242)])],
                            sendMessage=[ok({"message_id": 1})])

    def test_a_full_successful_run_configures_everything(self):
        fake = self.happy_fake()
        code = ts.run(["--config", self.path], out=self.say, http=fake,
                      ask=lambda p: TOKEN)
        self.assertEqual(code, 0)
        with open(self.path, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["telegram_token"], TOKEN)
        self.assertEqual(data["telegram_chat_id"], "4242")
        self.assertEqual(data["api_key"], "KEEP")
        self.assertEqual(data["mode"], "paper")
        methods = [c["method"] for c in fake.calls]
        self.assertIn("getMe", methods)
        self.assertIn("sendMessage", methods)

    def test_a_successful_run_prints_no_secret(self):
        ts.run(["--config", self.path], out=self.say, http=self.happy_fake(),
               ask=lambda p: TOKEN)
        self.assertNotIn(TOKEN, self.printed, "the token was printed")
        self.assertNotIn(TOKEN.split(":", 1)[1], self.printed)
        self.assertIn("my_paper_bot", self.printed)

    def test_an_explicit_chat_id_skips_the_wait(self):
        fake = FakeTelegram(getMe=[ok(ME)], sendMessage=[ok({"message_id": 1})])
        ts.run(["--config", self.path, "--chat-id", "777"], out=self.say,
               http=fake, ask=lambda p: TOKEN)
        self.assertNotIn("getUpdates", [c["method"] for c in fake.calls])
        with open(self.path, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["telegram_chat_id"], "777")

    def test_no_save_leaves_the_config_alone(self):
        before = self.contents()
        code = ts.run(["--config", self.path, "--no-save"], out=self.say,
                      http=self.happy_fake(), ask=lambda p: TOKEN)
        self.assertEqual(code, 0)
        self.assertEqual(self.contents(), before)

    def test_no_test_message_skips_sendmessage(self):
        fake = self.happy_fake()
        ts.run(["--config", self.path, "--no-test-message"], out=self.say,
               http=fake, ask=lambda p: TOKEN)
        self.assertNotIn("sendMessage", [c["method"] for c in fake.calls])

    def test_a_bad_token_writes_nothing(self):
        before = self.contents()
        fake = FakeTelegram(getMe=[err(404, "Not Found")])
        with self.assertRaises(ts.SetupError):
            ts.run(["--config", self.path], out=self.say, http=fake,
                   ask=lambda p: TOKEN)
        self.assertEqual(self.contents(), before)

    def test_main_turns_a_setup_error_into_exit_code_1(self):
        os.environ["TELEGRAM_TOKEN"] = BAD_SHAPE
        tb_requests = tb.requests
        tb.requests = FakeTelegram()
        try:
            code = ts.main(["--config", self.path, "--no-test-message"])
        finally:
            tb.requests = tb_requests
        self.assertEqual(code, 1)
        self.assertIn("FAILED", sys.stdout.getvalue())
        self.assertIn("Nothing was saved", sys.stdout.getvalue())

    def test_check_reports_a_placeholder_config_as_a_failure(self):
        fake = FakeTelegram()
        code = ts.run(["--config", self.path, "--check"], out=self.say,
                      http=fake)
        self.assertEqual(code, 1)
        self.assertIn("404", self.printed)
        self.assertEqual(fake.calls, [], "no request should be made")

    def test_check_passes_on_a_good_config(self):
        ts.save_credentials(TOKEN, "4242", path=self.path)
        code = ts.run(["--config", self.path, "--check"], out=self.say,
                      http=self.happy_fake())
        self.assertEqual(code, 0)
        self.assertIn("correctly configured", self.printed)
        self.assertNotIn(TOKEN, self.printed)

    def test_check_never_writes_to_the_config(self):
        ts.save_credentials(TOKEN, "4242", path=self.path)
        before = self.contents()
        ts.run(["--config", self.path, "--check"], out=self.say,
               http=self.happy_fake())
        self.assertEqual(self.contents(), before)


# ------------------------------------------------------- 9. bot-side diagnostics

class TestNotifierDiagnostics(unittest.TestCase):
    """The hints added to trading_bot.Notifier, and the paper-mode guarantee."""

    def setUp(self):
        self._requests = tb.requests
        self._stdout = sys.stdout
        sys.stdout = io.StringIO()

    def tearDown(self):
        tb.requests = self._requests
        self.out = sys.stdout.getvalue()
        sys.stdout = self._stdout

    class Fake:
        class R:
            def __init__(self, status, payload):
                self.status_code, self._p, self.text = status, payload, ""

            def json(self):
                return self._p

        def __init__(self, status=200, payload=None, boom=None):
            self.status, self.payload, self.boom = status, payload, boom
            self.posts = []

        def post(self, url, json=None, timeout=10, **kw):
            self.posts.append(json)
            if self.boom:
                raise self.boom
            return self.R(self.status, self.payload or {"ok": self.status < 400})

        def get(self, url, params=None, timeout=10, **kw):
            if self.boom:
                raise self.boom
            return self.R(200, {"ok": True, "result": []})

    def notifier(self, fake, chat="4242"):
        tb.requests = fake
        c = dict(tb.DEFAULTS)
        c.update(telegram_token=TOKEN, telegram_chat_id=chat,
                 api_key="", api_secret="")
        return tb.Notifier(c)

    def test_hint_for_each_documented_status(self):
        self.assertIn("revoked", tb.telegram_error_hint(401))
        self.assertIn("URL path", tb.telegram_error_hint(404))
        self.assertIn("webhook", tb.telegram_error_hint(409))
        self.assertIn("rate limited", tb.telegram_error_hint(429))
        self.assertIn("Telegram's own servers", tb.telegram_error_hint(502))
        self.assertEqual(tb.telegram_error_hint(200), "")
        self.assertEqual(tb.telegram_error_hint(None), "")

    def test_network_hints(self):
        self.assertIn("did not answer in time",
                      tb.telegram_network_hint(OSError("read timed out")))
        self.assertIn("DNS-blocked",
                      tb.telegram_network_hint(OSError("getaddrinfo failed")))
        self.assertIn("intercepted",
                      tb.telegram_network_hint(OSError("certificate verify failed")))
        self.assertEqual(tb.telegram_network_hint(ValueError("odd")), "")

    def test_a_404_on_send_tells_the_user_the_token_is_wrong(self):
        n = self.notifier(self.Fake(status=404,
                                    payload={"ok": False, "description": "Not Found"}))
        n.send("hello")
        out = sys.stdout.getvalue()
        self.assertIn("Telegram rejected the message", out)
        self.assertIn("URL path", out)
        self.assertIn("telegram_setup.py", out)
        self.assertNotIn(TOKEN, out)

    def test_a_401_on_send_points_at_botfather(self):
        n = self.notifier(self.Fake(status=401,
                                    payload={"ok": False, "description": "Unauthorized"}))
        n.send("hello")
        self.assertIn("revoked", sys.stdout.getvalue())

    def test_a_timeout_on_send_is_diagnosed_and_does_not_raise(self):
        class ReadTimeout(Exception):
            pass
        n = self.notifier(self.Fake(boom=ReadTimeout("HTTPSConnectionPool read timed out")))
        n.send("hello")                       # must not raise
        out = sys.stdout.getvalue()
        self.assertIn("telegram send failed", out)
        self.assertIn("did not answer in time", out)

    def test_a_placeholder_token_points_at_the_setup_tool(self):
        tb.requests = self.Fake()
        c = dict(tb.DEFAULTS)
        c.update(telegram_token="PASTE_FROM_BOTFATHER",
                 telegram_chat_id="PASTE_YOUR_CHAT_ID")
        n = tb.Notifier(c)
        out = sys.stdout.getvalue()
        self.assertEqual(n.token, "")
        self.assertIn("telegram_setup.py", out)
        self.assertIn("404", out)
        self.assertEqual(tb.requests.posts, [])

    def test_the_setup_module_never_touches_live_trading(self):
        """telegram_setup must not be able to trade: no client, no broker, no order."""
        with open(ts.__file__, encoding="utf-8") as f:
            source = f.read()
        for forbidden in ("LiveBroker", "place_order", "new_order", "Bot(",
                          "i_understand_real_money", "tb.Binance"):
            self.assertNotIn(forbidden, source,
                             f"telegram_setup.py references {forbidden}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
