# Futures trend-pullback bot (Binance USDT-M) — paper trading

Watches ETH and SOL perpetuals, trades one simple rule set on **simulated money and
real prices**, and is driven from your phone over Telegram.

**This is a paper-trading simulator.** It places no orders. Paper, backtest and
research mode build an exchange client that holds no API credentials and raises
on any signed request, so no code path in normal use can reach an order.

## What it does

| Part | Rule |
|---|---|
| Trend filter | 4h close above the 200 EMA → longs only; below → shorts only |
| Entry | A closed 1h candle dips into the 20 EMA and closes back on the trend side (20 EMA on the right side of the 50 EMA) |
| Stop loss | **Mandatory.** Beyond the swing low/high of the last 10 hourly candles. Skips trades if the stop is tighter than 0.4% or wider than 3%. A stop may be tightened, never widened |
| Target | 2 × the risk |
| Size | Risks **0.5% of current equity**, with both taker fees and a slippage allowance on both legs inside that budget. Leverage follows from the size, max 5x |
| Daily | Stops for the day at **−2%** of the day's starting equity · max **3 entries** · max 2 losses · 60-minute cooldown after a loss |
| Account | **Halts permanently at −5%** from the highest equity ever recorded, until you review it and clear it by hand |
| Other | One position at a time · no averaging down · no martingale · no risk increase after a loss · skips when funding is against you · refuses to trade on stale, missing or implausible data |

The strategy itself is unchanged from the earlier build. Everything above that is
a risk rule is new.

Why no BTC by default: Binance's minimum BTC futures order is about $100. On a ~$22
account that is far more than 0.5% of equity, and the bot now **rejects** such a
trade rather than sizing up to the minimum. Add `"BTCUSDT"` back once your account
is bigger.

## Setup (about 15 minutes)

1. **Install Python 3.10+**, then run: `pip install requests`
2. Copy `config.example.json` to `config.json`.
3. **Make a Telegram bot.** In Telegram, message **@BotFather** and send `/newbot`.
4. **Wire up Telegram** — do not edit the token into `config.json` by hand:

   ```
   python telegram_setup.py
   ```

   It asks for the token on a hidden prompt (nothing is echoed or logged), checks
   it with `getMe`, waits for you to send `/start` to your bot, reads your private
   chat id, sends a test message, and writes only `telegram_token` and
   `telegram_chat_id` into `config.json`. Everything else in the file — Binance
   keys, `mode`, the risk settings — is left exactly as it was, and the old file is
   copied to `config.json.bak` first. Nothing is written unless the test message
   actually arrives.

   Useful flags: `--check` (re-test the saved settings, change nothing),
   `--chat-id <id>` (skip the `/start` wait), `--wait 300`, `--no-test-message`,
   `--no-save`. To run it unattended, set `TELEGRAM_TOKEN` in the environment
   instead of typing it.

### If Telegram fails

Run `python telegram_setup.py --check` — it names the cause instead of printing a
traceback:

| Symptom | What it actually means |
| --- | --- |
| **HTTP 404** from `getMe`/`sendMessage` | The token is wrong, truncated, or still `PASTE_FROM_BOTFATHER`. The token is part of the URL path (`/bot<TOKEN>/getMe`), so a bad token makes the whole path a non-existent route — 404, not 401. This is the single most common cause. |
| **HTTP 401** | The token is well-formed but revoked or regenerated. Get a new one from BotFather (`/mytoken`). |
| **Timeout** | `api.telegram.org` is reachable but silent — most ISPs in some regions and nearly all corporate/campus networks filter Telegram. Use another network, a VPN, or set `HTTPS_PROXY`. |
| **HTTP 409** | Another copy of the bot, or a webhook, is already consuming the updates. |
| **HTTP 400 / 403** on send | The chat id is wrong, or `/start` was never sent to the bot from that chat. |

The bot itself prints the same hints at runtime, once per fault, and never prints
the token.

## Step 1: backtest (5 minutes)

```
python trading_bot.py --mode backtest
```
Replays the last 180 days with fees, slippage and the full risk engine, and prints
win rate, return, costs and drawdown. Every trade is saved to `backtest_trades.csv`.

## Step 2: validate it properly (10 minutes)

```
python trading_bot.py --mode research
```
This is the one to trust more than the backtest. It splits the history
chronologically, reports the earlier 70% and the held-back 30% **separately**, then
replays the same fixed rules over four consecutive walk-forward windows, each
starting from the same balance. Longs and shorts, ETH and SOL, exit reasons and
market conditions are all broken out, and the report ends with a plain statement
of what a sample that size can and cannot establish. Written to
`research_report.txt`.

