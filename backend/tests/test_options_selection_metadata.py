"""
TradeLens AI - Phase 0 Test Suite: Option Strike Selection & Instrument Metadata.
Verifies that:
1. NIFTY 50 and FINNIFTY strike steps are 50 points (not 100 points).
2. BANKNIFTY strike step is 100 points.
3. MIDCPNIFTY strike step is 25 points.
4. When broker option chain is missing, the engine does NOT fabricate fake OI (1.8M) or volume.
5. If available_strikes metadata is provided, recommended strike snaps to verified tradable contracts.
"""
import unittest
from app.core.options_analytics import get_strike_step, evaluate_option_chain_for_underlying


class TestOptionsSelectionMetadata(unittest.TestCase):

    def test_nifty_and_banknifty_strike_steps(self):
        """NIFTY 50 must have 50-point strikes; BANKNIFTY must have 100-point strikes."""
        # NIFTY at 24,875 -> step must be 50.0
        nifty_step = get_strike_step(spot=24875.0, symbol="NIFTY 50")
        self.assertEqual(nifty_step, 50.0, f"Expected 50.0 for NIFTY, got {nifty_step}")
        
        # FINNIFTY at 26,200 -> step must be 50.0
        fin_step = get_strike_step(spot=26200.0, symbol="FINNIFTY")
        self.assertEqual(fin_step, 50.0, f"Expected 50.0 for FINNIFTY, got {fin_step}")
        
        # BANKNIFTY at 57,000 -> step must be 100.0
        bank_step = get_strike_step(spot=57000.0, symbol="BANKNIFTY")
        self.assertEqual(bank_step, 100.0, f"Expected 100.0 for BANKNIFTY, got {bank_step}")
        
        # MIDCPNIFTY at 13,900 -> step must be 25.0
        mid_step = get_strike_step(spot=13900.0, symbol="MIDCPNIFTY")
        self.assertEqual(mid_step, 25.0, f"Expected 25.0 for MIDCPNIFTY, got {mid_step}")

    def test_no_fabricated_oi_when_chain_unavailable(self):
        """
        When chain_data is empty or unavailable, the option engine must NOT fabricate
        hardcoded 1.8M OI, 32% OI changes, or 950k volume.
        """
        res = evaluate_option_chain_for_underlying(
            spot_price=24862.0,
            lot_size=25,
            available_strikes=None,
            chain_data=None, # No broker option chain
            symbol="NIFTY 50"
        )
        self.assertEqual(res["strike_step"], 50.0)
        self.assertEqual(res["atm_strike"], 24850.0) # 24862 rounds to 24850 with 50-step!
        self.assertIsNone(res["pcr"], "PCR must be None when real option chain is missing")
        self.assertEqual(res["pcr_sentiment"], "UNAVAILABLE")
        self.assertEqual(res["option_chain_status"], "UNAVAILABLE")
        self.assertEqual(res["oi_alerts"], [])
        
        # Check strikes in matrix
        atm_cand = next(m for m in res["strike_matrix"] if m["strike"] == 24850.0)
        self.assertIsNone(atm_cand["oi"], "OI must not be fabricated")
        self.assertIsNone(atm_cand["oi_change_pct"], "OI change must not be fabricated")
        self.assertIsNone(atm_cand["volume"], "Volume must not be fabricated")

    def test_snap_to_available_tradable_strikes(self):
        """
        When available_strikes metadata is provided, the selected strikes
        must snap to actual existing contracts.
        """
        # Available strikes on an illiquid stock or specific expiry
        available = [24000.0, 24500.0, 25000.0, 25500.0]
        res = evaluate_option_chain_for_underlying(
            spot_price=24620.0,
            lot_size=25,
            available_strikes=available,
            chain_data={},
            symbol="NIFTY"
        )
        # Nearest available strike to 24620 is 24500
        self.assertEqual(res["atm_strike"], 24500.0)
        self.assertIn(res["atm_strike"], available)


if __name__ == "__main__":
    unittest.main()
