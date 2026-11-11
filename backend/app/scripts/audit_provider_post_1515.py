"""
TradeLens AI — Phase 3E.5.2.1: Isolated Read-Only Angel One Historical Provider Probe.

Implements a minimal, strictly read-only, bounded probe client for diagnosing Angel One
SmartAPI historical `getCandleData` intraday behavior around 15:15–15:30 IST across:
  1. NIFTY   1m  (09:15–15:30 IST) on reference trading date (default 2026-11-06)
  2. NIFTY   1m  (09:15–16:00 IST) on reference trading date
  3. NIFTY   5m  (09:15–15:30 IST) on reference trading date
  4. BANKNIFTY 1m (09:15–15:30 IST) on reference trading date
  5. RELIANCE 1m (09:15–15:30 IST) on reference trading date
  6. NIFTY   1m  (09:15–15:30 IST) on 2026-05-08 (182 calendar days prior retention check)

Safety & Read-Only Guarantees:
- Never uses AngelOneSmartAPIProvider, live quote endpoints, position endpoints, or order/account APIs.
- Never writes to data/historical/ or data/checkpoints/, nor runs replay or outcome tracking.
- Validates all 4 credentials (API key, client code, password, Base32 TOTP secret) before any network call.
- Rejects missing/placeholder/invalid credentials with zero HTTP requests.
- Redacts all credentials, Authorization headers, JWTs, refresh/feed tokens from errors and reports.
- Enforces strict HTTP budgets: <=2 auth attempts, <=3 per-probe attempts, <=18 candle attempts, <=20 total HTTP attempts.
- Defaults to offline preflight mode unless explicit --execute-live-probes authorization is supplied.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import sys
from dataclasses import dataclass, asdict
from datetime import datetime, date, time as dt_time, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Union

from backend.app.data.historical_acquisition import (
    ANGEL_CREDENTIAL_KEYS,
    AngelOneHistoricalSpotProvider,
    normalize_angel_one_interval,
    redact_sensitive_text,
    resolve_angel_credentials,
    resolve_angel_one_instrument,
)
from backend.app.data.session_clock import IndianMarketSessionClock, IST_ZONE


REPO_ROOT: Path = Path(__file__).resolve().parents[3]
DEFAULT_DOTENV_PATH: Path = REPO_ROOT / ".env"

LOGICAL_CANDLE_PROBES_PLANNED: int = 6
DEFAULT_REFERENCE_TRADING_DATE: date = date(2026, 11, 6)
EXPLICIT_RETENTION_CHECK_DATE: date = date(2026, 5, 8)  # 182 calendar days before 2026-11-06

MAX_AUTH_HTTP_ATTEMPTS: int = 2
MAX_ATTEMPTS_PER_PROBE: int = 3
MAX_CANDLE_HTTP_ATTEMPTS: int = 18
MAX_TOTAL_HTTP_ATTEMPTS: int = 20

DEFAULT_CONNECT_TIMEOUT_SEC: float = 10.0
DEFAULT_READ_TIMEOUT_SEC: float = 12.0
DEFAULT_RATE_LIMIT_PAUSE_SEC: float = 0.5

ALLOWED_ANGEL_ONE_URLS: Set[str] = {
    f"{AngelOneHistoricalSpotProvider.BASE_URL}{AngelOneHistoricalSpotProvider.LOGIN_ENDPOINT}",
    f"{AngelOneHistoricalSpotProvider.BASE_URL}{AngelOneHistoricalSpotProvider.CANDLE_ENDPOINT}",
}
LOGIN_URL: str = f"{AngelOneHistoricalSpotProvider.BASE_URL}{AngelOneHistoricalSpotProvider.LOGIN_ENDPOINT}"
CANDLE_URL: str = f"{AngelOneHistoricalSpotProvider.BASE_URL}{AngelOneHistoricalSpotProvider.CANDLE_ENDPOINT}"


class ProbeClassification:
    PASS = "PASS"
    FAIL = "FAIL"
    INCONCLUSIVE = "INCONCLUSIVE"
    BLOCKED_MISSING_CREDENTIALS = "BLOCKED_MISSING_CREDENTIALS"


@dataclass(frozen=True)
class ProbeSpec:
    probe_id: str
    purpose: str
    instrument: str
    token: str
    exchange: str
    interval: str
    session_date: date
    from_dt: datetime
    to_dt: datetime


class HttpExecutionBudgetGuard:
    """Enforces endpoint allowlist and strict global/per-probe HTTP attempt caps."""

    def __init__(
        self,
        max_auth_attempts: int = MAX_AUTH_HTTP_ATTEMPTS,
        max_attempts_per_probe: int = MAX_ATTEMPTS_PER_PROBE,
        max_candle_attempts: int = MAX_CANDLE_HTTP_ATTEMPTS,
        max_total_http_attempts: int = MAX_TOTAL_HTTP_ATTEMPTS,
    ):
        self.max_auth_attempts = max_auth_attempts
        self.max_attempts_per_probe = max_attempts_per_probe
        self.max_candle_attempts = max_candle_attempts
        self.max_total_http_attempts = max_total_http_attempts

        self.auth_http_attempts: int = 0
        self.candle_http_attempts: int = 0
        self.total_http_attempts: int = 0
        self.current_probe_id: Optional[str] = None
        self.current_probe_candle_attempts: int = 0
        self.request_log: List[Dict[str, Any]] = []

    def begin_probe(self, probe_id: str) -> None:
        self.current_probe_id = probe_id
        self.current_probe_candle_attempts = 0

    def before_request(self, url: str) -> None:
        if url not in ALLOWED_ANGEL_ONE_URLS:
            raise PermissionError(f"FORBIDDEN_ENDPOINT_BLOCKED: {url}")

        if self.total_http_attempts >= self.max_total_http_attempts:
            raise PermissionError(
                f"GLOBAL_HTTP_ATTEMPT_LIMIT_EXCEEDED: limit={self.max_total_http_attempts}"
            )

        if url == LOGIN_URL:
            if self.auth_http_attempts >= self.max_auth_attempts:
                raise PermissionError(
                    f"MAX_AUTH_ATTEMPTS_EXCEEDED: limit={self.max_auth_attempts}"
                )
            self.auth_http_attempts += 1
            self.total_http_attempts += 1
            self.request_log.append({
                "type": "AUTH",
                "endpoint": AngelOneHistoricalSpotProvider.LOGIN_ENDPOINT,
                "probe_id": self.current_probe_id,
            })
            return

        if url == CANDLE_URL:
            if self.current_probe_candle_attempts >= self.max_attempts_per_probe:
                raise PermissionError(
                    f"MAX_ATTEMPTS_PER_PROBE_EXCEEDED: probe={self.current_probe_id} limit={self.max_attempts_per_probe}"
                )
            if self.candle_http_attempts >= self.max_candle_attempts:
                raise PermissionError(
                    f"MAX_CANDLE_ATTEMPTS_EXCEEDED: limit={self.max_candle_attempts}"
                )
            self.current_probe_candle_attempts += 1
            self.candle_http_attempts += 1
            self.total_http_attempts += 1
            self.request_log.append({
                "type": "CANDLE",
                "endpoint": AngelOneHistoricalSpotProvider.CANDLE_ENDPOINT,
                "probe_id": self.current_probe_id,
            })
            return

        raise PermissionError(f"FORBIDDEN_ENDPOINT_BLOCKED: {url}")


def build_six_probe_plan(
    reference_date: Optional[date] = None,
    session_clock: Optional[IndianMarketSessionClock] = None,
) -> List[ProbeSpec]:
    """Constructs the exact six single-session diagnostic probes."""
    clock = session_clock or IndianMarketSessionClock()
    ref_date = reference_date or DEFAULT_REFERENCE_TRADING_DATE
    ret_date = EXPLICIT_RETENTION_CHECK_DATE

    if not clock.is_trading_day(ref_date):
        raise ValueError(f"Reference date {ref_date.isoformat()} is not a valid NSE trading session.")
    if not clock.is_trading_day(ret_date):
        raise ValueError(f"Historical retention date {ret_date.isoformat()} is not a valid NSE trading session.")

    specs_raw = [
        (
            "nifty_1m_to_1530",
            "Check whether NIFTY 1m continuous bars terminate at 15:14 IST or cover 15:15-15:29 when todate=15:30.",
            "NIFTY",
            "ONE_MINUTE",
            ref_date,
            dt_time(15, 30),
        ),
        (
            "nifty_1m_to_1600",
            "Check NIFTY 1m response through 16:00 IST and isolate any post-15:30 observations.",
            "NIFTY",
            "ONE_MINUTE",
            ref_date,
            dt_time(16, 0),
        ),
        (
            "nifty_5m_to_1530",
            "Check whether NIFTY 5m bars cover 15:15, 15:20, and 15:25 when todate=15:30.",
            "NIFTY",
            "FIVE_MINUTE",
            ref_date,
            dt_time(15, 30),
        ),
        (
            "banknifty_1m_to_1530",
            "Verify whether BANKNIFTY 1m exhibits the same 15:15-15:30 intraday profile as NIFTY.",
            "BANKNIFTY",
            "ONE_MINUTE",
            ref_date,
            dt_time(15, 30),
        ),
        (
            "reliance_1m_to_1530",
            "Compare index feed against cash equity RELIANCE 1m around 15:15-15:30 IST.",
            "RELIANCE",
            "ONE_MINUTE",
            ref_date,
            dt_time(15, 30),
        ),
        (
            "nifty_1m_180d_prior_trading_session",
            (
                f"Single-session historical retention check on {ret_date.isoformat()} "
                f"({(DEFAULT_REFERENCE_TRADING_DATE - ret_date).days} calendar days before "
                f"{DEFAULT_REFERENCE_TRADING_DATE.isoformat()})."
            ),
            "NIFTY",
            "ONE_MINUTE",
            ret_date,
            dt_time(15, 30),
        ),
    ]

    probes: List[ProbeSpec] = []
    for probe_id, purpose, instrument, interval, s_date, end_time in specs_raw:
        token, exchange = resolve_angel_one_instrument(instrument)
        norm_interval = normalize_angel_one_interval(interval)
        probes.append(
            ProbeSpec(
                probe_id=probe_id,
                purpose=purpose,
                instrument=instrument,
                token=token,
                exchange=exchange,
                interval=norm_interval,
                session_date=s_date,
                from_dt=datetime.combine(s_date, dt_time(9, 15), tzinfo=IST_ZONE),
                to_dt=datetime.combine(s_date, end_time, tzinfo=IST_ZONE),
            )
        )

    validate_probe_plan(probes, session_clock=clock)
    return probes


def validate_probe_plan(
    probes: List[ProbeSpec],
    session_clock: Optional[IndianMarketSessionClock] = None,
) -> None:
    """Enforces exact count of 6 single-session probes, valid trading dates, tokens, and intervals."""
    clock = session_clock or IndianMarketSessionClock()
    if len(probes) != LOGICAL_CANDLE_PROBES_PLANNED:
        raise ValueError(
            f"Expected exactly {LOGICAL_CANDLE_PROBES_PLANNED} logical probes, got {len(probes)}."
        )
    seen_ids: Set[str] = set()
    for p in probes:
        if p.probe_id in seen_ids:
            raise ValueError(f"Duplicate probe_id: {p.probe_id}")
        seen_ids.add(p.probe_id)

        if p.from_dt.date() != p.to_dt.date() or p.from_dt.date() != p.session_date:
            raise ValueError(f"Probe {p.probe_id} violates single-session query rule.")
        if p.from_dt >= p.to_dt:
            raise ValueError(f"Probe {p.probe_id} has invalid time bounds {p.from_dt} >= {p.to_dt}.")
        if p.from_dt.time() < dt_time(9, 15) or p.to_dt.time() > dt_time(16, 0):
            raise ValueError(f"Probe {p.probe_id} falls outside allowed 09:15-16:00 single-session window.")
        if not clock.is_trading_day(p.session_date):
            raise ValueError(f"Probe {p.probe_id} targets non-trading date {p.session_date.isoformat()}.")

        expected_token, expected_exchange = resolve_angel_one_instrument(p.instrument)
        if p.token != expected_token or p.exchange != expected_exchange:
            raise ValueError(f"Probe {p.probe_id} token/exchange mismatch.")
        if p.interval != normalize_angel_one_interval(p.interval):
            raise ValueError(f"Probe {p.probe_id} interval not normalized: {p.interval}")


def validate_safe_report_path(
    output_path: Union[str, Path],
    repo_root: Optional[Union[str, Path]] = None,
) -> Path:
    """
    Validates that a report output path avoids directory traversal, symlinks,
    and protected historical dataset / checkpoint directories.
    """
    raw_str = str(output_path or "").strip()
    if not raw_str:
        raise ValueError("Report output path must not be empty.")
    raw_path = Path(raw_str)
    if ".." in raw_path.parts:
        raise ValueError(f"Path traversal ('..') is forbidden in report path: {raw_str}")

    if raw_path.is_symlink() or raw_path.parent.is_symlink():
        raise ValueError(f"Symlink report paths or symlink parent directories are forbidden: {raw_str}")

    root = Path(repo_root) if repo_root is not None else REPO_ROOT
    root_real = Path(os.path.realpath(root))
    abs_target = raw_path if raw_path.is_absolute() else (root / raw_path)

    system_symlink_roots = {
        Path("/"),
        Path("/var"),
        Path("/tmp"),
        Path("/etc"),
        Path("/private"),
        Path("/private/var"),
        Path("/private/tmp"),
    }
    cursor = abs_target
    while cursor not in system_symlink_roots and cursor != cursor.parent:
        if cursor.is_symlink():
            raise ValueError(f"Symlink component blocked in report path: {cursor}")
        cursor = cursor.parent

    resolved_target = Path(os.path.realpath(abs_target))
    protected_dirs = [
        Path(os.path.realpath(root_real / "data" / "historical")),
        Path(os.path.realpath(root_real / "data" / "checkpoints")),
        Path(os.path.realpath(root_real / ".git")),
    ]
    for pdir in protected_dirs:
        if resolved_target == pdir or pdir in resolved_target.parents:
            raise ValueError(f"Writing inside protected directory is forbidden: {pdir}")

    return resolved_target


def parse_timestamp_to_ist(raw_ts: Any) -> datetime:
    """Parses Angel One raw timestamps into timezone-aware Asia/Kolkata datetimes."""
    ts_str = str(raw_ts or "").strip()
    if not ts_str:
        raise ValueError("Empty timestamp")
    if "T" in ts_str or ts_str.endswith("Z"):
        dt_val = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    else:
        fmt = "%Y-%m-%d %H:%M:%S" if len(ts_str) >= 19 else "%Y-%m-%d %H:%M"
        dt_val = datetime.strptime(ts_str[:19], fmt)
    return IndianMarketSessionClock.to_ist(dt_val)


def analyze_probe_candles(probe: ProbeSpec, raw_rows: List[Any]) -> Dict[str, Any]:
    """
    Normalizes candle timestamps to IST and computes session coverage, missing intervals,
    post-15:30 classification, and flat post-gap observations without inferring unverified semantics.
    """
    step_minutes = 5 if probe.interval == "FIVE_MINUTE" else 1
    step_seconds = step_minutes * 60

    parsed: List[Dict[str, Any]] = []
    parse_errors = 0
    for row in raw_rows:
        try:
            if isinstance(row, (list, tuple)):
                if len(row) < 5:
                    raise ValueError("Row length < 5")
                dt_ist = parse_timestamp_to_ist(row[0])
                o_val, h_val, l_val, c_val = float(row[1]), float(row[2]), float(row[3]), float(row[4])
            elif isinstance(row, dict):
                raw_ts = row.get("start_time") or row.get("timestamp") or row.get("time")
                dt_ist = parse_timestamp_to_ist(raw_ts)
                o_val = float(row["open"])
                h_val = float(row["high"])
                l_val = float(row["low"])
                c_val = float(row["close"])
            else:
                raise ValueError("Unsupported row format")
            parsed.append({
                "dt_ist": dt_ist,
                "iso": dt_ist.isoformat(),
                "open": o_val,
                "high": h_val,
                "low": l_val,
                "close": c_val,
            })
        except Exception:
            parse_errors += 1

    parsed.sort(key=lambda r: r["dt_ist"])
    ist_timestamps = [r["iso"] for r in parsed]

    count_pre_0915 = 0
    count_before_1515 = 0
    count_1515_to_1530 = 0
    count_after_1530 = 0
    post_1530_records: List[Dict[str, Any]] = []

    observed_set: Set[str] = set()
    for item in parsed:
        observed_set.add(item["iso"])
        t = item["dt_ist"].time()
        if t < dt_time(9, 15):
            count_pre_0915 += 1
        elif t < dt_time(15, 15):
            count_before_1515 += 1
        elif t < dt_time(15, 30):
            count_1515_to_1530 += 1
        else:
            count_after_1530 += 1
            is_flat = (item["open"] == item["high"] == item["low"] == item["close"])
            post_1530_records.append({
                "timestamp": item["iso"],
                "open": item["open"],
                "high": item["high"],
                "low": item["low"],
                "close": item["close"],
                "is_flat_ohlc": is_flat,
                "classification": "POST_1530_UNVERIFIED",
            })

    # Expected regular session intervals within [09:15, min(to_dt, 15:30))
    expected_end_dt = min(probe.to_dt, datetime.combine(probe.session_date, dt_time(15, 30), tzinfo=IST_ZONE))
    missing_intervals: List[str] = []
    missing_1515_to_1530_intervals: List[str] = []

    cursor_dt = probe.from_dt
    while cursor_dt < expected_end_dt:
        iso_k = cursor_dt.isoformat()
        if iso_k not in observed_set:
            missing_intervals.append(iso_k)
            if cursor_dt.time() >= dt_time(15, 15):
                missing_1515_to_1530_intervals.append(iso_k)
        cursor_dt += timedelta(minutes=step_minutes)

    # Detect isolated flat observations occurring after a continuity gap
    gap_threshold_sec = max(300, step_seconds * 2)
    flat_post_gap_observations: List[Dict[str, Any]] = []
    for idx in range(1, len(parsed)):
        prev_item = parsed[idx - 1]
        curr_item = parsed[idx]
        gap_sec = (curr_item["dt_ist"] - prev_item["dt_ist"]).total_seconds()
        is_flat = (
            curr_item["open"] == curr_item["high"] == curr_item["low"] == curr_item["close"]
        )
        if gap_sec >= gap_threshold_sec and is_flat:
            flat_post_gap_observations.append({
                "timestamp": curr_item["iso"],
                "open": curr_item["open"],
                "high": curr_item["high"],
                "low": curr_item["low"],
                "close": curr_item["close"],
                "gap_seconds_since_previous": int(gap_sec),
                "classification": "ISOLATED_POST_GAP_FLAT_OBSERVATION_UNVERIFIED",
            })

    return {
        "received_record_count": len(raw_rows),
        "parsed_record_count": len(parsed),
        "parse_error_count": parse_errors,
        "first_timestamp": ist_timestamps[0] if ist_timestamps else None,
        "last_timestamp": ist_timestamps[-1] if ist_timestamps else None,
        "ist_candle_timestamps": ist_timestamps,
        "count_pre_0915": count_pre_0915,
        "count_before_1515": count_before_1515,
        "count_1515_to_1530": count_1515_to_1530,
        "count_after_1530": count_after_1530,
        "post_1530_records": post_1530_records,
        "missing_intervals": missing_intervals,
        "missing_intervals_count": len(missing_intervals),
        "missing_1515_to_1530_intervals": missing_1515_to_1530_intervals,
        "missing_1515_to_1530_count": len(missing_1515_to_1530_intervals),
        "flat_post_gap_observations": flat_post_gap_observations,
    }


def _build_probe_interpretation(
    probe: ProbeSpec,
    status: str,
    analysis: Optional[Dict[str, Any]],
    redacted_errors: List[str],
) -> str:
    if status == ProbeClassification.BLOCKED_MISSING_CREDENTIALS:
        return "Blocked locally due to missing, placeholder, or invalid-format credentials; zero HTTP requests issued."
    if status == ProbeClassification.FAIL:
        return (
            "Probe failed due to authentication, HTTP, or status=False API error "
            f"({'; '.join(redacted_errors) if redacted_errors else 'unspecified error'}). "
            "An API error does not prove the provider lacks underlying candles."
        )
    if status == ProbeClassification.INCONCLUSIVE:
        if analysis and analysis["received_record_count"] == 0:
            return (
                "API returned status=True with an empty record list for this window. "
                "An empty API response does not prove that the provider lacks candles for the session."
            )
        return "Response did not yield valid parseable IST candles; probe result is inconclusive."

    assert analysis is not None
    parts = [
        f"Received {analysis['parsed_record_count']} candles on {probe.session_date.isoformat()} "
        f"({analysis['count_before_1515']} before 15:15 IST, "
        f"{analysis['count_1515_to_1530']} in 15:15-15:30 IST, "
        f"{analysis['count_after_1530']} at/after 15:30 IST; "
        f"{analysis['missing_1515_to_1530_count']} missing in 15:15-15:30 IST)."
    ]
    if analysis["flat_post_gap_observations"]:
        parts.append(
            f"Observed {len(analysis['flat_post_gap_observations'])} flat post-gap candle(s) "
            "classified as ISOLATED_POST_GAP_FLAT_OBSERVATION_UNVERIFIED without inferring official "
            "closing settlement or auction semantics from OHLC values alone."
        )
    if probe.to_dt.time() >= dt_time(16, 0) or analysis["count_after_1530"] > 0:
        parts.append(
            f"Post-15:30 records ({analysis['count_after_1530']}) are separated under POST_1530_UNVERIFIED; "
            "official settlement or auction semantics are never inferred from OHLC values alone."
        )
    return " ".join(parts)


def verify_probe_artifact_readiness(repo_root: Optional[Union[str, Path]] = None) -> Dict[str, Any]:
    """Verifies expected probe files, .gitignore coverage, probe definitions, and read-only AST boundaries."""
    root = Path(repo_root) if repo_root is not None else REPO_ROOT
    script_path = root / "backend" / "app" / "scripts" / "audit_provider_post_1515.py"
    test_path = root / "backend" / "tests" / "test_provider_probe_audit.py"
    gitignore_path = root / ".gitignore"
    env_example_path = root / ".env.example"

    gitignore_covers_env = False
    if gitignore_path.is_file():
        lines = [ln.strip() for ln in gitignore_path.read_text(encoding="utf-8").splitlines()]
        gitignore_covers_env = any(ln in (".env", "/.env", "*.env") for ln in lines)

    forbidden_refs_found: List[str] = []
    target_src = Path(__file__).read_text(encoding="utf-8")
    tree = ast.parse(target_src)
    forbidden_names = {
        "AngelOneSmartAPIProvider",
        "fetch_live_quotes",
        "get_live_positions",
        "HistoricalAcquisitionService",
        "HistoricalSpotStore",
        "HistoricalSpotReplayEngine",
        "SpotOutcomeTracker",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name in forbidden_names:
                    forbidden_refs_found.append(alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in forbidden_names:
                    forbidden_refs_found.append(alias.name)
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in forbidden_names:
                forbidden_refs_found.append(node.func.id)
            elif isinstance(node.func, ast.Attribute) and node.func.attr in forbidden_names:
                forbidden_refs_found.append(node.func.attr)

    return {
        "script_present": script_path.is_file(),
        "test_present": test_path.is_file(),
        "env_example_present": env_example_path.is_file(),
        "gitignore_covers_env": gitignore_covers_env,
        "logical_candle_probes_planned": LOGICAL_CANDLE_PROBES_PLANNED,
        "forbidden_references": sorted(set(forbidden_refs_found)),
    }


def _empty_probe_result(
    probe: ProbeSpec,
    status: str,
    http_attempts: int = 0,
    retry_count: int = 0,
    api_status: Optional[bool] = None,
    redacted_errors: Optional[List[str]] = None,
) -> Dict[str, Any]:
    errors = redacted_errors or []
    return {
        "probe_id": probe.probe_id,
        "purpose": probe.purpose,
        "instrument": probe.instrument,
        "token": probe.token,
        "exchange": probe.exchange,
        "interval": probe.interval,
        "requested_start": probe.from_dt.strftime("%Y-%m-%d %H:%M"),
        "requested_end": probe.to_dt.strftime("%Y-%m-%d %H:%M"),
        "http_attempts": http_attempts,
        "retry_count": retry_count,
        "api_status": api_status,
        "received_record_count": 0,
        "first_timestamp": None,
        "last_timestamp": None,
        "ist_candle_timestamps": [],
        "count_pre_0915": 0,
        "count_before_1515": 0,
        "count_1515_to_1530": 0,
        "count_after_1530": 0,
        "post_1530_records": [],
        "missing_intervals": [],
        "missing_1515_to_1530_intervals": [],
        "flat_post_gap_observations": [],
        "redacted_errors": errors,
        "status": status,
        "interpretation": _build_probe_interpretation(probe, status, None, errors),
    }


def run_post_1515_probe_audit(
    *,
    execute_live: bool = False,
    reference_date: Optional[date] = None,
    dotenv_path: Optional[Union[str, Path]] = DEFAULT_DOTENV_PATH,
    environ: Optional[Dict[str, str]] = None,
    rate_limit_pause_sec: float = DEFAULT_RATE_LIMIT_PAUSE_SEC,
    connect_timeout_sec: float = DEFAULT_CONNECT_TIMEOUT_SEC,
    read_timeout_sec: float = DEFAULT_READ_TIMEOUT_SEC,
    output_report_path: Optional[Union[str, Path]] = None,
    repo_root: Optional[Union[str, Path]] = None,
) -> Dict[str, Any]:
    """
    Executes either offline preflight verification (execute_live=False) or the bounded
    six-probe Angel One historical read-only audit (execute_live=True).
    """
    validated_output: Optional[Path] = None
    if output_report_path is not None:
        validated_output = validate_safe_report_path(output_report_path, repo_root=repo_root)

    probes = build_six_probe_plan(reference_date=reference_date)
    dotenv_candidates = [dotenv_path] if dotenv_path is not None else None
    cred_res = resolve_angel_credentials(environ=environ, dotenv_paths=dotenv_candidates)
    cred_statuses = dict(cred_res["statuses"])
    cred_sources = dict(cred_res["sources"])
    secret_values = list(cred_res["values"].values())

    readiness = verify_probe_artifact_readiness(repo_root=repo_root)
    budget_guard = HttpExecutionBudgetGuard()
    probe_results: List[Dict[str, Any]] = []

    if not cred_res["all_valid"]:
        reason = cred_res["blocking_reason"] or ProbeClassification.BLOCKED_MISSING_CREDENTIALS
        for p in probes:
            probe_results.append(
                _empty_probe_result(
                    p,
                    status=ProbeClassification.BLOCKED_MISSING_CREDENTIALS,
                    redacted_errors=[reason],
                )
            )
        overall_status = ProbeClassification.BLOCKED_MISSING_CREDENTIALS
    elif not execute_live:
        for p in probes:
            probe_results.append(
                _empty_probe_result(
                    p,
                    status=ProbeClassification.INCONCLUSIVE,
                    redacted_errors=["DRY_RUN_PREFLIGHT_ONLY"],
                )
            )
        overall_status = "PREFLIGHT_ONLY_ZERO_HTTP"
    else:
        vals = cred_res["values"]
        provider = AngelOneHistoricalSpotProvider(
            api_key=vals["ANGEL_API_KEY"],
            client_code=vals["ANGEL_CLIENT_CODE"],
            password=vals["ANGEL_PASSWORD"],
            totp_secret=vals["ANGEL_TOTP_SECRET"],
            max_chunk_days=1,
            rate_limit_pause_sec=rate_limit_pause_sec,
            max_auth_attempts=MAX_AUTH_HTTP_ATTEMPTS,
            connect_timeout_sec=connect_timeout_sec,
            read_timeout_sec=read_timeout_sec,
            env_paths=[],
            http_request_guard=budget_guard.before_request,
        )

        auth_ok = provider.connect()
        if provider.jwt_token:
            secret_values.append(provider.jwt_token)

        if not auth_ok:
            auth_err = redact_sensitive_text(
                provider.last_auth_error or "Authentication failed",
                extra_secrets=secret_values,
            )
            for p in probes:
                probe_results.append(
                    _empty_probe_result(
                        p,
                        status=ProbeClassification.FAIL,
                        api_status=False,
                        redacted_errors=[auth_err],
                    )
                )
            overall_status = ProbeClassification.FAIL
        else:
            halted_reason: Optional[str] = None
            for p in probes:
                if halted_reason is not None:
                    probe_results.append(
                        _empty_probe_result(
                            p,
                            status=ProbeClassification.FAIL,
                            api_status=False,
                            redacted_errors=[halted_reason],
                        )
                    )
                    continue

                budget_guard.begin_probe(p.probe_id)
                try:
                    raw_rows, _ = provider.fetch_candles(
                        symbol=p.instrument,
                        interval=p.interval,
                        from_dt=p.from_dt,
                        to_dt=p.to_dt,
                    )
                    if provider.jwt_token:
                        secret_values.append(provider.jwt_token)
                    meta = dict(provider.last_fetch_metadata)
                    api_status = meta.get("api_status")
                    redacted_errs = [meta["redacted_error"]] if meta.get("redacted_error") else []
                    http_attempts = int(meta.get("http_attempts", 0))
                    retry_count = int(meta.get("retry_count", 0))

                    if api_status is False:
                        probe_results.append(
                            _empty_probe_result(
                                p,
                                status=ProbeClassification.FAIL,
                                http_attempts=http_attempts,
                                retry_count=retry_count,
                                api_status=False,
                                redacted_errors=redacted_errs,
                            )
                        )
                        continue

                    analysis = analyze_probe_candles(p, raw_rows)
                    if analysis["received_record_count"] == 0 or analysis["parsed_record_count"] == 0:
                        status = ProbeClassification.INCONCLUSIVE
                    else:
                        status = ProbeClassification.PASS

                    probe_results.append({
                        "probe_id": p.probe_id,
                        "purpose": p.purpose,
                        "instrument": p.instrument,
                        "token": p.token,
                        "exchange": p.exchange,
                        "interval": p.interval,
                        "requested_start": p.from_dt.strftime("%Y-%m-%d %H:%M"),
                        "requested_end": p.to_dt.strftime("%Y-%m-%d %H:%M"),
                        "http_attempts": http_attempts,
                        "retry_count": retry_count,
                        "api_status": api_status,
                        "received_record_count": analysis["received_record_count"],
                        "first_timestamp": analysis["first_timestamp"],
                        "last_timestamp": analysis["last_timestamp"],
                        "ist_candle_timestamps": analysis["ist_candle_timestamps"],
                        "count_pre_0915": analysis["count_pre_0915"],
                        "count_before_1515": analysis["count_before_1515"],
                        "count_1515_to_1530": analysis["count_1515_to_1530"],
                        "count_after_1530": analysis["count_after_1530"],
                        "post_1530_records": analysis["post_1530_records"],
                        "missing_intervals": analysis["missing_intervals"],
                        "missing_1515_to_1530_intervals": analysis["missing_1515_to_1530_intervals"],
                        "flat_post_gap_observations": analysis["flat_post_gap_observations"],
                        "redacted_errors": redacted_errs,
                        "status": status,
                        "interpretation": _build_probe_interpretation(p, status, analysis, redacted_errs),
                    })
                except Exception as exc:
                    if provider.jwt_token:
                        secret_values.append(provider.jwt_token)
                    meta = dict(provider.last_fetch_metadata)
                    err_str = redact_sensitive_text(str(exc), extra_secrets=secret_values)
                    probe_results.append(
                        _empty_probe_result(
                            p,
                            status=ProbeClassification.FAIL,
                            http_attempts=int(meta.get("http_attempts", budget_guard.current_probe_candle_attempts)),
                            retry_count=int(meta.get("retry_count", max(0, budget_guard.current_probe_candle_attempts - 1))),
                            api_status=False,
                            redacted_errors=[err_str],
                        )
                    )
                    if "GLOBAL_HTTP_ATTEMPT_LIMIT_EXCEEDED" in err_str or "MAX_CANDLE_ATTEMPTS_EXCEEDED" in err_str:
                        halted_reason = err_str

            statuses_seen = {r["status"] for r in probe_results}
            if statuses_seen == {ProbeClassification.PASS}:
                overall_status = ProbeClassification.PASS
            elif ProbeClassification.FAIL in statuses_seen and ProbeClassification.PASS not in statuses_seen:
                overall_status = ProbeClassification.FAIL
            else:
                overall_status = ProbeClassification.INCONCLUSIVE

    report: Dict[str, Any] = {
        "execution_mode": "LIVE_READ_ONLY" if execute_live and cred_res["all_valid"] else "OFFLINE_PREFLIGHT",
        "overall_status": overall_status,
        "logical_candle_probes_planned": LOGICAL_CANDLE_PROBES_PLANNED,
        "artifact_readiness": readiness,
        "credential_statuses": cred_statuses,
        "credential_sources": cred_sources,
        "http_budget": {
            "auth_http_attempts": budget_guard.auth_http_attempts,
            "max_auth_attempts": budget_guard.max_auth_attempts,
            "candle_http_attempts": budget_guard.candle_http_attempts,
            "max_candle_attempts": budget_guard.max_candle_attempts,
            "total_http_attempts": budget_guard.total_http_attempts,
            "max_total_http_attempts": budget_guard.max_total_http_attempts,
            "allowed_endpoints": sorted(ALLOWED_ANGEL_ONE_URLS),
        },
        "probes": probe_results,
    }

    # Recursive string sanitization skipping structured credential status keys
    reserved_tokens = {
        "PASS", "FAIL", "INCONCLUSIVE", "BLOCKED_MISSING_CREDENTIALS",
        "NIFTY", "BANKNIFTY", "RELIANCE", "NSE", "ONE_MINUTE", "FIVE_MINUTE",
        "DOTENV", "EXPLICIT", "PROCESS_ENV", "UNSET", "MISSING", "PLACEHOLDER",
        "INVALID_FORMAT", "PRESENT_VALID_FORMAT", "LIVE_READ_ONLY", "OFFLINE_PREFLIGHT",
        "TRUE", "FALSE", "NULL",
    }
    filtered_secrets = [
        s for s in secret_values
        if s and str(s).strip().upper() not in reserved_tokens
    ]

    def _sanitize_node(node: Any, key_name: Optional[str] = None) -> Any:
        if key_name in ("credential_statuses", "credential_sources", "http_budget", "artifact_readiness"):
            return node
        if isinstance(node, dict):
            return {k: _sanitize_node(v, k) for k, v in node.items()}
        if isinstance(node, list):
            return [_sanitize_node(item, key_name) for item in node]
        if isinstance(node, str):
            return redact_sensitive_text(node, extra_secrets=filtered_secrets)
        return node

    sanitized_report: Dict[str, Any] = _sanitize_node(report)

    if validated_output is not None:
        validated_output.parent.mkdir(parents=True, exist_ok=True)
        validated_output.write_text(json.dumps(sanitized_report, indent=2), encoding="utf-8")

    return sanitized_report


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only Angel One SmartAPI post-15:15 historical candle diagnostic probe."
    )
    parser.add_argument(
        "--reference-date",
        type=str,
        default=DEFAULT_REFERENCE_TRADING_DATE.isoformat(),
        help="Valid NSE reference trading date in YYYY-MM-DD (default: 2026-11-06).",
    )
    parser.add_argument(
        "--output-report",
        type=str,
        default=None,
        help="Optional safe JSON report output path (must not target data/historical or data/checkpoints).",
    )
    parser.add_argument(
        "--execute-live-probes",
        action="store_true",
        help="Explicit authorization flag to perform bounded login + 6 getCandleData HTTP probes.",
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    try:
        ref_date = datetime.strptime(args.reference_date, "%Y-%m-%d").date()
        report = run_post_1515_probe_audit(
            execute_live=bool(args.execute_live_probes),
            reference_date=ref_date,
            output_report_path=args.output_report,
        )
    except Exception as exc:
        print(redact_sensitive_text(f"[!] Probe preflight error: {exc}"), file=sys.stderr)
        return 1

    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
