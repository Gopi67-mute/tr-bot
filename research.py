#!/usr/bin/env python3
"""Strategy validation reporting: in-sample, out-of-sample and walk-forward.

Run with:   python trading_bot.py --mode research

What this module is for
-----------------------
A backtest that makes money is not evidence that a strategy works. It is one
sample of one parameter set on one slice of history, and the person who chose
the slice and the parameters is the person reading the result. This module
exists to make that honest:

  * The history is split chronologically. The earlier part is the part you are
    allowed to look at; the later part is held back and reported separately.
    Nothing is fitted on the held-back part, because nothing is fitted at all -
    the parameters come from the config file and are never searched here. That
    removes the most common source of overfitting rather than measuring it.
  * A walk-forward pass replays the same fixed rules over consecutive windows,
    so you can see whether the result is spread across the period or comes from
    one lucky stretch.
  * Every figure includes taker fees and the configured slippage allowance, and
    the risk engine (daily loss lock, drawdown halt, trades per day) is active,
    so the numbers describe the system you would actually run.
  * Longs and shorts, and each symbol, are reported separately, as are results
    grouped by the recorded market condition.

How look-ahead is avoided
-------------------------
`backtest_symbol` takes its signal from closed candle i and fills at the open
of candle i+1; the 4h trend index only ever advances to candles that closed at
or before the current 1h candle's close; management uses candle ranges from the
entry candle onwards. The 4h warm-up candles needed by the 200 EMA are fetched
from *before* the test window, so no EMA value is informed by a future bar, and
a window that cannot warm its indicators is reported as such rather than
silently producing zero trades.

What it still cannot tell you
-----------------------------
Funding payments are not modelled. Slippage is a flat per-side allowance, not a
measured book impact. Candle ranges do not record the order of the high and the
low, so a candle that touches both the stop and the target is scored as a loss.
Sample sizes on a few months of 1h data are small - tens of trades - which is
far too few to separate skill from noise. Treat every number here as a
description of the past, not a forecast.
"""

import os

import trading_bot as tb


def _metric_rows(trades, symbol=None):
    """Normalise backtest trades into the shape performance_stats() expects."""
    rows = []
    for t in trades:
        rows.append({
            "trade_id": t.get("trade_id") or tb.new_trade_id(),
            "symbol": symbol or t.get("symbol") or "?",
            "side": t["side"],
            "closed_ms": t.get("closed_ms"),
            "closed_ist": t.get("closed_ist") or t.get("closed") or "",
            "entry": t["entry"], "exit": t["exit"], "qty": t["qty"],
            "initial_stop": t.get("initial_stop", t.get("stop")),
            "take_profit": t.get("target"),
            "planned_risk": t.get("planned_risk") or 0.0,
            "pnl": t["pnl"], "fees": t.get("fees") or 0.0,
            "slippage": t.get("slippage") or 0.0,
            "r_multiple": ((t["pnl"] / t["planned_risk"])
                           if t.get("planned_risk") else None),
            "exit_reason": t.get("exit_reason") or "?",
            "market_condition": t.get("market_condition") or "",
            "equity_before": t.get("equity_before"),
            "equity_after": t.get("equity_after"),
        })
    return rows


def window(candles, lo_ms, hi_ms):
    return [c for c in candles if lo_ms <= c["t"] and c["T"] < hi_ms]


def split_index(candles, fraction):
    """The timestamp that divides `candles` into (1-fraction, fraction)."""
    if not candles:
        return None
    cut = int(len(candles) * (1 - fraction))
    cut = max(1, min(cut, len(candles) - 1))
    return candles[cut]["t"]


