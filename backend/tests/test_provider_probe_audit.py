"""
TradeLens AI — Phase 3E.5.1.1 Dedicated Offline Audit Test Suite:
Validates `backend/app/scripts/audit_provider_post_1515.py` without hitting live APIs.

Tests cover all 12 Phase 3E.5.1.1 requirements:
1.  Logical probe count remains strictly six (distinct from actual HTTP attempts).
2.  Actual HTTP attempt counts & retry counts accurately reflect retries.
3.  Retry limits cannot be exceeded (hard cap at MAX_CANDLE_HTTP_ATTEMPTS_PER_PROBE=3).
4.  Authentication refresh cannot cause an unbounded retry loop (max 1 refresh across run).
5.  Historical probe date offset is accurately reported (`nifty_1m_180d_prior_trading_session`, 182d).
6.  The 16:00 request is classified as a `REQUEST_BOUNDARY_EXPERIMENT`.
7.  Post-15:30 records are separately classified and never inflate continuous-session coverage.
8.  Secret redaction covers errors, report fields, and nested response objects.
9.  `.env` is ignored by Git and `.env.example` contains placeholders only.
10. Report output paths cannot escape safety restrictions through symlinks or relative traversal.
11. No acquisition, replay, outcome, or order/funds/holdings/positions endpoints are called.
12. Missing/placeholder credentials cause zero network requests (`BLOCKED_MISSING_CREDENTIALS`).
"""

import ast
import json
import os
import subprocess
import tempfile
import unittest
import urllib.error
from datetime import datetime, date, time as dt_time, timedelta
from typing import Any, Dict, List
from unittest.mock import patch

from backend.app.data.angel_one import AngelOneSmartAPIProvider
from backend.app.data.fno_tokens import FNO_TOKEN_MAP
from backend.app.data.session_clock import IST_ZONE
from backend.app.scripts import audit_provider_post_1515 as probe_mod
from backend.app.scripts.audit_provider_post_1515 import VerificationStatus


class _FakeHTTPResponse:
    def __init__(self, payload: Dict[str, Any]):
        self._raw = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        return False


def _build_session_rows(
    session_date: date,
    count: int,
    interval_minutes: int = 1,
    include_flat_1529: bool = False,
) -> List[List[Any]]:
    rows: List[List[Any]] = []
    curr = datetime.combine(session_date, dt_time(9, 15), tzinfo=IST_ZONE)
    for i in range(count):
        p = round(24000.0 + i * 0.5, 2)
        rows.append([curr.isoformat(), p, round(p + 2.0, 2), round(p - 1.5, 2), round(p + 1.0, 2), 0])
        curr += timedelta(minutes=interval_minutes)
    if include_flat_1529:
        dt_1529 = datetime.combine(session_date, dt_time(15, 29), tzinfo=IST_ZONE)
        rows.append([dt_1529.isoformat(), 24500.0, 24500.0, 24500.0, 24500.0, 0])
    return rows