Nothing is optimised or searched anywhere in that report — the rules come straight
from `config.json` — so the hold-out window is a genuine hold-out rather than
something that has been tuned against.

## Step 3: paper trade (2–4 weeks)

```
python trading_bot.py --mode paper
```

The engine starts in **standby**: it watches the market, answers Telegram and
manages any open position, but takes no new entries until you send `/paper_on`.
That is deliberate for a fresh install.

Leave it running. A laptop that sleeps stops the engine. A cheap VPS or an old
Android phone with Termux works too. Every closed trade goes to `trades.csv`,
which is your permanent journal — nothing ever deletes it, not even
`/resetpaper`.

## Running it from your phone

`/paper_on` and `/paper_off` switch *entries* on and off inside the one running
process; there is no second bot to start, and Telegram is polled every ~3 seconds
while the engine idles, so commands answer quickly.

**The limitation, stated plainly: Telegram cannot start the engine.** If the Python
process is stopped, or the laptop is off or asleep, the bot is not running, your
commands go nowhere, and they are **discarded** when it next starts (so a
`/paper_on` sent overnight cannot arm the bot while you are not watching). What
Telegram controls is a bot that is already running. To have it answer at all hours,
run it somewhere that stays on — a small VPS, or a phone with Termux.

While the engine is off, an open paper position is simply frozen: its stop is not
an exchange order, it is a rule this simulator applies. On restart the engine
pages through every 1-minute candle it missed, so a stop or target hit during the
downtime is found and booked at the right price.

A restart never duplicates anything: the position, the risk state, the day's
counters, the equity peak and the activation flag all come back from
`bot_state_paper.json`, the last processed candle is remembered so the same signal
is not retaken, each trade carries an id so the journal cannot double-count it,
and a heartbeat lock file refuses to let a second engine run against the same
state.

## Telegram commands

| Command | What it does |
|---|---|
| `/help` | the list below |
| `/status` | running state, entries on/off, market, trend, uptime, risk summary |
| `/paper_on` | allow new simulated entries |
| `/paper_off` | no new entries; an open position is still managed to its exit |
| `/positions` | entry, current price, stop, target, unrealised P&L, risk still on the table |
| `/performance` | full statistics from journalled closed trades only |
| `/balance` | virtual balance, equity, available, peak and drawdown |
| `/history` | the last completed trades |
| `/risk` | every configured limit and what is left of today's allowance |
| `/halt` | emergency: close the simulated position and block entries |
| `/clearhalt CONFIRM` | lift a halt after a manual review (re-baselines the equity peak) |
| `/resetpaper` | reset the virtual account — asks, then needs `/resetpaper CONFIRM` |
| `/health` | market-data and Telegram connectivity, error counts, engine health |

`/pause`, `/resume` and `/close` still work as aliases. Only the configured
`telegram_chat_id` is obeyed; anything from another chat is dropped without a
reply, so a stranger learns nothing — not even that the bot exists. Each update is
acted on once, and the ids already handled are persisted, so a redelivered command
cannot fire twice across a restart. No reply ever contains a token, a chat id or an
API key.

No command can raise a risk limit, bypass a gate, or unlock a day that the daily
loss limit has closed.

### Why commands answer quickly

Commands used to be read and run by the same single thread that ran the trading
cycle, so a cycle waiting on Binance delayed them. Three things changed:

- **A command worker.** One dedicated thread polls Telegram every 2 seconds and
  runs the commands. It is the *only* poller: while it is alive the trading loop
  does not poll (two readers of one `getUpdates` offset would drop or replay a
  command), and `--once` runs single-threaded exactly as before.
- **Reports never fetch.** `/status`, `/risk`, `/balance`, `/positions`,
  `/performance`, `/history`, `/health` and `/help` are built from state and
  cached market data only. They print how old the quote they used is — "live",
  "40s ago" — so a cached number is never passed off as current, and they still
  answer when the exchange is unreachable.
- **The state lock is never held across a network call.** A trading cycle
  snapshots what it needs (microseconds, locked), talks to the exchange (no
  lock), then applies the result (microseconds, locked). Mutating commands take
  the same lock and so wait microseconds, not seconds.

Measured against the same fake exchange at 200 ms per request (what a healthy
connection from here actually costs), with the real 1.7-day-old position:

| | before | after |
|---|---|---|
| steady-state trading cycle | 1233 ms, 4 requests | 410 ms, 2 requests |
| `/status` | 1203 ms, 6 requests | **0 ms, 0 requests** |
| `/risk` | 201 ms, 1 request | **0 ms, 0 requests** |
| slowest command | 1203 ms | **0 ms** |
| worst case before a command is even *read* | cycle + up to 3 s poll slice | ≤ 2 s, independent of the cycle |

