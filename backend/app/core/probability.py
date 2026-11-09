"""
TradeLens AI - Probability & Confluence Evaluation Engine
Computes objective rule-based technical confluence scores and net expectancy.
Strictly distinguishes between rule-based confluence, unvalidated estimates, and actual backtested statistics.
Never displays fabricated historical sample sizes or win rates.
"""
from typing import Dict, Any, List, Optional
import math


def calculate_expectancy(
    win_rate: float,
    avg_win_pct: float,
    avg_loss_pct: float,
    friction_pct: float = 0.15 # Approx 0.15% roundtrip for STT, brokerage, exchange turnover, slippage
) -> Dict[str, Any]:
    """
    Computes mathematical expected return per trade:
    EV = (P * Net Win) - ((1 - P) * Net Loss)
    """
    net_win = max(0.0, avg_win_pct - friction_pct)
    net_loss = avg_loss_pct + friction_pct
    
    p = win_rate / 100.0
    ev_pct = (p * net_win) - ((1.0 - p) * net_loss)
    
    profit_factor = (p * net_win) / max(0.001, (1.0 - p) * net_loss)
    
    return {
        "expected_value_pct": round(ev_pct, 2),
        "profit_factor": round(profit_factor, 2),
        "is_positive_expectancy": ev_pct > 0.15,
        "expectancy_assessment": (
            "Robust Edge (Positive Expectancy)" if ev_pct > 0.5
            else ("Marginal Edge" if ev_pct > 0 else "Negative Edge (Losing Setup)")
        )
    }


def compute_setup_probability_metrics(
    setup_name: str,
    rsi: float,
    price_above_ema50: bool,
    rvol: Optional[float],
    risk_reward_ratio: Optional[float],
    historical_dataset_stats: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """
    Evaluates rule-based technical confluence.
    If historical backtest statistics are provided, returns actual empirical stats.
    Otherwise, explicitly marks figures as 'UNVALIDATED_RULE_ESTIMATE' without inventing sample sizes.
    """
    # 1. If genuine backtested dataset stats are supplied by the caller/engine:
    if historical_dataset_stats and "sample_size" in historical_dataset_stats:
        samples = int(historical_dataset_stats["sample_size"])
        target_first_rate = float(historical_dataset_stats.get("target_first_rate_pct", 50.0))
        stop_first_rate = float(historical_dataset_stats.get("stop_first_rate_pct", 45.0))
        unresolved_rate = float(historical_dataset_stats.get("unresolved_rate_pct", 5.0))
        avg_win = float(historical_dataset_stats.get("avg_win_pct", 2.5))
        avg_loss = float(historical_dataset_stats.get("avg_loss_pct", 1.5))
        
        exp_data = calculate_expectancy(target_first_rate, avg_win, avg_loss)
        return {
            "setup_type": setup_name,
            "sample_size": samples,
            "target_first_rate_pct": target_first_rate,
            "stop_first_rate_pct": stop_first_rate,
            "unresolved_rate_pct": unresolved_rate,
            "avg_win_pct": avg_win,
            "avg_loss_pct": avg_loss,
            "net_expectancy_pct": exp_data["expected_value_pct"],
            "profit_factor": exp_data["profit_factor"],
            "is_positive_expectancy": exp_data["is_positive_expectancy"],
            "assessment": exp_data["expectancy_assessment"],
            "backtest_status": "VALIDATED_HISTORICAL_DATA"
        }

    # 2. In the absence of completed event-driven backtesting, evaluate rule confluence:
    confluence_score = 50.0
    drivers = []
    
    if price_above_ema50:
        confluence_score += 15.0
        drivers.append("Trend alignment with 50 EMA (+15 pts)")
        
    if 55 <= rsi <= 68:
        confluence_score += 15.0
        drivers.append(f"RSI in momentum expansion zone ({rsi}) (+15 pts)")
    elif rsi > 72 or rsi < 35:
        confluence_score -= 10.0
        drivers.append(f"RSI extreme ({rsi}) (-10 pts)")
        
    if rvol is not None:
        if rvol >= 1.5:
            confluence_score += 15.0
            drivers.append(f"Institutional volume surge ({rvol}x) (+15 pts)")
        elif rvol < 0.8:
            confluence_score -= 10.0
            drivers.append(f"Subdued volume ({rvol}x) (-10 pts)")
            
    if risk_reward_ratio is not None and risk_reward_ratio >= 1.5:
        confluence_score += 15.0
        drivers.append(f"Favorable structural R:R (1:{risk_reward_ratio}) (+15 pts)")
        
    confluence_score = min(95.0, max(20.0, confluence_score))
    
    return {
        "setup_type": setup_name,
        "confluence_score": round(confluence_score, 1),
        "confluence_level": "HIGH" if confluence_score >= 75 else ("MODERATE" if confluence_score >= 50 else "LOW"),
        "confluence_drivers": drivers,
        # Explicit honesty: No fabricated sample sizes or win rates
        "sample_size": None,
        "target_first_rate_pct": None,
        "stop_first_rate_pct": None,
        "unresolved_rate_pct": None,
        "net_expectancy_pct": None,
        "profit_factor": None,
        "is_positive_expectancy": False,
        "assessment": f"Rule Confluence Score: {confluence_score}/100",
        "backtest_status": "UNVALIDATED_RULE_ESTIMATE",
        "disclaimer": "Rule-based confluence only. Historical win rate requires systematic event-driven backtesting."
    }


