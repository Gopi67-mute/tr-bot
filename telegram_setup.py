#!/usr/bin/env python3
"""Local Telegram setup, validation and diagnostics for trading_bot.py.

Run it from the project folder:

    python telegram_setup.py

What it does, in order:
  1. Reads the BotFather token from a hidden prompt (nothing is echoed) or from
     the TELEGRAM_TOKEN environment variable. The token is never printed, never
     written to a log and never sent anywhere except api.telegram.org.
  2. Validates the token with Telegram's getMe endpoint and explains exactly
     what a 401 / 404 / timeout means instead of showing a raw traceback.
  3. Waits for you to send /start to your own bot, then reads the private chat
     id out of getUpdates.
  4. Sends a test message so delivery is proven end to end.
  5. Writes telegram_token and telegram_chat_id into config.json, leaving every
     other setting - Binance keys, mode, risk limits - exactly as it found them.
     The previous config.json is copied to config.json.bak first.

Nothing in this file can place an order: it imports trading_bot only for its
console/scrubbing helpers and never constructs a client, a broker or a Bot.

Why getMe returns 404
---------------------
The token sits in the URL path: https://api.telegram.org/bot<TOKEN>/getMe
If <TOKEN> is wrong, malformed or still a placeholder, the whole path is an
unknown route, so Telegram answers 404 Not Found - not 401. A 404 from getMe
therefore means "that is not a token", almost never "the endpoint moved".
"""

import argparse
import getpass
import json
import os
import re
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import trading_bot as tb  # noqa: E402  (same folder; import has no side effects)

API_ROOT = "https://api.telegram.org"
CONFIG_PATH = os.path.join(tb.HERE, "config.json")
EXAMPLE_PATH = os.path.join(tb.HERE, "config.example.json")

# 123456789:AA... - digits, a colon, then at least 30 url-safe characters.
TOKEN_RE = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{30,}$")

PLACEHOLDERS = {
    "", "paste_from_botfather", "paste_your_chat_id", "your_token_here",
    "changeme", "none", "null", "todo", "xxx",
}

GITIGNORE_ENTRIES = [
    "config.json",
    "config.json.bak",
    "config.local.json",
    "bot_state_*.json",
    "trades.csv",
    "engine.lock",
    "backtest_trades.csv",
    "research_report.txt",
    "__pycache__/",
    "*.pyc",
    ".env",
    "backup-*/",
]


class SetupError(Exception):
    """A failure that already carries a human-readable explanation."""


# ---------------------------------------------------------------- token hygiene

def mask_token(token):
    """A token rendered safe to print: bot id kept, secret half replaced.

    The bot id before the colon is public (it is in the bot's own username
    lookup), the half after it is the password. Only the id survives.
    """
    token = str(token or "").strip()
    if not token:
        return "(empty)"
    head, _, tail = token.partition(":")
    if not tail:
        return "***"
    return f"{head}:***({len(tail)} chars)"


def clean_token(raw):
    """Forgive the usual copy/paste damage without ever guessing at content.

    Accepts a bare token, a `bot<token>` prefix, or a full
    https://api.telegram.org/bot<token>/getMe URL pasted out of a browser.
    """
    t = str(raw or "").strip().strip('"').strip("'")
    t = "".join(t.split())                      # stray newlines/spaces from a paste
    m = re.search(r"/bot(\d{5,}:[A-Za-z0-9_-]{30,})", t)
    if m:
        return m.group(1)
    if t.lower().startswith("bot") and ":" in t[3:]:
        t = t[3:]
    return t


def token_problem(token):
    """Return None if the token is shaped like a BotFather token, else why not."""
    t = str(token or "").strip()
    if not t or t.lower() in PLACEHOLDERS:
        return ("the token is empty or still the placeholder from "
                "config.example.json")
    if ":" not in t:
        return ("there is no ':' in it. A BotFather token always looks like "
                "123456789:AA... - the part you pasted is probably only half of it")
    head, _, tail = t.partition(":")
    if not head.isdigit():
        return (f"the part before the ':' must be the numeric bot id, got "
                f"{len(head)} non-numeric character(s)")
    if len(tail) < 30:
        return (f"the secret after the ':' is only {len(tail)} characters; a real "
                f"one is about 35. The paste was probably truncated")
    if not TOKEN_RE.match(t):
        return ("it contains characters that cannot appear in a token (only "
                "letters, digits, '_' and '-' after the ':')")
    return None