`price_cache_sec` (5 s) is only a within-cycle reuse window, and
`display_cache_sec` (150 s) bounds how old a quote a report will show.
**Neither can affect a trade**: an entry always calls `fresh_price()`, which
goes to the exchange every time, and candle freshness is judged on the candles'
own close times rather than on when they were fetched.

### Network budgets

A single GET could previously block for 15 s × 3 attempts plus 4.5 s of backoff
— **49.5 s** — and while it did, nothing read Telegram. Timeouts are now split
and configurable: `http_connect_timeout_sec` (5), `http_read_timeout_sec` (10),
`http_retries` (2), giving a worst case near 11 s; a `Retry-After` on a 429 is
capped at 5 s so the exchange cannot park the engine. Order requests are still
never retried. `/health` reports timings, the slowest operation, API timeout and
Telegram error counts, and flags anything over `slow_command_ms` /
`slow_cycle_ms` / `slow_http_ms`.

The open position is also scanned incrementally now. It used to re-download
every 1-minute candle since the entry on *every* cycle — about 2,900 candles in
two paged requests for a 2-day-old trade, growing with its age — and now resumes
from a persisted `scanned_to` cursor, which is one small request per cycle
regardless of how old the position is. A candle that is still forming is never
marked as scanned, so nothing is skipped, and a position from before the upgrade
does one full catch-up pass and is cheap from then on.

## The risk engine

One object, `RiskManager`, owns every limit and all the risk state, and the live
loop and the backtest both drive it — so a limit cannot behave differently in a
test than it does against real prices.

**Before every entry** it must all be clear: not halted, not locked for the day,
inside the daily loss limit, inside the account drawdown limit, under 3 trades and
2 losses today, past the post-loss cooldown, no position already open, the planned
risk fits what is left of today's loss budget, the account can be valued right now,
and the market data is present, complete, fresh and consistent with the ticker.
Any one of those **rejects the trade**. None of them is a warning.

**Position sizing** divides the permitted dollar risk by the cost of one unit being
stopped out — the stop distance plus both taker fees plus a slippage allowance on
both legs. The quantity is then floored, never raised. If the exchange minimum
order or notional would cost more than 0.5% of equity, the trade is **rejected**;
the earlier build raised the quantity instead, which is why a "1.5% risk" trade
could really risk 3%. For paper trading the simulated quantity is allowed to go
below the exchange minimum (`paper_fractional_qty`), so a small account is
simulated at its true risk rather than pushed above it.

**Loss protection** is evaluated every loop on realised **plus unrealised** P&L, so
an open position that is 2% underwater trips the daily limit without waiting for
its exit:

- **2% of the day's starting equity** → close the position, disable entries, lock
  until the next IST day, notify Telegram. A restart does not hand back a fresh
  budget: the day's starting equity is captured once and persisted.
- **5% from the highest equity ever recorded** → close the position, **halt
  permanently**, emergency Telegram alert. It takes `/clearhalt CONFIRM` to lift,
  which re-baselines the peak to the reviewed equity (and keeps the old peak in the
  state file). Clearing a halt does not re-arm trading; that is a separate
  `/paper_on`.

If the account cannot be valued — no price, no candles — the engine **does nothing**
rather than guess: no entry, and no breach action either.

## The trade journal

`trades.csv` holds one row per completed trade with 23 columns: trade id, mode,
symbol, side, strategy, both timestamps in IST and in milliseconds, entry, exit,
quantity, the **initial** stop, the take profit, planned risk, actual P&L, fees,
slippage, realised R multiple, exit reason, market condition, and equity before and
after.

`/performance` and the research report share one metric implementation, and it only
ever reads **finished** rows: a row without a trade id, without a numeric exit
timestamp, without a usable P&L, or with an id already seen is skipped rather than
guessed at. Totals, win rate, gross profit and loss, net P&L after fees, profit
factor, average win and loss, expectancy, maximum drawdown, long vs short, ETH vs
SOL, consecutive wins and losses, daily and weekly results and realised
risk/reward all come from that set and from nothing else.

## Not live trading

`LiveBroker` is still in the file, untouched by this upgrade, and unreachable:
`mode` is checked against an exact whitelist before anything connects, live mode
needs API keys, and real money additionally needs `i_understand_real_money`. None
of those settings were changed and nothing in the paper path reads them.

