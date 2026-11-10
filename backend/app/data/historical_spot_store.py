"""
TradeLens AI - Phase 3B: Historical Spot Data Acquisition, Validation, and Persistent Storage.

Architectural Guarantees:
1. Genuine Parquet Compatibility:
   - When pyarrow is available, writes and reads genuine Apache Parquet files.
   - When pyarrow is unavailable, fails clearly or uses a separately named genuine CSV fallback.
   - Never writes a custom pseudo-binary format with a '.parquet' extension.
2. Dual Checksum Integrity:
   - source_raw_sha256: Hash of the raw input file/bytes.
   - canonical_content_sha256: Deterministic hash covering all material fields (symbol, timeframe,
     timestamps in IST, OHLC, volume, volume semantics, data status, closed flag, source, provider).
3. Explicit Duplicate & Conflict Handling:
   - Exact duplicates are safely deduplicated.
   - Conflicting duplicates (same timestamp, differing OHLC/volume) are quarantined and reported;
     the system refuses to mark the dataset as VERIFIED. Silent selection is prohibited.
4. Bounded Memory Streaming & Partition Pruning:
   - stream_candles() reads partition files iteratively and streams records lazily.
   - Partitions are filtered by year/month prior to disk reads.
   - Memory usage remains bounded as dataset size increases.
5. Official NSE Calendar Provenance:
   - Validates against IndianMarketSessionClock (with calendar version, coverage bounds, and assumptions).
6. Phase 3 Signal Adapter:
   - Underlying spot signals strictly marked option_execution_eligible = False.
   - Date-effective transaction costs (Circular FA73061 & STT schedules).
   - Prevention of bid/ask spread double-counting.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
from dataclasses import dataclass, field, asdict, fields
from datetime import datetime, date, time, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Any, Union, Generator, Tuple, Set
import zoneinfo

from backend.app.data.base import DataStatus
from backend.app.data.session_clock import IndianMarketSessionClock, MarketSessionState, IST_ZONE

# Attempt to import pyarrow for native Parquet handling if available
try:
    import pyarrow as pa
    import pyarrow.parquet as pq
    PYARROW_AVAILABLE = True
except ImportError:
    pa = None
    pq = None
    PYARROW_AVAILABLE = False


# ============================================================================
# 1. Enums and Constants
# ============================================================================

class VolumeSemantics:
    NOT_APPLICABLE = "NOT_APPLICABLE"      # Mandatory for spot indices (e.g. NIFTY 50, BANKNIFTY)
    UNAVAILABLE = "UNAVAILABLE"            # When provider does not report volume
    CONTRACT_VOLUME = "CONTRACT_VOLUME"    # Traded shares/contracts (for equities/futures)


class ValidationStatus:
    VERIFIED = "VERIFIED"                  # Passed all structural, temporal, and market-session checks
    VALIDATION_FAILED = "VALIDATION_FAILED"# Failed critical validation (OHLC corruption, timestamps, etc.)
    REJECTED_SYNTHETIC = "REJECTED_SYNTHETIC" # Contains simulated/synthetic data


class DuplicateClassification:
    EXACT_DUPLICATE = "EXACT_DUPLICATE"
    CONFLICTING_DUPLICATE = "CONFLICTING_DUPLICATE"
    PROVIDER_CORRECTION = "PROVIDER_CORRECTION"


class DataQualityFlag:
    VALID = "VALID"
    DEVELOPING = "DEVELOPING"
    INVALID_OHLC = "INVALID_OHLC"
    OUT_OF_SESSION = "OUT_OF_SESSION"
    EXACT_DUPLICATE = "EXACT_DUPLICATE"
    CONFLICTING_DUPLICATE = "CONFLICTING_DUPLICATE"
    OUT_OF_ORDER = "OUT_OF_ORDER"
    FEED_GAP = "FEED_GAP"
    SYNTHETIC = "SYNTHETIC"
    UNSUPPORTED_CALENDAR_DATE = "UNSUPPORTED_CALENDAR_DATE"
    ISOLATED_POST_GAP_UNVERIFIED = "ISOLATED_POST_GAP_UNVERIFIED"


# Standard NIFTY 50 Index Aliases
INDEX_SYMBOL_ALIASES = {"NIFTY", "NIFTY 50", "NIFTY50", "NSE:NIFTY50-INDEX", "^NSEI"}

# Supported Historical Calendar Metadata
CALENDAR_VERSION = "NSE_OFFICIAL_CALENDAR_2025_2026_V1"
CALENDAR_COVERAGE_START = date(2025, 1, 1)
CALENDAR_COVERAGE_END = date(2026, 12, 31)
CALENDAR_ASSUMPTIONS = (
    "Continuous trading 09:15-15:30 IST Mon-Fri. Official NSE holidays applied for 2025-2026. "
    "Exceptional sessions (e.g. Diwali Muhurat 2026-11-08) explicitly supported. "
    "Pre-2025 sessions lack official holiday data and are flagged as unsupported calendar dates."
)


# ============================================================================
# 2. Data Models
# ============================================================================

@dataclass
class HistoricalSpotCandle:
    symbol: str
    timeframe: str
    start_time: datetime
    end_time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: Optional[float] = None
    volume_semantics: str = VolumeSemantics.NOT_APPLICABLE
    data_status: str = DataStatus.HISTORICAL
    is_closed: bool = True
    source_timestamp: str = ""
    validation_flags: List[str] = field(default_factory=list)

    @property
    def is_isolated_post_gap(self) -> bool:
        return DataQualityFlag.ISOLATED_POST_GAP_UNVERIFIED in self.validation_flags

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "timestamp": self.start_time.isoformat(),
            "start_time": self.start_time.isoformat(),
            "end_time": self.end_time.isoformat(),
            "open": round(float(self.open), 2),
            "high": round(float(self.high), 2),
            "low": round(float(self.low), 2),
            "close": round(float(self.close), 2),
            "volume": self.volume if self.volume is not None else None,
            "volume_semantics": self.volume_semantics,
            "data_status": self.data_status,
            "is_closed": self.is_closed,
            "source_timestamp": self.source_timestamp,
            "validation_flags": list(self.validation_flags)
        }


@dataclass
class DatasetManifest:
    dataset_id: str
    symbol: str
    timeframe: str
    provider: str
    source_file: str
    retrieval_date: str
    dataset_version: str
    record_count: int
    valid_record_count: int
    invalid_record_count: int
    duplicate_count: int
    exact_duplicate_count: int
    conflicting_duplicate_count: int
    quarantined_conflicts: List[Dict[str, Any]]
    missing_intervals_count: int
    missing_intervals: List[Dict[str, Any]]
    start_timestamp_ist: Optional[str]
    end_timestamp_ist: Optional[str]
    timezone_convention: str
    volume_semantics: str
    validation_status: str
    source_raw_sha256: str
    canonical_content_sha256: str
    sha256_checksum: str  # Kept identical to canonical_content_sha256 for backward compatibility
    partition_paths: List[str]
    storage_format: str
    calendar_version: str
    calendar_coverage_start: str
    calendar_coverage_end: str
    calendar_assumptions: str
    unsupported_calendar_dates_count: int
    manifest_created_at: str
    # Phase 3E.1 Provider Cutoff Metadata
    provider_cutoff_observed: bool = False
    last_validated_candle_start: Optional[str] = None
    last_validated_candle_end: Optional[str] = None
    exchange_session_close: str = "15:30:00+05:30"
    provider_cutoff_cause: str = "BROKER_HISTORICAL_FEED_INTRADAY_TRUNCATION"
    provider_cutoff_cause_verified: bool = False
    # Phase 3E.2 Closing Settlement / Isolated Post-Gap Metadata
    isolated_post_gap_count: int = 0
    isolated_post_gap_records: List[Dict[str, Any]] = field(default_factory=list)
    settlement_semantics: str = "UNVERIFIED"

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["PROVIDER_CUTOFF_OBSERVED"] = self.provider_cutoff_observed
        d["LAST_VALIDATED_CANDLE_START"] = self.last_validated_candle_start
        d["LAST_VALIDATED_CANDLE_END"] = self.last_validated_candle_end
        d["EXCHANGE_SESSION_CLOSE"] = self.exchange_session_close
        d["PROVIDER_CUTOFF_CAUSE"] = self.provider_cutoff_cause
        d["PROVIDER_CUTOFF_CAUSE_VERIFIED"] = self.provider_cutoff_cause_verified
        d["ISOLATED_POST_GAP_COUNT"] = self.isolated_post_gap_count
        d["SETTLEMENT_SEMANTICS"] = self.settlement_semantics
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> DatasetManifest:
        clean_data = dict(data)
        if "PROVIDER_CUTOFF_OBSERVED" in clean_data and "provider_cutoff_observed" not in clean_data:
            clean_data["provider_cutoff_observed"] = clean_data["PROVIDER_CUTOFF_OBSERVED"]
        if "LAST_VALIDATED_CANDLE_START" in clean_data and "last_validated_candle_start" not in clean_data:
            clean_data["last_validated_candle_start"] = clean_data["LAST_VALIDATED_CANDLE_START"]
        if "LAST_VALIDATED_CANDLE_END" in clean_data and "last_validated_candle_end" not in clean_data:
            clean_data["last_validated_candle_end"] = clean_data["LAST_VALIDATED_CANDLE_END"]
        if "EXCHANGE_SESSION_CLOSE" in clean_data and "exchange_session_close" not in clean_data:
            clean_data["exchange_session_close"] = clean_data["EXCHANGE_SESSION_CLOSE"]
        if "PROVIDER_CUTOFF_CAUSE" in clean_data and "provider_cutoff_cause" not in clean_data:
            clean_data["provider_cutoff_cause"] = clean_data["PROVIDER_CUTOFF_CAUSE"]
        if "PROVIDER_CUTOFF_CAUSE_VERIFIED" in clean_data and "provider_cutoff_cause_verified" not in clean_data:
            clean_data["provider_cutoff_cause_verified"] = clean_data["PROVIDER_CUTOFF_CAUSE_VERIFIED"]
        if "ISOLATED_POST_GAP_COUNT" in clean_data and "isolated_post_gap_count" not in clean_data:
            clean_data["isolated_post_gap_count"] = clean_data["ISOLATED_POST_GAP_COUNT"]
        if "SETTLEMENT_SEMANTICS" in clean_data and "settlement_semantics" not in clean_data:
            clean_data["settlement_semantics"] = clean_data["SETTLEMENT_SEMANTICS"]

        field_names = {f.name for f in fields(cls)}
        filtered = {k: v for k, v in clean_data.items() if k in field_names}
        return cls(**filtered)


# ============================================================================
# 3. Canonical Parquet & CSV Codecs
# ============================================================================

class ParquetCodec:
    """
    Genuine Apache Parquet Codec.
    - When PyArrow is available: produces and reads genuine Apache Parquet files.
    - When PyArrow is unavailable: raises clear RuntimeError / ImportError.
      Never creates custom binary pseudo-Parquet formats.
    """
    PARQUET_MAGIC = b"PAR1"

    @classmethod
    def is_pyarrow_available(cls) -> bool:
        return PYARROW_AVAILABLE

    @classmethod
    def verify_parquet_file(cls, file_path: Union[str, Path]) -> Tuple[bool, str]:
        """
        Validates whether a file is a genuine Apache Parquet file.
        Checks file existence, minimum size (12 bytes), and PAR1 framing bytes.
        If PyArrow is available, verifies schema readability.
        """
        path = Path(file_path)
        if not path.exists():
            return False, f"File does not exist: {path}"
        
        file_size = path.stat().st_size
        if file_size < 12:
            return False, f"File too small ({file_size} bytes) to be valid Parquet"

        with open(path, "rb") as f:
            header = f.read(4)
            f.seek(file_size - 4)
            footer = f.read(4)

        if header != cls.PARQUET_MAGIC:
            return False, f"Invalid Parquet header magic bytes: {header!r} (expected b'PAR1')"
        if footer != cls.PARQUET_MAGIC:
            return False, f"Invalid Parquet footer magic bytes: {footer!r} (expected b'PAR1')"

        if PYARROW_AVAILABLE:
            try:
                schema = pq.read_schema(str(path))
                if len(schema.names) == 0:
                    return False, "Parquet schema contains no columns"
            except Exception as e:
                return False, f"PyArrow failed to read Parquet schema: {e}"

        return True, "Valid Apache Parquet file"

    @classmethod
    def write(cls, records: List[Dict[str, Any]], target_path: Union[str, Path]) -> None:
        """
        Writes records to genuine Apache Parquet.
        Requires pyarrow. If pyarrow is absent, raises RuntimeError.
        """
        if not PYARROW_AVAILABLE:
            raise RuntimeError(
                "Apache Parquet export requires 'pyarrow'. "
                "PyArrow is not installed in this Python environment. "
                "Install pyarrow or use storage_format='csv'."
            )

        target = Path(target_path)
        target.parent.mkdir(parents=True, exist_ok=True)

        fields = [
            ("symbol", pa.string()),
            ("timeframe", pa.string()),
            ("timestamp", pa.string()),
            ("start_time", pa.string()),
            ("end_time", pa.string()),
            ("open", pa.float64()),
            ("high", pa.float64()),
            ("low", pa.float64()),
            ("close", pa.float64()),
            ("volume", pa.float64()),
            ("volume_semantics", pa.string()),
            ("data_status", pa.string()),
            ("is_closed", pa.bool_()),
            ("source_timestamp", pa.string()),
            ("validation_flags", pa.string())
        ]
        arrow_schema = pa.schema(fields)

        col_data: Dict[str, List[Any]] = {name: [] for name, _ in fields}
        for r in records:
            col_data["symbol"].append(str(r["symbol"]))
            col_data["timeframe"].append(str(r["timeframe"]))
            col_data["timestamp"].append(str(r["timestamp"]))
            col_data["start_time"].append(str(r["start_time"]))
            col_data["end_time"].append(str(r["end_time"]))
            col_data["open"].append(float(r["open"]))
            col_data["high"].append(float(r["high"]))
            col_data["low"].append(float(r["low"]))
            col_data["close"].append(float(r["close"]))
            col_data["volume"].append(float(r["volume"]) if r.get("volume") is not None else None)
            col_data["volume_semantics"].append(str(r.get("volume_semantics", VolumeSemantics.NOT_APPLICABLE)))
            col_data["data_status"].append(str(r.get("data_status", DataStatus.HISTORICAL)))
            col_data["is_closed"].append(bool(r.get("is_closed", True)))
            col_data["source_timestamp"].append(str(r.get("source_timestamp", "")))
            flags = r.get("validation_flags", [])
            col_data["validation_flags"].append(",".join(flags) if isinstance(flags, list) else str(flags))

        table = pa.Table.from_pydict(col_data, schema=arrow_schema)
        pq.write_table(table, str(target), compression="SNAPPY")

    @classmethod
    def read(cls, file_path: Union[str, Path]) -> List[Dict[str, Any]]:
        """
        Reads genuine Apache Parquet into record dictionaries.
        Requires pyarrow. If pyarrow is absent, raises RuntimeError.
        """
        if not PYARROW_AVAILABLE:
            raise RuntimeError(
                "Cannot read Apache Parquet file: 'pyarrow' is not installed. "
                "Install pyarrow to read parquet files or use CSV storage."
            )

        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"Parquet file not found: {path}")

        table = pq.read_table(str(path))
        pydict = table.to_pylist()
        for r in pydict:
            if "validation_flags" in r and isinstance(r["validation_flags"], str):
                raw = r["validation_flags"].strip()
                r["validation_flags"] = [f.strip() for f in raw.split(",") if f.strip()]
        return pydict

    @classmethod
    def iter_records(
        cls,
        file_path: Union[str, Path],
        batch_size: int = 1000
    ) -> Generator[Dict[str, Any], None, None]:
        """
        Streams records from Apache Parquet batch-by-batch without materializing
        the entire file into memory.
        """
        if not PYARROW_AVAILABLE:
            raise RuntimeError(
                "Streaming Apache Parquet requires 'pyarrow'. Install pyarrow or use CSV."
            )

        parquet_file = pq.ParquetFile(str(file_path))
        for batch in parquet_file.iter_batches(batch_size=batch_size):
            for row in batch.to_pylist():
                if "validation_flags" in row and isinstance(row["validation_flags"], str):
                    raw = row["validation_flags"].strip()
                    row["validation_flags"] = [f.strip() for f in raw.split(",") if f.strip()]
                yield row


# ============================================================================
# 4. Data Quality & Structural Validator
# ============================================================================

class HistoricalSpotDataValidator:
    """
    Validates genuine historical NIFTY 50 spot data:
    1. Rejects simulated/synthetic records.
    2. Enforces strict OHLC price geometry (High >= Low, Open/Close bounded).
    3. Detects exact duplicates and quarantines conflicting duplicates.
    4. Validates timestamps against official NSE trading hours and holidays.
    5. Flags developing/incomplete candles.
    6. Verifies spot volume semantics (NOT_APPLICABLE).
    7. Computes dual checksums: source_raw_sha256 and canonical_content_sha256.
    """

    def __init__(self, session_clock: Optional[IndianMarketSessionClock] = None):
        self.session_clock = session_clock or IndianMarketSessionClock()

    @staticmethod
    def normalize_timestamp_to_ist(raw_ts: Any) -> datetime:
        """
        Parses raw timestamp and normalizes to Asia/Kolkata (UTC+05:30).
        Accepts ISO strings, unix timestamps, and datetime objects.
        """
        if isinstance(raw_ts, datetime):
            dt = raw_ts
        elif isinstance(raw_ts, (int, float)):
            ts_sec = raw_ts / 1000.0 if raw_ts > 1e11 else float(raw_ts)
            dt = datetime.fromtimestamp(ts_sec, tz=timezone.utc)
        elif isinstance(raw_ts, str):
            ts_str = raw_ts.strip().replace("Z", "+00:00")
            dt = datetime.fromisoformat(ts_str)
        else:
            raise ValueError(f"Unsupported timestamp format: {raw_ts}")

        if dt.tzinfo is None:
            return dt.replace(tzinfo=IST_ZONE)
        return dt.astimezone(IST_ZONE)

    def validate_and_normalize_records(
        self,
        raw_records: List[Dict[str, Any]],
        symbol: str = "NIFTY",
        timeframe: str = "1m",
        provider: str = "UNKNOWN",
        raw_source_bytes: Optional[bytes] = None
    ) -> Tuple[List[HistoricalSpotCandle], DatasetManifest]:
        """
        Processes and validates an imported candle batch.
        Returns a tuple of (validated_candles, dataset_manifest).
        """
        norm_symbol = "NIFTY" if symbol.strip().upper() in INDEX_SYMBOL_ALIASES else symbol.strip().upper()
        expected_interval_sec = 60 if timeframe == "1m" else 60

        valid_candles: List[HistoricalSpotCandle] = []
        invalid_count = 0
        exact_duplicate_count = 0
        conflicting_duplicate_count = 0
        quarantined_conflicts: List[Dict[str, Any]] = []
        seen_records: Dict[datetime, Tuple[float, float, float, float, Optional[float]]] = {}
        missing_intervals: List[Dict[str, Any]] = []
        unsupported_calendar_dates_count = 0
        isolated_post_gap_records: List[Dict[str, Any]] = []

        is_synthetic_detected = False
        parsed_entries: List[Tuple[datetime, Dict[str, Any], str]] = []

        # Step 1: Initial Parsing and Timestamp Normalization
        for rec in raw_records:
            source_ts = str(rec.get("timestamp") or rec.get("start_time") or rec.get("Date") or rec.get("datetime") or "")
            if not source_ts:
                invalid_count += 1
                continue

            try:
                dt_ist = self.normalize_timestamp_to_ist(source_ts)
            except Exception:
                invalid_count += 1
                continue

            # Check synthetic data flags
            data_status = str(rec.get("data_status") or rec.get("candle_status") or DataStatus.HISTORICAL).upper()
            if data_status == "SIMULATED" or provider.upper() == "SAMPLEUNIVERSEPROVIDER":
                is_synthetic_detected = True

            parsed_entries.append((dt_ist, rec, source_ts))

        # Stable Sort chronologically by IST start_time
        parsed_entries.sort(key=lambda x: x[0])

        # Step 2: Validate OHLC Physics, Calendar Bounds, and Duplicate Conflicts
        for dt_ist, rec, source_ts in parsed_entries:
            flags: List[str] = []

            # Extract numeric prices
            try:
                op = float(rec.get("open", rec.get("Open", 0.0)))
                hp = float(rec.get("high", rec.get("High", 0.0)))
                lp = float(rec.get("low", rec.get("Low", 0.0)))
                cp = float(rec.get("close", rec.get("Close", 0.0)))
            except (ValueError, TypeError):
                invalid_count += 1
                continue

            raw_vol = rec.get("volume", rec.get("Volume", None))
            vol_val: Optional[float] = None
            if raw_vol is not None and str(raw_vol).strip() != "" and str(raw_vol).strip() != "None":
                try:
                    vol_val = float(raw_vol)
                except ValueError:
                    vol_val = None

            current_tuple = (round(op, 2), round(hp, 2), round(lp, 2), round(cp, 2), vol_val)

            # Check Duplicates and Conflicts
            if dt_ist in seen_records:
                prev_tuple = seen_records[dt_ist]
                is_exact_match = (
                    abs(prev_tuple[0] - current_tuple[0]) < 0.001 and
                    abs(prev_tuple[1] - current_tuple[1]) < 0.001 and
                    abs(prev_tuple[2] - current_tuple[2]) < 0.001 and
                    abs(prev_tuple[3] - current_tuple[3]) < 0.001 and
                    (prev_tuple[4] == current_tuple[4] or (prev_tuple[4] is None and current_tuple[4] is None))
                )
                if is_exact_match:
                    exact_duplicate_count += 1
                    continue
                else:
                    conflicting_duplicate_count += 1
                    quarantined_conflicts.append({
                        "timestamp": dt_ist.isoformat(),
                        "existing_candle": {
                            "open": prev_tuple[0], "high": prev_tuple[1],
                            "low": prev_tuple[2], "close": prev_tuple[3],
                            "volume": prev_tuple[4]
                        },
                        "conflicting_candle": {
                            "open": current_tuple[0], "high": current_tuple[1],
                            "low": current_tuple[2], "close": current_tuple[3],
                            "volume": current_tuple[4]
                        },
                        "classification": DuplicateClassification.CONFLICTING_DUPLICATE
                    })
                    # Do not silently accept or overwrite; record failure
                    invalid_count += 1
                    continue

            seen_records[dt_ist] = current_tuple

            # Check Calendar Bounds (supported 2025-2026)
            c_date = dt_ist.date()
            if c_date < CALENDAR_COVERAGE_START or c_date > CALENDAR_COVERAGE_END:
                unsupported_calendar_dates_count += 1
                flags.append(DataQualityFlag.UNSUPPORTED_CALENDAR_DATE)

            # Check OHLC Relationships
            if (
                hp < lp
                or op > hp
                or op < lp
                or cp > hp
                or cp < lp
                or op <= 0.0
                or hp <= 0.0
                or lp <= 0.0
                or cp <= 0.0
            ):
                flags.append(DataQualityFlag.INVALID_OHLC)
                invalid_count += 1

            # Check if candle is closed
            is_closed = rec.get("is_closed", True)
            if isinstance(is_closed, str):
                is_closed = is_closed.lower() in ("true", "1", "t")
            if not is_closed:
                flags.append(DataQualityFlag.DEVELOPING)

            # Check Session Boundaries
            is_market_open = self.session_clock.is_market_open(dt_ist)
            if not is_market_open:
                flags.append(DataQualityFlag.OUT_OF_SESSION)

            # Preserve existing flags from record if present
            existing_flags = rec.get("validation_flags", [])
            if isinstance(existing_flags, str):
                existing_flags = [f.strip() for f in existing_flags.split(",") if f.strip()]
            if isinstance(existing_flags, list):
                for f in existing_flags:
                    if f and f not in flags:
                        flags.append(f)

            # Check for Isolated Post-Gap Unverified Candle (Phase 3E.2)
            if valid_candles and valid_candles[-1].start_time.date() == dt_ist.date():
                gap_sec = (dt_ist - valid_candles[-1].start_time).total_seconds()
                if gap_sec >= 300 and (op == hp == lp == cp):
                    if DataQualityFlag.ISOLATED_POST_GAP_UNVERIFIED not in flags:
                        flags.append(DataQualityFlag.ISOLATED_POST_GAP_UNVERIFIED)

            if DataQualityFlag.ISOLATED_POST_GAP_UNVERIFIED in flags:
                isolated_post_gap_records.append({
                    "timestamp": dt_ist.isoformat(),
                    "price": op,
                    "validation_flags": list(flags)
                })

            # Volume Semantics: For spot index, volume must be NOT_APPLICABLE
            vol_semantics = VolumeSemantics.NOT_APPLICABLE if norm_symbol == "NIFTY" else (
                VolumeSemantics.CONTRACT_VOLUME if vol_val is not None else VolumeSemantics.UNAVAILABLE
            )

            end_dt = dt_ist + timedelta(seconds=expected_interval_sec)

            candle = HistoricalSpotCandle(
                symbol=norm_symbol,
                timeframe=timeframe,
                start_time=dt_ist,
                end_time=end_dt,
                open=round(op, 2),
                high=round(hp, 2),
                low=round(lp, 2),
                close=round(cp, 2),
                volume=vol_val,
                volume_semantics=vol_semantics,
                data_status=DataStatus.HISTORICAL,
                is_closed=bool(is_closed),
                source_timestamp=source_ts,
                validation_flags=flags if flags else [DataQualityFlag.VALID]
            )

            if DataQualityFlag.INVALID_OHLC not in flags and DataQualityFlag.DEVELOPING not in flags:
                valid_candles.append(candle)

        # Step 3: Missing Interval Detection within Official Trading Sessions
        if len(valid_candles) > 1:
            for i in range(1, len(valid_candles)):
                prev_c = valid_candles[i - 1]
                curr_c = valid_candles[i]

                if prev_c.start_time.date() == curr_c.start_time.date():
                    expected_next = prev_c.start_time + timedelta(seconds=expected_interval_sec)
                    if curr_c.start_time > expected_next:
                        gap_sec = (curr_c.start_time - prev_c.start_time).total_seconds()
                        missing_bars = int(gap_sec // expected_interval_sec) - 1
                        missing_intervals.append({
                            "date": prev_c.start_time.date().isoformat(),
                            "from_time": prev_c.end_time.time().isoformat(),
                            "to_time": curr_c.start_time.time().isoformat(),
                            "missing_candles": missing_bars,
                            "gap_seconds": gap_sec
                        })

        # Step 4: Determine Validation Status
        if is_synthetic_detected:
            validation_status = ValidationStatus.REJECTED_SYNTHETIC
        elif conflicting_duplicate_count > 0 or invalid_count > 0 or not valid_candles:
            validation_status = ValidationStatus.VALIDATION_FAILED
        else:
            validation_status = ValidationStatus.VERIFIED

        # Step 5: Compute Dual Checksums
        # 1. Source Raw SHA-256
        if raw_source_bytes is not None:
            source_raw_hash = hashlib.sha256(raw_source_bytes).hexdigest()
        else:
            # Fallback: Hash normalized JSON representation of raw input records
            raw_canonical = json.dumps(raw_records, sort_keys=True, separators=(',', ':')).encode('utf-8')
            source_raw_hash = hashlib.sha256(raw_canonical).hexdigest()

        # 2. Canonical Normalized Content SHA-256 (includes all material fields)
        canonical_lines = []
        for c in valid_candles:
            vol_str = f"{c.volume:.2f}" if c.volume is not None else "NONE"
            line = (
                f"{c.symbol}|{c.timeframe}|"
                f"{c.start_time.astimezone(IST_ZONE).isoformat()}|"
                f"{c.end_time.astimezone(IST_ZONE).isoformat()}|"
                f"{c.open:.2f}|{c.high:.2f}|{c.low:.2f}|{c.close:.2f}|"
                f"{vol_str}|{c.volume_semantics}|{c.data_status}|{c.is_closed}|"
                f"{c.source_timestamp}|{provider}"
            )
            canonical_lines.append(line)

        canonical_content = "\n".join(canonical_lines) + ("\n" if canonical_lines else "")
        canonical_content_hash = hashlib.sha256(canonical_content.encode('utf-8')).hexdigest()

        start_ts_str = valid_candles[0].start_time.isoformat() if valid_candles else None
        end_ts_str = valid_candles[-1].end_time.isoformat() if valid_candles else None

        # Provider cutoff metadata (Phase 3E.1)
        cutoff_observed = False
        last_candle_start = None
        last_candle_end = None
        if valid_candles:
            last_c = valid_candles[-1]
            last_candle_start = last_c.start_time.astimezone(IST_ZONE).isoformat()
            last_candle_end = last_c.end_time.astimezone(IST_ZONE).isoformat()
            if missing_intervals:
                earliest_missing = min(missing_intervals, key=lambda m: m["from_time"])
                if earliest_missing["from_time"] < "15:30:00":
                    cutoff_observed = True
                    before_gap = [c for c in valid_candles if c.end_time.strftime("%H:%M:%S") <= earliest_missing["from_time"]]
                    if before_gap:
                        last_candle_start = before_gap[-1].start_time.astimezone(IST_ZONE).isoformat()
                        last_candle_end = before_gap[-1].end_time.astimezone(IST_ZONE).isoformat()
            elif last_c.end_time.time() < time(15, 30):
                cutoff_observed = True

        manifest = DatasetManifest(
            dataset_id=f"manifest_{norm_symbol.lower()}_{canonical_content_hash[:8]}",
            symbol=norm_symbol,
            timeframe=timeframe,
            provider=provider,
            source_file="",
            retrieval_date=datetime.now(IST_ZONE).isoformat(),
            dataset_version="1.1.0",
            record_count=len(raw_records),
            valid_record_count=len(valid_candles),
            invalid_record_count=invalid_count,
            duplicate_count=exact_duplicate_count + conflicting_duplicate_count,
            exact_duplicate_count=exact_duplicate_count,
            conflicting_duplicate_count=conflicting_duplicate_count,
            quarantined_conflicts=quarantined_conflicts,
            missing_intervals_count=len(missing_intervals),
            missing_intervals=missing_intervals,
            start_timestamp_ist=start_ts_str,
            end_timestamp_ist=end_ts_str,
            timezone_convention="Asia/Kolkata (UTC+05:30)",
            volume_semantics=VolumeSemantics.NOT_APPLICABLE if norm_symbol == "NIFTY" else VolumeSemantics.CONTRACT_VOLUME,
            validation_status=validation_status,
            source_raw_sha256=source_raw_hash,
            canonical_content_sha256=canonical_content_hash,
            sha256_checksum=canonical_content_hash,
            partition_paths=[],
            storage_format="csv",
            calendar_version=CALENDAR_VERSION,
            calendar_coverage_start=CALENDAR_COVERAGE_START.isoformat(),
            calendar_coverage_end=CALENDAR_COVERAGE_END.isoformat(),
            calendar_assumptions=CALENDAR_ASSUMPTIONS,
            unsupported_calendar_dates_count=unsupported_calendar_dates_count,
            manifest_created_at=datetime.now(IST_ZONE).isoformat(),
            provider_cutoff_observed=cutoff_observed,
            last_validated_candle_start=last_candle_start,
            last_validated_candle_end=last_candle_end,
            exchange_session_close="15:30:00+05:30",
            provider_cutoff_cause="BROKER_HISTORICAL_FEED_INTRADAY_TRUNCATION",
            provider_cutoff_cause_verified=False,
            isolated_post_gap_count=len(isolated_post_gap_records),
            isolated_post_gap_records=isolated_post_gap_records,
            settlement_semantics="UNVERIFIED"
        )

        return valid_candles, manifest


# ============================================================================
# 5. Partitioned Historical Spot Store
# ============================================================================

class HistoricalSpotStore:
    """
    Persistent partitioned historical spot database.
    Storage Layout:
        {base_dir}/spot/{symbol}/year={YYYY}/month={MM}/candles_{symbol}_{YYYY}_{MM}.csv (or .parquet)
        {base_dir}/spot/{symbol}/manifests/{manifest_id}.json
    """

    def __init__(
        self,
        base_dir: Union[str, Path] = "data/historical",
        storage_format: str = "csv",
        session_clock: Optional[IndianMarketSessionClock] = None,
        allow_csv_fallback: bool = True
    ):
        self.base_dir = Path(base_dir)
        requested_format = storage_format.lower()

        if requested_format == "parquet":
            if not PYARROW_AVAILABLE:
                if allow_csv_fallback:
                    self.storage_format = "csv"
                else:
                    raise ImportError(
                        "Storage format 'parquet' requested, but 'pyarrow' is not installed. "
                        "Install pyarrow or use storage_format='csv'."
                    )
            else:
                self.storage_format = "parquet"
        else:
            self.storage_format = "csv"

        self.validator = HistoricalSpotDataValidator(session_clock=session_clock)

    def _get_partition_dir(self, symbol: str, year: int, month: int) -> Path:
        return self.base_dir / "spot" / symbol.upper() / f"year={year:04d}" / f"month={month:02d}"

    def _get_partition_file(self, symbol: str, year: int, month: int) -> Path:
        ext = "parquet" if self.storage_format == "parquet" else "csv"
        return self._get_partition_dir(symbol, year, month) / f"candles_{symbol.upper()}_{year:04d}_{month:02d}.{ext}"

    def _get_manifest_dir(self, symbol: str) -> Path:
        return self.base_dir / "spot" / symbol.upper() / "manifests"

    def ingest_records(
        self,
        records: List[Dict[str, Any]],
        symbol: str = "NIFTY",
        timeframe: str = "1m",
        provider: str = "HISTORICAL_FEED",
        source_name: str = "import",
        raw_source_bytes: Optional[bytes] = None
    ) -> DatasetManifest:
        """
        Validates records, quarantines conflicting duplicates, partitions into year/month directories,
        and saves an immutable manifest. Refuses to mark as VERIFIED if critical checks fail.
        """
        valid_candles, manifest = self.validator.validate_and_normalize_records(
            raw_records=records,
            symbol=symbol,
            timeframe=timeframe,
            provider=provider,
            raw_source_bytes=raw_source_bytes
        )
        manifest.source_file = source_name
        manifest.storage_format = self.storage_format

        # Refuse to commit corrupt, synthetic, or conflicting data
        if manifest.validation_status != ValidationStatus.VERIFIED:
            return manifest

        # Group valid candles by (year, month) for partitioned storage
        partitions: Dict[Tuple[int, int], List[HistoricalSpotCandle]] = {}
        for c in valid_candles:
            key = (c.start_time.year, c.start_time.month)
            partitions.setdefault(key, []).append(c)

        created_partition_paths: List[str] = []

        for (year, month), partition_candles in partitions.items():
            target_file = self._get_partition_file(symbol, year, month)
            target_file.parent.mkdir(parents=True, exist_ok=True)

            # Incremental ingestion: merge with existing partition if present
            existing_records: List[Dict[str, Any]] = []
            if target_file.exists():
                if self.storage_format == "parquet":
                    existing_records = ParquetCodec.read(target_file)
                else:
                    existing_records = self._read_csv_file(target_file)

            # Deduplicate by start_time, checking for conflicts during merge
            candle_map: Dict[str, Dict[str, Any]] = {}
            for r in existing_records:
                candle_map[r["start_time"]] = r

            for c in partition_candles:
                c_dict = c.to_dict()
                s_key = c_dict["start_time"]
                if s_key in candle_map:
                    ex = candle_map[s_key]
                    is_conflict = (
                        abs(float(ex["open"]) - float(c_dict["open"])) >= 0.001 or
                        abs(float(ex["high"]) - float(c_dict["high"])) >= 0.001 or
                        abs(float(ex["low"]) - float(c_dict["low"])) >= 0.001 or
                        abs(float(ex["close"]) - float(c_dict["close"])) >= 0.001
                    )
                    if is_conflict:
                        raise ValueError(
                            f"Conflicting duplicate in partition {target_file} at {s_key}. "
                            f"Existing: O={ex['open']} H={ex['high']} L={ex['low']} C={ex['close']}; "
                            f"Incoming: O={c_dict['open']} H={c_dict['high']} L={c_dict['low']} C={c_dict['close']}. "
                            f"Quarantine required; silent selection prohibited."
                        )
                    # Exact duplicate -> keep existing
                else:
                    candle_map[s_key] = c_dict

            merged_records = list(candle_map.values())
            merged_records.sort(key=lambda r: r["start_time"])

            # Write merged partition
            if self.storage_format == "parquet":
                ParquetCodec.write(merged_records, target_file)
            else:
                self._write_csv_file(merged_records, target_file)

            created_partition_paths.append(str(target_file))

        manifest.partition_paths = created_partition_paths

        # Save manifest
        manifest_dir = self._get_manifest_dir(symbol)
        manifest_dir.mkdir(parents=True, exist_ok=True)
        manifest_file = manifest_dir / f"{manifest.dataset_id}.json"
        with open(manifest_file, "w", encoding="utf-8") as f:
            json.dump(manifest.to_dict(), f, indent=2)

        return manifest

    def ingest_csv(
        self,
        file_path_or_str: Union[str, Path],
        symbol: str = "NIFTY",
        timeframe: str = "1m",
        provider: str = "HISTORICAL_CSV"
    ) -> DatasetManifest:
        """Ingests a CSV file or CSV string into partitioned store."""
        records: List[Dict[str, Any]] = []
        raw_bytes: Optional[bytes] = None
        path = Path(file_path_or_str)

        if path.exists() and path.is_file():
            source_name = path.name
            raw_bytes = path.read_bytes()
            with open(path, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                records = list(reader)
        else:
            source_name = "in_memory_csv_string"
            csv_text = str(file_path_or_str)
            raw_bytes = csv_text.encode('utf-8')
            reader = csv.DictReader(io.StringIO(csv_text))
            records = list(reader)

        return self.ingest_records(
            records=records,
            symbol=symbol,
            timeframe=timeframe,
            provider=provider,
            source_name=source_name,
            raw_source_bytes=raw_bytes
        )

    def ingest_parquet(
        self,
        file_path: Union[str, Path],
        symbol: str = "NIFTY",
        timeframe: str = "1m",
        provider: str = "HISTORICAL_PARQUET"
    ) -> DatasetManifest:
        """Ingests a genuine Apache Parquet file into partitioned store."""
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"Parquet file not found: {path}")

        # Verify Parquet framing
        is_valid, reason = ParquetCodec.verify_parquet_file(path)
        if not is_valid:
            raise ValueError(f"File is not a valid Apache Parquet file: {reason}")

        raw_bytes = path.read_bytes()
        records = ParquetCodec.read(path)

        return self.ingest_records(
            records=records,
            symbol=symbol,
            timeframe=timeframe,
            provider=provider,
            source_name=path.name,
            raw_source_bytes=raw_bytes
        )

    def _get_target_partition_files(
        self,
        symbol: str,
        start_dt: Optional[datetime] = None,
        end_dt: Optional[datetime] = None
    ) -> List[Path]:
        """
        Partition Pruning: Returns only partition files that overlap with [start_dt, end_dt].
        Avoids scanning irrelevant years and months.
        """
        sym = "NIFTY" if symbol.strip().upper() in INDEX_SYMBOL_ALIASES else symbol.strip().upper()
        sym_dir = self.base_dir / "spot" / sym
        if not sym_dir.exists():
            return []

        ext = "parquet" if self.storage_format == "parquet" else "csv"
        all_files = sorted(sym_dir.glob(f"year=*/month=*/candles_*.{ext}"))

        if not start_dt and not end_dt:
            return all_files

        filtered: List[Path] = []
        for p in all_files:
            # Extract year and month from folder path: year=YYYY/month=MM
            try:
                parts = p.parts
                y_str = [x for x in parts if x.startswith("year=")][0].replace("year=", "")
                m_str = [x for x in parts if x.startswith("month=")][0].replace("month=", "")
                pyear, pmonth = int(y_str), int(m_str)

                # Check lower bound
                if start_dt:
                    s_ym = (start_dt.year, start_dt.month)
                    if (pyear, pmonth) < s_ym:
                        continue

                # Check upper bound
                if end_dt:
                    e_ym = (end_dt.year, end_dt.month)
                    if (pyear, pmonth) > e_ym:
                        continue

                filtered.append(p)
            except Exception:
                # Fallback to including if path cannot be parsed
                filtered.append(p)

        return filtered

    def stream_candles(
        self,
        symbol: str = "NIFTY",
        start_dt: Optional[datetime] = None,
        end_dt: Optional[datetime] = None
    ) -> Generator[HistoricalSpotCandle, None, None]:
        """
        Streams candles lazily partition-by-partition and row-by-row.
        Does NOT materialize the full dataset in memory.
        Enables lookahead-free sequential replay in backtesting.
        """
        partition_files = self._get_target_partition_files(symbol, start_dt, end_dt)

        for p_file in partition_files:
            if self.storage_format == "parquet":
                # Stream batches from Parquet
                for r in ParquetCodec.iter_records(p_file):
                    s_dt = datetime.fromisoformat(r["start_time"])
                    if s_dt.tzinfo is None:
                        s_dt = s_dt.replace(tzinfo=IST_ZONE)
                    else:
                        s_dt = s_dt.astimezone(IST_ZONE)

                    if start_dt and s_dt < start_dt:
                        continue
                    if end_dt and s_dt > end_dt:
                        continue

                    e_dt = datetime.fromisoformat(r["end_time"])
                    if e_dt.tzinfo is None:
                        e_dt = e_dt.replace(tzinfo=IST_ZONE)
                    else:
                        e_dt = e_dt.astimezone(IST_ZONE)

                    yield HistoricalSpotCandle(
                        symbol=r["symbol"],
                        timeframe=r["timeframe"],
                        start_time=s_dt,
                        end_time=e_dt,
                        open=float(r["open"]),
                        high=float(r["high"]),
                        low=float(r["low"]),
                        close=float(r["close"]),
                        volume=float(r["volume"]) if r.get("volume") is not None and r["volume"] != "None" else None,
                        volume_semantics=r.get("volume_semantics", VolumeSemantics.NOT_APPLICABLE),
                        data_status=r.get("data_status", DataStatus.HISTORICAL),
                        is_closed=bool(r.get("is_closed", True)),
                        source_timestamp=r.get("source_timestamp", ""),
                        validation_flags=r.get("validation_flags", [DataQualityFlag.VALID])
                    )
            else:
                # Stream lines from CSV
                with open(p_file, "r", encoding="utf-8") as f:
                    reader = csv.DictReader(f)
                    for r in reader:
                        s_dt = datetime.fromisoformat(r["start_time"])
                        if s_dt.tzinfo is None:
                            s_dt = s_dt.replace(tzinfo=IST_ZONE)
                        else:
                            s_dt = s_dt.astimezone(IST_ZONE)

                        if start_dt and s_dt < start_dt:
                            continue
                        if end_dt and s_dt > end_dt:
                            continue

                        e_dt = datetime.fromisoformat(r["end_time"])
                        if e_dt.tzinfo is None:
                            e_dt = e_dt.replace(tzinfo=IST_ZONE)
                        else:
                            e_dt = e_dt.astimezone(IST_ZONE)

                        vol_raw = r.get("volume")
                        vol_val = float(vol_raw) if vol_raw and vol_raw != "None" else None
                        flags_str = r.get("validation_flags", "")
                        flags = [x.strip() for x in flags_str.split(",") if x.strip()]

                        yield HistoricalSpotCandle(
                            symbol=r["symbol"],
                            timeframe=r["timeframe"],
                            start_time=s_dt,
                            end_time=e_dt,
                            open=float(r["open"]),
                            high=float(r["high"]),
                            low=float(r["low"]),
                            close=float(r["close"]),
                            volume=vol_val,
                            volume_semantics=r.get("volume_semantics", VolumeSemantics.NOT_APPLICABLE),
                            data_status=r.get("data_status", DataStatus.HISTORICAL),
                            is_closed=r.get("is_closed", "True").lower() in ("true", "1"),
                            source_timestamp=r.get("source_timestamp", ""),
                            validation_flags=flags if flags else [DataQualityFlag.VALID]
                        )

    def query_candles(
        self,
        symbol: str = "NIFTY",
        start_dt: Optional[datetime] = None,
        end_dt: Optional[datetime] = None,
        limit: Optional[int] = None
    ) -> List[HistoricalSpotCandle]:
        """
        Queries candles across partition files without bounded ring-buffer truncation.
        Consumes from stream_candles() up to optional limit.
        """
        candles: List[HistoricalSpotCandle] = []
        for c in self.stream_candles(symbol, start_dt, end_dt):
            candles.append(c)
            if limit and len(candles) >= limit:
                break
        return candles

    def export_dataset(
        self,
        symbol: str = "NIFTY",
        output_path: Union[str, Path] = "export.csv",
        format: str = "csv",
        start_dt: Optional[datetime] = None,
        end_dt: Optional[datetime] = None
    ) -> Path:
        """Exports stored historical records to a consolidated CSV or Parquet file."""
        candles = self.query_candles(symbol=symbol, start_dt=start_dt, end_dt=end_dt)
        records = [c.to_dict() for c in candles]
        out_target = Path(output_path)
        out_target.parent.mkdir(parents=True, exist_ok=True)

        if format.lower() == "parquet":
            ParquetCodec.write(records, out_target)
        else:
            self._write_csv_file(records, out_target)

        return out_target

    def get_manifest(self, symbol: str, manifest_id: str) -> Optional[DatasetManifest]:
        m_file = self._get_manifest_dir(symbol) / f"{manifest_id}.json"
        if not m_file.exists():
            return None
        with open(m_file, "r", encoding="utf-8") as f:
            return DatasetManifest.from_dict(json.load(f))

    # Internal CSV Helper Methods
    @staticmethod
    def _write_csv_file(records: List[Dict[str, Any]], target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        if not records:
            return
        fieldnames = [
            "symbol", "timeframe", "timestamp", "start_time", "end_time",
            "open", "high", "low", "close", "volume", "volume_semantics",
            "data_status", "is_closed", "source_timestamp", "validation_flags"
        ]
        with open(target, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            for r in records:
                row = dict(r)
                if isinstance(row.get("validation_flags"), list):
                    row["validation_flags"] = ",".join(row["validation_flags"])
                writer.writerow(row)

    @staticmethod
    def _read_csv_file(target: Path) -> List[Dict[str, Any]]:
        records: List[Dict[str, Any]] = []
        with open(target, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                rec = dict(row)
                rec["open"] = float(rec["open"])
                rec["high"] = float(rec["high"])
                rec["low"] = float(rec["low"])
                rec["close"] = float(rec["close"])
                rec["volume"] = float(rec["volume"]) if rec.get("volume") and rec["volume"] != "None" else None
                rec["is_closed"] = rec.get("is_closed", "True").lower() in ("true", "1")
                flags_str = rec.get("validation_flags", "")
                rec["validation_flags"] = [f.strip() for f in flags_str.split(",") if f.strip()]
                records.append(rec)
        return records


# ============================================================================
# 6. Future-Facing Phase 3 Adapter
# ============================================================================

class Phase3SignalAdapter:
    """
    Adapter ensuring that:
    1. Underlying-only spot signals CANNOT be represented as option-execution eligible.
    2. Missing 15-second intervals remain strictly UNAVAILABLE (never synthesized).
    3. 15:15 IST square-off is a configurable simulation assumption, not universal exchange rule.
    4. Regulatory transaction cost dates and spread-slippage separation are strictly enforced.
    """

    DEFAULT_SQUARE_OFF_TIME = time(15, 15, 0)

    def __init__(
        self,
        intraday_square_off_time: Optional[time] = DEFAULT_SQUARE_OFF_TIME,
        enforce_spread_separation: bool = True
    ):
        self.intraday_square_off_time = intraday_square_off_time
        self.enforce_spread_separation = enforce_spread_separation

    @staticmethod
    def adapt_spot_signal(setup_result: Dict[str, Any]) -> Dict[str, Any]:
        """
        Adapts a Phase 2C setup score for underlying spot backtesting.
        Guarantees that an underlying-only signal cannot be represented as option execution eligible.
        """
        adapted = dict(setup_result)
        adapted["option_execution_eligible"] = False
        adapted["option_execution_status"] = "UNAVAILABLE"
        adapted["underlying_signal_only"] = True
        adapted["execution_adapter_notes"] = (
            "Phase 3 Underlying Spot Signal: Option contract execution is UNAVAILABLE "
            "due to lack of verified historical option quotes and contract masters."
        )
        return adapted

    @staticmethod
    def get_transaction_cost_schedule(trade_date: date) -> Dict[str, float]:
        """
        Returns date-effective regulatory and exchange transaction cost parameters.
        - NSE Circular FA73061 effective from 1 March 2026 (Option transaction charge: ₹3,553/crore).
        - STT revised schedule effective from 1 April 2026 (Option sale STT: 0.15%, exercise: 0.15%).
        """
        # Circular FA73061: Option transaction charge boundary
        if trade_date >= date(2026, 3, 1):
            nse_option_txn_rate = 0.0003553  # ₹3,553 per crore = 0.03553%
        else:
            nse_option_txn_rate = 0.0005000  # Historical rate

        # Finance Act STT boundary
        if trade_date >= date(2026, 4, 1):
            option_sale_stt_rate = 0.0015     # 0.15% on premium turnover
            option_exercise_stt_rate = 0.0015 # 0.15% on intrinsic value
            futures_sale_stt_rate = 0.0005    # 0.05% on traded value
        else:
            option_sale_stt_rate = 0.0010     # 0.10% on premium turnover
            option_exercise_stt_rate = 0.00125# 0.125% on intrinsic value
            futures_sale_stt_rate = 0.0002    # 0.02% on traded value

        return {
            "effective_date": trade_date.isoformat(),
            "nse_option_txn_rate": nse_option_txn_rate,
            "option_sale_stt_rate": option_sale_stt_rate,
            "option_exercise_stt_rate": option_exercise_stt_rate,
            "futures_sale_stt_rate": futures_sale_stt_rate,
            "gst_rate": 0.18,
            "sebi_turnover_rate": 0.000001,   # ₹10 per crore
            "stamp_duty_buy_rate": 0.00003    # 0.003% on buyer
        }

    @staticmethod
    def compute_execution_fill(
        action: str,
        best_bid: float,
        best_ask: float,
        slippage_points: float = 0.0
    ) -> float:
        """
        Prevents spread double-counting:
        - Buy: Enters at Best Ask + additional latency/impact slippage.
        - Sell: Exits at Best Bid - additional latency/impact slippage.
        The bid/ask spread is inherently incorporated into crossing the market.
        """
        if action.upper() == "BUY":
            return round(best_ask + slippage_points, 2)
        elif action.upper() == "SELL":
            return round(best_bid - slippage_points, 2)
        else:
            raise ValueError(f"Unknown action: {action}")