def chat_id_problem(chat_id):
    c = str(chat_id or "").strip()
    if not c or c.lower() in PLACEHOLDERS:
        return "the chat id is empty or still a placeholder"
    if not c.lstrip("-").isdigit():
        return f"a chat id is a whole number (negative for groups), got {c!r}"
    return None


# ---------------------------------------------------------------- diagnostics

def classify_exception(e):
    """Map a transport failure onto a short kind, without touching .exceptions.

    trading_bot falls back to a urllib shim when `requests` is missing, and that
    shim has no exception hierarchy, so the kind is derived from the type name
    and message instead of from isinstance checks.
    """
    name = type(e).__name__.lower()
    text = str(e).lower()
    if "timeout" in name or "timed out" in text:
        return "timeout"
    if "ssl" in name or "certificate" in text:
        return "tls"
    if "proxy" in name or "proxy" in text:
        return "proxy"
    if ("connection" in name or "resolve" in text or "getaddrinfo" in text
            or "name or service not known" in text or "unreachable" in text
            or "refused" in text or "urlerror" in name):
        return "network"
    return "other"


NETWORK_HINTS = {
    "timeout": ("api.telegram.org accepted the connection but did not answer in "
                "time. This is what a blocked or throttled route looks like: "
                "many ISPs and almost all corporate/campus networks filter "
                "Telegram. Try a different network, a VPN, or set HTTPS_PROXY."),
    "network": ("the connection to api.telegram.org could not be made at all - "
                "DNS failure, no route, or the request was refused. Check that "
                "this machine is online and that Telegram is not DNS-blocked "
                "(some ISPs resolve api.telegram.org to a block page)."),
    "tls": ("the TLS handshake failed. Something is intercepting HTTPS (a "
            "corporate proxy or antivirus). Install its root certificate or "
            "use another network."),
    "proxy": ("the proxy configured in HTTP_PROXY/HTTPS_PROXY rejected the "
              "request or is unreachable."),
    "other": ("the request to api.telegram.org failed before a reply arrived."),
}


def describe_http(status, description=""):
    """Turn a Telegram HTTP status into one actionable sentence."""
    d = f" Telegram said: {description}" if description else ""
    if status == 401:
        return ("HTTP 401 Unauthorized - the token is correctly shaped but "
                "Telegram does not accept it. It was revoked, regenerated, or "
                "belongs to a deleted bot. Open BotFather, send /mytoken or "
                "/revoke for this bot and paste the new token." + d)
    if status == 404:
        return ("HTTP 404 Not Found - the token is in the URL path, so a 404 "
                "from getMe means the path 'bot<token>' is not a real route: "
                "the token is wrong, truncated, or still a placeholder. It is "
                "NOT a sign that the API moved." + d)
    if status == 409:
        return ("HTTP 409 Conflict - something else is already reading this "
                "bot's updates: another copy of the bot is running, or a "
                "webhook is set. Stop the other process, or clear the webhook "
                "with deleteWebhook, then retry." + d)
    if status == 400:
        return ("HTTP 400 Bad Request - Telegram refused the arguments. For "
                "sendMessage this is almost always a chat id the bot cannot "
                "write to: send /start to the bot from that chat first." + d)
    if status == 403:
        return ("HTTP 403 Forbidden - the bot is blocked by that chat, or was "
                "never started there. Open the chat and send /start." + d)
    if status == 429:
        return ("HTTP 429 Too Many Requests - rate limited. Wait for the "
                "retry_after period and try again." + d)
    if 500 <= status < 600:
        return (f"HTTP {status} - Telegram's own servers are failing. This is "
                f"not a configuration problem; retry in a minute." + d)
    return f"HTTP {status} from Telegram." + d


# ---------------------------------------------------------------- api