Paper, backtest and research mode build the exchange client with
`public_only=True`. Such a client **does not load your API key or secret at all**,
and raises `ReadOnlyClient` on any signed request or any method other than GET — so
no order, leverage change or cancellation can leave that code path even if a future
edit wires one up by mistake. `PaperBroker` has no order methods to call, and a
test drives a whole trade cycle through a client that fails the suite if anything
order-shaped is so much as looked up.

If you ever do want the testnet, that is `"mode": "live"` with testnet keys and
`use_testnet` left `true` — a deliberate, separate decision, outside what this
build is for.

`mode` is checked against an exact whitelist (`backtest`, `research`, `paper`,
`live`) before anything connects. A misspelling, a capital letter, an empty value
or a missing one is a hard stop. It used to fall through to the live-order branch.

## Paper-mode candle timing (read this before trusting a paper result)

The simulator checks open positions against 1-minute candles. A 1m candle gives
the open, high, low and close of the minute but **not the order they happened
in**. If the bot entered at 10:30:45, the low of the 10:30 candle may have
printed at 10:30:05, forty seconds before the position existed. OHLC data cannot
distinguish the two, and nothing recovers the ordering after the fact.

So the simulator only evaluates candles that lie **entirely after the entry**:
the first candle considered is the next minute boundary at or after the fill. An
entry landing exactly on a boundary makes that whole candle post-entry, so it
counts.

The limitation this leaves, stated plainly:

- Up to 59 seconds of real exposure straight after entry is not simulated. A
  stop or target touched only inside that window, and not afterwards, is missed.
- The alternative - judging the entry candle in full - invents losses that never
  happened. That was the previous behaviour and it produced phantom stop-outs on
  the very wick that generated the entry signal, which is the worse error.
- When one candle touches both the stop and the target, the stop is assumed.
- A candle that *opens* beyond the stop fills at the open, not at the stop level,
  because a stop is a market order. A candle that opens better than the target
  still fills at exactly the target - a favourable gap is never credited.
- Every fill is then moved **against** the trade by `slippage_pct` (0.03% a side by
  default): the entry, the stop and the take profit alike, because all three would
  be market orders. Favourable slippage is never simulated.

Paper results are therefore a close approximation, not a replay. Treat a paper
P&L as accurate to within roughly a fee's width per trade, not to the cent.

## Files it creates

`bot_state_paper.json` (memory between restarts), `trades.csv` (the permanent
journal), `engine.lock` (the single-instance heartbeat), `backtest_trades.csv`,
`research_report.txt`. All are in `.gitignore`.

## Tests

```
python -m unittest -v test_trading_bot test_telegram_setup test_risk_controls test_commands test_journal test_research test_performance
```

405 tests, all offline: a fake exchange serving candles from memory, a fake
Telegram, throwaway state files. No credentials, no network, no orders.

- `test_trading_bot` — mode safety, paper fills, sizing, circuit breakers,
  persistence, the entry pipeline, network failures, Telegram transport, the
  backtest, output encoding.
- `test_risk_controls` — the 0.5% cap across a sweep of stops and balances,
  rejection above it, that the exchange minimum is rejected and never bumped,
  fees and slippage direction, stop execution and the refusal to widen a stop,
  the 2% daily lockout (including from an unrealised loss alone), the 5% drawdown
  halt and its confirmation workflow, trade and loss limits, the post-loss
  cooldown, the single-position rule, failing closed on stale/malformed/missing
  data, restart recovery, the engine lock, and a whole trade cycle driven through
  a client that fails the suite if anything order-shaped is looked up.
- `test_commands` — authorisation, silent rejection of other chat ids, duplicate
  suppression across a restart, every command in `/help`, the reset confirmation
  workflow, and that no reply or scrubbed exception contains a secret.
- `test_journal` — row completeness against the declared columns, header
  handling, that an older journal is renamed and never deleted, that duplicated,
  unfinished and unparseable rows are excluded, and the metric values themselves
  against hand-computed answers.
- `test_research` — that the in-sample and out-of-sample windows do not overlap,
  that each window and each walk-forward fold starts from the same balance, that
  costs are charged, that the risk engine is live during a replay, and that the
  report states its own limitations.
- `test_performance` — a fake exchange that parks inside a market-data call, to
  prove `/risk` and every other read-only command still answer while a trading
  cycle is stuck in it; that `/status` starts no cycle and makes no request;
  that `/halt` and `/paper_off` stay prompt; that the state lock is not held
  across network calls and nothing deadlocks under concurrent commands; that
  only one Telegram poller ever runs; that cached data expires correctly and can
  never open a position; that the incremental scan misses no candle; that
  timeouts are bounded and retries counted; and that every risk control still
  fires with all of it in place.

