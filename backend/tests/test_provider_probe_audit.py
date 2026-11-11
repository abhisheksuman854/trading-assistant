"""
TradeLens AI — Phase 3E.5.2.1 Offline Test Suite: Isolated Read-Only Probe Audit.

Covers all 22 mandatory offline verification tests (mocked HTTP only; zero live API calls):
1. Missing artifact recovery logic and expected file locations.
2. Missing credentials produce zero HTTP calls.
3. Placeholder credentials produce zero HTTP calls.
4. TOTP validation.
5. .env loading when process environment variables are absent.
6. Partial process environment plus .env loading.
7. No credential leakage in exceptions or reports.
8. Correct NIFTY, BANKNIFTY and RELIANCE tokens.
9. Correct one-minute and five-minute interval mapping.
10. Exactly six logical probes.
11. Single-session request bounds.
12. Bounded authentication refresh.
13. Bounded candle retries.
14. Global maximum of 20 HTTP attempts.
15. IST timestamp normalization.
16. Missing 15:15-15:30 interval detection.
17. Post-15:30 classification.
18. Flat post-gap observation classification.
19. HTTP errors and status=False responses classified correctly.
20. No forbidden endpoints or provider side effects.
21. Report-path traversal and symlink protection.
22. No modifications to historical datasets or checkpoints.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import tempfile
import unittest
import urllib.error
from datetime import date, datetime, time as dt_time, timedelta
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import patch

from backend.app.data.angel_one import generate_totp_code
from backend.app.data.fno_tokens import FNO_TOKEN_MAP
from backend.app.data.historical_acquisition import (
    AngelOneHistoricalSpotProvider,
    normalize_angel_one_interval,
    redact_sensitive_text,
    resolve_angel_credentials,
    resolve_angel_one_instrument,
)
from backend.app.data.session_clock import IST_ZONE
from backend.app.scripts import audit_provider_post_1515 as probe_mod
from backend.app.scripts.audit_provider_post_1515 import (
    CANDLE_URL,
    LOGICAL_CANDLE_PROBES_PLANNED,
    LOGIN_URL,
    HttpExecutionBudgetGuard,
    ProbeClassification,
    analyze_probe_candles,
    build_six_probe_plan,
    run_post_1515_probe_audit,
    validate_probe_plan,
    validate_safe_report_path,
    verify_probe_artifact_readiness,
)


VALID_TOTP_SECRET = "JBSWY3DPEHPK3PXP"
MOCK_API_KEY = "MockApiKeyAlpha99"
MOCK_CLIENT_CODE = "MockClientX88"
MOCK_PASSWORD = "MockPasswordSecret77"
MOCK_JWT_1 = "eyJhbGciOiJIUzUxMiJ9.mock_payload_token_1.mock_sig_1"
MOCK_JWT_2 = "eyJhbGciOiJIUzUxMiJ9.mock_payload_token_2.mock_sig_2"
MOCK_REFRESH = "eyJhbGciOiJIUzUxMiJ9.mock_refresh_secret.mock_sig"
MOCK_FEED = "mock_feed_secret_012345"


class _FakeHTTPResponse:
    def __init__(self, payload: Dict[str, Any], status: int = 200):
        self._raw = json.dumps(payload).encode("utf-8")
        self.status = status

    def read(self) -> bytes:
        return self._raw

    def __enter__(self) -> "_FakeHTTPResponse":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None


def _make_sample_candles(session_date: date, count: int = 10, step_minutes: int = 1) -> List[List[Any]]:
    base = datetime.combine(session_date, dt_time(9, 15), tzinfo=IST_ZONE)
    rows: List[List[Any]] = []
    for idx in range(count):
        dt = base + timedelta(minutes=idx * step_minutes)
        p = 24000.0 + idx
        rows.append([dt.isoformat(), p, p + 5.0, p - 3.0, p + 2.0, 0])
    return rows


def _valid_environ() -> Dict[str, str]:
    return {
        "ANGEL_API_KEY": MOCK_API_KEY,
        "ANGEL_CLIENT_CODE": MOCK_CLIENT_CODE,
        "ANGEL_PASSWORD": MOCK_PASSWORD,
        "ANGEL_TOTP_SECRET": VALID_TOTP_SECRET,
    }


class TestProviderProbeAudit(unittest.TestCase):

    # 1. Missing artifact recovery logic and expected file locations
    def test_01_missing_artifact_recovery_and_expected_locations(self):
        repo_root = Path(__file__).resolve().parents[2]
        readiness = verify_probe_artifact_readiness(repo_root=repo_root)

        self.assertTrue(readiness["script_present"])
        self.assertTrue(readiness["test_present"])
        self.assertTrue(readiness["env_example_present"])
        self.assertTrue(readiness["gitignore_covers_env"])
        self.assertEqual(readiness["logical_candle_probes_planned"], 6)
        self.assertEqual(readiness["forbidden_references"], [])

        probes = build_six_probe_plan()
        self.assertIn("nifty_1m_180d_prior_trading_session", [p.probe_id for p in probes])

    # 2. Missing credentials produce zero HTTP calls
    def test_02_missing_credentials_produce_zero_http_calls(self):
        with patch("urllib.request.urlopen") as mock_urlopen:
            report = run_post_1515_probe_audit(
                execute_live=True,
                dotenv_path=None,
                environ={},
            )
            self.assertEqual(mock_urlopen.call_count, 0)
            self.assertEqual(report["overall_status"], ProbeClassification.BLOCKED_MISSING_CREDENTIALS)
            self.assertEqual(report["http_budget"]["total_http_attempts"], 0)
            for key in ("ANGEL_API_KEY", "ANGEL_CLIENT_CODE", "ANGEL_PASSWORD", "ANGEL_TOTP_SECRET"):
                self.assertEqual(report["credential_statuses"][key], "MISSING")
            self.assertEqual(len(report["probes"]), 6)
            for p in report["probes"]:
                self.assertEqual(p["status"], ProbeClassification.BLOCKED_MISSING_CREDENTIALS)
                self.assertEqual(p["http_attempts"], 0)

            provider = AngelOneHistoricalSpotProvider(
                api_key="", client_code="", password="", totp_secret="", env_paths=[]
            )
            with patch.dict(os.environ, {}, clear=True):
                self.assertFalse(provider.connect())
            self.assertEqual(mock_urlopen.call_count, 0)
            self.assertEqual(provider.last_auth_error, "BLOCKED_MISSING_CREDENTIALS")

    # 3. Placeholder credentials produce zero HTTP calls
    def test_03_placeholder_credentials_produce_zero_http_calls(self):
        placeholders = {
            "ANGEL_API_KEY": "your_api_key_here",
            "ANGEL_CLIENT_CODE": "your_client_code_here",
            "ANGEL_PASSWORD": "your_password_or_mpin_here",
            "ANGEL_TOTP_SECRET": "your_totp_secret_here",
        }
        with patch("urllib.request.urlopen") as mock_urlopen:
            report = run_post_1515_probe_audit(
                execute_live=True,
                dotenv_path=None,
                environ=placeholders,
            )
            self.assertEqual(mock_urlopen.call_count, 0)
            self.assertEqual(report["overall_status"], ProbeClassification.BLOCKED_MISSING_CREDENTIALS)
            for status in report["credential_statuses"].values():
                self.assertEqual(status, "PLACEHOLDER")

            # Partial placeholder: 3 real + 1 placeholder TOTP
            partial_ph = dict(_valid_environ())
            partial_ph["ANGEL_TOTP_SECRET"] = "your_totp_secret_here"
            report2 = run_post_1515_probe_audit(
                execute_live=True,
                dotenv_path=None,
                environ=partial_ph,
            )
            self.assertEqual(mock_urlopen.call_count, 0)
            self.assertEqual(report2["credential_statuses"]["ANGEL_TOTP_SECRET"], "PLACEHOLDER")

    # 4. TOTP validation
    def test_04_totp_validation(self):
        code = generate_totp_code(VALID_TOTP_SECRET)
        self.assertEqual(len(code), 6)
        self.assertTrue(code.isdigit())

        for bad_secret in ("123456", "NOT!BASE32", "ABC", "JBSW=AAA"):
            env = dict(_valid_environ())
            env["ANGEL_TOTP_SECRET"] = bad_secret
            with patch("urllib.request.urlopen") as mock_urlopen:
                report = run_post_1515_probe_audit(
                    execute_live=True,
                    dotenv_path=None,
                    environ=env,
                )
                self.assertEqual(mock_urlopen.call_count, 0)
                self.assertEqual(report["credential_statuses"]["ANGEL_TOTP_SECRET"], "INVALID_FORMAT")
                self.assertEqual(report["overall_status"], ProbeClassification.BLOCKED_MISSING_CREDENTIALS)

    # 5. .env loading when process environment variables are absent
    def test_05_dotenv_loading_when_process_env_absent(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            env_file = Path(tmpdir) / ".env"
            env_file.write_text(
                f'ANGEL_API_KEY="{MOCK_API_KEY}"\n'
                f"ANGEL_CLIENT_CODE={MOCK_CLIENT_CODE}\n"
                f"ANGEL_PASSWORD='{MOCK_PASSWORD}'\n"
                f"ANGEL_TOTP_SECRET={VALID_TOTP_SECRET}\n",
                encoding="utf-8",
            )
            res = resolve_angel_credentials(environ={}, dotenv_paths=[env_file])
            self.assertTrue(res["all_valid"])
            self.assertEqual(res["values"]["ANGEL_API_KEY"], MOCK_API_KEY)
            self.assertEqual(res["values"]["ANGEL_PASSWORD"], MOCK_PASSWORD)
            for k in res["sources"]:
                self.assertEqual(res["sources"][k], "DOTENV")

            # Also verify AngelOneHistoricalSpotProvider loads all 4 from temp .env
            provider = AngelOneHistoricalSpotProvider(env_paths=[str(env_file)])
            with patch.dict(os.environ, {}, clear=True), patch("urllib.request.urlopen") as mock_urlopen:
                mock_urlopen.return_value = _FakeHTTPResponse(
                    {"status": True, "message": "SUCCESS", "data": {"jwtToken": MOCK_JWT_1}}
                )
                self.assertTrue(provider.connect())
                self.assertEqual(mock_urlopen.call_count, 1)

    # 6. Partial process environment plus .env loading
    def test_06_partial_process_env_plus_dotenv_loading(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            env_file = Path(tmpdir) / ".env"
            env_file.write_text(
                "ANGEL_API_KEY=DotEnvShouldNotOverrideProcessEnv\n"
                f"ANGEL_CLIENT_CODE={MOCK_CLIENT_CODE}\n"
                f"ANGEL_PASSWORD={MOCK_PASSWORD}\n"
                f"ANGEL_TOTP_SECRET={VALID_TOTP_SECRET}\n",
                encoding="utf-8",
            )
            partial_env = {
                "ANGEL_API_KEY": MOCK_API_KEY,
                "ANGEL_CLIENT_CODE": MOCK_CLIENT_CODE,
            }
            res = resolve_angel_credentials(environ=partial_env, dotenv_paths=[env_file])
            self.assertTrue(res["all_valid"])
            self.assertEqual(res["values"]["ANGEL_API_KEY"], MOCK_API_KEY)
            self.assertEqual(res["sources"]["ANGEL_API_KEY"], "PROCESS_ENV")
            self.assertEqual(res["sources"]["ANGEL_PASSWORD"], "DOTENV")
            self.assertEqual(res["sources"]["ANGEL_TOTP_SECRET"], "DOTENV")

            # Verify provider._load_env_if_needed loads missing TOTP even when the other 3 exist
            provider = AngelOneHistoricalSpotProvider(env_paths=[str(env_file)])
            three_keys_env = {
                "ANGEL_API_KEY": MOCK_API_KEY,
                "ANGEL_CLIENT_CODE": MOCK_CLIENT_CODE,
                "ANGEL_PASSWORD": MOCK_PASSWORD,
            }
            with patch.dict(os.environ, three_keys_env, clear=True):
                provider._load_env_if_needed()
                self.assertEqual(provider.totp_secret, VALID_TOTP_SECRET)

    # 7. No credential leakage in exceptions or reports
    def test_07_no_credential_leakage_in_exceptions_or_reports(self):
        secrets_list = [MOCK_API_KEY, MOCK_CLIENT_CODE, MOCK_PASSWORD, VALID_TOTP_SECRET, MOCK_JWT_1, MOCK_REFRESH, MOCK_FEED]

        # Test fetch_candles RuntimeError redaction
        provider = AngelOneHistoricalSpotProvider(
            api_key=MOCK_API_KEY,
            client_code=MOCK_CLIENT_CODE,
            password=MOCK_PASSWORD,
            totp_secret=VALID_TOTP_SECRET,
            rate_limit_pause_sec=0.0,
            env_paths=[],
        )
        provider.is_authenticated = True
        provider.jwt_token = MOCK_JWT_1

        leaky_body = json.dumps({
            "message": (
                f"Bad request apikey={MOCK_API_KEY} client={MOCK_CLIENT_CODE} "
                f"pwd={MOCK_PASSWORD} totp_secret={VALID_TOTP_SECRET} "
                f"Bearer {MOCK_JWT_1} refreshToken={MOCK_REFRESH} feedToken={MOCK_FEED}"
            )
        }).encode("utf-8")

        def _raise_leaky_http(req, timeout=12):
            raise urllib.error.HTTPError(
                req.full_url,
                500,
                "Server Error",
                hdrs={},
                fp=io.BytesIO(leaky_body),
            )

        with patch("urllib.request.urlopen", side_effect=_raise_leaky_http), patch("time.sleep"):
            with self.assertRaises(RuntimeError) as ctx:
                provider.fetch_candles(
                    "NIFTY",
                    "1m",
                    datetime(2026, 11, 6, 9, 15, tzinfo=IST_ZONE),
                    datetime(2026, 11, 6, 15, 30, tzinfo=IST_ZONE),
                )
            err_msg = str(ctx.exception)
            for secret in secrets_list:
                self.assertNotIn(secret, err_msg)

        # Test full probe report redaction + file persistence
        with tempfile.TemporaryDirectory() as tmpdir:
            report_file = Path(tmpdir) / "probe_report.json"

            def _mock_urlopen(req, timeout=10):
                if req.full_url == LOGIN_URL:
                    return _FakeHTTPResponse({
                        "status": True,
                        "data": {
                            "jwtToken": MOCK_JWT_1,
                            "refreshToken": MOCK_REFRESH,
                            "feedToken": MOCK_FEED,
                        },
                    })
                return _FakeHTTPResponse({
                    "status": False,
                    "errorcode": "AB1019",
                    "message": (
                        f"Denied for client {MOCK_CLIENT_CODE} key {MOCK_API_KEY} "
                        f"Bearer {MOCK_JWT_1} refreshToken={MOCK_REFRESH} feedToken={MOCK_FEED}"
                    ),
                })

            with patch("urllib.request.urlopen", side_effect=_mock_urlopen), patch("time.sleep"):
                report = run_post_1515_probe_audit(
                    execute_live=True,
                    dotenv_path=None,
                    environ=_valid_environ(),
                    rate_limit_pause_sec=0.0,
                    output_report_path=report_file,
                )

            disk_text = report_file.read_text(encoding="utf-8")
            mem_text = json.dumps(report)
            for secret in secrets_list:
                self.assertNotIn(secret, disk_text)
                self.assertNotIn(secret, mem_text)
            self.assertNotIn("refreshToken", mem_text)
            self.assertNotIn("feedToken", mem_text)

    # 8. Correct NIFTY, BANKNIFTY and RELIANCE tokens
    def test_08_correct_instrument_tokens_and_exchange(self):
        self.assertEqual(resolve_angel_one_instrument("NIFTY"), (FNO_TOKEN_MAP["NIFTY 50"]["token"], "NSE"))
        self.assertEqual(resolve_angel_one_instrument("NIFTY"), ("99926000", "NSE"))
        self.assertEqual(resolve_angel_one_instrument("BANKNIFTY"), (FNO_TOKEN_MAP["BANKNIFTY"]["token"], "NSE"))
        self.assertEqual(resolve_angel_one_instrument("BANKNIFTY"), ("99926009", "NSE"))
        self.assertEqual(resolve_angel_one_instrument("RELIANCE"), (FNO_TOKEN_MAP["RELIANCE"]["token"], "NSE"))
        self.assertEqual(resolve_angel_one_instrument("RELIANCE"), ("2885", "NSE"))

        with self.assertRaises(ValueError):
            resolve_angel_one_instrument("NONEXISTENT_SYMBOL")

    # 9. Correct one-minute and five-minute interval mapping
    def test_09_interval_normalization(self):
        for s in ("1m", "1min", "ONE_MINUTE"):
            self.assertEqual(normalize_angel_one_interval(s), "ONE_MINUTE")
        for s in ("5m", "5min", "FIVE_MINUTE"):
            self.assertEqual(normalize_angel_one_interval(s), "FIVE_MINUTE")
        with self.assertRaises(ValueError):
            normalize_angel_one_interval("15s")

        captured_intervals: List[str] = []
        provider = AngelOneHistoricalSpotProvider(
            api_key=MOCK_API_KEY,
            client_code=MOCK_CLIENT_CODE,
            password=MOCK_PASSWORD,
            totp_secret=VALID_TOTP_SECRET,
            rate_limit_pause_sec=0.0,
            env_paths=[],
        )
        provider.is_authenticated = True
        provider.jwt_token = MOCK_JWT_1

        def _mock_urlopen(req, timeout=12):
            body = json.loads(req.data.decode("utf-8"))
            captured_intervals.append(body["interval"])
            return _FakeHTTPResponse({"status": True, "data": _make_sample_candles(date(2026, 11, 6), 2)})

        with patch("urllib.request.urlopen", side_effect=_mock_urlopen):
            provider.fetch_candles("NIFTY", "1m", datetime(2026, 11, 6, 9, 15), datetime(2026, 11, 6, 15, 30))
            provider.fetch_candles("NIFTY", "5m", datetime(2026, 11, 6, 9, 15), datetime(2026, 11, 6, 15, 30))
        self.assertEqual(captured_intervals, ["ONE_MINUTE", "FIVE_MINUTE"])

    # 10. Exactly six logical probes
    def test_10_exactly_six_logical_probes(self):
        probes = build_six_probe_plan()
        self.assertEqual(LOGICAL_CANDLE_PROBES_PLANNED, 6)
        self.assertEqual(len(probes), 6)
        expected = [
            ("nifty_1m_to_1530", "NIFTY", "99926000", "ONE_MINUTE", date(2026, 11, 6), "15:30"),
            ("nifty_1m_to_1600", "NIFTY", "99926000", "ONE_MINUTE", date(2026, 11, 6), "16:00"),
            ("nifty_5m_to_1530", "NIFTY", "99926000", "FIVE_MINUTE", date(2026, 11, 6), "15:30"),
            ("banknifty_1m_to_1530", "BANKNIFTY", "99926009", "ONE_MINUTE", date(2026, 11, 6), "15:30"),
            ("reliance_1m_to_1530", "RELIANCE", "2885", "ONE_MINUTE", date(2026, 11, 6), "15:30"),
            ("nifty_1m_180d_prior_trading_session", "NIFTY", "99926000", "ONE_MINUTE", date(2026, 5, 8), "15:30"),
        ]
        for p, (exp_id, exp_inst, exp_tok, exp_iv, exp_dt, exp_end_hm) in zip(probes, expected):
            self.assertEqual(p.probe_id, exp_id)
            self.assertEqual(p.instrument, exp_inst)
            self.assertEqual(p.token, exp_tok)
            self.assertEqual(p.interval, exp_iv)
            self.assertEqual(p.session_date, exp_dt)
            self.assertEqual(p.to_dt.strftime("%H:%M"), exp_end_hm)
        self.assertEqual((date(2026, 11, 6) - date(2026, 5, 8)).days, 182)

    # 11. Single-session request bounds
    def test_11_single_session_request_bounds(self):
        probes = build_six_probe_plan()
        for p in probes:
            self.assertEqual(p.from_dt.date(), p.to_dt.date())

        # Weekend / Holiday reference date rejected
        with self.assertRaises(ValueError):
            build_six_probe_plan(reference_date=date(2026, 11, 7))  # Saturday
        with self.assertRaises(ValueError):
            build_six_probe_plan(reference_date=date(2026, 10, 20))  # Holiday

        # Multi-day probe rejected
        bad = list(probes)
        first = bad[0]
        bad[0] = probe_mod.ProbeSpec(
            probe_id=first.probe_id,
            purpose=first.purpose,
            instrument=first.instrument,
            token=first.token,
            exchange=first.exchange,
            interval=first.interval,
            session_date=first.session_date,
            from_dt=first.from_dt - timedelta(days=1),
            to_dt=first.to_dt,
        )
        with self.assertRaises(ValueError):
            validate_probe_plan(bad)

    # 12. Bounded authentication refresh
    def test_12_bounded_authentication_refresh(self):
        auth_calls = 0

        def _mock_urlopen(req, timeout=10):
            nonlocal auth_calls
            if req.full_url == LOGIN_URL:
                auth_calls += 1
                tok = MOCK_JWT_1 if auth_calls == 1 else MOCK_JWT_2
                return _FakeHTTPResponse({"status": True, "data": {"jwtToken": tok}})

            auth_header = req.headers.get("Authorization", "")
            payload = json.loads(req.data.decode("utf-8"))
            if payload["symboltoken"] == "99926000" and payload["todate"].endswith("15:30") and payload["interval"] == "ONE_MINUTE":
                # First probe expires once on MOCK_JWT_1, then succeeds after 1 refresh with MOCK_JWT_2
                if MOCK_JWT_1 in auth_header:
                    return _FakeHTTPResponse({"status": False, "errorcode": "AG8001", "message": "Expired"})
                return _FakeHTTPResponse({"status": True, "data": _make_sample_candles(date(2026, 11, 6), 5)})

            # Subsequent probe hits AG8001 again -> must NOT trigger a 3rd loginByPassword call!
            return _FakeHTTPResponse({"status": False, "errorcode": "AG8001", "message": "Expired again"})

        with patch("urllib.request.urlopen", side_effect=_mock_urlopen), patch("time.sleep"):
            report = run_post_1515_probe_audit(
                execute_live=True,
                dotenv_path=None,
                environ=_valid_environ(),
                rate_limit_pause_sec=0.0,
            )

        self.assertEqual(auth_calls, 2)
        self.assertLessEqual(report["http_budget"]["auth_http_attempts"], 2)
        self.assertEqual(report["probes"][0]["status"], ProbeClassification.PASS)
        self.assertEqual(report["probes"][0]["http_attempts"], 2)
        self.assertEqual(report["probes"][0]["retry_count"], 1)
        self.assertEqual(report["probes"][1]["status"], ProbeClassification.FAIL)
        self.assertTrue(any("AUTH_REFRESH_LIMIT_EXCEEDED" in e for e in report["probes"][1]["redacted_errors"]))

    # 13. Bounded candle retries
    def test_13_bounded_candle_retries(self):
        candle_calls = 0

        def _mock_urlopen(req, timeout=10):
            nonlocal candle_calls
            if req.full_url == LOGIN_URL:
                return _FakeHTTPResponse({"status": True, "data": {"jwtToken": MOCK_JWT_1}})
            candle_calls += 1
            return _FakeHTTPResponse({"status": False, "errorcode": "AB1004", "message": "Rate limited"})

        with patch("urllib.request.urlopen", side_effect=_mock_urlopen), patch("time.sleep"):
            provider = AngelOneHistoricalSpotProvider(
                api_key=MOCK_API_KEY,
                client_code=MOCK_CLIENT_CODE,
                password=MOCK_PASSWORD,
                totp_secret=VALID_TOTP_SECRET,
                rate_limit_pause_sec=0.0,
                env_paths=[],
            )
            with self.assertRaises(RuntimeError):
                provider.fetch_candles(
                    "NIFTY",
                    "1m",
                    datetime(2026, 11, 6, 9, 15, tzinfo=IST_ZONE),
                    datetime(2026, 11, 6, 15, 30, tzinfo=IST_ZONE),
                )
        self.assertEqual(candle_calls, 3)
        self.assertEqual(provider.last_fetch_metadata["http_attempts"], 3)
        self.assertEqual(provider.last_fetch_metadata["retry_count"], 2)

    # 14. Global maximum of 20 HTTP attempts
    def test_14_global_max_20_http_attempts(self):
        # Test exact cap via runner (1 initial auth + 1 AG8001 refresh + 18 worst-case candle calls = 20 max)
        total_http = 0
        auth_calls = 0

        def _mock_urlopen(req, timeout=10):
            nonlocal total_http, auth_calls
            total_http += 1
            if req.full_url == LOGIN_URL:
                auth_calls += 1
                tok = MOCK_JWT_1 if auth_calls == 1 else MOCK_JWT_2
                return _FakeHTTPResponse({"status": True, "data": {"jwtToken": tok}})
            # Probe 1 triggers 1 refresh on 1st call then rate-limit errors, remaining probes rate-limit x3 each
            if auth_calls == 1:
                return _FakeHTTPResponse({"status": False, "errorcode": "AG8001", "message": "Refresh once"})
            return _FakeHTTPResponse({"status": False, "errorcode": "AB1004", "message": "Busy"})

        with patch("urllib.request.urlopen", side_effect=_mock_urlopen), patch("time.sleep"):
            report = run_post_1515_probe_audit(
                execute_live=True,
                dotenv_path=None,
                environ=_valid_environ(),
                rate_limit_pause_sec=0.0,
            )

        self.assertEqual(total_http, 20)
        self.assertEqual(report["http_budget"]["auth_http_attempts"], 2)
        self.assertEqual(report["http_budget"]["candle_http_attempts"], 18)
        self.assertEqual(report["http_budget"]["total_http_attempts"], 20)

        # Verify guard itself blocks attempt #21 before dispatch
        guard = HttpExecutionBudgetGuard()
        guard.before_request(LOGIN_URL)
        guard.before_request(LOGIN_URL)
        for idx in range(6):
            guard.begin_probe(f"p{idx}")
            for _ in range(3):
                guard.before_request(CANDLE_URL)
        guard.begin_probe("p_extra")
        with self.assertRaises(PermissionError):
            guard.before_request(CANDLE_URL)

    # 15. IST timestamp normalization
    def test_15_ist_timestamp_normalization(self):
        probes = build_six_probe_plan()
        p = probes[0]
        mixed_rows = [
            ["2026-11-06T03:45:00Z", 24000, 24010, 23990, 24005, 0],           # UTC -> 09:15 IST
            ["2026-11-06T09:16:00+05:30", 24005, 24015, 24000, 24010, 0],      # Already IST
            ["2026-11-06 09:17", 24010, 24020, 24005, 24015, 0],               # Naive Angel One string
        ]
        res = analyze_probe_candles(p, mixed_rows)
        self.assertEqual(
            res["ist_candle_timestamps"],
            [
                "2026-11-06T09:15:00+05:30",
                "2026-11-06T09:16:00+05:30",
                "2026-11-06T09:17:00+05:30",
            ],
        )

    # 16. Missing 15:15-15:30 interval detection
    def test_16_missing_1515_to_1530_interval_detection(self):
        probes = build_six_probe_plan()
        p = probes[0]  # NIFTY 1m 09:15-15:30 on 2026-11-06
        rows = _make_sample_candles(date(2026, 11, 6), count=360, step_minutes=1)  # 09:15..15:14
        res = analyze_probe_candles(p, rows)

        self.assertEqual(res["count_before_1515"], 360)
        self.assertEqual(res["count_1515_to_1530"], 0)
        self.assertEqual(res["missing_1515_to_1530_count"], 15)
        self.assertEqual(len(res["missing_intervals"]), 15)
        self.assertEqual(res["missing_1515_to_1530_intervals"][0], "2026-11-06T15:15:00+05:30")
        self.assertEqual(res["missing_1515_to_1530_intervals"][-1], "2026-11-06T15:29:00+05:30")

    # 17. Post-15:30 classification
    def test_17_post_1530_classification(self):
        probes = build_six_probe_plan()
        p_1600 = probes[1]  # nifty_1m_to_1600
        rows = _make_sample_candles(date(2026, 11, 6), count=360, step_minutes=1)
        rows.append(["2026-11-06T15:30:00+05:30", 24360.0, 24360.0, 24360.0, 24360.0, 0])
        rows.append(["2026-11-06T15:35:00+05:30", 24360.0, 24360.0, 24360.0, 24360.0, 0])

        res = analyze_probe_candles(p_1600, rows)
        self.assertEqual(res["count_before_1515"], 360)
        self.assertEqual(res["count_1515_to_1530"], 0)
        self.assertEqual(res["count_after_1530"], 2)
        self.assertEqual(len(res["post_1530_records"]), 2)
        self.assertEqual(res["post_1530_records"][0]["classification"], "POST_1530_UNVERIFIED")
        self.assertTrue(res["post_1530_records"][0]["is_flat_ohlc"])

        interp = probe_mod._build_probe_interpretation(p_1600, ProbeClassification.PASS, res, [])
        self.assertIn("never inferred from ohlc values alone", interp.lower())

    # 18. Flat post-gap observation classification
    def test_18_flat_post_gap_observation_classification(self):
        probes = build_six_probe_plan()
        # 1m: 09:15..15:14 (360 bars) + gap + isolated flat bar at 15:29
        rows_1m = _make_sample_candles(date(2026, 11, 6), count=360, step_minutes=1)
        rows_1m.append(["2026-11-06T15:29:00+05:30", 24500.0, 24500.0, 24500.0, 24500.0, 0])
        res_1m = analyze_probe_candles(probes[0], rows_1m)
        self.assertEqual(len(res_1m["flat_post_gap_observations"]), 1)
        obs = res_1m["flat_post_gap_observations"][0]
        self.assertEqual(obs["timestamp"], "2026-11-06T15:29:00+05:30")
        self.assertEqual(obs["gap_seconds_since_previous"], 900)
        self.assertEqual(obs["classification"], "ISOLATED_POST_GAP_FLAT_OBSERVATION_UNVERIFIED")

        # 5m: consecutive flat candles spaced 300s apart must NOT false-positive as post-gap
        p_5m = probes[2]
        rows_5m = [
            ["2026-11-06T09:15:00+05:30", 24000.0, 24000.0, 24000.0, 24000.0, 0],
            ["2026-11-06T09:20:00+05:30", 24000.0, 24000.0, 24000.0, 24000.0, 0],
        ]
        res_5m = analyze_probe_candles(p_5m, rows_5m)
        self.assertEqual(len(res_5m["flat_post_gap_observations"]), 0)

    # 19. HTTP errors and status=False responses classified correctly
    def test_19_http_errors_and_status_false_classification(self):
        req_count = 0

        def _mock_urlopen(req, timeout=10):
            nonlocal req_count
            if req.full_url == LOGIN_URL:
                return _FakeHTTPResponse({"status": True, "data": {"jwtToken": MOCK_JWT_1}})
            req_count += 1
            payload = json.loads(req.data.decode("utf-8"))
            if payload["symboltoken"] == "99926000" and payload["interval"] == "FIVE_MINUTE":
                return _FakeHTTPResponse({"status": False, "errorcode": "AB1019", "message": "Invalid filter"})
            if payload["symboltoken"] == "99926009":
                raise urllib.error.HTTPError(req.full_url, 503, "Service Unavailable", hdrs={}, fp=io.BytesIO(b"down"))
            if payload["symboltoken"] == "2885":
                return _FakeHTTPResponse({"status": True, "data": []})
            return _FakeHTTPResponse({"status": True, "data": _make_sample_candles(date(2026, 11, 6), 3)})

        with patch("urllib.request.urlopen", side_effect=_mock_urlopen), patch("time.sleep"):
            report = run_post_1515_probe_audit(
                execute_live=True,
                dotenv_path=None,
                environ=_valid_environ(),
                rate_limit_pause_sec=0.0,
            )

        by_id = {p["probe_id"]: p for p in report["probes"]}
        self.assertEqual(by_id["nifty_1m_to_1530"]["status"], ProbeClassification.PASS)
        self.assertEqual(by_id["nifty_5m_to_1530"]["status"], ProbeClassification.FAIL)
        self.assertFalse(by_id["nifty_5m_to_1530"]["api_status"])
        self.assertEqual(by_id["banknifty_1m_to_1530"]["status"], ProbeClassification.FAIL)
        self.assertEqual(by_id["reliance_1m_to_1530"]["status"], ProbeClassification.INCONCLUSIVE)
        self.assertIn(
            "does not prove",
            by_id["reliance_1m_to_1530"]["interpretation"].lower(),
        )
        self.assertIn(
            "does not prove",
            by_id["nifty_5m_to_1530"]["interpretation"].lower(),
        )

    # 20. No forbidden endpoints or provider side effects
    def test_20_no_forbidden_endpoints_or_provider_side_effects(self):
        guard = HttpExecutionBudgetGuard()
        for forbidden_url in (
            "https://apiconnect.angelone.in/rest/secure/angelbroking/order/v1/getPosition",
            "https://apiconnect.angelone.in/rest/secure/angelbroking/market/v1/quote",
            "https://apiconnect.angelone.in/rest/secure/angelbroking/user/v1/getProfile",
            "https://apiconnect.angelone.in/rest/secure/angelbroking/order/v1/placeOrder",
        ):
            with self.assertRaises(PermissionError):
                guard.before_request(forbidden_url)
        self.assertEqual(guard.total_http_attempts, 0)

        readiness = verify_probe_artifact_readiness()
        self.assertEqual(readiness["forbidden_references"], [])

    # 21. Report-path traversal and symlink protection
    def test_21_report_path_traversal_and_symlink_protection(self):
        repo_root = Path(__file__).resolve().parents[2]
        with self.assertRaises(ValueError):
            validate_safe_report_path("../escape.json", repo_root=repo_root)
        with self.assertRaises(ValueError):
            validate_safe_report_path("data/historical/bad.json", repo_root=repo_root)
        with self.assertRaises(ValueError):
            validate_safe_report_path("data/checkpoints/bad.json", repo_root=repo_root)

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            safe_out = tmp / "safe_probe_report.json"
            resolved = validate_safe_report_path(safe_out, repo_root=repo_root)
            self.assertEqual(resolved.name, "safe_probe_report.json")

            # Symlink target rejected
            real_target = tmp / "real.json"
            real_target.write_text("{}", encoding="utf-8")
            sym_file = tmp / "sym_report.json"
            os.symlink(real_target, sym_file)
            with self.assertRaises(ValueError):
                validate_safe_report_path(sym_file, repo_root=repo_root)

            # Symlink directory pointing to data/historical rejected
            sym_dir = tmp / "sym_hist"
            os.symlink(repo_root / "data" / "historical", sym_dir)
            with self.assertRaises(ValueError):
                validate_safe_report_path(sym_dir / "leak.json", repo_root=repo_root)

    # 22. No modifications to historical datasets or checkpoints
    def test_22_no_modifications_to_historical_datasets_or_checkpoints(self):
        repo_root = Path(__file__).resolve().parents[2]
        data_root = repo_root / "data"

        def _snapshot_tree() -> Dict[str, str]:
            hashes: Dict[str, str] = {}
            if not data_root.exists():
                return hashes
            for p in sorted(data_root.rglob("*")):
                if p.is_file():
                    hashes[str(p.relative_to(repo_root))] = hashlib.sha256(p.read_bytes()).hexdigest()
            return hashes

        before_hashes = _snapshot_tree()

        def _mock_urlopen(req, timeout=10):
            if req.full_url == LOGIN_URL:
                return _FakeHTTPResponse({"status": True, "data": {"jwtToken": MOCK_JWT_1}})
            return _FakeHTTPResponse({"status": True, "data": _make_sample_candles(date(2026, 11, 6), 5)})

        with tempfile.TemporaryDirectory() as tmpdir, \
                patch("urllib.request.urlopen", side_effect=_mock_urlopen), \
                patch("time.sleep"):
            out_file = Path(tmpdir) / "report.json"
            report = run_post_1515_probe_audit(
                execute_live=True,
                dotenv_path=None,
                environ=_valid_environ(),
                rate_limit_pause_sec=0.0,
                output_report_path=out_file,
                repo_root=repo_root,
            )
            self.assertEqual(report["overall_status"], ProbeClassification.PASS)
            self.assertTrue(out_file.is_file())

        after_hashes = _snapshot_tree()
        self.assertEqual(before_hashes, after_hashes)


if __name__ == "__main__":
    unittest.main()
