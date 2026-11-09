"""
TradeLens AI - Phase 0 Test Suite: Probability and Confidence Claims.
Verifies that:
1. Hardcoded mock sample sizes (e.g. 428, 512) are removed from default output.
2. Fabricated win rates are labeled UNAVAILABLE or None until real historical backtesting is run.
3. Rule-based confluence scores (0-100) are explicitly distinguished from empirical win rates.
4. Mathematical expectancy formula calculation is tested independently.
"""
import unittest
from app.core.probability import calculate_expectancy, compute_setup_probability_metrics


class TestProbabilityClaims(unittest.TestCase):

    def test_expectancy_mathematical_formula(self):
        """
        EV = (P * Net Win) - ((1 - P) * Net Loss)
        Let P = 0.60 (60%), Avg Win = 4.0%, Avg Loss = 2.0%, Friction = 0.15%
        Net Win = 4.0 - 0.15 = 3.85%
        Net Loss = 2.0 + 0.15 = 2.15%
        EV = (0.60 * 3.85) - (0.40 * 2.15) = 2.31 - 0.86 = 1.45%
        Profit Factor = (0.60 * 3.85) / (0.40 * 2.15) = 2.31 / 0.86 = 2.69
        """
        exp = calculate_expectancy(win_rate=60.0, avg_win_pct=4.0, avg_loss_pct=2.0, friction_pct=0.15)
        self.assertLess(abs(exp["expected_value_pct"] - 1.45), 0.02)
        self.assertLess(abs(exp["profit_factor"] - 2.69), 0.05)
        self.assertTrue(exp["is_positive_expectancy"])

    def test_removal_of_fabricated_sample_sizes(self):
        """
        Without real historical backtest datasets, compute_setup_probability_metrics
        must NOT display hardcoded 428 sample sizes or fake target-first percentages.
        """
        metrics = compute_setup_probability_metrics(
            setup_name="Pullback to 20 EMA + Support",
            rsi=60.0,
            price_above_ema50=True,
            rvol=1.6,
            risk_reward_ratio=2.0,
            historical_dataset_stats=None
        )
        # Must report confluence score, NOT fabricated backtest win rate
        self.assertIn("confluence_score", metrics)
        self.assertGreater(metrics["confluence_score"], 50.0)
        self.assertIsNone(metrics["sample_size"], "Sample size must be None until real backtesting is performed")
        self.assertIsNone(metrics["target_first_rate_pct"], "Target-first rate must be None without backtesting")
        self.assertEqual(metrics["backtest_status"], "UNVALIDATED_RULE_ESTIMATE")
        self.assertIn("disclaimer", metrics)

    def test_real_historical_stats_when_provided(self):
        """When genuine backtested statistics are supplied, they are formatted properly."""
        real_stats = {
            "sample_size": 150,
            "target_first_rate_pct": 58.0,
            "stop_first_rate_pct": 36.0,
            "unresolved_rate_pct": 6.0,
            "avg_win_pct": 3.2,
            "avg_loss_pct": 1.6
        }
        metrics = compute_setup_probability_metrics(
            setup_name="Breakout",
            rsi=62.0,
            price_above_ema50=True,
            rvol=1.8,
            risk_reward_ratio=2.0,
            historical_dataset_stats=real_stats
        )
        self.assertEqual(metrics["sample_size"], 150)
        self.assertEqual(metrics["target_first_rate_pct"], 58.0)
        self.assertEqual(metrics["backtest_status"], "VALIDATED_HISTORICAL_DATA")


if __name__ == "__main__":
    unittest.main()
