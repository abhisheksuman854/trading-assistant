"""
TradeLens AI - Phase 2C Multi-Timeframe Setup Scoring & Opportunity Engine Tests
================================================================================
Comprehensive test suite validating independent bullish/bearish scoring,
orthogonal weight ceilings, fail-closed safety gates, structural risk/reward,
and option contract tradability validation.
"""
import unittest
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Any

from app.data.candle_store import MultiTimeframeCandleStore, Candle, Timeframe
from app.core.indicators import TechnicalIndicatorService
from app.core.market_structure import (
    MarketStructureService,
    StructureType,
    StructuralBreakType,
    MarketRegime,
    DivergenceType,
    BreakLifecycleState,
    DivergenceInvalidationStatus
)
from app.core.setup_scoring import (
    SetupScoringService,
    SetupDirection,
    SetupQualityTier,
    TradeActionRecommendation,
    GateFailureReason,
    EvidenceFamily,
    CanonicalGateID,
    TargetType,
    SqueezeLifecycleState,
    QuoteFreshnessThresholds,
    get_evidence_family_registry,
    score_directional_confluence,
    evaluate_fail_closed_gates,
    evaluate_canonical_eight_gates,
    calculate_structural_trade_levels,
    calculate_structural_target,
    project_option_premium_change,
    validate_option_contract_against_master,
    MIN_QUALIFIED_SCORE,
    HIGH_QUALITY_SCORE,
    MIN_RISK_REWARD
)
from app.core.options_analytics import verify_option_contract_tradability


class TestSetupScoringEngine(unittest.TestCase):

    def setUp(self):
        self.store = MultiTimeframeCandleStore()
        self.ind_service = TechnicalIndicatorService(self.store)
        self.struct_service = MarketStructureService(self.store, self.ind_service)
        self.scoring_service = SetupScoringService(self.store, self.struct_service, self.ind_service)
        self.symbol = "NIFTY 50"
        self.store._ensure_symbol_initialized(self.symbol)

    def _populate_store_with_candles(
        self,
        trend: str = "BULLISH",
        bars_per_tf: int = 50,
        base_price: float = 24000.0,
        has_feed_gap: bool = False
    ):
        """Populates multi-timeframe store with synthetic structured candles."""
        ist = timezone(timedelta(hours=5, minutes=30))
        base_time = datetime(2026, 11, 9, 9, 15, 0, tzinfo=ist)
        timeframes = [Timeframe.S15, Timeframe.M1, Timeframe.M3, Timeframe.M5, Timeframe.M15]
        tf_seconds = {Timeframe.S15: 15, Timeframe.M1: 60, Timeframe.M3: 180, Timeframe.M5: 300, Timeframe.M15: 900}

        for tf in timeframes:
            delta_sec = tf_seconds[tf]
            candles = []
            for i in range(bars_per_tf):
                step = (i * 2.5) if trend == "BULLISH" else (-(i * 2.5) if trend == "BEARISH" else 0.0)
                p = base_price + step
                sec_offset = i * delta_sec
                if has_feed_gap and i >= 20:
                    sec_offset += (delta_sec * 5)  # Inject 5-interval gap
                s_dt = base_time + timedelta(seconds=sec_offset)
                e_dt = s_dt + timedelta(seconds=delta_sec)

                c = Candle(
                    symbol=self.symbol,
                    timeframe=tf,
                    start_time=s_dt,
                    end_time=e_dt,
                    open_price=round(p, 2),
                    high_price=round(p + 5.0, 2),
                    low_price=round(p - 5.0, 2),
                    close_price=round(p + 2.0, 2),
                    volume=0,
                    is_closed=True,
                    candle_status="HISTORICAL"
                )
                candles.append(c)
            self.store._closed_candles[self.symbol][tf] = candles

    # =========================================================================
    # 1. Independent Scoring & Category Weight Ceilings Tests
    # =========================================================================

    def test_independent_bullish_and_bearish_scoring(self):
        """
        Verifies that Bullish and Bearish scores are evaluated independently on 0-100 scales
        without mutual canceling.
        """
        self._populate_store_with_candles(trend="BULLISH", bars_per_tf=60)
        res = self.scoring_service.evaluate_opportunity(self.symbol)

        bull_eval = res["bullish_assessment"]
        bear_eval = res["bearish_assessment"]

        self.assertIn("total_score", bull_eval)
        self.assertIn("total_score", bear_eval)
        self.assertGreaterEqual(bull_eval["total_score"], 0.0)
        self.assertLessEqual(bull_eval["total_score"], 100.0)
        self.assertGreaterEqual(bear_eval["total_score"], 0.0)
        self.assertLessEqual(bear_eval["total_score"], 100.0)

        # Bullish trend must produce higher Bullish score than Bearish score
        self.assertGreater(bull_eval["total_score"], bear_eval["total_score"])

    def test_orthogonal_category_weight_ceilings(self):
        """
        Verifies strict category weight ceilings to prevent double counting:
        - Category 1 (Macro): Max 30 pts
        - Category 2 (Intermediate): Max 30 pts
        - Category 3 (Volatility/Location): Max 20 pts
        - Category 4 (Divergence/Timing): Max 20 pts
        """
        self._populate_store_with_candles(trend="BULLISH", bars_per_tf=60)
        res = self.scoring_service.evaluate_opportunity(self.symbol)
        cats = res["bullish_assessment"]["category_scores"]

        self.assertLessEqual(cats["macro_context_trend"], 30.0)
        self.assertLessEqual(cats["intermediate_structure_momentum"], 30.0)
        self.assertLessEqual(cats["volatility_value_location"], 20.0)
        self.assertLessEqual(cats["divergence_timing"], 20.0)

    # =========================================================================
    # 2. Confluence Quality Tier Classification Tests
    # =========================================================================

    def test_quality_tiers_thresholds(self):
        """
        Tier 1: >= 75 pts (High Quality)
        Tier 2: 60 - 74 pts (Moderate)
        Tier 3: < 60 pts (Weak / Unqualified)
        """
        dummy_mtf = {"timeframe_analyses": {}}
        dummy_ind = {}

        eval_weak = score_directional_confluence(SetupDirection.BULLISH, dummy_mtf, dummy_ind)
        self.assertEqual(eval_weak["quality_tier"], SetupQualityTier.TIER_3_WEAK)
        self.assertFalse(eval_weak["is_qualified"])

    # =========================================================================
    # 3. Fail-Closed Safety Gates Tests
    # =========================================================================

    def test_gate_squeeze_compression_forces_no_trade(self):
        """
        Carter's Squeeze Gate:
        When 5m or 15m is in VOLATILITY_COMPRESSION, trade action MUST be NO_TRADE
        regardless of confluence score.
        """
        mtf_suite = {
            "timeframe_analyses": {
                Timeframe.M5: {"regime": {"regime": MarketRegime.VOLATILITY_COMPRESSION}},
                Timeframe.M15: {"regime": {"regime": MarketRegime.VOLATILITY_COMPRESSION}}
            }
        }
        bull_eval = {"total_score": 85.0}  # Very high score
        bear_eval = {"total_score": 20.0}
        levels = {"is_valid_levels": True, "risk_reward_ratio": 2.2}

        passes, rejections = evaluate_fail_closed_gates(
            direction=SetupDirection.BULLISH,
            bullish_eval=bull_eval,
            bearish_eval=bear_eval,
            mtf_suite=mtf_suite,
            structural_levels=levels
        )
        self.assertFalse(passes)
        self.assertTrue(any(GateFailureReason.SQUEEZE_COMPRESSION in r for r in rejections))

    def test_gate_conflicting_mtf_regimes_forces_no_trade(self):
        """
        Macro Conflict Gate:
        When 15m Anchor regime is TRENDING_BEARISH, a Bullish setup MUST fail.
        """
        mtf_suite = {
            "timeframe_analyses": {
                Timeframe.M15: {"regime": {"regime": MarketRegime.TRENDING_BEARISH}},
                Timeframe.M5: {"regime": {"regime": MarketRegime.TRENDING_BULLISH}}
            }
        }
        bull_eval = {"total_score": 78.0}
        bear_eval = {"total_score": 30.0}
        levels = {"is_valid_levels": True, "risk_reward_ratio": 2.0}

        passes, rejections = evaluate_fail_closed_gates(
            direction=SetupDirection.BULLISH,
            bullish_eval=bull_eval,
            bearish_eval=bear_eval,
            mtf_suite=mtf_suite,
            structural_levels=levels
        )
        self.assertFalse(passes)
        self.assertTrue(any(GateFailureReason.CONFLICTING_MTF_REGIMES in r for r in rejections))

    def test_gate_high_mutual_confluence_forces_no_trade(self):
        """
        Mutual Confluence Gate:
        When Bullish score >= 60 AND Bearish score >= 60, market is in violent whipsaw -> NO_TRADE.
        """
        mtf_suite = {"timeframe_analyses": {}}
        bull_eval = {"total_score": 72.0}
        bear_eval = {"total_score": 68.0}
        levels = {"is_valid_levels": True, "risk_reward_ratio": 2.0}

        passes, rejections = evaluate_fail_closed_gates(
            direction=SetupDirection.BULLISH,
            bullish_eval=bull_eval,
            bearish_eval=bear_eval,
            mtf_suite=mtf_suite,
            structural_levels=levels
        )
        self.assertFalse(passes)
        self.assertTrue(any(GateFailureReason.HIGH_MUTUAL_CONFLUENCE in r for r in rejections))

    def test_gate_unfavorable_risk_reward_forces_no_trade(self):
        """
        Structural Risk/Reward Gate:
        When R:R < 1.50, setup MUST fail tradability gate.
        """
        mtf_suite = {"timeframe_analyses": {}}
        bull_eval = {"total_score": 80.0}
        bear_eval = {"total_score": 20.0}
        levels = {"is_valid_levels": True, "risk_reward_ratio": 1.25}  # Below 1.50

        passes, rejections = evaluate_fail_closed_gates(
            direction=SetupDirection.BULLISH,
            bullish_eval=bull_eval,
            bearish_eval=bear_eval,
            mtf_suite=mtf_suite,
            structural_levels=levels
        )
        self.assertFalse(passes)
        self.assertTrue(any(GateFailureReason.UNFAVORABLE_RISK_REWARD in r for r in rejections))

    def test_gate_developing_unconfirmed_candle_segregation(self):
        """
        Developing Candle Gate:
        A setup on an active, unclosed bar (is_candle_closed=False) MUST be rejected with
        DEVELOPING_UNCONFIRMED_BAR.
        """
        mtf_suite = {"timeframe_analyses": {}}
        bull_eval = {"total_score": 82.0}
        bear_eval = {"total_score": 15.0}
        levels = {"is_valid_levels": True, "risk_reward_ratio": 2.5}

        passes, rejections = evaluate_fail_closed_gates(
            direction=SetupDirection.BULLISH,
            bullish_eval=bull_eval,
            bearish_eval=bear_eval,
            mtf_suite=mtf_suite,
            structural_levels=levels,
            is_candle_closed=False
        )
        self.assertFalse(passes)
        self.assertTrue(any(GateFailureReason.DEVELOPING_UNCONFIRMED_BAR in r for r in rejections))

    def test_gate_feed_gap_forces_no_trade(self):
        """
        Data Quality Gate:
        When structure spans an unverified feed gap, setup MUST fail with FEED_GAP_OR_UNRELIABLE_DATA.
        """
        mtf_suite = {
            "timeframe_analyses": {
                Timeframe.M5: {"structural_break": {"data_quality_status": "FEED_GAP"}}
            }
        }
        bull_eval = {"total_score": 80.0}
        bear_eval = {"total_score": 10.0}
        levels = {"is_valid_levels": True, "risk_reward_ratio": 2.0}

        passes, rejections = evaluate_fail_closed_gates(
            direction=SetupDirection.BULLISH,
            bullish_eval=bull_eval,
            bearish_eval=bear_eval,
            mtf_suite=mtf_suite,
            structural_levels=levels
        )
        self.assertFalse(passes)
        self.assertTrue(any(GateFailureReason.FEED_GAP_OR_UNRELIABLE_DATA in r for r in rejections))

    # =========================================================================
    # 4. Structural Trade Levels & Risk/Reward Validation Tests
    # =========================================================================

    def test_structural_long_trade_levels(self):
        """
        Bullish Trade Levels:
        Stop-Loss must be strictly < Entry < Target.
        """
        mtf_suite = {
            "timeframe_analyses": {
                Timeframe.M5: {
                    "structure": {
                        "latest_confirmed_low": {"pivot_price": 24150.0},
                        "latest_confirmed_high": {"pivot_price": 24400.0}
                    }
                }
            }
        }
        indicators = {"volatility": {"atr": 10.0}}
        cmp_price = 24250.0

        levels = calculate_structural_trade_levels(SetupDirection.BULLISH, cmp_price, mtf_suite, indicators)
        self.assertTrue(levels["is_valid_levels"])
        self.assertEqual(levels["entry_price"], 24250.0)
        self.assertLess(levels["stop_loss_price"], levels["entry_price"])
        self.assertGreater(levels["target_price"], levels["entry_price"])
        self.assertGreaterEqual(levels["risk_reward_ratio"], 1.0)

    def test_structural_short_trade_levels(self):
        """
        Bearish Trade Levels:
        Target must be strictly < Entry < Stop-Loss.
        """
        mtf_suite = {
            "timeframe_analyses": {
                Timeframe.M5: {
                    "structure": {
                        "latest_confirmed_low": {"pivot_price": 23900.0},
                        "latest_confirmed_high": {"pivot_price": 24200.0}
                    }
                }
            }
        }
        indicators = {"volatility": {"atr": 10.0}}
        cmp_price = 24100.0

        levels = calculate_structural_trade_levels(SetupDirection.BEARISH, cmp_price, mtf_suite, indicators)
        self.assertTrue(levels["is_valid_levels"])
        self.assertEqual(levels["entry_price"], 24100.0)
        self.assertGreater(levels["stop_loss_price"], levels["entry_price"])
        self.assertLess(levels["target_price"], levels["entry_price"])
        self.assertGreaterEqual(levels["risk_reward_ratio"], 1.0)

    def test_invalid_stop_loss_placement_detected(self):
        """
        If swing low is higher than entry price (corrupted pivot), levels must be marked invalid.
        """
        mtf_suite = {
            "timeframe_analyses": {
                Timeframe.M5: {
                    "structure": {
                        "latest_confirmed_low": {"pivot_price": 24300.0},  # Higher than entry!
                        "latest_confirmed_high": {"pivot_price": 24400.0}
                    }
                }
            }
        }
        indicators = {"volatility": {"atr": 10.0}}
        cmp_price = 24250.0

        levels = calculate_structural_trade_levels(SetupDirection.BULLISH, cmp_price, mtf_suite, indicators)
        self.assertFalse(levels["is_valid_levels"])
        self.assertIsNone(levels["risk_reward_ratio"])

    # =========================================================================
    # 5. Option Contract Tradability Gate Integration Tests
    # =========================================================================

    def test_option_tradability_liquid_passes(self):
        """
        Valid liquid option contract (LTP > 0, spread <= 4%, high OI and volume)
        must pass tradability gate.
        """
        liquid_quote = {
            "symbol": "NIFTY26NOV24300CE",
            "ltp": 150.0,
            "bid": 149.5,
            "ask": 150.5,
            "oi": 120000,
            "volume": 45000,
            "last_trade_time": datetime.now()
        }
        mtf_suite = {"timeframe_analyses": {}}
        bull_eval = {"total_score": 82.0}
        bear_eval = {"total_score": 15.0}
        levels = {"is_valid_levels": True, "risk_reward_ratio": 2.1}

        passes, rejections = evaluate_fail_closed_gates(
            direction=SetupDirection.BULLISH,
            bullish_eval=bull_eval,
            bearish_eval=bear_eval,
            mtf_suite=mtf_suite,
            structural_levels=levels,
            option_tradability=verify_option_contract_tradability(liquid_quote)
        )
        self.assertTrue(passes)
        self.assertEqual(len(rejections), 0)

    def test_option_tradability_illiquid_wide_spread_fails(self):
        """
        Illiquid option contract with excessive spread (> 4%) MUST fail gate.
        """
        illiquid_quote = {
            "symbol": "NIFTY26NOV25000CE",
            "ltp": 20.0,
            "bid": 18.0,
            "ask": 22.0,  # Spread 4.0 / 20.0 = 20% > 4%
            "oi": 15000,
            "volume": 800,
            "last_trade_time": datetime.now()
        }
        mtf_suite = {"timeframe_analyses": {}}
        bull_eval = {"total_score": 82.0}
        bear_eval = {"total_score": 15.0}
        levels = {"is_valid_levels": True, "risk_reward_ratio": 2.1}

        passes, rejections = evaluate_fail_closed_gates(
            direction=SetupDirection.BULLISH,
            bullish_eval=bull_eval,
            bearish_eval=bear_eval,
            mtf_suite=mtf_suite,
            structural_levels=levels,
            option_tradability=verify_option_contract_tradability(illiquid_quote)
        )
        self.assertFalse(passes)
        self.assertTrue(any(GateFailureReason.UNTRADABLE_OPTION_CONTRACT in r for r in rejections))

    # =========================================================================
    # 6. Complete End-to-End Evaluation & Transparent Explanations
    # =========================================================================

    def test_end_to_end_qualified_setup_explanation(self):
        """
        End-to-end evaluation producing QUALIFIED_SETUP must generate a rich,
        transparent qualitative explanation detailing entry, stop, target, R:R, and confluence.
        """
        self._populate_store_with_candles(trend="BULLISH", bars_per_tf=60)
        liquid_quote = {
            "symbol": "NIFTY26NOV24300CE",
            "ltp": 160.0, "bid": 159.5, "ask": 160.5,
            "oi": 150000, "volume": 60000,
            "last_trade_time": datetime.now()
        }
        res = self.scoring_service.evaluate_opportunity(self.symbol, option_quote=liquid_quote)

        self.assertIn("explanation", res)
        self.assertIn("TradeLens AI Opportunity Evaluation", res["explanation"])
        self.assertIn("Confluence Scores", res["explanation"])


