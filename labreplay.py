#!/usr/bin/env python3
"""The simulator that judges a candidate rule set, and the statistics it reports.

Research-only: no orders, no account reads, no paper state.

The honesty rules this simulator is built around
------------------------------------------------
1. **Signal on a closed bar, fill on the next open.** A signal is read from
   15m bar i, which has closed; the fill is the open of bar i+1, worsened by
   the configured slippage allowance. Nothing is ever filled at a price inside
   the bar that produced the signal.

2. **A bar that touches both the stop and the target is a loss.** OHLC data
   does not record whether the high or the low came first, and no arithmetic
   recovers it. Guessing favourably is how a backtest manufactures an edge, so
   the simulator always guesses against itself - and then counts how often it
   had to, and reports what the result would have been had every one of those
   bars gone the other way. The truth is inside that bracket.

3. **A gap through the stop fills at the open.** A stop is a market order. If
   a bar opened beyond it, the fill is the open, not the stop level. Targets
   get no matching benefit: a bar that gaps past the target still fills at the
   target exactly.

4. **Every cost is charged.** Two taker fees, a slippage allowance on both
   legs, and funding on every 8-hourly settlement the position was open
   across.

5. **The real risk engine gates it.** `trading_bot.RiskManager` decides when
   the replay may enter, using the limits in config.json. The daily lock, the
   drawdown halt, the monthly stop, the loss cooldown and the trade counters
   are all live during a replay, so the trade count a report shows is the trade
   count the engine would have permitted.

6. **One account, not one per symbol.** The replay walks a single shared 15m
   timeline across every symbol, holding one balance, one `RiskManager` and at
   most `max_open_positions` positions - which is what the live engine does.
   Replaying each symbol into its own $22 account and then merging the trade
   lists, as an earlier version of this file did, is wrong in three ways at
   once: it doubles the number of trades the engine would really have taken
   (the second symbol's signal is not skipped while the first is in a
   position), it doubles the risk deployed at any moment, and it applies the
   daily-loss and drawdown limits per symbol instead of per account, so the
   reported drawdown can exceed the limit that was supposedly enforcing it.
   For per-symbol attribution, filter the resulting trades by symbol; for "how
   would this symbol do alone", replay it alone.
"""

import math
import os

import indicators as ind
import labdata
import strategy_lab as lab
import trading_bot as tb


# ---------------------------------------------------------------- replay

def _signal(cfg, p, S, i, rules, bal, risk, fee, slip, fractional, note):
    """Everything between "a bar closed" and "open this position", or None.

    Kept apart from the loop so the order of the gates is visible in one place,
    and so the loop body does not decide anything. Each gate reports its own
    reason through `note`, which is what lets a run show which filters are
    load-bearing and which are decoration.
    """
    e = S.e
    side, why = lab.trend_view(S, p, S.ti[i])
    if side is None:
        return note(why)
    ok, why = lab.confirm_view(S, p, S.ci[i], side)
    if not ok:
        return note(why)
    why = lab.volatility_block(S, p, i)
    if why:
        return note(why)
    ok, why = lab.entry_trigger(S, p, i, side)
    if not ok:
        return note(why)
    why = lab.funding_block(S, p, i, side)
    if why:
        return note(why)
    stop = lab.stop_level(S, p, i, side)
    if stop is None:
        return note("no stop could be computed")

    raw_entry = e[i + 1]["o"]
    why = tb.check_stop(side, raw_entry, stop, cfg)
    if why:
        return note("stop rules: " + why)
    entry = tb.adverse_fill(side, raw_entry, slip, entering=True)
    if (side == "LONG" and stop >= entry) or (side == "SHORT" and stop <= entry):
        return note("slipped fill is already past the stop")
    target = lab.net_target(side, entry, stop, p["rr_net"], fee, slip)
    why = lab.room_block(S, p, i, side, entry, target)
    if why:
        return note("no room: " + why)
    qty, planned, msg = tb.size_position(bal, entry, stop, rules, cfg,
                                         fractional=fractional)
    if qty <= 0:
        return note("sizing rejected: " + msg)
    if planned > risk.max_risk_usdt(bal) + 1e-9:
        return note("planned risk exceeds the per-trade cap")
    if planned > risk.remaining_daily_loss(bal) + 1e-9:
        return note("planned risk does not fit today's remaining loss budget")
    return {"side": side, "entry": entry, "stop": stop, "target": target,
            "qty": qty, "planned_risk": planned, "raw_entry": raw_entry}


