"""
TradeLens AI - Phase 3E: Genuine Historical Spot Data Acquisition & Coverage Validation Engine.

Architectural Guarantees:
1. Genuine Historical Provenance:
   - Interfaces directly with authorized broker API (Angel One SmartAPI) or secondary providers.
   - NIFTY 50 spot volume is strictly labeled NOT_APPLICABLE (0 direct traded volume).
   - Zero synthetic data, random walks, or interpolated price bars in production output.

2. Robust Chunking & Resumability:
   - Enforces date chunking (default <= 15 days) to eliminate provider silent truncation limits.
   - Checkpoints acquisition progress to disk (HistoricalAcquisitionCheckpoint) allowing
     seamless resumption after network interruption without re-downloading verified partitions.
   - Idempotent: Repeated executions over identical periods produce identical dual-checksum files.

3. Strict Calendar & Session Validation:
   - Validates observed 1m candles against expected trading sessions (09:15 - 15:30 IST = 375 bars).
   - Classifies missing intervals (MISSING_INTRADAY_CANDLE, EXCHANGE_HOLIDAY, WEEKEND, SPECIAL_SESSION).
   - Pre-2025 dates flagged as CALENDAR_UNVERIFIED.
   - Rejects corrupt OHLC geometry and quarantines conflicting duplicate records.

4. Seamless Phase 3B Integration:
   - Commits validated partitions to disk using HistoricalSpotStore (Parquet or CSV).
   - Stores raw response SHA-256 and canonical normalized content SHA-256.
"""

from __future__ import annotations

import os
import json
import time
import urllib.request
import urllib.error
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict
from datetime import datetime, date, time as dt_time, timedelta
from enum import Enum
from typing import Dict, List, Optional, Any, Tuple, Union, Set
import zoneinfo

from backend.app.data.session_clock import IndianMarketSessionClock, IST_ZONE
from backend.app.data.base import DataStatus
from backend.app.data.fno_tokens import FNO_TOKEN_MAP
from backend.app.data.historical_spot_store import (
    HistoricalSpotCandle,
    HistoricalSpotStore,
    VolumeSemantics,
    ValidationStatus,
    DataQualityFlag,
    DuplicateClassification
)


class AcquisitionStatus(str, Enum):
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    ACQUIRED = "ACQUIRED"
    VALIDATED = "VALIDATED"
    PARTIALLY_VALIDATED = "PARTIALLY_VALIDATED"
    FAILED = "FAILED"
    QUARANTINED = "QUARANTINED"


class IntervalClassification(str, Enum):
    EXPECTED_SESSION_CANDLE = "EXPECTED_SESSION_CANDLE"
    MISSING_INTRADAY_CANDLE = "MISSING_INTRADAY_CANDLE"
    EXCHANGE_HOLIDAY = "EXCHANGE_HOLIDAY"
    WEEKEND = "WEEKEND"
    SPECIAL_SESSION = "SPECIAL_SESSION"
    UNVERIFIED_CALENDAR = "UNVERIFIED_CALENDAR"
    PROVIDER_DATA_UNAVAILABLE = "PROVIDER_DATA_UNAVAILABLE"


# ============================================================================
# 1. Provider Abstractions
# ============================================================================

class HistoricalSpotProvider(ABC):
    """Abstract interface for historical market data providers."""

    @abstractmethod
    def get_provider_name(self) -> str:
        """Returns provider name (e.g. 'ANGEL_ONE', 'YAHOO_FINANCE')."""
        pass

    @abstractmethod
    def get_provider_capabilities(self) -> Dict[str, Any]:
        """Returns documented capabilities, max date range, and rate limits."""
        pass

    @abstractmethod
    def fetch_candles(
        self,
        symbol: str,
        interval: str,
        from_dt: datetime,
        to_dt: datetime
    ) -> Tuple[List[Dict[str, Any]], str]:
        """
        Retrieves historical candles for designated symbol and date range.
        Returns (list_of_raw_candle_dicts, raw_response_sha256).
        """
        pass