# =============================================================================
# Phase 2C Final Hardening Dedicated Test Suites
# =============================================================================

class TestEvidenceFamilyCorrelationProtection(unittest.TestCase):
    """
    Validates evidence-family scoring and capping within each of the 4 categories
    to prevent correlated indicators from stacking points for the same condition.
    """

    def test_trend_alignment_family_capping(self):
        """
        In Category 1, multiple correlated trend indicators (15m + 5m Regimes: raw 20 pts)
        must not exceed the Trend Alignment family ceiling of 15.0 pts.
        """
        mtf_suite = {
            "timeframe_analyses": {
                Timeframe.M15: {
                    "regime": {"regime": MarketRegime.TRENDING_BULLISH},
                    "supertrend": {"direction": "BULLISH"},
                    "structure": {"structural_bias": "BULLISH"}
                },
                Timeframe.M5: {
                    "regime": {"regime": MarketRegime.TRENDING_BULLISH},
                    "structure": {"structural_bias": "BULLISH", "last_event": StructuralBreakType.BOS_BULLISH}
                }
            }
        }
        indicators_by_tf = {
            Timeframe.M15: {
                "trend": {"supertrend": {"direction": "BULLISH"}, "ema_20": 24200.0}
            },
            Timeframe.M5: {
                "trend": {"ema_20": 24200.0},
                "vwap": {"current_vwap": 24190.0}
            }
        }
        res = score_directional_confluence(SetupDirection.BULLISH, mtf_suite, indicators_by_tf, cmp_price=24250.0)
        fam1 = res["evidence_family_breakdown"]["category_1"]
        self.assertLessEqual(fam1[EvidenceFamily.TREND_ALIGNMENT]["capped"], 15.0)
        self.assertEqual(fam1[EvidenceFamily.TREND_ALIGNMENT]["capped"], 15.0)

    def test_market_structure_family_capping(self):
        """
        In Category 1, multiple structural confirmations (15m and 5m structures confirmed)
        must not exceed the Market Structure family ceiling of 15.0 pts.
        """
        mtf_suite = {
            "timeframe_analyses": {
                Timeframe.M15: {
                    "regime": {"regime": MarketRegime.TRENDING_BULLISH},
                    "structure": {
                        "structure_state": StructureType.BULLISH,
                        "structural_bias": "BULLISH"
                    }
                },
                Timeframe.M5: {
                    "structure": {
                        "structure_state": StructureType.BULLISH,
                        "structural_bias": "BULLISH"
                    }
                }
            }
        }
        indicators_by_tf = {}
        res = score_directional_confluence(SetupDirection.BULLISH, mtf_suite, indicators_by_tf, cmp_price=24250.0)
        fam1 = res["evidence_family_breakdown"]["category_1"]
        self.assertLessEqual(fam1[EvidenceFamily.MARKET_STRUCTURE]["capped"], 15.0)
        self.assertEqual(fam1[EvidenceFamily.MARKET_STRUCTURE]["capped"], 15.0)

    def test_category_1_combined_ceiling(self):
        """
        Even when both Trend Alignment (15 pts) and Market Structure (15 pts) are maxed,
        Category 1 total score must strictly cap at 30.0 pts.
        """
        mtf_suite = {
            "timeframe_analyses": {
                Timeframe.M15: {
                    "regime": {"regime": MarketRegime.TRENDING_BULLISH},
                    "supertrend": {"direction": "BULLISH"},
                    "structure": {"structural_bias": "BULLISH"}
                },
                Timeframe.M5: {
                    "structure": {"structural_bias": "BULLISH", "last_event": StructuralBreakType.BOS_BULLISH}
                }
            }
        }
        indicators_by_tf = {
            Timeframe.M15: {"trend": {"supertrend": {"direction": "BULLISH"}, "ema_20": 24200.0}},
            Timeframe.M5: {"trend": {"ema_20": 24200.0}, "vwap": {"current_vwap": 24190.0}}
        }
        res = score_directional_confluence(SetupDirection.BULLISH, mtf_suite, indicators_by_tf, cmp_price=24250.0)
        cat1_score = res["category_scores"]["macro_context_trend"]
        self.assertLessEqual(cat1_score, 30.0)

    def test_category_2_directional_strength_and_momentum_capping(self):
        """
        In Category 2, Directional Strength (ADX/DMI) is capped at 5 pts,
        and Momentum (RSI/MACD) is capped at 5 pts, with category total capped at 30.0 pts.
        """
        mtf_suite = {"timeframe_analyses": {}}
        indicators_by_tf = {
            Timeframe.M5: {
                "trend": {"adx": {"adx": 35.0, "plus_di": 30.0, "minus_di": 10.0}},
                "momentum": {
                    "rsi": {"value": 62.0},
                    "macd": {"histogram": 5.2, "macd": 10.0, "signal": 4.8}
                }
            },
            Timeframe.M1: {
                "momentum": {"rsi": {"value": 58.0}}
            }
        }
        res = score_directional_confluence(SetupDirection.BULLISH, mtf_suite, indicators_by_tf, cmp_price=24250.0)
        fam2 = res["evidence_family_breakdown"]["category_2"]
        cat2_score = res["category_scores"]["intermediate_structure_momentum"]
        self.assertLessEqual(fam2[EvidenceFamily.DIRECTIONAL_STRENGTH]["capped"], 5.0)
        self.assertLessEqual(fam2[EvidenceFamily.MOMENTUM]["capped"], 5.0)
        self.assertLessEqual(cat2_score, 30.0)


