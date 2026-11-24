#!/usr/bin/env python3
"""The validation report for the candidate strategy.

Research-only: no orders, no account access, no paper-state writes.

Run with:  python labreport.py

This produces `strategy_lab_report.txt`: in-sample, out-of-sample,
walk-forward, per-symbol, per-side, per-regime and cost-sensitivity views of
the candidate, next to the strategy the engine runs today, on identical data
and identical cost assumptions.

Two conventions make the numbers readable
-----------------------------------------
*Deployable* rows run under the real config.json limits, including the 5%
permanent drawdown halt. They answer "what would this system have done".

*Diagnostic* rows lift the daily-loss, monthly and drawdown halts and nothing
else. They answer a different and narrower question: "do the entry and exit
rules have an edge at all?" - which the deployable rows cannot answer, because
the halt fires early in the window and every number after it describes an
account that stopped trading. A diagnostic row is never a proposal; the
drawdowns in that column are what the halt exists to prevent.
"""

import copy
import os

import labdata
import labreplay as rp
import strategy_lab as lab
import trading_bot as tb

DAYS = 1090
WARMUP_DAYS = 60
OOS_FRACTION = 0.35
FOLDS = 6
REPORT = os.path.join(tb.HERE, "strategy_lab_report.txt")

# The candidate, after the ablation in the write-up: the filters that survived
# measurement on a full-history sample, not the ones that sounded prudent.
CANDIDATE = dict(
    t_require_stack=False, t_require_slow=False, t_adx_min=25.0,
    t_require_structure=True, c_adx_min=0.0, c_rsi_min=0.0, c_rsi_max=100.0,
    c_require_close_above_mid=True, confirm_break_prev=False, vol_mult=1.0,
    max_ext_atr=1.5, atr_pct_min=0.10, atr_pct_max=1.20,
    funding_max_pct=0.05, room_mult=0.0, rr_net=2.5,
)


def diagnostic_cfg(cfg):
    """`cfg` with only the halting limits lifted. Research use only."""
    c = copy.deepcopy(cfg)
    c["max_account_drawdown_pct"] = 1e9
    c["monthly_drawdown_stop_pct"] = 1e9
    c["max_daily_loss_pct"] = 1e9
    return c


def _costs(st):
    return st["fees"] + st["slippage"] + st["funding"]


def run(cfg, p, series, rules, lo, hi, start=None):
    r = rp.replay(cfg, p, series, rules, lo, hi, start_balance=start)
    return r, rp.stats(r["trades"], r["start"])


def section(L, title):
    L.append("")
    L.append("-" * 78)
    L.append(title)
    L.append("-" * 78)