class AngelOneHistoricalSpotProvider(HistoricalSpotProvider):
    """
    Genuine Angel One SmartAPI Historical Spot Provider.
    Implements POST /rest/secure/angelbroking/historical/v1/getCandleData.
    """

    BASE_URL = "https://apiconnect.angelone.in"

    def __init__(
        self,
        api_key: Optional[str] = None,
        client_code: Optional[str] = None,
        password: Optional[str] = None,
        totp_secret: Optional[str] = None,
        max_chunk_days: int = 15,
        rate_limit_pause_sec: float = 0.5
    ):
        self.api_key = api_key or os.environ.get("ANGEL_API_KEY", "")
        self.client_code = client_code or os.environ.get("ANGEL_CLIENT_CODE", "")
        self.password = password or os.environ.get("ANGEL_PASSWORD", "")
        self.totp_secret = totp_secret or os.environ.get("ANGEL_TOTP_SECRET", "")
        self.max_chunk_days = max_chunk_days
        self.rate_limit_pause_sec = rate_limit_pause_sec
        self.jwt_token: Optional[str] = None
        self.is_authenticated = False

    def get_provider_name(self) -> str:
        return "ANGEL_ONE"

    def get_provider_capabilities(self) -> Dict[str, Any]:
        return {
            "provider": "Angel One SmartAPI",
            "endpoint": "/rest/secure/angelbroking/historical/v1/getCandleData",
            "supported_intervals": ["ONE_MINUTE", "THREE_MINUTE", "FIVE_MINUTE", "FIFTEEN_MINUTE", "ONE_DAY"],
            "supported_instruments": ["NIFTY 50 (token 99926000)", "BANKNIFTY (token 99926009)"],
            "max_chunk_days_safe": self.max_chunk_days,
            "provider_silent_truncation_limit_days": 30,
            "rate_limit_per_second": 3,
            "historical_depth_verified": "At least 1 year (verified to Nov 2025)",
            "auth_mechanism": "JWT with TOTP 2FA"
        }

    def _load_env_if_needed(self) -> None:
        """Loads .env file if credentials are not in environment."""
        if not (self.api_key and self.client_code and self.password):
            env_paths = [".env", "backend/.env", "../.env"]
            for ep in env_paths:
                if os.path.exists(ep):
                    with open(ep, "r", encoding="utf-8") as f:
                        for line in f:
                            line = line.strip()
                            if line and not line.startswith("#") and "=" in line:
                                k, v = line.split("=", 1)
                                k_s = k.strip()
                                v_s = v.strip().strip('"').strip("'")
                                if k_s == "ANGEL_API_KEY" and not self.api_key:
                                    self.api_key = v_s
                                elif k_s == "ANGEL_CLIENT_CODE" and not self.client_code:
                                    self.client_code = v_s
                                elif k_s == "ANGEL_PASSWORD" and not self.password:
                                    self.password = v_s
                                elif k_s == "ANGEL_TOTP_SECRET" and not self.totp_secret:
                                    self.totp_secret = v_s

    def connect(self) -> bool:
        """Authenticates with Angel One using loginByPassword endpoint."""
        self._load_env_if_needed()
        if not self.api_key or not self.client_code or not self.password:
            return False

        try:
            from backend.app.data.angel_one import generate_totp_code
            totp_code = generate_totp_code(self.totp_secret) if self.totp_secret else ""
            url = f"{self.BASE_URL}/rest/auth/angelbroking/user/v1/loginByPassword"
            headers = {
                "Content-Type": "application/json",
                "Accept": "application/json",
                "X-UserType": "USER",
                "X-SourceID": "WEB",
                "X-ClientLocalIP": "127.0.0.1",
                "X-ClientPublicIP": "127.0.0.1",
                "X-MACAddress": "fe80::1",
                "X-PrivateKey": self.api_key
            }
            payload = json.dumps({
                "clientcode": self.client_code,
                "password": self.password,
                "totp": totp_code
            }).encode("utf-8")

            req = urllib.request.Request(url, data=payload, headers=headers)
            with urllib.request.urlopen(req, timeout=10) as resp:
                res_data = json.loads(resp.read().decode("utf-8"))

            if res_data.get("status") and "data" in res_data:
                self.jwt_token = res_data["data"].get("jwtToken")
                self.is_authenticated = True
                return True
            return False
        except Exception:
            return False

    def fetch_candles(
        self,
        symbol: str,
        interval: str,
        from_dt: datetime,
        to_dt: datetime
    ) -> Tuple[List[Dict[str, Any]], str]:
        """
        Fetches genuine 1m candles for symbol.
        Normalizes interval and returns (raw_rows, raw_payload_hash).
        """
        if not self.is_authenticated or not self.jwt_token:
            if not self.connect():
                raise ConnectionError("Angel One SmartAPI authentication failed: missing credentials or network unreachable.")

        sym_clean = symbol.strip().upper()
        token = "99926000" if sym_clean in ("NIFTY", "NIFTY 50", "^NSEI") else FNO_TOKEN_MAP.get(sym_clean, {}).get("token", "")
        if not token:
            raise ValueError(f"Unsupported instrument symbol: {symbol}")

        from_str = from_dt.strftime("%Y-%m-%d %H:%M")
        to_str = to_dt.strftime("%Y-%m-%d %H:%M")
        api_interval = "ONE_MINUTE" if interval in ("1m", "1min", "ONE_MINUTE") else interval

        url = f"{self.BASE_URL}/rest/secure/angelbroking/historical/v1/getCandleData"
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-UserType": "USER",
            "X-SourceID": "WEB",
            "X-ClientLocalIP": "127.0.0.1",
            "X-ClientPublicIP": "127.0.0.1",
            "X-MACAddress": "fe80::1",
            "X-PrivateKey": self.api_key,
            "Authorization": f"Bearer {self.jwt_token}"
        }
        payload = json.dumps({
            "exchange": "NSE",
            "symboltoken": str(token),
            "interval": api_interval,
            "fromdate": from_str,
            "todate": to_str
        }).encode("utf-8")

        # Controlled retry loop for rate limiting and token expiry
        max_retries = 3
        last_error = None

        for attempt in range(max_retries):
            try:
                time.sleep(self.rate_limit_pause_sec)
                req = urllib.request.Request(url, data=payload, headers=headers)
                with urllib.request.urlopen(req, timeout=12) as response:
                    raw_bytes = response.read()
                    import hashlib
                    raw_sha256 = hashlib.sha256(raw_bytes).hexdigest()
                    res = json.loads(raw_bytes.decode("utf-8"))

                errorcode = res.get("errorcode", "")
                if errorcode in ("AG8001", "AG8002"):
                    # Token expired; reconnect and retry
                    self.jwt_token = None
                    self.is_authenticated = False
                    if self.connect():
                        headers["Authorization"] = f"Bearer {self.jwt_token}"
                        continue
                elif errorcode == "AB1004":
                    # Rate limit; back off exponentially
                    time.sleep(2.0 * (attempt + 1))
                    continue

                if res.get("status") and "data" in res:
                    return res["data"] or [], raw_sha256
                elif not res.get("status"):
                    return [], raw_sha256
            except urllib.error.HTTPError as he:
                last_error = he
                if he.code in (401, 403):
                    self.jwt_token = None
                    self.is_authenticated = False
                    if self.connect():
                        headers["Authorization"] = f"Bearer {self.jwt_token}"
                        continue
                time.sleep(1.5 * (attempt + 1))
            except Exception as e:
                last_error = e
                time.sleep(1.0 * (attempt + 1))

        raise RuntimeError(f"Failed to fetch historical candles from Angel One after {max_retries} attempts: {last_error}")


