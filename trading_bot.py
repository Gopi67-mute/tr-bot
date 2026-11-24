#!/usr/bin/env python3
"""Trend-pullback futures bot for Binance USDT-M perpetuals - PAPER TRADING.

Strategy (unchanged from the original build; the same rules in every mode):
  * Trend filter on 4h: only LONG when the 4h close is above the 200 EMA, only SHORT below it.
  * Entry on 1h: after a closed 1h candle that dipped into the 20 EMA and closed back
    on the trend side (with 20 EMA above/below the 50 EMA), enter at market.
  * Stop: beyond the swing low/high of the last N 1h candles. Target: reward_risk x the risk.
  * Size: risk a fixed % of current equity; leverage is whatever that size needs (capped).

Risk controls (rebuilt - see RiskManager):
  * 0.5% planned risk per trade, fees and slippage included in the risk budget.
  * 2% daily loss limit on realised + unrealised P&L -> close, lock until tomorrow.
  * 5% account drawdown from the persisted equity peak -> close, permanent halt.
  * One position at a time, three entries a day, a cooldown after a loss.
  * Mandatory stop on every position; a stop may never be widened.
  * Position size is NEVER raised to satisfy an exchange minimum. The trade is rejected.

Modes:
  backtest  - replay past candles with fees and slippage, print stats (no keys needed)
  research  - backtest plus out-of-sample and walk-forward validation reporting
  paper     - live prices, simulated money, Telegram remote control (no keys needed)
  live      - present but deliberately untouched; paper mode cannot reach it

Only dependency: requests  (pip install requests)
"""

import argparse
import csv
import hashlib
import hmac
import json
import math
import os
import re
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from urllib.parse import urlencode

try:
    import requests
except ImportError:
    from urllib.error import HTTPError
    from urllib.request import Request, urlopen

    class _UrlResponse:
        def __init__(self, status_code, body, headers=None):
            self.status_code = status_code
            self.headers = headers or {}
            self.text = body.decode("utf-8", errors="replace")

        def json(self):
            return json.loads(self.text)

    class _UrlSession:
        def request(self, method, url, headers=None, timeout=15, params=None, json=None):
            if params:
                url += ("&" if "?" in url else "?") + urlencode(params)
            body = None if json is None else __import__("json").dumps(json).encode("utf-8")
            req = Request(url, data=body, headers=headers or {}, method=method.upper())
            if json is not None:
                req.add_header("Content-Type", "application/json")
            try:
                with urlopen(req, timeout=timeout) as response:
                    return _UrlResponse(response.status, response.read(),
                                        dict(response.headers))
            except HTTPError as error:
                return _UrlResponse(error.code, error.read(), dict(error.headers or {}))

    class _RequestsFallback:
        Session = _UrlSession

        @staticmethod
        def get(url, params=None, timeout=15, **kwargs):
            return _UrlSession().request("GET", url, params=params, timeout=timeout, **kwargs)

        @staticmethod
        def post(url, json=None, timeout=15, **kwargs):
            return _UrlSession().request("POST", url, json=json, timeout=timeout, **kwargs)

    requests = _RequestsFallback()

IST = timezone(timedelta(hours=5, minutes=30))
HERE = os.path.dirname(os.path.abspath(__file__))
JOURNAL_FILE = os.path.join(HERE, "trades.csv")
LIVE_BASE = "https://fapi.binance.com"
TESTNET_BASE = "https://demo-fapi.binance.com"
MODES = ("backtest", "research", "paper", "live")
STRATEGY_NAME = "trend-pullback-ema20"

# Paper trading simulates fractional quantities so that an exchange minimum can
# never force the risk above the cap. This is the granularity of that simulation.
PAPER_QTY_STEP = 1e-6

# A hard ceiling on the configurable per-trade risk. This exists so that no
# config edit, and no remote command, can turn the simulator into something
# that risks a large share of the account on one trade.
RISK_PCT_HARD_CAP = 2.0

# A hard ceiling on the configurable *absolute* per-trade risk, in USDT. The
# percentage cap alone is not enough: 0.5% of a grown account is an unbounded
# number of USDT, so an absolute ceiling is what actually bounds the loss on
# any single trade. No config edit may set the absolute cap above this.
RISK_USDT_HARD_CAP = 5.0


# ---------------------------------------------------------------- console output

