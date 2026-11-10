"""
TradeLens AI — Phase 3E.5.1.1 Read-Only Angel One Historical Provider Probe Audit.

Purpose:
  Performs strictly bounded, read-only diagnostics on Angel One SmartAPI's
  `/rest/secure/angelbroking/historical/v1/getCandleData` endpoint to determine
  whether continuous 1m/5m historical data exists after 15:15 IST before planning
  any 6- or 12-month historical acquisition.

Safety & Read-Only Guarantees:
  - Strictly read-only: NEVER writes under `data/historical/` or `data/checkpoints/`.
  - Output-path guard resolves relative paths and symlinks via `os.path.realpath`.
  - Never modifies manifests, never invokes acquisition persistence, replay,
    outcome tracking, or order/funds/holdings/positions endpoints.
  - Logical plan is strictly 1 auth op + 6 single-session `getCandleData` probes.
  - Actual HTTP attempts are hard-capped:
      MAX_AUTH_HTTP_ATTEMPTS = 2 (1 initial login + at most 1 refresh)
      MAX_CANDLE_HTTP_ATTEMPTS_PER_PROBE = 3
      MAX_TOTAL_HTTP_ATTEMPTS = 20
  - Rejects missing or placeholder credentials with `BLOCKED_MISSING_CREDENTIALS`.
  - Recursively redacts secrets, JWTs, TOTP codes, and Authorization headers
    from logs, exceptions, nested response objects, and JSON reports.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, date, time as dt_time, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

from backend.app.data.angel_one import AngelOneSmartAPIProvider, generate_totp_code
from backend.app.data.fno_tokens import FNO_TOKEN_MAP
from backend.app.data.historical_acquisition import (
    AngelOneHistoricalSpotProvider,
    HistoricalCoverageValidator,
)
from backend.app.data.session_clock import IST_ZONE, IndianMarketSessionClock


PROBE_ENDPOINT_PATH = "/rest/secure/angelbroking/historical/v1/getCandleData"
LOGIN_ENDPOINT_PATH = "/rest/auth/angelbroking/user/v1/loginByPassword"
BASE_URL = AngelOneHistoricalSpotProvider.BASE_URL

# Hard upper bounds on HTTP execution (preventing nested or auth-refresh retry loops)
LOGICAL_AUTH_OPERATIONS_PLANNED = 1
LOGICAL_CANDLE_PROBES_PLANNED = 6
MAX_AUTH_HTTP_ATTEMPTS = 2
MAX_CANDLE_HTTP_ATTEMPTS_PER_PROBE = 3
MAX_TOTAL_CANDLE_HTTP_ATTEMPTS = LOGICAL_CANDLE_PROBES_PLANNED * MAX_CANDLE_HTTP_ATTEMPTS_PER_PROBE
MAX_TOTAL_HTTP_ATTEMPTS = MAX_AUTH_HTTP_ATTEMPTS + MAX_TOTAL_CANDLE_HTTP_ATTEMPTS  # 20

PLACEHOLDER_VALUES = frozenset({
    "",
    "your_api_key_here",
    "your_client_code_here",
    "your_password_or_mpin_here",
    "your_totp_secret_here",
    "placeholder",
    "changeme",
    "none",
    "null",
})

SENSITIVE_KEYS = frozenset({
    "authorization",
    "x-privatekey",
    "api_key",
    "apikey",
    "angel_api_key",
    "client_code",
    "clientcode",
    "angel_client_code",
    "password",
    "angel_password",
    "totp",
    "totp_secret",
    "angel_totp_secret",
    "jwt",
    "jwttoken",
    "jwt_token",
    "feedtoken",
    "feed_token",
    "refreshtoken",
    "refresh_token",
})

# Canonical instrument aliases resolved strictly against FNO_TOKEN_MAP
INSTRUMENT_ALIASES = {
    "NIFTY": "NIFTY 50",
    "NIFTY 50": "NIFTY 50",
    "^NSEI": "NIFTY 50",
    "BANKNIFTY": "BANKNIFTY",
    "RELIANCE": "RELIANCE",
}


class VerificationStatus:
    PASS = "PASS"
    FAIL = "FAIL"
    INCONCLUSIVE = "INCONCLUSIVE"
    BLOCKED_MISSING_CREDENTIALS = "BLOCKED_MISSING_CREDENTIALS"


@dataclass(frozen=True)
class ProbeSpec:
    probe_id: str
    instrument: str
    exchange: str
    interval: str
    interval_minutes: int
    reference_date: date
    session_date: date
    start_time: dt_time
    end_time: dt_time
    probe_purpose: str = "CONTINUOUS_SESSION_CHECK"


def resolve_verified_token(instrument: str) -> str:
    """Resolves instrument token strictly from the verified FNO_TOKEN_MAP master."""
    key = INSTRUMENT_ALIASES.get(instrument.strip().upper(), instrument.strip().upper())
    entry = FNO_TOKEN_MAP.get(key)
    if not entry or not entry.get("token"):
        raise ValueError(f"Unsupported or unverified instrument symbol: {instrument!r}")
    return str(entry["token"])


def resolve_verified_interval(interval: str) -> Tuple[str, int]:
    """Resolves Angel One interval string strictly via AngelOneSmartAPIProvider.INTERVAL_MAP."""
    api_interval = AngelOneSmartAPIProvider.INTERVAL_MAP.get(interval)
    if not api_interval:
        raise ValueError(f"Unsupported historical interval: {interval!r}")
    minutes_map = {
        "ONE_MINUTE": 1,
        "THREE_MINUTE": 3,
        "FIVE_MINUTE": 5,
        "TEN_MINUTE": 10,
        "FIFTEEN_MINUTE": 15,
        "THIRTY_MINUTE": 30,
        "ONE_HOUR": 60,
        "ONE_DAY": 375,
    }
    return api_interval, minutes_map[api_interval]


def is_placeholder_or_missing(value: Optional[str]) -> bool:
    """Returns True if credential value is empty, whitespace, or a known placeholder."""
    if value is None:
        return True
    cleaned = str(value).strip()
    if not cleaned:
        return True
    lowered = cleaned.lower()
    if lowered in PLACEHOLDER_VALUES or (lowered.startswith("your_") and lowered.endswith("_here")):
        return True
    return False


def redact_secrets(text: str, secrets: Iterable[Optional[str]]) -> str:
    """Scrubs known secret strings, Bearer headers, and JWT-shaped strings from text."""
    sanitized = str(text)
    for secret in secrets:
        if secret and len(str(secret).strip()) >= 3:
            sanitized = sanitized.replace(str(secret).strip(), "[REDACTED]")
    sanitized = re.sub(r"Bearer\s+[A-Za-z0-9\-_\.]+", "Bearer [REDACTED]", sanitized, flags=re.IGNORECASE)
    sanitized = re.sub(r"eyJ[A-Za-z0-9\-_]+\.[A-Za-z0-9\-_]+\.[A-Za-z0-9\-_]+", "[REDACTED_JWT]", sanitized)
    return sanitized


def sanitize_nested_obj(obj: Any, secrets: Iterable[Optional[str]]) -> Any:
    """Recursively scrubs secrets, sensitive keys, and JWT tokens from nested report/response objects."""
    secret_list = [s for s in secrets if s]
    if isinstance(obj, dict):
        cleaned: Dict[Any, Any] = {}
        for k, v in obj.items():
            if str(k).strip().lower() in SENSITIVE_KEYS:
                cleaned[k] = "[REDACTED]"
            else:
                cleaned[k] = sanitize_nested_obj(v, secret_list)
        return cleaned
    if isinstance(obj, list):
        return [sanitize_nested_obj(item, secret_list) for item in obj]
    if isinstance(obj, tuple):
        return tuple(sanitize_nested_obj(item, secret_list) for item in obj)
    if isinstance(obj, str):
        return redact_secrets(obj, secret_list)
    return obj


def load_credentials(
    env_override: Optional[Dict[str, str]] = None,
) -> Tuple[Dict[str, str], List[str]]:
    """
    Loads Angel One credentials from environment / `.env` via existing provider convention
    and rejects missing or placeholder values.
    """
    if env_override is not None:
        creds = {
            "ANGEL_API_KEY": env_override.get("ANGEL_API_KEY", ""),
            "ANGEL_CLIENT_CODE": env_override.get("ANGEL_CLIENT_CODE", ""),
            "ANGEL_PASSWORD": env_override.get("ANGEL_PASSWORD", ""),
            "ANGEL_TOTP_SECRET": env_override.get("ANGEL_TOTP_SECRET", ""),
        }
    else:
        helper = AngelOneHistoricalSpotProvider(max_chunk_days=1)
        helper._load_env_if_needed()
        creds = {
            "ANGEL_API_KEY": helper.api_key or "",
            "ANGEL_CLIENT_CODE": helper.client_code or "",
            "ANGEL_PASSWORD": helper.password or "",
            "ANGEL_TOTP_SECRET": helper.totp_secret or "",
        }

    missing = [k for k, v in creds.items() if is_placeholder_or_missing(v)]
    return creds, missing


def _parse_timestamp_ist(raw_ts: Any) -> datetime:
    """Parses ISO or Angel One space-delimited timestamps and converts strictly to IST."""
    ts_str = str(raw_ts).strip()
    if not ts_str:
        raise ValueError("Empty candle timestamp")
    if "T" in ts_str:
        dt_val = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    else:
        dt_val = datetime.strptime(ts_str[:16], "%Y-%m-%d %H:%M")
    return IndianMarketSessionClock.to_ist(dt_val)


def _is_valid_trading_session(d: date, clock: Optional[IndianMarketSessionClock] = None) -> bool:
    """Checks calendar validity for single-session historical probes."""
    session_clock = clock or IndianMarketSessionClock()
    d_str = d.strftime("%Y-%m-%d")
    if d_str in session_clock.exceptional_sessions:
        return any(s.get("is_market_open", True) for s in session_clock.exceptional_sessions[d_str])
    return d.weekday() < 5 and not session_clock.is_holiday(d)


def _last_trading_weekday(reference_date: date, offset_days: int = 0) -> date:
    """Returns `reference_date - offset_days` adjusted backward to Mon-Fri non-holiday."""
    clock = IndianMarketSessionClock()
    d = reference_date - timedelta(days=offset_days)
    while not _is_valid_trading_session(d, clock):
        d -= timedelta(days=1)
    return d


def build_probe_plan(probe_date: date) -> List[ProbeSpec]:
    """
    Builds the exact 6 single-session logical probes specified in Phase 3E.5.1.1:
      1. nifty_1m_1530 (NIFTY 1m, todate 15:30)
      2. nifty_1m_1600 (NIFTY 1m, todate 16:00 — request-boundary experiment)
      3. nifty_5m_1530 (NIFTY 5m, todate 15:30)
      4. banknifty_1m_1530 (BANKNIFTY 1m, todate 15:30)
      5. reliance_1m_1530 (RELIANCE 1m, todate 15:30)
      6. nifty_1m_180d_prior_trading_session (NIFTY 1m, prior valid session ~180d earlier)
    """
    date_180d = _last_trading_weekday(probe_date, offset_days=180)
    return [
        ProbeSpec(
            "nifty_1m_1530", "NIFTY", "NSE", "ONE_MINUTE", 1,
            probe_date, probe_date, dt_time(9, 15), dt_time(15, 30),
            probe_purpose="CONTINUOUS_SESSION_CHECK",
        ),
        ProbeSpec(
            "nifty_1m_1600", "NIFTY", "NSE", "ONE_MINUTE", 1,
            probe_date, probe_date, dt_time(9, 15), dt_time(16, 0),
            probe_purpose="REQUEST_BOUNDARY_EXPERIMENT",
        ),
        ProbeSpec(
            "nifty_5m_1530", "NIFTY", "NSE", "FIVE_MINUTE", 5,
            probe_date, probe_date, dt_time(9, 15), dt_time(15, 30),
            probe_purpose="CONTINUOUS_SESSION_CHECK",
        ),
        ProbeSpec(
            "banknifty_1m_1530", "BANKNIFTY", "NSE", "ONE_MINUTE", 1,
            probe_date, probe_date, dt_time(9, 15), dt_time(15, 30),
            probe_purpose="CONTINUOUS_SESSION_CHECK",
        ),
        ProbeSpec(
            "reliance_1m_1530", "RELIANCE", "NSE", "ONE_MINUTE", 1,
            probe_date, probe_date, dt_time(9, 15), dt_time(15, 30),
            probe_purpose="CONTINUOUS_SESSION_CHECK",
        ),
        ProbeSpec(
            "nifty_1m_180d_prior_trading_session", "NIFTY", "NSE", "ONE_MINUTE", 1,
            probe_date, date_180d, dt_time(9, 15), dt_time(15, 30),
            probe_purpose="HISTORICAL_RETENTION_CHECK",
        ),
    ]


def build_candle_request_payload(spec: ProbeSpec) -> Dict[str, Any]:
    """Constructs the exact validated Angel One `getCandleData` JSON request payload."""
    token = resolve_verified_token(spec.instrument)
    api_interval, _ = resolve_verified_interval(spec.interval)
    from_dt = datetime.combine(spec.session_date, spec.start_time, tzinfo=IST_ZONE)
    to_dt = datetime.combine(spec.session_date, spec.end_time, tzinfo=IST_ZONE)
    return {
        "exchange": spec.exchange,
        "symboltoken": token,
        "interval": api_interval,
        "fromdate": from_dt.strftime("%Y-%m-%d %H:%M"),
        "todate": to_dt.strftime("%Y-%m-%d %H:%M"),
    }


def analyze_probe_rows(
    rows: List[List[Any]],
    session_date: date,
    interval_minutes: int,
    clock: Optional[IndianMarketSessionClock] = None,
) -> Dict[str, Any]:
    """
    Analyzes raw `[ts, O, H, L, C, V]` rows for a single session in IST.
    Strictly separates:
      - Normal NSE continuous session window (09:15-15:30 IST)
      - Observations before 15:15 vs between 15:15 and 15:30
      - Observations at/after 15:30 (never treated as normal continuous-session bars)
      - Isolated post-gap flat observations (without inferring settlement/auction semantics)
    """
    session_clock = clock or IndianMarketSessionClock()
    validator = HistoricalCoverageValidator(session_clock)
    expected_map = validator.generate_expected_session_intervals(
        session_date, session_date, interval_minutes=interval_minutes
    )
    expected_keys = sorted(expected_map.keys())

    parsed_rows: List[Tuple[datetime, float, float, float, float, float]] = []
    parse_errors: List[str] = []
    for idx, row in enumerate(rows):
        if not isinstance(row, (list, tuple)) or len(row) < 5:
            parse_errors.append(f"Row {idx}: incomplete OHLC row")
            continue
        try:
            dt_ist = _parse_timestamp_ist(row[0])
            o_val, h_val, l_val, c_val = float(row[1]), float(row[2]), float(row[3]), float(row[4])
            v_val = float(row[5]) if len(row) > 5 and row[5] is not None else 0.0
            parsed_rows.append((dt_ist, o_val, h_val, l_val, c_val, v_val))
        except Exception as exc:
            parse_errors.append(f"Row {idx}: unparseable row ({exc})")

    parsed_rows.sort(key=lambda item: item[0])

    observed_keys = {item[0].isoformat() for item in parsed_rows}
    missing_intervals = [ts for ts in expected_keys if ts not in observed_keys]

    candles_before_1515 = sum(1 for item in parsed_rows if item[0].time() < dt_time(15, 15))
    candles_1515_to_1530 = sum(
        1 for item in parsed_rows if dt_time(15, 15) <= item[0].time() < dt_time(15, 30)
    )
    post_1530_observations = [
        {
            "timestamp": item[0].isoformat(),
            "open": item[1],
            "high": item[2],
            "low": item[3],
            "close": item[4],
            "volume": item[5],
            "is_flat": (item[1] == item[2] == item[3] == item[4]),
            "classification": "POST_1530_OBSERVATION_UNVERIFIED",
        }
        for item in parsed_rows
        if item[0].time() >= dt_time(15, 30)
    ]

    intraday_gaps: List[Dict[str, Any]] = []
    flat_post_gap_observations: List[Dict[str, Any]] = []
    for i in range(1, len(parsed_rows)):
        prev_dt = parsed_rows[i - 1][0]
        curr_dt, o_v, h_v, l_v, c_v, _ = parsed_rows[i]
        gap_sec = (curr_dt - prev_dt).total_seconds()
        if int(gap_sec // 60) != interval_minutes:
            intraday_gaps.append({
                "from": prev_dt.isoformat(),
                "to": curr_dt.isoformat(),
                "gap_seconds": gap_sec,
            })
        if gap_sec >= 300 and (o_v == h_v == l_v == c_v):
            flat_post_gap_observations.append({
                "timestamp": curr_dt.isoformat(),
                "flat_price": c_v,
                "gap_seconds_from_prev": gap_sec,
                "semantics": "UNVERIFIED_FLAT_POST_GAP_OBSERVATION",
            })

    # Continuity requires full expected 09:15-15:30 grid coverage with zero flat post-gap bars
    # (post-15:30 observations never count toward normal continuous-session continuity).
    session_intraday_gaps = [
        g for g in intraday_gaps if _parse_timestamp_ist(g["to"]).time() < dt_time(15, 30)
    ]
    last_expected_bar = expected_keys[-1] if expected_keys else None
    continuous_to_close = (
        len(expected_keys) > 0
        and len(missing_intervals) == 0
        and len(session_intraday_gaps) == 0
        and len([
            f for f in flat_post_gap_observations
            if _parse_timestamp_ist(f["timestamp"]).time() < dt_time(15, 30)
        ]) == 0
        and last_expected_bar in observed_keys
    )

    return {
        "expected_session_intervals": len(expected_keys),
        "received_candle_count": len(parsed_rows),
        "first_timestamp": parsed_rows[0][0].isoformat() if parsed_rows else None,
        "last_timestamp": parsed_rows[-1][0].isoformat() if parsed_rows else None,
        "candles_before_1515": candles_before_1515,
        "candles_between_1515_and_1530": candles_1515_to_1530,
        "candles_after_1530": len(post_1530_observations),
        "post_1530_observations": post_1530_observations,
        "missing_intervals": missing_intervals,
        "missing_intervals_count": len(missing_intervals),
        "intraday_gaps": intraday_gaps,
        "flat_post_gap_observations": flat_post_gap_observations,
        "continuous_to_session_close": continuous_to_close,
        "parse_errors": parse_errors,
    }


def classify_probe_result(
    spec: ProbeSpec,
    payload: Dict[str, Any],
    api_status: Any,
    rows: List[List[Any]],
    response_errors: List[str],
    raw_sha256: Optional[str] = None,
    actual_http_attempts: int = 1,
) -> Dict[str, Any]:
    """
    Produces the standardized Phase 3E.5.1.1 probe result record and assigns
    explicit verification status (`PASS`, `FAIL`, `INCONCLUSIVE`, or
    `BLOCKED_MISSING_CREDENTIALS`).
    """
    clock = IndianMarketSessionClock()
    analysis = analyze_probe_rows(rows, spec.session_date, spec.interval_minutes, clock=clock)
    all_errors = list(response_errors) + list(analysis["parse_errors"])
    cal_diff = (spec.reference_date - spec.session_date).days
    is_valid_session = _is_valid_trading_session(spec.session_date, clock)

    if api_status is not True or len(response_errors) > 0:
        status = VerificationStatus.INCONCLUSIVE
        interpretation = (
            "API request failed or returned non-true status; empty/partial payload cannot be "
            "interpreted as historical data unavailability."
        )
    elif analysis["received_candle_count"] == 0:
        status = VerificationStatus.INCONCLUSIVE
        interpretation = (
            "API returned status=True with 0 candles. Could reflect holiday, non-trading session, "
            "lookback limit, or feed unavailability; do not infer continuity without session check."
        )
    elif analysis["continuous_to_session_close"] and analysis["candles_between_1515_and_1530"] > 0:
        status = VerificationStatus.PASS
        interpretation = (
            f"Continuous {spec.interval} bars confirmed through normal 09:15-15:30 session close "
            f"({analysis['candles_between_1515_and_1530']} bars in 15:15-15:30, 0 missing intervals)."
        )
    elif analysis["candles_before_1515"] > 0 and analysis["missing_intervals_count"] > 0:
        status = VerificationStatus.FAIL
        interpretation = (
            f"Pre-15:15 feed is continuous ({analysis['candles_before_1515']} bars), "
            f"but {analysis['missing_intervals_count']} normal-session (09:15-15:30) intervals are missing "
            f"({analysis['candles_between_1515_and_1530']} bars in 15:15-15:30; "
            f"{len(analysis['flat_post_gap_observations'])} flat post-gap observation(s))."
        )
    else:
        status = VerificationStatus.INCONCLUSIVE
        interpretation = (
            "Partial or unexpected timestamp distribution received; inspect missing_intervals "
            "and intraday_gaps before drawing conclusions."
        )

    if spec.probe_purpose == "REQUEST_BOUNDARY_EXPERIMENT":
        interpretation += (
            f" Request-boundary experiment (requested todate {spec.end_time.strftime('%H:%M')} vs "
            f"normal 15:30 NSE close): {analysis['candles_after_1530']} post-15:30 observation(s) "
            f"recorded separately and excluded from normal continuous-session counts."
        )

    return {
        "probe_id": spec.probe_id,
        "probe_purpose": spec.probe_purpose,
        "instrument": spec.instrument,
        "instrument_token": payload["symboltoken"],
        "exchange": payload["exchange"],
        "interval": payload["interval"],
        "reference_date": spec.reference_date.isoformat(),
        "actual_probe_date": spec.session_date.isoformat(),
        "calendar_day_difference": cal_diff,
        "is_valid_trading_session": is_valid_session,
        "normal_nse_continuous_session": "09:15:00-15:30:00 IST",
        "requested_api_boundary": f"{spec.end_time.strftime('%H:%M:%S')} IST",
        "requested_start": payload["fromdate"],
        "requested_end": payload["todate"],
        "actual_http_attempts": actual_http_attempts,
        "retry_count": max(0, actual_http_attempts - 1),
        "api_response_status": api_status,
        "received_candle_count": analysis["received_candle_count"],
        "first_timestamp": analysis["first_timestamp"],
        "last_timestamp": analysis["last_timestamp"],
        "candles_before_1515": analysis["candles_before_1515"],
        "candles_between_1515_and_1530": analysis["candles_between_1515_and_1530"],
        "candles_after_1530": analysis["candles_after_1530"],
        "post_1530_observations": analysis["post_1530_observations"],
        "missing_intervals": analysis["missing_intervals"],
        "missing_intervals_count": analysis["missing_intervals_count"],
        "flat_post_gap_observations": analysis["flat_post_gap_observations"],
        "response_errors": all_errors,
        "raw_response_sha256": raw_sha256,
        "interpretation": interpretation,
        "verification_status": status,
    }


def _authenticate_session(creds: Dict[str, str], timeout_sec: float = 10.0) -> Tuple[Optional[str], Optional[str]]:
    """
    Authenticates via Angel One loginByPassword without fetching quotes or storing tokens on disk.
    Returns (jwt_token, sanitized_error).
    """
    secrets = list(creds.values())
    try:
        totp_code = generate_totp_code(creds["ANGEL_TOTP_SECRET"])
        secrets.append(totp_code)
        url = f"{BASE_URL}{LOGIN_ENDPOINT_PATH}"
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-UserType": "USER",
            "X-SourceID": "WEB",
            "X-ClientLocalIP": "127.0.0.1",
            "X-ClientPublicIP": "127.0.0.1",
            "X-MACAddress": "fe80::1",
            "X-PrivateKey": creds["ANGEL_API_KEY"],
        }
        body = json.dumps({
            "clientcode": creds["ANGEL_CLIENT_CODE"],
            "password": creds["ANGEL_PASSWORD"],
            "totp": totp_code,
        }).encode("utf-8")

        req = urllib.request.Request(url, data=body, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            res_data = json.loads(resp.read().decode("utf-8"))

        if res_data.get("status") and isinstance(res_data.get("data"), dict):
            jwt = res_data["data"].get("jwtToken")
            if jwt:
                return jwt, None
        sanitized_res = sanitize_nested_obj(res_data, secrets)
        msg = str(sanitized_res.get("message") or "Authentication rejected")
        code = str(sanitized_res.get("errorcode") or "")
        return None, f"Auth failed ({code}): {msg}".strip()
    except Exception as exc:
        return None, redact_secrets(f"Auth exception: {exc}", secrets)


def _execute_single_probe_http(
    payload: Dict[str, Any],
    creds: Dict[str, str],
    jwt_state: Dict[str, Any],
    timeout_sec: float = 12.0,
    rate_limit_pause_sec: float = 0.5,
    max_attempts: int = MAX_CANDLE_HTTP_ATTEMPTS_PER_PROBE,
) -> Tuple[Any, List[List[Any]], List[str], Optional[str], int]:
    """
    Executes a single bounded read-only `getCandleData` POST request with strict
    non-nested retry accounting. Auth refresh consumes both a probe attempt and
    from `jwt_state["remaining_auth_refreshes"]` (max 1 refresh across whole run),
    guaranteeing total attempts cannot exceed configured bounds.
    Returns (api_status, rows, sanitized_errors, raw_sha256, actual_http_attempts).
    """
    url = f"{BASE_URL}{PROBE_ENDPOINT_PATH}"
    bounded_attempts = min(max_attempts, MAX_CANDLE_HTTP_ATTEMPTS_PER_PROBE)
    encoded = json.dumps(payload).encode("utf-8")
    errors: List[str] = []
    attempts_used = 0

    for attempt in range(bounded_attempts):
        if jwt_state.get("total_http_attempts", 0) >= MAX_TOTAL_HTTP_ATTEMPTS:
            errors.append("Global maximum HTTP attempt safeguard reached; aborting remaining retries.")
            return "MAX_ATTEMPTS_SAFEGUARD", [], errors, None, attempts_used

        current_jwt = jwt_state["jwt_token"]
        secrets = list(creds.values()) + [current_jwt]
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-UserType": "USER",
            "X-SourceID": "WEB",
            "X-ClientLocalIP": "127.0.0.1",
            "X-ClientPublicIP": "127.0.0.1",
            "X-MACAddress": "fe80::1",
            "X-PrivateKey": creds["ANGEL_API_KEY"],
            "Authorization": f"Bearer {current_jwt}",
        }

        attempts_used += 1
        jwt_state["total_http_attempts"] = jwt_state.get("total_http_attempts", 0) + 1

        try:
            time.sleep(rate_limit_pause_sec)
            req = urllib.request.Request(url, data=encoded, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
                raw_bytes = resp.read()
            raw_sha = hashlib.sha256(raw_bytes).hexdigest()
            res = json.loads(raw_bytes.decode("utf-8"))

            api_status = res.get("status")
            err_code = str(res.get("errorcode") or "")
            sanitized_res = sanitize_nested_obj(res, secrets)
            err_msg = str(sanitized_res.get("message") or "")

            if err_code in ("AG8001", "AG8002"):
                errors.append(f"Attempt {attempt + 1}: Token expired ({err_code})")
                if (
                    jwt_state.get("remaining_auth_refreshes", 0) > 0
                    and attempt + 1 < bounded_attempts
                ):
                    jwt_state["remaining_auth_refreshes"] -= 1
                    jwt_state["auth_attempts"] = jwt_state.get("auth_attempts", 0) + 1
                    jwt_state["total_http_attempts"] += 1
                    new_jwt, auth_err = _authenticate_session(creds)
                    if new_jwt:
                        jwt_state["jwt_token"] = new_jwt
                        continue
                    errors.append(f"Auth refresh failed: {auth_err}")
                return False, [], errors, raw_sha, attempts_used

            if err_code == "AB1004":
                errors.append(f"Attempt {attempt + 1}: Rate limit AB1004 ({err_msg})")
                if attempt + 1 < bounded_attempts:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                return False, [], errors, raw_sha, attempts_used

            if api_status is True:
                rows = res.get("data") or []
                return True, rows, [], raw_sha, attempts_used

            errors.append(f"API errorcode={err_code or 'NONE'}: {err_msg or 'status=False'}")
            return api_status, [], errors, raw_sha, attempts_used
        except urllib.error.HTTPError as he:
            http_code = he.code
            safe_msg = redact_secrets(f"HTTP {http_code}: {he.reason}", secrets)
            try:
                he.close()
            except Exception:
                pass
            errors.append(f"Attempt {attempt + 1}: {safe_msg}")
            if http_code in (401, 403):
                if (
                    jwt_state.get("remaining_auth_refreshes", 0) > 0
                    and attempt + 1 < bounded_attempts
                ):
                    jwt_state["remaining_auth_refreshes"] -= 1
                    jwt_state["auth_attempts"] = jwt_state.get("auth_attempts", 0) + 1
                    jwt_state["total_http_attempts"] += 1
                    new_jwt, auth_err = _authenticate_session(creds)
                    if new_jwt:
                        jwt_state["jwt_token"] = new_jwt
                        continue
                    errors.append(f"Auth refresh failed: {auth_err}")
                return f"HTTP_{http_code}", [], errors, None, attempts_used
            if http_code == 429 and attempt + 1 < bounded_attempts:
                time.sleep(1.5 * (attempt + 1))
                continue
            return f"HTTP_{http_code}", [], errors, None, attempts_used
        except Exception as exc:
            safe_msg = redact_secrets(f"Exception: {exc}", secrets)
            errors.append(f"Attempt {attempt + 1}: {safe_msg}")
            if attempt + 1 < bounded_attempts:
                time.sleep(1.0 * (attempt + 1))
                continue
            return "EXCEPTION", [], errors, None, attempts_used

    return "EXHAUSTED_RETRIES", [], errors, None, attempts_used


def validate_safe_report_path(report_path: str) -> str:
    """
    Validates that `report_path` resolves strictly outside `data/historical` and
    `data/checkpoints`, handling relative paths, `..` segments, and symlinks.
    """
    real_target = os.path.realpath(report_path)
    real_parent = os.path.realpath(os.path.dirname(os.path.abspath(report_path)))
    resolved_leaf = os.path.join(real_parent, os.path.basename(real_target))

    forbidden_roots = [
        os.path.realpath("data/historical"),
        os.path.realpath("data/checkpoints"),
    ]
    for root in forbidden_roots:
        for candidate in (real_target, real_parent, resolved_leaf):
            if candidate == root or candidate.startswith(root + os.sep):
                raise ValueError(
                    f"Forbidden report path {report_path!r}: resolves inside protected directory {root!r}."
                )
    return real_target


def run_audit(
    probe_date: date,
    checkpoint_raw_path: str = "data/checkpoints/raw_nifty_2026-11-02_2026-11-06_chunk_0.json",
    offline_only: bool = False,
    env_override: Optional[Dict[str, str]] = None,
    report_output_path: Optional[str] = None,
) -> Tuple[Dict[str, Any], int]:
    """Executes the read-only offline checkpoint audit and (when authorized) the 6-probe live audit."""
    clock = IndianMarketSessionClock()
    plan = build_probe_plan(probe_date)
    report: Dict[str, Any] = {
        "audit_type": "PHASE_3E_5_1_1_READ_ONLY_PROVIDER_PROBE_AUDIT",
        "read_only": True,
        "logical_request_plan": {
            "authentication_operations_planned": LOGICAL_AUTH_OPERATIONS_PLANNED,
            "candle_probes_planned": len(plan),
        },
        "safeguard_bounds": {
            "max_auth_http_attempts": MAX_AUTH_HTTP_ATTEMPTS,
            "max_candle_http_attempts_per_probe": MAX_CANDLE_HTTP_ATTEMPTS_PER_PROBE,
            "max_total_candle_http_attempts": MAX_TOTAL_CANDLE_HTTP_ATTEMPTS,
            "max_total_http_attempts": MAX_TOTAL_HTTP_ATTEMPTS,
            "retries_bounded": True,
        },
        "probe_plan": [
            {
                "probe_id": spec.probe_id,
                "probe_purpose": spec.probe_purpose,
                "reference_date": spec.reference_date.isoformat(),
                "actual_probe_date": spec.session_date.isoformat(),
                "calendar_day_difference": (spec.reference_date - spec.session_date).days,
                "is_valid_trading_session": _is_valid_trading_session(spec.session_date, clock),
                **build_candle_request_payload(spec),
            }
            for spec in plan
        ],
        "offline_checkpoint_forensics": None,
        "live_api_audit": None,
    }

    if os.path.exists(checkpoint_raw_path):
        with open(checkpoint_raw_path, "r", encoding="utf-8") as f:
            raw_rows = json.load(f)
        by_day: Dict[str, List[List[Any]]] = {}
        for r in raw_rows:
            if r:
                by_day.setdefault(str(r[0])[:10], []).append(r)
        session_analyses = {
            d_str: analyze_probe_rows(d_rows, datetime.strptime(d_str, "%Y-%m-%d").date(), interval_minutes=1, clock=clock)
            for d_str, d_rows in sorted(by_day.items())
        }
        report["offline_checkpoint_forensics"] = {
            "source_file": checkpoint_raw_path,
            "total_rows": len(raw_rows),
            "sessions": session_analyses,
        }

    exit_code = 0
    secrets_to_scrub: List[str] = []

    if offline_only:
        report["live_api_audit"] = {
            "status": "SKIPPED_OFFLINE_ONLY",
            "request_accounting": {
                "logical_request_plan": report["logical_request_plan"],
                "actual_request_execution": {
                    "authentication_attempts": 0,
                    "total_candle_http_attempts": 0,
                    "total_actual_http_attempts": 0,
                    "unplanned_candle_probes_executed": 0,
                    "per_probe": [],
                },
            },
            "probes": [],
        }
    else:
        creds, missing = load_credentials(env_override=env_override)
        secrets_to_scrub = [v for v in creds.values() if not is_placeholder_or_missing(v)]

        if missing:
            blocked_probes = []
            per_probe_acct = []
            for spec in plan:
                payload = build_candle_request_payload(spec)
                blocked_probes.append({
                    "probe_id": spec.probe_id,
                    "probe_purpose": spec.probe_purpose,
                    "instrument": spec.instrument,
                    "instrument_token": payload["symboltoken"],
                    "exchange": payload["exchange"],
                    "interval": payload["interval"],
                    "reference_date": spec.reference_date.isoformat(),
                    "actual_probe_date": spec.session_date.isoformat(),
                    "calendar_day_difference": (spec.reference_date - spec.session_date).days,
                    "is_valid_trading_session": _is_valid_trading_session(spec.session_date, clock),
                    "normal_nse_continuous_session": "09:15:00-15:30:00 IST",
                    "requested_api_boundary": f"{spec.end_time.strftime('%H:%M:%S')} IST",
                    "requested_start": payload["fromdate"],
                    "requested_end": payload["todate"],
                    "actual_http_attempts": 0,
                    "retry_count": 0,
                    "api_response_status": VerificationStatus.BLOCKED_MISSING_CREDENTIALS,
                    "received_candle_count": 0,
                    "first_timestamp": None,
                    "last_timestamp": None,
                    "candles_before_1515": 0,
                    "candles_between_1515_and_1530": 0,
                    "candles_after_1530": 0,
                    "post_1530_observations": [],
                    "missing_intervals": [],
                    "missing_intervals_count": 0,
                    "flat_post_gap_observations": [],
                    "response_errors": [f"Missing/placeholder env vars: {', '.join(missing)}"],
                    "interpretation": "Cannot execute live probe without valid Angel One credentials.",
                    "verification_status": VerificationStatus.BLOCKED_MISSING_CREDENTIALS,
                })
                per_probe_acct.append({
                    "probe_id": spec.probe_id,
                    "actual_http_attempts": 0,
                    "retry_count": 0,
                    "final_status": VerificationStatus.BLOCKED_MISSING_CREDENTIALS,
                })
            report["live_api_audit"] = {
                "status": VerificationStatus.BLOCKED_MISSING_CREDENTIALS,
                "missing_env_vars": missing,
                "request_accounting": {
                    "logical_request_plan": report["logical_request_plan"],
                    "actual_request_execution": {
                        "authentication_attempts": 0,
                        "total_candle_http_attempts": 0,
                        "total_actual_http_attempts": 0,
                        "unplanned_candle_probes_executed": 0,
                        "per_probe": per_probe_acct,
                    },
                },
                "probes": blocked_probes,
            }
            exit_code = 2
        else:
            jwt_token, auth_err = _authenticate_session(creds)
            if not jwt_token:
                inconclusive_probes = []
                per_probe_acct = []
                for spec in plan:
                    payload = build_candle_request_payload(spec)
                    inconclusive_probes.append({
                        "probe_id": spec.probe_id,
                        "probe_purpose": spec.probe_purpose,
                        "instrument": spec.instrument,
                        "instrument_token": payload["symboltoken"],
                        "exchange": payload["exchange"],
                        "interval": payload["interval"],
                        "reference_date": spec.reference_date.isoformat(),
                        "actual_probe_date": spec.session_date.isoformat(),
                        "calendar_day_difference": (spec.reference_date - spec.session_date).days,
                        "is_valid_trading_session": _is_valid_trading_session(spec.session_date, clock),
                        "normal_nse_continuous_session": "09:15:00-15:30:00 IST",
                        "requested_api_boundary": f"{spec.end_time.strftime('%H:%M:%S')} IST",
                        "requested_start": payload["fromdate"],
                        "requested_end": payload["todate"],
                        "actual_http_attempts": 0,
                        "retry_count": 0,
                        "api_response_status": "AUTH_FAILED",
                        "received_candle_count": 0,
                        "first_timestamp": None,
                        "last_timestamp": None,
                        "candles_before_1515": 0,
                        "candles_between_1515_and_1530": 0,
                        "candles_after_1530": 0,
                        "post_1530_observations": [],
                        "missing_intervals": [],
                        "missing_intervals_count": 0,
                        "flat_post_gap_observations": [],
                        "response_errors": [auth_err or "Authentication failed"],
                        "interpretation": "Authentication failed before historical queries could run.",
                        "verification_status": VerificationStatus.INCONCLUSIVE,
                    })
                    per_probe_acct.append({
                        "probe_id": spec.probe_id,
                        "actual_http_attempts": 0,
                        "retry_count": 0,
                        "final_status": VerificationStatus.INCONCLUSIVE,
                    })
                report["live_api_audit"] = {
                    "status": "AUTH_FAILED",
                    "error": auth_err,
                    "request_accounting": {
                        "logical_request_plan": report["logical_request_plan"],
                        "actual_request_execution": {
                            "authentication_attempts": 1,
                            "total_candle_http_attempts": 0,
                            "total_actual_http_attempts": 1,
                            "unplanned_candle_probes_executed": 0,
                            "per_probe": per_probe_acct,
                        },
                    },
                    "probes": inconclusive_probes,
                }
                exit_code = 3
            else:
                secrets_to_scrub.append(jwt_token)
                jwt_state: Dict[str, Any] = {
                    "jwt_token": jwt_token,
                    "auth_attempts": 1,
                    "remaining_auth_refreshes": MAX_AUTH_HTTP_ATTEMPTS - 1,  # Max 1 refresh
                    "total_http_attempts": 1,
                }
                probe_results: List[Dict[str, Any]] = []
                per_probe_acct = []
                total_candle_attempts = 0

                for spec in plan:
                    payload = build_candle_request_payload(spec)
                    api_status, rows, errs, raw_sha, used_attempts = _execute_single_probe_http(
                        payload, creds, jwt_state
                    )
                    if jwt_state["jwt_token"] not in secrets_to_scrub:
                        secrets_to_scrub.append(jwt_state["jwt_token"])
                    total_candle_attempts += used_attempts

                    classified = classify_probe_result(
                        spec=spec,
                        payload=payload,
                        api_status=api_status,
                        rows=rows,
                        response_errors=errs,
                        raw_sha256=raw_sha,
                        actual_http_attempts=used_attempts,
                    )
                    probe_results.append(classified)
                    per_probe_acct.append({
                        "probe_id": spec.probe_id,
                        "actual_http_attempts": used_attempts,
                        "retry_count": max(0, used_attempts - 1),
                        "final_status": classified["verification_status"],
                    })

                report["live_api_audit"] = {
                    "status": "COMPLETED",
                    "probe_date": probe_date.isoformat(),
                    "probes_executed": len(probe_results),
                    "request_accounting": {
                        "logical_request_plan": report["logical_request_plan"],
                        "actual_request_execution": {
                            "authentication_attempts": jwt_state["auth_attempts"],
                            "total_candle_http_attempts": total_candle_attempts,
                            "total_actual_http_attempts": jwt_state["auth_attempts"] + total_candle_attempts,
                            "unplanned_candle_probes_executed": 0,
                            "per_probe": per_probe_acct,
                        },
                    },
                    "probes": probe_results,
                }

    report = sanitize_nested_obj(report, secrets_to_scrub)

    if report_output_path:
        safe_path = validate_safe_report_path(report_output_path)
        os.makedirs(os.path.dirname(safe_path), exist_ok=True)
        with open(safe_path, "w", encoding="utf-8") as out_f:
            json.dump(report, out_f, indent=2)

    return report, exit_code


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read-only Angel One Historical API audit for post-15:15 continuity."
    )
    parser.add_argument(
        "--checkpoint-raw",
        type=str,
        default="data/checkpoints/raw_nifty_2026-11-02_2026-11-06_chunk_0.json",
        help="Path to existing raw checkpoint JSON for offline forensic inspection.",
    )
    parser.add_argument(
        "--probe-date",
        type=str,
        default="2026-11-06",
        help="Single trading session YYYY-MM-DD for live API probes (if authenticated).",
    )
    parser.add_argument(
        "--offline-only",
        action="store_true",
        help="Skip live API authentication and run offline checkpoint forensics only.",
    )
    parser.add_argument(
        "--report-out",
        type=str,
        default=None,
        help="Optional safe report JSON output path (rejected inside data/historical or data/checkpoints).",
    )
    args = parser.parse_args()

    probe_d = datetime.strptime(args.probe_date, "%Y-%m-%d").date()
    report, exit_code = run_audit(
        probe_date=probe_d,
        checkpoint_raw_path=args.checkpoint_raw,
        offline_only=args.offline_only,
        report_output_path=args.report_out,
    )
    print(json.dumps(report, indent=2))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