def api(token, method, params=None, timeout=20, http=None):
    """Call one Telegram Bot API method and return its `result`.

    Raises SetupError carrying a finished explanation. The token is scrubbed out
    of every message this can raise, so a traceback cannot leak it.
    """
    http = http or tb.requests
    url = f"{API_ROOT}/bot{token}/{method}"
    try:
        r = http.get(url, params=params or {}, timeout=timeout)
    except Exception as e:                                  # noqa: BLE001
        kind = classify_exception(e)
        raise SetupError(
            f"{method} could not reach Telegram ({type(e).__name__}): "
            f"{NETWORK_HINTS[kind]}\n  detail: {tb.scrub(e, token)}") from None

    status = int(getattr(r, "status_code", 200) or 200)
    body = {}
    try:
        parsed = r.json()
        if isinstance(parsed, dict):
            body = parsed
    except Exception:                                       # noqa: BLE001
        body = {}
    description = str(body.get("description", ""))[:200]

    if status >= 400 or not body.get("ok", status < 400):
        if not description:
            description = tb.scrub(str(getattr(r, "text", ""))[:200], token)
        raise SetupError(describe_http(status, tb.scrub(description, token)))
    return body.get("result")


def validate_token(token, http=None):
    """getMe. Returns {'id':…, 'username':…, 'first_name':…} for a good token."""
    problem = token_problem(token)
    if problem:
        raise SetupError(
            f"That is not a usable token: {problem}.\n"
            f"  A placeholder or malformed token is exactly what makes getMe "
            f"answer HTTP 404, because the token is part of the URL path.\n"
            f"  Get a fresh one from @BotFather: /newbot, or /mytoken for an "
            f"existing bot.")
    me = api(token, "getMe", http=http)
    if not isinstance(me, dict) or not me.get("id"):
        raise SetupError("getMe succeeded but returned no bot identity, which "
                         "should be impossible. Retry; if it persists, Telegram "
                         "is having an outage.")
    return me


def get_updates(token, offset=None, poll=0, http=None):
    params = {"timeout": poll, "allowed_updates": json.dumps(["message"])}
    if offset is not None:
        params["offset"] = offset
    result = api(token, "getUpdates", params=params,
                 timeout=poll + 20, http=http)
    return result if isinstance(result, list) else []


def find_private_chat(updates):
    """Pick the newest private-chat message out of a getUpdates batch."""
    best = None
    for u in updates:
        if not isinstance(u, dict):
            continue
        m = u.get("message") or u.get("edited_message") or {}
        chat = m.get("chat") if isinstance(m, dict) else None
        if not isinstance(chat, dict) or chat.get("id") is None:
            continue
        if chat.get("type") not in (None, "private"):
            continue
        name = " ".join(str(chat.get(k)) for k in ("first_name", "last_name")
                        if chat.get(k)) or str(chat.get("username") or "you")
        best = {"chat_id": str(chat["id"]), "name": name,
                "text": str(m.get("text") or "")}
    return best


def wait_for_chat_id(token, username=None, wait_sec=180, poll=5, http=None,
                     clock=time.monotonic, sleep=time.sleep, out=tb.say):
    """Drain stale updates, then wait for a fresh message in a private chat."""
    link = f"https://t.me/{username}" if username else "your bot"
    out(f"\nStep 2/4  Open {link} in Telegram and send:  /start")
    out(f"          Waiting up to {wait_sec}s for that message "
        f"(Ctrl+C to cancel)...")

    offset = None
    try:                      # clear anything sent before this run started
        stale = get_updates(token, poll=0, http=http)
        if stale:
            offset = stale[-1]["update_id"] + 1
            found = find_private_chat(stale)
            if found:
                out(f"          Found an earlier message from "
                    f"{found['name']} - using that chat.")
                return found
    except SetupError:
        raise
    except Exception:                                       # noqa: BLE001
        offset = None

    deadline = clock() + wait_sec
    skipped_group = False
    while clock() < deadline:
        ups = get_updates(token, offset=offset, poll=poll, http=http)
        if ups:
            offset = ups[-1]["update_id"] + 1
            found = find_private_chat(ups)
            if found:
                return found
            if not skipped_group:
                skipped_group = True
                out("          Ignoring a message from a group chat - send "
                    "/start in a direct message to the bot instead.")
        else:
            sleep(1)
    raise SetupError(
        f"No private message arrived within {wait_sec}s.\n"
        f"  - Make sure you opened {link} and pressed START (or sent /start).\n"
        f"  - If another copy of the bot is running, it is consuming the "
        f"updates: stop it and rerun.\n"
        f"  - Rerun with a longer wait:  python telegram_setup.py --wait 300")