def replay(cfg, p, series, rules, lo_ms, hi_ms, start_balance=None,
           fractional=True, collect_rejections=False):
    """Replay the candidate as ONE account over every symbol in `series`.

    `series` is {symbol: strategy_lab.Series}; `rules` is {symbol: exchange
    rules}. All symbols are walked on one shared 15m timeline, so a signal on
    the second symbol is skipped while the first is in a position - exactly as
    `Bot.look_for_entry` does, which returns after opening one position.

    Each Series must cover the window plus indicator warm-up. Reusing one set
    of Series across several windows is what makes walk-forward cheap and
    guarantees every window sees bit-identical indicator values.
    """
    fee = cfg["taker_fee_pct"]
    slip = cfg.get("slippage_pct", 0.0)
    bal = start = float(cfg["paper_start_balance"] if start_balance is None
                        else start_balance)
    peak, max_dd = bal, 0.0
    max_open = int(cfg["max_open_positions"])
    state = {}
    risk = tb.RiskManager(cfg, state)
    syms = list(series)

    # One timeline. Symbols listed on the same exchange share the 15m grid, but
    # the union is taken rather than assumed so a symbol with a shorter history
    # simply contributes no bars where it has none.
    grid = sorted({c["T"] for s in syms for c in series[s].e
                   if lo_ms <= c["T"] < hi_ms})
    if len(grid) < 2:
        return _empty(start)
    at = {s: {c["T"]: i for i, c in enumerate(series[s].e)} for s in syms}
    risk.roll(bal, ms=grid[0], force=True)

    trades, open_pos = [], []
    rej = {}
    ambiguous = 0            # bars where stop and target were both touched
    ambiguous_gain = 0.0     # what resolving every one of them favourably adds
    equity_curve = [bal]

    def note(reason):
        if collect_rejections:
            key = reason.split(" (")[0][:60]
            rej[key] = rej.get(key, 0) + 1
        return None

    for close_ms in grid[:-1]:
        risk.roll(bal, ms=close_ms)

        # ---- manage every open position on this bar's range ---------------
        still = []
        for pos in open_pos:
            S = series[pos["symbol"]]
            i = at[pos["symbol"]].get(close_ms)
            if i is None:
                still.append(pos)
                continue
            bar, side = S.e[i], pos["side"]
            if side == "LONG":
                hit_stop, hit_tgt = bar["l"] <= pos["stop"], bar["h"] >= pos["target"]
            else:
                hit_stop, hit_tgt = bar["h"] >= pos["stop"], bar["l"] <= pos["target"]
            if hit_stop and hit_tgt:
                ambiguous += 1
            if not (hit_stop or hit_tgt):
                _advance_stop(p, S, pos, i, bar)
                still.append(pos)
                continue
            raw = (tb.stop_fill_price(side, pos["stop"], bar["o"])
                   if hit_stop else pos["target"])
            reason = "stop loss" if hit_stop else "take profit"
            rec, bal = _book(cfg, S, pos, raw, reason, bal, bar["T"], fee, slip)
            if hit_stop and hit_tgt:
                alt, _ = _book(cfg, S, pos, pos["target"], "take profit",
                               bal - rec["pnl"], bar["T"], fee, slip)
                ambiguous_gain += alt["pnl"] - rec["pnl"]
            trades.append(rec)
            equity_curve.append(bal)
            risk.record_close(rec["pnl"], bar["T"])
            peak = max(peak, bal)
            max_dd = max(max_dd, (peak - bal) / peak * 100 if peak > 0 else 0.0)
            breach = risk.breach(bal)
            if breach:
                if breach[0] == "daily":
                    risk.lock_day(bar["T"])
                else:
                    risk.halt(breach[1], bar["T"])
        open_pos = still
        if len(open_pos) >= max_open:
            note(f"already in {len(open_pos)} position(s)")
            continue

        # ---- look for an entry, first symbol that qualifies ---------------
        blocks = risk.entry_blocks(bal, ms=close_ms)
        if blocks:
            note("risk limit: " + blocks[0])
            continue
        for sym in syms:
            S = series[sym]
            i = at[sym].get(close_ms)
            if i is None or i + 1 >= len(S.e):
                continue
            sig = _signal(cfg, p, S, i, rules[sym], bal, risk, fee, slip,
                          fractional, note)
            if not sig:
                continue
            risk.record_open(S.e[i + 1]["t"])
            open_pos.append({
                "trade_id": tb.new_trade_id(), "symbol": sym, "side": sig["side"],
                "entry": sig["entry"], "stop": sig["stop"],
                "initial_stop": sig["stop"], "target": sig["target"],
                "qty": sig["qty"], "planned_risk": sig["planned_risk"],
                "strategy": lab.STRATEGY_NAME,
                "regime": lab.regime_at(S, i),
                "market_condition": lab.regime_at(S, i),
                "entry_slippage": abs(sig["entry"] - sig["raw_entry"]) * sig["qty"],
                "equity_before": bal, "best": sig["entry"],
                "opened_ms": S.e[i + 1]["t"],
                "opened": tb.ist(S.e[i + 1]["t"]).strftime("%Y-%m-%d %H:%M")})
            break

    wins = [t for t in trades if t["pnl"] > 0]
    return {"start": start, "end": bal, "trades": trades, "wins": len(wins),
            "fees": sum(t["fees"] for t in trades),
            "slippage": sum(t["slippage"] for t in trades),
            "funding": sum(t["funding"] for t in trades),
            "max_dd": max_dd, "open": list(open_pos), "rejections": rej,
            "ambiguous_bars": ambiguous, "ambiguous_gain": ambiguous_gain,
            "halted": bool(state.get("halted")),
            "halt_reason": state.get("halt_reason", ""),
            "equity_curve": equity_curve}