def enable_utf8_stdout():
    """Force UTF-8 on stdout/stderr.

    Windows defaults stdout to a legacy code page (cp1252 on this machine). The
    status messages below contain emoji and arrows, and printing those to a
    cp1252 stream raises UnicodeEncodeError - which used to kill the bot on its
    very first message as soon as output was redirected to a log file or a pipe.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def say(text):
    """print() that can never raise because of an un-encodable character."""
    try:
        print(text, flush=True)
        return
    except Exception:
        pass
    try:
        enc = getattr(sys.stdout, "encoding", None) or "ascii"
        print(str(text).encode(enc, "replace").decode(enc, "replace"), flush=True)
    except Exception:
        pass  # a message is never worth crashing the bot for


_SECRET_RE = re.compile(r"((?:signature|api[_-]?key|api[_-]?secret|token)=)[^&\s\"']+", re.I)
_BOT_URL_RE = re.compile(r"(/bot)[0-9]+:[A-Za-z0-9_\-]+")


def scrub(text, *secrets):
    """Strip credentials out of anything that is about to be printed or sent.

    Exception messages from `requests` embed the full request URL, which for
    Telegram contains the bot token and for signed Binance calls contains the
    request signature. Those must never reach stdout, a log file or a chat.
    """
    out = _BOT_URL_RE.sub(r"\1***", _SECRET_RE.sub(r"\1***", str(text)))
    for s in secrets:
        s = str(s or "")
        if len(s) >= 8:
            out = out.replace(s, "***")
    return out


DEFAULTS = {
    "mode": "paper",                    # backtest | research | paper | live
    "symbols": ["ETHUSDT", "SOLUSDT"],  # BTC's $100 minimum order is too big for a ~$22 account
    "use_testnet": True,                # live mode trades on the testnet unless this is false
    "i_understand_real_money": False,   # must ALSO be true to trade real money
    "api_key": "",
    "api_secret": "",
    "telegram_token": "",
    "telegram_chat_id": "",

    "paper_start_balance": 22.0,        # USDT (about 2000 INR)
    "inr_per_usdt": 88.0,               # only used to show INR in messages

    # ---- risk profile (conservative by design) ----
    "risk_per_trade_pct": 0.5,          # planned risk, fees + slippage included
    "max_risk_usdt_abs": 1.00,          # absolute per-trade ceiling in USDT;
                                        # the stricter of this and the % wins
    "max_daily_loss_pct": 2.0,          # of the day's starting equity
    "max_account_drawdown_pct": 5.0,    # from the highest recorded equity, ever
    "max_open_positions": 1,
    "max_trades_per_day": 3,
    "max_losses_per_day": 2,
    "loss_cooldown_min": 60,            # no new entry for this long after a loss
    "monthly_drawdown_stop_pct": 6.0,
    "max_leverage": 5,
    "slippage_pct": 0.03,               # allowance per side, % of price
    "taker_fee_pct": 0.05,              # per side

    # ---- paper-trading behaviour ----
    "paper_trading_enabled": False,     # fresh installs start in standby
    "paper_fractional_qty": True,       # simulate below exchange minimums
    "reset_confirm_minutes": 5,

    # ---- strategy (unchanged) ----
    "reward_risk": 2.0,
    "trend_interval": "4h",
    "entry_interval": "1h",
    "trend_ema": 200,
    "fast_ema": 20,
    "slow_ema": 50,
    "swing_lookback": 10,
    "min_stop_pct": 0.4,                # tighter stops get eaten by fees/noise
    "max_stop_pct": 3.0,
    "max_adverse_funding_pct": 0.05,    # skip if funding per 8h is worse than this against you

    # ---- engine ----
    "check_every_sec": 60,
    "command_poll_sec": 2,              # how often the command worker polls Telegram
    "status_every_hours": 6,
    "max_price_disagreement_pct": 20.0,  # ticker vs last close sanity check

    # ---- network budgets (a hung request must never freeze the bot) ----
    "http_connect_timeout_sec": 5.0,
    "http_read_timeout_sec": 10.0,
    "http_retries": 2,                  # total attempts for an idempotent GET
    "telegram_timeout_sec": 8.0,

    # ---- market-data cache (informational reads only; see MarketData) ----
    "price_cache_sec": 5,               # engine reuse window for a ticker
    "display_cache_sec": 150,           # how old a cached price may be in a report
    "funding_cache_sec": 60,

    # ---- performance thresholds for /health ----
    "slow_command_ms": 1000,
    "slow_cycle_ms": 5000,
    "slow_http_ms": 2000,
    "backtest_days": 180,
    "oos_fraction": 0.3,                # share of history held back for validation
    "walk_forward_folds": 4,
}

# Keys that existed in earlier builds and are now refused outright, because
# silently ignoring them would leave the user believing a limit is in force.
REMOVED_KEYS = {
    "max_risk_pct_when_bumped":
        "removed: position size is never raised to meet an exchange minimum any "
        "more. A trade whose minimum size would exceed risk_per_trade_pct is "
        "rejected instead. Delete this key from your config file.",
    "max_risk_when_bumped": "removed: see max_risk_pct_when_bumped.",
}


# ---------------------------------------------------------------- helpers

def now_ms():
    return int(time.time() * 1000)


def ist(ms=None):
    ms = now_ms() if ms is None else ms
    return datetime.fromtimestamp(ms / 1000, IST)


def day_key(ms=None):
    return ist(ms).strftime("%Y-%m-%d")


def week_key(ms=None):
    y, w, _ = ist(ms).isocalendar()
    return f"{y}-W{w:02d}"


def month_key(ms=None):
    return ist(ms).strftime("%Y-%m")


_UNIT_MS = {"m": 60_000, "h": 3_600_000, "d": 86_400_000, "w": 604_800_000}


def interval_ms(interval):
    """'1h' -> 3600000. Falls back to one hour for anything unrecognised."""
    try:
        text = str(interval).strip().lower()
        return int(text[:-1]) * _UNIT_MS[text[-1]]
    except Exception:
        return 3_600_000


def is_finite(*values):
    """True only if every value is a real, finite number.

    NaN and infinity are the dangerous cases, not None. A NaN compares False
    against *every* threshold, so `if risk > cap: reject` waves it straight
    through, and `qty <= 0` does not catch it either. Any risk input that is
    not finite therefore has to be rejected explicitly, before it is compared
    with anything.
    """
    for v in values:
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return False
        if not math.isfinite(v):
            return False
    return True


def _round_step(x, step, rounding):
    d, s = Decimal(str(x)), Decimal(str(step))
    return float((d / s).to_integral_value(rounding=rounding) * s)


def floor_step(x, step):
    return _round_step(x, step, ROUND_DOWN)


def ceil_step(x, step):
    return _round_step(x, step, ROUND_UP)


def fmt_step(x, step):
    places = max(0, -Decimal(str(step)).normalize().as_tuple().exponent)
    return f"{x:.{places}f}"


def fmt_qty(qty):
    """Human-readable quantity that does not print 6.5e-05."""
    return f"{qty:.8f}".rstrip("0").rstrip(".") or "0"


def ema(values, period):
    out = [None] * len(values)
    if len(values) < period:
        return out
    k = 2 / (period + 1)
    s = sum(values[:period]) / period
    out[period - 1] = s
    for i in range(period, len(values)):
        s = values[i] * k + s * (1 - k)
        out[i] = s
    return out


def new_trade_id():
    return uuid.uuid4().hex[:12]


def load_config(path):
    cfg = dict(DEFAULTS)
    if path and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                user = json.load(f)
        except (OSError, ValueError) as e:
            raise SystemExit(f"Could not read config file {path}: {e}")
        if not isinstance(user, dict):
            raise SystemExit(f"{path} must contain a JSON object.")
        dead = sorted(set(user) & set(REMOVED_KEYS))
        if dead:
            raise SystemExit("Refusing to start - the configuration uses keys that no "
                             "longer exist:\n  - "
                             + "\n  - ".join(f"{k}: {REMOVED_KEYS[k]}" for k in dead))
        unknown = set(user) - set(DEFAULTS)
        if unknown:
            say(f"warning: unknown config keys ignored: {sorted(unknown)}")
        cfg.update({k: v for k, v in user.items() if k in DEFAULTS})
    for env, key in [("BINANCE_API_KEY", "api_key"), ("BINANCE_API_SECRET", "api_secret"),
                     ("TELEGRAM_TOKEN", "telegram_token"), ("TELEGRAM_CHAT_ID", "telegram_chat_id")]:
        if os.environ.get(env):
            cfg[key] = os.environ[env]
    return cfg


def _num(cfg, key):
    v = cfg.get(key)
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def validate_config(cfg):
    """Reject a configuration that is unsafe or nonsensical, before anything connects.

    `mode` is checked against an exact whitelist. Previously anything that was
    not exactly "backtest" or "paper" - including "Paper", a typo, an empty
    string or null - fell through to the live-order branch of main(). That is
    now a hard stop rather than a silent promotion to live trading.
    """
    bad = []
    if cfg.get("mode") not in MODES:
        bad.append(f"mode must be exactly one of {', '.join(MODES)} (lower case), "
                   f"got {cfg.get('mode')!r}")
    syms = cfg.get("symbols")
    if not isinstance(syms, list) or not syms or not all(
            isinstance(s, str) and s.strip() for s in syms):
        bad.append(f"symbols must be a non-empty list of symbol strings, got {syms!r}")
    for key in ("paper_trading_enabled", "paper_fractional_qty"):
        if not isinstance(cfg.get(key), bool):
            bad.append(f"{key} must be true or false, got {cfg.get(key)!r}")

    checks = [
        ("paper_start_balance", lambda v: v > 0, "must be greater than 0"),
        ("risk_per_trade_pct", lambda v: 0 < v <= RISK_PCT_HARD_CAP,
         f"must be greater than 0 and at most {RISK_PCT_HARD_CAP}% (this build is a "
         f"conservative paper simulator and will not risk more on one trade)"),
        ("max_risk_usdt_abs", lambda v: 0 < v <= RISK_USDT_HARD_CAP,
         f"must be greater than 0 and at most {RISK_USDT_HARD_CAP} USDT - it is the "
         f"absolute ceiling on the planned loss of a single trade, fees and "
         f"slippage included"),
        ("max_daily_loss_pct", lambda v: 0 < v <= 100, "must be between 0 and 100"),
        ("max_account_drawdown_pct", lambda v: 0 < v <= 100, "must be between 0 and 100"),
        ("max_open_positions", lambda v: v == 1,
         "must be 1 - this engine manages exactly one position at a time"),
        ("max_trades_per_day", lambda v: v >= 1, "must be at least 1"),
        ("max_losses_per_day", lambda v: v >= 1, "must be at least 1"),
        ("loss_cooldown_min", lambda v: v >= 0, "must be 0 or more"),
        ("max_leverage", lambda v: v >= 1, "must be at least 1"),
        ("slippage_pct", lambda v: v >= 0, "must be 0 or more"),
        ("taker_fee_pct", lambda v: v >= 0, "must be 0 or more"),
        ("reset_confirm_minutes", lambda v: v > 0, "must be greater than 0"),
        ("reward_risk", lambda v: v > 0, "must be greater than 0"),
        ("min_stop_pct", lambda v: v > 0, "must be greater than 0"),
        ("max_stop_pct", lambda v: v > 0, "must be greater than 0"),
        ("monthly_drawdown_stop_pct", lambda v: 0 < v < 100, "must be between 0 and 100"),
        ("check_every_sec", lambda v: v >= 5, "must be at least 5 seconds"),
        ("command_poll_sec", lambda v: v >= 1, "must be at least 1 second"),
        ("max_price_disagreement_pct", lambda v: v > 0, "must be greater than 0"),
        ("http_connect_timeout_sec", lambda v: 0 < v <= 60, "must be between 0 and 60"),
        ("http_read_timeout_sec", lambda v: 0 < v <= 120, "must be between 0 and 120"),
        ("http_retries", lambda v: 1 <= v <= 5, "must be between 1 and 5"),
        ("telegram_timeout_sec", lambda v: 0 < v <= 60, "must be between 0 and 60"),
        ("price_cache_sec", lambda v: v >= 0, "must be 0 or more"),
        ("display_cache_sec", lambda v: v >= 0, "must be 0 or more"),
        ("funding_cache_sec", lambda v: v >= 0, "must be 0 or more"),
        ("slow_command_ms", lambda v: v > 0, "must be greater than 0"),
        ("slow_cycle_ms", lambda v: v > 0, "must be greater than 0"),
        ("slow_http_ms", lambda v: v > 0, "must be greater than 0"),
        ("swing_lookback", lambda v: v >= 2, "must be at least 2"),
        ("trend_ema", lambda v: v >= 2, "must be at least 2"),
        ("fast_ema", lambda v: v >= 2, "must be at least 2"),
        ("slow_ema", lambda v: v >= 2, "must be at least 2"),
        ("backtest_days", lambda v: v >= 1, "must be at least 1"),
        ("oos_fraction", lambda v: 0 < v < 0.9, "must be between 0 and 0.9"),
        ("walk_forward_folds", lambda v: v >= 2, "must be at least 2"),
    ]
    for key, ok, why in checks:
        v = _num(cfg, key)
        if v is None:
            bad.append(f"{key} must be a number, got {cfg.get(key)!r}")
        elif not ok(v):
            bad.append(f"{key} {why}, got {v!r}")

    lo, hi = _num(cfg, "min_stop_pct"), _num(cfg, "max_stop_pct")
    if lo is not None and hi is not None and hi <= lo:
        bad.append(f"max_stop_pct ({hi}) must be greater than min_stop_pct ({lo})")
    risk, daily = _num(cfg, "risk_per_trade_pct"), _num(cfg, "max_daily_loss_pct")
    if risk is not None and daily is not None and risk > daily:
        bad.append(f"risk_per_trade_pct ({risk}%) must not exceed max_daily_loss_pct "
                   f"({daily}%) - one trade would be able to break the daily limit")
    dd = _num(cfg, "max_account_drawdown_pct")
    if daily is not None and dd is not None and daily > dd:
        bad.append(f"max_daily_loss_pct ({daily}%) must not exceed "
                   f"max_account_drawdown_pct ({dd}%)")
    fast, slow = _num(cfg, "fast_ema"), _num(cfg, "slow_ema")
    if fast is not None and slow is not None and fast >= slow:
        bad.append(f"fast_ema ({fast}) must be shorter than slow_ema ({slow})")
    if bad:
        raise SystemExit("Refusing to start - the configuration is not usable:\n  - "
                         + "\n  - ".join(bad))
    return cfg


# ---------------------------------------------------------------- strategy (shared by all modes)

def trend_bias_at(closes4, ema4, j):
    if j is None or j < 0 or ema4[j] is None:
        return None
    return "LONG" if closes4[j] > ema4[j] else "SHORT"


def signal_at(c1, fast, slow, i, bias, cfg):
    """Look at CLOSED 1h candle i. Returns {'side','stop'} or None."""
    lb = cfg["swing_lookback"]
    if bias is None or i < lb - 1 or fast[i] is None or slow[i] is None:
        return None
    c = c1[i]
    window = c1[i - lb + 1: i + 1]
    if bias == "LONG" and fast[i] > slow[i] and c["l"] <= fast[i] and c["c"] > fast[i] and c["c"] > c["o"]:
        return {"side": "LONG", "stop": min(x["l"] for x in window)}
    if bias == "SHORT" and fast[i] < slow[i] and c["h"] >= fast[i] and c["c"] < fast[i] and c["c"] < c["o"]:
        return {"side": "SHORT", "stop": max(x["h"] for x in window)}
    return None


def market_condition(side, entry, stop, fast_v=None, slow_v=None):
    """A short, recorded description of the regime the trade was taken in."""
    parts = [f"{side} bias", f"stop {abs(entry - stop) / entry * 100:.2f}%"]
    if fast_v is not None and slow_v is not None:
        parts.append("fast>slow EMA" if fast_v > slow_v else "fast<slow EMA")
    return "; ".join(parts)


def check_stop(side, entry, stop, cfg):
    """Returns None if the stop is usable, else a reason string."""
    if (side == "LONG" and stop >= entry) or (side == "SHORT" and stop <= entry):
        return "price already past the stop"
    pct = abs(entry - stop) / entry * 100
    if pct < cfg["min_stop_pct"]:
        return f"stop too tight ({pct:.2f}% < {cfg['min_stop_pct']}%) - fees would dominate"
    if pct > cfg["max_stop_pct"]:
        return f"stop too wide ({pct:.2f}% > {cfg['max_stop_pct']}%)"
    return None


def stop_is_widened(side, entry, old_stop, new_stop):
    """True if moving old_stop -> new_stop would increase the risk on the trade.

    A stop may be tightened (moved towards or past the entry) but never widened.
    Widening a stop to keep a losing trade alive is the single most common way a
    'fixed risk' system ends up losing several times its planned risk, so the
    engine refuses it outright rather than leaving it to discipline.
    """
    if side == "LONG":
        return new_stop < old_stop
    return new_stop > old_stop


def tighten_stop(pos, new_stop):
    """Move a position's stop, but only in the risk-reducing direction.

    Returns True if the stop moved. Never raises; a widening request is simply
    refused, because a caller that gets an exception might be tempted to catch
    it and carry on without a stop at all.
    """
    if stop_is_widened(pos["side"], pos["entry"], pos["stop"], new_stop):
        return False
    pos["stop"] = new_stop
    return True


def slip_amount(price, slippage_pct):
    return abs(price) * slippage_pct / 100.0


def adverse_fill(side, price, slippage_pct, entering):
    """The fill price once slippage has been applied against us.

    Entering long or exiting short pays up; entering short or exiting long gets
    less. Slippage is always adverse here: a simulator that models favourable
    slippage flatters itself.
    """
    slip = slip_amount(price, slippage_pct)
    pays_up = (side == "LONG") == bool(entering)
    return price + slip if pays_up else price - slip


def cost_per_unit(entry, stop, fee_pct, slippage_pct=0.0):
    """What one unit of the position actually costs if the stop is hit.

    The price move, *plus* both taker fees, *plus* a slippage allowance on both
    the entry and the exit. Sizing on the price move alone (as the first build
    did) understates every loss by the round trip cost, so a '0.5% risk' trade
    really risked more than 0.5%.
    """
    return (abs(entry - stop)
            + (entry + stop) * fee_pct / 100.0
            + (entry + stop) * slippage_pct / 100.0)


def risk_budget(equity, cfg):
    """(budget_usdt, which, detail) - the planned risk allowed on one trade.

    Two independent ceilings apply and the *stricter* one always wins:

      * `risk_per_trade_pct` of current equity - the normal risk target, which
        scales the position down as the account shrinks.
      * `max_risk_usdt_abs` - an absolute ceiling in USDT, which bounds the
        loss on any single trade however large the account becomes.

    The percentage alone is not a bound: 0.5% of a grown account is an
    unbounded number of USDT. The absolute cap alone is not a bound either, as
    it would let a shrinking account risk an ever larger share of itself. Only
    the minimum of the two is safe, so this function is the single place that
    decides it and every caller - sizing, the entry gates and the reports -
    goes through it.

    `which` is "pct" or "abs", naming the binding limit, so a message can say
    which one applied. A budget of 0.0 with which == "invalid" means the
    inputs could not be trusted and no trade may be sized.
    """
    if not is_finite(equity) or equity <= 0:
        return 0.0, "invalid", "equity is not a usable number"
    pct = _num(cfg, "risk_per_trade_pct")
    if pct is None or not is_finite(pct) or pct <= 0:
        return 0.0, "invalid", "risk_per_trade_pct is not a usable number"
    pct_budget = equity * pct / 100.0
    abs_cap = _num(cfg, "max_risk_usdt_abs")
    if abs_cap is None:
        # Absent rather than merely unset: fail closed. A missing absolute cap
        # must not silently become "no absolute cap".
        return 0.0, "invalid", "max_risk_usdt_abs is missing or not a number"
    if not is_finite(abs_cap) or abs_cap <= 0:
        return 0.0, "invalid", f"max_risk_usdt_abs is not usable ({abs_cap!r})"
    abs_cap = min(float(abs_cap), RISK_USDT_HARD_CAP)
    if not is_finite(pct_budget):
        return 0.0, "invalid", "percentage risk budget is not a finite number"
    if abs_cap < pct_budget:
        return abs_cap, "abs", f"{abs_cap:.4f} USDT absolute cap"
    return pct_budget, "pct", f"{pct}% of {equity:.4f} USDT equity"


def size_position(equity, entry, stop, rules, cfg, fractional=False):
    """Returns (qty, risk_usdt, note). qty == 0 means reject; note says why.

    risk_usdt is the full planned cost of being stopped out - price move, both
    fees and a slippage allowance on both legs - so the risk budget is a
    budget for the real loss, not for the nominal price move. The only thing
    that can still exceed it is a gap straight through the stop, which no
    position size prevents.

    `entry` must be the price the position will actually FILL at, not the
    quote the decision was made on. Those differ by entry slippage, and the
    difference always moves the fill *away* from the stop, so sizing on the
    pre-slippage quote understates the real stop distance - by about 7% of it
    at the tightest allowed stop. Callers compute the projected fill with
    `adverse_fill(..., entering=True)` and pass that in.

    The budget is the stricter of the percentage target and the absolute USDT
    cap; see `risk_budget`.

    What this function deliberately does NOT do
    -------------------------------------------
    It never raises the quantity to reach the exchange's minimum order size or
    minimum notional. The previous build did, up to a separate and much larger
    cap, which meant a '1.5% risk' trade on a small account could really risk
    3%. If the minimum would breach the cap the trade is rejected.

    With `fractional=True` (paper trading only) the quantity is rounded to
    PAPER_QTY_STEP and exchange minimums are ignored entirely, so a small
    account can still be simulated at its true risk instead of being pushed
    above it. Nothing on this path can reach a real order.
    """
    # Non-finite inputs first: a NaN passes every `>` and every `<=` test
    # below, so it would otherwise be sized, opened and journalled as a
    # NaN-quantity position. Reject before comparing anything.
    if not is_finite(equity, entry, stop):
        return 0.0, 0.0, ("rejected: price, stop or equity is not a finite number "
                          "(NaN/inf) - refusing to size")
    dist = abs(entry - stop)
    if entry <= 0 or dist <= 0 or equity <= 0:
        return 0.0, 0.0, "bad stop, bad price or empty balance"
    fee = _num(cfg, "taker_fee_pct")
    slip = _num(cfg, "slippage_pct")
    if fee is None or slip is None or not is_finite(fee, slip) or fee < 0 or slip < 0:
        return 0.0, 0.0, ("rejected: the fee or slippage setting is not a usable "
                          "number - risk cannot be calculated")
    unit = cost_per_unit(entry, stop, fee, slip)
    if not is_finite(unit) or unit <= 0:
        return 0.0, 0.0, "risk per unit is not positive - refusing to size"
    budget, which, detail = risk_budget(equity, cfg)
    if budget <= 0 or not is_finite(budget):
        return 0.0, 0.0, f"rejected: no usable risk budget ({detail})"
    qty = budget / unit
    if not is_finite(qty):
        return 0.0, 0.0, "rejected: the computed quantity is not a finite number"

    # Cap the notional so the implied leverage stays inside max_leverage. This
    # can only ever reduce the quantity, so it cannot raise the risk.
    max_notional = equity * cfg["max_leverage"] * 0.9  # leave room for fees/margin
    capped = False
    if qty * entry > max_notional:
        qty, capped = max_notional / entry, True

    step = PAPER_QTY_STEP if fractional else rules["step"]
    qty = floor_step(qty, step)
    if qty <= 0:
        return 0.0, 0.0, (f"position size rounds to zero at a risk budget of "
                          f"{budget:.4f} USDT ({detail})")

    note = ""
    if fractional:
        if qty < rules["min_qty"] or qty * entry < rules["min_notional"]:
            note = (f"simulated fractional size {fmt_qty(qty)} is below the exchange "
                    f"minimum ({fmt_qty(rules['min_qty'])} / "
                    f"${rules['min_notional']:.0f}) - paper only, risk kept at the cap")
        elif abs(qty - floor_step(qty, rules["step"])) > 1e-12:
            note = (f"simulated size {fmt_qty(qty)} is not on the exchange step "
                    f"{fmt_qty(rules['step'])} - paper only, risk kept at the cap")
    else:
        if qty < rules["min_qty"] or qty * entry < rules["min_notional"]:
            need = ceil_step(max(rules["min_notional"] / entry, rules["min_qty"]),
                             rules["step"])
            need_pct = need * unit / equity * 100
            return 0.0, 0.0, (
                f"rejected: the exchange minimum ({fmt_qty(rules['min_qty'])} / "
                f"${rules['min_notional']:.0f}) needs {fmt_qty(need)}, which would risk "
                f"{need * unit:.4f} USDT ({need_pct:.2f}% of equity) against a "
                f"{budget:.4f} USDT budget ({detail}). "
                f"Risk is never raised to meet a minimum")
    if capped:
        note = (note + "; " if note else "") + f"size capped by {cfg['max_leverage']}x leverage"
    if which == "abs":
        note = (note + "; " if note else "") + (
            f"size set by the {budget:.2f} USDT absolute risk cap, which is "
            f"stricter here than {cfg['risk_per_trade_pct']}% of equity")

    risk = qty * unit
    # The last gate before a quantity is handed to a broker. Flooring only ever
    # reduces the size, so this should be unreachable - but "should be" is not
    # a risk control, and a non-finite risk has to be caught here too because
    # every comparison above would have let it through.
    if not is_finite(risk) or risk <= 0:
        return 0.0, 0.0, "rejected: the computed risk is not a usable number"
    if risk > budget + 1e-9:
        return 0.0, 0.0, (f"rejected: computed risk {risk:.4f} USDT exceeds the "
                          f"{budget:.4f} USDT budget ({detail})")
    return qty, risk, note


def trade_pnl(side, entry, exit_px, qty, fee_pct):
    d = 1 if side == "LONG" else -1
    fees = (entry + exit_px) * qty * fee_pct / 100
    return d * (exit_px - entry) * qty - fees, fees


def stop_fill_price(side, stop, candle_open):
    """Where a stop actually fills inside a candle, before slippage.

    A stop is a market order. If the candle *opened* already beyond the stop
    level - a gap or a wick through it between two of our checks - the fill is
    the open, not the stop level. Filling at the stop level, as the first build
    did, quietly understates every gap loss.

    Targets get no equivalent benefit on purpose: a take profit always fills at
    exactly the target, even when the candle opened better than it. Crediting a
    favourable gap would flatter the results.
    """
    return min(stop, candle_open) if side == "LONG" else max(stop, candle_open)


def target_for(side, entry, stop, rr):
    return entry + rr * abs(entry - stop) * (1 if side == "LONG" else -1)


# ---------------------------------------------------------------- risk engine

class RiskManager:
    """Every hard limit, in one place, over the persisted state dict.

    The live loop and the backtest both drive this object, so a limit cannot
    behave differently in a test than it does with real prices. It owns these
    state keys and nothing else touches them:

        day, day_start_equity, equity_peak, realized_today, trades_today,
        losses_today, locked_day, halted, halt_reason, halted_at, last_loss_ms,
        month, month_start

    Every method is explicit about the equity it is given. The manager never
    reads a price or a balance itself - that keeps it testable and keeps 'I
    could not value the account' a decision the caller has to make, rather than
    a silent zero in here.
    """

    def __init__(self, cfg, state):
        self.cfg, self.s = cfg, state
        s = state
        s.setdefault("realized_today", 0.0)
        s.setdefault("trades_today", 0)
        s.setdefault("losses_today", 0)
        s.setdefault("locked_day", None)
        s.setdefault("halted", False)
        s.setdefault("halt_reason", "")
        s.setdefault("halted_at", None)
        s.setdefault("last_loss_ms", None)

    # ---- period handling -------------------------------------------------

    def roll(self, equity, ms=None, force=False):
        """Start a new trading day/month when the IST date has changed.

        The day's starting equity is captured once per day and then left alone,
        so a restart in the middle of a bad day does not hand the account a
        fresh 2% to lose.
        """
        s, today, month = self.s, day_key(ms), month_key(ms)
        if force or s.get("day") != today:
            s["day"] = today
            s["day_start_equity"] = float(equity)
            s["realized_today"] = 0.0
            s["trades_today"] = 0
            s["losses_today"] = 0
            if s.get("locked_day") != today:
                s["locked_day"] = None
        elif s.get("day_start_equity") is None:
            # Same day, but an older state file never recorded the starting
            # equity. Backfill it without wiping the day's counters: a restart
            # must not hand the account a fresh set of trades or losses.
            s["day_start_equity"] = float(equity)
        if force or s.get("month") != month:
            s["month"], s["month_start"] = month, float(equity)
        elif s.get("month_start") is None:
            s["month_start"] = float(equity)
        self.observe(equity)

    def observe(self, equity):
        """Record a new equity high. The peak only ever moves up, and persists."""
        peak = self.s.get("equity_peak")
        if peak is None or equity > peak:
            self.s["equity_peak"] = float(equity)
        return self.s["equity_peak"]

    # ---- measurements ----------------------------------------------------

    def day_start_equity(self, equity=None):
        v = self.s.get("day_start_equity")
        return float(equity if v is None else v)

    def peak(self, equity=None):
        v = self.s.get("equity_peak")
        return float(equity if v is None else v)

    def drawdown_pct(self, equity):
        peak = self.peak(equity)
        if peak <= 0:
            return 0.0
        return max(0.0, (peak - equity) / peak * 100.0)

    def daily_pnl(self, equity):
        return equity - self.day_start_equity(equity)

    def daily_loss_pct(self, equity):
        start = self.day_start_equity(equity)
        if start <= 0:
            return 0.0
        return max(0.0, (start - equity) / start * 100.0)

    def max_risk_usdt(self, equity):
        """The planned risk one trade may carry: the stricter of the two caps.

        Shares `risk_budget` with `size_position`, so the number the sizing
        function works to and the number the entry gates check against cannot
        drift apart. A non-usable equity yields 0.0, which blocks entries
        rather than permitting an unbounded one.
        """
        return risk_budget(equity, self.cfg)[0]

    def risk_cap_detail(self, equity):
        """(budget, which, detail) for the reports. "abs" means $1 is binding."""
        return risk_budget(equity, self.cfg)

    def daily_loss_budget(self, equity):
        return self.day_start_equity(equity) * self.cfg["max_daily_loss_pct"] / 100.0

    def remaining_daily_loss(self, equity):
        """How much more the account may lose today before the day is locked."""
        return max(0.0, self.daily_loss_budget(equity) + self.daily_pnl(equity))

    def trades_left_today(self):
        return max(0, self.cfg["max_trades_per_day"] - int(self.s.get("trades_today", 0)))

    def cooldown_left_sec(self, ms=None):
        last = self.s.get("last_loss_ms")
        if not last or self.cfg["loss_cooldown_min"] <= 0:
            return 0
        ms = now_ms() if ms is None else ms
        over = (ms - last) / 1000.0
        return max(0, int(self.cfg["loss_cooldown_min"] * 60 - over))

    def is_locked_today(self, ms=None):
        return self.s.get("locked_day") == day_key(ms)

    def is_halted(self):
        return bool(self.s.get("halted"))

    # ---- breaches --------------------------------------------------------

    def breach(self, equity):
        """A limit that demands immediate action, or None.

        Returns (kind, message). Drawdown is checked first: it is the more
        serious of the two and it halts the engine permanently.
        """
        dd = self.drawdown_pct(equity)
        if dd >= self.cfg["max_account_drawdown_pct"]:
            return ("drawdown",
                    f"account drawdown {dd:.2f}% has reached the "
                    f"{self.cfg['max_account_drawdown_pct']}% limit "
                    f"(peak {self.peak(equity):.2f} -> now {equity:.2f} USDT)")
        dl = self.daily_loss_pct(equity)
        if dl >= self.cfg["max_daily_loss_pct"]:
            return ("daily",
                    f"today's loss {dl:.2f}% has reached the "
                    f"{self.cfg['max_daily_loss_pct']}% daily limit "
                    f"(day start {self.day_start_equity(equity):.2f} -> now {equity:.2f} USDT)")
        return None

    def entry_blocks(self, equity, ms=None):
        """Every risk reason a new entry must not be taken. Empty list = clear."""
        cfg, s, out = self.cfg, self.s, []
        if self.is_halted():
            out.append(f"HALTED: {s.get('halt_reason') or 'manual halt'} "
                       f"(manual review needed - /clearhalt CONFIRM)")
        if self.is_locked_today(ms):
            out.append(f"daily loss limit hit - locked until the next trading day "
                       f"(IST). Resumes {day_key((now_ms() if ms is None else ms) + 86400000)}")
        dd = self.drawdown_pct(equity)
        if dd >= cfg["max_account_drawdown_pct"]:
            out.append(f"account drawdown {dd:.2f}% >= {cfg['max_account_drawdown_pct']}%")
        dl = self.daily_loss_pct(equity)
        if dl >= cfg["max_daily_loss_pct"]:
            out.append(f"today's loss {dl:.2f}% >= {cfg['max_daily_loss_pct']}%")
        if int(s.get("trades_today", 0)) >= cfg["max_trades_per_day"]:
            out.append(f"{s.get('trades_today', 0)} of {cfg['max_trades_per_day']} "
                       f"trades already taken today")
        if int(s.get("losses_today", 0)) >= cfg["max_losses_per_day"]:
            out.append(f"{s.get('losses_today', 0)} losses today - done for the day")
        cool = self.cooldown_left_sec(ms)
        if cool > 0:
            out.append(f"cooling off after a loss for another {cool // 60}m{cool % 60:02d}s "
                       f"(no revenge trades)")
        month_start = s.get("month_start")
        if month_start is None:
            month_start = s["month_start"] = float(equity)
        floor = month_start * (1 - cfg["monthly_drawdown_stop_pct"] / 100)
        if equity <= floor:
            out.append(f"down {cfg['monthly_drawdown_stop_pct']}% this month - "
                       f"stopped until next month")
        # Fail closed on an unusable equity or an unusable risk setting: a
        # budget of zero must read as "cannot size a trade", never as "no
        # limit applies".
        if not is_finite(equity):
            out.append("equity is not a finite number - refusing to trade on it")
            return out
        planned, _which, detail = self.risk_cap_detail(equity)
        if planned <= 0:
            out.append(f"no usable per-trade risk budget ({detail})")
            return out
        # A new entry must still fit inside what is left of the day's budget.
        left = self.remaining_daily_loss(equity)
        if planned > left + 1e-9:
            out.append(f"a {planned:.4f} USDT trade does not fit in the "
                       f"{left:.4f} USDT left of today's loss budget")
        return out

    # ---- transitions -----------------------------------------------------

    def record_open(self, ms=None):
        self.s["trades_today"] = int(self.s.get("trades_today", 0)) + 1

    def record_close(self, pnl, ms=None):
        ms = now_ms() if ms is None else ms
        self.s["realized_today"] = float(self.s.get("realized_today", 0.0)) + float(pnl)
        if pnl < 0:
            self.s["losses_today"] = int(self.s.get("losses_today", 0)) + 1
            self.s["last_loss_ms"] = ms

    def lock_day(self, ms=None):
        self.s["locked_day"] = day_key(ms)

    def halt(self, reason, ms=None):
        self.s["halted"] = True
        self.s["halt_reason"] = str(reason)
        self.s["halted_at"] = now_ms() if ms is None else ms

    def clear_halt(self, equity, ms=None):
        """Lift a halt after a manual review.

        The equity peak is re-baselined to the reviewed equity, otherwise the
        drawdown breach would fire again on the very next loop and the 'manual
        review' would be theatre. The old peak is kept in the state file so the
        history of the account is not lost.
        """
        s = self.s
        s.setdefault("halt_history", []).append({
            "cleared_at": ist().strftime("%Y-%m-%d %H:%M:%S"),
            "reason": s.get("halt_reason", ""),
            "old_peak": s.get("equity_peak"),
            "equity": float(equity)})
        s["halted"], s["halt_reason"], s["halted_at"] = False, "", None
        s["equity_peak"] = float(equity)
        s["locked_day"] = None
        return s["equity_peak"]

    # ---- reporting -------------------------------------------------------

    def limits_text(self, equity, inr=None):
        cfg = self.cfg
        budget, which, _detail = self.risk_cap_detail(equity)
        pct_only = max(0.0, equity) * cfg["risk_per_trade_pct"] / 100.0
        abs_cap = min(float(cfg.get("max_risk_usdt_abs") or 0.0), RISK_USDT_HARD_CAP)
        lines = [
            "🛡 Risk limits (paper)",
            f"Risk per trade: {cfg['risk_per_trade_pct']}% of equity "
            f"= {pct_only:.4f} USDT (fees + slippage included)",
            f"  absolute ceiling: {abs_cap:.2f} USDT per trade",
            f"  → applies now: {budget:.4f} USDT "
            f"({'absolute ceiling' if which == 'abs' else 'percentage target'} "
            f"is the stricter one)",
            f"Daily loss limit: {cfg['max_daily_loss_pct']}% of the day's start "
            f"({self.day_start_equity(equity):.2f} USDT)",
            f"  today so far: {self.daily_pnl(equity):+.4f} USDT "
            f"({-self.daily_loss_pct(equity):+.2f}%) | "
            f"left to risk: {self.remaining_daily_loss(equity):.4f} USDT",
            f"Max account drawdown: {cfg['max_account_drawdown_pct']}% from peak "
            f"{self.peak(equity):.2f} USDT | now {self.drawdown_pct(equity):.2f}%",
            f"Monthly stop: {cfg['monthly_drawdown_stop_pct']}% from "
            f"{float(self.s.get('month_start') or equity):.2f} USDT",
            f"Trades today: {int(self.s.get('trades_today', 0))}/"
            f"{cfg['max_trades_per_day']} | losses {int(self.s.get('losses_today', 0))}/"
            f"{cfg['max_losses_per_day']} | left: {self.trades_left_today()}",
            f"Max open positions: {cfg['max_open_positions']} | "
            f"loss cooldown: {cfg['loss_cooldown_min']}m",
            f"Fees {cfg['taker_fee_pct']}%/side, slippage allowance "
            f"{cfg['slippage_pct']}%/side",
            "Mandatory stop-loss. No averaging down, no martingale, no risk "
            "increase after a loss, no widening a stop.",
            "⚠️ A stop bounds the *planned* loss, not the worst case. A gap or "
            "a fast move straight through the stop level fills beyond it, and "
            "no position size prevents that - the simulator models such fills "
            "at the candle open rather than pretending the stop held.",
        ]
        cool = self.cooldown_left_sec()
        if cool:
            lines.append(f"Cooling off for another {cool // 60}m{cool % 60:02d}s")
        if self.is_locked_today():
            lines.append("⛔ Locked for today (daily loss limit)")
        if self.is_halted():
            lines.append(f"🚨 HALTED: {self.s.get('halt_reason')}")
        return "\n".join(lines)


# ---------------------------------------------------------------- trade journal

JOURNAL_FIELDS = [
    "trade_id", "mode", "symbol", "side", "strategy",
    "opened_ist", "closed_ist", "opened_ms", "closed_ms",
    "entry", "exit", "qty", "initial_stop", "take_profit",
    "planned_risk", "pnl", "fees", "slippage", "r_multiple",
    "exit_reason", "market_condition", "equity_before", "equity_after",
]


def journal(row, path=None):
    """Append one closed trade to the CSV journal.

    If the file already carries a different header - a journal from an older
    build, say - the rows would be appended under the wrong column names and
    silently mis-align. The old file is set aside (renamed, never deleted)
    instead, so no completed trade is ever lost.
    """
    path = JOURNAL_FILE if path is None else path
    fields = list(row)
    new = not os.path.exists(path) or os.path.getsize(path) == 0
    if not new:
        try:
            with open(path, newline="", encoding="utf-8") as f:
                header = next(csv.reader(f), [])
        except Exception:
            header = []
        if header and header != fields:
            moved = f"{path}.{datetime.now().strftime('%Y%m%d-%H%M%S')}.old"
            os.replace(path, moved)
            say(f"(notice: {path} had a different header; kept the old one as {moved})")
            new = True
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        if new:
            w.writeheader()
        w.writerow(row)


def journal_row(pos, res, mode, equity_before, equity_after, closed_ms=None):
    """Build one complete journal record from a position and its exit."""
    closed_ms = now_ms() if closed_ms is None else closed_ms
    planned = float(pos.get("planned_risk") or 0.0)
    pnl = float(res["pnl"])
    return {
        "trade_id": pos.get("trade_id") or new_trade_id(),
        "mode": mode,
        "symbol": pos["symbol"],
        "side": pos["side"],
        "strategy": pos.get("strategy") or STRATEGY_NAME,
        "opened_ist": ist(pos["opened"]).strftime("%Y-%m-%d %H:%M:%S"),
        "closed_ist": ist(closed_ms).strftime("%Y-%m-%d %H:%M:%S"),
        "opened_ms": int(pos["opened"]),
        "closed_ms": int(closed_ms),
        "entry": round(float(pos["entry"]), 8),
        "exit": round(float(res["exit"]), 8),
        "qty": float(pos["qty"]),
        "initial_stop": round(float(pos.get("initial_stop", pos["stop"])), 8),
        "take_profit": round(float(pos["target"]), 8),
        "planned_risk": round(planned, 6),
        "pnl": round(pnl, 6),
        "fees": None if res.get("fees") is None else round(float(res["fees"]), 6),
        "slippage": None if res.get("slippage") is None else round(float(res["slippage"]), 6),
        "r_multiple": round(pnl / planned, 4) if planned > 0 else None,
        "exit_reason": res["reason"],
        "market_condition": pos.get("market_condition") or "",
        "equity_before": round(float(equity_before), 6),
        "equity_after": round(float(equity_after), 6),
    }


def _f(row, key, default=0.0):
    try:
        v = row.get(key)
        if v in (None, ""):
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


_JOURNAL_CACHE = {}       # path -> (mtime_ns, size, rows)
_JOURNAL_LOCK = threading.Lock()


def _journal_rows(path):
    """Parsed CSV rows for `path`, reused while the file has not changed.

    /performance, /history and /health all read the journal, and a Telegram
    command should not re-parse the whole file to answer. The cache key is the
    file's mtime and size, so any write - by this process or any other - is
    picked up immediately.
    """
    try:
        st = os.stat(path)
        key = (st.st_mtime_ns, st.st_size)
    except OSError:
        return None
    with _JOURNAL_LOCK:
        hit = _JOURNAL_CACHE.get(path)
        if hit and hit[0] == key[0] and hit[1] == key[1]:
            return hit[2]
    try:
        with open(path, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    except Exception as e:
        say(f"(warning: could not read the trade journal {path}: {type(e).__name__}: {e})")
        return None
    with _JOURNAL_LOCK:
        _JOURNAL_CACHE[path] = (key[0], key[1], rows)
    return rows


def load_journal(path=None, mode=None):
    """Read completed trades from the CSV, de-duplicated and validated.

    Only rows that describe a *finished* trade are returned: a trade id, a
    closing timestamp and a numeric P&L are all required. An unfinished or
    duplicated row is skipped, never guessed at, because a performance figure
    computed from a half-written row is worse than no figure at all.
    """
    path = JOURNAL_FILE if path is None else path
    if not os.path.exists(path):
        return []
    raw = _journal_rows(path)
    if raw is None:
        return []
    out, seen = [], set()
    for r in raw:
        tid = (r.get("trade_id") or "").strip()
        if not tid or tid in seen:
            continue
        if (r.get("pnl") in (None, "")) or (r.get("side") not in ("LONG", "SHORT")):
            continue
        try:
            float(r["pnl"])
            # A finished trade always carries a machine-readable exit time. A
            # row without one is either half-written or hand-edited, and is not
            # guessed at.
            int(float(r.get("closed_ms")))
        except (TypeError, ValueError):
            continue
        if mode and (r.get("mode") or "") != mode:
            continue
        seen.add(tid)
        out.append(r)
    out.sort(key=lambda r: (_f(r, "closed_ms"), r.get("closed_ist") or ""))
    return out


def _bucket(rows):
    pnls = [_f(r, "pnl") for r in rows]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_profit, gross_loss = sum(wins), -sum(losses)
    n = len(pnls)
    return {
        "trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "breakeven": n - len(wins) - len(losses),
        "win_rate": (len(wins) / n * 100) if n else 0.0,
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "net_pnl": sum(pnls),
        "profit_factor": (gross_profit / gross_loss) if gross_loss > 0
        else (float("inf") if gross_profit > 0 else 0.0),
        "avg_win": (gross_profit / len(wins)) if wins else 0.0,
        "avg_loss": (gross_loss / len(losses)) if losses else 0.0,
        "expectancy": (sum(pnls) / n) if n else 0.0,
    }


def performance_stats(rows, start_equity=None):
    """Every requested statistic, computed only from finished trades.

    `rows` are journal records (or anything with the same keys). Nothing here
    estimates, interpolates or includes an open position: with no closed trades
    the result is a well-formed zero, and the caller says 'no trades yet'.
    """
    rows = list(rows)
    stats = dict(_bucket(rows))
    stats["sample_size"] = len(rows)
    stats["total_fees"] = sum(_f(r, "fees") for r in rows)
    stats["total_slippage"] = sum(_f(r, "slippage") for r in rows)
    stats["planned_risk_total"] = sum(_f(r, "planned_risk") for r in rows)
    rs = [_f(r, "r_multiple") for r in rows if (r.get("r_multiple") not in (None, ""))]
    stats["avg_r"] = (sum(rs) / len(rs)) if rs else 0.0
    stats["realized_rr"] = stats["avg_r"]

    # equity curve and maximum drawdown
    if start_equity is None:
        start_equity = _f(rows[0], "equity_before", 0.0) if rows else 0.0
    eq = float(start_equity)
    peak, max_dd, max_dd_pct = eq, 0.0, 0.0
    curve = [eq]
    for r in rows:
        eq += _f(r, "pnl")
        curve.append(eq)
        peak = max(peak, eq)
        if peak > 0:
            max_dd = max(max_dd, peak - eq)
            max_dd_pct = max(max_dd_pct, (peak - eq) / peak * 100)
    stats["start_equity"] = float(start_equity)
    stats["end_equity"] = eq
    stats["equity_curve"] = curve
    stats["max_drawdown"] = max_dd
    stats["max_drawdown_pct"] = max_dd_pct

    # streaks
    best_w = best_l = cur_w = cur_l = 0
    for r in rows:
        p = _f(r, "pnl")
        if p > 0:
            cur_w, cur_l = cur_w + 1, 0
        elif p < 0:
            cur_l, cur_w = cur_l + 1, 0
        else:
            cur_w = cur_l = 0
        best_w, best_l = max(best_w, cur_w), max(best_l, cur_l)
    stats["max_consecutive_wins"] = best_w
    stats["max_consecutive_losses"] = best_l
    stats["current_win_streak"] = cur_w
    stats["current_loss_streak"] = cur_l

    stats["by_side"] = {side: _bucket([r for r in rows if r.get("side") == side])
                        for side in ("LONG", "SHORT")}
    symbols = sorted({(r.get("symbol") or "?") for r in rows})
    stats["by_symbol"] = {sym: _bucket([r for r in rows if r.get("symbol") == sym])
                          for sym in symbols}
    stats["by_reason"] = {}
    for r in rows:
        key = r.get("exit_reason") or "?"
        b = stats["by_reason"].setdefault(key, {"trades": 0, "net_pnl": 0.0})
        b["trades"] += 1
        b["net_pnl"] += _f(r, "pnl")
    stats["by_condition"] = {}
    for r in rows:
        key = (r.get("market_condition") or "").split(";")[0].strip() or "?"
        b = stats["by_condition"].setdefault(key, {"trades": 0, "net_pnl": 0.0})
        b["trades"] += 1
        b["net_pnl"] += _f(r, "pnl")

    daily, weekly = {}, {}
    for r in rows:
        ms = _f(r, "closed_ms", None)
        if ms:
            d, w = day_key(int(ms)), week_key(int(ms))
        else:
            d = (r.get("closed_ist") or "")[:10] or "?"
            w = "?"
        daily[d] = daily.get(d, 0.0) + _f(r, "pnl")
        weekly[w] = weekly.get(w, 0.0) + _f(r, "pnl")
    stats["daily"] = dict(sorted(daily.items()))
    stats["weekly"] = dict(sorted(weekly.items()))
    return stats


def _pf(v):
    return "inf" if v == float("inf") else f"{v:.2f}"


def format_performance(stats, title="📈 Performance (closed paper trades)"):
    if not stats["sample_size"]:
        return (f"{title}\nNo closed trades yet. Statistics are only ever computed "
                f"from completed, journalled trades.")
    lines = [
        title,
        f"Closed trades: {stats['trades']} (wins {stats['wins']}, "
        f"losses {stats['losses']}, flat {stats['breakeven']})",
        f"Win rate: {stats['win_rate']:.1f}%",
        f"Net P&L after fees: {stats['net_pnl']:+.4f} USDT",
        f"Gross profit {stats['gross_profit']:.4f} | gross loss {stats['gross_loss']:.4f}",
        f"Profit factor: {_pf(stats['profit_factor'])}",
        f"Avg win {stats['avg_win']:.4f} | avg loss {stats['avg_loss']:.4f}",
        f"Expectancy/trade: {stats['expectancy']:+.4f} USDT | "
        f"realised R: {stats['avg_r']:+.2f}",
        f"Max drawdown: {stats['max_drawdown']:.4f} USDT "
        f"({stats['max_drawdown_pct']:.2f}%)",
        f"Fees paid {stats['total_fees']:.4f} | slippage {stats['total_slippage']:.4f}",
        f"Streaks: {stats['max_consecutive_wins']}W / "
        f"{stats['max_consecutive_losses']}L (now "
        f"{stats['current_win_streak']}W/{stats['current_loss_streak']}L)",
    ]
    for side in ("LONG", "SHORT"):
        b = stats["by_side"][side]
        if b["trades"]:
            lines.append(f"{side}: {b['trades']} trades, {b['win_rate']:.0f}% win, "
                         f"{b['net_pnl']:+.4f} USDT, PF {_pf(b['profit_factor'])}")
    for sym, b in stats["by_symbol"].items():
        if b["trades"]:
            lines.append(f"{sym}: {b['trades']} trades, {b['win_rate']:.0f}% win, "
                         f"{b['net_pnl']:+.4f} USDT, PF {_pf(b['profit_factor'])}")
    days = list(stats["daily"].items())[-7:]
    if days:
        lines.append("Last days: " + " | ".join(f"{d[5:]} {p:+.3f}" for d, p in days))
    weeks = list(stats["weekly"].items())[-4:]
    if weeks:
        lines.append("Last weeks: " + " | ".join(f"{w} {p:+.3f}" for w, p in weeks))
    if stats["sample_size"] < 30:
        lines.append(f"⚠️ {stats['sample_size']} trades is far too small a sample to "
                     f"conclude anything about the strategy.")
    return "\n".join(lines)


def format_history(rows, limit=8):
    rows = list(rows)[-limit:]
    if not rows:
        return "🧾 No completed trades in the journal yet."
    out = [f"🧾 Last {len(rows)} completed trade(s)"]
    for r in rows:
        pnl = _f(r, "pnl")
        mark = "✅" if pnl > 0 else ("➖" if pnl == 0 else "❌")
        rm = r.get("r_multiple")
        rtxt = f" | {float(rm):+.2f}R" if rm not in (None, "") else ""
        out.append(f"{mark} {r.get('closed_ist', '')[5:16]} {r.get('side')} "
                   f"{r.get('symbol')} {_f(r, 'entry'):.4f}→{_f(r, 'exit'):.4f} "
                   f"{pnl:+.4f} USDT{rtxt} ({r.get('exit_reason')})")
    return "\n".join(out)


# ---------------------------------------------------------------- performance

class Perf:
    """Lightweight timing, shared between threads.

    Deliberately tiny: a dict of running totals per named operation plus a few
    counters. No history is kept, so it cannot grow without bound, and it holds
    nothing that could identify a request - only durations - so it is safe to
    print in a Telegram reply.
    """

    def __init__(self, slow_ms=None):
        self.lock = threading.Lock()
        self.ops = {}                 # name -> {n, total_ms, max_ms, last_ms, slow}
        self.api_timeouts = 0
        self.api_errors = 0
        self.telegram_errors = 0
        self.slow_ms = dict(slow_ms or {})
        self.started = time.time()

    def threshold(self, name):
        for prefix, limit in self.slow_ms.items():
            if name.startswith(prefix):
                return limit
        return None

    def record(self, name, ms):
        limit = self.threshold(name)
        with self.lock:
            o = self.ops.setdefault(name, {"n": 0, "total_ms": 0.0, "max_ms": 0.0,
                                           "last_ms": 0.0, "slow": 0})
            o["n"] += 1
            o["total_ms"] += ms
            o["last_ms"] = ms
            o["max_ms"] = max(o["max_ms"], ms)
            if limit is not None and ms > limit:
                o["slow"] += 1
        return ms

    def timer(self, name):
        return _Timer(self, name)

    def bump(self, counter, by=1):
        with self.lock:
            setattr(self, counter, getattr(self, counter, 0) + by)

    def snapshot(self):
        with self.lock:
            return ({k: dict(v) for k, v in self.ops.items()},
                    {"api_timeouts": self.api_timeouts,
                     "api_errors": self.api_errors,
                     "telegram_errors": self.telegram_errors})

    def slowest(self):
        ops, _ = self.snapshot()
        if not ops:
            return None
        name = max(ops, key=lambda k: ops[k]["max_ms"])
        return name, ops[name]["max_ms"]

    def report_lines(self, limit=8):
        ops, counters = self.snapshot()
        out = [f"Timings (since start, {len(ops)} operation kinds):"]
        rows = sorted(ops.items(), key=lambda kv: -kv[1]["total_ms"])[:limit]
        for name, o in rows:
            avg = o["total_ms"] / o["n"] if o["n"] else 0.0
            flag = f"  ⚠️ {o['slow']} slow" if o["slow"] else ""
            out.append(f"  {name}: {o['n']}x avg {avg:.0f}ms "
                       f"max {o['max_ms']:.0f}ms last {o['last_ms']:.0f}ms{flag}")
        worst = self.slowest()
        if worst:
            out.append(f"  slowest single operation: {worst[0]} at {worst[1]:.0f}ms")
        out.append(f"  API timeouts: {counters['api_timeouts']} | API errors: "
                   f"{counters['api_errors']} | Telegram errors: "
                   f"{counters['telegram_errors']}")
        return out


class _Timer:
    def __init__(self, perf, name):
        self.perf, self.name, self.ms = perf, name, 0.0

    def __enter__(self):
        self.t0 = time.perf_counter()
        return self

    def __exit__(self, *exc):
        self.ms = (time.perf_counter() - self.t0) * 1000
        if self.perf is not None:
            self.perf.record(self.name, self.ms)
        return False


# a process-wide default so helpers can record without being handed one
PERF = Perf()


def perf_thresholds(cfg):
    return {"http": cfg.get("slow_http_ms", 2000),
            "telegram": cfg.get("slow_http_ms", 2000),
            "cmd": cfg.get("slow_command_ms", 1000),
            "cycle": cfg.get("slow_cycle_ms", 5000),
            "broker": cfg.get("slow_cycle_ms", 5000)}


# ---------------------------------------------------------------- exchange

class ApiError(Exception):
    def __init__(self, status, data):
        self.status = status
        self.code = data.get("code") if isinstance(data, dict) else None
        msg = data.get("msg") if isinstance(data, dict) else str(data)
        super().__init__(f"HTTP {status} code={self.code} {msg}")


class ReadOnlyClient(PermissionError):
    """Raised when a public-data-only client is asked to do anything else."""


class Binance:
    """Binance USDT-M futures REST client.

    With public_only=True the client is structurally incapable of placing an
    order: no credentials are loaded at all, and any signed request or any
    non-GET method raises before a socket is opened. Backtest, research and
    paper mode build the client this way, so "paper mode cannot trade" is
    enforced by the object itself and not merely by which branch of main() ran.
    """

    def __init__(self, cfg, base=None, public_only=False, perf=None):
        self.base = base or (TESTNET_BASE if cfg["use_testnet"] else LIVE_BASE)
        self.public_only = bool(public_only)
        # A read-only client never even holds the credentials.
        self.key = "" if self.public_only else cfg["api_key"]
        self.secret = b"" if self.public_only else (cfg["api_secret"] or "").encode()
        # One Session for the whole process: it keeps the TLS connection to
        # Binance alive, so every request after the first skips the handshake.
        self.session = requests.Session()
        self.offset = 0
        self.perf = perf or PERF
        # Separate connect and read budgets. A single GET used to be able to
        # block for 15 + 1.5 + 15 + 3 + 15 = 49.5 s, during which the one
        # thread that polled Telegram was inside a socket read.
        self.timeout = (float(cfg.get("http_connect_timeout_sec", 5.0)),
                        float(cfg.get("http_read_timeout_sec", 10.0)))
        self.tries = int(cfg.get("http_retries", 2))

    RETRY_STATUS = (408, 418, 429, 500, 502, 503, 504)

    def _req(self, method, path, params=None, signed=False, tries=None):
        tries = self.tries if tries is None else tries
        method = method.upper()
        if self.public_only and (signed or method != "GET"):
            raise ReadOnlyClient(
                f"read-only client refused {method} {path} (signed={signed}). "
                f"Backtest and paper mode can never place or cancel an order.")
        params = dict(params or {})
        # Only idempotent reads are ever retried. Retrying a POST could submit
        # the same order twice.
        if method != "GET":
            tries = 1
        last = None
        for attempt in range(tries):
            headers = {}
            if signed:
                params["timestamp"] = now_ms() + self.offset
                params["recvWindow"] = 5000
                q = urlencode(params)
                q += "&signature=" + hmac.new(self.secret, q.encode(),
                                              hashlib.sha256).hexdigest()
                headers["X-MBX-APIKEY"] = self.key
            else:
                q = urlencode(params)
            try:
                with self.perf.timer(f"http {path}"):
                    r = self.session.request(
                        method, self.base + path + ("?" + q if q else ""),
                        headers=headers, timeout=self.timeout)
            except Exception as e:  # DNS failure, timeout, reset connection
                last = e
                if "timeout" in type(e).__name__.lower() or "timed out" in str(e).lower():
                    self.perf.bump("api_timeouts")
                else:
                    self.perf.bump("api_errors")
                if attempt == tries - 1:
                    raise
                time.sleep(0.75 * (attempt + 1))
                continue
            try:
                data = r.json()
            except ValueError:
                data = {"msg": r.text[:200]}
            if r.status_code in self.RETRY_STATUS and attempt < tries - 1:
                wait = 0.0
                try:
                    wait = float(getattr(r, "headers", {}).get("Retry-After") or 0)
                except (TypeError, ValueError):
                    wait = 0.0
                # Bounded: a Retry-After of several minutes must not park the
                # engine inside a sleep. Give up instead and fail closed.
                time.sleep(min(max(wait, 0.75 * (attempt + 1)), 5))
                continue
            if r.status_code >= 400:
                self.perf.bump("api_errors")
                raise ApiError(r.status_code, data)
            return data
        raise last if last else ApiError(599, {"msg": "request failed"})

    # public
    def sync_time(self):
        self.offset = self._req("GET", "/fapi/v1/time")["serverTime"] - now_ms()

    def klines(self, symbol, interval, limit=500, start=None, end=None):
        p = {"symbol": symbol, "interval": interval, "limit": limit}
        if start is not None:
            p["startTime"] = start
        if end is not None:
            p["endTime"] = end
        raw = self._req("GET", "/fapi/v1/klines", p)
        return [{"t": int(r[0]), "o": float(r[1]), "h": float(r[2]), "l": float(r[3]),
                 "c": float(r[4]), "T": int(r[6])} for r in raw]

    def klines_range(self, symbol, interval, start, end):
        out, cur = {}, start
        while cur < end:
            batch = self.klines(symbol, interval, 1500, cur, end)
            for c in batch:
                out[c["t"]] = c
            if len(batch) < 1500:
                break
            cur = batch[-1]["t"] + 1
            time.sleep(0.25)
        return [out[t] for t in sorted(out)]

    def closed_klines(self, symbol, interval, limit=500):
        t = now_ms() + self.offset
        return [c for c in self.klines(symbol, interval, limit) if c["T"] < t]

    def rules(self, symbols):
        info = self._req("GET", "/fapi/v1/exchangeInfo")
        out = {}
        for s in info["symbols"]:
            if s["symbol"] not in symbols:
                continue
            f = {x["filterType"]: x for x in s["filters"]}
            lot = f.get("MARKET_LOT_SIZE") or f["LOT_SIZE"]
            out[s["symbol"]] = {
                "step": float(lot["stepSize"]),
                "min_qty": float(lot["minQty"]),
                "min_notional": float(f.get("MIN_NOTIONAL", {}).get("notional", 5)),
                "tick": float(f["PRICE_FILTER"]["tickSize"]),
            }
        missing = set(symbols) - set(out)
        if missing:
            raise SystemExit(f"Unknown symbols on this exchange: {sorted(missing)}")
        return out

    def price(self, symbol):
        return float(self._req("GET", "/fapi/v1/ticker/price", {"symbol": symbol})["price"])

    def funding_pct(self, symbol):
        return float(self._req("GET", "/fapi/v1/premiumIndex", {"symbol": symbol})["lastFundingRate"]) * 100

    # signed
    def _v3_or_v2(self, name, params=None):
        try:
            return self._req("GET", f"/fapi/v3/{name}", params, signed=True)
        except ApiError as e:
            if e.status == 404:
                return self._req("GET", f"/fapi/v2/{name}", params, signed=True)
            raise

    def balance(self):
        for a in self._v3_or_v2("balance"):
            if a["asset"] == "USDT":
                return float(a["balance"])
        return 0.0

    def position_amt(self, symbol):
        rows = self._v3_or_v2("positionRisk", {"symbol": symbol})
        return sum(float(r["positionAmt"]) for r in rows if r["symbol"] == symbol)

    def hedge_mode(self):
        return bool(self._req("GET", "/fapi/v1/positionSide/dual", signed=True)["dualSidePosition"])

    def ensure_isolated(self, symbol):
        try:
            self._req("POST", "/fapi/v1/marginType", {"symbol": symbol, "marginType": "ISOLATED"}, signed=True)
        except ApiError as e:
            if e.code != -4046:  # "No need to change margin type"
                raise

    def set_leverage(self, symbol, lev):
        self._req("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": int(lev)}, signed=True)

    def market(self, symbol, side, qty_str, reduce_only=False):
        p = {"symbol": symbol, "side": side, "type": "MARKET", "quantity": qty_str,
             "newOrderRespType": "RESULT"}
        if reduce_only:
            p["reduceOnly"] = "true"
        return self._req("POST", "/fapi/v1/order", p, signed=True)

    def close_all_order(self, symbol, side, otype, trigger_str):
        # Since Dec 2025 conditional orders (STOP_MARKET / TAKE_PROFIT_MARKET) go through the Algo API.
        return self._req("POST", "/fapi/v1/algoOrder", {
            "algoType": "CONDITIONAL", "symbol": symbol, "side": side, "type": otype,
            "triggerPrice": trigger_str, "closePosition": "true",
            "workingType": "MARK_PRICE", "priceProtect": "true"}, signed=True)

    def open_algo_orders(self, symbol):
        return self._req("GET", "/fapi/v1/openAlgoOrders", {"symbol": symbol}, signed=True)

    def cancel_algo_orders(self, symbol):
        return self._req("DELETE", "/fapi/v1/algoOpenOrders", {"symbol": symbol}, signed=True)


# ---------------------------------------------------------------- market data cache

class MarketData:
    """A caching, read-only view of the exchange with explicit freshness.

    It exposes the same methods as `Binance`, so it can be dropped in wherever
    a client is passed. What it adds is a timestamp on everything and a rule
    about who may use stale data:

      * `price(symbol)` reuses a ticker for `price_cache_sec` (5s). That window
        exists only to stop one trading cycle fetching the same price three or
        four times; it is far shorter than the cycle itself.
      * `fresh_price(symbol)` always goes to the network. **The trade-entry
        path uses this**, so a cached price can never be the basis of a new
        position.
      * `price_cached(symbol, max_age_sec)` never goes to the network at all.
        Informational commands use it and print the age they were given, so a
        report cannot present a stale number as current.
      * Closed candles are cached until the next candle is due to close. There
        is no new closed candle to fetch before then, so this is exact rather
        than a guess - and it turns 1h/4h kline downloads from once a minute
        into once an hour.

    None of this can weaken a freshness check: `Bot.data_problem()` judges
    candles by their own close time, not by when they were fetched, so an
    outage shows up as stale data however the cache behaves.
    """

    KLINE_GRACE_MS = 1500   # let the exchange publish the candle it just closed

    def __init__(self, client, cfg, perf=None):
        self.client, self.cfg = client, cfg
        self.perf = perf or PERF
        self.lock = threading.RLock()
        self._px = {}        # symbol -> (price, fetched_ms)
        self._kl = {}        # (symbol, interval) -> (candles, limit, fetched_ms)
        self._fund = {}      # symbol -> (pct, fetched_ms)
        self._rules = None
        self.hits = 0
        self.misses = 0

    # ---- plumbing --------------------------------------------------------

    @property
    def base(self):
        return self.client.base

    @property
    def public_only(self):
        return getattr(self.client, "public_only", True)

    def sync_time(self):
        return self.client.sync_time()

    def rules(self, symbols):
        """Exchange rules change rarely; one fetch per process is enough."""
        with self.lock:
            if self._rules is not None and set(symbols) <= set(self._rules):
                return {s: self._rules[s] for s in symbols}
        got = self.client.rules(symbols)
        with self.lock:
            self._rules = dict(got)
        return got

    def klines(self, symbol, interval, limit=500, start=None, end=None):
        """Raw passthrough: the 1-minute position scan is never cached."""
        return self.client.klines(symbol, interval, limit, start, end)

    def klines_range(self, symbol, interval, start, end):
        return self.client.klines_range(symbol, interval, start, end)

    # ---- prices ----------------------------------------------------------

    def price(self, symbol, max_age_sec=None):
        age = self.cfg.get("price_cache_sec", 5) if max_age_sec is None else max_age_sec
        if age <= 0:                      # a zero window means never reuse
            return self.fresh_price(symbol)
        px, _got_age = self.price_cached(symbol, age)
        if px is not None:
            return px
        return self.fresh_price(symbol)

    def fresh_price(self, symbol):
        """Always a network round trip. Used for every trade decision."""
        with self.perf.timer("md fresh_price"):
            px = float(self.client.price(symbol))
        with self.lock:
            self.misses += 1
            self._px[symbol] = (px, now_ms())
        return px

    def price_cached(self, symbol, max_age_sec=None):
        """(price, age_ms) if one is cached and young enough, else (None, None)."""
        with self.lock:
            hit = self._px.get(symbol)
        if not hit:
            return None, None
        px, fetched = hit
        age = now_ms() - fetched
        if max_age_sec is not None and age > max_age_sec * 1000:
            return None, None
        with self.lock:
            self.hits += 1
        return px, age

    def price_age_ms(self, symbol):
        with self.lock:
            hit = self._px.get(symbol)
        return None if not hit else now_ms() - hit[1]

    # ---- candles ---------------------------------------------------------

    def closed_klines(self, symbol, interval, limit=500):
        cached, _age = self.klines_cached(symbol, interval, limit)
        if cached is not None:
            return cached
        with self.perf.timer(f"md klines {interval}"):
            got = self.client.closed_klines(symbol, interval, limit)
        with self.lock:
            self.misses += 1
            if got:
                self._kl[(symbol, interval)] = (got, limit, now_ms())
        return got

    def klines_cached(self, symbol, interval, limit=0, allow_overdue=False):
        """(candles, age_ms) while no newer candle can exist yet, else (None, None).

        With allow_overdue=True an expired entry is still returned, for a
        report that wants to say "as of 12 minutes ago" rather than nothing.
        """
        with self.lock:
            hit = self._kl.get((symbol, interval))
        if not hit:
            return None, None
        candles, had_limit, fetched = hit
        if limit and had_limit < limit:
            return None, None
        age = now_ms() - fetched
        due = candles[-1]["T"] + interval_ms(interval) + self.KLINE_GRACE_MS
        if not allow_overdue and now_ms() >= due:
            return None, None
        with self.lock:
            self.hits += 1
        return candles, age

    # ---- funding ---------------------------------------------------------

    def funding_pct(self, symbol, max_age_sec=None):
        age = (self.cfg.get("funding_cache_sec", 60)
               if max_age_sec is None else max_age_sec)
        with self.lock:
            hit = self._fund.get(symbol)
        if age > 0 and hit and now_ms() - hit[1] <= age * 1000:
            with self.lock:
                self.hits += 1
            return hit[0]
        with self.perf.timer("md funding"):
            pct = float(self.client.funding_pct(symbol))
        with self.lock:
            self.misses += 1
            self._fund[symbol] = (pct, now_ms())
        return pct

    # ---- reporting -------------------------------------------------------

    def invalidate(self, symbol=None):
        with self.lock:
            if symbol is None:
                self._px.clear()
                self._kl.clear()
                self._fund.clear()
            else:
                self._px.pop(symbol, None)
                self._fund.pop(symbol, None)
                for key in [k for k in self._kl if k[0] == symbol]:
                    self._kl.pop(key, None)

    def stats(self):
        with self.lock:
            total = self.hits + self.misses
            return {"hits": self.hits, "misses": self.misses,
                    "hit_rate": (self.hits / total * 100) if total else 0.0,
                    "prices": len(self._px), "kline_sets": len(self._kl)}


# ---------------------------------------------------------------- telegram

TELEGRAM_HTTP_HINTS = {
    401: ("the token is shaped correctly but Telegram does not accept it - it "
          "was revoked or regenerated. Get a new one from @BotFather"),
    404: ("the token is part of the URL path, so a 404 means the token is "
          "wrong, truncated or still a placeholder - not that the API moved"),
    400: ("Telegram refused the arguments; for sendMessage this is nearly "
          "always a chat id the bot may not write to - send /start to the bot"),
    403: ("the bot is blocked in that chat, or /start was never sent there"),
    409: ("another copy of the bot, or a webhook, is already consuming this "
          "bot's updates"),
    429: ("rate limited - too many messages too quickly"),
}


def telegram_error_hint(status):
    """One actionable sentence for a Telegram HTTP status, or '' if unknown."""
    try:
        status = int(status)
    except (TypeError, ValueError):
        return ""
    if status in TELEGRAM_HTTP_HINTS:
        return TELEGRAM_HTTP_HINTS[status]
    if 500 <= status < 600:
        return "Telegram's own servers are failing; this is not a config problem"
    return ""


def telegram_network_hint(exc):
    """Explain a transport failure reaching api.telegram.org."""
    name, text = type(exc).__name__.lower(), str(exc).lower()
    if "timeout" in name or "timed out" in text:
        return ("api.telegram.org did not answer in time - many ISPs and most "
                "corporate/campus networks filter Telegram. Try another "
                "network, a VPN, or set HTTPS_PROXY")
    if "ssl" in name or "certificate" in text:
        return "HTTPS is being intercepted (proxy or antivirus); TLS failed"
    if ("connection" in name or "resolve" in text or "getaddrinfo" in text
            or "unreachable" in text or "refused" in text):
        return ("api.telegram.org could not be reached at all - offline, or "
                "Telegram is DNS-blocked on this connection")
    return ""


def telegram_failure_hint(exc):
    """Hint for any Telegram failure: an HTTP refusal or a transport error."""
    if isinstance(exc, ApiError):
        return telegram_error_hint(exc.status)
    return telegram_network_hint(exc)


def _hint_suffix(text):
    return f" [{text}]" if text else ""


def parse_command(text):
    """'/Paper_On@mybot yes' -> ('/paper_on', ['yes']). None if not a command."""
    text = str(text or "").strip()
    if not text.startswith("/"):
        return None
    parts = text.split()
    return parts[0].split("@")[0].lower(), parts[1:]


class Notifier:
    """Telegram out (alerts) and in (commands).

    Only `telegram_chat_id` may command the bot; a message from any other chat
    is dropped without a reply, so an unauthorised user learns nothing - not
    even that the bot is running. Delivery problems are reported on the console
    instead of being swallowed: a wrong token or chat id used to produce no
    error at all, because requests.post() does not raise on a 4xx, so the bot
    looked healthy while every alert was being discarded.

    `commands()` returns dicts, each carrying the Telegram update_id, so the
    caller can refuse to act on the same command twice even across a restart.
    """

    def __init__(self, cfg, perf=None):
        self.token = str(cfg["telegram_token"] or "").strip()
        self.chat = str(cfg["telegram_chat_id"] or "").strip()
        self.offset = None
        self.perf = perf or PERF
        self.timeout = float(cfg.get("telegram_timeout_sec", 8.0))
        # Serialises getUpdates (which advances a shared offset) and sends.
        # Two callers polling at once would race the offset and could drop or
        # replay a command, so there is exactly one critical section for it.
        self.io_lock = threading.RLock()
        self.warned = set()
        self.sent_ok = 0
        self.sent_failed = 0
        self.poll_ok = 0
        self.poll_failed = 0
        self.refused = 0
        self.last_error = ""
        self.last_ok_ms = None
        if self.token and ":" not in self.token:
            # e.g. the "PASTE_FROM_BOTFATHER" placeholder. Disable rather than
            # fire requests at a URL that cannot work.
            self._warn("token", "telegram_token is not a BotFather token (it should look "
                                "like 123456789:AA...). Telegram is disabled; alerts will "
                                "only appear on this console. Fix it with "
                                "`python telegram_setup.py` - sending it as-is is what "
                                "makes getMe/sendMessage answer HTTP 404, because the "
                                "token is part of the request URL")
            self.token = ""
        if self.token and not self.chat.lstrip("-").isdigit():
            self._warn("chat", "telegram_chat_id is not a numeric id, so alerts cannot be "
                               "delivered and every command will be refused. Run "
                               "`python telegram_setup.py` to capture the right id")
        if self.token:
            try:  # skip commands sent while the bot was off
                ups = self._get_updates()
                if ups:
                    self.offset = ups[-1]["update_id"] + 1
            except Exception as e:
                self._warn("startup", f"could not reach Telegram at startup: "
                                      f"{type(e).__name__}: {scrub(e, self.token)}"
                                      + _hint_suffix(telegram_failure_hint(e))
                                      + ". Run `python telegram_setup.py --check` "
                                        "to diagnose it")

    @property
    def enabled(self):
        return bool(self.token and self.chat)

    def _warn(self, key, msg):
        """Report a problem once per kind, so a persistent fault cannot spam the log."""
        self.last_error = str(msg)[:200]
        if key in self.warned or len(self.warned) > 64:
            return
        self.warned.add(key)
        say(f"(warning: {msg} - reported once)")

    def _recovered(self):
        """Clear the once-per-fault warnings after a success.

        Without this a fault that healed and then came back would never be
        reported again, which is exactly when you most want to hear about it.
        """
        self.last_ok_ms = now_ms()
        self.warned.discard("network")
        self.warned.discard("poll")
        self.warned.discard("startup")
        for key in [k for k in self.warned if k.startswith("http")]:
            self.warned.discard(key)

    def send(self, text):
        say(f"[{ist().strftime('%d %b %H:%M')}] {text}\n")
        if not self.token:
            return
        try:
            with self.perf.timer("telegram send"):
                r = requests.post(
                    f"https://api.telegram.org/bot{self.token}/sendMessage",
                    json={"chat_id": self.chat, "text": text}, timeout=self.timeout)
        except Exception as e:
            self.sent_failed += 1
            self.perf.bump("telegram_errors")
            self._warn("network", f"telegram send failed: {type(e).__name__}: "
                                  f"{scrub(e, self.token)}"
                                  + _hint_suffix(telegram_failure_hint(e)))
            return
        if getattr(r, "status_code", 200) >= 400:
            self.sent_failed += 1
            self.perf.bump("telegram_errors")
            detail = ""
            try:
                detail = str(r.json().get("description", ""))[:160]
            except Exception:
                detail = str(getattr(r, "text", ""))[:160]
            self._warn(f"http{r.status_code}",
                       f"Telegram rejected the message (HTTP {r.status_code}"
                       + (f": {scrub(detail, self.token)}" if detail else "")
                       + "). Alerts are NOT being delivered"
                       + _hint_suffix(telegram_error_hint(r.status_code))
                       + ". Run `python telegram_setup.py --check` to fix it")
            return
        self.sent_ok += 1
        self._recovered()

    def _get_updates(self):
        p = {"timeout": 0}
        if self.offset is not None:
            p["offset"] = self.offset
        with self.perf.timer("telegram getUpdates"):
            r = requests.get(f"https://api.telegram.org/bot{self.token}/getUpdates",
                             params=p, timeout=self.timeout)
        if getattr(r, "status_code", 200) >= 400:
            raise ApiError(r.status_code, {"msg": "telegram getUpdates refused"})
        body = r.json()
        return body.get("result", []) if isinstance(body, dict) else []

    def commands(self):
        """Authorised commands waiting for us, as dicts.

        Each is {"id": update_id, "text": raw, "cmd": "/status", "args": [...]}.
        Anything from another chat is dropped silently (counted, and reported on
        the console only).
        """
        if not self.token:
            return []
        # One poller at a time. getUpdates both reads and advances the shared
        # offset, so two concurrent callers could drop or replay a command.
        if not self.io_lock.acquire(blocking=False):
            return []
        try:
            ups = self._get_updates()
        except Exception as e:
            self.poll_failed += 1
            self.perf.bump("telegram_errors")
            self._warn("poll", f"telegram command polling failed: {type(e).__name__}: "
                               f"{scrub(e, self.token)}"
                               + _hint_suffix(telegram_failure_hint(e))
                               + ". Remote commands will not respond until this clears")
            return []
        finally:
            self.io_lock.release()
        self.poll_ok += 1
        self._recovered()
        out, refused = [], 0
        for u in ups:
            try:
                self.offset = u["update_id"] + 1
                m = u.get("message") or u.get("edited_message") or {}
                text = str(m.get("text") or "").strip()
                parsed = parse_command(text)
                if not parsed:
                    continue
                if not self.chat or str(m.get("chat", {}).get("id", "")) != self.chat:
                    refused += 1
                    continue
                # Telegram stamps every message with the time the user sent it,
                # which is what makes a true end-to-end latency measurable.
                try:
                    sent_ms = int(m.get("date")) * 1000
                except (TypeError, ValueError):
                    sent_ms = None
                out.append({"id": int(u["update_id"]), "text": text,
                            "cmd": parsed[0], "args": parsed[1],
                            "sent_ms": sent_ms, "received_ms": now_ms()})
            except Exception as e:
                self._warn("parse", f"could not read a Telegram update: {type(e).__name__}: {e}")
        if refused:
            self.refused += refused
            self._warn("unauth", f"refused {refused} command(s) from chat id(s) other than the "
                                 f"configured telegram_chat_id")
        return out

    def health_text(self):
        if not self.token:
            return "Telegram: disabled (no usable token) - console output only"
        age = "never" if not self.last_ok_ms else f"{(now_ms() - self.last_ok_ms) // 1000}s ago"
        lines = [f"Telegram: token configured, chat id "
                 f"{'configured' if self.chat else 'MISSING'}",
                 f"  sends ok/failed: {self.sent_ok}/{self.sent_failed} | "
                 f"polls ok/failed: {self.poll_ok}/{self.poll_failed}",
                 f"  last success: {age} | unauthorised commands refused: {self.refused}"]
        if self.last_error:
            lines.append(f"  last problem: {self.last_error[:120]}")
        return "\n".join(lines)


# ---------------------------------------------------------------- brokers

class PaperBroker:
    """Simulated fills at the live price, with taker fees and adverse slippage.
    Places no orders, ever.

    How the entry minute is handled, and why
    ----------------------------------------
    A 1-minute OHLC candle records the open, high, low and close of the whole
    minute but *not the order in which they happened*. If we entered at
    10:30:45, the low of the 10:30 candle may well have printed at 10:30:05 -
    forty seconds before we were in the market. OHLC data simply cannot tell
    the two apart, and no amount of arithmetic recovers the ordering.

    This simulator therefore evaluates only candles that are entirely after the
    entry: the first candle considered is the next minute boundary at or after
    `opened`. An entry landing exactly on a boundary makes that whole candle
    post-entry, so it is used.

    The cost of that choice is stated plainly: up to 59 seconds of genuine
    exposure immediately after entry is not simulated. A stop or target touched
    only inside that window and not afterwards is missed. The alternative -
    using the entry candle in full - manufactures losses that never occurred,
    which was the previous behaviour and is the worse error of the two: it
    produced phantom stop-outs on the wick that triggered the entry signal in
    the first place. With stops 0.4-3.0% wide, a move that reaches one and then
    fully retraces inside a single minute is far rarer than an entry wick.

    Slippage
    --------
    Every fill is moved against us by `slippage_pct` of the price: the entry,
    the stop and even the take profit, because the bot would use market orders
    for all three. A favourable gap through the target is never credited.
    """
    name = "PAPER"
    fractional_qty = True
    MAX_PAGES = 20  # 20 x 1500 one-minute candles ~ 20 days of downtime

    def __init__(self, client, cfg, state, perf=None):
        self.client, self.cfg, self.state = client, cfg, state
        self.perf = perf or PERF
        state.setdefault("paper_balance", cfg["paper_start_balance"])
        self.fractional_qty = bool(cfg.get("paper_fractional_qty", True))

    def balance(self):
        return self.state["paper_balance"]

    def foreign_positions(self, symbols):
        return []

    def open(self, symbol, side, qty, stop, price, rules, meta=None):
        """Simulate a market entry. Raises ValueError if the fill is unusable.

        A position is never created without a stop that is still on the right
        side of the fill. Adverse entry slippage always moves the fill *away*
        from the stop, so it cannot cause this by itself - but a bad signal, a
        stale stop level or a future caller could, and a position without a
        live stop is exactly what this build refuses to hold.
        """
        slip = self.cfg.get("slippage_pct", 0.0)
        fill = adverse_fill(side, price, slip, entering=True)
        if (side == "LONG" and stop >= fill) or (side == "SHORT" and stop <= fill):
            raise ValueError(f"simulated fill {fill:.6f} is already past the stop "
                             f"{stop:.6f} after slippage - entry refused")
        if qty <= 0:
            raise ValueError("refusing to open a position with no quantity")
        meta = dict(meta or {})
        pos = {"symbol": symbol, "side": side, "qty": qty, "entry": fill,
                "stop": stop, "initial_stop": stop,
                "target": target_for(side, fill, stop, self.cfg["reward_risk"]),
                "opened": now_ms(), "checked": now_ms(),
                "scanned_to": self.first_post_entry_minute(now_ms()),
                "trade_id": meta.get("trade_id") or new_trade_id(),
                "planned_risk": float(meta.get("planned_risk") or 0.0),
                "strategy": meta.get("strategy") or STRATEGY_NAME,
                "market_condition": meta.get("market_condition") or "",
                "requested_price": price,
                "entry_slippage": abs(fill - price) * qty,
                "equity_before": float(meta.get("equity_before") or self.balance())}
        return pos

    def _close(self, pos, raw_exit, reason):
        """Book a simulated exit at `raw_exit`, worsened by slippage."""
        slip = self.cfg.get("slippage_pct", 0.0)
        exit_px = adverse_fill(pos["side"], raw_exit, slip, entering=False)
        pnl, fees = trade_pnl(pos["side"], pos["entry"], exit_px, pos["qty"],
                              self.cfg["taker_fee_pct"])
        self.state["paper_balance"] += pnl
        slip_cost = float(pos.get("entry_slippage") or 0.0) + abs(exit_px - raw_exit) * pos["qty"]
        return {"exit": exit_px, "raw_exit": raw_exit, "pnl": pnl, "fees": fees,
                "slippage": slip_cost, "reason": reason}

    @staticmethod
    def first_post_entry_minute(opened_ms):
        """Open time of the first 1m candle that lies wholly after the entry."""
        floor = opened_ms - opened_ms % 60000
        return floor if floor == opened_ms else floor + 60000

    @staticmethod
    def scan_from(pos):
        """The first 1m candle this check needs to look at.

        The scan resumes from `scanned_to` - the minute after the last candle
        that was *definitely closed* when we last looked - instead of starting
        again at the entry. Without this the cost of a check grew with the age
        of the position: a two-day-old trade re-downloaded ~2,900 one-minute
        candles (two paged requests) every single cycle, which is what made the
        whole loop, and therefore Telegram, slow.

        A position written before this field existed falls back to the entry,
        so the first check after an upgrade does one full pass and every later
        one is cheap.
        """
        floor = PaperBroker.first_post_entry_minute(pos["opened"])
        return max(floor, int(pos.get("scanned_to") or 0))

    def _candles_since(self, symbol, start_ms):
        """Every 1m candle from start_ms to now, paging past the 1500 limit.

        klines() without a startTime returns only the *most recent* `limit`
        candles, so after a long outage the window used to skip the gap entirely
        and a stop hit inside it was never noticed. Paging from the entry
        forwards closes that hole.
        """
        out, cur, end = [], start_ms, now_ms()
        for page in range(self.MAX_PAGES):
            if cur > end:
                break
            batch = [c for c in self.client.klines(symbol, "1m", 1500, cur, None)
                     if c["t"] >= cur]
            if not batch:
                break
            out.extend(batch)
            if len(batch) < 1500:
                break
            cur = batch[-1]["t"] + 60000
            if page + 1 < self.MAX_PAGES:
                time.sleep(0.25)  # stay well inside the public rate limit
        else:
            say(f"(warning: {symbol} has more than {self.MAX_PAGES * 1500} minutes of "
                f"unchecked history; only the first part was examined)")
        return out

    def evaluate(self, pos, rules=None):
        """Decide what this position's candles imply - without changing anything.

        This is the slow half of a check: it talks to the exchange. It is kept
        free of side effects so the engine can run it *outside* the state lock
        and then apply the outcome in a short, local critical section. Returns
        a plan dict:

            {"exit": raw_exit, "reason": ..., "scanned_to": ms}  -> close it
            {"scanned_to": ms}                                    -> still open
        """
        start = self.scan_from(pos)
        scanned_to = start
        with (self.perf.timer("broker evaluate") if self.perf else _Timer(None, "")):
            candles = self._candles_since(pos["symbol"], start)
        settled = now_ms()
        for c in candles:
            if c["t"] < start:
                continue
            if pos["side"] == "LONG":
                hit_stop, hit_tgt = c["l"] <= pos["stop"], c["h"] >= pos["target"]
            else:
                hit_stop, hit_tgt = c["h"] >= pos["stop"], c["l"] <= pos["target"]
            if hit_stop:  # if both in one candle, assume the worse outcome
                return {"exit": stop_fill_price(pos["side"], pos["stop"], c["o"]),
                        "reason": "stop loss", "scanned_to": scanned_to}
            if hit_tgt:
                return {"exit": pos["target"], "reason": "take profit",
                        "scanned_to": scanned_to}
            # Only a candle that had certainly closed may be skipped next time;
            # the one still forming has to be looked at again.
            if c["T"] < settled:
                scanned_to = c["t"] + 60000
        return {"scanned_to": scanned_to}

    def book(self, pos, raw_exit, reason):
        """Apply an exit decided by evaluate(). Local only - no network."""
        return self._close(pos, raw_exit, reason)

    def check(self, pos, rules=None):
        """evaluate() + book(), for callers that do not need the two apart."""
        plan = self.evaluate(pos, rules)
        pos["scanned_to"] = plan["scanned_to"]
        pos["checked"] = now_ms()
        if "exit" not in plan:
            return None
        return self.book(pos, plan["exit"], plan["reason"])

    def exit_price(self, pos):
        """The current market price for a manual/emergency close (network)."""
        return self.client.price(pos["symbol"])

    def close_now(self, pos, reason="closed manually", price=None):
        """Close at `price`, or fetch one. Pass a price to avoid network I/O
        while the engine holds its state lock."""
        px = self.exit_price(pos) if price is None else float(price)
        return self._close(pos, px, reason)

    def mark_to_market(self, pos, price):
        """What closing right now at `price` would realise, fees and slippage in."""
        exit_px = adverse_fill(pos["side"], price, self.cfg.get("slippage_pct", 0.0),
                               entering=False)
        return trade_pnl(pos["side"], pos["entry"], exit_px, pos["qty"],
                         self.cfg["taker_fee_pct"])[0]


class LiveBroker:
    """Real orders. Every position gets an exchange-side stop; if the stop can't be placed, the
    position is closed immediately.

    NOT USED by this build. It is left intact and unmodified so that the
    paper-mode isolation tests keep something real to assert against, but
    nothing in paper, backtest or research mode can construct it, and no part of
    this upgrade touched its order logic or the real-money consent flags.
    """

    fractional_qty = False      # a real exchange minimum is a real constraint

    def __init__(self, client, cfg, state, notifier):
        self.client, self.cfg, self.state, self.n = client, cfg, state, notifier
        self.name = "TESTNET" if cfg["use_testnet"] else "REAL MONEY"
        if client.hedge_mode():
            raise SystemExit("Your futures account is in Hedge Mode. Switch to One-way Mode "
                             "(Futures > Preferences > Position Mode) and restart.")

    def balance(self):
        return self.client.balance()

    def foreign_positions(self, symbols):
        mine = (self.state.get("position") or {}).get("symbol")
        return [s for s in symbols if s != mine and abs(self.client.position_amt(s)) > 0]

    def _place_stop(self, pos, rules):
        side = "SELL" if pos["side"] == "LONG" else "BUY"
        rnd = floor_step if pos["side"] == "LONG" else ceil_step
        self.client.close_all_order(pos["symbol"], side, "STOP_MARKET",
                                    fmt_step(rnd(pos["stop"], rules["tick"]), rules["tick"]))

    def open(self, symbol, side, qty, stop, price, rules, meta=None):
        c = self.client
        meta = dict(meta or {})
        c.ensure_isolated(symbol)
        bal = c.balance()
        lev = max(1, min(self.cfg["max_leverage"], math.ceil(qty * price / (bal * 0.9))))
        c.set_leverage(symbol, lev)
        o = c.market(symbol, "BUY" if side == "LONG" else "SELL", fmt_step(qty, rules["step"]))
        fill = float(o.get("avgPrice") or 0) or price
        filled = float(o.get("executedQty") or 0) or qty
        pos = {"symbol": symbol, "side": side, "qty": filled, "entry": fill, "stop": stop,
               "initial_stop": stop,
               "target": target_for(side, fill, stop, self.cfg["reward_risk"]),
               "opened": now_ms(), "checked": now_ms(), "balance_before": bal, "leverage": lev,
               "step": rules["step"],
               "trade_id": meta.get("trade_id") or new_trade_id(),
               "planned_risk": float(meta.get("planned_risk") or 0.0),
               "strategy": meta.get("strategy") or STRATEGY_NAME,
               "market_condition": meta.get("market_condition") or "",
               "equity_before": bal}
        if check_stop(side, fill, stop, {**self.cfg, "min_stop_pct": 0, "max_stop_pct": 100}):
            self.close_now(pos)
            raise RuntimeError("Fill slipped past the stop - position closed straight away.")
        try:
            self._place_stop(pos, rules)
        except Exception as e:
            self.close_now(pos)
            raise RuntimeError(f"Could not place stop loss ({e}) - position closed for safety.")
        try:
            tside = "SELL" if side == "LONG" else "BUY"
            rnd = ceil_step if side == "LONG" else floor_step
            c.close_all_order(symbol, tside, "TAKE_PROFIT_MARKET",
                              fmt_step(rnd(pos["target"], rules["tick"]), rules["tick"]))
        except Exception as e:
            self.n.send(f"⚠️ Take-profit order failed ({e}). Stop loss is in place; "
                        f"I'll keep watching.")
        return pos

    def check(self, pos, rules=None):
        c = self.client
        if abs(c.position_amt(pos["symbol"])) == 0:
            try:
                c.cancel_algo_orders(pos["symbol"])
            except Exception:
                pass
            bal = c.balance()
            pnl = bal - pos["balance_before"]
            return {"exit": c.price(pos["symbol"]), "pnl": pnl, "fees": None,
                    "slippage": None,
                    "reason": "take profit" if pnl > 0 else "stop loss"}
        # still open: make sure the stop is still there
        algos = c.open_algo_orders(pos["symbol"])
        algos = algos if isinstance(algos, list) else algos.get("orders", [])
        if rules and not any(a.get("orderType") == "STOP_MARKET" for a in algos):
            try:
                self._place_stop(pos, rules)
                self.n.send(f"⚠️ Stop loss for {pos['symbol']} was missing - placed it again.")
            except Exception as e:
                self.n.send(f"🚨 Stop missing and could not be re-placed ({e}). Closing position.")
                return self.close_now(pos)
        pos["checked"] = now_ms()
        return None

    def close_now(self, pos, reason="closed manually", price=None):
        # `price` is accepted for signature compatibility with PaperBroker and
        # deliberately ignored: a real close is priced by the exchange, not by
        # the caller. The order logic below is unchanged.
        c = self.client
        try:
            c.cancel_algo_orders(pos["symbol"])
        except Exception:
            pass
        amt = c.position_amt(pos["symbol"])
        if amt != 0:
            c.market(pos["symbol"], "SELL" if amt > 0 else "BUY",
                     fmt_step(abs(amt), pos.get("step", 0.001)), reduce_only=True)
        bal = c.balance()
        return {"exit": c.price(pos["symbol"]), "pnl": bal - pos.get("balance_before", bal),
                "fees": None, "slippage": None, "reason": reason}

    def mark_to_market(self, pos, price):
        return trade_pnl(pos["side"], pos["entry"], price, pos["qty"],
                         self.cfg["taker_fee_pct"])[0]


# ---------------------------------------------------------------- single-instance lock

def lock_path():
    return os.path.join(HERE, "engine.lock")


def read_engine_lock(path=None):
    path = lock_path() if path is None else path
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def touch_engine_lock(path=None, started=None):
    """Write/refresh the heartbeat so another process can tell we are alive."""
    path = lock_path() if path is None else path
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"pid": os.getpid(), "started": started or time.time(),
                       "heartbeat": time.time(), "host": os.environ.get("COMPUTERNAME", "")}, f)
        os.replace(tmp, path)
    except OSError:
        pass  # a missing heartbeat must never stop the engine from trading safely


def acquire_engine_lock(path=None, stale_sec=180):
    """Refuse to start a second engine while one is still beating.

    Two engines polling the same Telegram bot would fight over getUpdates (and
    Telegram answers 409), each would manage the same state file, and a command
    could be executed twice. A heartbeat file is used rather than a pid check
    because os.kill(pid, 0) on Windows *terminates* the process.
    """
    path = lock_path() if path is None else path
    old = read_engine_lock(path)
    if old and old.get("pid") != os.getpid():
        try:
            age = time.time() - float(old.get("heartbeat") or 0)
        except (TypeError, ValueError):
            age = 1e9
        if age < stale_sec:
            raise SystemExit(
                f"Another engine looks like it is already running (pid "
                f"{old.get('pid')}, heartbeat {age:.0f}s ago, lock {path}).\n"
                f"Two engines would both poll Telegram and both manage the same\n"
                f"paper position. Stop the other one, or delete the lock file if\n"
                f"you are certain it is dead.")
        say(f"(notice: taking over a stale engine lock from pid {old.get('pid')}, "
            f"last beat {age:.0f}s ago)")
    touch_engine_lock(path)
    return path


def release_engine_lock(path=None):
    path = lock_path() if path is None else path
    data = read_engine_lock(path)
    if data and data.get("pid") == os.getpid():
        try:
            os.unlink(path)
        except OSError:
            pass


# ---------------------------------------------------------------- state

def state_path(mode, testnet):
    name = "paper" if mode in ("paper", "backtest", "research") else (
        "testnet" if testnet else "real")
    return os.path.join(HERE, f"bot_state_{name}.json")


def load_state(path):
    """Read the state file, surviving a corrupt one instead of crashing on it."""
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("state file does not contain a JSON object")
        return data
    except Exception as e:
        moved = f"{path}.broken-{int(time.time())}"
        try:
            os.replace(path, moved)
        except OSError:
            moved = "(could not be renamed)"
        say(f"WARNING: {path} could not be read ({e}).\n"
            f"         Moved to {moved}; starting from a clean state. If a position was\n"
            f"         open, reconcile it against trades.csv before trading further.")
        return {}


_SAVE_LOCK = threading.Lock()


def save_state(state, path):
    """Atomically write the state file.

    The write goes to a temporary file and is then renamed over the real one,
    so a crash mid-write cannot leave a truncated state behind. The lock is
    what makes that safe when two threads save at once: on Windows, one thread
    renaming `.tmp` while another still has it open raises WinError 32, which
    is exactly what happens when a Telegram command and a trading cycle both
    decide to save.
    """
    with _SAVE_LOCK:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
            f.flush()
            os.fsync(f.fileno())  # survive a power cut, not just a crash
        os.replace(tmp, path)


# ---------------------------------------------------------------- bot loop

HELP_TEXT = """🤖 Paper-trading bot - commands
/help - this list
/status - engine state, mode, market, uptime, risk
/paper_on - allow new simulated entries
/paper_off - no new entries; an open position is still managed
/positions - the open position in detail
/performance - statistics from journalled closed trades
/balance - virtual balance, equity, available, drawdown
/history - the last completed trades
/risk - all risk limits and what is left today
/halt - EMERGENCY: close the simulated position and block entries
/clearhalt CONFIRM - lift a halt after a manual review
/resetpaper - reset the virtual account (asks to confirm; the journal is kept)
/health - market data, Telegram, errors, engine health