def send_message(token, chat_id, text, http=None):
    """sendMessage, returning the sent message dict."""
    problem = chat_id_problem(chat_id)
    if problem:
        raise SetupError(f"Cannot send a test message: {problem}.")
    return api(token, "sendMessage",
               params={"chat_id": str(chat_id), "text": text[:4096]},
               http=http)


# ---------------------------------------------------------------- config i/o

def _seed_config():
    """A starting config when config.json does not exist yet."""
    for path in (EXAMPLE_PATH,):
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    return data
            except (OSError, ValueError):
                pass
    return {"mode": "paper"}


def save_credentials(token, chat_id, path=CONFIG_PATH, backup=True):
    """Write only the two Telegram keys into config.json.

    Every other key keeps its value and its position, so Binance credentials,
    `mode` and the risk settings cannot be clobbered. The write is atomic
    (temp file + os.replace) and the old file is copied to config.json.bak.
    Returns (path, backup_path_or_None, mode_in_file).
    """
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError) as e:
            raise SetupError(f"config.json exists but could not be read ({e}). "
                             f"Fix or move it, then rerun.")
        if not isinstance(data, dict):
            raise SetupError("config.json must contain a JSON object.")
    else:
        data = _seed_config()

    made_backup = None
    if backup and os.path.exists(path):
        made_backup = path + ".bak"
        shutil.copy2(path, made_backup)

    data["telegram_token"] = str(token)
    data["telegram_chat_id"] = str(chat_id)
    data.setdefault("mode", "paper")

    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    try:                       # best effort on Windows; no-op if unsupported
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path, made_backup, data.get("mode")


def ensure_gitignore(folder=None, entries=GITIGNORE_ENTRIES):
    """Make sure config.json and friends can never be committed.

    Returns the list of entries this call had to add.
    """
    folder = folder or tb.HERE
    path = os.path.join(folder, ".gitignore")
    existing, lines = set(), []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
        existing = {ln.strip() for ln in lines}
    missing = [e for e in entries if e not in existing]
    if not missing:
        return []
    out = list(lines)
    if out and out[-1].strip():
        out.append("")
    out.append("# secrets and local state - added by telegram_setup.py")
    out.extend(missing)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")
    return missing


# ---------------------------------------------------------------- cli

def read_token(env="TELEGRAM_TOKEN", prompt=None, out=tb.say,
               ask=getpass.getpass):
    """Hidden token input. Env var wins so this can run unattended."""
    from_env = os.environ.get(env)
    if from_env:
        out(f"Using the token from the {env} environment variable "
            f"(not displayed).")
        return clean_token(from_env)
    prompt = prompt or ("Paste the BotFather token, then press Enter.\n"
                        "  Nothing is echoed and nothing is logged: ")
    try:
        raw = ask(prompt)
    except (EOFError, KeyboardInterrupt):
        raise SetupError("No token was entered.") from None
    if not raw:
        raise SetupError(
            "Nothing was entered.\n"
            f"  If this terminal cannot hide input, set the token in the "
            f"environment instead:\n"
            f"    $env:{env} = '<token>' ; python telegram_setup.py")
    return clean_token(raw)


