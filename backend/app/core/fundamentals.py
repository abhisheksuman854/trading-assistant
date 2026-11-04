"""
TradeLens AI - Fundamental Analysis & Intrinsic Valuation Engine
Comprehensive valuation, solvency, profitability, cash flow, and governance checks.
Supports standard commercial/industrial models and dedicated Banking/NBFC analysis.
"""
from typing import Dict, Any, Optional
import math


def calculate_dcf_intrinsic_value(
    fcf: float,
    shares_outstanding: float,
    growth_rate: float = 0.12,
    discount_rate: float = 0.10,
    terminal_growth_rate: float = 0.04,
    years: int = 5
) -> float:
    """Calculates Intrinsic Value per share using 2-Stage Discounted Cash Flow model."""
    if fcf <= 0 or shares_outstanding <= 0:
        return 0.0
        
    pv_fcf = 0.0
    current_fcf = fcf
    for t in range(1, years + 1):
        current_fcf *= (1.0 + growth_rate)
        pv_fcf += current_fcf / ((1.0 + discount_rate) ** t)
        
    # Terminal Value
    terminal_fcf = current_fcf * (1.0 + terminal_growth_rate)
    terminal_value = terminal_fcf / (discount_rate - terminal_growth_rate)
    pv_terminal_value = terminal_value / ((1.0 + discount_rate) ** years)
    
    total_enterprise_equity_value = pv_fcf + pv_terminal_value
    intrinsic_per_share = total_enterprise_equity_value / shares_outstanding
    return round(intrinsic_per_share, 2)


def calculate_graham_number(eps: float, bvps: float) -> Optional[float]:
    """Calculates Benjamin Graham's classic value ceiling: sqrt(22.5 * EPS * BVPS)."""
    if eps <= 0 or bvps <= 0:
        return None
    return round(math.sqrt(22.5 * eps * bvps), 2)