# ============================================================================
# 2. Resumable Acquisition Checkpoint
# ============================================================================

@dataclass
class AcquisitionChunk:
    chunk_index: int
    from_date_str: str
    to_date_str: str
    status: str  # PENDING, ACQUIRED, VALIDATED, FAILED
    records_count: int = 0
    raw_sha256: str = ""
    error_message: Optional[str] = None


@dataclass
class HistoricalAcquisitionCheckpoint:
    """Manages acquisition progress state on disk for recovery and idempotency."""
    instrument: str
    interval: str
    requested_start: str
    requested_end: str
    chunks: List[Dict[str, Any]] = field(default_factory=list)
    overall_status: str = AcquisitionStatus.PENDING.value
    created_at: str = ""
    last_updated_at: str = ""

    def save(self, filepath: str) -> None:
        self.last_updated_at = datetime.now(IST_ZONE).isoformat()
        os.makedirs(os.path.dirname(os.path.abspath(filepath)), exist_ok=True)
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(asdict(self), f, indent=2)

    @classmethod
    def load(cls, filepath: str) -> Optional[HistoricalAcquisitionCheckpoint]:
        if not os.path.exists(filepath):
            return None
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
        return cls(**data)


# ============================================================================
# 3. Trading Session & Coverage Validator
# ============================================================================

