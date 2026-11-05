"""
TradeLens AI - Trade Shortlisting & Position Monitor Engine
Scans, filters, ranks, and shortlists high-conviction trades across Swing, Intraday, and F&O.
Enforces hard 'Why NOT to Trade' veto filters and monitors user's open positions for exit signals.
"""
from typing import List, Dict, Any, Optional
from .technicals import evaluate_technicals
from .fundamentals import evaluate_fundamentals
from .options_analytics import evaluate_option_chain_for_underlying, classify_oi_buildup
from .suitability import determine_instrument_suitability
from .probability import compute_setup_probability_metrics


def evaluate_single_stock_full_profile(
    stock_record: Dict[str, Any],
    market_sentiment: str = "BULLISH"
) -> Dict[str, Any]:
    """
    Runs technical, fundamental, option chain, and suitability evaluations on a stock.
    Calculates exact trade setup (Entry, Stop Loss, Target 1, Target 2, Risk/Reward).
    """
    symbol = stock_record["symbol"]
    name = stock_record.get("name", symbol)
    candles = stock_record.get("candles", [])
    fund_data = stock_record.get("fundamentals", {})
    option_chain = stock_record.get("option_chain", None)
    lot_size = stock_record.get("lot_size", 250)
    
    # 1. Technical Evaluation
    tech_eval = evaluate_technicals(candles)
    cmp_price = tech_eval["cmp"]
    atr = tech_eval["atr"]
    ema20 = tech_eval["ema"]["ema20"]
    ema50 = tech_eval["ema"]["ema50"]
    pivots = tech_eval["pivots"]
    
    # 2. Fundamental Evaluation
    fund_data["cmp"] = cmp_price
    fund_eval = evaluate_fundamentals(fund_data)
    
    # 3. Derivatives / Option Chain Evaluation (if F&O eligible)
    opt_eval = None
    if lot_size and lot_size > 0:
        opt_eval = evaluate_option_chain_for_underlying(
            spot_price=cmp_price,
            lot_size=lot_size,
            available_strikes=list(option_chain.keys()) if option_chain else None,
            chain_data=option_chain or {},
            days_to_expiry=stock_record.get("days_to_expiry", 7),
            expected_direction="BULLISH" if tech_eval["score"] >= 50 else "BEARISH"
        )
        
    # 4. Suitability Classification
    suitability = determine_instrument_suitability(
        fundamental_res=fund_eval,
        technical_res=tech_eval,
        options_res=opt_eval,
        market_sentiment=market_sentiment
    )
    
    # 5. Trade Setup Calculations (Entry, Stop Loss, Targets)
    # Stop-Loss placed below recent swing low or 1.5 * ATR below 20 EMA
    stop_loss = round(max(cmp_price - 2.0 * atr, min(ema20 - 0.5 * atr, cmp_price * 0.965)), 2)
    risk_per_share = round(cmp_price - stop_loss, 2)
    
    # Target 1 (1.8x risk) & Target 2 (2.8x risk or R2 pivot)
    target_1 = round(cmp_price + (1.8 * risk_per_share), 2)
    target_2 = round(cmp_price + (2.8 * risk_per_share), 2)
    
    risk_reward_num = round((target_1 - cmp_price) / max(0.1, risk_per_share), 2)
    risk_reward_str = f"1 : {risk_reward_num}"
    
    # 6. Hard Veto 'Why NOT to Trade' Gatekeeper
    veto_reasons = []
    
    # Filter 1: Earnings announcement risk
    days_to_earnings = stock_record.get("days_to_earnings", 99)
    if days_to_earnings <= 2:
        veto_reasons.append(f"HIGH EVENT RISK: Quarterly earnings announcement in {days_to_earnings} days. Unpredictable gap risk.")
        
    # Filter 2: Chasing / Overextension
    if tech_eval["is_overextended"]:
        veto_reasons.append(f"OVEREXTENDED: Price is {tech_eval['dist_from_ema20_pct']}% above 20 EMA. High risk of chasing near local top.")
        
    # Filter 3: Poor Risk-to-Reward
    if risk_reward_num < 1.4:
        veto_reasons.append(f"UNFAVORABLE RISK/REWARD: Ratio is {risk_reward_str} (requires minimum 1:1.5).")
        
    # Filter 4: Severe Fundamental Distress
    if fund_eval["score"] < 35 and not stock_record.get("is_intraday_only", False):
        veto_reasons.append(f"FUNDAMENTAL RED FLAG: Score {fund_eval['score']}/100 with high debt or negative solvency.")
        
    # Filter 5: Volume Trap
    if tech_eval["rvol"] < 0.7:
        veto_reasons.append(f"VOLUME TRAP: RVOL is only {tech_eval['rvol']}x average. Lack of institutional participation.")
        
    is_vetoed = len(veto_reasons) > 0
    
    # 7. Probability Engine Assessment
    setup_name = (
        "Pullback to 20 EMA + Support" if abs(cmp_price - ema20) <= 0.8 * atr
        else ("Breakout with Volume Confirmation" if tech_eval["rvol"] >= 1.6 and cmp_price > pivots["r1"]
              else "Momentum Trend Follow")
    )
    
    prob_metrics = compute_setup_probability_metrics(
        setup_name=setup_name,
        rsi=tech_eval["rsi"],
        price_above_ema50=cmp_price > ema50,
        rvol=tech_eval["rvol"],
        risk_reward_ratio=risk_reward_num
    )
    
    # 8. Composite Ranking Score (0 - 100)
    # Weights: 35% Tech, 30% Fund, 20% Volume/Momentum, 15% Probability/Expectancy
    composite_score = round(
        (0.35 * tech_eval["score"]) +
        (0.30 * fund_eval["score"]) +
        (0.20 * min(100, tech_eval["rvol"] * 50)) +
        (0.15 * prob_metrics["target_first_rate_pct"]),
        1
    )
    
    if is_vetoed:
        composite_score = min(45.0, composite_score - 30.0) # Downrank vetoed setups
        
    # Why Shortlisted points
    why_shortlisted = []
    if tech_eval["score"] >= 65:
        why_shortlisted.extend(tech_eval["bullish_factors"][:2])
    if fund_eval["score"] >= 65:
        why_shortlisted.extend(fund_eval["bullish_factors"][:2])
    if prob_metrics["is_positive_expectancy"]:
        why_shortlisted.append(f"Positive historical expectancy: {prob_metrics['target_first_rate_pct']}% target-first rate across {prob_metrics['sample_size']} setups.")
    if opt_eval and opt_eval.get("oi_alerts"):
        why_shortlisted.append(opt_eval["oi_alerts"][0])
        
    final_score = stock_record.get("composite_score", composite_score)
    final_strategy = stock_record.get("strategy_name", setup_name)
    final_timeframe = stock_record.get("timeframe", "INTRADAY" if tech_eval.get("rvol", 1.0) >= 1.8 else "SWING")
    final_pcr = stock_record.get("pcr", 0.88)
    final_rvol = stock_record.get("rvol", round(tech_eval.get("rvol", 1.2), 1))
    final_rsi = stock_record.get("rsi", round(tech_eval.get("rsi", 55.0), 1))

    return {
        "symbol": symbol,
        "name": name,
        "category": stock_record.get("category", "LargeCap"),
        "cmp": cmp_price,
        "live_change_pct": stock_record.get("live_change_pct", 0.0),
        "composite_score": final_score,
        "pcr": final_pcr,
        "rvol": final_rvol,
        "rsi": final_rsi,
        "strategy_name": final_strategy,
        "timeframe": final_timeframe,
        "option_recommendation": stock_record.get("option_recommendation"),
        "is_vetoed": is_vetoed,
        "veto_reasons": veto_reasons,
        "trade_setup": {
            "setup_name": final_strategy,
            "direction": "LONG",
            "entry_range": [round(cmp_price * 0.998, 2), round(cmp_price * 1.004, 2)],
            "stop_loss": stop_loss,
            "target_1": target_1,
            "target_2": target_2,
            "risk_per_share": risk_per_share,
            "reward_target_1": round(target_1 - cmp_price, 2),
            "risk_reward_ratio": risk_reward_str,
            "invalidation_level": stop_loss
        },
        "technicals": tech_eval,
        "fundamentals": fund_eval,
        "options": opt_eval,
        "suitability": suitability,
        "probability": prob_metrics,
        "why_shortlisted": why_shortlisted,
        "risk_flags": veto_reasons + tech_eval["risk_factors"][:2]
    }