class TestProviderProbeAudit(unittest.TestCase):

    # ------------------------------------------------------------------------
    # 1 & 12. Logical Probe Count = 6 & Missing/Placeholder Credentials = 0 HTTP Calls
    # ------------------------------------------------------------------------
    def test_01_logical_probe_count_and_missing_credentials_zero_network(self):
        plan = probe_mod.build_probe_plan(date(2026, 11, 6))
        self.assertEqual(len(plan), 6)

        with patch("urllib.request.urlopen") as mock_urlopen:
            report_missing, code_missing = probe_mod.run_audit(
                probe_date=date(2026, 11, 6),
                offline_only=False,
                env_override={},
            )
            self.assertEqual(code_missing, 2)
            self.assertEqual(report_missing["logical_request_plan"]["authentication_operations_planned"], 1)
            self.assertEqual(report_missing["logical_request_plan"]["candle_probes_planned"], 6)
            self.assertEqual(report_missing["live_api_audit"]["status"], VerificationStatus.BLOCKED_MISSING_CREDENTIALS)
            self.assertEqual(report_missing["live_api_audit"]["request_accounting"]["actual_request_execution"]["total_actual_http_attempts"], 0)

            report_ph, code_ph = probe_mod.run_audit(
                probe_date=date(2026, 11, 6),
                offline_only=False,
                env_override={
                    "ANGEL_API_KEY": "your_api_key_here",
                    "ANGEL_CLIENT_CODE": "your_client_code_here",
                    "ANGEL_PASSWORD": "your_password_or_mpin_here",
                    "ANGEL_TOTP_SECRET": "your_totp_secret_here",
                },
            )
            self.assertEqual(code_ph, 2)
            self.assertEqual(report_ph["live_api_audit"]["status"], VerificationStatus.BLOCKED_MISSING_CREDENTIALS)
            self.assertEqual(len(report_ph["live_api_audit"]["missing_env_vars"]), 4)
            mock_urlopen.assert_not_called()

    # ------------------------------------------------------------------------
    # 2. Actual HTTP Attempt Accounting Reflects Retries Accurately
    # ------------------------------------------------------------------------
    def test_02_actual_http_attempt_counts_reflect_retries(self):
        candle_calls_by_token_interval: Dict[str, int] = {}

        def _fake_urlopen(req, timeout=12):
            if req.full_url.endswith("/loginByPassword"):
                return _FakeHTTPResponse({"status": True, "data": {"jwtToken": "jwt_fake_tok"}})
            body = json.loads(req.data.decode("utf-8"))
            key = f"{body['symboltoken']}:{body['interval']}:{body['todate']}"
            candle_calls_by_token_interval[key] = candle_calls_by_token_interval.get(key, 0) + 1
            # Force 2 rate-limit retries on the first probe (succeeds on 3rd attempt)
            if key == "99926000:ONE_MINUTE:2026-11-06 15:30" and candle_calls_by_token_interval[key] < 3:
                return _FakeHTTPResponse({"status": False, "errorcode": "AB1004", "message": "Rate limit"})
            if body["interval"] == "FIVE_MINUTE":
                return _FakeHTTPResponse({"status": True, "data": _build_session_rows(date(2026, 11, 6), 75, 5)})
            d = datetime.strptime(body["fromdate"][:10], "%Y-%m-%d").date()
            return _FakeHTTPResponse({"status": True, "data": _build_session_rows(d, 375, 1)})

        creds = {
            "ANGEL_API_KEY": "k_val",
            "ANGEL_CLIENT_CODE": "C001",
            "ANGEL_PASSWORD": "p_val",
            "ANGEL_TOTP_SECRET": "JBSWY3DPEHPK3PXP",
        }
        with patch("time.sleep", return_value=None), patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            report, code = probe_mod.run_audit(
                probe_date=date(2026, 11, 6),
                offline_only=False,
                env_override=creds,
            )

        self.assertEqual(code, 0)
        acct = report["live_api_audit"]["request_accounting"]
        self.assertEqual(acct["logical_request_plan"]["candle_probes_planned"], 6)
        self.assertEqual(acct["actual_request_execution"]["authentication_attempts"], 1)
        # Probe 1 took 3 attempts (2 retries); remaining 5 took 1 attempt each -> 8 candle attempts + 1 auth = 9
        self.assertEqual(acct["actual_request_execution"]["total_candle_http_attempts"], 8)
        self.assertEqual(acct["actual_request_execution"]["total_actual_http_attempts"], 9)
        self.assertEqual(acct["actual_request_execution"]["unplanned_candle_probes_executed"], 0)

        p0 = report["live_api_audit"]["probes"][0]
        self.assertEqual(p0["probe_id"], "nifty_1m_1530")
        self.assertEqual(p0["actual_http_attempts"], 3)
        self.assertEqual(p0["retry_count"], 2)
        self.assertEqual(p0["verification_status"], VerificationStatus.PASS)

    # ------------------------------------------------------------------------
    # 3. Retry Limits Cannot Be Exceeded
    # ------------------------------------------------------------------------
    def test_03_retry_limits_cannot_be_exceeded(self):
        attempts = {"n": 0}

        def _always_rate_limit(req, timeout=12):
            attempts["n"] += 1
            return _FakeHTTPResponse({"status": False, "errorcode": "AB1004", "message": "Rate limit"})

        creds = {
            "ANGEL_API_KEY": "k_val",
            "ANGEL_CLIENT_CODE": "C001",
            "ANGEL_PASSWORD": "p_val",
            "ANGEL_TOTP_SECRET": "JBSWY3DPEHPK3PXP",
        }
        jwt_state = {"jwt_token": "tok", "auth_attempts": 1, "remaining_auth_refreshes": 1, "total_http_attempts": 1}
        spec = probe_mod.build_probe_plan(date(2026, 11, 6))[0]
        payload = probe_mod.build_candle_request_payload(spec)

        with patch("time.sleep", return_value=None), patch("urllib.request.urlopen", side_effect=_always_rate_limit):
            # Even if caller passes max_attempts=99, hard cap MAX_CANDLE_HTTP_ATTEMPTS_PER_PROBE (3) applies
            api_status, rows, errs, _, used = probe_mod._execute_single_probe_http(
                payload, creds, jwt_state, max_attempts=99
            )

        self.assertEqual(attempts["n"], probe_mod.MAX_CANDLE_HTTP_ATTEMPTS_PER_PROBE)
        self.assertEqual(used, probe_mod.MAX_CANDLE_HTTP_ATTEMPTS_PER_PROBE)
        self.assertIs(api_status, False)
        self.assertEqual(rows, [])
        res = probe_mod.classify_probe_result(spec, payload, api_status, rows, errs, actual_http_attempts=used)
        self.assertEqual(res["verification_status"], VerificationStatus.INCONCLUSIVE)

    # ------------------------------------------------------------------------
    # 4. Authentication Refresh Cannot Cause Unbounded Retry Loop
    # ------------------------------------------------------------------------
    def test_04_auth_refresh_cannot_cause_unbounded_retry_loop(self):
        auth_calls = {"n": 0}
        candle_calls = {"n": 0}

        def _always_expired(req, timeout=12):
            if req.full_url.endswith("/loginByPassword"):
                auth_calls["n"] += 1
                return _FakeHTTPResponse({"status": True, "data": {"jwtToken": f"jwt_{auth_calls['n']}"}})
            candle_calls["n"] += 1
            return _FakeHTTPResponse({"status": False, "errorcode": "AG8001", "message": "Token expired"})

        creds = {
            "ANGEL_API_KEY": "k_val",
            "ANGEL_CLIENT_CODE": "C001",
            "ANGEL_PASSWORD": "p_val",
            "ANGEL_TOTP_SECRET": "JBSWY3DPEHPK3PXP",
        }
        with patch("time.sleep", return_value=None), patch("urllib.request.urlopen", side_effect=_always_expired):
            report, code = probe_mod.run_audit(
                probe_date=date(2026, 11, 6),
                offline_only=False,
                env_override=creds,
            )

        self.assertEqual(code, 0)
        # Initial auth (1) + at most 1 refresh (1) = max 2 auth HTTP calls across entire run
        self.assertEqual(auth_calls["n"], 2)
        self.assertLessEqual(auth_calls["n"], probe_mod.MAX_AUTH_HTTP_ATTEMPTS)
        # Probe 1 triggers 1 refresh (2 candle attempts); Probes 2..6 see refresh budget=0 so stop in 1 attempt each -> 7 candle calls
        self.assertEqual(candle_calls["n"], 7)
        acct = report["live_api_audit"]["request_accounting"]["actual_request_execution"]
        self.assertEqual(acct["authentication_attempts"], 2)
        self.assertEqual(acct["total_candle_http_attempts"], 7)
        self.assertLessEqual(acct["total_actual_http_attempts"], probe_mod.MAX_TOTAL_HTTP_ATTEMPTS)
        for p in report["live_api_audit"]["probes"]:
            self.assertEqual(p["verification_status"], VerificationStatus.INCONCLUSIVE)

    # ------------------------------------------------------------------------
    # 5. Historical Probe Naming & Date Offset Accuracy
    # ------------------------------------------------------------------------
    def test_05_historical_probe_naming_and_date_offset_accuracy(self):
        plan = {s.probe_id: s for s in probe_mod.build_probe_plan(date(2026, 11, 6))}
        self.assertIn("nifty_1m_180d_prior_trading_session", plan)
        self.assertNotIn("nifty_1m_180d_ago", plan)

        spec = plan["nifty_1m_180d_prior_trading_session"]
        payload = probe_mod.build_candle_request_payload(spec)
        res = probe_mod.classify_probe_result(
            spec,
            payload,
            True,
            _build_session_rows(date(2026, 5, 8), 375, interval_minutes=1),
            [],
        )
        self.assertEqual(res["reference_date"], "2026-11-06")
        self.assertEqual(res["actual_probe_date"], "2026-05-08")
        self.assertEqual(res["calendar_day_difference"], 182)
        self.assertTrue(res["is_valid_trading_session"])
        self.assertEqual(res["instrument_token"], FNO_TOKEN_MAP["NIFTY 50"]["token"])

    # ------------------------------------------------------------------------
    # 6 & 7. 16:00 Request-Boundary Experiment & Post-15:30 Separate Classification
    # ------------------------------------------------------------------------
    def test_06_and_07_1600_boundary_experiment_and_post_1530_separation(self):
        plan = {s.probe_id: s for s in probe_mod.build_probe_plan(date(2026, 11, 6))}
        spec_1600 = plan["nifty_1m_1600"]
        payload_1600 = probe_mod.build_candle_request_payload(spec_1600)

        self.assertEqual(spec_1600.probe_purpose, "REQUEST_BOUNDARY_EXPERIMENT")
        self.assertEqual(payload_1600["todate"], "2026-11-06 16:00")

        # 360 continuous bars (09:15-15:14), missing 15:15-15:29, PLUS 1 observation at 15:35
        rows = _build_session_rows(date(2026, 11, 6), 360, interval_minutes=1, include_flat_1529=False)
        rows.append(["2026-11-06T15:35:00+05:30", 24250.0, 24250.0, 24250.0, 24250.0, 0])

        res = probe_mod.classify_probe_result(spec_1600, payload_1600, True, rows, [])
        self.assertEqual(res["probe_purpose"], "REQUEST_BOUNDARY_EXPERIMENT")
        self.assertEqual(res["normal_nse_continuous_session"], "09:15:00-15:30:00 IST")
        self.assertEqual(res["requested_api_boundary"], "16:00:00 IST")
        self.assertEqual(res["candles_before_1515"], 360)
        self.assertEqual(res["candles_between_1515_and_1530"], 0)
        self.assertEqual(res["candles_after_1530"], 1)
        self.assertEqual(len(res["post_1530_observations"]), 1)
        self.assertEqual(res["post_1530_observations"][0]["classification"], "POST_1530_OBSERVATION_UNVERIFIED")
        self.assertEqual(res["post_1530_observations"][0]["timestamp"], "2026-11-06T15:35:00+05:30")
        # Must still FAIL normal 09:15-15:30 continuity check despite post-15:30 record!
        self.assertEqual(res["verification_status"], VerificationStatus.FAIL)
        self.assertEqual(res["missing_intervals_count"], 15)

        # Also test UTC / space-delimited timestamps map to IST properly
        utc_rows = [
            ["2026-11-06 09:15:00", 24000.0, 24005.0, 23995.0, 24002.0, 0],
            ["2026-11-06T09:44:00Z", 24010.0, 24015.0, 24005.0, 24012.0, 0],       # 15:14 IST
            ["2026-11-06T09:45:00+00:00", 24012.0, 24020.0, 24010.0, 24018.0, 0],  # 15:15 IST
            ["2026-11-06T10:01:00Z", 24020.0, 24020.0, 24020.0, 24020.0, 0],       # 15:31 IST
        ]
        utc_analysis = probe_mod.analyze_probe_rows(utc_rows, date(2026, 11, 6), 1)
        self.assertEqual(utc_analysis["candles_before_1515"], 2)
        self.assertEqual(utc_analysis["candles_between_1515_and_1530"], 1)
        self.assertEqual(utc_analysis["candles_after_1530"], 1)

    # ------------------------------------------------------------------------
    # 8. Secret Redaction Covers Errors, Report Fields & Nested Objects
    # ------------------------------------------------------------------------
    def test_08_secret_redaction_covers_errors_reports_and_nested_objects(self):
        creds = {
            "ANGEL_API_KEY": "REDACTED_TEST_KEY_01",
            "ANGEL_CLIENT_CODE": "REDACTED_USER_01",
            "ANGEL_PASSWORD": "REDACTED_PASS_01",
            "ANGEL_TOTP_SECRET": "JBSWY3DPEHPK3PXP",
        }
        leaked_jwt = "eyJhbGciOiJIUzI1NiJ9.nestedpayload.signaturepart"

        nested = {
            "Authorization": f"Bearer {leaked_jwt}",
            "x-privatekey": creds["ANGEL_API_KEY"],
            "nested_list": [
                {"jwtToken": leaked_jwt, "detail": f"failed for {creds['ANGEL_CLIENT_CODE']} pass={creds['ANGEL_PASSWORD']}"}
            ],
        }
        cleaned = probe_mod.sanitize_nested_obj(nested, list(creds.values()) + [leaked_jwt])
        cleaned_str = json.dumps(cleaned)
        for secret in list(creds.values()) + [leaked_jwt]:
            self.assertNotIn(secret, cleaned_str)
        self.assertEqual(cleaned["Authorization"], "[REDACTED]")
        self.assertEqual(cleaned["x-privatekey"], "[REDACTED]")
        self.assertEqual(cleaned["nested_list"][0]["jwtToken"], "[REDACTED]")

        def _err_urlopen(req, timeout=12):
            if req.full_url.endswith("/loginByPassword"):
                return _FakeHTTPResponse({"status": True, "data": {"jwtToken": leaked_jwt}})
            raise urllib.error.HTTPError(
                req.full_url,
                401,
                f"Rejected Bearer {leaked_jwt} key={creds['ANGEL_API_KEY']}",
                hdrs=None,  # type: ignore[arg-type]
                fp=None,
            )

        with patch("time.sleep", return_value=None), patch("urllib.request.urlopen", side_effect=_err_urlopen):
            report, code = probe_mod.run_audit(
                probe_date=date(2026, 11, 6),
                offline_only=False,
                env_override=creds,
            )

        self.assertEqual(code, 0)
        dumped = json.dumps(report)
        for secret in list(creds.values()) + [leaked_jwt]:
            self.assertNotIn(secret, dumped)
        self.assertIn("[REDACTED]", dumped)

    # ------------------------------------------------------------------------
    # 9. `.env` Ignored by Git & `.env.example` Contains Placeholders Only
    # ------------------------------------------------------------------------
    def test_09_env_ignored_by_git_and_env_example_placeholders_only(self):
        proc = subprocess.run(
            ["git", "check-ignore", "-q", ".env"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, ".env must be ignored by .gitignore")

        with open(".env.example", "r", encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if not stripped or stripped.startswith("#") or "=" not in stripped:
                    continue
                key, val = stripped.split("=", 1)
                if key.strip().startswith("ANGEL_"):
                    self.assertTrue(
                        probe_mod.is_placeholder_or_missing(val),
                        f".env.example key {key} must remain a placeholder",
                    )

    # ------------------------------------------------------------------------
    # 10. Report Output Paths Cannot Escape Safety Restrictions via Symlinks
    # ------------------------------------------------------------------------
    def test_10_output_path_guard_blocks_symlinks_and_relative_escapes(self):
        with self.assertRaises(ValueError):
            probe_mod.validate_safe_report_path("data/historical/spot/NIFTY/report.json")
        with self.assertRaises(ValueError):
            probe_mod.validate_safe_report_path("data/checkpoints/probe_report.json")
        with self.assertRaises(ValueError):
            probe_mod.validate_safe_report_path("scratch/../data/historical/escape.json")

        with tempfile.TemporaryDirectory() as tmpd:
            # Directory symlink pointing into data/historical
            dir_link = os.path.join(tmpd, "hist_symlink")
            os.symlink(os.path.abspath("data/historical"), dir_link)
            with self.assertRaises(ValueError):
                probe_mod.validate_safe_report_path(os.path.join(dir_link, "sneaky_report.json"))

            # Leaf file symlink pointing into data/checkpoints
            leaf_link = os.path.join(tmpd, "cp_leaf_symlink.json")
            os.symlink(os.path.abspath("data/checkpoints/sneaky_leaf.json"), leaf_link)
            with self.assertRaises(ValueError):
                probe_mod.validate_safe_report_path(leaf_link)

            # Valid safe output path succeeds
            safe_file = os.path.join(tmpd, "safe_probe_audit.json")
            self.assertEqual(probe_mod.validate_safe_report_path(safe_file), os.path.realpath(safe_file))

    # ------------------------------------------------------------------------
    # 11. No Acquisition, Replay, Outcome, or Order/Account Endpoints Called
    # ------------------------------------------------------------------------
    def test_11_no_forbidden_modules_or_trading_endpoints(self):
        with open("backend/app/scripts/audit_provider_post_1515.py", "r", encoding="utf-8") as f:
            src = f.read()

        tree = ast.parse(src)
        referenced_identifiers = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                referenced_identifiers.add(node.id)
            elif isinstance(node, ast.Attribute):
                referenced_identifiers.add(node.attr)
            elif isinstance(node, ast.alias):
                referenced_identifiers.add(node.name)

        forbidden_symbols = (
            "HistoricalSpotStore",
            "HistoricalAcquisitionService",
            "SpotOutcomeTracker",
            "HistoricalSpotReplayEngine",
            "placeOrder",
            "modifyOrder",
            "cancelOrder",
            "getOrderBook",
            "getPosition",
            "getHolding",
            "getRMS",
            "ingest_records",
            "execute_acquisition",
        )
        for sym in forbidden_symbols:
            self.assertNotIn(sym, referenced_identifiers)

        # Also verify live mocked run hits ONLY loginByPassword and getCandleData
        called_paths: List[str] = []

        def _trace_urlopen(req, timeout=12):
            called_paths.append(req.full_url)
            if req.full_url.endswith("/loginByPassword"):
                return _FakeHTTPResponse({"status": True, "data": {"jwtToken": "jwt_trace"}})
            body = json.loads(req.data.decode("utf-8"))
            interval_min = 5 if body["interval"] == "FIVE_MINUTE" else 1
            count = 75 if interval_min == 5 else 375
            d = datetime.strptime(body["fromdate"][:10], "%Y-%m-%d").date()
            return _FakeHTTPResponse({"status": True, "data": _build_session_rows(d, count, interval_min)})

        creds = {
            "ANGEL_API_KEY": "k_val",
            "ANGEL_CLIENT_CODE": "C001",
            "ANGEL_PASSWORD": "p_val",
            "ANGEL_TOTP_SECRET": "JBSWY3DPEHPK3PXP",
        }
        with patch("time.sleep", return_value=None), patch("urllib.request.urlopen", side_effect=_trace_urlopen):
            probe_mod.run_audit(probe_date=date(2026, 11, 6), offline_only=False, env_override=creds)

        for url in called_paths:
            self.assertTrue(
                url.endswith("/loginByPassword") or url.endswith("/getCandleData"),
                f"Unexpected endpoint called: {url}",
            )


if __name__ == "__main__":
    unittest.main()