class TestUnderlyingVersusOptionRiskReward(unittest.TestCase):
    """
    Validates explicit mathematical separation between Underlying Structural R:R
    (NIFTY index points) and Option Premium R:R (rupee options).
    """

    def setUp(self):
        self.mtf_suite = {
            "timeframe_analyses": {
                Timeframe.M5: {
                    "structure": {
                        "latest_confirmed_low": {"pivot_price": 24150.0},
                        "latest_confirmed_high": {"pivot_price": 24400.0}
                    }
                }
            }
        }
        self.indicators = {"volatility": {"atr": 10.0}}
        self.cmp_price = 24250.0

    def test_option_rr_marked_unavailable_without_quote(self):
        """
        When reliable option quotes are absent, option R:R must be explicitly UNAVAILABLE,
        while underlying structural R:R is fully computed in index points.
        """
        levels = calculate_structural_trade_levels(
            direction=SetupDirection.BULLISH,
            cmp_price=self.cmp_price,
            mtf_suite=self.mtf_suite,
            indicators=self.indicators,
            option_quote=None
        )

        # Underlying R:R must be valid in index points
        underlying = levels["underlying_risk_reward"]
        self.assertTrue(underlying["is_valid_levels"])
        self.assertEqual(underlying["entry_price"], 24250.0)
        self.assertEqual(underlying["stop_loss_price"], 24148.0) # 24150 - 2.0 buffer
        self.assertEqual(underlying["target_price"], 24400.0)
        self.assertEqual(underlying["risk_points"], 102.0)
        self.assertEqual(underlying["reward_points"], 150.0)
        self.assertAlmostEqual(underlying["risk_reward_ratio"], 1.47, places=2)

        # Option R:R must be explicitly marked UNAVAILABLE
        opt = levels["option_premium_risk_reward"]
        self.assertEqual(opt["status"], "UNAVAILABLE")
        self.assertIsNone(opt["option_risk_reward_ratio"])
        self.assertEqual(opt["option_risk_reward_str"], "UNAVAILABLE")
        self.assertIn("cannot be fabricated from underlying points", opt["explanation"])

    def test_option_rr_estimated_with_valid_quote_and_delta(self):
        """
        When valid option quote is provided, option premium R:R is estimated via
        Delta/Gamma Taylor projection to underlying stop and target.
        """
        now_str = datetime.now(timezone(timedelta(hours=5, minutes=30))).isoformat()
        quote = {
            "symbol": "NIFTY26NOV24300CE",
            "strike": 24300.0,
            "option_type": "CE",
            "ltp": 150.0,
            "bid": 149.0,
            "ask": 151.0,
            "oi": 120000,
            "volume": 45000,
            "last_trade_time": now_str
        }
        levels = calculate_structural_trade_levels(
            direction=SetupDirection.BULLISH,
            cmp_price=self.cmp_price,
            mtf_suite=self.mtf_suite,
            indicators=self.indicators,
            option_quote=quote
        )

        opt = levels["option_premium_risk_reward"]
        self.assertEqual(opt["status"], "VALID_ESTIMATE")
        self.assertEqual(opt["contract_symbol"], "NIFTY26NOV24300CE")
        self.assertEqual(opt["option_entry_price"], 151.0) # Uses Ask for buying
        self.assertIsNotNone(opt["option_stop_loss_estimate"])
        self.assertIsNotNone(opt["option_target_estimate"])
        self.assertLess(opt["option_stop_loss_estimate"], opt["option_entry_price"])
        self.assertGreater(opt["option_target_estimate"], opt["option_entry_price"])
        self.assertGreater(opt["option_risk_reward_ratio"], 0.5)
        self.assertIsNotNone(opt["delta_applied"])

    def test_underlying_and_option_risk_points_decoupled(self):
        """
        Verifies that underlying risk points (NIFTY index points) and option risk points (rupees)
        are never conflated or substituted for each other.
        """
        now_str = datetime.now(timezone(timedelta(hours=5, minutes=30))).isoformat()
        quote = {
            "symbol": "NIFTY26NOV24300CE",
            "strike": 24300.0,
            "option_type": "CE",
            "ltp": 150.0,
            "bid": 149.0,
            "ask": 151.0,
            "oi": 120000,
            "volume": 45000,
            "last_trade_time": now_str
        }
        levels = calculate_structural_trade_levels(
            direction=SetupDirection.BULLISH,
            cmp_price=self.cmp_price,
            mtf_suite=self.mtf_suite,
            indicators=self.indicators,
            option_quote=quote
        )
        underlying = levels["underlying_risk_reward"]
        opt = levels["option_premium_risk_reward"]

        self.assertNotEqual(underlying["risk_points"], opt["option_risk_points"])
        self.assertNotEqual(underlying["reward_points"], opt["option_reward_points"])


class TestOptionContractMasterValidation(unittest.TestCase):
    """
    Validates option contract cross-checks against broker contract master:
    underlying mapping, option type, expiry, strike step, orderbook, and status.
    """

    def setUp(self):
        self.now_ist = datetime(2026, 11, 9, 10, 30, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))

    def test_valid_liquid_contract_passes(self):
        """Standard liquid NIFTY CE quote with valid parameters passes validation."""
        contract = {
            "symbol": "NIFTY26NOV24200CE",
            "option_type": "CE",
            "strike": 24200.0,
            "ltp": 180.0,
            "bid": 179.5,
            "ask": 180.5,
            "oi": 150000,
            "volume": 45000,
            "last_trade_time": self.now_ist.isoformat(),
            "status": "ACTIVE"
        }
        res = validate_option_contract_against_master(
            contract,
            underlying_symbol="NIFTY 50",
            expected_option_type="CE",
            current_dt=self.now_ist
        )
        self.assertTrue(res["is_tradable"])
        self.assertEqual(res["tradability_status"], "TRADABLE_LIQUID")
        self.assertEqual(len(res["rejection_reasons"]), 0)

    def test_mismatched_option_type_rejected(self):
        """Setup direction expects CE (bullish), but PE contract is provided -> rejected."""
        contract = {
            "symbol": "NIFTY26NOV24200PE",
            "option_type": "PE",
            "strike": 24200.0,
            "ltp": 120.0,
            "bid": 119.5,
            "ask": 120.5,
            "oi": 100000,
            "volume": 30000,
            "last_trade_time": self.now_ist.isoformat()
        }
        res = validate_option_contract_against_master(
            contract,
            underlying_symbol="NIFTY 50",
            expected_option_type="CE",
            current_dt=self.now_ist
        )
        self.assertFalse(res["is_tradable"])
        self.assertTrue(any("Option type mismatch" in r for r in res["rejection_reasons"]))

    def test_non_standard_strike_step_rejected(self):
        """NIFTY 50 strike interval is 50.0. A non-standard strike of 24125.0 must be rejected."""
        contract = {
            "symbol": "NIFTY26NOV24125CE",
            "option_type": "CE",
            "strike": 24125.0,  # Invalid strike step for NIFTY
            "ltp": 210.0,
            "bid": 209.0,
            "ask": 211.0,
            "oi": 80000,
            "volume": 20000,
            "last_trade_time": self.now_ist.isoformat()
        }
        res = validate_option_contract_against_master(
            contract,
            underlying_symbol="NIFTY 50",
            expected_option_type="CE",
            current_dt=self.now_ist
        )
        self.assertFalse(res["is_tradable"])
        self.assertTrue(any("does not align with standard interval step" in r for r in res["rejection_reasons"]))

    def test_inactive_contract_status_rejected(self):
        """Suspended or expired contract master status must fail closed."""
        contract = {
            "symbol": "NIFTY26NOV24200CE",
            "option_type": "CE",
            "strike": 24200.0,
            "ltp": 180.0,
            "bid": 179.5,
            "ask": 180.5,
            "oi": 150000,
            "volume": 45000,
            "last_trade_time": self.now_ist.isoformat(),
            "status": "SUSPENDED"
        }
        res = validate_option_contract_against_master(
            contract,
            underlying_symbol="NIFTY 50",
            current_dt=self.now_ist
        )
        self.assertFalse(res["is_tradable"])
        self.assertTrue(any("Contract master status is SUSPENDED" in r for r in res["rejection_reasons"]))


class TestQuoteFreshnessAndMidPriceSpread(unittest.TestCase):
    """
    Validates numerical quote freshness thresholds (15s strict, 60s latency, 180s stale)
    and Mid-Price reference spread calculation (Bid + Ask) / 2.0.
    """

    def setUp(self):
        self.now_ist = datetime(2026, 11, 9, 10, 30, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))

    def _base_quote(self, age_seconds: float = 0.0) -> Dict[str, Any]:
        q_time = self.now_ist - timedelta(seconds=age_seconds)
        return {
            "symbol": "NIFTY26NOV24200CE",
            "option_type": "CE",
            "strike": 24200.0,
            "ltp": 180.0,
            "bid": 179.5,
            "ask": 180.5,
            "oi": 150000,
            "volume": 45000,
            "last_trade_time": q_time.isoformat(),
            "status": "ACTIVE"
        }

    def test_strict_intraday_quote_freshness_passes(self):
        """Quote aged 5.0s (<= 15.0s) is FRESH_LIVE_QUOTE with zero warnings."""
        quote = self._base_quote(age_seconds=5.0)
        res = validate_option_contract_against_master(quote, current_dt=self.now_ist)
        self.assertTrue(res["is_tradable"])
        self.assertEqual(res["freshness_status"], "FRESH_LIVE_QUOTE")
        self.assertEqual(res["quote_age_seconds"], 5.0)

    def test_acceptable_latency_quote_warns(self):
        """Quote aged 35.0s (15s < age <= 60s) has ACCEPTABLE_LATENCY with warning."""
        quote = self._base_quote(age_seconds=35.0)
        res = validate_option_contract_against_master(quote, current_dt=self.now_ist)
        self.assertTrue(res["is_tradable"])
        self.assertEqual(res["freshness_status"], "ACCEPTABLE_LATENCY")
        self.assertTrue(any("exceeds optimal threshold" in w for w in res["warnings"]))

    def test_stale_quote_over_60s_rejected(self):
        """Quote aged 90.0s (60s < age <= 180s) has STALE_REJECT and is rejected at qualification boundary."""
        quote = self._base_quote(age_seconds=90.0)
        res = validate_option_contract_against_master(quote, current_dt=self.now_ist)
        self.assertFalse(res["is_tradable"])
        self.assertEqual(res["freshness_status"], "STALE_REJECT")
        self.assertTrue(any("Quote is stale" in r for r in res["rejection_reasons"]))

    def test_stale_hard_reject(self):
        """Quote aged 250.0s (> 180.0s) has STALE_HARD_REJECT and is rejected."""
        quote = self._base_quote(age_seconds=250.0)
        res = validate_option_contract_against_master(quote, current_dt=self.now_ist)
        self.assertFalse(res["is_tradable"])
        self.assertEqual(res["freshness_status"], "STALE_HARD_REJECT")
        self.assertTrue(any("severely stale" in r for r in res["rejection_reasons"]))

    def test_missing_timestamp_fails_closed(self):
        """Quote without timestamp fails closed with MISSING_TIMESTAMP."""
        quote = self._base_quote()
        quote["last_trade_time"] = None
        res = validate_option_contract_against_master(quote, current_dt=self.now_ist)
        self.assertFalse(res["is_tradable"])
        self.assertEqual(res["freshness_status"], "MISSING_TIMESTAMP")
        self.assertTrue(any("Missing quote timestamp" in r for r in res["rejection_reasons"]))

    def test_future_timestamp_anomaly_fails_closed(self):
        """Quote with future timestamp (> 2s ahead) fails closed."""
        future_dt = self.now_ist + timedelta(seconds=15)
        quote = self._base_quote()
        quote["last_trade_time"] = future_dt.isoformat()
        res = validate_option_contract_against_master(quote, current_dt=self.now_ist)
        self.assertFalse(res["is_tradable"])
        self.assertEqual(res["freshness_status"], "FUTURE_TIMESTAMP_ANOMALY")
        self.assertTrue(any("Future timestamp anomaly" in r for r in res["rejection_reasons"]))

    def test_mid_price_spread_calculation_and_crossed_book(self):
        """
        Verifies spread percentage is calculated relative to Mid-Price = (Bid + Ask) / 2.0.
        Crossed order book (Bid > Ask) fails closed.
        """
        # Normal order book: Bid 198, Ask 202 -> Mid = 200, Spread = 4, Pct = 2.0%
        quote = self._base_quote(age_seconds=2.0)
        quote["bid"] = 198.0
        quote["ask"] = 202.0
        res = validate_option_contract_against_master(quote, current_dt=self.now_ist)
        self.assertEqual(res["mid_price"], 200.0)
        self.assertEqual(res["bid_ask_spread"], 4.0)
        self.assertEqual(res["spread_pct"], 2.0)
        self.assertEqual(res["spread_reference"], "MID_PRICE")

        # Crossed order book: Bid 205, Ask 200
        quote["bid"] = 205.0
        quote["ask"] = 200.0
        res_crossed = validate_option_contract_against_master(quote, current_dt=self.now_ist)
        self.assertFalse(res_crossed["is_tradable"])
        self.assertTrue(any("Crossed orderbook anomaly" in r for r in res_crossed["rejection_reasons"]))