`test_telegram_setup` covers `telegram_setup.py` with a scripted fake
`api.telegram.org`: token shape and masking, the 401/404/409/429/5xx and
timeout/DNS diagnoses, chat-id discovery (and that group chats are rejected),
the test message, and the config write — including that Binance credentials,
`mode` and the risk settings survive untouched, that a corrupt `config.json` is
refused rather than overwritten, that a failed validation writes nothing, and
that no secret is ever printed.

## Never commit these

`config.json`, `config.json.bak`, `bot_state_*.json`, `trades.csv`,
`engine.lock`, `backtest_trades.csv` and `research_report.txt` are all in
`.gitignore`. Only `config.example.json`, which holds placeholders, is meant to be
committed. Your Telegram token currently sits in `config.json` in plain text; if
you would rather it did not, delete it from the file and set `TELEGRAM_TOKEN` and
`TELEGRAM_CHAT_ID` in the environment instead — the loader prefers those.

## Limits to know

- Position sizing budgets the loss **including both fees and a slippage
  allowance**, so `risk_per_trade_pct` is the real cost of being stopped out. The
  one thing that can still exceed it is a gap straight through the stop, which no
  position size prevents — and when that happens the journal records the planned
  risk and the larger actual loss side by side.
- **Two per-trade caps apply and the stricter one always wins.**
  `risk_per_trade_pct` (0.5%) is the normal risk target and scales with the
  account; `max_risk_usdt_abs` (1.00 USDT) is an absolute ceiling on the planned
  loss of any single position. Below ~200 USDT of equity the percentage is
  stricter and sets the size; above it the absolute ceiling takes over, so the
  planned loss on one trade never exceeds 1.00 USDT however large the account
  grows. `/risk` prints both and says which one is binding. `max_risk_usdt_abs`
  is itself capped by `RISK_USDT_HARD_CAP` (5.00 USDT) in code, so no config
  edit can lift it far. A missing or unusable value **rejects the trade** rather
  than defaulting to "no ceiling".
- Sizing works from the **projected fill**, not the quote the decision was made
  on. Entry slippage always moves the fill away from the stop, so sizing on the
  quote understates the real stop distance — by about 7% of the risk at the
  tightest allowed stop. After the broker fills, the engine re-prices the
  position from the actual fill and discards it if the cap is breached.
- `slippage_pct` is a flat allowance, not a measured order-book impact. On a thin
  book or a fast move the real figure would be worse.
- Funding is not modelled anywhere, in paper or in the reports. On a
  trend-following perpetual strategy it is a real, persistent cost.
- The backtest enters at the open of the candle *after* the signal candle, so the
  whole management candle is post-entry. The 1-minute ambiguity described above
  affects paper mode only.
- Market entries pay taker fees (~0.05% each side). On a ~$22 account at 0.5%
  risk, fees and slippage are a large share of every trade — the research report
  prints both totals so you can see it.
- The daily loss and drawdown gates are evaluated on realised **plus unrealised**
  equity, every loop. The **monthly** gate is the one legacy exception: it still
  looks at equity against the month's starting balance and is only a gate on new
  entries, not an emergency close.
- Paper mode does not model margin or liquidation. With stops capped at 3% and
  effective leverage at 5x, the stop is always reached long before liquidation
  would be — if you raise `max_stop_pct` or `max_leverage` that stops being true.
- `max_open_positions` must be 1. The engine stores one position and the config
  refuses anything else rather than pretending to support more.
- A position opened by the earlier build is restored and managed correctly, but it
  has no recorded planned risk, so its journal row shows none and its R multiple is
  blank. Reports say so rather than inventing a number.
- Binance only (CoinDCX has a different API).
- **Some Indian ISPs resolve `binance.com` to a block page.** If
  `fapi.binance.com`, `api.binance.com` and `demo-fapi.binance.com` all resolve
  to the same single address and connections time out, that is what is happening,
  and no mode will work until you run the bot on a network or VPS that can reach
  the API. Check with:
  `python -c "import socket;print(socket.gethostbyname('fapi.binance.com'))"`
- The code is tested against simulated markets and a fake exchange. The tests
  prove the **risk machinery** behaves as described. They say nothing whatever
  about whether the strategy makes money, and a passing suite is not evidence of
  an edge.
- A backtest or a paper run of a few dozen trades is far too small a sample to
  distinguish a real edge from luck. Do not change the strategy because of one
  losing trade, or one losing fold: document the change, test it on its own,
  and compare it against the current `research_report.txt` on the same windows.
- Not financial advice. Past results don't predict future ones.