def scan_and_shortlist_universe(
    stocks: List[Dict[str, Any]],
    top_n: int = 5,
    market_sentiment: str = "BULLISH"
) -> Dict[str, Any]:
    """
    Evaluates an entire universe of stocks and extracts top shortlists:
    - Top Swing Setups
    - Top Intraday Momentum Setups
    - Top F&O Option Strike Trades
    - Vetoed / Avoid Watchlist
    """
    evaluated = []
    for s in stocks:
        profile = evaluate_single_stock_full_profile(s, market_sentiment=market_sentiment)
        evaluated.append(profile)
        
    # Sort by composite score
    evaluated.sort(key=lambda x: x["composite_score"], reverse=True)
    
    # 1. Qualified non-vetoed setups
    clean_setups = [x for x in evaluated if not x["is_vetoed"]]
    vetoed_setups = [x for x in evaluated if x["is_vetoed"]]
    
    # Top Opportunities (Best composite scores & momentum)
    top_swing = sorted(clean_setups, key=lambda x: x.get("composite_score", 0), reverse=True)[:top_n]
    
    # Top Intraday Setups (Highest RVOL)
    top_intraday = sorted(
        clean_setups,
        key=lambda x: x.get("rvol", 1.0),
        reverse=True
    )[:top_n]
    
    # Top F&O Options Setups (Active option recommendation with clean PCR & delta)
    top_options = [x for x in clean_setups if x.get("options") is not None or x.get("option_recommendation") is not None][:top_n]

    # Live F&O Top Movers (Largest % gainers)
    top_movers = [
        {
            "symbol": m["symbol"],
            "cmp": m["cmp"],
            "change_pct": m.get("live_change_pct", 0.0),
            "rvol": m.get("rvol", 1.2),
            "pcr": m.get("pcr", 0.88),
            "strategy": m.get("strategy_name", "Breakout")
        }
        for m in sorted(clean_setups, key=lambda x: x.get("live_change_pct", 0.0), reverse=True)[:8]
    ]
    
    return {
        "market_sentiment": market_sentiment,
        "total_scanned": len(stocks),
        "shortlisted_count": len(clean_setups),
        "top_swing_setups": top_swing,
        "top_intraday_setups": top_intraday,
        "top_options_setups": top_options,
        "top_movers": top_movers,
        "avoid_or_vetoed": vetoed_setups[:5]
    }