Aliases: /pause = /paper_off, /resume = /paper_on, /close = close the position now.
This is a PAPER simulator. It never places a real order."""


class Bot:
    def __init__(self, cfg, client, broker, notifier, state, path, lock=None):
        self.cfg, self.c, self.b, self.n, self.s, self.path = cfg, client, broker, notifier, state, path
        self.lock = lock
        try:
            self.rules = client.rules(cfg["symbols"])
        except SystemExit:
            raise
        except Exception as e:
            raise SystemExit(f"Could not load the exchange rules for "
                             f"{', '.join(cfg['symbols'])} from {client.base}:\n"
                             f"  {type(e).__name__}: {scrub(e, cfg['api_secret'])}")
        self._secrets = (cfg["api_key"], cfg["api_secret"], cfg["telegram_token"])
        self.started = time.time()
        self.loops = 0
        self.error_count = 0
        self.last_error = ""
        self.data_ok_ms = None
        self.data_fail = 0
        self.last_data_error = ""
        self.perf = Perf(perf_thresholds(cfg))
        # One Perf for the whole engine, so /health reports a single picture.
        for obj in (client, getattr(client, "client", None), broker, notifier):
            if obj is not None and hasattr(obj, "perf"):
                obj.perf = self.perf
        # Guards every mutation of `state`. It is held only for short, local
        # sections - never across a network call - so a command thread never
        # waits on a socket to read or change the engine's state.
        self.state_lock = threading.RLock()
        self._stop = threading.Event()
        self._worker = None          # the Telegram command thread, once running
        self.last_cycle_ms = 0.0
        self.cmd_count = 0
        # The paper broker can split a position check into a slow, side-effect
        # free part and a fast booking part. A broker without evaluate() (the
        # untouched LiveBroker) keeps the original single-call path.
        self.phased = hasattr(self.b, "evaluate") and hasattr(self.b, "book")
        s = state
        s.setdefault("position", None)
        s.setdefault("paused", False)
        s.setdefault("last_candle", {})
        s.setdefault("last_status", 0)
        s.setdefault("block_note", "")
        s.setdefault("seen_updates", [])
        s.setdefault("pending_reset", None)
        # Paper trading is OFF after a fresh install unless the config says
        # otherwise. The flag then lives in the state file, so /paper_on and
        # /paper_off survive a restart.
        s.setdefault("paper_enabled", bool(cfg.get("paper_trading_enabled", False)))
        self._migrate_position()
        self.risk = RiskManager(cfg, s)
        # A state file missing any period field (hand-edited, or written by an
        # older build) would otherwise leave the day's limits undefined.
        start = self._equity_safe()
        self.risk.roll(start, force=not s.get("day") or not s.get("month"))

    # ---- money -----------------------------------------------------------

    def inr(self, usdt):
        return f"₹{usdt * self.cfg['inr_per_usdt']:,.0f}"

    def _migrate_position(self):
        """Make a position written by an older build safe to manage and journal.

        The fields that build never recorded are filled in where they can be
        known (a trade id, the initial stop) and left alone where they cannot:
        the planned risk of a trade taken under the old 1.5% rules is not
        recoverable, and inventing one would corrupt the journal. It is marked
        as unrecorded so every report says so.
        """
        p = self.s.get("position")
        if not p:
            return
        if not p.get("trade_id"):
            p["trade_id"] = new_trade_id()
            p["legacy"] = True
        p.setdefault("initial_stop", p.get("stop"))
        p.setdefault("strategy", STRATEGY_NAME)
        p.setdefault("market_condition", "")
        p.setdefault("entry_slippage", 0.0)
        p.setdefault("planned_risk", 0.0)

    def _roll_periods(self, force=False):
        """Roll the day/month counters. Thin wrapper over RiskManager.roll()."""
        self.risk.roll(self._equity_safe(), force=force)

    def price(self, symbol):
        """A ticker price, with the data-health bookkeeping done for us."""
        try:
            px = float(self.c.price(symbol))
        except Exception as e:
            self.data_fail += 1
            self.last_data_error = f"{type(e).__name__}: {scrub(e, *self._secrets)}"[:160]
            raise
        if px <= 0:
            self.data_fail += 1
            self.last_data_error = f"{symbol} price came back as {px}"
            raise ValueError(self.last_data_error)
        self.data_ok_ms = now_ms()
        return px

    def entry_price(self, symbol):
        """The price a new position may be opened at: always freshly fetched.

        A cached quote is fine for telling you what your account is worth; it
        is not fine for deciding to take on risk, so this bypasses the cache
        entirely.
        """
        fresh = getattr(self.c, "fresh_price", None)
        if fresh is None:
            return self.price(symbol)
        try:
            px = float(fresh(symbol))
        except Exception as e:
            self.data_fail += 1
            self.last_data_error = f"{type(e).__name__}: {scrub(e, *self._secrets)}"[:160]
            raise
        if px <= 0:
            self.data_fail += 1
            self.last_data_error = f"{symbol} price came back as {px}"
            raise ValueError(self.last_data_error)
        self.data_ok_ms = now_ms()
        return px

    # ---- cached reads, for reports that must answer instantly -------------

    def cached_price(self, symbol, max_age_sec=None):
        """(price, age_ms) from the cache only - never a network call.

        Informational commands use this so that a hung or slow exchange can
        delay a *report* by nothing at all. The age comes back with the number
        so the reply can say how old it is instead of implying it is live. A
        plain (uncached) client falls back to a direct read, which is what the
        offline tests use.
        """
        if max_age_sec is None:
            max_age_sec = self.cfg.get("display_cache_sec", 150)
        getter = getattr(self.c, "price_cached", None)
        if getter is None:
            try:
                return self.price(symbol), 0
            except Exception:
                return None, None
        return getter(symbol, max_age_sec)

    def cached_bias(self, symbol):
        """The 4h trend from cached candles only, or None if none are cached."""
        getter = getattr(self.c, "klines_cached", None)
        if getter is None:
            try:
                return self._bias(symbol)
            except Exception:
                return None
        candles, _age = getter(symbol, self.cfg["trend_interval"],
                               allow_overdue=True)
        if not candles:
            return None
        closes = [x["c"] for x in candles]
        return trend_bias_at(closes, ema(closes, self.cfg["trend_ema"]),
                             len(closes) - 1)

    @staticmethod
    def _age_text(age_ms):
        if age_ms is None:
            return "no recent quote"
        secs = age_ms / 1000.0
        if secs < 2:
            return "live"
        if secs < 90:
            return f"{secs:.0f}s ago"
        return f"{secs / 60:.0f}m ago"

    def unrealized(self):
        """(pnl, error). The fee- and slippage-inclusive value of closing now."""
        p = self.s.get("position")
        if not p:
            return 0.0, None
        try:
            px = self.price(p["symbol"])
        except Exception as e:
            return 0.0, f"{type(e).__name__}: {scrub(e, *self._secrets)}"
        return self.b.mark_to_market(p, px), None

    def equity(self):
        """(equity, error). Equity is balance + the open position marked to market.

        An error means the account cannot currently be valued, which is treated
        as a reason not to trade rather than as 'no unrealised loss'.
        """
        bal = self.b.balance()
        up, err = self.unrealized()
        return bal + up, err

    def equity_cached(self):
        """(equity, age_ms, error) using cached prices only - no network.

        Reports use this. It can say 'the last quote was 40 seconds old', which
        is honest and instant; what it can never do is make a Telegram reply
        wait on a socket.
        """
        bal = self.b.balance()
        p = self.s.get("position")
        if not p:
            return bal, 0, None
        px, age = self.cached_price(p["symbol"])
        if px is None:
            return bal, None, "no recent price for the open position"
        return bal + self.b.mark_to_market(p, px), age, None

    def _equity_safe(self):
        """Equity if it can be had, otherwise the bare balance. Startup only."""
        try:
            eq, err = self.equity()
            return self.b.balance() if err else eq
        except Exception:
            try:
                return self.b.balance()
            except Exception:
                return float(self.cfg["paper_start_balance"])

    def available(self, equity):
        """Virtual balance not reserved as margin for the open position."""
        p = self.s.get("position")
        if not p:
            return self.b.balance()
        notional = p["qty"] * p["entry"]
        return self.b.balance() - notional / max(1, self.cfg["max_leverage"])

    # ---- gates -----------------------------------------------------------

    def cached_candles(self, sym, interval=None):
        """(candles, age_ms) for the entry interval, from cache only."""
        interval = interval or self.cfg["entry_interval"]
        getter = getattr(self.c, "klines_cached", None)
        if getter is None:
            try:
                return self.c.closed_klines(sym, interval, 300), 0
            except Exception:
                return None, None
        return getter(sym, interval, allow_overdue=True)

    def candle_problem(self, sym, c1, price=None):
        """The pure half of data_problem: validate candles already in hand.

        Separated out so a health report can judge cached candles without
        making a request, and so the entry path and the report cannot drift
        apart - both call this same function.
        """
        return self._validate_candles(sym, c1, price)

    def data_problem(self, sym, c1=None, price=None):
        """Why the market data for `sym` must not be traded on, or None.

        Fetches the candles if none were handed in. The freshness verdict is
        based on the candles' own close times, never on when they were fetched,
        so caching cannot hide a stale feed.
        """
        try:
            c1 = self.c.closed_klines(sym, self.cfg["entry_interval"], 300) if c1 is None else c1
        except Exception as e:
            self.data_fail += 1
            self.last_data_error = f"{type(e).__name__}: {scrub(e, *self._secrets)}"[:160]
            return f"candles unavailable ({type(e).__name__})"
        return self._validate_candles(sym, c1, price)

    def _validate_candles(self, sym, c1, price=None):
        if not c1:
            return "no candle data"
        need = max(self.cfg["slow_ema"], self.cfg["swing_lookback"]) + 2
        if len(c1) < need:
            return f"only {len(c1)} candles, need {need}"
        last = c1[-1]
        if any(last.get(k) is None for k in ("o", "h", "l", "c")) or last["h"] < last["l"]:
            return "last candle is malformed"
        age_ms = now_ms() - last["T"]
        limit = 2 * interval_ms(self.cfg["entry_interval"])
        if age_ms > limit:
            return (f"last closed {self.cfg['entry_interval']} candle is "
                    f"{age_ms / 60000:.0f} min old (limit {limit / 60000:.0f} min) - "
                    f"stale data")
        self.data_ok_ms = now_ms()
        if price is not None:
            drift = abs(price / last["c"] - 1) * 100
            if drift > self.cfg["max_price_disagreement_pct"]:
                return (f"ticker {price:.4f} disagrees with the last close "
                        f"{last['c']:.4f} by {drift:.1f}%")
        return None

    def entry_blocks(self, cached=False, equity=None):
        """Every reason a new entry must not be taken right now.

        `cached=True` values the account from cached prices instead of fetching,
        for a report that must answer instantly. The *trading* path always
        calls it with freshly valued equity, so no gate is ever decided on a
        stale number.
        """
        s, cfg = self.s, self.cfg
        out = []
        if not s.get("paper_enabled"):
            out.append("paper trading is OFF (/paper_on to enable)")
        if s.get("paused"):
            out.append("paused (/resume or /paper_on to restart)")
        if s.get("position"):
            out.append(f"already in a {s['position']['side']} "
                       f"{s['position']['symbol']} position "
                       f"(max {cfg['max_open_positions']})")
        if equity is not None:
            eq, err = equity, None
        elif cached:
            eq, _age, err = self.equity_cached()
        else:
            eq, err = self.equity()
        if err:
            out.append(f"account cannot be valued right now: {err[:80]}")
            return out
        out.extend(self.risk.entry_blocks(eq))
        foreign = self.b.foreign_positions(cfg["symbols"])
        if foreign:
            out.append(f"other open positions ({', '.join(foreign)}) - close them first")
        return out

    def blocked(self):
        """The first reason entries are blocked, or None. Kept for compatibility."""
        blocks = self.entry_blocks()
        return blocks[0] if blocks else None

    # ---- reporting -------------------------------------------------------

    def uptime_text(self):
        secs = int(time.time() - self.started)
        d, rem = divmod(secs, 86400)
        h, rem = divmod(rem, 3600)
        m, s = divmod(rem, 60)
        return (f"{d}d {h}h {m}m" if d else (f"{h}h {m}m" if h else f"{m}m {s}s"))

    def status_text(self):
        """A full status report built only from state and cached market data.

        Nothing here fetches: the trading loop refreshes the cache every cycle,
        and this prints how old what it found is. That is what makes /status
        answer in milliseconds instead of waiting on seven HTTP round trips, and
        it is also what lets it answer at all while the exchange is unreachable.
        """
        s = self.s
        bal = self.b.balance()
        eq, age, err = self.equity_cached()
        lines = [f"📊 Status ({self.b.name} - simulated money)",
                 f"Entries: {'ON' if s.get('paper_enabled') else 'OFF'}"
                 + (" | PAUSED" if s.get("paused") else "")
                 + (f" | 🚨 HALTED" if self.risk.is_halted() else "")
                 + (" | ⛔ day locked" if self.risk.is_locked_today() else ""),
                 f"Balance {bal:.2f} USDT ({self.inr(bal)}) | equity "
                 + (f"unavailable ({err[:40]})" if err else f"{eq:.2f} USDT")
                 + (f" ({self._age_text(age)})" if age else ""),
                 f"Today: {self.risk.daily_pnl(eq):+.4f} USDT | drawdown "
                 f"{self.risk.drawdown_pct(eq):.2f}% of a "
                 f"{self.cfg['max_account_drawdown_pct']}% limit | trades "
                 f"{int(s.get('trades_today', 0))}/{self.cfg['max_trades_per_day']}",
                 f"Uptime {self.uptime_text()} | checks {self.loops} | "
                 f"errors {self.error_count} | last cycle "
                 f"{self.last_cycle_ms:.0f}ms"]
        for sym in self.cfg["symbols"]:
            px, px_age = self.cached_price(sym)
            bias = self.cached_bias(sym) or "n/a"
            if px is None:
                lines.append(f"{sym}: no recent price (4h trend: {bias})")
            else:
                lines.append(f"{sym}: {px:,.4f} ({self._age_text(px_age)}) | "
                             f"4h trend: {bias}")
        lines.append(self.position_text())
        blocks = self.entry_blocks(cached=True)
        if blocks:
            lines.append("⏸ Not taking entries:\n  - " + "\n  - ".join(blocks))
        return "\n".join(lines)

    def position_text(self):
        p = self.s.get("position")
        if not p:
            return "No open position."
        lines = [f"📌 {p['side']} {p['symbol']} (id {p.get('trade_id', '?')})",
                 f"Entry {p['entry']:,.4f} | qty {fmt_qty(p['qty'])} "
                 f"(≈{p['qty'] * p['entry']:.2f} USDT notional)",
                 f"Stop {p['stop']:,.4f} | target {p['target']:,.4f}",
                 (f"Planned risk {float(p['planned_risk']):.4f} USDT "
                  f"({self.inr(float(p['planned_risk']))})"
                  if float(p.get("planned_risk") or 0) > 0 else
                  "Planned risk: not recorded (position predates this build)"),
                 f"Opened {ist(p['opened']).strftime('%d %b %H:%M')} IST"]
        px, age = self.cached_price(p["symbol"])
        if px is None:
            lines.append("Current price: no recent quote (the engine will refresh "
                         "it on its next cycle)")
        else:
            up = self.b.mark_to_market(p, px)
            risk_left = abs(px - p["stop"]) * p["qty"]
            lines.append(f"Now {px:,.4f} ({self._age_text(age)}) | unrealised "
                         f"{up:+.4f} USDT after fees | still at risk "
                         f"≈{risk_left:.4f} USDT")
        if p.get("market_condition"):
            lines.append(f"Setup: {p['market_condition']}")
        return "\n".join(lines)

    def balance_text(self):
        bal = self.b.balance()
        eq, age, err = self.equity_cached()
        lines = [f"💰 Virtual account ({self.b.name})",
                 f"Balance: {bal:.4f} USDT ({self.inr(bal)})"]
        if err:
            lines.append(f"Equity: unavailable ({err[:80]})")
        else:
            lines.append(f"Equity (balance + open P&L): {eq:.4f} USDT"
                         + (f" (price {self._age_text(age)})" if age else ""))
            lines.append(f"Available: {self.available(eq):.4f} USDT")
            lines.append(f"Peak equity: {self.risk.peak(eq):.4f} USDT | "
                         f"drawdown {self.risk.drawdown_pct(eq):.2f}% "
                         f"(limit {self.cfg['max_account_drawdown_pct']}%)")
            lines.append(f"Day start: {self.risk.day_start_equity(eq):.4f} USDT | "
                         f"today {self.risk.daily_pnl(eq):+.4f} USDT")
        lines.append(f"Start balance was {self.cfg['paper_start_balance']:.2f} USDT. "
                     f"All of this is simulated.")
        return "\n".join(lines)

    def health_text(self):
        """Connectivity, error counts and timings. Built without any network I/O."""
        age = ("never" if not self.data_ok_ms
               else f"{(now_ms() - self.data_ok_ms) // 1000}s ago")
        lines = [f"🩺 Health ({self.b.name})",
                 f"Engine: uptime {self.uptime_text()}, {self.loops} cycles, "
                 f"{self.error_count} errors, {self.cmd_count} commands",
                 f"Last trading cycle: {self.last_cycle_ms:.0f}ms"
                 + ("  ⚠️ slow" if self.last_cycle_ms > self.cfg["slow_cycle_ms"]
                    else ""),
                 f"Market data ({self.c.base}): last good read {age}, "
                 f"{self.data_fail} failures"]
        if self.last_data_error:
            lines.append(f"  last data problem: {self.last_data_error[:120]}")
        for sym in self.cfg["symbols"]:
            px, px_age = self.cached_price(sym)
            cached, c_age = self.cached_candles(sym)
            # Candles are only read while looking for an entry, so "none fetched
            # yet" is the normal report while a position is open - not a fault.
            bad = (self.candle_problem(sym, cached) if cached
                   else "none fetched yet")
            lines.append(f"  {sym}: price {self._age_text(px_age)}, candles "
                         f"{'OK' if not bad else bad}")
        stats = getattr(self.c, "stats", None)
        if stats:
            st = stats()
            lines.append(f"Market-data cache: {st['hits']} hits / {st['misses']} "
                         f"fetches ({st['hit_rate']:.0f}% reused)")
        lines.append(self.n.health_text() if hasattr(self.n, "health_text")
                     else "Telegram: n/a")
        lines.extend(self.perf.report_lines())
        if self.last_error:
            lines.append(f"Last engine error: {self.last_error[:120]}")
        lines.append(f"Command worker: "
                     + ("running" if self._worker and self._worker.is_alive()
                        else "not running (commands polled by the trading loop)"))
        lines.append(f"State file: {os.path.basename(self.path)} "
                     f"({'writable' if self._state_writable() else 'NOT WRITABLE'})")
        lines.append(f"Journal: {os.path.basename(JOURNAL_FILE)} "
                     f"({len(load_journal(mode=self.b.name))} closed trades)")
        lines.append("Risk engine: " + ("HALTED" if self.risk.is_halted() else
                                        ("day locked" if self.risk.is_locked_today()
                                         else "armed")))
        return "\n".join(lines)

    def _state_writable(self):
        """Can the state file be written? Checked without writing it.

        /health used to answer this by actually saving the state, which both
        cost an fsync on every health check and raced the trading cycle for
        the same file.
        """
        try:
            if os.path.exists(self.path):
                return os.access(self.path, os.W_OK)
            return os.access(os.path.dirname(self.path) or ".", os.W_OK)
        except Exception:
            return False

    def _bias(self, sym):
        c4 = self.c.closed_klines(sym, self.cfg["trend_interval"], self.cfg["trend_ema"] + 100)
        closes = [x["c"] for x in c4]
        return trend_bias_at(closes, ema(closes, self.cfg["trend_ema"]), len(closes) - 1)

    # ---- commands --------------------------------------------------------

    def _seen(self, update_id):
        """True if this Telegram update was already acted on.

        The offset alone stops Telegram re-delivering an update inside one run;
        this list is persisted so a crash between acting on a command and
        advancing the offset cannot make the command run twice.
        """
        if update_id is None:
            return False
        seen = self.s.setdefault("seen_updates", [])
        if update_id in seen:
            return True
        seen.append(update_id)
        del seen[:-300]
        return False

    # Commands that only read: they take no state lock and make no network
    # call, so they answer in milliseconds even mid-trading-cycle or while the
    # exchange is unreachable.
    READ_ONLY = frozenset({"/help", "/start", "/status", "/positions",
                           "/performance", "/balance", "/history", "/risk",
                           "/health"})

    def handle_commands(self):
        """Poll once and run whatever came back. Safe to call from either thread."""
        for item in self.n.commands():
            if isinstance(item, str):           # tolerate a plain-string source
                parsed = parse_command(item)
                if not parsed:
                    continue
                item = {"id": None, "text": item, "cmd": parsed[0], "args": parsed[1]}
            self.run_command(item)

    def run_command(self, item):
        """Authorise-by-id, de-duplicate, execute and time one command."""
        cmd = (item.get("cmd") or "").lower()
        # De-duplication touches the state, so it is done under the lock. It is
        # a dict append: microseconds, never a network call.
        with self.state_lock:
            if self._seen(item.get("id")):
                return
        self.cmd_count += 1
        t0 = time.perf_counter()
        queued_ms = None
        if item.get("sent_ms"):
            queued_ms = max(0, now_ms() - item["sent_ms"])
        try:
            self.dispatch(cmd, item.get("args") or [])
        except Exception as e:
            self.n.send(f"⚠️ /{cmd.lstrip('/')} failed: {type(e).__name__}: "
                        f"{scrub(e, *self._secrets)}")
        finally:
            ms = (time.perf_counter() - t0) * 1000
            self.perf.record(f"cmd {cmd}", ms)
            if queued_ms is not None:
                self.perf.record("cmd end-to-end latency", queued_ms)
            if ms > self.cfg.get("slow_command_ms", 1000):
                say(f"(slow command: {cmd} took {ms:.0f}ms)")
            # Only a command that may have changed something needs a save.
            if cmd not in self.READ_ONLY:
                try:
                    with self.state_lock:
                        save_state(self.s, self.path)
                except Exception as e:
                    say(f"(warning: could not save state after a command: "
                        f"{type(e).__name__}: {e})")

    def dispatch(self, cmd, args):
        """Run one authorised command. Never bypasses a risk limit."""
        cmd = (cmd or "").lower()
        if cmd == "/help" or cmd == "/start":
            return self.n.send(HELP_TEXT)
        if cmd == "/status":
            return self.n.send(self.status_text())
        if cmd in ("/paper_on", "/resume", "/on"):
            with self.state_lock:
                if self.risk.is_halted():
                    return self.n.send(f"🚨 Refused: the engine is halted "
                                       f"({self.s.get('halt_reason')}).\n"
                                       f"A halt needs a manual review: "
                                       f"/clearhalt CONFIRM.")
                self.s["paper_enabled"], self.s["paused"] = True, False
                # cached: arming must answer at once, and the trading loop
                # re-checks every gate with fresh data before it ever enters.
                blocks = self.entry_blocks(cached=True)
            return self.n.send("▶️ Paper trading ON. New simulated entries allowed.\n"
                               + ("Still blocked by:\n  - " + "\n  - ".join(blocks)
                                  if blocks else "No blocks - watching for a setup."))
        if cmd in ("/paper_off", "/pause", "/off"):
            # Deliberately the shortest path in the file: setting the flag is
            # all it takes to stop new risk being taken, so it cannot be held
            # up by anything.
            with self.state_lock:
                self.s["paper_enabled"], self.s["paused"] = False, True
                p = self.s.get("position")
            return self.n.send("⏸ Paper trading OFF. No new entries.\n"
                               + (f"The open {p['side']} {p['symbol']} keeps its stop and "
                                  f"will still be managed to its exit."
                                  if p else "No position is open."))
        if cmd == "/positions":
            return self.n.send(self.position_text())
        if cmd == "/performance":
            rows = load_journal(mode=self.b.name)
            return self.n.send(format_performance(
                performance_stats(rows, start_equity=self.cfg["paper_start_balance"])))
        if cmd == "/balance":
            return self.n.send(self.balance_text())
        if cmd == "/history":
            return self.n.send(format_history(load_journal(mode=self.b.name)))
        if cmd == "/risk":
            # The limits themselves are config and state - no market data is
            # needed to report them. Equity only scales the dollar figures, and
            # a cached quote is explicitly labelled.
            eq, age, err = self.equity_cached()
            if err:
                eq = self.b.balance()
            return self.n.send(self.risk.limits_text(eq)
                               + (f"\n(equity from a quote {self._age_text(age)})"
                                  if age else "")
                               + ("\n(no recent price; limits shown against the "
                                  "balance)" if err else ""))
        if cmd == "/health":
            return self.n.send(self.health_text())
        if cmd == "/halt":
            return self.emergency("manual /halt from Telegram", kind="manual")
        if cmd == "/clearhalt":
            return self._clear_halt(args)
        if cmd in ("/close", "/flat"):
            return self._close_now_command()
        if cmd == "/resetpaper":
            return self._reset_paper(args)
        return self.n.send(f"Unknown command {cmd}.\n\n{HELP_TEXT}")

    def _exit_price_for(self, pos):
        """A price to close at, fetched *before* any lock is taken.

        Closing is a state change and has to happen under the lock, but asking
        the exchange for a price is a network call and must not. So the price
        is obtained first - from the cache if it is recent, otherwise fetched -
        and then handed to the booking step.
        """
        px, _age = self.cached_price(pos["symbol"], self.cfg.get("price_cache_sec", 5))
        if px is not None:
            return px
        return self.price(pos["symbol"])

    def _close_now_command(self):
        pos = self.s.get("position")
        if not pos:
            return self.n.send("No open position.")
        trade_id = pos.get("trade_id")
        try:
            px = self._exit_price_for(pos)
        except Exception as e:
            return self.n.send(f"Could not get a price to close at "
                               f"({type(e).__name__}). The position keeps its stop "
                               f"and is unchanged. Try /halt if you need it out.")
        with self.state_lock:
            cur = self.s.get("position")
            if not cur or cur.get("trade_id") != trade_id:
                return self.n.send("The position closed on its own just now.")
            self._finish(self.b.close_now(cur, price=px))

    def _clear_halt(self, args):
        if not self.risk.is_halted():
            return self.n.send("The engine is not halted.")
        if [a.upper() for a in args][:1] != ["CONFIRM"]:
            return self.n.send(
                f"🚨 The engine is halted: {self.s.get('halt_reason')}\n\n"
                f"Clearing a halt is a manual-review action. Check the journal "
                f"(/performance, /history) first, then send exactly:\n"
                f"/clearhalt CONFIRM\n\n"
                f"This re-baselines the equity peak to the current equity, so the "
                f"5% drawdown limit will be measured from here on.")
        eq, _age, err = self.equity_cached()
        if err:
            return self.n.send(f"Refusing to clear the halt while the account cannot "
                               f"be valued ({err[:80]}). Try again when market data "
                               f"is back.")
        old = self.s.get("halt_reason")
        peak = self.risk.clear_halt(eq)
        self.s["paper_enabled"] = False   # re-arming entries stays a separate decision
        self.s["paused"] = True
        save_state(self.s, self.path)
        return self.n.send(f"✅ Halt cleared after review (was: {old}).\n"
                           f"Equity peak re-baselined to {peak:.4f} USDT.\n"
                           f"Entries are still OFF - send /paper_on when you are ready.")

    def _reset_paper(self, args):
        """Reset the virtual account, only after an explicit confirmation.

        The trade journal is never touched: it is the permanent record of what
        happened and resetting a simulated balance must not erase it.
        """
        cfg, s = self.cfg, self.s
        if s.get("position"):
            return self.n.send("Refusing to reset while a position is open. "
                               "Close it first (/close) or /halt, then reset.")
        want = [a.upper() for a in args][:1]
        pending = s.get("pending_reset") or {}
        fresh = (pending.get("asked_ms") and
                 now_ms() - pending["asked_ms"] <= cfg["reset_confirm_minutes"] * 60000)
        if want != ["CONFIRM"]:
            s["pending_reset"] = {"asked_ms": now_ms()}
            return self.n.send(
                f"⚠️ /resetpaper will set the virtual balance back to "
                f"{cfg['paper_start_balance']:.2f} USDT and clear the day's counters, "
                f"the equity peak and any halt.\n"
                f"Your trade journal ({os.path.basename(JOURNAL_FILE)}) is NOT deleted - "
                f"{len(load_journal(mode=self.b.name))} closed trades stay on record, "
                f"and /performance will keep counting them.\n\n"
                f"Send exactly `/resetpaper CONFIRM` within "
                f"{cfg['reset_confirm_minutes']} minutes to go ahead.")
        if not fresh:
            s["pending_reset"] = {"asked_ms": now_ms()}
            return self.n.send("That confirmation has expired. Send /resetpaper again, "
                               "then /resetpaper CONFIRM.")
        start = float(cfg["paper_start_balance"])
        keep_candle = dict(s.get("last_candle") or {})
        s["pending_reset"] = None
        s["paper_balance"] = start
        s["position"] = None
        s["paper_enabled"] = False
        s["paused"] = True
        s["block_note"] = ""
        s["last_candle"] = keep_candle   # do not re-trade a candle we already saw
        s["equity_peak"] = start
        s["day"] = None
        s["month"] = None
        s["month_start"] = None
        s["day_start_equity"] = None
        s["halted"], s["halt_reason"], s["halted_at"] = False, "", None
        s["last_loss_ms"] = None
        s.setdefault("resets", []).append(ist().strftime("%Y-%m-%d %H:%M:%S"))
        self.risk.roll(start, force=True)
        save_state(s, self.path)
        return self.n.send(f"🔄 Virtual account reset to {start:.2f} USDT. Entries are "
                           f"OFF; send /paper_on when you want to trade again.\n"
                           f"The trade journal was left untouched.")

    # ---- position lifecycle ---------------------------------------------

    def emergency(self, reason, kind="manual", price=None):
        """Close the simulated position (if any) and stop taking entries.

        `kind` decides how hard the stop is:
          daily     - locked until the next IST day
          drawdown  - halted until a human clears it
          manual    - halted (a human asked for it, a human lifts it)
        """
        s = self.s
        # Disabling entries is the first thing that happens and needs nothing
        # from the network, so an emergency always stops new risk immediately
        # even if the close below cannot be priced.
        with self.state_lock:
            s["paper_enabled"], s["paused"] = False, True
        exit_note = "No position was open."
        p = s.get("position")
        if p:
            label = {"daily": "daily loss limit", "drawdown": "drawdown halt"}.get(
                kind, "emergency halt")
            trade_id = p.get("trade_id")
            try:
                # Price first, outside the lock; book second, under it.
                px = price if price is not None else self._exit_price_for(p)
                with self.state_lock:
                    cur = s.get("position")
                    if cur and cur.get("trade_id") == trade_id:
                        res = self.b.close_now(cur, reason=label, price=px)
                        self._finish(res, notify=False)
                        exit_note = (f"Closed {cur['side']} {cur['symbol']} at "
                                     f"{res['exit']:,.4f} for {res['pnl']:+.4f} USDT "
                                     f"({label}).")
                    else:
                        exit_note = "The position had already closed."
            except Exception as e:
                exit_note = (f"🚨 COULD NOT CLOSE the simulated position "
                             f"({type(e).__name__}: {scrub(e, *self._secrets)}). "
                             f"Its stop is still in force and entries are blocked.")
        with self.state_lock:
            if kind == "daily":
                self.risk.lock_day()
            else:
                self.risk.halt(reason)
            try:
                save_state(s, self.path)
            except Exception as e:
                say(f"(warning: could not save state after an emergency: "
                    f"{type(e).__name__}: {e})")
        icon = "🚨" if kind != "daily" else "⛔"
        tail = ("Trading is locked until the next trading day (IST)."
                if kind == "daily" else
                "Trading is HALTED until you review it and send /clearhalt CONFIRM.")
        self.n.send(f"{icon} {reason}\n{exit_note}\nNew entries are disabled. {tail}")

    def _finish(self, res, notify=True):
        """Book a closed trade.

        Order matters. The broker has already moved the balance by the time we
        get here, so the bookkeeping - risk counters, clearing the position,
        saving the state - must happen before anything that can fail. It used
        to notify first, and a failed notification (which a non-UTF-8 console
        made likely) aborted the function with the position still in the state
        file, so the next loop closed the same trade again and debited the P&L
        twice.
        """
        s = self.s
        p = s.get("position")
        if p is None:
            return
        equity_before = float(p.get("equity_before") or self.b.balance())
        closed_ms = now_ms()
        self.risk.record_close(res["pnl"], closed_ms)
        s["position"] = None
        balance_after = self.b.balance()
        try:
            journal(journal_row(p, res, self.b.name, equity_before, balance_after,
                                closed_ms))
        except Exception as e:
            say(f"(warning: could not append to the trade journal: {type(e).__name__}: {e})")
        try:
            save_state(s, self.path)
        except Exception as e:
            say(f"(warning: could not save state after closing: {type(e).__name__}: {e})")
        if not notify:
            return
        emoji = "✅" if res["pnl"] > 0 else ("➖" if res["pnl"] == 0 else "❌")
        planned = float(p.get("planned_risk") or 0.0)
        rtxt = f" ({res['pnl'] / planned:+.2f}R)" if planned > 0 else ""
        self.n.send(f"{emoji} Closed {p['side']} {p['symbol']} ({res['reason']})\n"
                    f"Entry {p['entry']:,.4f} → exit {res['exit']:,.4f}\n"
                    f"Planned risk {planned:.4f} → actual {res['pnl']:+.4f} USDT"
                    f"{rtxt} ({self.inr(res['pnl'])})\n"
                    f"Fees {float(res.get('fees') or 0):.4f} | slippage "
                    f"{float(res.get('slippage') or 0):.4f}\n"
                    f"Balance {balance_after:.4f} USDT | trades today "
                    f"{int(s.get('trades_today', 0))}/{self.cfg['max_trades_per_day']}")

    def look_for_entry(self):
        cfg = self.cfg
        for sym in cfg["symbols"]:
            try:
                c1 = self.c.closed_klines(sym, cfg["entry_interval"], 300)
            except Exception as e:
                self.data_fail += 1
                self.last_data_error = f"{type(e).__name__}: {scrub(e, *self._secrets)}"[:160]
                self.n.send(f"⚠️ {sym}: no candles ({type(e).__name__}) - not trading on "
                            f"missing data")
                continue
            if not c1 or self.s["last_candle"].get(sym) == c1[-1]["t"]:
                continue
            self.s["last_candle"][sym] = c1[-1]["t"]
            bad = self.data_problem(sym, c1=c1)
            if bad:
                self.n.send(f"⚠️ {sym}: {bad} - no entry (fail closed)")
                continue
            closes = [x["c"] for x in c1]
            fast, slow = ema(closes, cfg["fast_ema"]), ema(closes, cfg["slow_ema"])
            try:
                bias = self._bias(sym)
            except Exception as e:
                self.n.send(f"⚠️ {sym}: trend data unavailable ({type(e).__name__}) - "
                            f"no entry")
                continue
            sig = signal_at(c1, fast, slow, len(c1) - 1, bias, cfg)
            if not sig:
                continue
            side, stop = sig["side"], sig["stop"]
            try:
                # A new position is never sized off a cached quote: this always
                # goes to the exchange, however warm the cache is.
                price = self.entry_price(sym)
            except Exception as e:
                self.n.send(f"⚠️ {sym} {side} setup found but the price is unavailable "
                            f"({type(e).__name__}) - no entry")
                continue
            why = self.data_problem(sym, c1=c1, price=price) or check_stop(side, price, stop, cfg)
            if not why:
                try:
                    f = self.c.funding_pct(sym)
                except Exception as e:
                    why = f"funding unavailable ({type(e).__name__})"
                else:
                    against = f if side == "LONG" else -f
                    if against > cfg["max_adverse_funding_pct"]:
                        why = f"funding {f:+.3f}% is against a {side}"
            # Re-check every gate immediately before sizing: a limit may have
            # moved since the top of the loop (a close, a new day, a command).
            if not why:
                blocks = self.entry_blocks()
                if blocks:
                    why = blocks[0]
            qty, risk, note = 0.0, 0.0, ""
            equity = 0.0
            if not why:
                equity, err = self.equity()
                if err:
                    why = f"account cannot be valued ({err[:60]})"
            # Size off the price we will actually FILL at, not the quote. Entry
            # slippage always moves the fill away from the stop, so sizing on
            # `price` understates the stop distance - and therefore the real
            # loss - by one slippage allowance. At the 0.4% minimum stop that
            # is ~7% of the risk, which would quietly breach the cap on every
            # tight-stopped trade. The broker derives its fill from `price`
            # with this same function, so the two agree exactly.
            fill_est = price
            if not why:
                if not is_finite(price, stop, equity):
                    why = ("price, stop or equity is not a finite number - "
                           "refusing to size (fail closed)")
                else:
                    fill_est = adverse_fill(side, price, cfg.get("slippage_pct", 0.0),
                                            entering=True)
                    if check_stop(side, fill_est, stop,
                                  {**cfg, "min_stop_pct": 0, "max_stop_pct": 100}):
                        why = (f"the projected fill {fill_est:.6f} after slippage is "
                               f"already past the stop {stop:.6f}")
            if not why:
                try:
                    qty, risk, note = size_position(equity, fill_est, stop,
                                                    self.rules[sym], cfg,
                                                    fractional=self.b.fractional_qty)
                except Exception as e:
                    # Sizing must never be able to let a trade through by
                    # raising. An exception here is a rejection, not a pass.
                    qty, risk, note = 0.0, 0.0, ""
                    why = (f"position sizing failed ({type(e).__name__}: "
                           f"{scrub(e, *self._secrets)[:60]}) - no entry")
                else:
                    cap = self.risk.max_risk_usdt(equity)
                    if not is_finite(qty, risk) or qty <= 0 or risk <= 0:
                        why = note or "sizing produced no usable quantity"
                    elif cap <= 0:
                        why = "no usable per-trade risk cap - refusing to size"
                    elif risk > cap + 1e-9:
                        why = (f"sizing produced {risk:.4f} USDT of risk against a cap "
                               f"of {cap:.4f} USDT")
                    elif risk > self.risk.remaining_daily_loss(equity) + 1e-9:
                        why = (f"{risk:.4f} USDT of risk does not fit in the "
                               f"{self.risk.remaining_daily_loss(equity):.4f} USDT left "
                               f"of today's loss budget")
            if why:
                self.n.send(f"👀 {sym} {side} setup found but skipped: {why}")
                continue
            i = len(c1) - 1
            meta = {"trade_id": new_trade_id(), "planned_risk": risk,
                    "strategy": STRATEGY_NAME,
                    "market_condition": market_condition(side, fill_est, stop,
                                                         fast[i], slow[i]),
                    "equity_before": equity}
            try:
                pos = self.b.open(sym, side, qty, stop, price, self.rules[sym], meta=meta)
            except Exception as e:
                self.n.send(f"🚨 {sym} entry refused: {type(e).__name__}: "
                            f"{scrub(e, *self._secrets)}")
                continue
            # A position without a usable stop is not allowed to exist.
            if check_stop(side, pos["entry"], pos["stop"],
                          {**cfg, "min_stop_pct": 0, "max_stop_pct": 100}):
                try:
                    self.b.close_now(pos, reason="no valid stop")
                except Exception:
                    pass
                self.n.send(f"🚨 {sym}: the simulated fill ended up past the stop - "
                            f"position discarded, no trade booked.")
                continue
            # Re-price the risk from the fill the broker actually gave us, not
            # from the projection we sized against. The two agree today, but a
            # broker is free to fill elsewhere, and the cap has to be enforced
            # on what happened rather than on what was predicted. A position
            # that would exceed the cap is discarded, not kept and reported.
            actual_risk = (pos["qty"] * cost_per_unit(
                pos["entry"], pos["stop"], cfg["taker_fee_pct"],
                cfg.get("slippage_pct", 0.0)))
            cap = self.risk.max_risk_usdt(equity)
            if not is_finite(actual_risk) or cap <= 0 or actual_risk > cap + 1e-9:
                try:
                    self.b.close_now(pos, reason="risk cap breached at fill")
                except Exception:
                    pass
                self.n.send(f"🚨 {sym}: the actual fill {pos['entry']:,.4f} prices this "
                            f"trade at {actual_risk:.4f} USDT of risk against a "
                            f"{cap:.4f} USDT cap - position discarded, no trade "
                            f"booked.")
                continue
            pos["planned_risk"] = actual_risk
            risk = actual_risk
            with self.state_lock:
                # Last line of defence against anything that changed while we
                # were sizing: a command may have opened or closed a position,
                # or switched entries off, in the second this took.
                if self.s.get("position"):
                    self.n.send(f"👀 {sym} {side} setup dropped: a position appeared "
                                f"while it was being sized.")
                    return
                if not self.s.get("paper_enabled") or self.s.get("paused"):
                    self.n.send(f"👀 {sym} {side} setup dropped: entries were "
                                f"switched off while it was being sized.")
                    return
                if self.risk.is_halted() or self.risk.is_locked_today():
                    self.n.send(f"👀 {sym} {side} setup dropped: the engine was "
                                f"halted or locked while it was being sized.")
                    return
                self.s["position"] = pos
                self.risk.record_open(pos["opened"])
                try:  # persist at once: a crash before step() ends must not lose it
                    save_state(self.s, self.path)
                except Exception as e:
                    say(f"(warning: could not save state after opening: "
                        f"{type(e).__name__}: {e})")
            self.n.send(f"🟢 Opened {side} {sym} ({self.b.name} - simulated)\n"
                        f"Entry {pos['entry']:,.4f} | stop {pos['stop']:,.4f} | "
                        f"target {pos['target']:,.4f}\n"
                        f"Size {fmt_qty(pos['qty'])} (≈{pos['qty'] * pos['entry']:.2f} USDT) | "
                        f"planned risk {risk:.4f} USDT ({self.inr(risk)}) = "
                        f"{risk / equity * 100:.2f}% of equity\n"
                        f"Trade {int(self.s.get('trades_today', 0))}/"
                        f"{cfg['max_trades_per_day']} today | id {pos['trade_id']}"
                        + (f"\nNote: {note}" if note else ""))
            return

    # ---- the loop --------------------------------------------------------

    def step(self):
        """One trading cycle.

        Structured in three phases so that the only thing done while the state
        lock is held is local work:

          A. decide what to look at      (lock, microseconds)
          B. talk to the exchange        (NO lock - this is the slow part)
          C. apply what came back        (lock, microseconds, no network)

        That is what lets a Telegram command be answered mid-cycle: phase B can
        sit in a socket read for ten seconds without the command thread waiting
        on anything. Every risk decision still happens in the same cycle, on
        data fetched in that cycle - nothing is deferred and nothing is skipped.
        """
        s = self.s
        cycle = time.perf_counter()
        self.loops += 1
        if self.lock:
            touch_engine_lock(self.lock, self.started)

        # When no command worker is running (the --once path, and the tests)
        # this loop is the only thing that can poll, so it still does.
        if not self.worker_polls:
            self.handle_commands()

        # ---- phase A: what is there to do ---------------------------------
        with self.state_lock:
            pos = dict(s["position"]) if s.get("position") else None
            trade_id = pos.get("trade_id") if pos else None

        # ---- phase B: the slow part, lock-free ----------------------------
        plan, plan_error = None, None
        if pos:
            try:
                if self.phased:
                    plan = self.b.evaluate(pos, self.rules.get(pos["symbol"]))
                else:
                    plan = {"legacy": True}
            except Exception as e:
                plan_error = e
                self.data_fail += 1
                self.last_data_error = (f"{type(e).__name__}: "
                                        f"{scrub(e, *self._secrets)}")[:160]
        eq, err = self.equity()
        mark_px = None
        if pos and not err:
            mark_px, _age = self.cached_price(pos["symbol"],
                                              self.cfg.get("price_cache_sec", 5))

        # ---- phase C: apply ------------------------------------------------
        with self.state_lock:
            self.risk.roll(eq if not err else self.b.balance())
            cur = s.get("position")
            still_mine = cur and cur.get("trade_id") == trade_id
            if plan is not None and still_mine:
                if plan.get("legacy"):
                    # LiveBroker path, untouched: it does its own network work.
                    res = self.b.check(cur, self.rules.get(cur["symbol"]))
                    if res:
                        self._finish(res)
                else:
                    cur["scanned_to"] = plan["scanned_to"]
                    cur["checked"] = now_ms()
                    if "exit" in plan:
                        self._finish(self.b.book(cur, plan["exit"], plan["reason"]))
            elif plan_error is not None:
                say(f"(warning: could not check the open position: "
                    f"{self.last_data_error})")

        # Loss protection runs on realised + unrealised equity, every cycle,
        # whether or not a position is open. It is evaluated here, after any
        # exit has been booked, so it sees the true current equity.
        if not err:
            eq_now, err_now = self.equity()
            if not err_now:
                breach = None
                with self.state_lock:
                    self.risk.observe(eq_now)
                    b = self.risk.breach(eq_now)
                    if b and not (b[0] == "daily" and self.risk.is_locked_today()) \
                            and not (b[0] == "drawdown" and self.risk.is_halted()):
                        breach = b
                if breach:
                    # emergency() takes the lock itself, in short sections, and
                    # is handed the price so it never fetches while holding it.
                    self.emergency(breach[1], kind=breach[0], price=mark_px)

        if not s.get("position"):
            blocks = self.entry_blocks()
            note = "; ".join(blocks)
            if note != s.get("block_note"):
                with self.state_lock:
                    s["block_note"] = note
                if blocks:
                    self.n.send("⏸ Not taking entries:\n  - " + "\n  - ".join(blocks))
            if not blocks:
                self.look_for_entry()

        if time.time() - s.get("last_status", 0) >= self.cfg["status_every_hours"] * 3600:
            s["last_status"] = time.time()
            self.n.send(self.status_text())
        with self.state_lock:
            save_state(s, self.path)
        self.last_cycle_ms = self.perf.record("cycle step",
                                              (time.perf_counter() - cycle) * 1000)

    # ---- the command worker ----------------------------------------------

    def _command_loop(self):
        """Poll Telegram and run commands on a thread of their own.

        This is the whole point of the threading: a trading cycle that is stuck
        waiting on Binance cannot stop a command being read and answered,
        because reading it happens here and answering it needs no market data.
        """
        poll = max(1, int(self.cfg.get("command_poll_sec", 2)))
        while not self._stop.is_set():
            try:
                self.handle_commands()
            except Exception as e:
                self.last_error = f"{type(e).__name__}: {scrub(e, *self._secrets)}"
                say(f"(warning: command worker: {self.last_error})")
            self._stop.wait(poll)

    def start_command_worker(self):
        """Start the one and only command poller. Idempotent."""
        if self._worker and self._worker.is_alive():
            return self._worker
        self._stop.clear()
        self._worker = threading.Thread(target=self._command_loop,
                                        name="telegram-commands", daemon=True)
        self._worker.start()
        return self._worker

    def stop_command_worker(self, timeout=5):
        self._stop.set()
        w = self._worker
        if w and w.is_alive():
            w.join(timeout=timeout)
        self._worker = None

    @property
    def worker_polls(self):
        """True when the worker owns polling, so step() must not also poll."""
        return bool(self._worker and self._worker.is_alive())

    def _idle(self, seconds):
        """Wait between cycles.

        When the command worker is running this just sleeps - polling there as
        well would mean two readers of the same Telegram offset. Without a
        worker (the --once path and the tests) it keeps the old behaviour of
        polling in slices.
        """
        if self.worker_polls:
            self._stop.wait(seconds)
            return
        slice_sec = max(1, min(int(self.cfg.get("command_poll_sec", 2)),
                               int(seconds) or 1))
        waited = 0.0
        while waited < seconds:
            time.sleep(min(slice_sec, seconds - waited))
            waited += slice_sec
            try:
                self.handle_commands()
            except Exception as e:
                self.last_error = f"{type(e).__name__}: {scrub(e, *self._secrets)}"
                say(f"(warning: command polling failed: {self.last_error})")

    def run(self, once=False):
        s = self.s
        self.n.send(
            f"🤖 Engine started ({self.b.name} - simulated money, no real orders) watching "
            f"{', '.join(self.cfg['symbols'])}.\n"
            f"Entries are {'ON' if s.get('paper_enabled') else 'OFF'}"
            f"{'' if s.get('paper_enabled') else ' - send /paper_on to arm them'}.\n"
            f"Balance {self.b.balance():.2f} USDT. /help for commands.")
        # One command worker for a continuous run. --once stays single-threaded
        # so a one-shot check behaves exactly as it always has.
        if not once:
            self.start_command_worker()
        errors = 0
        while True:
            try:
                self.step()
                errors = 0
            except KeyboardInterrupt:
                raise
            except Exception as e:
                errors += 1
                self.error_count += 1
                self.last_error = f"{type(e).__name__}: {scrub(e, *self._secrets)}"
                if isinstance(e, ApiError) and e.code == -1021:
                    try:
                        self.c.sync_time()
                    except Exception:
                        pass
                if errors in (1, 5) or errors % 30 == 0:
                    self.n.send(f"⚠️ Error (#{errors}): {self.last_error}")
            if once:
                return
            self._idle(self.cfg["check_every_sec"])

    def shutdown(self):
        """Stop the command worker. Safe to call when one was never started."""
        self.stop_command_worker()


# ---------------------------------------------------------------- backtest

def backtest_symbol(cfg, c1, c4, rules, fractional=True, start_balance=None):
    """Replay the strategy over closed candles with fees, slippage and the
    full risk engine.

    The same RiskManager that gates the live loop gates this replay, so a limit
    cannot pass here and fail there. Nothing looks ahead: the signal is taken
    from candle i, the entry fills at the open of candle i+1, and management
    only ever uses candle ranges at or after the entry candle.
    """
    closes1 = [x["c"] for x in c1]
    fast, slow = ema(closes1, cfg["fast_ema"]), ema(closes1, cfg["slow_ema"])
    closes4 = [x["c"] for x in c4]
    ema4 = ema(closes4, cfg["trend_ema"])
    fee, slip = cfg["taker_fee_pct"], cfg.get("slippage_pct", 0.0)
    bal = start = float(cfg["paper_start_balance"] if start_balance is None else start_balance)
    peak, max_dd = bal, 0.0
    trades, skipped_small, skipped_rules, skipped_risk = [], 0, 0, 0
    state = {}
    risk = RiskManager(cfg, state)
    risk.roll(bal, ms=c1[0]["t"] if c1 else None, force=True)
    pos, j = None, -1
    for i in range(len(c1) - 1):
        c = c1[i]
        while j + 1 < len(c4) and c4[j + 1]["T"] <= c["T"]:
            j += 1
        # The engine "wakes up" when a candle closes, so that is the moment the
        # day rolls over and the moment a P&L is booked against. Rolling on the
        # candle's open instead would book a loss from 00:29 against yesterday.
        risk.roll(bal, ms=c["T"])
        if pos:  # manage on this candle's range
            hit_stop = c["l"] <= pos["stop"] if pos["side"] == "LONG" else c["h"] >= pos["stop"]
            hit_tgt = c["h"] >= pos["target"] if pos["side"] == "LONG" else c["l"] <= pos["target"]
            if hit_stop or hit_tgt:
                # A stop is a market order: a candle that opened past it fills at
                # the open, not at the stop level. Same rule as the paper broker.
                raw = (stop_fill_price(pos["side"], pos["stop"], c["o"])
                       if hit_stop else pos["target"])
                exit_px = adverse_fill(pos["side"], raw, slip, entering=False)
                pnl, fees = trade_pnl(pos["side"], pos["entry"], exit_px, pos["qty"], fee)
                bal += pnl
                risk.record_close(pnl, c["T"])
                trades.append({**pos, "exit": exit_px, "pnl": pnl, "fees": fees,
                               "slippage": pos["entry_slippage"]
                               + abs(exit_px - raw) * pos["qty"],
                               "exit_reason": "stop loss" if hit_stop else "take profit",
                               "closed_ms": c["T"],
                               "closed": ist(c["T"]).strftime("%Y-%m-%d %H:%M"),
                               "closed_ist": ist(c["T"]).strftime("%Y-%m-%d %H:%M:%S"),
                               "equity_after": bal})
                peak = max(peak, bal)
                max_dd = max(max_dd, (peak - bal) / peak * 100 if peak > 0 else 0.0)
                pos = None
                # A breach that the live engine would act on also stops the replay
                # for the rest of the day / for good.
                breach = risk.breach(bal)
                if breach:
                    if breach[0] == "daily":
                        risk.lock_day(c["T"])
                    else:
                        risk.halt(breach[1], c["T"])
            continue
        blocks = risk.entry_blocks(bal, ms=c["T"])
        if blocks:
            skipped_risk += 1
            continue
        sig = signal_at(c1, fast, slow, i, trend_bias_at(closes4, ema4, j), cfg)
        if not sig:
            continue
        raw_entry = c1[i + 1]["o"]  # fill at next candle's open
        if check_stop(sig["side"], raw_entry, sig["stop"], cfg):
            skipped_rules += 1
            continue
        # The fill is computed BEFORE sizing and the size is worked out from
        # it, because the fill is what the loss is measured from. Sizing off
        # raw_entry understates the stop distance by one slippage allowance and
        # so overstates the quantity, which is how a backtest reports a 0.5%
        # risk per trade while actually having risked more.
        entry = adverse_fill(sig["side"], raw_entry, slip, entering=True)
        if (sig["side"] == "LONG" and sig["stop"] >= entry) or \
           (sig["side"] == "SHORT" and sig["stop"] <= entry):
            skipped_rules += 1
            continue
        qty, planned, note = size_position(bal, entry, sig["stop"], rules, cfg,
                                           fractional=fractional)
        if qty <= 0:
            skipped_small += 1
            continue
        risk.record_open(c1[i + 1]["t"])
        pos = {"trade_id": new_trade_id(), "symbol": rules.get("symbol", ""),
               "side": sig["side"], "entry": entry, "stop": sig["stop"],
               "initial_stop": sig["stop"], "qty": qty, "planned_risk": planned,
               "strategy": STRATEGY_NAME,
               "market_condition": market_condition(sig["side"], entry, sig["stop"],
                                                    fast[i], slow[i]),
               "target": target_for(sig["side"], entry, sig["stop"], cfg["reward_risk"]),
               "entry_slippage": abs(entry - raw_entry) * qty,
               "equity_before": bal,
               "opened_ms": c1[i + 1]["t"],
               "opened": ist(c1[i + 1]["t"]).strftime("%Y-%m-%d %H:%M"),
               "opened_ist": ist(c1[i + 1]["t"]).strftime("%Y-%m-%d %H:%M:%S")}
    wins = [t for t in trades if t["pnl"] > 0]
    return {"start": start, "end": bal, "trades": trades, "wins": len(wins),
            "fees": sum(t["fees"] for t in trades),
            "slippage": sum(t["slippage"] for t in trades), "max_dd": max_dd,
            "skipped_small": skipped_small, "skipped_rules": skipped_rules,
            "skipped_risk": skipped_risk, "open": pos,
            "halted": bool(state.get("halted")), "halt_reason": state.get("halt_reason", "")}


def run_backtest(cfg, client):
    rules = client.rules(cfg["symbols"])
    end = now_ms()
    start = end - cfg["backtest_days"] * 86400000
    warm4 = (cfg["trend_ema"] + 10) * 4 * 3600000
    rows = []
    for sym in cfg["symbols"]:
        say(f"Downloading {sym} candles ...")
        c1 = client.klines_range(sym, cfg["entry_interval"], start, end)
        c4 = client.klines_range(sym, cfg["trend_interval"], start - warm4, end)
        r = backtest_symbol(cfg, [c for c in c1 if c["T"] < end],
                            [c for c in c4 if c["T"] < end],
                            {**rules[sym], "symbol": sym})
        n = len(r["trades"])
        ret = (r["end"] / r["start"] - 1) * 100
        say(f"\n=== {sym}: last {cfg['backtest_days']} days, start {r['start']:.2f} USDT ===")
        say(f"Trades: {n} | wins: {r['wins']} ({(r['wins'] / n * 100) if n else 0:.0f}%)")
        say(f"End balance: {r['end']:.2f} USDT ({ret:+.1f}%, ≈{ret / (cfg['backtest_days'] / 30):+.2f}%/month)")
        say(f"Fees paid: {r['fees']:.2f} USDT | slippage: {r['slippage']:.2f} USDT "
            f"| max drawdown: {r['max_dd']:.1f}%")
        say(f"Signals skipped - too small: {r['skipped_small']}, stop rules: "
            f"{r['skipped_rules']}, risk limits: {r['skipped_risk']}")
        if r["halted"]:
            say(f"Risk engine HALTED during the replay: {r['halt_reason']}")
        if r["open"]:
            # Previously computed and then thrown away, so the numbers above
            # silently omitted a trade that was still running.
            o = r["open"]
            say(f"Still open when the data ran out: {o['side']} entered {o['opened']} at "
                f"{o['entry']:.4f} (stop {o['stop']:.4f}, target {o['target']:.4f}) - "
                f"not counted above")
        for t in r["trades"]:
            rows.append({"symbol": sym, **{k: (round(v, 6) if isinstance(v, float) else v)
                                           for k, v in t.items()}})
    if rows:
        path = os.path.join(HERE, "backtest_trades.csv")
        fields = sorted({k for row in rows for k in row})
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows(rows)
        say(f"\nEvery trade saved to {path}")
    say("\nReminder: past results don't predict future ones. Funding is not included. "
        "Slippage is a flat allowance, not a measured book impact. A positive "
        "backtest is not evidence the strategy is profitable - see "
        "`--mode research` for out-of-sample and walk-forward checks.")


# ---------------------------------------------------------------- main

def sync_or_exit(client):
    """Clock sync, with a readable message instead of a traceback when offline."""
    try:
        client.sync_time()
    except Exception as e:
        raise SystemExit(
            f"Could not reach {client.base}:\n"
            f"  {type(e).__name__}: {e}\n"
            f"The bot needs Binance market data to do anything at all. Check the\n"
            f"connection - and note that some ISPs resolve binance.com to a block\n"
            f"page, in which case no mode will work until you run it on a network\n"
            f"or VPS that can reach the API.")


def main():
    enable_utf8_stdout()
    ap = argparse.ArgumentParser(description="Binance futures trend-pullback bot "
                                             "(paper trading)")
    ap.add_argument("--config", default=os.path.join(HERE, "config.json"))
    ap.add_argument("--mode", choices=list(MODES))
    ap.add_argument("--once", action="store_true", help="run a single check and exit")
    args = ap.parse_args()
    cfg = load_config(args.config)
    if args.mode:
        cfg["mode"] = args.mode
    validate_config(cfg)          # rejects any mode outside MODES, before connecting
    mode = cfg["mode"]

    if mode in ("backtest", "research", "paper"):
        # public_only: no credentials loaded, and the client raises on any signed
        # request or any non-GET method. Nothing on this path can place an order.
        client = Binance(cfg, base=LIVE_BASE, public_only=True)
        sync_or_exit(client)
        if mode == "backtest":
            return run_backtest(cfg, client)
        if mode == "research":
            import research
            return research.run_research(cfg, client)
        path = state_path(mode, True)
        state = load_state(path)
        n = Notifier(cfg)
        lock = acquire_engine_lock(stale_sec=max(180, 5 * cfg["check_every_sec"]))
        # The caching view goes in front of the client, so the broker and the
        # engine share one set of quotes and candles per cycle instead of each
        # fetching its own. Trade entries still bypass it (see entry_price).
        md = MarketData(client, cfg)
        bot = Bot(cfg, md, PaperBroker(md, cfg, state), n, state, path, lock=lock)
        try:
            bot.run(args.once)
        finally:
            bot.shutdown()
            release_engine_lock(lock)
        return

    if mode != "live":  # unreachable: validate_config already rejected it
        raise SystemExit(f"Refusing to start: unhandled mode {mode!r}.")
    if not cfg["api_key"] or not cfg["api_secret"]:
        raise SystemExit("Live mode needs api_key and api_secret (config.json or env vars).")
    if not cfg["use_testnet"] and not cfg["i_understand_real_money"]:
        raise SystemExit("Refusing to trade real money: set \"i_understand_real_money\": true "
                         "in config.json if you really mean it.")
    client = Binance(cfg)
    sync_or_exit(client)
    path = state_path(mode, cfg["use_testnet"])
    state = load_state(path)
    n = Notifier(cfg)
    Bot(cfg, client, LiveBroker(client, cfg, state, n), n, state, path).run(args.once)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        say("\nStopped. Open positions (if any) keep their exchange stop-loss.")
