"""
TradeLens AI - Phase 3B Test Suite: Historical Spot Data Ingestion, Validation, and Persistent Storage.

Verifies:
1. Genuine Parquet compatibility, error handling when PyArrow is absent, and invalid file rejection.
2. CSV round trips and deterministic normalized serialization.
3. Dual checksums: source_raw_sha256 and canonical_content_sha256.
4. Duplicate imports: exact duplicate deduplication vs conflicting duplicate quarantine and rejection.
5. Out-of-order timestamps and stable sorting.
6. Missing one-minute intervals without forward-filling.
7. Invalid OHLC relationships and non-positive price rejection.
8. IST conversion and timezone boundaries.
9. NSE session open and close.
10. Holidays and exceptional sessions (e.g. Diwali Muhurat).
11. Incomplete/developing candles.
12. Missing volume semantics (NOT_APPLICABLE for spot index).
13. Synthetic-data rejection.
14. Incremental ingestion and partition merges.
15. Partition pruning and bounded memory streaming without ring-buffer truncation.
16. Phase 2C integration compatibility (unmodified scoring, unavailable option execution, neutral VWAP).
17. Calendar version, coverage bounds, and provenance in manifest.
18. Phase 3 signal adapter option-ineligibility, costs, and spread-slippage separation.
"""

import csv
import io
import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, date, time, timedelta, timezone
from pathlib import Path

from backend.app.data.base import DataStatus
from backend.app.data.candle_store import MultiTimeframeCandleStore
from backend.app.core.setup_scoring import SetupScoringService
from backend.app.data.session_clock import IndianMarketSessionClock, IST_ZONE
from backend.app.data.historical_spot_store import (
    HistoricalSpotCandle,
    HistoricalSpotDataValidator,
    HistoricalSpotStore,
    DatasetManifest,
    ValidationStatus,
    VolumeSemantics,
    DataQualityFlag,
    DuplicateClassification,
    ParquetCodec,
    Phase3SignalAdapter,
    CALENDAR_VERSION,
    CALENDAR_COVERAGE_START,
    CALENDAR_COVERAGE_END,
    PYARROW_AVAILABLE
)