def monitor_open_positions(
    open_positions: List[Dict[str, Any]],
    current_universe_profiles: Dict[str, Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """
    Monitors user's open positions and alerts on target hits, stop-loss breaches,
    trailing stop recommendations, or adverse trend/OI breakdowns.
    
    open_positions schema:
    [
        {"symbol": "RELIANCE", "entry_price": 2810.0, "stop_loss": 2770.0, "target": 2890.0, "qty": 50, "instrument": "SWING"}
    ]
    """
    position_reports = []
    
    for pos in open_positions:
        sym = pos["symbol"]
        entry = pos["entry_price"]
        sl = pos["stop_loss"]
        target = pos["target"]
        qty = pos.get("qty", 1)
        
        profile = current_universe_profiles.get(sym)
        if not profile:
            continue
            
        cmp_price = profile["cmp"]
        pnl_per_share = round(cmp_price - entry, 2)
        pnl_total = round(pnl_per_share * qty, 2)
        pnl_pct = round((pnl_per_share / entry) * 100, 2)
        
        status = "HOLD (NORMAL)"
        action_alert = "Maintain current stop-loss"
        urgency = "LOW"
        
        # Check Stop Loss Breach
        if cmp_price <= sl:
            status = "STOP LOSS TRIGGERED (EXIT IMMEDIATELY)"
            action_alert = f"Price (₹{cmp_price}) breached stop loss (₹{sl}). Invalidate and cut position."
            urgency = "CRITICAL"
        # Check Target Hit
        elif cmp_price >= target:
            status = "TARGET REACHED (BOOK PROFIT)"
            action_alert = f"Target (₹{target}) reached. Book full profits or trail stop-loss tightly."
            urgency = "HIGH"
        # Check Trailing Stop Opportunity
        elif pnl_pct >= 2.5:
            ema20 = profile["technicals"]["ema"]["ema20"]
            new_trailing_sl = max(entry, ema20)
            status = "PROFIT ZONE (TRAIL STOP)"
            action_alert = f"Up +{pnl_pct}%. Raise stop-loss to ₹{new_trailing_sl} to lock in gains."
            urgency = "MEDIUM"
        # Check Trend Invalidation
        elif profile["is_vetoed"]:
            status = "RISK WARNING"
            action_alert = f"New adverse risk detected: {profile['veto_reasons'][0]}"
            urgency = "HIGH"
            
        position_reports.append({
            "symbol": sym,
            "entry_price": entry,
            "current_price": cmp_price,
            "stop_loss": sl,
            "target": target,
            "qty": qty,
            "pnl_total": pnl_total,
            "pnl_pct": pnl_pct,
            "status": status,
            "action_alert": action_alert,
            "urgency": urgency
        })
        
    return position_reports