class TestCanonicalEightGateRegistry(unittest.TestCase):
    """
    Validates the Canonical Eight Mandatory Tradability Gates:
    GATE_1_SQUEEZE_COMPRESSION to GATE_8_DEVELOPING_UNCONFIRMED_BAR.
    """

    def setUp(self):
        self.bull_eval = {"total_score": 78.0}
        self.bear_eval = {"total_score": 20.0}
        self.mtf_suite = {
            "timeframe_analyses": {
                Timeframe.M15: {
                    "regime": {"regime": MarketRegime.TRENDING_BULLISH, "is_squeeze": False, "is_feed_gap": False}
                },
                Timeframe.M5: {
                    "regime": {"regime": MarketRegime.TRENDING_BULLISH, "is_squeeze": False, "is_feed_gap": False}
                }
            }
        }
        self.levels = {
            "entry_price": 24250.0,
            "stop_loss_price": 24150.0,
            "target_price": 24450.0,
            "risk_reward_ratio": 2.0,
            "is_valid_levels": True
        }
        self.opt_tradability = {"is_tradable": True, "rejection_reasons": []}

    def test_registry_contains_all_eight_gates(self):
        """Gate evaluation must return all 8 canonical gates with full audit metadata."""
        res = evaluate_canonical_eight_gates(
            direction=SetupDirection.BULLISH,
            bullish_eval=self.bull_eval,
            bearish_eval=self.bear_eval,
            mtf_suite=self.mtf_suite,
            structural_levels=self.levels,
            option_tradability=self.opt_tradability,
            is_candle_closed=True
        )
        self.assertTrue(res["all_gates_passed"])
        self.assertEqual(res["gate_count"], 8)
        self.assertEqual(res["passed_gate_count"], 8)
        self.assertEqual(res["failed_gate_count"], 0)

        expected_gates = [
            CanonicalGateID.GATE_1_SQUEEZE_COMPRESSION,
            CanonicalGateID.GATE_2_CONFLICTING_MTF_REGIMES,
            CanonicalGateID.GATE_3_HIGH_MUTUAL_CONFLUENCE,
            CanonicalGateID.GATE_4_FEED_GAP_OR_UNRELIABLE_DATA,
            CanonicalGateID.GATE_5_UNFAVORABLE_RISK_REWARD,
            CanonicalGateID.GATE_6_UNTRADABLE_OPTION_CONTRACT,
            CanonicalGateID.GATE_7_INVALID_STRUCTURAL_LEVELS,
            CanonicalGateID.GATE_8_DEVELOPING_UNCONFIRMED_BAR
        ]
        for gid in expected_gates:
            self.assertIn(gid, res["gate_registry"])
            gate = res["gate_registry"][gid]
            self.assertEqual(gate["gate_id"], gid)
            self.assertTrue(gate["passed"])
            self.assertIsNone(gate["rejection_reason"])
            self.assertIsInstance(gate["source_data"], dict)

    def test_gate_1_squeeze_rejection(self):
        """Gate 1 fails when 15m or 5m is in squeeze compression."""
        self.mtf_suite["timeframe_analyses"][Timeframe.M5]["regime"]["is_squeeze"] = True
        res = evaluate_canonical_eight_gates(
            SetupDirection.BULLISH, self.bull_eval, self.bear_eval,
            self.mtf_suite, self.levels, self.opt_tradability, True
        )
        self.assertFalse(res["all_gates_passed"])
        self.assertFalse(res["gate_registry"][CanonicalGateID.GATE_1_SQUEEZE_COMPRESSION]["passed"])
        self.assertTrue(any(CanonicalGateID.GATE_1_SQUEEZE_COMPRESSION in r for r in res["rejection_reasons"]))

    def test_gate_6_untradable_option_contract_rejection(self):
        """Gate 6 fails when option tradability is rejected."""
        bad_opt = {"is_tradable": False, "rejection_reasons": ["Excessive bid-ask spread: 8.5%"]}
        res = evaluate_canonical_eight_gates(
            SetupDirection.BULLISH, self.bull_eval, self.bear_eval,
            self.mtf_suite, self.levels, bad_opt, True
        )
        self.assertFalse(res["all_gates_passed"])
        self.assertFalse(res["gate_registry"][CanonicalGateID.GATE_6_UNTRADABLE_OPTION_CONTRACT]["passed"])
        self.assertTrue(any(CanonicalGateID.GATE_6_UNTRADABLE_OPTION_CONTRACT in r for r in res["rejection_reasons"]))

    def test_gate_7_invalid_structural_levels_rejection(self):
        """Gate 7 fails when structural levels are corrupted or invalid."""
        bad_levels = dict(self.levels)
        bad_levels["is_valid_levels"] = False
        res = evaluate_canonical_eight_gates(
            SetupDirection.BULLISH, self.bull_eval, self.bear_eval,
            self.mtf_suite, bad_levels, self.opt_tradability, True
        )
        self.assertFalse(res["all_gates_passed"])
        self.assertFalse(res["gate_registry"][CanonicalGateID.GATE_7_INVALID_STRUCTURAL_LEVELS]["passed"])
        self.assertTrue(any(CanonicalGateID.GATE_7_INVALID_STRUCTURAL_LEVELS in r for r in res["rejection_reasons"]))

    def test_gate_8_developing_unconfirmed_bar_rejection(self):
        """Gate 8 fails when evaluation is performed on an unclosed candle."""
        res = evaluate_canonical_eight_gates(
            SetupDirection.BULLISH, self.bull_eval, self.bear_eval,
            self.mtf_suite, self.levels, self.opt_tradability, is_candle_closed=False
        )
        self.assertFalse(res["all_gates_passed"])
        self.assertFalse(res["gate_registry"][CanonicalGateID.GATE_8_DEVELOPING_UNCONFIRMED_BAR]["passed"])
        self.assertTrue(any(CanonicalGateID.GATE_8_DEVELOPING_UNCONFIRMED_BAR in r for r in res["rejection_reasons"]))


class TestStructuralTargetSelectionHierarchy(unittest.TestCase):
    """
    Validates deterministic target selection hierarchy (Tier 1: 5m, Tier 2: 15m, Tier 3: 1.5 ATR)
    and intermediate obstacle clamping on 1m/3m.
    """

    def test_tier_1_5m_target_selection(self):
        """Nearest confirmed opposing 5m swing high above entry is selected as Tier 1."""
        mtf_suite = {
            "timeframe_analyses": {
                Timeframe.M5: {
                    "structure": {
                        "swing_highs": [{"pivot_price": 24350.0}, {"pivot_price": 24450.0}]
                    }
                },
                Timeframe.M15: {
                    "structure": {
                        "swing_highs": [{"pivot_price": 24500.0}]
                    }
                }
            }
        }
        res = calculate_structural_target(SetupDirection.BULLISH, entry_price=24250.0, mtf_suite=mtf_suite)
        self.assertEqual(res["target_price"], 24350.0)
        self.assertEqual(res["target_tier"], "TIER_1_INTERMEDIATE_5M")
        self.assertEqual(res["target_source"], "5m_nearest_swing_high")

    def test_tier_2_15m_target_when_5m_absent(self):
        """When 5m opposing swing high is absent, Tier 2 (15m swing high) is selected."""
        mtf_suite = {
            "timeframe_analyses": {
                Timeframe.M5: {"structure": {"swing_highs": []}},
                Timeframe.M15: {
                    "structure": {"swing_highs": [{"pivot_price": 24480.0}]}
                }
            }
        }
        res = calculate_structural_target(SetupDirection.BULLISH, entry_price=24250.0, mtf_suite=mtf_suite)
        self.assertEqual(res["target_price"], 24480.0)
        self.assertEqual(res["target_tier"], "TIER_2_MACRO_15M")
        self.assertEqual(res["target_source"], "15m_nearest_swing_high")

    def test_tier_3_fallback_expansion_in_price_discovery(self):
        """When in price discovery with no historical opposing pivots, fallback to entry + 1.5 ATR."""
        mtf_suite = {
            "timeframe_analyses": {
                Timeframe.M5: {"structure": {"swing_highs": []}},
                Timeframe.M15: {"structure": {"swing_highs": []}}
            }
        }
        res = calculate_structural_target(SetupDirection.BULLISH, entry_price=24250.0, mtf_suite=mtf_suite, atr=20.0)
        self.assertEqual(res["target_price"], 24280.0) # 24250 + (1.5 * 20.0)
        self.assertEqual(res["target_tier"], "TIER_3_FALLBACK_EXPANSION")

    def test_intermediate_obstacle_detection_and_clamping(self):
        """
        When an intermediate 1m swing high sits directly between entry and macro target
        within 1.0 ATR, target is clamped to the obstacle to prevent inflated R:R.
        """
        mtf_suite = {
            "timeframe_analyses": {
                Timeframe.M5: {
                    "structure": {"swing_highs": [{"pivot_price": 24450.0}]}
                },
                Timeframe.M1: {
                    # Intermediate obstacle at 24258 (8 pts above entry 24250, ATR = 12.0)
                    "structure": {"swing_highs": [{"pivot_price": 24258.0}]}
                }
            }
        }
        res = calculate_structural_target(SetupDirection.BULLISH, entry_price=24250.0, mtf_suite=mtf_suite, atr=12.0)
        self.assertTrue(res["has_intermediate_obstacle"])
        self.assertEqual(res["obstacle_price"], 24258.0)
        self.assertEqual(res["target_price"], 24258.0)
        self.assertEqual(res["target_tier"], "CLAMPED_TO_OBSTACLE")

    def test_clamped_obstacle_fails_gate_5_insufficient_rr(self):
        """
        When target is clamped to an intermediate obstacle, drastically reducing reward,
        Gate 5 must fail closed due to insufficient Risk/Reward ratio (< 1.50).
        """
        mtf_suite = {
            "timeframe_analyses": {
                Timeframe.M5: {
                    "structure": {
                        "latest_confirmed_low": {"pivot_price": 24150.0},
                        "swing_highs": [{"pivot_price": 24450.0}]
                    },
                    "regime": {"regime": MarketRegime.TRENDING_BULLISH, "is_squeeze": False, "is_feed_gap": False}
                },
                Timeframe.M15: {
                    "regime": {"regime": MarketRegime.TRENDING_BULLISH, "is_squeeze": False, "is_feed_gap": False}
                },
                Timeframe.M1: {
                    # Close obstacle clamps target to 24258
                    "structure": {"swing_highs": [{"pivot_price": 24258.0}]}
                }
            }
        }
        indicators = {"volatility": {"atr": 10.0}}
        levels = calculate_structural_trade_levels(SetupDirection.BULLISH, cmp_price=24250.0, mtf_suite=mtf_suite, indicators=indicators)

        # Risk is 102 pts (24250 - 24148), Reward is only 8 pts (24258 - 24250) -> R:R = 0.08 < 1.50
        self.assertLess(levels["risk_reward_ratio"], 1.50)

        passes, rejections = evaluate_fail_closed_gates(
            direction=SetupDirection.BULLISH,
            bullish_eval={"total_score": 80.0},
            bearish_eval={"total_score": 15.0},
            mtf_suite=mtf_suite,
            structural_levels=levels,
            option_tradability={"is_tradable": True, "rejection_reasons": []}
        )
        self.assertFalse(passes)
        self.assertTrue(any(CanonicalGateID.GATE_5_UNFAVORABLE_RISK_REWARD in r for r in rejections))


