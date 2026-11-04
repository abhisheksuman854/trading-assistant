"""
TradeLens AI - Command Line Market Scanner & Assistant
Runs the multi-factor shortlisting engine, enforces 'Why NOT to Trade' vetoes,
and monitors user's open positions.
"""
import sys
import os

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.data.sample_universe import SampleUniverseProvider
from app.core.shortlist_engine import scan_and_shortlist_universe, monitor_open_positions, evaluate_single_stock_full_profile


def print_banner():
    print("=" * 80)
    print("  🚀 TRADELENS AI - INDIAN MARKET DECISION SUPPORT & SHORTLIST ASSISTANT")
    print("  Empirical Probabilities · Derivatives (OI) · Fundamentals · Risk Vetoes")
    print("=" * 80)


def run_scanner():
    print_banner()
    provider = SampleUniverseProvider()
    all_stocks = provider.get_all_stocks()
    
    print(f"\n[*] Scanning universe of {len(all_stocks)} eligible Indian F&O / LargeCap tickers...")
    shortlist_result = scan_and_shortlist_universe(all_stocks, top_n=3, market_sentiment="BULLISH")
    
    # 1. Top Swing Setups
    print("\n" + "─" * 80)
    print("🎯 TOP SHORTLISTED SWING TRADES (Holding Horizon: 3 - 15 Days)")
    print("─" * 80)
    
    for idx, s in enumerate(shortlist_result["top_swing_setups"], 1):
        ts = s["trade_setup"]
        prob = s["probability"]
        print(f"\n[{idx}] {s['symbol']} ({s['name']}) | Score: {s['composite_score']}/100")
        print(f"    • CMP: ₹{s['cmp']:,.2f} | Entry Range: ₹{ts['entry_range'][0]} - ₹{ts['entry_range'][1]}")
        print(f"    • Stop-Loss: ₹{ts['stop_loss']} (Risk: ₹{ts['risk_per_share']})")
        print(f"    • Target 1: ₹{ts['target_1']} | Target 2: ₹{ts['target_2']} | R:R Ratio: {ts['risk_reward_ratio']}")
        print(f"    • Win Probability: {prob['target_first_rate_pct']}% Target-First (Based on {prob['sample_size']} historical setups)")
        print(f"    • Net Expectancy: {prob['net_expectancy_pct']}% / trade | Profit Factor: {prob['profit_factor']}")
        print(f"    • Suitability: {s['suitability']['swing']['reason']}")
        print(f"    • Key Drivers:")
        for driver in s["why_shortlisted"][:3]:
            print(f"      ✔ {driver}")
        if s["risk_flags"]:
            print(f"    • Watchout Flags:")
            for flag in s["risk_flags"][:2]:
                print(f"      ⚠ {flag}")

    # 2. Top F&O Options Trades
    print("\n" + "─" * 80)
    print("⚡ TOP SHORTLISTED OPTIONS / DERIVATIVES STRIKES (OI Confirmed)")
    print("─" * 80)
    
    for idx, s in enumerate(shortlist_result["top_options_setups"], 1):
        opt = s["options"]
        if not opt:
            continue
        rec = opt["recommended_strike"]
        mat = opt["strike_matrix"]
        atm_entry = next((m for m in mat if m["strike"] == rec), mat[1] if len(mat) > 1 else mat[0])
        
        print(f"\n[{idx}] {s['symbol']} - Recommended Strike: {atm_entry['strike']} {atm_entry['type']}")
        print(f"    • Spot Price: ₹{s['cmp']} | Strike Premium (LTP): ₹{atm_entry['ltp']}")
        print(f"    • Lot Size: {atm_entry['lot_size']} | Capital Required: ₹{atm_entry['capital_required']:,.2f}")
        print(f"    • Delta: {atm_entry['delta']} | Daily Theta Decay: -₹{abs(atm_entry['theta_daily']):.2f}")
        print(f"    • Strike OI: {atm_entry['oi']:,} ({'+' if atm_entry['oi_change_pct'] > 0 else ''}{atm_entry['oi_change_pct']}% session change)")
        print(f"    • Total PCR: {opt['pcr']} ({opt['pcr_sentiment']})")
        print(f"    • Payoff Target 1 Estimate: +₹{round(atm_entry['delta'] * s['trade_setup']['reward_target_1'] * atm_entry['lot_size']):,}")

    # 3. 'Why NOT to Trade' Veto Watchlist
    print("\n" + "─" * 80)
    print("⛔ 'WHY NOT TO TRADE' - REJECTED SETUPS (Saved Capital / Invalidation Gate)")
    print("─" * 80)
    
    for idx, s in enumerate(shortlist_result["avoid_or_vetoed"], 1):
        print(f"\n[{idx}] {s['symbol']} ({s['name']}) - REJECTED / DOWNRANKED")
        for reason in s["veto_reasons"]:
            print(f"    🚫 {reason}")

    # 4. Open Positions Monitoring Simulation
    print("\n" + "─" * 80)
    print("🛡 ACTIVE PORTFOLIO POSITION MONITOR (Exit / Trailing Alerts)")
    print("─" * 80)
    
    # Mock open positions held by the user
    user_positions = [
        {"symbol": "RELIANCE", "entry_price": 2780.0, "stop_loss": 2735.0, "target": 2880.0, "qty": 50},
        {"symbol": "TCS", "entry_price": 4020.0, "stop_loss": 3970.0, "target": 4120.0, "qty": 25}
    ]
    
    universe_profiles = {s["symbol"]: evaluate_single_stock_full_profile(s) for s in all_stocks}
    pos_reports = monitor_open_positions(user_positions, universe_profiles)
    
    for r in pos_reports:
        pnl_symbol = "+" if r["pnl_total"] >= 0 else ""
        print(f"\n[Position] {r['symbol']} | Qty: {r['qty']} | Entry: ₹{r['entry_price']} | CMP: ₹{r['current_price']}")
        print(f"    • P&L: {pnl_symbol}₹{r['pnl_total']:,.2f} ({pnl_symbol}{r['pnl_pct']}%)")
        print(f"    • Status: [{r['urgency']}] {r['status']}")
        print(f"    • Action Directive: 👉 {r['action_alert']}")

    print("\n" + "=" * 80)
    print("  Scan Complete. Ready to serve web dashboard API.")
    print("=" * 80)


if __name__ == "__main__":
    run_scanner()