def _empty(start):
    return {"start": start, "end": start, "trades": [], "wins": 0, "fees": 0.0,
            "slippage": 0.0, "funding": 0.0, "max_dd": 0.0, "open": [],
            "rejections": {}, "ambiguous_bars": 0, "ambiguous_gain": 0.0,
            "halted": False, "halt_reason": "", "equity_curve": [start]}


def _book(cfg, S, pos, raw_exit, reason, bal, closed_ms, fee, slip):
    """Close `pos` at `raw_exit` and return (trade row, new balance)."""
    exit_px = tb.adverse_fill(pos["side"], raw_exit, slip, entering=False)
    pnl, fees = tb.trade_pnl(pos["side"], pos["entry"], exit_px, pos["qty"], fee)
    fund = labdata.funding_cost(S.funding, pos["side"], pos["qty"], pos["entry"],
                                pos["opened_ms"], closed_ms)
    pnl -= fund
    slip_cost = pos["entry_slippage"] + abs(exit_px - raw_exit) * pos["qty"]
    row = {**pos, "exit": exit_px, "raw_exit": raw_exit, "pnl": pnl, "fees": fees,
           "funding": fund, "slippage": slip_cost, "exit_reason": reason,
           "closed_ms": closed_ms,
           "closed_ist": tb.ist(closed_ms).strftime("%Y-%m-%d %H:%M:%S"),
           "hold_hours": (closed_ms - pos["opened_ms"]) / 3_600_000.0,
           "equity_after": bal + pnl}
    row.pop("best", None)
    return row, bal + pnl


def _advance_stop(p, S, pos, i, bar):
    """Trailing / break-even management. Can only ever reduce risk.

    Every move goes through `trading_bot.tighten_stop`, which refuses a stop
    that would increase the distance to the entry. That is not a style
    preference: a widened stop is how a fixed-risk system loses several times
    its planned risk on one trade, so the guard is structural rather than left
    to the caller.
    """
    mode = p["trail"]
    if mode == "off":
        return
    side, entry = pos["side"], pos["entry"]
    risk_dist = abs(entry - pos["initial_stop"])
    if risk_dist <= 0:
        return
    pos["best"] = (max(pos.get("best", entry), bar["h"]) if side == "LONG"
                   else min(pos.get("best", entry), bar["l"]))
    moved = (pos["best"] - entry) if side == "LONG" else (entry - pos["best"])
    if moved < p["trail_trigger_r"] * risk_dist:
        return
    if mode == "breakeven":
        want = entry
    elif mode == "atr":
        atr = S.e_atr[i] or 0.0
        if atr <= 0:
            return
        want = (pos["best"] - p["trail_atr"] * atr if side == "LONG"
                else pos["best"] + p["trail_atr"] * atr)
    else:
        return
    tb.tighten_stop(pos, want)


# ---------------------------------------------------------------- statistics

