"""
TradeLens AI - Instrument Suitability Engine
Categorizes stocks into their optimal instrument bucket:
1. Long-term Investment
2. Swing Trading
3. Intraday Long
4. Intraday Short
5. Futures
6. Options
Provides explicit rationales and invalidation conditions.
"""
from typing import Dict, Any, List, Optional


def determine_instrument_suitability(
    fundamental_res: Dict[str, Any],
    technical_res: Dict[str, Any],
    options_res: Optional[Dict[str, Any]] = None,
    market_sentiment: str = "BULLISH" # BULLISH, BEARISH, SIDEWAYS
) -> Dict[str, Any]:
    """
    Evaluates where a stock/instrument belongs right now.
    """
    fund_score = fundamental_res.get("score", 50)
    tech_score = technical_res.get("score", 50)
    cmp_price = technical_res.get("cmp", 0.0)
    ema20 = technical_res.get("ema", {}).get("ema20", cmp_price)
    ema50 = technical_res.get("ema", {}).get("ema50", cmp_price)
    ema200 = technical_res.get("ema", {}).get("ema200", cmp_price)
    rsi = technical_res.get("rsi", 50.0)
    rvol = technical_res.get("rvol", 1.0)
    is_overextended = technical_res.get("is_overextended", False)
    mos = fundamental_res.get("valuation", {}).get("margin_of_safety_pct", 0.0)
    
    # 1. Long-term Investment Assessment
    if fund_score >= 70 and mos >= 10:
        inv_status = "Suitable"
        inv_reason = f"High fundamental score ({fund_score}/100) with favorable {mos}% margin of safety to intrinsic value."
    elif fund_score >= 65:
        inv_status = "Watchlist (Wait for Better Valuation)"
        inv_reason = f"Financially solid ({fund_score}/100) but currently trading near or above fair valuation."
    else:
        inv_status = "Not Suitable"
        inv_reason = f"Low fundamental score ({fund_score}/100) or high debt/solvency concerns."

    # 2. Swing Trading Assessment (Holding: 3 - 15 days)
    if tech_score >= 70 and not is_overextended and cmp_price > ema50 and market_sentiment != "BEARISH":
        swing_status = "Active Setup"
        swing_reason = f"Clean trend alignment (Score {tech_score}/100), price well-supported above 20 & 50 EMA without overextension."
        swing_invalidation = f"Daily close below 50 EMA (₹{ema50})"
    elif is_overextended:
        swing_status = "Wait (Avoid Chasing)"
        swing_reason = f"Price is overextended from 20 EMA. High risk of immediate mean-reversion."
        swing_invalidation = "Wait for pullback to 20 EMA test"
    elif tech_score >= 55:
        swing_status = "Watchlist"
        swing_reason = "Constructive structure, awaiting volume breakout or confirmation."
        swing_invalidation = f"Break below ₹{ema50}"
    else:
        swing_status = "Avoid"
        swing_reason = "Weak technical momentum or unfavorable risk/reward."
        swing_invalidation = "N/A"

    # 3. Intraday Long Assessment (Holding: Same day)
    if cmp_price > ema20 and rvol >= 1.5 and 55 <= rsi <= 72 and not is_overextended:
        intra_long_status = "Active Opportunity"
        intra_long_reason = f"Strong intraday volume ({rvol}x RVOL) with RSI ({rsi}) in expansion zone."
        intra_long_invalidation = f"Price slips below day open or 20 EMA (₹{ema20})"
    else:
        intra_long_status = "Neutral / Wait"
        intra_long_reason = "Requires intraday volume surge and price stability above 20 EMA."
        intra_long_invalidation = "N/A"

    # 4. Intraday Short Assessment
    if cmp_price < ema20 and rvol >= 1.5 and rsi < 42:
        intra_short_status = "Active Opportunity"
        intra_short_reason = f"Heavy selling volume ({rvol}x) with breakdown below moving averages."
        intra_short_invalidation = f"Price reclaiming ₹{ema20}"
    else:
        intra_short_status = "Not Recommended"
        intra_short_reason = "Trend is not under confirmed distribution."
        intra_short_invalidation = "N/A"

    # 5. Futures Assessment
    if tech_score >= 75 and market_sentiment == "BULLISH":
        fut_status = "Consider (Strict Stop-loss)"
        fut_reason = "High directional conviction justifies leveraged futures contract."
        fut_invalidation = f"Trailing stop below ₹{ema20}"
    else:
        fut_status = "High Leverage Caution"
        fut_reason = "Directional conviction is not sufficiently high to justify unhedged futures margin."
        fut_invalidation = "N/A"

    # 6. Options Assessment
    if options_res:
        atm_strike = options_res.get("atm_strike", cmp_price)
        pcr = options_res.get("pcr", 1.0)
        if tech_score >= 68 and not is_overextended:
            opt_status = f"Buy Call Option ({atm_strike} CE)"
            opt_reason = f"Bullish momentum with ATM delta ~0.50 offering defined-risk upside."
            opt_invalidation = f"Underlying crosses below ₹{ema20}"
        elif tech_score <= 40 and cmp_price < ema50:
            opt_status = f"Buy Put Option ({atm_strike} PE)"
            opt_reason = "Bearish continuation with Put delta advantage."
            opt_invalidation = f"Underlying breaks above ₹{ema20}"
        else:
            opt_status = "Wait / High IV Spread"
            opt_reason = "Options pricing or direction does not offer asymmetric edge."
            opt_invalidation = "Wait for breakout"
    else:
        opt_status = "Equity Cash Only (No F&O)"
        opt_reason = "Instrument not traded in derivatives segment."
        opt_invalidation = "N/A"

    return {
        "investment": {
            "status": inv_status,
            "horizon": "1 - 3 Years",
            "reason": inv_reason
        },
        "swing": {
            "status": swing_status,
            "horizon": "3 - 15 Days",
            "reason": swing_reason,
            "invalidation": swing_invalidation if 'swing_invalidation' in locals() else ""
        },
        "intraday_long": {
            "status": intra_long_status,
            "horizon": "Same Day (Intraday)",
            "reason": intra_long_reason,
            "invalidation": intra_long_invalidation if 'intra_long_invalidation' in locals() else ""
        },
        "intraday_short": {
            "status": intra_short_status,
            "horizon": "Same Day (Intraday)",
            "reason": intra_short_reason,
            "invalidation": intra_short_invalidation if 'intra_short_invalidation' in locals() else ""
        },
        "futures": {
            "status": fut_status,
            "horizon": "Monthly Expiry",
            "reason": fut_reason
        },
        "options": {
            "status": opt_status,
            "horizon": "Weekly / Monthly Expiry",
            "reason": opt_reason
        }
    }
