"""
TradeLens AI - Core Engine Test Suite
Tests technical indicators, fundamental models, option Greeks, and shortlisting.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.data.sample_universe import SampleUniverseProvider
from app.core.shortlist_engine import scan_and_shortlist_universe, evaluate_single_stock_full_profile
from app.core.options_analytics import calculate_black_scholes_greeks


def run_tests():
    print("Running TradeLens AI Core Engine Tests...")
    
    # 1. Test Black Scholes
    greeks = calculate_black_scholes_greeks(spot=25000, strike=25000, time_to_expiry_years=7/365, volatility=0.15, option_type="CE")
    assert 0.45 <= greeks["delta"] <= 0.55, f"Unexpected ATM delta: {greeks['delta']}"
    assert greeks["theoretical_price"] > 0, "Option price should be positive"
    print("  ✔ Black-Scholes Greeks calculation: PASSED (ATM Delta ~0.50)")
    
    # 2. Test Universe Evaluation
    provider = SampleUniverseProvider()
    stocks = provider.get_all_stocks()
    assert len(stocks) >= 5, "Universe should have stocks"
    
    res = scan_and_shortlist_universe(stocks)
    assert "top_swing_setups" in res, "Should contain top_swing_setups"
    assert "top_options_setups" in res, "Should contain top_options_setups"
    assert "avoid_or_vetoed" in res, "Should contain avoid_or_vetoed"
    print(f"  ✔ Universe shortlisting scanner: PASSED ({len(res['top_swing_setups'])} swing, {len(res['top_options_setups'])} options, {len(res['avoid_or_vetoed'])} vetoed)")
    
    # 3. Test Invalidation Gate
    tcs = next(s for s in stocks if s["symbol"] == "TCS")
    tcs_eval = evaluate_single_stock_full_profile(tcs)
    assert tcs_eval["is_vetoed"] is True, "TCS should be vetoed due to earnings event risk"
    print("  ✔ 'Why NOT to Trade' Veto Gate: PASSED (Blocked earnings event risk)")
    
    print("\nALL TESTS PASSED! 🚀")


if __name__ == "__main__":
    run_tests()