def evaluate_fundamentals(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Evaluates comprehensive fundamental health, intrinsic values, and solvency.
    
    Expected input schema:
    {
        "symbol": str,
        "is_banking": bool,
        "cmp": float,
        "eps": float,
        "bvps": float,
        "pe": float,
        "pb": float,
        "industry_pe": float,
        "shares_cr": float,
        "fcf_cr": float,
        "cfo_cr": float,
        "net_profit_cr": float,
        "revenue_growth_3yr": float,
        "profit_growth_3yr": float,
        "roe": float,
        "roce": float,
        "debt_to_equity": float,
        "interest_coverage": float,
        "current_ratio": float,
        "quick_ratio": float,
        "promoter_holding": float,
        "promoter_pledge": float,
        # Banking specific (optional)
        "crar": Optional[float],
        "gross_npa": Optional[float],
        "net_npa": Optional[float],
        "nim": Optional[float],
        "casa_ratio": Optional[float]
    }
    """
    cmp = data.get("cmp", 1.0)
    eps = data.get("eps", 0.0)
    bvps = data.get("bvps", 0.0)
    pe = data.get("pe", cmp / max(0.01, eps))
    pb = data.get("pb", cmp / max(0.01, bvps))
    industry_pe = data.get("industry_pe", 22.0)
    is_banking = data.get("is_banking", False)
    
    # 1. Intrinsic Valuation
    fcf = data.get("fcf_cr", 0.0)
    shares = data.get("shares_cr", 1.0)
    
    # Three DCF cases
    dcf_conservative = calculate_dcf_intrinsic_value(fcf, shares, growth_rate=0.08, discount_rate=0.12, terminal_growth_rate=0.03)
    dcf_base = calculate_dcf_intrinsic_value(fcf, shares, growth_rate=0.12, discount_rate=0.10, terminal_growth_rate=0.04)
    dcf_optimistic = calculate_dcf_intrinsic_value(fcf, shares, growth_rate=0.16, discount_rate=0.09, terminal_growth_rate=0.045)
    
    graham_val = calculate_graham_number(eps, bvps)
    
    # Pick benchmark intrinsic value
    benchmark_intrinsic = dcf_base if dcf_base > 0 else (graham_val or cmp)
    margin_of_safety_pct = round(((benchmark_intrinsic - cmp) / benchmark_intrinsic) * 100, 2) if benchmark_intrinsic > 0 else 0.0
    
    # 2. Scoring System (0 - 100)
    score = 0
    bullish_factors = []
    risk_factors = []
    
    # Valuation Score (25 pts)
    if eps < 0:
        risk_factors.append("Loss-making company: Negative EPS / Negative P/E")
    else:
        if pe < industry_pe * 0.85:
            score += 15
            bullish_factors.append(f"Undervalued: P/E ({pe:.1f}) is below industry average ({industry_pe:.1f})")
        elif pe <= industry_pe * 1.15:
            score += 10
            bullish_factors.append(f"Fairly valued: P/E ({pe:.1f}) aligned with industry ({industry_pe:.1f})")
        else:
            risk_factors.append(f"Expensive P/E ({pe:.1f}) vs industry ({industry_pe:.1f})")
            
    if margin_of_safety_pct >= 20:
        score += 10
        bullish_factors.append(f"Favourable Margin of Safety: {margin_of_safety_pct}% upside to DCF Base Value (₹{benchmark_intrinsic})")
    elif margin_of_safety_pct < -15:
        risk_factors.append(f"Trading at a {abs(margin_of_safety_pct)}% premium above estimated DCF intrinsic value")
        
    # Solvency & Liquidity (25 pts)
    if is_banking:
        crar = data.get("crar", 16.0)
        gnpa = data.get("gross_npa", 2.5)
        nnpa = data.get("net_npa", 0.6)
        nim = data.get("nim", 3.8)
        
        if crar >= 15.0:
            score += 10
            bullish_factors.append(f"Solid Capital Adequacy: CRAR {crar}% exceeds regulatory requirement")
        if gnpa < 3.0 and nnpa < 1.0:
            score += 15
            bullish_factors.append(f"Clean Asset Quality: GNPA {gnpa}%, NNPA {nnpa}%")
        else:
            risk_factors.append(f"Elevated NPAs: GNPA {gnpa}%, NNPA {nnpa}%")
    else:
        debt_to_equity = data.get("debt_to_equity", 0.5)
        icr = data.get("interest_coverage", 4.0)
        cr = data.get("current_ratio", 1.5)
        
        if debt_to_equity < 0.3:
            score += 12
            bullish_factors.append(f"Virtually debt-free / Low leverage: D/E {debt_to_equity}")
        elif debt_to_equity <= 0.8:
            score += 8
            bullish_factors.append(f"Manageable leverage: D/E {debt_to_equity}")
        elif debt_to_equity > 1.5:
            risk_factors.append(f"High Debt-to-Equity ({debt_to_equity}) - elevated balance sheet leverage")
            
        if icr >= 5.0:
            score += 8
            bullish_factors.append(f"Robust interest paying capacity: ICR {icr:.1f}x")
        elif icr < 2.0:
            risk_factors.append(f"Weak interest coverage: ICR {icr:.1f}x (Vulnerable to rate hikes)")
            
        if cr >= 1.3:
            score += 5
            bullish_factors.append(f"Healthy working capital liquidity: Current Ratio {cr:.2f}")
        elif cr < 1.0:
            risk_factors.append(f"Liquidity crunch: Current Ratio {cr:.2f} is below 1.0")
            
    # Profitability & Returns (25 pts)
    roe = data.get("roe", 16.0)
    roce = data.get("roce", 18.0)
    rev_growth = data.get("revenue_growth_3yr", 12.0)
    profit_growth = data.get("profit_growth_3yr", 15.0)
    
    if roe >= 18.0:
        score += 10
        bullish_factors.append(f"High Return on Equity: ROE {roe}%")
    elif roe >= 12.0:
        score += 6
    else:
        risk_factors.append(f"Subdued ROE ({roe}%)")
        
    if not is_banking:
        if roce >= 20.0:
            score += 8
            bullish_factors.append(f"Exceptional capital efficiency: ROCE {roce}%")
        elif roce >= 14.0:
            score += 5
            
    if rev_growth >= 12.0 and profit_growth >= 15.0:
        score += 7
        bullish_factors.append(f"Consistent 3-Yr Growth: Revenue +{rev_growth}%, PAT +{profit_growth}%")
        
    # Cash Flow Quality (15 pts)
    cfo = data.get("cfo_cr", 100.0)
    pat = data.get("net_profit_cr", 90.0)
    if pat > 0 and cfo > 0:
        cfo_pat_ratio = cfo / pat
        if cfo_pat_ratio >= 1.0:
            score += 10
            bullish_factors.append(f"High earnings quality: CFO/PAT ratio is {cfo_pat_ratio:.2f} (Clean cash generation)")
        else:
            score += 5
            risk_factors.append(f"Cash conversion divergence: CFO/PAT ratio is {cfo_pat_ratio:.2f}")
    if fcf > 0:
        score += 5
        bullish_factors.append(f"Free Cash Flow positive: ₹{fcf:,.0f} Cr")
    else:
        risk_factors.append("Negative Free Cash Flow after Capex")
        
    # Governance & Ownership (10 pts)
    promoter_holding = data.get("promoter_holding", 52.0)
    promoter_pledge = data.get("promoter_pledge", 0.0)
    
    if promoter_pledge == 0.0:
        score += 5
        bullish_factors.append("Zero promoter share pledge")
    elif promoter_pledge > 20.0:
        risk_factors.append(f"CRITICAL RED FLAG: Promoter pledge is {promoter_pledge}%")
    elif promoter_pledge > 5.0:
        risk_factors.append(f"Moderate promoter pledge: {promoter_pledge}%")
        
    if promoter_holding >= 50.0:
        score += 5
        bullish_factors.append(f"Strong promoter stake: {promoter_holding}%")
        
    return {
        "score": min(100, score),
        "valuation": {
            "pe": pe,
            "pb": pb,
            "industry_pe": industry_pe,
            "dcf_conservative": dcf_conservative,
            "dcf_base": dcf_base,
            "dcf_optimistic": dcf_optimistic,
            "graham_number": graham_val,
            "margin_of_safety_pct": margin_of_safety_pct,
            "assessment": "Undervalued" if margin_of_safety_pct > 15 else ("Fair" if margin_of_safety_pct > -10 else "Overvalued")
        },
        "solvency": {
            "debt_to_equity": data.get("debt_to_equity", 0.0),
            "interest_coverage": data.get("interest_coverage", 0.0),
            "current_ratio": data.get("current_ratio", 0.0),
            "quick_ratio": data.get("quick_ratio", 0.0)
        },
        "profitability": {
            "roe": roe,
            "roce": roce,
            "revenue_growth_3yr": rev_growth,
            "profit_growth_3yr": profit_growth
        },
        "cash_flow": {
            "cfo_cr": cfo,
            "fcf_cr": fcf,
            "cfo_pat_ratio": round(cfo / max(0.01, pat), 2) if pat > 0 else 0.0
        },
        "governance": {
            "promoter_holding": promoter_holding,
            "promoter_pledge": promoter_pledge
        },
        "bullish_factors": bullish_factors,
        "risk_factors": risk_factors
    }