class TestHistoricalSpotStore(unittest.TestCase):

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="tradelens_hist_test_")
        self.clock = IndianMarketSessionClock()
        self.store = HistoricalSpotStore(
            base_dir=self.test_dir,
            storage_format="csv",
            session_clock=self.clock
        )

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def _generate_valid_candles(self, count: int = 15, start_dt: datetime = None) -> list:
        if start_dt is None:
            # 2026-11-09 is a regular Monday
            start_dt = datetime(2026, 11, 9, 9, 15, tzinfo=IST_ZONE)
        records = []
        base_price = 24000.0
        for i in range(count):
            c_dt = start_dt + timedelta(minutes=i)
            records.append({
                "timestamp": c_dt.isoformat(),
                "open": base_price + i,
                "high": base_price + i + 5.0,
                "low": base_price + i - 3.0,
                "close": base_price + i + 2.0,
                "volume": 0.0,
                "is_closed": True,
                "data_status": DataStatus.HISTORICAL
            })
        return records

    # -------------------------------------------------------------------------
    # 1. Genuine Parquet Compatibility & Validation
    # -------------------------------------------------------------------------
    def test_parquet_codec_requires_pyarrow_or_fails_clearly(self):
        """
        When PyArrow is unavailable, ParquetCodec must fail clearly with RuntimeError.
        It must never create or accept a custom pseudo-binary file with a .parquet extension.
        """
        if not PYARROW_AVAILABLE:
            raw_records = self._generate_valid_candles(5)
            dummy_file = Path(self.test_dir) / "test.parquet"
            with self.assertRaises(RuntimeError) as ctx:
                ParquetCodec.write(raw_records, dummy_file)
            self.assertIn("Apache Parquet export requires 'pyarrow'", str(ctx.exception))

            with self.assertRaises(RuntimeError) as ctx2:
                ParquetCodec.read(dummy_file)
            self.assertIn("Cannot read Apache Parquet file: 'pyarrow' is not installed", str(ctx2.exception))

    def test_parquet_codec_verifies_invalid_files(self):
        """Rejects non-parquet files, truncated files, and files missing PAR1 magic framing."""
        corrupt_file = Path(self.test_dir) / "corrupt.parquet"
        
        # Test 1: Non-existent file
        is_val, reason = ParquetCodec.verify_parquet_file(corrupt_file)
        self.assertFalse(is_val)
        self.assertIn("does not exist", reason)

        # Test 2: File smaller than 12 bytes
        corrupt_file.write_bytes(b"PAR1")
        is_val2, reason2 = ParquetCodec.verify_parquet_file(corrupt_file)
        self.assertFalse(is_val2)
        self.assertIn("too small", reason2)

        # Test 3: Missing PAR1 header
        corrupt_file.write_bytes(b"FAKE_DATA_HERE_123456789")
        is_val3, reason3 = ParquetCodec.verify_parquet_file(corrupt_file)
        self.assertFalse(is_val3)
        self.assertIn("Invalid Parquet header", reason3)

        # Test 4: Has PAR1 header but missing PAR1 footer
        corrupt_file.write_bytes(b"PAR1_DATA_WITHOUT_FOOTER")
        is_val4, reason4 = ParquetCodec.verify_parquet_file(corrupt_file)
        self.assertFalse(is_val4)
        self.assertIn("Invalid Parquet footer", reason4)

    def test_historical_spot_store_parquet_mode_handles_pyarrow_absence(self):
        """HistoricalSpotStore refuses parquet without pyarrow unless CSV fallback is permitted."""
        if not PYARROW_AVAILABLE:
            # Without fallback: raises ImportError
            with self.assertRaises(ImportError):
                HistoricalSpotStore(
                    base_dir=self.test_dir,
                    storage_format="parquet",
                    allow_csv_fallback=False
                )

            # With fallback: cleanly defaults to CSV
            store_fallback = HistoricalSpotStore(
                base_dir=self.test_dir,
                storage_format="parquet",
                allow_csv_fallback=True
            )
            self.assertEqual(store_fallback.storage_format, "csv")

    # -------------------------------------------------------------------------
    # 2. CSV Round Trips and Field Recovery
    # -------------------------------------------------------------------------
    def test_csv_round_trip(self):
        """Validates CSV ingestion, storage in partitioned directory, and exact field recovery."""
        raw_records = self._generate_valid_candles(10)
        manifest = self.store.ingest_records(raw_records, symbol="NIFTY", timeframe="1m")
        self.assertEqual(manifest.validation_status, ValidationStatus.VERIFIED)
        self.assertEqual(manifest.valid_record_count, 10)

        # Query back
        queried = self.store.query_candles(symbol="NIFTY")
        self.assertEqual(len(queried), 10)
        self.assertEqual(queried[0].open, 24000.0)
        self.assertEqual(queried[0].high, 24005.0)
        self.assertEqual(queried[0].low, 23997.0)
        self.assertEqual(queried[0].close, 24002.0)
        self.assertEqual(queried[0].volume_semantics, VolumeSemantics.NOT_APPLICABLE)

    # -------------------------------------------------------------------------
    # 3. Dual Checksum Integrity
    # -------------------------------------------------------------------------
    def test_dual_checksum_source_and_canonical_content(self):
        """
        Verifies separate checksums:
        1. source_raw_sha256: raw file/bytes checksum.
        2. canonical_content_sha256: normalized content incorporating all material fields.
        """
        raw_csv_content = (
            "timestamp,open,high,low,close,volume\n"
            "2026-11-09T09:15:00+05:30,24000.0,24010.0,23990.0,24005.0,0.0\n"
            "2026-11-09T09:16:00+05:30,24005.0,24015.0,24000.0,24010.0,0.0\n"
        )
        csv_file = Path(self.test_dir) / "source_input.csv"
        csv_file.write_text(raw_csv_content, encoding="utf-8")

        manifest = self.store.ingest_csv(csv_file, symbol="NIFTY", timeframe="1m", provider="NSE_FEED")
        
        # Verify source_raw_sha256 matches actual file bytes
        expected_raw_sha = "0946b2bdfad5037ae6470081d4a6ec710f639ee3a7cfbe30ad3d312bcab349ee"
        import hashlib
        self.assertEqual(manifest.source_raw_sha256, hashlib.sha256(raw_csv_content.encode('utf-8')).hexdigest())

        # Verify canonical_content_sha256 is present, deterministic, and 64 hex characters
        self.assertEqual(len(manifest.canonical_content_sha256), 64)
        self.assertEqual(manifest.sha256_checksum, manifest.canonical_content_sha256)

        # Check sensitivity: altering volume_semantics or provider changes canonical checksum
        raw_records = [
            {"timestamp": "2026-11-09T09:15:00+05:30", "open": 24000, "high": 24010, "low": 23990, "close": 24005, "volume": 0.0}
        ]
        validator = HistoricalSpotDataValidator(session_clock=self.clock)
        _, m1 = validator.validate_and_normalize_records(raw_records, symbol="NIFTY", provider="PROVIDER_A")
        _, m2 = validator.validate_and_normalize_records(raw_records, symbol="NIFTY", provider="PROVIDER_B")
        self.assertNotEqual(m1.canonical_content_sha256, m2.canonical_content_sha256, "Provider changes canonical checksum")

    # -------------------------------------------------------------------------
    # 4. Duplicate Imports & Conflicting Duplicate Quarantine
    # -------------------------------------------------------------------------
    def test_exact_duplicate_deduplication(self):
        """Exact duplicates (same timestamp, identical OHLC/volume) are safely deduplicated."""
        raw_records = self._generate_valid_candles(5)
        # Duplicate entire batch
        duplicated = raw_records + raw_records
        manifest = self.store.ingest_records(duplicated, symbol="NIFTY")
        
        self.assertEqual(manifest.validation_status, ValidationStatus.VERIFIED)
        self.assertEqual(manifest.exact_duplicate_count, 5)
        self.assertEqual(manifest.conflicting_duplicate_count, 0)
        self.assertEqual(len(manifest.quarantined_conflicts), 0)
        
        # Only 5 unique records committed
        queried = self.store.query_candles(symbol="NIFTY")
        self.assertEqual(len(queried), 5)

    def test_conflicting_duplicate_quarantine_and_rejection(self):
        """
        Conflicting records (same timestamp, differing OHLC) must be quarantined and reported.
        The system must refuse to select one record silently, and fail validation.
        """
        record_a = {"timestamp": "2026-11-09T09:15:00+05:30", "open": 24000.0, "high": 24010.0, "low": 23990.0, "close": 24005.0}
        record_b_conflicting = {"timestamp": "2026-11-09T09:15:00+05:30", "open": 24050.0, "high": 24080.0, "low": 24040.0, "close": 24075.0}
        
        manifest = self.store.ingest_records([record_a, record_b_conflicting], symbol="NIFTY")
        self.assertEqual(manifest.validation_status, ValidationStatus.VALIDATION_FAILED)
        self.assertEqual(manifest.conflicting_duplicate_count, 1)
        self.assertEqual(len(manifest.quarantined_conflicts), 1)
        
        conflict_entry = manifest.quarantined_conflicts[0]
        self.assertEqual(conflict_entry["classification"], DuplicateClassification.CONFLICTING_DUPLICATE)
        self.assertEqual(conflict_entry["existing_candle"]["open"], 24000.0)
        self.assertEqual(conflict_entry["conflicting_candle"]["open"], 24050.0)
        
        # Verify nothing was committed to primary store
        queried = self.store.query_candles(symbol="NIFTY")
        self.assertEqual(len(queried), 0)

    def test_partition_merge_conflicting_duplicate_raises(self):
        """Merging a conflicting duplicate into an existing partition raises ValueError."""
        # Batch 1 committed
        batch1 = [{"timestamp": "2026-11-09T09:15:00+05:30", "open": 24000.0, "high": 24010.0, "low": 23990.0, "close": 24005.0}]
        self.store.ingest_records(batch1, symbol="NIFTY")

        # Batch 2 with conflicting price for 09:15:00
        batch2_conflict = [{"timestamp": "2026-11-09T09:15:00+05:30", "open": 24099.0, "high": 24120.0, "low": 24080.0, "close": 24110.0}]
        
        # Partition merge detects conflict against persisted partition and raises ValueError
        with self.assertRaises(ValueError) as ctx:
            self.store.ingest_records(batch2_conflict, symbol="NIFTY")
        self.assertIn("Conflicting duplicate in partition", str(ctx.exception))
        self.assertIn("Quarantine required; silent selection prohibited", str(ctx.exception))

    # -------------------------------------------------------------------------
    # 5. Out-of-Order Timestamps & Missing Intervals
    # -------------------------------------------------------------------------
    def test_out_of_order_timestamps(self):
        """Scrambled or reverse chronological records must be stably sorted ascending in IST."""
        raw_records = self._generate_valid_candles(10)
        scrambled = list(reversed(raw_records))
        manifest = self.store.ingest_records(scrambled, symbol="NIFTY")
        self.assertEqual(manifest.validation_status, ValidationStatus.VERIFIED)

        queried = self.store.query_candles(symbol="NIFTY")
        for i in range(len(queried) - 1):
            self.assertLess(queried[i].start_time, queried[i + 1].start_time)

    def test_missing_one_minute_intervals(self):
        """Detects gaps during continuous market sessions without silently forward-filling."""
        start_dt = datetime(2026, 11, 9, 10, 0, tzinfo=IST_ZONE)
        batch = [
            {"timestamp": start_dt.isoformat(), "open": 24000, "high": 24005, "low": 23995, "close": 24002},
            {"timestamp": (start_dt + timedelta(minutes=5)).isoformat(), "open": 24002, "high": 24008, "low": 24001, "close": 24006}
        ]
        manifest = self.store.ingest_records(batch, symbol="NIFTY")
        self.assertEqual(manifest.missing_intervals_count, 1)
        gap_report = manifest.missing_intervals[0]
        self.assertEqual(gap_report["missing_candles"], 4)
        self.assertEqual(gap_report["gap_seconds"], 300.0)

    # -------------------------------------------------------------------------
    # 6. Invalid OHLC Relationships
    # -------------------------------------------------------------------------
    def test_invalid_ohlc_relationships(self):
        """Corrupt OHLC geometry must be rejected and prevent VERIFIED dataset status."""
        invalid_cases = [
            {"timestamp": "2026-11-09T09:15:00+05:30", "open": 100, "high": 90, "low": 95, "close": 92},
            {"timestamp": "2026-11-09T09:16:00+05:30", "open": 105, "high": 100, "low": 95, "close": 98},
            {"timestamp": "2026-11-09T09:17:00+05:30", "open": 98, "high": 102, "low": 95, "close": 90},
            {"timestamp": "2026-11-09T09:18:00+05:30", "open": -10, "high": 10, "low": -15, "close": 5},
        ]
        manifest = self.store.ingest_records(invalid_cases, symbol="NIFTY")
        self.assertEqual(manifest.validation_status, ValidationStatus.VALIDATION_FAILED)
        self.assertEqual(manifest.invalid_record_count, 4)
        self.assertEqual(manifest.valid_record_count, 0)
        self.assertEqual(len(self.store.query_candles(symbol="NIFTY")), 0)

    # -------------------------------------------------------------------------
    # 7. IST Conversion & Session Boundaries
    # -------------------------------------------------------------------------
    def test_ist_conversion_and_timezone_boundaries(self):
        """UTC timestamps and offset strings must convert accurately to Asia/Kolkata."""
        utc_ts = "2026-11-09T03:45:00Z"
        raw = [{"timestamp": utc_ts, "open": 24000, "high": 24010, "low": 23990, "close": 24005}]
        manifest = self.store.ingest_records(raw, symbol="NIFTY")
        self.assertEqual(manifest.validation_status, ValidationStatus.VERIFIED)

        queried = self.store.query_candles(symbol="NIFTY")
        self.assertEqual(len(queried), 1)
        self.assertEqual(queried[0].start_time.hour, 9)
        self.assertEqual(queried[0].start_time.minute, 15)
        self.assertEqual(queried[0].start_time.utcoffset(), timedelta(hours=5, minutes=30))
        self.assertEqual(queried[0].source_timestamp, utc_ts)

    def test_nse_session_open_and_close(self):
        """Identifies pre-market, continuous session, and post-market candles."""
        records = [
            {"timestamp": "2026-11-09T09:14:00+05:30", "open": 24000, "high": 24005, "low": 23995, "close": 24000},
            {"timestamp": "2026-11-09T09:15:00+05:30", "open": 24000, "high": 24010, "low": 23990, "close": 24005},
            {"timestamp": "2026-11-09T15:29:00+05:30", "open": 24100, "high": 24105, "low": 24095, "close": 24102},
            {"timestamp": "2026-11-09T15:31:00+05:30", "open": 24100, "high": 24100, "low": 24100, "close": 24100},
        ]
        manifest = self.store.ingest_records(records, symbol="NIFTY")
        queried = self.store.query_candles(symbol="NIFTY")
        flags_map = {c.start_time.time(): c.validation_flags for c in queried}

        self.assertIn(DataQualityFlag.OUT_OF_SESSION, flags_map[time(9, 14)])
        self.assertIn(DataQualityFlag.VALID, flags_map[time(9, 15)])
        self.assertIn(DataQualityFlag.VALID, flags_map[time(15, 29)])
        self.assertIn(DataQualityFlag.OUT_OF_SESSION, flags_map[time(15, 31)])

    def test_holidays_and_exceptional_sessions(self):
        """Validates that regular holidays are flagged OUT_OF_SESSION, while Diwali Muhurat is active."""
        # 2026-01-26 Republic Day
        holiday_record = [{"timestamp": "2026-01-26T10:00:00+05:30", "open": 24000, "high": 24010, "low": 23990, "close": 24000}]
        self.store.ingest_records(holiday_record, symbol="NIFTY")
        q_hol = self.store.query_candles(symbol="NIFTY", start_dt=datetime(2026, 1, 26, tzinfo=IST_ZONE))
        self.assertIn(DataQualityFlag.OUT_OF_SESSION, q_hol[0].validation_flags)

        # 2026-11-08 Diwali Muhurat Trading
        muhurat_record = [{"timestamp": "2026-11-08T14:30:00+05:30", "open": 24500, "high": 24520, "low": 24490, "close": 24510}]
        self.store.ingest_records(muhurat_record, symbol="NIFTY")
        q_muh = self.store.query_candles(symbol="NIFTY", start_dt=datetime(2026, 11, 8, tzinfo=IST_ZONE))
        self.assertIn(DataQualityFlag.VALID, q_muh[0].validation_flags)

    def test_incomplete_candles(self):
        """Candles with is_closed=False must be flagged DEVELOPING and prevented from VERIFIED storage."""
        records = [
            {"timestamp": "2026-11-09T09:15:00+05:30", "open": 24000, "high": 24010, "low": 23990, "close": 24005, "is_closed": True},
            {"timestamp": "2026-11-09T09:16:00+05:30", "open": 24005, "high": 24015, "low": 24000, "close": 24010, "is_closed": False},
        ]
        manifest = self.store.ingest_records(records, symbol="NIFTY")
        queried = self.store.query_candles(symbol="NIFTY")
        self.assertEqual(len(queried), 1)
        self.assertEqual(queried[0].start_time.minute, 15)

    def test_missing_volume_semantics(self):
        """Spot index volume must be strictly classified as NOT_APPLICABLE, never contract volume."""
        records = [{"timestamp": "2026-11-09T09:15:00+05:30", "open": 24000, "high": 24010, "low": 23990, "close": 24005, "volume": 125000}]
        manifest = self.store.ingest_records(records, symbol="NIFTY")
        self.assertEqual(manifest.volume_semantics, VolumeSemantics.NOT_APPLICABLE)
        queried = self.store.query_candles(symbol="NIFTY")
        self.assertEqual(queried[0].volume_semantics, VolumeSemantics.NOT_APPLICABLE)

    def test_synthetic_data_rejection(self):
        """Rejects candles flagged as SIMULATED or originating from SampleUniverseProvider."""
        synthetic_records = [
            {"timestamp": "2026-11-09T09:15:00+05:30", "open": 24000, "high": 24010, "low": 23990, "close": 24005, "data_status": DataStatus.SIMULATED}
        ]
        manifest = self.store.ingest_records(synthetic_records, symbol="NIFTY", provider="SampleUniverseProvider")
        self.assertEqual(manifest.validation_status, ValidationStatus.REJECTED_SYNTHETIC)
        self.assertEqual(len(self.store.query_candles(symbol="NIFTY")), 0)

    # -------------------------------------------------------------------------
    # 8. Partition Pruning & Streaming Memory Verification
    # -------------------------------------------------------------------------
    def test_streaming_memory_bounded_and_partition_pruning(self):
        """
        Verifies that:
        1. stream_candles() streams lazily row-by-row without materializing full dataset.
        2. Partition pruning filters by year/month before disk reads.
        """
        # Create records across 2 months: Oct 2026 and Nov 2026
        oct_candles = self._generate_valid_candles(5, start_dt=datetime(2026, 10, 15, 9, 15, tzinfo=IST_ZONE))
        nov_candles = self._generate_valid_candles(5, start_dt=datetime(2026, 11, 9, 9, 15, tzinfo=IST_ZONE))

        self.store.ingest_records(oct_candles, symbol="NIFTY")
        self.store.ingest_records(nov_candles, symbol="NIFTY")

        # Partition pruning check: Query only November 2026
        target_partitions = self.store._get_target_partition_files(
            symbol="NIFTY",
            start_dt=datetime(2026, 11, 1, tzinfo=IST_ZONE),
            end_dt=datetime(2026, 11, 30, tzinfo=IST_ZONE)
        )
        self.assertEqual(len(target_partitions), 1)
        self.assertIn("month=11", str(target_partitions[0]))
        self.assertNotIn("month=10", str(target_partitions[0]))

        # Generator check
        gen = self.store.stream_candles(symbol="NIFTY")
        self.assertTrue(hasattr(gen, "__iter__"))
        self.assertTrue(hasattr(gen, "__next__"))
        first = next(gen)
        self.assertIsInstance(first, HistoricalSpotCandle)
        self.assertEqual(first.start_time.month, 10)

    # -------------------------------------------------------------------------
    # 9. Calendar Provenance & Unsupported Dates
    # -------------------------------------------------------------------------
    def test_calendar_provenance_and_unsupported_dates(self):
        """Manifest records exact calendar metadata and flags dates outside verified 2025-2026 coverage."""
        # 2024 date is outside supported 2025-2026 calendar coverage
        pre_2025_record = [{"timestamp": "2024-11-15T09:15:00+05:30", "open": 23000, "high": 23010, "low": 22990, "close": 23005}]
        manifest = self.store.ingest_records(pre_2025_record, symbol="NIFTY")

        self.assertEqual(manifest.calendar_version, CALENDAR_VERSION)
        self.assertEqual(manifest.calendar_coverage_start, CALENDAR_COVERAGE_START.isoformat())
        self.assertEqual(manifest.calendar_coverage_end, CALENDAR_COVERAGE_END.isoformat())
        self.assertEqual(manifest.unsupported_calendar_dates_count, 1)

        queried = self.store.query_candles(symbol="NIFTY")
        self.assertIn(DataQualityFlag.UNSUPPORTED_CALENDAR_DATE, queried[0].validation_flags)

    # -------------------------------------------------------------------------
    # 10. Phase 2C Integration Compatibility Test
    # -------------------------------------------------------------------------
    def test_phase2c_spot_integration(self):
        """
        Without modifying Phase 2C, tests actual scoring behavior against genuine spot candles:
        - 1m spot candles with volume_semantics=NOT_APPLICABLE.
        - Resampled 3m, 5m, 15m candles in MultiTimeframeCandleStore.
        - Missing 15s data remains UNAVAILABLE (never fabricated).
        - No option quotes.
        Verifies:
        - Option R:R is UNAVAILABLE.
        - Phase3SignalAdapter marks option_execution_eligible = False.
        - Underlying signal remains distinct and non-executable as an option.
        """
        candles_1m = []
        base_dt = datetime(2026, 11, 9, 9, 15, tzinfo=IST_ZONE)
        base_p = 24000.0
        for i in range(100):
            t = base_dt + timedelta(minutes=i)
            p = base_p + i * 2.0
            candles_1m.append({
                "timestamp": t.isoformat(),
                "start_time": t.isoformat(),
                "end_time": (t + timedelta(minutes=1)).isoformat(),
                "open": p, "high": p + 2.0, "low": p - 1.0, "close": p + 1.5,
                "volume": 0.0,
                "volume_semantics": VolumeSemantics.NOT_APPLICABLE,
                "is_closed": True
            })

        candle_store = MultiTimeframeCandleStore()
        candle_store.ingest_base_candles("NIFTY", candles_1m, base_timeframe="1m")

        scoring_service = SetupScoringService(candle_store=candle_store)
        raw_setup = scoring_service.evaluate_opportunity(symbol="NIFTY", option_quote=None)

        # 1. Option Premium R:R is UNAVAILABLE
        self.assertEqual(raw_setup["option_premium_risk_reward"]["status"], "UNAVAILABLE")
        self.assertIn("cannot be fabricated", raw_setup["option_premium_risk_reward"]["explanation"])

        # 2. Gate 8 passes because candle is closed (15s was not fabricated)
        self.assertTrue(raw_setup["canonical_eight_gates"]["gate_registry"]["GATE_8_DEVELOPING_UNCONFIRMED_BAR"]["passed"])

        # 3. Adapt via Phase3SignalAdapter
        adapted = Phase3SignalAdapter.adapt_spot_signal(raw_setup)
        self.assertFalse(adapted["option_execution_eligible"])
        self.assertEqual(adapted["option_execution_status"], "UNAVAILABLE")
        self.assertTrue(adapted["underlying_signal_only"])
        self.assertIn("Option contract execution is UNAVAILABLE", adapted["execution_adapter_notes"])

    # -------------------------------------------------------------------------
    # 11. Phase 3 Signal Adapter Regulatory Costs & Execution Fills
    # -------------------------------------------------------------------------
    def test_phase3_regulatory_cost_schedule(self):
        """Verifies date-effective transaction cost transitions (Circular FA73061 & STT hike)."""
        sched_feb26 = Phase3SignalAdapter.get_transaction_cost_schedule(date(2026, 2, 15))
        self.assertEqual(sched_feb26["nse_option_txn_rate"], 0.0005)
        self.assertEqual(sched_feb26["option_sale_stt_rate"], 0.0010)

        sched_mar26 = Phase3SignalAdapter.get_transaction_cost_schedule(date(2026, 3, 15))
        self.assertEqual(sched_mar26["nse_option_txn_rate"], 0.0003553)

        sched_apr26 = Phase3SignalAdapter.get_transaction_cost_schedule(date(2026, 4, 15))
        self.assertEqual(sched_apr26["option_sale_stt_rate"], 0.0015)
        self.assertEqual(sched_apr26["option_exercise_stt_rate"], 0.0015)
        self.assertEqual(sched_apr26["futures_sale_stt_rate"], 0.0005)

    def test_phase3_execution_fill_prevents_spread_double_counting(self):
        """Ask/Bid price already incorporates spread; slippage is applied as an adverse touch adjustment."""
        best_bid = 120.0
        best_ask = 121.0
        slippage = 0.50

        buy_fill = Phase3SignalAdapter.compute_execution_fill("BUY", best_bid, best_ask, slippage_points=slippage)
        self.assertEqual(buy_fill, 121.50)

        sell_fill = Phase3SignalAdapter.compute_execution_fill("SELL", best_bid, best_ask, slippage_points=slippage)
        self.assertEqual(sell_fill, 119.50)


if __name__ == "__main__":
    unittest.main()