def run(argv=None, out=tb.say, http=None, ask=getpass.getpass):
    ap = argparse.ArgumentParser(
        description="Configure and test Telegram for trading_bot.py "
                    "(paper mode; places no orders).")
    ap.add_argument("--config", default=CONFIG_PATH,
                    help="config file to update (default: ./config.json)")
    ap.add_argument("--chat-id", help="skip the /start wait and use this chat id")
    ap.add_argument("--wait", type=int, default=180,
                    help="seconds to wait for /start (default 180)")
    ap.add_argument("--poll", type=int, default=5,
                    help="long-poll seconds per getUpdates call (default 5)")
    ap.add_argument("--no-save", action="store_true",
                    help="validate and test only; do not touch config.json")
    ap.add_argument("--no-test-message", action="store_true",
                    help="do not send the test message")
    ap.add_argument("--check", action="store_true",
                    help="diagnose the Telegram settings already in config.json "
                         "(no prompt, no writes)")
    ap.add_argument("--message", default=None,
                    help="text of the test message")
    args = ap.parse_args(argv)

    tb.enable_utf8_stdout()
    out("Telegram setup for the paper-trading bot")
    out("=" * 46)

    if args.check:
        return _check(args, out, http)

    token = read_token(out=out, ask=ask)

    out("\nStep 1/4  Validating the token with getMe...")
    me = validate_token(token, http=http)
    username = me.get("username")
    out(f"          OK - bot @{username} (id {me.get('id')}), "
        f"token {mask_token(token)}")
    if not me.get("can_read_all_group_messages", True):
        pass  # privacy mode is irrelevant for a private chat

    if args.chat_id:
        problem = chat_id_problem(args.chat_id)
        if problem:
            raise SetupError(f"--chat-id is unusable: {problem}")
        chat = {"chat_id": str(args.chat_id).strip(), "name": "you"}
        out(f"\nStep 2/4  Using the chat id given on the command line.")
    else:
        chat = wait_for_chat_id(token, username=username, wait_sec=args.wait,
                                poll=args.poll, http=http, out=out)
        out(f"          Got it - private chat with {chat['name']}.")

    if args.no_test_message:
        out("\nStep 3/4  Skipped (--no-test-message).")
    else:
        out("\nStep 3/4  Sending a test message...")
        text = args.message or (
            "✅ Telegram is wired up.\n"
            "This is a test message from your Binance futures bot "
            "(PAPER mode - no real orders).")
        send_message(token, chat["chat_id"], text, http=http)
        out("          Delivered. Check your Telegram.")

    if args.no_save:
        out("\nStep 4/4  Skipped (--no-save). Nothing was written.")
        out("\nDone. Telegram works; config.json was left untouched.")
        return 0

    out("\nStep 4/4  Saving to config.json...")
    path, backup, mode = save_credentials(token, chat["chat_id"],
                                          path=args.config)
    out(f"          Wrote telegram_token and telegram_chat_id to {path}")
    out(f"          (secrets not displayed: token {mask_token(token)}, "
        f"chat id {chat['chat_id']})")
    if backup:
        out(f"          Previous config backed up to {backup}")
    out(f"          All other settings untouched - mode is still {mode!r}.")

    added = ensure_gitignore(os.path.dirname(os.path.abspath(args.config)))
    if added:
        out(f"          .gitignore updated so secrets cannot be committed: "
            f"{', '.join(added)}")

    out("\nDone. Start the bot with:")
    out("    python trading_bot.py --mode paper")
    return 0


def _check(args, out, http):
    """--check: explain whatever is in config.json, without changing anything."""
    cfg = tb.load_config(args.config)
    token = clean_token(cfg.get("telegram_token"))
    chat_id = str(cfg.get("telegram_chat_id") or "").strip()
    out(f"config: {args.config}")
    out(f"  telegram_token    : {mask_token(token)}")
    out(f"  telegram_chat_id  : {chat_id or '(empty)'}")

    tp, cp = token_problem(token), chat_id_problem(chat_id)
    if tp:
        out(f"\nFAIL  token: {tp}.")
        out("      This is the usual cause of the HTTP 404 from getMe - the "
            "token is part of the URL path.")
        out("      Fix it by running:  python telegram_setup.py")
        return 1
    me = validate_token(token, http=http)
    out(f"\nOK    token accepted by getMe - bot @{me.get('username')} "
        f"(id {me.get('id')}).")
    if cp:
        out(f"FAIL  chat id: {cp}.")
        out("      Run:  python telegram_setup.py")
        return 1
    send_message(token, chat_id,
                 "✅ Telegram check passed (PAPER mode bot).", http=http)
    out("OK    test message delivered to the configured chat.")
    out("\nTelegram is correctly configured.")
    return 0


def main(argv=None):
    try:
        return run(argv)
    except SetupError as e:
        tb.say(f"\nFAILED: {e}")
        tb.say("\nNothing was saved. Rerun when the problem above is fixed; "
               "`python telegram_setup.py --check` re-tests the saved settings.")
        return 1
    except KeyboardInterrupt:
        tb.say("\nCancelled. Nothing was saved.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