# ============================================================================
# Phase 2C Final Hardening Dedicated Regression Tests
# ============================================================================

class TestEvidenceFamilyRegistryCompleteness(unittest.TestCase):
    """
    Validates complete 8-family registry, per-category ceilings, correlation-control
    group documentation, and independent Category 4 caps.
    """

    def test_registry_contains_all_eight_distinct_families(self):
        registry = get_evidence_family_registry()
        expected_families = {
            EvidenceFamily.TREND_ALIGNMENT,
            EvidenceFamily.MARKET_STRUCTURE,
            EvidenceFamily.DIRECTIONAL_STRENGTH,
            EvidenceFamily.MOMENTUM,
            EvidenceFamily.VALUE_LOCATION,
            EvidenceFamily.VOLATILITY,
            EvidenceFamily.DIVERGENCE,
            EvidenceFamily.EXECUTION_TIMING,
        }
        self.assertEqual(set(registry.keys()), expected_families)
        self.assertEqual(len(registry), 8)

    def test_per_category_ceilings_and_assignments(self):
        registry = get_evidence_family_registry()
        # Cat 1
        cat1_trend = registry[EvidenceFamily.TREND_ALIGNMENT]["category_assignments"]["category_1"]["ceiling"]
        cat1_struct = registry[EvidenceFamily.MARKET_STRUCTURE]["category_assignments"]["category_1"]["ceiling"]
        self.assertEqual(cat1_trend, 15.0)
        self.assertEqual(cat1_struct, 15.0)

        # Cat 4
        cat4_div = registry[EvidenceFamily.DIVERGENCE]["category_assignments"]["category_4"]["ceiling"]
        cat4_timing = registry[EvidenceFamily.EXECUTION_TIMING]["category_assignments"]["category_4"]["ceiling"]
        self.assertEqual(cat4_div, 15.0)
        self.assertEqual(cat4_timing, 5.0)

    def test_backward_compatibility_alias(self):
        self.assertEqual(EvidenceFamily.DIVERGENCE_TIMING, EvidenceFamily.DIVERGENCE)

    def test_category_4_independent_capping(self):
        # Create a suite where both Divergence (15) and 15s timing (5) trigger
        # Divergence raw = 15.0 (confirmed + untouched), Timing raw = 5.0 (15s aligned)
        mtf_suite = {
            "timeframe_analyses": {
                Timeframe.M15: {"regime": {"regime": MarketRegime.TRENDING_BULLISH, "is_squeeze": False, "is_feed_gap": False}},
                Timeframe.M5: {"regime": {"regime": MarketRegime.TRENDING_BULLISH, "is_squeeze": False, "is_feed_gap": False}},
                Timeframe.M1: {
                    "regime": {"regime": MarketRegime.TRENDING_BULLISH, "is_squeeze": False, "is_feed_gap": False},
                    "divergences": {
                        "rsi_divergence": {
                            "has_divergence": True,
                            "divergence_type": DivergenceType.REGULAR_BULLISH,
                            "invalidation_status": DivergenceInvalidationStatus.ACTIVE_UNTOUCHED
                        }
                    }
                },
                Timeframe.S15: {
                    "structure": {"structure_state": StructureType.BULLISH}
                }
            }
        }
        res = score_directional_confluence(SetupDirection.BULLISH, mtf_suite, indicators_by_tf={})
        cat4_breakdown = res["evidence_family_breakdown"]["category_4"]
        self.assertEqual(cat4_breakdown[EvidenceFamily.DIVERGENCE]["capped"], 15.0)
        self.assertEqual(cat4_breakdown[EvidenceFamily.EXECUTION_TIMING]["capped"], 5.0)
        self.assertEqual(res["category_scores"]["divergence_timing"], 20.0)


class TestAuthoritativeQuoteFreshness(unittest.TestCase):
    """
    Validates single authoritative qualification boundary at <= 60.0s.
    0-15s: FRESH_LIVE_QUOTE (pass)
    >15-60s: ACCEPTABLE_LATENCY (pass with latency warning)
    >60s: STALE_REJECT (fails closed at qualification boundary)
    >180s: STALE_HARD_REJECT (diagnostic sub-classification, fails closed)
    Missing / Future (>2s skew): fail closed.
    """

    def setUp(self):
        self.now = datetime(2026, 11, 10, 10, 30, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))

    def _base_contract(self, quote_time):
        return {
            "symbol": "NIFTY26NOV24200CE",
            "option_type": "CE",
            "strike": 24200,
            "ltp": 150.0,
            "bid": 149.0,
            "ask": 151.0,
            "oi": 100000,
            "volume": 20000,
            "expiry_date": "2026-11-23",
            "lot_size": 65,
            "instrument_token": "999123",
            "last_trade_time": quote_time,
            "status": "ACTIVE"
        }

    def test_fresh_live_quote_within_15s(self):
        contract = self._base_contract(self.now - timedelta(seconds=8))
        res = validate_option_contract_against_master(contract, "NIFTY 50", expected_expiry_date="2026-11-23", current_dt=self.now)
        self.assertTrue(res["is_tradable"])
        self.assertEqual(res["freshness_status"], "FRESH_LIVE_QUOTE")
        self.assertEqual(len(res["warnings"]), 0)

    def test_acceptable_latency_between_15s_and_60s(self):
        contract = self._base_contract(self.now - timedelta(seconds=45))
        res = validate_option_contract_against_master(contract, "NIFTY 50", expected_expiry_date="2026-11-23", current_dt=self.now)
        self.assertTrue(res["is_tradable"])
        self.assertEqual(res["freshness_status"], "ACCEPTABLE_LATENCY")
        self.assertTrue(any("Latency warning" in w for w in res["warnings"]))

    def test_stale_reject_at_65s_fails_closed(self):
        contract = self._base_contract(self.now - timedelta(seconds=65))
        res = validate_option_contract_against_master(contract, "NIFTY 50", expected_expiry_date="2026-11-23", current_dt=self.now)
        self.assertFalse(res["is_tradable"])
        self.assertEqual(res["freshness_status"], "STALE_REJECT")
        self.assertTrue(any("intraday qualification limit (60.0s)" in r for r in res["rejection_reasons"]))

    def test_stale_hard_reject_at_200s_diagnostic(self):
        contract = self._base_contract(self.now - timedelta(seconds=200))
        res = validate_option_contract_against_master(contract, "NIFTY 50", expected_expiry_date="2026-11-23", current_dt=self.now)
        self.assertFalse(res["is_tradable"])
        self.assertEqual(res["freshness_status"], "STALE_HARD_REJECT")
        self.assertTrue(any("hard staleness limit (180.0s)" in r for r in res["rejection_reasons"]))

    def test_missing_timestamp_fails_closed(self):
        contract = self._base_contract(None)
        res = validate_option_contract_against_master(contract, "NIFTY 50", expected_expiry_date="2026-11-23", current_dt=self.now)
        self.assertFalse(res["is_tradable"])
        self.assertEqual(res["freshness_status"], "MISSING_TIMESTAMP")
        self.assertTrue(any("Missing quote timestamp" in r for r in res["rejection_reasons"]))

    def test_future_timestamp_beyond_skew_fails_closed(self):
        contract = self._base_contract(self.now + timedelta(seconds=5))
        res = validate_option_contract_against_master(contract, "NIFTY 50", expected_expiry_date="2026-11-23", current_dt=self.now)
        self.assertFalse(res["is_tradable"])
        self.assertEqual(res["freshness_status"], "FUTURE_TIMESTAMP_ANOMALY")
        self.assertTrue(any("Future timestamp anomaly" in r for r in res["rejection_reasons"]))

    def test_future_timestamp_within_skew_allowed(self):
        # 1 second ahead is within 2.0s clock skew
        contract = self._base_contract(self.now + timedelta(seconds=1))
        res = validate_option_contract_against_master(contract, "NIFTY 50", expected_expiry_date="2026-11-23", current_dt=self.now)
        self.assertTrue(res["is_tradable"])
        self.assertEqual(res["freshness_status"], "FRESH_LIVE_QUOTE")