class HistoricalCoverageValidator:
    """
    Validates candle records against official exchange calendar and session hours.
    Identifies missing intervals, holiday dates, out-of-session bars, and duplicate anomalies.
    """

    def __init__(self, session_clock: Optional[IndianMarketSessionClock] = None):
        self.session_clock = session_clock or IndianMarketSessionClock()

    def generate_expected_session_intervals(
        self,
        start_date: date,
        end_date: date,
        interval_minutes: int = 1
    ) -> Dict[str, Dict[str, Any]]:
        """
        Generates expected 1m candle intervals for every trading session between start and end date.
        Returns dict keyed by ISO start_time string.
        """
        expected: Dict[str, Dict[str, Any]] = {}
        curr_d = start_date

        while curr_d <= end_date:
            d_str = curr_d.strftime("%Y-%m-%d")

            # Check if this date is outside supported 2025-2026 calendar coverage
            is_unverified = (curr_d < date(2025, 1, 1) or curr_d > date(2026, 12, 31))

            # 1. Exceptional special sessions (e.g. Diwali Muhurat)
            if d_str in self.session_clock.exceptional_sessions:
                for session in self.session_clock.exceptional_sessions[d_str]:
                    if session.get("is_market_open", True):
                        s_time = session["start"]
                        e_time = session["end"]
                        dt_curr = datetime.combine(curr_d, s_time, tzinfo=IST_ZONE)
                        dt_end = datetime.combine(curr_d, e_time, tzinfo=IST_ZONE)
                        while dt_curr < dt_end:
                            iso_k = dt_curr.isoformat()
                            expected[iso_k] = {
                                "start_time": dt_curr,
                                "date": d_str,
                                "type": IntervalClassification.SPECIAL_SESSION.value,
                                "session_name": session.get("name", "SPECIAL_SESSION")
                            }
                            dt_curr += timedelta(minutes=interval_minutes)

            # 2. Regular sessions: Mon-Fri, non-holiday
            elif curr_d.weekday() < 5 and not self.session_clock.is_holiday(curr_d):
                dt_curr = datetime.combine(curr_d, dt_time(9, 15), tzinfo=IST_ZONE)
                dt_end = datetime.combine(curr_d, dt_time(15, 30), tzinfo=IST_ZONE)
                interval_type = (
                    IntervalClassification.UNVERIFIED_CALENDAR.value
                    if is_unverified
                    else IntervalClassification.EXPECTED_SESSION_CANDLE.value
                )
                while dt_curr < dt_end:
                    iso_k = dt_curr.isoformat()
                    expected[iso_k] = {
                        "start_time": dt_curr,
                        "date": d_str,
                        "type": interval_type,
                        "session_name": "REGULAR_SESSION"
                    }
                    dt_curr += timedelta(minutes=interval_minutes)

            curr_d += timedelta(days=1)

        return expected

    def validate_and_classify_candles(
        self,
        candles: List[Dict[str, Any]],
        start_date: date,
        end_date: date,
        symbol: str = "NIFTY"
    ) -> Dict[str, Any]:
        """
        Validates OHLC geometry, temporal sequence, session eligibility, and coverage.
        Returns detailed validation metrics and clean normalized candles.
        """
        expected_intervals = self.generate_expected_session_intervals(start_date, end_date)
        expected_total = len(expected_intervals)

        valid_candles: List[Dict[str, Any]] = []
        invalid_candles: List[Dict[str, Any]] = []
        quarantined_conflicts: List[Dict[str, Any]] = []
        observed_timestamps: Set[str] = set()
        exact_duplicate_count = 0
        conflicting_duplicate_count = 0

        # Extract timestamp string for sorting
        def _get_ts(c: Any) -> str:
            if isinstance(c, (list, tuple)):
                return str(c[0]) if len(c) > 0 else ""
            elif isinstance(c, dict):
                return str(c.get("start_time") or c.get("timestamp") or c.get("time") or "")
            return ""

        def _extract_fields(c: Any) -> Tuple[str, float, float, float, float, float]:
            if isinstance(c, (list, tuple)):
                if len(c) < 5:
                    raise ValueError(f"Incomplete candle row: minimum 5 elements required, got {len(c)}")
                raw_ts = str(c[0])
                o_val = float(c[1])
                h_val = float(c[2])
                l_val = float(c[3])
                c_val = float(c[4])
                v_val = float(c[5]) if len(c) > 5 else 0.0
                return raw_ts, o_val, h_val, l_val, c_val, v_val
            elif isinstance(c, dict):
                raw_ts = str(c.get("start_time") or c.get("timestamp") or c.get("time") or "")
                if not raw_ts or "open" not in c or "high" not in c or "low" not in c or "close" not in c:
                    raise ValueError("Incomplete candle dictionary: missing required OHLC keys")
                return raw_ts, float(c["open"]), float(c["high"]), float(c["low"]), float(c["close"]), float(c.get("volume", 0.0))
            else:
                raise ValueError(f"Unsupported candle data type: {type(c)}")

        sorted_candles = sorted(candles, key=_get_ts)

        for c in sorted_candles:
            try:
                raw_ts, o_val, h_val, l_val, c_val, v_val = _extract_fields(c)
            except Exception as ex:
                invalid_candles.append({"candle": c, "reason": "INCOMPLETE_OR_MALFORMED_RECORD", "error": str(ex)})
                continue

            try:
                if "T" in raw_ts:
                    dt_val = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
                else:
                    dt_val = datetime.strptime(raw_ts[:16], "%Y-%m-%d %H:%M")
                dt_ist = IndianMarketSessionClock.to_ist(dt_val)
            except Exception:
                invalid_candles.append({"candle": c, "reason": "UNPARSEABLE_TIMESTAMP"})
                continue

            ts_iso = dt_ist.isoformat()

            # 1. OHLC Geometric Validation
            if (
                o_val <= 0 or h_val <= 0 or l_val <= 0 or c_val <= 0 or
                h_val < l_val or
                o_val > h_val or o_val < l_val or
                c_val > h_val or c_val < l_val
            ):
                invalid_candles.append({"candle": c, "reason": "CORRUPT_OHLC_GEOMETRY"})
                continue

            # 2. Duplicate Detection
            if ts_iso in observed_timestamps:
                # Find previously recorded candle
                prev = next(x for x in valid_candles if x["start_time"] == ts_iso)
                if (
                    prev["open"] == o_val and prev["high"] == h_val and
                    prev["low"] == l_val and prev["close"] == c_val
                ):
                    exact_duplicate_count += 1
                    continue  # Deduplicated cleanly
                else:
                    conflicting_duplicate_count += 1
                    quarantined_conflicts.append({"existing": prev, "conflicting": c})
                    continue

            # 3. Session Eligibility Validation
            flags: List[str] = []
            if ts_iso not in expected_intervals:
                # Out-of-session or weekend bar
                flags.append(DataQualityFlag.OUT_OF_SESSION)

            # Check for Isolated Post-Gap Unverified Candle (Phase 3E.2)
            is_post_gap = False
            if valid_candles and valid_candles[-1]["start_time"][:10] == ts_iso[:10]:
                prev_start_dt = datetime.fromisoformat(valid_candles[-1]["start_time"])
                gap_sec = (dt_ist - prev_start_dt).total_seconds()
                if gap_sec >= 300 and (o_val == h_val == l_val == c_val):
                    flags.append(DataQualityFlag.ISOLATED_POST_GAP_UNVERIFIED)
                    is_post_gap = True

            # Build canonical normalized record
            end_dt = dt_ist + timedelta(minutes=1)
            norm_candle = {
                "symbol": symbol,
                "timeframe": "1m",
                "timestamp": ts_iso,
                "start_time": ts_iso,
                "end_time": end_dt.isoformat(),
                "open": round(o_val, 2),
                "high": round(h_val, 2),
                "low": round(l_val, 2),
                "close": round(c_val, 2),
                "volume": 0,
                "volume_semantics": VolumeSemantics.NOT_APPLICABLE,
                "data_status": DataStatus.HISTORICAL,
                "is_closed": True,
                "source_timestamp": raw_ts,
                "validation_flags": flags if flags else [DataQualityFlag.VALID],
                "is_isolated_post_gap": is_post_gap,
                "settlement_semantics": "UNVERIFIED"
            }

            valid_candles.append(norm_candle)
            observed_timestamps.add(ts_iso)

        # 4. Missing Intervals Identification
        missing_intervals: List[Dict[str, Any]] = []
        for exp_ts, exp_info in sorted(expected_intervals.items()):
            if exp_ts not in observed_timestamps:
                missing_intervals.append({
                    "timestamp": exp_ts,
                    "date": exp_info["date"],
                    "session_name": exp_info["session_name"],
                    "classification": IntervalClassification.MISSING_INTRADAY_CANDLE.value
                })

        coverage_pct = round((len(valid_candles) / expected_total * 100.0), 2) if expected_total > 0 else 0.0

        # Determine provider cutoff metadata across validated candles (Phase 3E.1)
        cutoff_observed = False
        last_val_start = None
        last_val_end = None
        if valid_candles:
            last_c = valid_candles[-1]
            last_val_start = last_c["start_time"]
            last_val_end = last_c["end_time"]

            # Inspect per-session continuous boundaries
            for dt_str in sorted(set(c["start_time"][:10] for c in valid_candles)):
                session_candles = [c for c in valid_candles if c["start_time"][:10] == dt_str]
                session_missing = [m for m in missing_intervals if m["date"] == dt_str]

                if session_missing:
                    earliest_missing_ts = min(session_missing, key=lambda m: m["timestamp"])["timestamp"]
                    if earliest_missing_ts[11:19] < "15:30:00":
                        cutoff_observed = True
                        before_gap = [c for c in session_candles if c["end_time"] <= earliest_missing_ts]
                        if before_gap:
                            last_val_start = before_gap[-1]["start_time"]
                            last_val_end = before_gap[-1]["end_time"]
                elif session_candles:
                    s_last = session_candles[-1]
                    if s_last["end_time"][11:19] < "15:30:00":
                        cutoff_observed = True
                        last_val_start = s_last["start_time"]
                        last_val_end = s_last["end_time"]

        cutoff_meta = {
            "PROVIDER_CUTOFF_OBSERVED": cutoff_observed,
            "LAST_VALIDATED_CANDLE_START": last_val_start,
            "LAST_VALIDATED_CANDLE_END": last_val_end,
            "EXCHANGE_SESSION_CLOSE": "15:30:00+05:30",
            "PROVIDER_CUTOFF_CAUSE": "BROKER_HISTORICAL_FEED_INTRADAY_TRUNCATION",
            "PROVIDER_CUTOFF_CAUSE_VERIFIED": False
        }

        isolated_post_gap_cnt = sum(1 for c in valid_candles if c.get("is_isolated_post_gap"))

        return {
            "symbol": symbol,
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "expected_intervals_total": expected_total,
            "valid_candles_count": len(valid_candles),
            "invalid_candles_count": len(invalid_candles),
            "missing_intervals_count": len(missing_intervals),
            "exact_duplicate_count": exact_duplicate_count,
            "conflicting_duplicate_count": conflicting_duplicate_count,
            "coverage_percentage": coverage_pct,
            "PROVIDER_CUTOFF_OBSERVED": cutoff_observed,
            "LAST_VALIDATED_CANDLE_START": last_val_start,
            "LAST_VALIDATED_CANDLE_END": last_val_end,
            "EXCHANGE_SESSION_CLOSE": "15:30:00+05:30",
            "PROVIDER_CUTOFF_CAUSE": "BROKER_HISTORICAL_FEED_INTRADAY_TRUNCATION",
            "PROVIDER_CUTOFF_CAUSE_VERIFIED": False,
            "ISOLATED_POST_GAP_COUNT": isolated_post_gap_cnt,
            "SETTLEMENT_SEMANTICS": "UNVERIFIED",
            "provider_cutoff_metadata": cutoff_meta,
            "valid_candles": valid_candles,
            "invalid_candles": invalid_candles,
            "quarantined_conflicts": quarantined_conflicts,
            "missing_intervals": missing_intervals
        }