def build(cfg, data, series, rules, lo, hi):
    L = []
    diag = diagnostic_cfg(cfg)
    p = lab.params(**CANDIDATE)
    cut = lo + int((hi - lo) * (1 - OOS_FRACTION))

    L += [
        "=" * 78,
        "CANDIDATE STRATEGY VALIDATION  (paper research - no order was placed)",
        "=" * 78,
        f"Generated      : {tb.ist().strftime('%Y-%m-%d %H:%M')} IST",
        f"Candidate      : {lab.STRATEGY_NAME} - "
        f"{p['trend_tf']}/{p['confirm_tf']}/{p['entry_tf']}",
        f"Baseline       : {tb.STRATEGY_NAME} (what the engine runs today)",
        f"Symbols        : {', '.join(series)}",
        f"History        : {tb.ist(lo).strftime('%Y-%m-%d')} .. "
        f"{tb.ist(hi).strftime('%Y-%m-%d')}  ({(hi - lo) / 86400000:.0f} days)",
        f"Data source    : {tb.LIVE_BASE} (real market data; the engine pins this "
        f"host too)",
        f"Costs          : {cfg['taker_fee_pct']}% taker fee/side, "
        f"{cfg['slippage_pct']}% slippage/side, funding charged per settlement",
        f"Risk           : {cfg['risk_per_trade_pct']}%/trade, "
        f"{cfg['max_daily_loss_pct']}%/day, {cfg['max_account_drawdown_pct']}% "
        f"max drawdown (permanent halt), {cfg['max_trades_per_day']} trades/day, "
        f"{cfg['max_open_positions']} position",
        "",
        "Rows marked DEPLOYABLE run under the real limits above, halt included.",
        "Rows marked DIAGNOSTIC lift the daily/monthly/drawdown halts ONLY, to",
        "measure whether the rules have an edge at all. A diagnostic row is not",
        "a proposal and its drawdowns are what the halt exists to prevent.",
    ]

    # ---- headline -------------------------------------------------------
    section(L, "1. HEADLINE - candidate vs the strategy running today")
    dep_r, dep = run(cfg, p, series, rules, lo, hi)
    dia_r, dia = run(diag, p, series, rules, lo, hi)
    L.append(f"  candidate  DEPLOYABLE : {rp.headline(dep)}")
    if dep_r["halted"]:
        last = (tb.ist(dep_r['trades'][-1]['closed_ms']).strftime('%Y-%m-%d')
                if dep_r["trades"] else "-")
        L.append(f"      -> HALTED permanently; last trade {last}, then no trades "
                 f"for the rest of the window.")
        L.append(f"      -> {dep_r['halt_reason'][:100]}")
    L.append(f"  candidate  DIAGNOSTIC : {rp.headline(dia)}")
    L.append(f"      gross before costs {dia['net_pnl'] + _costs(dia):+.3f} USDT | "
             f"costs {_costs(dia):.3f} "
             f"(fees {dia['fees']:.3f}, slip {dia['slippage']:.3f}, "
             f"funding {dia['funding']:+.3f})")

    for label, c in (("DEPLOYABLE", cfg), ("DIAGNOSTIC", diag)):
        for sym in series:
            c1 = [x for x in data[sym]["1h"] if lo <= x["t"] and x["T"] < hi]
            c4 = [x for x in data[sym]["4h"] if x["T"] < hi]
            b = tb.backtest_symbol(c, c1, c4, {**rules[sym], "symbol": sym},
                                   fractional=True)
            st = rp.stats([{**t, "funding": 0.0,
                            "hold_hours": 0.0} for t in b["trades"]], b["start"])
            L.append(f"  baseline {label} {sym:<8}: {rp.headline(st)}"
                     + (" HALTED" if b["halted"] else ""))
    L.append("  (the baseline is 1h-only and single-symbol by construction, so it is")
    L.append("   replayed per symbol; funding is not modelled by the engine's own")
    L.append("   backtest, which flatters it slightly against the candidate.)")

    # ---- in-sample / out-of-sample --------------------------------------
    section(L, "2. CHRONOLOGICAL HOLD-OUT  (in-sample tuned, out-of-sample never seen)")
    L.append(f"  IS : {tb.ist(lo).strftime('%Y-%m-%d')} .. "
             f"{tb.ist(cut).strftime('%Y-%m-%d')}")
    L.append(f"  OOS: {tb.ist(cut).strftime('%Y-%m-%d')} .. "
             f"{tb.ist(hi).strftime('%Y-%m-%d')}")
    for label, c in (("DEPLOYABLE", cfg), ("DIAGNOSTIC", diag)):
        _, a = run(c, p, series, rules, lo, cut)
        _, b = run(c, p, series, rules, cut, hi)
        L.append(f"  {label} IS : {rp.headline(a)}")
        L.append(f"  {label} OOS: {rp.headline(b)}")
        if a["trades"] and b["trades"]:
            L.append(f"      expectancy IS {a['expectancy']:+.4f} vs OOS "
                     f"{b['expectancy']:+.4f} USDT/trade")

    # ---- walk-forward ---------------------------------------------------
    section(L, f"3. WALK-FORWARD  ({FOLDS} consecutive windows, each from a fresh balance)")
    for label, c in (("DEPLOYABLE", cfg), ("DIAGNOSTIC", diag)):
        L.append(f"  {label}:")
        pos = 0
        for k, (a, b) in enumerate(rp.folds(lo, hi, FOLDS), 1):
            _, st = run(c, p, series, rules, a, b)
            if st["net_pnl"] > 0:
                pos += 1
            L.append(f"    fold {k} {tb.ist(a).strftime('%Y-%m-%d')}.."
                     f"{tb.ist(b).strftime('%Y-%m-%d')}: {rp.headline(st)}")
        L.append(f"    folds profitable: {pos}/{FOLDS}")

    # ---- breakdowns -----------------------------------------------------
    section(L, "4. BREAKDOWN  (diagnostic run - the deployable run halts too early "
               "to break down)")
    t = dia_r["trades"]
    for key, title in (("side", "by direction"), ("symbol", "by symbol"),
                       ("exit_reason", "by exit"), ("regime", "by market regime")):
        L.append(f"  {title}:")
        for k, st in rp.by_key(t, key, dia_r["start"]).items():
            if st["trades"] >= 5:
                L.append(f"    {str(k)[:26]:<26} {rp.headline(st)}")

    section(L, "5. RISK SHAPE")
    L.append(f"  max consecutive losses : {dia['max_consec_losses']} "
             f"(diagnostic) / {dep['max_consec_losses']} (deployable)")
    L.append(f"  max consecutive wins   : {dia['max_consec_wins']}")
    L.append(f"  avg win {dia['avg_win']:+.4f} | avg loss {-dia['avg_loss']:+.4f} "
             f"| avg hold {dia['avg_hold_hours']:.1f} h")
    L.append(f"  max drawdown           : {dia['max_drawdown_pct']:.1f}% diagnostic, "
             f"{dep['max_drawdown_pct']:.1f}% deployable")
    risk_pct = cfg["risk_per_trade_pct"]
    halt_pct = cfg["max_account_drawdown_pct"]
    L.append(f"  THE BINDING CONSTRAINT : a {halt_pct}% permanent halt at "
             f"{risk_pct}% risk per trade is {halt_pct / risk_pct:.0f} consecutive "
             f"full-risk losses.")
    L.append(f"  At a {dia['win_rate']:.0f}% win rate the chance of hitting a "
             f"{halt_pct / risk_pct:.0f}-loss streak is "
             f"{_streak_prob(200, int(halt_pct / risk_pct), 1 - dia['win_rate'] / 100):.0%} "
             f"within 200 trades.")

    # ---- sensitivity ----------------------------------------------------
    section(L, "6. SENSITIVITY TO FEES AND SLIPPAGE  (diagnostic, full window)")
    L.append("    fee%/side  slip%/side   result")
    for fee, slip in ((cfg["taker_fee_pct"], cfg["slippage_pct"]),
                      (cfg["taker_fee_pct"], 0.0), (0.02, 0.01), (0.02, 0.0),
                      (0.0, 0.0), (0.08, 0.05)):
        c = diagnostic_cfg(cfg)
        c["taker_fee_pct"], c["slippage_pct"] = fee, slip
        _, st = run(c, p, series, rules, lo, hi)
        L.append(f"    {fee:>6.3f}    {slip:>6.3f}      {rp.headline(st)}")
    L.append("  The candidate only turns positive once per-side costs fall well")
    L.append("  below the configured taker assumptions, i.e. only with maker fills,")
    L.append("  which a market-order stop cannot provide.")

    section(L, "7. AMBIGUOUS BARS  (stop and target touched inside one 15m bar)")
    L.append(f"  bars booked as losses on the pessimistic rule: "
             f"{dia_r['ambiguous_bars']}")
    L.append(f"  net P&L if every one had resolved favourably instead: "
             f"{dia['net_pnl'] + dia_r['ambiguous_gain']:+.3f} USDT "
             f"(vs {dia['net_pnl']:+.3f} booked)")
    L.append("  The truth lies between those two numbers; OHLC data cannot say where.")
    return L