class TestOptionGreekApproximationModel(unittest.TestCase):
    """
    Validates OPTION_GREEK_APPROXIMATION classification, explicit assumptions,
    and UNAVAILABLE status handling.
    """

    def setUp(self):
        self.mtf_suite = {
            "timeframe_analyses": {
                Timeframe.M5: {
                    "structure": {
                        "latest_confirmed_low": {"pivot_price": 24200.0},
                        "swing_highs": [{"pivot_price": 24350.0}]
                    }
                },
                Timeframe.M15: {
                    "structure": {"swing_highs": [{"pivot_price": 24350.0}]}
                },
                Timeframe.M1: {"structure": {"swing_highs": []}}
            }
        }
        self.indicators = {"volatility": {"atr": 10.0}}

    def test_option_greek_approximation_metadata_and_assumptions(self):
        now_ist = datetime.now(timezone(timedelta(hours=5, minutes=30)))
        quote = {
            "symbol": "NIFTY26NOV24250CE",
            "strike": 24250,
            "option_type": "CE",
            "ask": 120.0,
            "ltp": 119.5,
            "bid": 119.0,
            "last_trade_time": now_ist.isoformat()
        }
        levels = calculate_structural_trade_levels(
            SetupDirection.BULLISH,
            cmp_price=24250.0,
            mtf_suite=self.mtf_suite,
            indicators=self.indicators,
            option_quote=quote
        )
        opt_rr = levels["option_risk_reward"]
        self.assertEqual(opt_rr["status"], "VALID_ESTIMATE")
        self.assertEqual(opt_rr["model_classification"], "OPTION_GREEK_APPROXIMATION")
        self.assertIsNotNone(opt_rr["assumptions"])
        self.assertEqual(opt_rr["assumptions"]["implied_volatility"], "HELD_CONSTANT (Vega = 0)")
        self.assertEqual(opt_rr["assumptions"]["theta_decay"], "EXCLUDED")
        self.assertEqual(opt_rr["assumptions"]["entry_quote_basis"], "EXECUTABLE_ASK_PRICE")
        self.assertEqual(opt_rr["assumptions"]["exit_valuation_basis"], "THEORETICAL_MID_PROJECTION (spread decay and slippage excluded)")
        self.assertIn("greeks_timestamp", opt_rr["assumptions"])
        self.assertIsNotNone(opt_rr["provenance"])
        self.assertEqual(opt_rr["provenance"]["underlying_spot_price"], 24250.0)
        self.assertEqual(opt_rr["provenance"]["time_to_expiry_convention"], "ACT/365")
        self.assertAlmostEqual(opt_rr["provenance"]["risk_free_rate"], 0.065)

    def test_option_rr_marked_unavailable_when_no_quote(self):
        levels = calculate_structural_trade_levels(
            SetupDirection.BULLISH,
            cmp_price=24250.0,
            mtf_suite=self.mtf_suite,
            indicators=self.indicators,
            option_quote=None
        )
        opt_rr = levels["option_risk_reward"]
        self.assertEqual(opt_rr["status"], "UNAVAILABLE")
        self.assertEqual(opt_rr["model_classification"], "OPTION_GREEK_APPROXIMATION")
        self.assertIsNone(opt_rr["option_risk_reward_ratio"])
        self.assertEqual(opt_rr["option_risk_reward_str"], "UNAVAILABLE")


class TestExactContractMasterValidation(unittest.TestCase):
    """
    Validates exact expiry matching, lot size check against exchange master,
    and broker instrument token requirements.
    """

    def setUp(self):
        self.now = datetime(2026, 11, 10, 10, 30, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))

    def _valid_contract(self):
        return {
            "symbol": "NIFTY26NOV24200CE",
            "option_type": "CE",
            "strike": 24200,
            "ltp": 150.0,
            "bid": 149.0,
            "ask": 151.0,
            "oi": 100000,
            "volume": 20000,
            "expiry_date": "2026-11-23",
            "lot_size": 65,  # Authoritative NIFTY lot size for Nov 2026
            "instrument_token": "43210",
            "last_trade_time": self.now - timedelta(seconds=5),
            "status": "ACTIVE"
        }

    def test_exact_expiry_date_mismatch_fails_closed(self):
        contract = self._valid_contract()
        contract["expiry_date"] = "2026-11-24"  # Holiday regression: actual expected is 2026-11-23
        res = validate_option_contract_against_master(contract, "NIFTY 50", expected_expiry_date="2026-11-23", current_dt=self.now)
        self.assertFalse(res["is_tradable"])
        self.assertTrue(any("Exact expiry date mismatch" in r for r in res["rejection_reasons"]))

    def test_missing_exact_expiry_when_expected_fails(self):
        contract = self._valid_contract()
        del contract["expiry_date"]
        res = validate_option_contract_against_master(contract, "NIFTY 50", expected_expiry_date="2026-11-23", current_dt=self.now)
        self.assertFalse(res["is_tradable"])
        self.assertTrue(any("Missing exact expiry date" in r for r in res["rejection_reasons"]))

    def test_stale_nifty_lot_size_75_fails_closed_against_authoritative_65(self):
        """Proves that a stale NIFTY lot size of 75 cannot pass validation when authoritative is 65."""
        contract = self._valid_contract()
        contract["lot_size"] = 75  # Stale lot size from outdated specifications!
        res = validate_option_contract_against_master(contract, "NIFTY 50", expected_expiry_date="2026-11-23", current_dt=self.now)
        self.assertFalse(res["is_tradable"])
        self.assertTrue(any("Lot size mismatch: contract specifies lot size 75, but authoritative contract master requires 65" in r for r in res["rejection_reasons"]))

    def test_banknifty_authoritative_lot_size_30_passes(self):
        """BANKNIFTY contract with authoritative lot size 30 passes validation."""
        contract = {
            "symbol": "BANKNIFTY26NOV52000CE",
            "option_type": "CE",
            "strike": 52000,
            "ltp": 350.0,
            "bid": 349.0,
            "ask": 351.0,
            "oi": 100000,
            "volume": 20000,
            "expiry_date": "2026-11-23",
            "lot_size": 30,  # Authoritative BANKNIFTY lot size
            "instrument_token": "43211",
            "last_trade_time": self.now - timedelta(seconds=5),
            "status": "ACTIVE"
        }
        res = validate_option_contract_against_master(contract, "BANKNIFTY", expected_expiry_date="2026-11-23", current_dt=self.now)
        self.assertTrue(res["is_tradable"])
        self.assertEqual(res["lot_size"], 30)

    def test_banknifty_lot_size_mismatch_fails_closed(self):
        """BANKNIFTY contract with incorrect lot size (e.g. 25 or 15) fails closed."""
        contract = {
            "symbol": "BANKNIFTY26NOV52000CE",
            "option_type": "CE",
            "strike": 52000,
            "ltp": 350.0,
            "bid": 349.0,
            "ask": 351.0,
            "oi": 100000,
            "volume": 20000,
            "expiry_date": "2026-11-23",
            "lot_size": 25,  # Mismatched lot size
            "instrument_token": "43211",
            "last_trade_time": self.now - timedelta(seconds=5),
            "status": "ACTIVE"
        }
        res = validate_option_contract_against_master(contract, "BANKNIFTY", expected_expiry_date="2026-11-23", current_dt=self.now)
        self.assertFalse(res["is_tradable"])
        self.assertTrue(any("Lot size mismatch: contract specifies lot size 25, but authoritative contract master requires 30" in r for r in res["rejection_reasons"]))

    def test_empty_or_whitespace_instrument_token_fails_closed(self):
        contract = self._valid_contract()
        contract["instrument_token"] = "   "
        res = validate_option_contract_against_master(contract, "NIFTY 50", expected_expiry_date="2026-11-23", current_dt=self.now)
        self.assertFalse(res["is_tradable"])
        self.assertTrue(any("Empty broker instrument identifier" in r for r in res["rejection_reasons"]))

    def test_matching_master_contract_passes(self):
        contract = self._valid_contract()
        res = validate_option_contract_against_master(contract, "NIFTY 50", expected_expiry_date="2026-11-23", current_dt=self.now)
        self.assertTrue(res["is_tradable"])


class TestTargetClassificationAndGeometricValidity(unittest.TestCase):
    """
    Validates structural vs modelled target classification, geometric validity,
    and passes_minimum_rr flag.
    """

    def test_confirmed_structural_target_classification(self):
        mtf_suite = {
            "timeframe_analyses": {
                Timeframe.M5: {
                    "structure": {
                        "latest_confirmed_low": {"pivot_price": 24200.0},
                        "swing_highs": [{"pivot_price": 24400.0}]
                    }
                },
                Timeframe.M15: {"structure": {"swing_highs": []}},
                Timeframe.M1: {"structure": {"swing_highs": []}}
            }
        }
        res = calculate_structural_target(SetupDirection.BULLISH, entry_price=24250.0, mtf_suite=mtf_suite, atr=10.0)
        self.assertEqual(res["target_type"], TargetType.STRUCTURAL_TARGET_CONFIRMED)
        self.assertTrue(res["is_structurally_confirmed"])
        self.assertEqual(res["target_price"], 24400.0)

    def test_modelled_expansion_target_classification(self):
        mtf_suite = {
            "timeframe_analyses": {
                Timeframe.M5: {"structure": {"swing_highs": []}},
                Timeframe.M15: {"structure": {"swing_highs": []}},
                Timeframe.M1: {"structure": {"swing_highs": []}}
            }
        }
        res = calculate_structural_target(SetupDirection.BULLISH, entry_price=24250.0, mtf_suite=mtf_suite, atr=10.0)
        self.assertEqual(res["target_type"], TargetType.MODELLED_EXPANSION_TARGET)
        self.assertFalse(res["is_structurally_confirmed"])
        self.assertEqual(res["target_price"], 24265.0)  # 24250 + 1.5 * 10

    def test_geometric_validity_and_minimum_rr(self):
        # Valid bullish setup: Stop (24198) < Entry (24250) < Target (24400)
        mtf_suite = {
            "timeframe_analyses": {
                Timeframe.M5: {
                    "structure": {
                        "latest_confirmed_low": {"pivot_price": 24200.0},
                        "swing_highs": [{"pivot_price": 24400.0}]
                    }
                },
                Timeframe.M15: {"structure": {"swing_highs": []}},
                Timeframe.M1: {"structure": {"swing_highs": []}}
            }
        }
        indicators = {"volatility": {"atr": 10.0}}
        levels = calculate_structural_trade_levels(SetupDirection.BULLISH, cmp_price=24250.0, mtf_suite=mtf_suite, indicators=indicators)
        u_rr = levels["underlying_risk_reward"]
        self.assertTrue(u_rr["is_valid_levels"])
        self.assertTrue(u_rr["passes_minimum_rr"])
        self.assertGreaterEqual(u_rr["risk_reward_ratio"], 1.50)
        self.assertEqual(u_rr["target_type"], TargetType.STRUCTURAL_TARGET_CONFIRMED)
        self.assertTrue(u_rr["is_structurally_confirmed"])

    def test_inverted_geometric_levels_marked_invalid(self):
        # Inverted bullish levels: Stop > Entry
        mtf_suite = {
            "timeframe_analyses": {
                Timeframe.M5: {
                    "structure": {
                        "latest_confirmed_low": {"pivot_price": 24300.0},  # Low above entry! Corrupt pivot
                        "swing_highs": [{"pivot_price": 24400.0}]
                    }
                },
                Timeframe.M15: {"structure": {"swing_highs": []}},
                Timeframe.M1: {"structure": {"swing_highs": []}}
            }
        }
        indicators = {"volatility": {"atr": 10.0}}
        levels = calculate_structural_trade_levels(SetupDirection.BULLISH, cmp_price=24250.0, mtf_suite=mtf_suite, indicators=indicators)
        u_rr = levels["underlying_risk_reward"]
        self.assertFalse(u_rr["is_valid_levels"])
        self.assertFalse(u_rr["passes_minimum_rr"])