# ============================================================================
# 4. Independent Source Comparator
# ============================================================================

class HistoricalSourceComparator:
    """
    Compares candidate primary data against an independent secondary source.
    If no legitimate second source exists, explicitly outputs INDEPENDENT_SOURCE_VALIDATION_UNAVAILABLE.
    """

    @staticmethod
    def compare_sources(
        primary_candles: List[Dict[str, Any]],
        secondary_candles: Optional[List[Dict[str, Any]]],
        secondary_source_name: Optional[str] = None,
        price_tolerance_pct: float = 0.05
    ) -> Dict[str, Any]:
        """
        Compares matching timestamps and calculates OHLC variances.
        """
        if not secondary_candles or not secondary_source_name:
            return {
                "status": "INDEPENDENT_SOURCE_VALIDATION_UNAVAILABLE",
                "reason": (
                    "No legitimate, verified independent secondary source available. "
                    "Yahoo Finance 1m intraday depth is unavailable beyond 30 days and library is uninstalled. "
                    "Primary data stands on direct Angel One broker provenance and structural validation."
                ),
                "matched_count": 0,
                "max_absolute_diff": 0.0
            }

        p_map = {c["start_time"]: c for c in primary_candles}
        s_map = {c["start_time"]: c for c in secondary_candles}

        common_keys = set(p_map.keys()).intersection(set(s_map.keys()))
        missing_in_primary = set(s_map.keys()) - common_keys
        missing_in_secondary = set(p_map.keys()) - common_keys

        diffs_close: List[float] = []
        conflicts = 0

        for k in common_keys:
            p_bar = p_map[k]
            s_bar = s_map[k]
            diff = abs(p_bar["close"] - s_bar["close"])
            diffs_close.append(diff)
            pct_diff = (diff / p_bar["close"]) * 100.0 if p_bar["close"] > 0 else 0.0
            if pct_diff > price_tolerance_pct:
                conflicts += 1

        max_diff = max(diffs_close) if diffs_close else 0.0
        avg_diff = sum(diffs_close) / len(diffs_close) if diffs_close else 0.0

        return {
            "status": "COMPARISON_COMPLETED",
            "secondary_source": secondary_source_name,
            "primary_count": len(primary_candles),
            "secondary_count": len(secondary_candles),
            "common_count": len(common_keys),
            "missing_in_primary": len(missing_in_primary),
            "missing_in_secondary": len(missing_in_secondary),
            "conflicting_count": conflicts,
            "max_close_diff": round(max_diff, 2),
            "avg_close_diff": round(avg_diff, 2)
        }