def stats(trades, start_equity):
    """Everything the final report quotes, computed from the trade list alone."""
    n = len(trades)
    out = {"trades": n, "wins": 0, "losses": 0, "win_rate": 0.0, "net_pnl": 0.0,
           "gross_profit": 0.0, "gross_loss": 0.0, "profit_factor": None,
           "expectancy": 0.0, "avg_win": 0.0, "avg_loss": 0.0, "avg_r": 0.0,
           "max_drawdown": 0.0, "max_drawdown_pct": 0.0, "max_consec_losses": 0,
           "max_consec_wins": 0, "fees": 0.0, "slippage": 0.0, "funding": 0.0,
           "sharpe": None, "avg_hold_hours": 0.0, "return_pct": 0.0,
           "expectancy_r": 0.0}
    if not n:
        return out
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    out["wins"], out["losses"] = len(wins), len(losses)
    out["win_rate"] = len(wins) / n * 100.0
    out["net_pnl"] = sum(t["pnl"] for t in trades)
    out["gross_profit"] = sum(t["pnl"] for t in wins)
    out["gross_loss"] = -sum(t["pnl"] for t in losses)
    out["profit_factor"] = (out["gross_profit"] / out["gross_loss"]
                            if out["gross_loss"] > 0 else None)
    out["expectancy"] = out["net_pnl"] / n
    out["avg_win"] = out["gross_profit"] / len(wins) if wins else 0.0
    out["avg_loss"] = out["gross_loss"] / len(losses) if losses else 0.0
    rs = [t["pnl"] / t["planned_risk"] for t in trades if t.get("planned_risk")]
    out["avg_r"] = sum(rs) / len(rs) if rs else 0.0
    out["expectancy_r"] = out["avg_r"]
    out["fees"] = sum(t["fees"] for t in trades)
    out["slippage"] = sum(t["slippage"] for t in trades)
    out["funding"] = sum(t.get("funding", 0.0) for t in trades)
    out["avg_hold_hours"] = sum(t.get("hold_hours", 0.0) for t in trades) / n

    eq, peak, dd, ddp = start_equity, start_equity, 0.0, 0.0
    for t in sorted(trades, key=lambda x: x["closed_ms"]):
        eq += t["pnl"]
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
        if peak > 0:
            ddp = max(ddp, (peak - eq) / peak * 100.0)
    out["max_drawdown"], out["max_drawdown_pct"] = dd, ddp
    out["return_pct"] = (eq / start_equity - 1) * 100.0 if start_equity else 0.0

    run_w = run_l = 0
    for t in sorted(trades, key=lambda x: x["closed_ms"]):
        if t["pnl"] > 0:
            run_w, run_l = run_w + 1, 0
        else:
            run_l, run_w = run_l + 1, 0
        out["max_consec_wins"] = max(out["max_consec_wins"], run_w)
        out["max_consec_losses"] = max(out["max_consec_losses"], run_l)

    # Sharpe on per-trade R multiples. Quoted only with enough trades for the
    # number to mean anything; below that it is noise with a decimal point.
    if len(rs) >= 30:
        mean = sum(rs) / len(rs)
        var = sum((x - mean) ** 2 for x in rs) / (len(rs) - 1)
        sd = math.sqrt(var)
        if sd > 0:
            out["sharpe"] = mean / sd * math.sqrt(len(rs))
    return out


def subset(trades, **where):
    out = trades
    for k, v in where.items():
        out = [t for t in out if t.get(k) == v]
    return out


def by_key(trades, key, start_equity):
    groups = {}
    for t in trades:
        groups.setdefault(t.get(key, "?"), []).append(t)
    return {k: stats(v, start_equity) for k, v in sorted(groups.items())}


def headline(st, extra=""):
    if not st["trades"]:
        return "no trades"
    pf = "inf" if st["profit_factor"] is None else f"{st['profit_factor']:.2f}"
    s = (f"{st['trades']:>4} trades | win {st['win_rate']:>4.1f}% | "
         f"net {st['net_pnl']:+8.3f} | PF {pf:>5} | "
         f"exp {st['expectancy']:+.4f} ({st['avg_r']:+.2f}R) | "
         f"maxDD {st['max_drawdown_pct']:>4.1f}%")
    if st["sharpe"] is not None:
        s += f" | SR {st['sharpe']:+.2f}"
    return s + (f" | {extra}" if extra else "")


# ---------------------------------------------------------------- windows

def quarters(lo_ms, hi_ms):
    """[(label, lo, hi)] of calendar quarters covering the window, in IST."""
    out, cur = [], lo_ms
    while cur < hi_ms:
        d = tb.ist(cur)
        q = (d.month - 1) // 3
        y, m = d.year, q * 3 + 1
        nm, ny = (m + 3, y) if m + 3 <= 12 else (1, y + 1)
        from datetime import datetime
        nxt = int(datetime(ny, nm, 1, tzinfo=tb.IST).timestamp() * 1000)
        out.append((f"{y}Q{q + 1}", max(cur, lo_ms), min(nxt, hi_ms)))
        cur = nxt
    return out


def splits(lo_ms, hi_ms, oos_fraction):
    cut = lo_ms + int((hi_ms - lo_ms) * (1 - oos_fraction))
    return (lo_ms, cut), (cut, hi_ms)


def folds(lo_ms, hi_ms, n):
    size = (hi_ms - lo_ms) // n
    return [(lo_ms + k * size, lo_ms + (k + 1) * size if k < n - 1 else hi_ms)
            for k in range(n)]