class TestGate8ClosedCandleDependency(unittest.TestCase):
    """
    Validates Gate 8: Passing when setup is based on closed candles even if
    a developing candle exists in the multi-timeframe candle store.
    Fails closed only when setup depends on unclosed bar.
    """

    def setUp(self):
        self.mtf_suite = {
            "timeframe_analyses": {
                Timeframe.M5: {"regime": {"regime": MarketRegime.TRENDING_BULLISH, "is_squeeze": False, "is_feed_gap": False}},
                Timeframe.M15: {"regime": {"regime": MarketRegime.TRENDING_BULLISH, "is_squeeze": False, "is_feed_gap": False}}
            }
        }
        self.structural_levels = {
            "entry_price": 24250.0,
            "stop_loss_price": 24200.0,
            "target_price": 24350.0,
            "risk_reward_ratio": 2.0,
            "is_valid_levels": True
        }
        self.option_tradability = {"is_tradable": True, "rejection_reasons": []}

    def test_gate_8_passes_when_setup_uses_closed_bar(self):
        # Even if active developing bar is in store, setup uses closed bar
        passes, rejections = evaluate_fail_closed_gates(
            direction=SetupDirection.BULLISH,
            bullish_eval={"total_score": 78.0},
            bearish_eval={"total_score": 10.0},
            mtf_suite=self.mtf_suite,
            structural_levels=self.structural_levels,
            option_tradability=self.option_tradability,
            is_candle_closed=True,
            setup_depends_on_developing_bar=False
        )
        self.assertTrue(passes)
        self.assertEqual(len(rejections), 0)

    def test_gate_8_fails_when_setup_depends_on_developing_bar(self):
        passes, rejections = evaluate_fail_closed_gates(
            direction=SetupDirection.BULLISH,
            bullish_eval={"total_score": 78.0},
            bearish_eval={"total_score": 10.0},
            mtf_suite=self.mtf_suite,
            structural_levels=self.structural_levels,
            option_tradability=self.option_tradability,
            is_candle_closed=True,
            setup_depends_on_developing_bar=True
        )
        self.assertFalse(passes)
        self.assertTrue(any(CanonicalGateID.GATE_8_DEVELOPING_UNCONFIRMED_BAR in r for r in rejections))

    def test_gate_8_fails_when_trigger_candle_unclosed(self):
        passes, rejections = evaluate_fail_closed_gates(
            direction=SetupDirection.BULLISH,
            bullish_eval={"total_score": 78.0},
            bearish_eval={"total_score": 10.0},
            mtf_suite=self.mtf_suite,
            structural_levels=self.structural_levels,
            option_tradability=self.option_tradability,
            is_candle_closed=False,
            setup_depends_on_developing_bar=False
        )
        self.assertFalse(passes)
        self.assertTrue(any(CanonicalGateID.GATE_8_DEVELOPING_UNCONFIRMED_BAR in r for r in rejections))


class TestGate1SqueezeLifecycle(unittest.TestCase):
    """
    Validates Gate 1 distinguishing ACTIVE_COMPRESSION (reject) from
    CONFIRMED_RELEASE (pass) and exposing lifecycle state.
    """

    def setUp(self):
        self.structural_levels = {
            "entry_price": 24250.0,
            "stop_loss_price": 24200.0,
            "target_price": 24350.0,
            "risk_reward_ratio": 2.0,
            "is_valid_levels": True
        }
        self.option_tradability = {"is_tradable": True, "rejection_reasons": []}

    def test_active_compression_fails_gate_1(self):
        mtf_suite = {
            "timeframe_analyses": {
                Timeframe.M5: {
                    "regime": {
                        "regime": MarketRegime.VOLATILITY_COMPRESSION,
                        "is_squeeze": True,
                        "metrics": {"is_squeeze_on": True, "is_squeeze_release": False}
                    }
                },
                Timeframe.M15: {
                    "regime": {"regime": MarketRegime.TRENDING_BULLISH, "is_squeeze": False}
                }
            }
        }
        gates = evaluate_canonical_eight_gates(
            direction=SetupDirection.BULLISH,
            bullish_eval={"total_score": 80.0},
            bearish_eval={"total_score": 10.0},
            mtf_suite=mtf_suite,
            structural_levels=self.structural_levels,
            option_tradability=self.option_tradability
        )
        g1 = gates["gate_registry"][CanonicalGateID.GATE_1_SQUEEZE_COMPRESSION]
        self.assertFalse(g1["passed"])
        self.assertEqual(g1["source_data"]["5m_squeeze_lifecycle"], SqueezeLifecycleState.ACTIVE_COMPRESSION)

    def test_confirmed_release_passes_gate_1(self):
        mtf_suite = {
            "timeframe_analyses": {
                Timeframe.M5: {
                    "regime": {
                        "regime": MarketRegime.VOLATILITY_EXPANSION,
                        "is_squeeze": False,
                        "metrics": {"is_squeeze_on": False, "is_squeeze_release": True}
                    }
                },
                Timeframe.M15: {
                    "regime": {
                        "regime": MarketRegime.TRENDING_BULLISH,
                        "is_squeeze": False,
                        "metrics": {"is_squeeze_on": False, "is_squeeze_release": False}
                    }
                }
            }
        }
        gates = evaluate_canonical_eight_gates(
            direction=SetupDirection.BULLISH,
            bullish_eval={"total_score": 80.0},
            bearish_eval={"total_score": 10.0},
            mtf_suite=mtf_suite,
            structural_levels=self.structural_levels,
            option_tradability=self.option_tradability
        )
        g1 = gates["gate_registry"][CanonicalGateID.GATE_1_SQUEEZE_COMPRESSION]
        self.assertTrue(g1["passed"])
        self.assertEqual(g1["source_data"]["5m_squeeze_lifecycle"], SqueezeLifecycleState.CONFIRMED_RELEASE)

    def test_no_squeeze_passes_gate_1(self):
        mtf_suite = {
            "timeframe_analyses": {
                Timeframe.M5: {
                    "regime": {
                        "regime": MarketRegime.TRENDING_BULLISH,
                        "is_squeeze": False,
                        "metrics": {"is_squeeze_on": False, "is_squeeze_release": False}
                    }
                },
                Timeframe.M15: {
                    "regime": {
                        "regime": MarketRegime.TRENDING_BULLISH,
                        "is_squeeze": False,
                        "metrics": {"is_squeeze_on": False, "is_squeeze_release": False}
                    }
                }
            }
        }
        gates = evaluate_canonical_eight_gates(
            direction=SetupDirection.BULLISH,
            bullish_eval={"total_score": 80.0},
            bearish_eval={"total_score": 10.0},
            mtf_suite=mtf_suite,
            structural_levels=self.structural_levels,
            option_tradability=self.option_tradability
        )
        g1 = gates["gate_registry"][CanonicalGateID.GATE_1_SQUEEZE_COMPRESSION]
        self.assertTrue(g1["passed"])
        self.assertEqual(g1["source_data"]["5m_squeeze_lifecycle"], SqueezeLifecycleState.NO_SQUEEZE)


# ============================================================================
# Phase 2C Final Corrections Dedicated Test Suite
# ============================================================================

class TestSignedOptionDeltaAndConvexity(unittest.TestCase):
    """
    Validates preservation of signed Delta for Black-Scholes approximations:
    - CE Delta = N(d1) > 0
    - PE Delta = N(d1) - 1 < 0
    - Gamma > 0 for both option types
    - Formula: ΔP ≈ Delta * ΔS + 0.5 * Gamma * (ΔS)^2
    """

    def test_ce_premium_projection_underlying_rises(self):
        """When underlying rises (+50 pts), long CE Delta (+0.52) increases premium; Gamma adds positive convexity."""
        res = project_option_premium_change(delta=0.52, gamma=0.0012, delta_spot=50.0)
        self.assertGreater(res["first_order_delta_effect"], 0.0)  # +26.0
        self.assertGreater(res["second_order_gamma_effect"], 0.0) # +1.5
        self.assertEqual(res["delta_premium"], 27.5)              # 26.0 + 1.5
        self.assertIn("OPTION_GREEK_APPROXIMATION", res["model_classification"])

    def test_ce_premium_projection_underlying_falls(self):
        """When underlying falls (-50 pts), long CE Delta (+0.52) decreases premium; Gamma reduces the loss."""
        res = project_option_premium_change(delta=0.52, gamma=0.0012, delta_spot=-50.0)
        self.assertLess(res["first_order_delta_effect"], 0.0)     # -26.0
        self.assertGreater(res["second_order_gamma_effect"], 0.0) # +1.5 (convexity benefit)
        self.assertEqual(res["delta_premium"], -24.5)             # -26.0 + 1.5 = -24.5

    def test_pe_premium_projection_underlying_falls(self):
        """When underlying falls (-50 pts), long PE signed Delta (-0.48) increases premium."""
        res = project_option_premium_change(delta=-0.48, gamma=0.0012, delta_spot=-50.0)
        # (-0.48) * (-50) = +24.0
        self.assertGreater(res["first_order_delta_effect"], 0.0)
        self.assertGreater(res["second_order_gamma_effect"], 0.0)
        self.assertEqual(res["delta_premium"], 25.5)              # 24.0 + 1.5

    def test_pe_premium_projection_underlying_rises(self):
        """When underlying rises (+50 pts), long PE signed Delta (-0.48) decreases premium."""
        res = project_option_premium_change(delta=-0.48, gamma=0.0012, delta_spot=50.0)
        # (-0.48) * (+50) = -24.0
        self.assertLess(res["first_order_delta_effect"], 0.0)
        self.assertGreater(res["second_order_gamma_effect"], 0.0)
        self.assertEqual(res["delta_premium"], -22.5)             # -24.0 + 1.5

    def test_gamma_contributes_positive_convexity_both_ce_pe(self):
        """Gamma is strictly positive for both long calls and long puts across rising and falling moves."""
        for d in [0.55, -0.45]:
            for dS in [30.0, -30.0]:
                res = project_option_premium_change(delta=d, gamma=0.0015, delta_spot=dS)
                self.assertGreater(res["second_order_gamma_effect"], 0.0)

    def test_labeled_as_approximation_disclaimer(self):
        """Output is explicitly labeled as OPTION_GREEK_APPROXIMATION with non-guarantee disclaimer."""
        res = project_option_premium_change(delta=0.50, gamma=0.001, delta_spot=10.0)
        self.assertEqual(res["model_classification"], "OPTION_GREEK_APPROXIMATION")
        self.assertIn("Not an execution-guaranteed option price", res["disclaimer"])

    def test_negative_gamma_or_non_finite_greeks_rejected(self):
        """Negative gamma or non-finite Greek inputs must be rejected by projection model."""
        with self.assertRaises(ValueError):
            project_option_premium_change(delta=0.50, gamma=-0.001, delta_spot=10.0)
        with self.assertRaises(ValueError):
            project_option_premium_change(delta=float("nan"), gamma=0.001, delta_spot=10.0)

    def test_exchange_tick_size_rounding_ensures_0_05_multiples(self):
        """Ensures theoretical premiums are converted to discrete 5-paise (₹0.05) exchange increments."""
        from app.core.setup_scoring import round_to_exchange_tick
        # ₹124.73 is not a valid tick; nearest rounds to ₹124.75
        self.assertEqual(round_to_exchange_tick(124.73), 124.75)
        self.assertEqual(round_to_exchange_tick(124.71), 124.70)
        self.assertEqual(round_to_exchange_tick(0.04), 0.05)
        # Conservative directional rounding:
        # Round DOWN (conservative stop loss or non-optimistic target fill)
        self.assertEqual(round_to_exchange_tick(124.73, direction="DOWN"), 124.70)
        self.assertEqual(round_to_exchange_tick(124.79, direction="DOWN"), 124.75)
        # Round UP
        self.assertEqual(round_to_exchange_tick(124.71, direction="UP"), 124.75)
        # All rounded prices must be exact multiples of 0.05
        for p in [124.73, 150.12, 199.98, 45.34]:
            rounded = round_to_exchange_tick(p)
            self.assertAlmostEqual((rounded * 100) % 5, 0.0)