def _streak_prob(n, k, q):
    """P(at least one run of k losses in n trades), loss probability q."""
    dp = [0.0] * (k + 1)
    dp[0] = 1.0
    for _ in range(n):
        nd = [0.0] * (k + 1)
        for j in range(k):
            if not dp[j]:
                continue
            nd[0] += dp[j] * (1 - q)
            nd[j + 1] += dp[j] * q
        dp = nd
    return 1.0 - sum(dp[:k])


def main():
    cfg = tb.load_config(os.path.join(tb.HERE, "config.json"))
    client = labdata.public_client(cfg)
    rules = labdata.rules(cfg["symbols"], client=client, cfg=cfg)
    data = labdata.load(cfg["symbols"], ["4h", "1h", "15m"], days=DAYS,
                        warmup_days=WARMUP_DAYS, client=client, cfg=cfg)
    p = lab.params(**CANDIDATE)
    series = {s: lab.Series(data[s], p) for s in cfg["symbols"]}
    lo = max(data[s]["start"] for s in cfg["symbols"])
    hi = min(data[s]["end"] for s in cfg["symbols"])
    text = "\n".join(build(cfg, data, series, rules, lo, hi))
    tb.say(text)
    with open(REPORT, "w", encoding="utf-8") as f:
        f.write(text + "\n")
    tb.say(f"\nWritten to {REPORT}")


if __name__ == "__main__":
    main()
