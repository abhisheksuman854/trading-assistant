"""
TradeLens AI - Tip Verifier & Trade Sanity Checker
Protects users from bad external tips, chased entries, poor risk-to-reward ratios,
and unhedged option theta traps.
"""
from typing import Dict, Any, Optional, List
from datetime import datetime
from .technicals import evaluate_technicals


def verify_tip_setup(
    symbol: str,
    instrument_type: str,
    tip_entry: float,
    tip_sl: float,
    tip_target_1: float,
    tip_target_2: Optional[float] = None,
    strike_price: Optional[float] = None,
    expiry_str: Optional[str] = None,
    lots: int = 1,
    user_capital: float = 50000.0,
    max_risk_pct: float = 4.0,
    stock_record: Optional[Dict[str, Any]] = None,
    live_quote: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """
    Evaluates an external tip or manual setup against live market reality.
    Returns a comprehensive sanity check, lot-aware exit plan, and capital risk report.
    """
    symbol = symbol.strip().upper()
    instrument_type = instrument_type.strip().upper()  # 'CE', 'PE', 'FUT', 'EQUITY'
    is_option = instrument_type in ["CE", "PE"]
    
    # 1. Resolve lot size & underlying stock data
    lot_size = 1
    cmp_stock = 0.0
    tech_eval = None
    
    if stock_record:
        lot_size = stock_record.get("lot_size", 250 if is_option else 1)
        candles = stock_record.get("candles", [])
        if candles:
            tech_eval = evaluate_technicals(candles)
            cmp_stock = tech_eval["cmp"]
    elif live_quote:
        lot_size = live_quote.get("lot_size", 250 if is_option else 1)
        cmp_stock = live_quote.get("ltp", 0.0)
    else:
        # Defaults based on standard NSE lots
        if "NIFTY" in symbol and "BANK" not in symbol:
            lot_size = 65
        elif "BANKNIFTY" in symbol:
            lot_size = 15
        elif is_option:
            lot_size = 250

    # Current LTP of the traded instrument (option premium or stock price)
    current_ltp = live_quote.get("option_ltp", tip_entry) if (live_quote and is_option) else (cmp_stock or tip_entry)
    
    # 2. Risk & Reward Calculations
    is_long = tip_target_1 > tip_entry
    if is_long:
        risk_per_unit = max(0.05, tip_entry - tip_sl)
        reward_1_per_unit = max(0.05, tip_target_1 - tip_entry)
        reward_2_per_unit = (tip_target_2 - tip_entry) if tip_target_2 else (reward_1_per_unit * 1.6)
    else:
        # Short setup
        risk_per_unit = max(0.05, tip_sl - tip_entry)
        reward_1_per_unit = max(0.05, tip_entry - tip_target_1)
        reward_2_per_unit = (tip_entry - tip_target_2) if tip_target_2 else (reward_1_per_unit * 1.6)
        
    risk_reward_ratio = round(reward_1_per_unit / risk_per_unit, 2)
    
    total_qty = lots * lot_size
    capital_required = round(tip_entry * total_qty, 2)
    max_loss_in_rupees = round(risk_per_unit * total_qty, 2)
    profit_t1_in_rupees = round(reward_1_per_unit * total_qty, 2)
    profit_t2_in_rupees = round(reward_2_per_unit * total_qty, 2)
    
    # User Risk Budget
    allowed_risk_budget = round(user_capital * (max_risk_pct / 100.0), 2)
    risk_budget_used_pct = round((max_loss_in_rupees / max(1.0, allowed_risk_budget)) * 100, 1)
    
    # 3. Chase Guard Evaluation
    chase_threshold = round(tip_entry * (1.03 if is_option else 1.01), 2)
    slippage_pct = round(((current_ltp - tip_entry) / max(0.01, tip_entry)) * 100, 2)
    
    chase_status = "IN_ZONE"
    if current_ltp > chase_threshold:
        chase_status = "CHASED"
    elif current_ltp >= tip_target_1:
        chase_status = "TARGET_ALREADY_HIT"
    elif current_ltp < tip_sl:
        chase_status = "ALREADY_BELOW_SL"
        
    # 4. Lot-Aware Exit Guidance
    if lots == 1:
        exit_guidance = {
            "mode": "SINGLE_LOT",
            "message": "You are trading 1 single lot. Partial lot exit is NOT possible in Indian options.",
            "target_1_plan": f"At Target 1 (₹{tip_target_1}): Take 100% full exit (+₹{profit_t1_in_rupees:,}) OR instantly move your SL in Angel One to COST (₹{tip_entry}) to let it run risk-free.",
            "target_2_plan": f"If you trailed to Cost at T1: Exit remaining lot at Target 2 (₹{tip_target_2 or tip_target_1}) for +₹{profit_t2_in_rupees:,} total gain.",
            "hard_rule": "NEVER hold 1 lot past Target 1 without moving your SL to Cost. A winning trade must never turn into a loss."
        }
    else:
        half_lots = lots // 2
        remaining_lots = lots - half_lots
        exit_guidance = {
            "mode": "MULTI_LOT",
            "message": f"You are trading {lots} lots ({total_qty} units). You can execute a professional 50/50 scale-out.",
            "target_1_plan": f"At Target 1 (₹{tip_target_1}): Sell {half_lots} lot(s) (+₹{round(reward_1_per_unit * half_lots * lot_size):,}) and IMMEDIATELY move SL on remaining {remaining_lots} lot(s) to COST (₹{tip_entry}).",
            "target_2_plan": f"At Target 2 (₹{tip_target_2 or tip_target_1}): Sell the remaining {remaining_lots} lot(s) for runner profits (+₹{round(reward_2_per_unit * remaining_lots * lot_size):,}).",
            "hard_rule": f"Once {half_lots} lot is booked at T1, your remaining position is 100% risk-free."
        }

    # 5. Sanity Checklist & Veto Checks
    checklist = []
    veto_reasons = []
    warning_flags = []
    
    # Check A: Stop-Loss Validity
    if is_long and tip_sl >= tip_entry:
        veto_reasons.append("INVALID STOP LOSS: For a Long/Call setup, Stop Loss must be strictly below Entry price.")
        checklist.append({"label": "Stop Loss Level", "status": "FAIL", "detail": f"SL (₹{tip_sl}) >= Entry (₹{tip_entry})"})
    elif not is_long and tip_sl <= tip_entry:
        veto_reasons.append("INVALID STOP LOSS: For a Short/Put setup, Stop Loss must be strictly above Entry price.")
        checklist.append({"label": "Stop Loss Level", "status": "FAIL", "detail": f"SL (₹{tip_sl}) <= Entry (₹{tip_entry})"})
    else:
        checklist.append({"label": "Stop Loss Level", "status": "PASS", "detail": f"Valid SL at ₹{tip_sl} ({round((risk_per_unit/tip_entry)*100, 1)}% risk)"})

    # Check B: Risk-to-Reward Ratio
    if risk_reward_ratio < 1.3:
        veto_reasons.append(f"UNFAVORABLE RISK/REWARD: Ratio is 1:{risk_reward_ratio}. Minimum viable ratio is 1:1.5.")
        checklist.append({"label": "Risk-to-Reward", "status": "FAIL", "detail": f"1:{risk_reward_ratio} (Below 1:1.5 standard)"})
    elif risk_reward_ratio < 1.6:
        warning_flags.append(f"Mediocre R:R (1:{risk_reward_ratio}). Target 1 is close to entry.")
        checklist.append({"label": "Risk-to-Reward", "status": "WARN", "detail": f"1:{risk_reward_ratio} (Acceptable but tight)"})
    else:
        checklist.append({"label": "Risk-to-Reward", "status": "PASS", "detail": f"Healthy 1:{risk_reward_ratio} (Target 1: +₹{profit_t1_in_rupees:,})"})

    # Check C: Chase Check
    if chase_status == "CHASED":
        veto_reasons.append(f"PRICE HAS RUN AWAY: Current price (₹{current_ltp}) is +{slippage_pct}% above tip entry. Do not chase green candles.")
        checklist.append({"label": "Entry Timing", "status": "FAIL", "detail": f"Chased (+{slippage_pct}%). Max entry ₹{chase_threshold}"})
    elif chase_status == "TARGET_ALREADY_HIT":
        veto_reasons.append("TARGET ALREADY HIT: The move has already occurred. High probability of entering at the top.")
        checklist.append({"label": "Entry Timing", "status": "FAIL", "detail": "Target 1 already achieved"})
    elif chase_status == "ALREADY_BELOW_SL":
        veto_reasons.append("ALREADY STOPPED OUT: Current price is already below the suggested Stop Loss.")
        checklist.append({"label": "Entry Timing", "status": "FAIL", "detail": f"LTP (₹{current_ltp}) < SL (₹{tip_sl})"})
    else:
        checklist.append({"label": "Entry Timing", "status": "PASS", "detail": f"Within valid entry zone (LTP: ₹{current_ltp})"})

    # Check D: Capital Risk Budget
    if max_loss_in_rupees > allowed_risk_budget:
        warning_flags.append(f"OVER-LEVERAGED: This trade risks ₹{max_loss_in_rupees:,}, which exceeds your ₹{allowed_risk_budget:,} ({max_risk_pct}%) risk budget.")
        checklist.append({"label": "Capital Sizing", "status": "WARN", "detail": f"Risks ₹{max_loss_in_rupees:,} (Budget: ₹{allowed_risk_budget:,})"})
    else:
        checklist.append({"label": "Capital Sizing", "status": "PASS", "detail": f"Max risk ₹{max_loss_in_rupees:,} ({risk_budget_used_pct}% of budget)"})

    # Check E: Underlying Technical Alignment
    if tech_eval and cmp_stock > 0:
        ema20 = tech_eval["ema"]["ema20"]
        if instrument_type == "CE" and cmp_stock < ema20:
            warning_flags.append(f"COUNTER-TREND: Underlying {symbol} is trading at ₹{cmp_stock:,.2f}, below its 20 EMA (₹{ema20:,.2f}). Call buying is counter-trend.")
            checklist.append({"label": "Trend Alignment", "status": "WARN", "detail": f"Underlying below 20 EMA (Bearish bias)"})
        elif instrument_type == "PE" and cmp_stock > ema20:
            warning_flags.append(f"COUNTER-TREND: Underlying {symbol} is trading at ₹{cmp_stock:,.2f}, above its 20 EMA (₹{ema20:,.2f}). Put buying is counter-trend.")
            checklist.append({"label": "Trend Alignment", "status": "WARN", "detail": f"Underlying above 20 EMA (Bullish bias)"})
        else:
            checklist.append({"label": "Trend Alignment", "status": "PASS", "detail": f"Underlying aligns with {instrument_type} setup"})
    else:
        checklist.append({"label": "Trend Alignment", "status": "PASS", "detail": "Neutral index / sector context"})

    # Check F: Theta Decay Warning for Options
    days_to_expiry = 7
    if stock_record:
        days_to_expiry = stock_record.get("days_to_expiry", 7)
    if is_option and days_to_expiry <= 4:
        warning_flags.append(f"HIGH THETA DECAY HAZARD: Expiry in {days_to_expiry} days. Do NOT hold overnight ('CF'). Must close intraday.")
        checklist.append({"label": "Theta Decay Hazard", "status": "WARN", "detail": f"{days_to_expiry} days to expiry (Intraday only)"})
    elif is_option:
        checklist.append({"label": "Theta Decay Hazard", "status": "PASS", "detail": f"{days_to_expiry} days to expiry (Normal theta)"})

    # 6. Overall Verdict Determination
    if len(veto_reasons) > 0:
        verdict = "REJECTED"
        verdict_color = "rose"
        verdict_badge = "🚫 REJECTED - DO NOT ENTER"
        headline = veto_reasons[0]
    elif len(warning_flags) > 0:
        verdict = "CAUTION"
        verdict_color = "amber"
        verdict_badge = "⚠️ PROCEED WITH CAUTION"
        headline = warning_flags[0]
    else:
        verdict = "APPROVED"
        verdict_color = "emerald"
        verdict_badge = "🟢 VERIFIED - SAFE TO TRADE"
        headline = "Setup passes all sanity checks. Favorable Risk/Reward with disciplined risk budget."

    return {
        "symbol": symbol,
        "instrument_type": instrument_type,
        "strike_price": strike_price,
        "current_ltp": current_ltp,
        "tip_entry": tip_entry,
        "tip_sl": tip_sl,
        "tip_target_1": tip_target_1,
        "tip_target_2": tip_target_2 or round(tip_entry + (reward_1_per_unit * 1.6), 2),
        "lot_size": lot_size,
        "lots": lots,
        "total_qty": total_qty,
        "capital_required": capital_required,
        "risk_per_unit": round(risk_per_unit, 2),
        "max_loss_rupees": max_loss_in_rupees,
        "profit_t1_rupees": profit_t1_in_rupees,
        "profit_t2_rupees": profit_t2_in_rupees,
        "risk_reward_ratio": f"1 : {risk_reward_ratio}",
        "chase_status": chase_status,
        "verdict": verdict,
        "verdict_badge": verdict_badge,
        "verdict_color": verdict_color,
        "headline": headline,
        "checklist": checklist,
        "veto_reasons": veto_reasons,
        "warning_flags": warning_flags,
        "exit_guidance": exit_guidance,
        "anti_averaging_rule": f"CRITICAL: If price drops below ₹{tip_sl}, cut the trade immediately. NEVER buy more lots to 'average' a losing option."
    }