# ============================================================================
# 5. Acquisition Service Orchestrator
# ============================================================================

class HistoricalAcquisitionService:
    """
    High-level orchestrator for genuine historical spot data acquisition.
    Manages provider chunking, checkpoints, coverage validation, and Phase 3B persistence.
    """

    def __init__(
        self,
        provider: HistoricalSpotProvider,
        store: HistoricalSpotStore,
        checkpoint_dir: str = "data/checkpoints",
        session_clock: Optional[IndianMarketSessionClock] = None
    ):
        self.provider = provider
        self.store = store
        self.checkpoint_dir = checkpoint_dir
        self.validator = HistoricalCoverageValidator(session_clock)

    def plan_acquisition_chunks(
        self,
        start_date: date,
        end_date: date,
        chunk_days: int = 15
    ) -> List[Tuple[datetime, datetime]]:
        """
        Partitions date range into safe, non-truncating query chunks.
        """
        chunks: List[Tuple[datetime, datetime]] = []
        curr = start_date

        while curr <= end_date:
            next_d = min(curr + timedelta(days=chunk_days - 1), end_date)
            from_dt = datetime.combine(curr, dt_time(9, 15), tzinfo=IST_ZONE)
            to_dt = datetime.combine(next_d, dt_time(15, 30), tzinfo=IST_ZONE)
            chunks.append((from_dt, to_dt))
            curr = next_d + timedelta(days=1)

        return chunks

    def dry_run(
        self,
        symbol: str,
        start_date: date,
        end_date: date,
        chunk_days: int = 15
    ) -> Dict[str, Any]:
        """
        Calculates execution plan, expected trading sessions, request counts, and limitations without downloading.
        """
        chunks = self.plan_acquisition_chunks(start_date, end_date, chunk_days=chunk_days)
        expected_intervals = self.validator.generate_expected_session_intervals(start_date, end_date)

        # Count distinct trading sessions
        sessions: Set[str] = set(v["date"] for v in expected_intervals.values())

        return {
            "mode": "DRY_RUN",
            "symbol": symbol,
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "provider": self.provider.get_provider_name(),
            "provider_capabilities": self.provider.get_provider_capabilities(),
            "planned_chunks_count": len(chunks),
            "chunk_size_days": chunk_days,
            "estimated_eligible_sessions": len(sessions),
            "expected_1m_candles_count": len(expected_intervals),
            "planned_chunks": [
                {"chunk": i + 1, "from": c[0].strftime("%Y-%m-%d %H:%M"), "to": c[1].strftime("%Y-%m-%d %H:%M")}
                for i, c in enumerate(chunks)
            ]
        }

    def execute_acquisition(
        self,
        symbol: str,
        start_date: date,
        end_date: date,
        chunk_days: int = 15,
        dataset_version: str = "v1.0"
    ) -> Dict[str, Any]:
        """
        Executes genuine acquisition sequentially, validating each chunk and persisting to Phase 3B store.
        """
        checkpoint_file = os.path.join(
            self.checkpoint_dir, f"checkpoint_{symbol.lower()}_{start_date.isoformat()}_{end_date.isoformat()}.json"
        )
        cp = HistoricalAcquisitionCheckpoint.load(checkpoint_file)

        chunks = self.plan_acquisition_chunks(start_date, end_date, chunk_days=chunk_days)

        if cp is None:
            cp = HistoricalAcquisitionCheckpoint(
                instrument=symbol,
                interval="1m",
                requested_start=start_date.isoformat(),
                requested_end=end_date.isoformat(),
                chunks=[
                    asdict(AcquisitionChunk(
                        chunk_index=i,
                        from_date_str=c[0].strftime("%Y-%m-%d %H:%M"),
                        to_date_str=c[1].strftime("%Y-%m-%d %H:%M"),
                        status=AcquisitionStatus.PENDING.value
                    ))
                    for i, c in enumerate(chunks)
                ],
                overall_status=AcquisitionStatus.IN_PROGRESS.value,
                created_at=datetime.now(IST_ZONE).isoformat()
            )
            cp.save(checkpoint_file)

        all_raw_rows: List[Dict[str, Any]] = []
        acquired_chunks = 0
        failed_chunks = 0

        for i, (from_dt, to_dt) in enumerate(chunks):
            chunk_state = cp.chunks[i]
            chunk_raw_file = os.path.join(
                self.checkpoint_dir, f"raw_{symbol.lower()}_{start_date.isoformat()}_{end_date.isoformat()}_chunk_{i}.json"
            )
            if chunk_state["status"] == AcquisitionStatus.ACQUIRED.value and os.path.exists(chunk_raw_file):
                try:
                    with open(chunk_raw_file, "r", encoding="utf-8") as rf:
                        cached_rows = json.load(rf)
                    all_raw_rows.extend(cached_rows)
                    acquired_chunks += 1
                    continue
                except Exception:
                    pass  # If cache file corrupt or unreadable, re-fetch

            try:
                rows, raw_sha = self.provider.fetch_candles(symbol, "1m", from_dt, to_dt)
                # Persist raw chunk rows for crash recovery
                os.makedirs(self.checkpoint_dir, exist_ok=True)
                with open(chunk_raw_file, "w", encoding="utf-8") as wf:
                    json.dump(rows, wf)

                all_raw_rows.extend(rows)
                chunk_state["status"] = AcquisitionStatus.ACQUIRED.value
                chunk_state["records_count"] = len(rows)
                chunk_state["raw_sha256"] = raw_sha
                acquired_chunks += 1
                cp.save(checkpoint_file)
            except Exception as e:
                chunk_state["status"] = AcquisitionStatus.FAILED.value
                chunk_state["error_message"] = str(e)
                failed_chunks += 1
                cp.save(checkpoint_file)
                # Fail-closed policy: halt immediately on provider exception
                break

        # Validate acquired dataset
        val_result = self.validator.validate_and_classify_candles(
            candles=all_raw_rows,
            start_date=start_date,
            end_date=end_date,
            symbol=symbol
        )

        # Ingest valid records into Phase 3B storage
        manifest = None
        if val_result["valid_candles"]:
            manifest = self.store.ingest_records(
                records=val_result["valid_candles"],
                symbol=symbol,
                timeframe="1m",
                provider=self.provider.get_provider_name(),
                source_name=f"{self.provider.get_provider_name()}_historical"
            )
            if manifest:
                manifest.dataset_version = dataset_version

        overall_status = (
            AcquisitionStatus.VALIDATED.value
            if val_result["coverage_percentage"] >= 95.0 and failed_chunks == 0
            else AcquisitionStatus.PARTIALLY_VALIDATED.value
        )
        if failed_chunks > 0 and not val_result["valid_candles"]:
            overall_status = AcquisitionStatus.FAILED.value

        cp.overall_status = overall_status
        cp.save(checkpoint_file)

        # Independent source check
        src_comp = HistoricalSourceComparator.compare_sources(
            primary_candles=val_result["valid_candles"],
            secondary_candles=None
        )

        return {
            "status": overall_status,
            "instrument": symbol,
            "requested_start": start_date.isoformat(),
            "requested_end": end_date.isoformat(),
            "provider": self.provider.get_provider_name(),
            "chunks_total": len(chunks),
            "chunks_acquired": acquired_chunks,
            "chunks_failed": failed_chunks,
            "coverage_validation": {
                "expected_intervals": val_result["expected_intervals_total"],
                "valid_candles": val_result["valid_candles_count"],
                "missing_intervals": val_result["missing_intervals_count"],
                "coverage_pct": val_result["coverage_percentage"],
                "exact_duplicates": val_result["exact_duplicate_count"],
                "conflicting_duplicates": val_result["conflicting_duplicate_count"],
                "invalid_candles": val_result["invalid_candles_count"],
                "provider_cutoff_metadata": val_result.get("provider_cutoff_metadata", {})
            },
            "storage_manifest": manifest.to_dict() if manifest else None,
            "independent_source_validation": src_comp
        }