class TestExecutableAskEntryValidation(unittest.TestCase):
    """
    Validates executable long-option entry rules:
    - Long-option entry reference MUST use a valid, fresh Ask price.
    - LTP fallback is strictly prohibited.
    - Missing Ask, zero Ask, crossed book, or stale quote makes option R:R UNAVAILABLE.
    """

    def setUp(self):
        self.now_ist = datetime.now(timezone(timedelta(hours=5, minutes=30)))
        self.mtf_suite = {
            "timeframe_analyses": {
                Timeframe.M5: {
                    "structure": {
                        "latest_confirmed_low": {"pivot_price": 24200.0},
                        "swing_highs": [{"pivot_price": 24350.0}]
                    }
                },
                Timeframe.M15: {"structure": {"swing_highs": [{"pivot_price": 24350.0}]}},
                Timeframe.M1: {"structure": {"swing_highs": []}}
            }
        }
        self.indicators = {"volatility": {"atr": 10.0}}

    def test_valid_executable_ask_entry(self):
        """Valid quote with Ask=125.0 and Bid=124.0 produces valid estimate with entry=125.0."""
        quote = {
            "symbol": "NIFTY26NOV24250CE",
            "strike": 24250,
            "option_type": "CE",
            "ask": 125.0,
            "bid": 124.0,
            "ltp": 124.5,
            "last_trade_time": self.now_ist.isoformat()
        }
        levels = calculate_structural_trade_levels(
            SetupDirection.BULLISH, cmp_price=24250.0,
            mtf_suite=self.mtf_suite, indicators=self.indicators, option_quote=quote
        )
        opt_rr = levels["option_premium_risk_reward"]
        self.assertEqual(opt_rr["status"], "VALID_ESTIMATE")
        self.assertEqual(opt_rr["option_entry_price"], 125.0)  # Strictly Ask price

    def test_missing_ask_fails_closed_without_ltp_fallback(self):
        """When Ask is None, option R:R is UNAVAILABLE (ASK_UNAVAILABLE). No silent LTP fallback."""
        quote = {
            "symbol": "NIFTY26NOV24250CE",
            "strike": 24250,
            "option_type": "CE",
            "ask": None,
            "bid": 124.0,
            "ltp": 124.5,  # LTP exists, but MUST NOT be used!
            "last_trade_time": self.now_ist.isoformat()
        }
        levels = calculate_structural_trade_levels(
            SetupDirection.BULLISH, cmp_price=24250.0,
            mtf_suite=self.mtf_suite, indicators=self.indicators, option_quote=quote
        )
        opt_rr = levels["option_premium_risk_reward"]
        self.assertEqual(opt_rr["status"], "UNAVAILABLE")
        self.assertEqual(opt_rr["reason"], "ASK_UNAVAILABLE")
        self.assertIsNone(opt_rr["option_risk_reward_ratio"])
        self.assertIn("LTP fallback strictly prohibited", opt_rr["explanation"])

    def test_zero_or_negative_ask_fails_closed(self):
        """When Ask is <= 0, option R:R is UNAVAILABLE."""
        quote = {
            "symbol": "NIFTY26NOV24250CE",
            "strike": 24250,
            "option_type": "CE",
            "ask": 0.0,
            "bid": 124.0,
            "ltp": 124.5,
            "last_trade_time": self.now_ist.isoformat()
        }
        levels = calculate_structural_trade_levels(
            SetupDirection.BULLISH, cmp_price=24250.0,
            mtf_suite=self.mtf_suite, indicators=self.indicators, option_quote=quote
        )
        opt_rr = levels["option_premium_risk_reward"]
        self.assertEqual(opt_rr["status"], "UNAVAILABLE")
        self.assertEqual(opt_rr["reason"], "ASK_UNAVAILABLE")

    def test_crossed_orderbook_fails_closed(self):
        """Crossed orderbook (Bid > Ask) fails closed with CROSSED_ORDERBOOK."""
        quote = {
            "symbol": "NIFTY26NOV24250CE",
            "strike": 24250,
            "option_type": "CE",
            "ask": 120.0,
            "bid": 125.0,  # Corrupted crossed book
            "ltp": 122.5,
            "last_trade_time": self.now_ist.isoformat()
        }
        levels = calculate_structural_trade_levels(
            SetupDirection.BULLISH, cmp_price=24250.0,
            mtf_suite=self.mtf_suite, indicators=self.indicators, option_quote=quote
        )
        opt_rr = levels["option_premium_risk_reward"]
        self.assertEqual(opt_rr["status"], "UNAVAILABLE")
        self.assertEqual(opt_rr["reason"], "CROSSED_ORDERBOOK")

    def test_stale_quote_fails_option_rr(self):
        """Quote aged > 60s fails closed with STALE_QUOTE."""
        stale_time = self.now_ist - timedelta(seconds=75)
        quote = {
            "symbol": "NIFTY26NOV24250CE",
            "strike": 24250,
            "option_type": "CE",
            "ask": 125.0,
            "bid": 124.0,
            "ltp": 124.5,
            "last_trade_time": stale_time.isoformat()
        }
        levels = calculate_structural_trade_levels(
            SetupDirection.BULLISH, cmp_price=24250.0,
            mtf_suite=self.mtf_suite, indicators=self.indicators, option_quote=quote
        )
        opt_rr = levels["option_premium_risk_reward"]
        self.assertEqual(opt_rr["status"], "UNAVAILABLE")
        self.assertEqual(opt_rr["reason"], "STALE_QUOTE")


class TestExchangeContractMasterAuthority(unittest.TestCase):
    """
    Validates Exchange Contract Master as the authoritative source for expiry,
    lot size, active status, and underlying mapping.
    """

    def setUp(self):
        self.now_ist = datetime(2026, 11, 10, 10, 30, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))

    def test_contract_master_exact_expiry_authoritative(self):
        """Authoritative expiry in contract master is verified and passed."""
        master = {
            "NIFTY26NOV24200CE": {
                "symbol": "NIFTY26NOV24200CE",
                "token": "43210",
                "expiry": "2026-11-23",
                "lot_size": 65,
                "status": "ACTIVE",
                "option_type": "CE",
                "underlying": "NIFTY 50",
                "strike": 24200.0
            }
        }
        contract = {
            "symbol": "NIFTY26NOV24200CE",
            "option_type": "CE",
            "strike": 24200.0,
            "ltp": 150.0,
            "bid": 149.0,
            "ask": 151.0,
            "oi": 100000,
            "volume": 20000,
            "expiry_date": "2026-11-23",
            "lot_size": 65,
            "instrument_token": "43210",
            "last_trade_time": self.now_ist.isoformat(),
            "status": "ACTIVE"
        }
        res = validate_option_contract_against_master(
            contract, "NIFTY 50", expected_expiry_date="2026-11-23",
            current_dt=self.now_ist, contract_master=master
        )
        self.assertTrue(res["is_tradable"])
        self.assertTrue(res["is_contract_master_verified"])
        self.assertEqual(res["master_validation_status"], "VERIFIED_IN_CONTRACT_MASTER")
        self.assertEqual(res["expiry_date"], "2026-11-23")

    def test_contract_master_mismatched_expiry_fails_closed(self):
        """Master expiry differs from quote expiry -> fails closed."""
        master = {
            "NIFTY26NOV24200CE": {
                "symbol": "NIFTY26NOV24200CE",
                "token": "43210",
                "expiry": "2026-11-23",
                "lot_size": 65,
                "status": "ACTIVE"
            }
        }
        contract = {
            "symbol": "NIFTY26NOV24200CE",
            "option_type": "CE",
            "strike": 24200.0,
            "ltp": 150.0,
            "bid": 149.0,
            "ask": 151.0,
            "oi": 100000,
            "volume": 20000,
            "expiry_date": "2026-11-24",  # Quote has wrong unadjusted date
            "lot_size": 65,
            "instrument_token": "43210",
            "last_trade_time": self.now_ist.isoformat(),
            "status": "ACTIVE"
        }
        res = validate_option_contract_against_master(
            contract, "NIFTY 50", expected_expiry_date="2026-11-23",
            current_dt=self.now_ist, contract_master=master
        )
        self.assertFalse(res["is_tradable"])
        self.assertTrue(any("does not match authoritative master expiry" in r for r in res["rejection_reasons"]))

    def test_contract_master_expired_instrument_fails_closed(self):
        """Contract expired in master prior to current date fails closed."""
        master = {
            "NIFTY26NOV24200CE": {
                "symbol": "NIFTY26NOV24200CE",
                "token": "43210",
                "expiry": "2026-11-05",  # Already expired relative to Nov 10
                "lot_size": 65,
                "status": "ACTIVE"
            }
        }
        contract = {
            "symbol": "NIFTY26NOV24200CE",
            "option_type": "CE",
            "strike": 24200.0,
            "ltp": 150.0,
            "bid": 149.0,
            "ask": 151.0,
            "oi": 100000,
            "volume": 20000,
            "expiry_date": "2026-11-05",
            "lot_size": 65,
            "instrument_token": "43210",
            "last_trade_time": self.now_ist.isoformat(),
            "status": "ACTIVE"
        }
        res = validate_option_contract_against_master(
            contract, "NIFTY 50", current_dt=self.now_ist, contract_master=master
        )
        self.assertFalse(res["is_tradable"])
        self.assertTrue(any("Contract expired in exchange contract master" in r for r in res["rejection_reasons"]))

    def test_contract_master_unavailable_exposes_unvalidated_status(self):
        """When contract master is None, explicitly exposes UNVALIDATED_STANDALONE status."""
        contract = {
            "symbol": "NIFTY26NOV24200CE",
            "option_type": "CE",
            "strike": 24200.0,
            "ltp": 150.0,
            "bid": 149.0,
            "ask": 151.0,
            "oi": 100000,
            "volume": 20000,
            "expiry_date": "2026-11-23",
            "lot_size": 65,
            "instrument_token": "43210",
            "last_trade_time": self.now_ist.isoformat(),
            "status": "ACTIVE"
        }
        res = validate_option_contract_against_master(
            contract, "NIFTY 50", expected_expiry_date="2026-11-23",
            current_dt=self.now_ist, contract_master=None
        )
        self.assertTrue(res["is_tradable"])
        self.assertFalse(res["is_contract_master_verified"])
        self.assertEqual(res["master_validation_status"], "UNVALIDATED_STANDALONE")

    def test_calendar_master_disagreement_warning_master_governs(self):
        """If local calendar calculates 2026-11-24 but master is 2026-11-23, master governs and warning is recorded."""
        master = {
            "NIFTY26NOV24200CE": {
                "symbol": "NIFTY26NOV24200CE",
                "token": "43210",
                "expiry": "2026-11-23",
                "lot_size": 65,
                "status": "ACTIVE"
            }
        }
        contract = {
            "symbol": "NIFTY26NOV24200CE",
            "option_type": "CE",
            "strike": 24200.0,
            "ltp": 150.0,
            "bid": 149.0,
            "ask": 151.0,
            "oi": 100000,
            "volume": 20000,
            "expiry_date": "2026-11-23",
            "lot_size": 65,
            "instrument_token": "43210",
            "last_trade_time": self.now_ist.isoformat(),
            "status": "ACTIVE"
        }
        # Expected calendar has 2026-11-24 (unadjusted)
        res = validate_option_contract_against_master(
            contract, "NIFTY 50", expected_expiry_date="2026-11-24",
            current_dt=self.now_ist, contract_master=master
        )
        self.assertTrue(res["is_tradable"])
        self.assertEqual(res["expiry_date"], "2026-11-23")
        self.assertTrue(any("CALENDAR_MASTER_DISAGREEMENT" in w for w in res["warnings"]))


if __name__ == "__main__":
    unittest.main()
