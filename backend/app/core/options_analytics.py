"""
TradeLens AI - Derivatives & Options Intelligence Engine
Black-Scholes pricing, Option Greeks (Delta, Gamma, Theta, Vega),
Strike selection (ATM, ITM, OTM), Open Interest buildup classification, and Payoff matrix.
"""
from typing import Dict, List, Any, Optional, Tuple
from datetime import datetime
import math
from app.data.base import DataStatus


def standard_normal_cdf(x: float) -> float:
    """Cumulative distribution function for standard normal distribution using math.erf."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def standard_normal_pdf(x: float) -> float:
    """Probability density function for standard normal distribution."""
    return (1.0 / math.sqrt(2.0 * math.pi)) * math.exp(-0.5 * x * x)


def calculate_black_scholes_greeks(
    spot: float,
    strike: float,
    time_to_expiry_years: float,
    risk_free_rate: float = 0.065, # Approx RBI repo rate 6.5%
    volatility: float = 0.20,
    option_type: str = "CE"
) -> Dict[str, float]:
    """
    Computes theoretical option price and all four first/second order Greeks.
    time_to_expiry_years: e.g. 7 days / 365 = 0.01917
    """
    if time_to_expiry_years <= 0 or volatility <= 0 or spot <= 0 or strike <= 0:
        intrinsic = max(0.0, spot - strike) if option_type.upper() == "CE" else max(0.0, strike - spot)
        return {
            "theoretical_price": round(intrinsic, 2),
            "delta": 1.0 if (option_type == "CE" and spot > strike) else (-1.0 if (option_type == "PE" and spot < strike) else 0.0),
            "gamma": 0.0,
            "theta_daily": 0.0,
            "vega": 0.0
        }
        
    sqrt_t = math.sqrt(time_to_expiry_years)
    d1 = (math.log(spot / strike) + (risk_free_rate + 0.5 * volatility * volatility) * time_to_expiry_years) / (volatility * sqrt_t)
    d2 = d1 - volatility * sqrt_t
    
    pdf_d1 = standard_normal_pdf(d1)
    
    if option_type.upper() == "CE":
        price = spot * standard_normal_cdf(d1) - strike * math.exp(-risk_free_rate * time_to_expiry_years) * standard_normal_cdf(d2)
        delta = standard_normal_cdf(d1)
        theta_annual = -(spot * pdf_d1 * volatility) / (2.0 * sqrt_t) - risk_free_rate * strike * math.exp(-risk_free_rate * time_to_expiry_years) * standard_normal_cdf(d2)
    else:
        price = strike * math.exp(-risk_free_rate * time_to_expiry_years) * standard_normal_cdf(-d2) - spot * standard_normal_cdf(-d1)
        delta = standard_normal_cdf(d1) - 1.0
        theta_annual = -(spot * pdf_d1 * volatility) / (2.0 * sqrt_t) + risk_free_rate * strike * math.exp(-risk_free_rate * time_to_expiry_years) * standard_normal_cdf(-d2)
        
    gamma = pdf_d1 / (spot * volatility * sqrt_t)
    vega = spot * sqrt_t * pdf_d1 / 100.0 # Per 1% change in IV
    theta_daily = theta_annual / 365.0
    
    return {
        "theoretical_price": round(max(0.05, price), 2),
        "delta": round(delta, 3),
        "gamma": round(gamma, 5),
        "theta_daily": round(theta_daily, 2),
        "vega": round(vega, 2)
    }


def classify_oi_buildup(price_change_pct: float, oi_change_pct: float) -> str:
    """Classifies F&O position structure based on Price and Open Interest movements."""
    if price_change_pct > 0 and oi_change_pct > 0:
        return "Long Buildup (Bullish Aggression)"
    elif price_change_pct > 0 and oi_change_pct < 0:
        return "Short Covering (Bullish Momentum)"
    elif price_change_pct < 0 and oi_change_pct > 0:
        return "Short Buildup (Bearish Pressure)"
    elif price_change_pct < 0 and oi_change_pct < 0:
        return "Long Unwinding (Bearish Exhaustion)"
    return "Neutral / Indecisive"


def get_strike_step(spot: float, symbol: Optional[str] = None) -> float:
    """
    Returns standard NSE strike interval step size based on instrument and price tier.
    NIFTY 50 and FINNIFTY options trade with 50-point strike intervals.
    BANKNIFTY options trade with 100-point strike intervals.
    MIDCPNIFTY options trade with 25-point strike intervals.
    """
    sym = (symbol or "").strip().upper()
    if "BANKNIFTY" in sym or "NIFTY BANK" in sym:
        return 100.0
    elif "MIDCPNIFTY" in sym or "NIFTY MID SELECT" in sym:
        return 25.0
    elif "NIFTY" in sym or "FINNIFTY" in sym:
        return 50.0
        
    # Equities:
    if spot >= 5000:
        return 50.0
    elif spot >= 2500:
        return 20.0
    elif spot >= 1000:
        return 20.0
    elif spot >= 500:
        return 10.0
    elif spot >= 200:
        return 5.0
    else:
        return 2.5


def verify_option_contract_tradability(
    contract_info: Dict[str, Any],
    min_oi: int = 50000,
    min_volume: int = 5000,
    max_spread_pct: float = 4.0,
    max_quote_age_seconds: int = 180,
    current_dt: Optional[datetime] = None
) -> Dict[str, Any]:
    """
    Validates option contract tradability:
    1. Contract pricing and counterparty presence (bid/ask).
    2. Bid-Ask spread percentage (execution friction).
    3. Minimum open interest and daily volume.
    4. Quote freshness (detects stale prices during market hours).
    Returns strict tradability classification and rejection reasons.
    """
    rejection_reasons = []
    warnings = []
    
    ltp = contract_info.get("ltp")
    bid = contract_info.get("bid")
    ask = contract_info.get("ask")
    oi = contract_info.get("oi")
    volume = contract_info.get("volume")
    last_trade_time = contract_info.get("last_trade_time")
    
    # 1. Price check
    if ltp is None or ltp <= 0:
        return {
            "is_tradable": False,
            "tradability_status": "UNAVAILABLE",
            "bid": None,
            "ask": None,
            "bid_ask_spread": None,
            "spread_pct": None,
            "liquidity_status": "UNAVAILABLE",
            "quote_freshness": "UNAVAILABLE",
            "rejection_reasons": ["No active trade price or quote available from broker."]
        }
        
    # 2. Bid-Ask Spread check
    spread = None
    spread_pct = None
    bid_ask_status = "UNKNOWN"
    
    if bid is not None and ask is not None:
        if bid > ask:
            rejection_reasons.append(f"Crossed bid/ask anomaly: Bid price (₹{bid}) > Ask price (₹{ask}). Corrupted quote feed.")
            bid_ask_status = "CROSSED_ORDERBOOK_ANOMALY"
        elif bid <= 0 or ask <= 0:
            rejection_reasons.append("Zero bid or ask counterparty quotes available (Illiquid book).")
            bid_ask_status = "NO_COUNTERPARTY_LIQUIDITY"
        else:
            spread = round(ask - bid, 2)
            spread_pct = round((spread / max(0.05, ltp)) * 100.0, 2)
            if spread_pct > max_spread_pct:
                rejection_reasons.append(
                    f"Excessive bid-ask spread: {spread_pct}% (₹{spread}) exceeds threshold ({max_spread_pct}%). Execution friction will destroy edge."
                )
                bid_ask_status = "WIDE_SPREAD_EXCESSIVE_FRICTION"
            elif spread_pct > (max_spread_pct / 2.0):
                warnings.append(f"Moderate spread: {spread_pct}%. Use limit orders only.")
                bid_ask_status = "MODERATE_SPREAD"
            else:
                bid_ask_status = "TIGHT_SPREAD_OPTIMAL"
    else:
        bid_ask_status = "BID_ASK_DATA_MISSING"
        warnings.append("Bid/Ask depth unavailable; monitor order book liquidity.")

    # 3. Liquidity check (OI & Volume)
    liquidity_status = "LIQUID"
    if oi is None:
        warnings.append("Open Interest metric is missing from broker feed.")
    elif min_oi is not None and oi < min_oi:
        warnings.append(f"Open Interest ({oi:,}) is below institutional threshold ({min_oi:,}).")
        liquidity_status = "MARGINAL_OI"

    if volume is None:
        warnings.append("Traded volume metric is missing from broker feed.")
    elif min_volume is not None and volume < min_volume:
        warnings.append(f"Session volume ({volume:,}) is below threshold ({min_volume:,}).")
        liquidity_status = "LOW_VOLUME"

    if (oi is not None and oi == 0) and (volume is not None and volume == 0):
        rejection_reasons.append("Zero open interest and zero session volume on contract.")
        liquidity_status = "ILLIQUID_GHOST_STRIKE"

    # 4. Quote Freshness check
    quote_freshness = "NOT_ASSESSED"
    if last_trade_time:
        try:
            # Parse timestamp if ISO or string format
            if isinstance(last_trade_time, str):
                ts = datetime.fromisoformat(last_trade_time.replace("Z", "+00:00"))
            else:
                ts = last_trade_time
            now = current_dt or datetime.now(ts.tzinfo)
            age_sec = (now - ts).total_seconds()
            if age_sec > max_quote_age_seconds:
                rejection_reasons.append(f"Stale quote: last trade occurred {int(age_sec)}s ago. Slippage risk high.")
                quote_freshness = "STALE_QUOTE"
            else:
                quote_freshness = "FRESH_LIVE_QUOTE"
        except Exception:
            quote_freshness = "PARSE_ERROR"

    is_tradable = len(rejection_reasons) == 0
    if not is_tradable:
        tradability_status = "UNTRADABLE_ILLIQUID"
    elif len(warnings) > 0:
        tradability_status = "TRADABLE_CAUTION"
    else:
        tradability_status = "TRADABLE_LIQUID"

    return {
        "is_tradable": is_tradable,
        "tradability_status": tradability_status,
        "bid": bid,
        "ask": ask,
        "bid_ask_spread": spread,
        "spread_pct": spread_pct,
        "bid_ask_status": bid_ask_status,
        "liquidity_status": liquidity_status,
        "quote_freshness": quote_freshness,
        "rejection_reasons": rejection_reasons,
        "warnings": warnings
    }


def evaluate_option_chain_for_underlying(
    spot_price: float,
    lot_size: int,
    available_strikes: Optional[List[float]] = None,
    chain_data: Optional[Dict[float, Dict[str, Any]]] = None,
    days_to_expiry: int = 7,
    expected_direction: str = "BULLISH",
    symbol: Optional[str] = None
) -> Dict[str, Any]:
    """
    Selects verified ITM, ATM, and OTM strikes, calculates Greeks, payoffs, and PCR.
    Does NOT fabricate simulated OI or volume if broker option chain data is unavailable.
    """
    step = get_strike_step(spot_price, symbol=symbol)
    chain_data = chain_data or {}
    has_real_chain = len(chain_data) > 0
    
    # Calculate pure ATM strike rounded to step
    raw_atm = round(spot_price / step) * step
    
    # If available_strikes is provided, snap to verified tradable contracts
    if available_strikes:
        sorted_strikes = sorted(available_strikes)
        atm_strike = min(sorted_strikes, key=lambda x: abs(x - raw_atm))
        atm_idx = sorted_strikes.index(atm_strike)
        if expected_direction == "BULLISH":
            itm_strike = sorted_strikes[max(0, atm_idx - 1)]
            otm_strike = sorted_strikes[min(len(sorted_strikes) - 1, atm_idx + 1)]
            target_option_type = "CE"
        else:
            itm_strike = sorted_strikes[min(len(sorted_strikes) - 1, atm_idx + 1)]
            otm_strike = sorted_strikes[max(0, atm_idx - 1)]
            target_option_type = "PE"
    else:
        atm_strike = raw_atm
        if expected_direction == "BULLISH":
            itm_strike = atm_strike - step
            otm_strike = atm_strike + step
            target_option_type = "CE"
        else:
            itm_strike = atm_strike + step
            otm_strike = atm_strike - step
            target_option_type = "PE"
        
    t_years = max(1, days_to_expiry) / 365.0
    
    # PCR Calculation
    if has_real_chain:
        total_pe_oi = sum(metrics.get("pe_oi", 0) for metrics in chain_data.values() if metrics.get("pe_oi"))
        total_ce_oi = sum(metrics.get("ce_oi", 0) for metrics in chain_data.values() if metrics.get("ce_oi"))
        pcr = round(total_pe_oi / max(1, total_ce_oi), 2) if (total_pe_oi or total_ce_oi) else None
        pcr_sentiment = (
            "Bullish / Oversold" if (pcr and pcr < 0.75)
            else ("Bearish / Overbought" if (pcr and pcr > 1.35) else "Neutral")
        )
    else:
        pcr = None
        pcr_sentiment = "UNAVAILABLE"
        
    candidates = [
        {"role": "ITM (Higher Delta / Low Decay)", "strike": itm_strike, "base_iv": 0.17},
        {"role": "ATM (Balanced Delta / High Volume)", "strike": atm_strike, "base_iv": 0.18},
        {"role": "OTM (Higher Leverage / High Decay)", "strike": otm_strike, "base_iv": 0.19}
    ]
    
    strike_comparisons = []
    
    for cand in candidates:
        s = cand["strike"]
        data_s = chain_data.get(s, {})
        opt_key = "ce" if target_option_type == "CE" else "pe"
        iv = data_s.get(f"{opt_key}_iv", cand["base_iv"])
        
        greeks = calculate_black_scholes_greeks(
            spot=spot_price,
            strike=s,
            time_to_expiry_years=t_years,
            volatility=iv,
            option_type=target_option_type
        )
        
        # Real broker market data if available, or theoretical Black-Scholes estimate
        ltp = data_s.get(f"{opt_key}_ltp") or greeks["theoretical_price"]
        oi = data_s.get(f"{opt_key}_oi")
        oi_chg = data_s.get(f"{opt_key}_oi_chg_pct")
        vol = data_s.get(f"{opt_key}_vol")
        
        contract_quote = {
            "ltp": data_s.get(f"{opt_key}_ltp"),
            "bid": data_s.get(f"{opt_key}_bid"),
            "ask": data_s.get(f"{opt_key}_ask"),
            "oi": oi,
            "volume": vol,
            "last_trade_time": data_s.get(f"{opt_key}_last_trade_time")
        }
        tradability = verify_option_contract_tradability(contract_quote) if has_real_chain else {
            "is_tradable": False,
            "tradability_status": "UNAVAILABLE",
            "bid": None,
            "ask": None,
            "bid_ask_spread": None,
            "spread_pct": None,
            "bid_ask_status": "DATA_UNAVAILABLE",
            "liquidity_status": "UNAVAILABLE",
            "quote_freshness": "UNAVAILABLE",
            "rejection_reasons": ["Broker option chain data unavailable."],
            "warnings": []
        }
        
        total_premium_invested = round(ltp * lot_size, 2)
        break_even = round(s + ltp, 2) if target_option_type == "CE" else round(s - ltp, 2)
        
        strike_comparisons.append({
            "role": cand["role"],
            "strike": s,
            "type": target_option_type,
            "ltp": round(ltp, 2),
            "lot_size": lot_size,
            "capital_required": total_premium_invested,
            "break_even": break_even,
            "oi": oi,
            "oi_change_pct": oi_chg,
            "volume": vol,
            "data_status": DataStatus.LIVE if (has_real_chain and data_s.get(f"{opt_key}_ltp")) else DataStatus.UNAVAILABLE,
            "is_tradable": tradability["is_tradable"],
            "tradability_status": tradability["tradability_status"],
            "bid": tradability["bid"],
            "ask": tradability["ask"],
            "bid_ask_spread": tradability["bid_ask_spread"],
            "spread_pct": tradability["spread_pct"],
            "liquidity_status": tradability["liquidity_status"],
            "quote_freshness": tradability["quote_freshness"],
            "tradability_rejection_reasons": tradability["rejection_reasons"],
            "iv_pct": round(iv * 100, 1),
            "delta": greeks["delta"],
            "gamma": greeks["gamma"],
            "theta_daily": greeks["theta_daily"],
            "max_risk": total_premium_invested,
            "suitability_note": (
                "Ideal for directional holding with minimal time decay" if cand["role"].startswith("ITM")
                else ("Recommended strike: optimal liquidity & delta sensitivity (~0.50)" if cand["role"].startswith("ATM")
                      else "High risk of expiring worthless if momentum stalls")
            )
        })
        
    alerts = []
    if has_real_chain:
        for s, metrics in chain_data.items():
            if metrics.get("ce_oi_chg_pct") and metrics["ce_oi_chg_pct"] > 35:
                alerts.append(f"Heavy Call Writing at {s} CE (+{metrics['ce_oi_chg_pct']}% OI): Resistance capping upside")
            if metrics.get("pe_oi_chg_pct") and metrics["pe_oi_chg_pct"] > 35:
                alerts.append(f"Heavy Put Writing at {s} PE (+{metrics['pe_oi_chg_pct']}% OI): Strong support floor established")
        
    return {
        "spot_price": spot_price,
        "atm_strike": atm_strike,
        "recommended_strike": atm_strike,
        "strike_step": step,
        "pcr": pcr,
        "pcr_sentiment": pcr_sentiment,
        "strike_matrix": strike_comparisons,
        "oi_alerts": alerts,
        "option_chain_status": "LIVE" if has_real_chain else "UNAVAILABLE"
    }