def evaluate(cfg, c1, c4, rules, symbol, start_balance=None):
    """Run one replay and return (result, stats). Empty windows are safe."""
    if len(c1) < max(cfg["slow_ema"], cfg["trend_ema"] // 4) + 5:
        return None, None
    r = tb.backtest_symbol(cfg, c1, c4, {**rules, "symbol": symbol},
                           fractional=True, start_balance=start_balance)
    stats = tb.performance_stats(_metric_rows(r["trades"], symbol),
                                 start_equity=r["start"])
    return r, stats


def _headline(stats, result=None):
    if not stats or not stats["sample_size"]:
        return "no trades"
    out = (f"{stats['trades']} trades | win {stats['win_rate']:.0f}% | "
           f"net {stats['net_pnl']:+.3f} USDT | PF {tb._pf(stats['profit_factor'])} | "
           f"exp {stats['expectancy']:+.4f}/trade | maxDD "
           f"{stats['max_drawdown_pct']:.1f}% | avg {stats['avg_r']:+.2f}R")
    if result and result.get("halted"):
        out += " | HALTED mid-run"
    return out


def walk_forward(cfg, c1, c4, rules, symbol, folds):
    """Replay the same fixed rules over `folds` consecutive equal windows.

    Each fold starts from the configured starting balance, so one fold's luck
    cannot compound into the next and inflate the series.
    """
    out = []
    if not c1 or folds < 2:
        return out
    size = len(c1) // folds
    if size < max(cfg["slow_ema"], cfg["swing_lookback"]) + 5:
        return out
    for k in range(folds):
        part = c1[k * size: (k + 1) * size if k < folds - 1 else len(c1)]
        if not part:
            continue
        lo, hi = part[0]["t"], part[-1]["T"] + 1
        r, stats = evaluate(cfg, part, [c for c in c4 if c["T"] < hi], rules, symbol)
        out.append({
            "fold": k + 1,
            "from": tb.ist(lo).strftime("%Y-%m-%d"),
            "to": tb.ist(hi).strftime("%Y-%m-%d"),
            "result": r, "stats": stats})
    return out


def report_lines(cfg, data):
    """Build the whole report as a list of lines. `data` is per-symbol results."""
    L = [
        "=" * 72,
        "STRATEGY VALIDATION REPORT (paper simulation)",
        "=" * 72,
        f"Generated: {tb.ist().strftime('%Y-%m-%d %H:%M')} IST",
        f"Strategy: {tb.STRATEGY_NAME} - "
        f"{cfg['trend_interval']}/{cfg['entry_interval']}, "
        f"EMA {cfg['trend_ema']}/{cfg['fast_ema']}/{cfg['slow_ema']}, "
        f"swing {cfg['swing_lookback']}, RR {cfg['reward_risk']}",
        f"Risk: {cfg['risk_per_trade_pct']}%/trade, {cfg['max_daily_loss_pct']}%/day, "
        f"{cfg['max_account_drawdown_pct']}% max drawdown, "
        f"{cfg['max_trades_per_day']} trades/day, 1 position",
        f"Costs: {cfg['taker_fee_pct']}% taker fee per side, "
        f"{cfg['slippage_pct']}% slippage allowance per side",
        f"History: last {cfg['backtest_days']} days | out-of-sample share: "
        f"{cfg['oos_fraction'] * 100:.0f}% | walk-forward folds: "
        f"{cfg['walk_forward_folds']}",
        "",
        "No parameter is searched or optimised in this report. The rules come",
        "from config.json exactly as the live engine reads them, so the",
        "out-of-sample section is a genuine hold-out and not a tuning target.",
        "",
    ]
    all_rows = []
    for sym, d in data.items():
        L.append("-" * 72)
        L.append(f"{sym}")
        L.append("-" * 72)
        if d.get("error"):
            L.append(f"  Not evaluated: {d['error']}")
            continue
        L.append(f"  Full period    : {_headline(d['full'][1], d['full'][0])}")
        L.append(f"  In-sample  (IS): {_headline(d['is'][1], d['is'][0])}"
                 f"   [{d['is_from']} .. {d['is_to']}]")
        L.append(f"  Out-of-sample  : {_headline(d['oos'][1], d['oos'][0])}"
                 f"   [{d['oos_from']} .. {d['oos_to']}]")
        is_s, oos_s = d["is"][1], d["oos"][1]
        if is_s and oos_s and is_s["sample_size"] and oos_s["sample_size"]:
            L.append(f"  IS vs OOS expectancy: {is_s['expectancy']:+.4f} vs "
                     f"{oos_s['expectancy']:+.4f} USDT/trade"
                     + ("  <-- out-of-sample is worse"
                        if oos_s["expectancy"] < is_s["expectancy"] else ""))
        elif oos_s is not None and not oos_s["sample_size"]:
            L.append("  IS vs OOS: the hold-out produced no trades, so it says "
                     "nothing either way")
        wf = d.get("wf") or []
        if wf:
            L.append("  Walk-forward (each fold starts from the same balance):")
            for f in wf:
                L.append(f"    fold {f['fold']} {f['from']}..{f['to']}: "
                         f"{_headline(f['stats'], f['result'])}")
            pos = sum(1 for f in wf if f["stats"] and f["stats"]["net_pnl"] > 0)
            traded = sum(1 for f in wf if f["stats"] and f["stats"]["sample_size"])
            L.append(f"    folds profitable: {pos}/{len(wf)} "
                     f"({traded} fold(s) took any trade at all)")
        else:
            L.append("  Walk-forward: not enough candles per fold to run it")

        st = d["full"][1]
        r = d["full"][0]
        if st and st["sample_size"]:
            L.append("  Breakdown (full period):")
            for side in ("LONG", "SHORT"):
                b = st["by_side"][side]
                L.append(f"    {side:<5}: {b['trades']} trades, win "
                         f"{b['win_rate']:.0f}%, net {b['net_pnl']:+.3f}, PF "
                         f"{tb._pf(b['profit_factor'])}, exp {b['expectancy']:+.4f}"
                         if b["trades"] else f"    {side:<5}: no trades")
            for cond, b in sorted(st["by_condition"].items()):
                L.append(f"    cond {cond:<16}: {b['trades']} trades, "
                         f"net {b['net_pnl']:+.3f}")
            for reason, b in sorted(st["by_reason"].items()):
                L.append(f"    exit {reason:<16}: {b['trades']} trades, "
                         f"net {b['net_pnl']:+.3f}")
            L.append(f"    fees {st['total_fees']:.3f} | slippage "
                     f"{st['total_slippage']:.3f} | planned risk deployed "
                     f"{st['planned_risk_total']:.3f} USDT")
            L.append(f"    streaks {st['max_consecutive_wins']}W/"
                     f"{st['max_consecutive_losses']}L | "
                     f"max drawdown {st['max_drawdown']:.3f} USDT "
                     f"({st['max_drawdown_pct']:.2f}%)")
            L.append(f"    skipped - too small {r['skipped_small']}, stop rules "
                     f"{r['skipped_rules']}, risk limits {r['skipped_risk']}")
            all_rows.extend(_metric_rows(r["trades"], sym))
        L.append("")

    if all_rows:
        combined = tb.performance_stats(all_rows,
                                        start_equity=cfg["paper_start_balance"])
        L.append("=" * 72)
        L.append("COMBINED (both symbols, full period, trades interleaved by exit time)")
        L.append("=" * 72)
        L.append(tb.format_performance(combined, title="Combined"))
        L.append("")

    n = sum(len((d.get("full") or (None, None))[1]["equity_curve"]) - 1
            for d in data.values()
            if d.get("full") and d["full"][1])
    L += [
        "=" * 72,
        "HOW TO READ THIS",
        "=" * 72,
        f"Total closed trades across the report: {n}.",
        "",
        "* A sample of this size cannot establish an edge. With ~30 trades, a",
        "  win rate measured at 50% has a 95% interval of roughly 32-68%, which",
        "  spans 'clearly profitable' and 'clearly not' for a 2:1 reward/risk.",
        "* Out-of-sample worse than in-sample is the normal case, not a bug. It",
        "  only becomes informative when the out-of-sample set is large enough.",
        "* Funding is not modelled; on a trend-following perpetual strategy it",
        "  is a real and persistent cost.",
        "* A candle touching both the stop and the target is scored as a loss,",
        "  which biases these results pessimistically. The 1-minute entry-window",
        "  limitation described in the README biases paper trading the other way.",
        "* Nothing here is adjusted after the fact. Do not change the strategy on",
        "  the back of one losing trade, or one losing fold: document a proposed",
        "  change, test it independently, and compare it against this baseline",
        "  on the same windows before adopting it.",
        "",
        "This is a paper-trading simulator. No order was placed to produce this.",
    ]
    return L


def run_research(cfg, client):
    """Download history once per symbol, then report every validation view."""
    rules = client.rules(cfg["symbols"])
    end = tb.now_ms()
    start = end - cfg["backtest_days"] * 86400000
    warm4 = (cfg["trend_ema"] + 10) * tb.interval_ms(cfg["trend_interval"])
    data = {}
    for sym in cfg["symbols"]:
        tb.say(f"Downloading {sym} candles ...")
        try:
            c1 = [c for c in client.klines_range(sym, cfg["entry_interval"], start, end)
                  if c["T"] < end]
            c4 = [c for c in client.klines_range(sym, cfg["trend_interval"],
                                                 start - warm4, end) if c["T"] < end]
        except Exception as e:
            data[sym] = {"error": f"{type(e).__name__}: {e}"}
            continue
        if not c1:
            data[sym] = {"error": "no candles returned for this window"}
            continue
        cut = split_index(c1, cfg["oos_fraction"])
        is1 = [c for c in c1 if c["t"] < cut]
        oos1 = [c for c in c1 if c["t"] >= cut]
        entry = {
            "full": evaluate(cfg, c1, c4, rules[sym], sym),
            "is": evaluate(cfg, is1, [c for c in c4 if c["T"] < cut], rules[sym], sym),
            "oos": evaluate(cfg, oos1, c4, rules[sym], sym),
            "wf": walk_forward(cfg, c1, c4, rules[sym], sym,
                               int(cfg["walk_forward_folds"])),
            "is_from": tb.ist(is1[0]["t"]).strftime("%Y-%m-%d") if is1 else "-",
            "is_to": tb.ist(is1[-1]["T"]).strftime("%Y-%m-%d") if is1 else "-",
            "oos_from": tb.ist(oos1[0]["t"]).strftime("%Y-%m-%d") if oos1 else "-",
            "oos_to": tb.ist(oos1[-1]["T"]).strftime("%Y-%m-%d") if oos1 else "-",
        }
        for key in ("full", "is", "oos"):
            if entry[key] == (None, None):
                entry[key] = (None, None)
        data[sym] = entry

    lines = report_lines(cfg, data)
    text = "\n".join(lines)
    tb.say("\n" + text)
    path = os.path.join(tb.HERE, "research_report.txt")
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(text + "\n")
        tb.say(f"\nReport written to {path}")
    except OSError as e:
        tb.say(f"(warning: could not write {path}: {e})")
    return data
