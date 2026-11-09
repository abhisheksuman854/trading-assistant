"""
TradeLens AI - Phase 0 Test Suite: Independent Risk/Reward Calculation.
Verifies that:
1. Target is NOT artificially derived from a fixed multiplier (e.g. risk * 1.8).
2. Risk, reward, and R:R ratio are computed from independent structural inputs.
3. Both LONG (Bullish) and SHORT (Bearish) directions are handled correctly.
4. When structural targets are missing, status returns UNAVAILABLE rather than inventing numbers.
5. Invalidation conditions (SL on wrong side) are caught immediately.
"""
import unittest
from app.core.shortlist_engine import calculate_risk_reward


class TestRiskRewardIndependent(unittest.TestCase):

    def test_independent_long_risk_reward(self):
        """LONG setup: Stop < Entry < Target."""
        entry = 24850.0
        stop_loss = 24800.0   # Risk = 50.0 pts
        target = 24950.0      # Reward = 100.0 pts
        
        rr = calculate_risk_reward(entry=entry, stop_loss=stop_loss, target=target, direction="LONG")
        self.assertTrue(rr["is_valid"])
        self.assertEqual(rr["status"], "VALID")
        self.assertEqual(rr["direction"], "LONG")
        self.assertEqual(rr["risk_distance"], 50.0)
        self.assertEqual(rr["reward_distance"], 100.0)
        self.assertEqual(rr["risk_reward_ratio"], 2.0)
        self.assertEqual(rr["risk_reward_str"], "1 : 2.0")
        self.assertTrue(rr["is_favorable"])

    def test_independent_short_risk_reward(self):
        """SHORT setup: Target < Entry < Stop."""
        entry = 24850.0
        stop_loss = 24900.0   # Risk = 50.0 pts
        target = 24725.0      # Reward = 125.0 pts
        
        rr = calculate_risk_reward(entry=entry, stop_loss=stop_loss, target=target, direction="SHORT")
        self.assertTrue(rr["is_valid"])
        self.assertEqual(rr["status"], "VALID")
        self.assertEqual(rr["direction"], "SHORT")
        self.assertEqual(rr["risk_distance"], 50.0)
        self.assertEqual(rr["reward_distance"], 125.0)
        self.assertEqual(rr["risk_reward_ratio"], 2.5)
        self.assertEqual(rr["risk_reward_str"], "1 : 2.5")
        self.assertTrue(rr["is_favorable"])

    def test_unavailable_target_handling(self):
        """If structural target is None or on the wrong side, return UNAVAILABLE."""
        entry = 24850.0
        stop_loss = 24800.0
        
        # Target is None
        rr_none = calculate_risk_reward(entry=entry, stop_loss=stop_loss, target=None, direction="LONG")
        self.assertEqual(rr_none["status"], "UNAVAILABLE")
        self.assertFalse(rr_none["is_valid"])
        self.assertIsNone(rr_none["risk_reward_ratio"])
        self.assertEqual(rr_none["risk_reward_str"], "UNAVAILABLE")
        
        # Target is below entry for a Long trade
        rr_invalid_tgt = calculate_risk_reward(entry=entry, stop_loss=stop_loss, target=24820.0, direction="LONG")
        self.assertEqual(rr_invalid_tgt["status"], "UNAVAILABLE")

    def test_invalid_stop_loss_detection(self):
        """Stop loss on the wrong side must trigger an immediate invalidation."""
        entry = 24850.0
        
        # Long trade with Stop Loss above entry
        rr_long_bad_sl = calculate_risk_reward(entry=entry, stop_loss=24870.0, target=24950.0, direction="LONG")
        self.assertEqual(rr_long_bad_sl["status"], "INVALID_STOP_LOSS")
        self.assertFalse(rr_long_bad_sl["is_valid"])
        
        # Short trade with Stop Loss below entry
        rr_short_bad_sl = calculate_risk_reward(entry=entry, stop_loss=24820.0, target=24700.0, direction="SHORT")
        self.assertEqual(rr_short_bad_sl["status"], "INVALID_STOP_LOSS")
        self.assertFalse(rr_short_bad_sl["is_valid"])

    def test_no_tautological_fixed_multiplier(self):
        """
        Verifies that R:R varies genuinely based on target location,
        proving it is not derived from a hardcoded multiplier.
        """
        entry = 100.0
        sl = 90.0 # Risk = 10.0
        
        rr_tight = calculate_risk_reward(entry=entry, stop_loss=sl, target=105.0, direction="LONG")
        self.assertEqual(rr_tight["risk_reward_ratio"], 0.5)
        self.assertFalse(rr_tight["is_favorable"])  # 0.5 < 1.5
        
        rr_wide = calculate_risk_reward(entry=entry, stop_loss=sl, target=130.0, direction="LONG")
        self.assertEqual(rr_wide["risk_reward_ratio"], 3.0)
        self.assertTrue(rr_wide["is_favorable"])   # 3.0 >= 1.5


if __name__ == "__main__":
    unittest.main()
