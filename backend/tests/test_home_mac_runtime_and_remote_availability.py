"""
TradeLens AI - Phase 3E.5.2.3 Verification Suite:
Home Mac Always-On Runtime, Power/Sleep Policy, Private Remote Availability,
7-Signal Trading Data Safety, Security Invariants, and All 12 Failure Scenarios.
"""

from __future__ import annotations

import io
import json
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
import unittest

from backend.app.data.angel_one import AngelOneSmartAPIProvider
from backend.app.main import TradeLensAppRuntime, make_api_handler
from backend.app.runtime import cli as runtime_cli
from backend.app.runtime.frontend_server import FrontendStaticHandler
from backend.app.runtime.health_and_safety import (
    BrokerAuthState,
    EngineState,
    FeedState,
    FreshnessState,
    NetworkState,
    RuntimeHealthCoordinator,
)
from backend.app.runtime.launchd_config import HOME_ENGINE_LABEL, render_launchd_plists
from backend.app.runtime.power_policy import (
    MACBOOK_POWER_AND_LID_LIMITATIONS,
    PowerAndSleepMonitor,
    TradingHoursPolicy,
    parse_pmset_batt,
    parse_pmset_custom,
)
from backend.app.runtime.security import (
    DeviceKeyCrypto,
    RefusePublicBindError,
    RemoteAccessManager,
    RedactingLogFilter,
    is_allowed_remote_client_ip,
    redact_runtime_text,
    validate_bind_host,
)
from backend.app.runtime.supervisor import (
    DuplicateRuntimeError,
    RestartPolicy,
    SupervisedServiceSpec,
    TradeLensSupervisor,
    build_redacted_logger,
)


REPO_ROOT = Path(__file__).resolve().parents[2]


class _FakeProcess:
    _pid_seq = 50000

    def __init__(self, exit_code: Optional[int] = None) -> None:
        _FakeProcess._pid_seq += 1
        self.pid = _FakeProcess._pid_seq
        self._exit_code = exit_code
        self.terminated = False
        self.killed = False

    def crash(self, code: int = 1) -> None:
        self._exit_code = code

    def poll(self) -> Optional[int]:
        return self._exit_code

    def terminate(self) -> None:
        self.terminated = True
        if self._exit_code is None:
            self._exit_code = 0

    def wait(self, timeout: Optional[float] = None) -> Optional[int]:
        return self._exit_code

    def kill(self) -> None:
        self.killed = True
        if self._exit_code is None:
            self._exit_code = -9


class _MockClock:
    def __init__(self, start_utc: Optional[datetime] = None) -> None:
        self.utc = start_utc or datetime(2026, 11, 11, 5, 0, 0, tzinfo=timezone.utc)  # 10:30 IST
        self.mono = 1000.0

    def now_utc(self) -> datetime:
        return self.utc

    def now_mono(self) -> float:
        return self.mono

    def advance(self, wall_sec: float, mono_sec: Optional[float] = None) -> None:
        self.utc = self.utc + timedelta(seconds=wall_sec)
        self.mono += wall_sec if mono_sec is None else mono_sec


class _StubBrokerProvider:
    def __init__(self) -> None:
        self.is_authenticated = False
        self.jwt_token = None
        self.cached_quotes = {}
        self.last_fetch_time = 0.0
        self.connect_should_succeed = False
        self.quote_behavior = "SUCCESS"  # SUCCESS | EXPIRE_401 | OUTAGE_SWALLOW | RAISE_HTTP_503 | EMPTY
        self.current_ltp = 24850.0

    def connect(self) -> bool:
        if self.connect_should_succeed:
            self.is_authenticated = True
            self.jwt_token = "eyJhbGciOiJIUzUxMiJ9.stub_payload.stub_signature"
            return True
        self.is_authenticated = False
        self.jwt_token = None
        return False

    def fetch_live_quotes(self):
        if self.quote_behavior == "EXPIRE_401":
            self.is_authenticated = False
            self.jwt_token = None
            return self.cached_quotes
        if self.quote_behavior == "OUTAGE_SWALLOW":
            # Mimics AngelOneSmartAPIProvider catching network exception and returning old cache
            return self.cached_quotes
        if self.quote_behavior == "RAISE_HTTP_503":
            raise urllib.error.HTTPError("https://apiconnect.angelone.in", 503, "Service Unavailable", {}, None)
        if self.quote_behavior == "EMPTY":
            return {}
        self.last_fetch_time = time.time()
        self.cached_quotes = {
            "NIFTY 50": {"ltp": self.current_ltp, "percentChange": 0.45},
            "RELIANCE": {"ltp": 2910.0, "open": 2900.0, "high": 2925.0, "low": 2895.0, "tradeVolume": 1200000, "percentChange": 0.65},
        }
        return self.cached_quotes

    def get_indices(self):
        return [{"symbol": "NIFTY 50", "value": self.current_ltp, "change_pts": 110.0, "change_pct": 0.45, "data_status": "LIVE"}]

    def get_all_stocks(self):
        candles = []
        base = 2850.0
        for i in range(60):
            c = base + i * 1.0
            candles.append({
                "timestamp": f"2026-11-11T09:{15 + (i % 40):02d}:00+05:30",
                "open": c - 1.0,
                "high": c + 2.0,
                "low": c - 2.0,
                "close": c,
                "volume": 50000 + i * 100,
                "candle_status": "HISTORICAL",
            })
        return [
            {
                "symbol": "RELIANCE",
                "name": "Reliance Industries",
                "category": "Energy / Nifty 50",
                "lot_size": 250,
                "cmp": 2910.0,
                "live_change_pct": 0.65,
                "volume": 1200000,
                "data_status": "LIVE",
                "days_to_earnings": 30,
                "days_to_expiry": 15,
                "candles": candles,
                "fundamentals": {
                    "is_banking": False,
                    "pe": 24.0,
                    "pb": 2.9,
                    "industry_pe": 25.0,
                    "roe": 16.0,
                    "roce": 18.0,
                    "debt_to_equity": 0.4,
                    "interest_coverage": 9.0,
                    "current_ratio": 1.4,
                    "quick_ratio": 1.1,
                },
            }
        ]

    def get_live_positions(self):
        return {"positions": [], "total_pnl": 0.0, "realised_pnl": 0.0, "unrealised_pnl": 0.0, "has_live_positions": False}


class _InMemoryHTTPRequest:
    """Minimal socket adapter so BaseHTTPRequestHandler parses full HTTP wire messages in sandboxed tests."""

    def __init__(self, raw_request: bytes) -> None:
        self._rfile = io.BytesIO(raw_request)
        self._wfile = io.BytesIO()

    def makefile(self, mode: str, *args, **kwargs):
        return self._rfile if "r" in mode else self._wfile

    def sendall(self, data: bytes) -> None:
        self._wfile.write(data)

    def close(self) -> None:
        pass


class _DummyHTTPServer:
    server_name = "127.0.0.1"
    server_port = 8000


def _dispatch_handler(
    handler_cls,
    method: str,
    path: str,
    client_ip: str = "127.0.0.1",
    body: Optional[dict] = None,
    headers: Optional[dict] = None,
):
    req_headers = {"Host": "127.0.0.1:8000"}
    if headers:
        req_headers.update(headers)
    raw_body = b""
    if body is not None:
        raw_body = json.dumps(body).encode("utf-8")
        req_headers.setdefault("Content-Type", "application/json")
        req_headers["Content-Length"] = str(len(raw_body))

    lines = [f"{method} {path} HTTP/1.1"]
    for k, v in req_headers.items():
        lines.append(f"{k}: {v}")
    raw_req = ("\r\n".join(lines) + "\r\n\r\n").encode("utf-8") + raw_body
    sock = _InMemoryHTTPRequest(raw_req)
    handler_cls(sock, (client_ip, 54321), _DummyHTTPServer())
    raw_resp = sock._wfile.getvalue()
    head_bytes, _, body_bytes = raw_resp.partition(b"\r\n\r\n")
    status_line = head_bytes.decode("utf-8", errors="replace").splitlines()[0]
    status_code = int(status_line.split()[1])
    text_body = body_bytes.decode("utf-8", errors="replace")
    try:
        parsed = json.loads(text_body)
    except Exception:
        parsed = text_body
    return status_code, parsed, head_bytes.decode("utf-8", errors="replace")


class TestMacOSRuntimeAndSupervisor(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.tmp.name) / "state"
        self.state_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_launchd_plist_rendering_and_plutil_validation(self):
        out_dir = Path(self.tmp.name) / "launchd"
        paths = render_launchd_plists(out_dir, repo_root=REPO_ROOT, bind_host="127.0.0.1")
        engine_plist = paths[HOME_ENGINE_LABEL]
        text = engine_plist.read_text(encoding="utf-8")
        self.assertIn("<key>SuccessfulExit</key>", text)
        self.assertIn("<false/>", text)
        self.assertIn("<string>127.0.0.1</string>", text)
        self.assertNotIn("0.0.0.0", text)
        if shutil.which("plutil"):
            proc = subprocess.run(["plutil", "-lint", str(engine_plist)], capture_output=True, text=True, check=False)
            self.assertEqual(proc.returncode, 0, msg=proc.stderr or proc.stdout)

    def test_duplicate_process_prevention_and_stale_pid_recovery(self):
        # 1. Stale PID file from dead process recovers automatically
        stale_pid_file = self.state_dir / "supervisor.pid"
        stale_pid_file.write_text(json.dumps({"pid": 999999, "role": "supervisor"}), encoding="utf-8")

        clock = _MockClock()
        sup1 = TradeLensSupervisor(
            state_dir=self.state_dir,
            services=[SupervisedServiceSpec(name="backend", command=["sleep", "1"])],
            power_monitor=PowerAndSleepMonitor(self.state_dir / "hb1.json", wall_clock=clock.now_utc, mono_clock=clock.now_mono),
            process_spawner=lambda _spec: _FakeProcess(),
            health_probe=lambda _url: (True, "HTTP_200"),
            mono_clock=clock.now_mono,
            wall_clock=clock.now_utc,
        )
        res = sup1.startup()
        self.assertTrue(res["started"])
        self.assertTrue(res["lock"]["stale_pid_recovered"])
        self.assertEqual(res["lock"]["recovered_stale_pid"], 999999)

        # 2. Second supervisor refuses duplicate spawn while lock is active
        sup2 = TradeLensSupervisor(
            state_dir=self.state_dir,
            services=[SupervisedServiceSpec(name="backend", command=["sleep", "1"])],
            power_monitor=PowerAndSleepMonitor(self.state_dir / "hb2.json", wall_clock=clock.now_utc, mono_clock=clock.now_mono),
            process_spawner=lambda _spec: _FakeProcess(),
        )
        with self.assertRaises(DuplicateRuntimeError):
            sup2.startup()

        sup1.shutdown(owner_initiated=False, reason="TEST_END")

    def test_bounded_crash_restart_backoff_and_no_infinite_loop(self):
        clock = _MockClock()
        spawned: list[_FakeProcess] = []

        def spawner(_spec):
            p = _FakeProcess()
            spawned.append(p)
            return p

        sup = TradeLensSupervisor(
            state_dir=self.state_dir,
            services=[
                SupervisedServiceSpec(name="backend", command=["x"], health_url="http://127.0.0.1:8000/api/health"),
                SupervisedServiceSpec(name="frontend", command=["y"], health_url="http://127.0.0.1:8080/frontend-health"),
            ],
            restart_policy=RestartPolicy(max_restarts=3, window_seconds=300.0, initial_backoff_seconds=2.0, backoff_multiplier=2.0),
            power_monitor=PowerAndSleepMonitor(self.state_dir / "hb.json", wall_clock=clock.now_utc, mono_clock=clock.now_mono, cmd_runner=lambda _c: (0, "", "")),
            process_spawner=spawner,
            health_probe=lambda _url: (True, "HTTP_200"),
            mono_clock=clock.now_mono,
            wall_clock=clock.now_utc,
        )
        sup.startup()
        self.assertEqual(len(spawned), 2)

        # Crash backend repeatedly -> restarts 3 times with backoff (2s, 4s, 8s) then opens circuit
        # Restart 1
        sup.procs["backend"].crash(1)
        sup.step_once()
        self.assertEqual(sup.states["backend"].status, "RESTARTING")
        self.assertAlmostEqual(sup.states["backend"].next_restart_mono, 1002.0)
        clock.advance(2.1)
        sup.step_once()
        self.assertEqual(sup.states["backend"].status, "RUNNING")

        # Restart 2 (backoff 4s)
        sup.procs["backend"].crash(1)
        sup.step_once()
        self.assertAlmostEqual(sup.states["backend"].next_restart_mono, clock.mono + 4.0)
        clock.advance(4.1)
        sup.step_once()

        # Restart 3 (backoff 8s)
        sup.procs["backend"].crash(1)
        sup.step_once()
        self.assertAlmostEqual(sup.states["backend"].next_restart_mono, clock.mono + 8.0)
        clock.advance(8.1)
        sup.step_once()

        # 4th crash exceeds max_restarts=3 within 300s -> CIRCUIT_OPEN, never infinite loops
        sup.procs["backend"].crash(1)
        sup.step_once()
        self.assertEqual(sup.states["backend"].status, "CIRCUIT_OPEN")
        self.assertTrue(sup.states["backend"].circuit_open)
        count_when_open = len(spawned)
        clock.advance(60.0)
        sup.step_once()
        self.assertEqual(len(spawned), count_when_open)

        # Frontend crash recovers independently while backend circuit is tracked
        sup.procs["frontend"].crash(2)
        sup.step_once()
        self.assertEqual(sup.states["frontend"].status, "RESTARTING")
        clock.advance(2.1)
        sup.step_once()
        self.assertEqual(sup.states["frontend"].status, "RUNNING")

        sup.shutdown(owner_initiated=True, reason="TEST_OWNER_STOP")
        self.assertTrue((self.state_dir / "owner_shutdown.flag").exists())

        # While owner_shutdown.flag exists, subsequent launchd restart refuses to run
        sup_next = TradeLensSupervisor(
            state_dir=self.state_dir,
            services=[SupervisedServiceSpec(name="backend", command=["x"])],
            process_spawner=spawner,
        )
        res = sup_next.startup()
        self.assertFalse(res["started"])
        self.assertEqual(res["reason"], "OWNER_SHUTDOWN_FLAG_ACTIVE")


class TestPowerAndSleepBehavior(unittest.TestCase):
    SAMPLE_PMSET_CUSTOM = """
Battery Power:
 sleep 1
 displaysleep 2
 disksleep 10
AC Power:
 sleep 1
 displaysleep 10
 disksleep 10
"""

    def test_ac_vs_battery_and_display_sleep_policy(self):
        parsed = parse_pmset_custom(self.SAMPLE_PMSET_CUSTOM)
        self.assertEqual(parsed["Battery Power"]["sleep"], 1)
        self.assertEqual(parsed["AC Power"]["displaysleep"], 10)
        self.assertEqual(parse_pmset_batt("Now drawing from 'AC Power'\n -InternalBattery-0\t98%; charged;"), ("AC_POWER", 98))

        with tempfile.TemporaryDirectory() as td:
            # 10:30 IST on Wednesday (2026-11-11 05:00 UTC) + AC Power -> hold -i -s (NO -d, allows display sleep!)
            def runner_ac(cmd):
                if cmd == ["sysctl", "-n", "hw.model"]:
                    return 0, "MacBookAir10,1\n", ""
                if cmd == ["pmset", "-g", "custom"]:
                    return 0, self.SAMPLE_PMSET_CUSTOM, ""
                if cmd == ["pmset", "-g", "batt"]:
                    return 0, "Now drawing from 'AC Power'\n -InternalBattery-0\t99%;", ""
                return 0, "26.0\n", ""

            mon = PowerAndSleepMonitor(Path(td) / "hb.json", cmd_runner=runner_ac)
            ac_eval = mon.evaluate_policy(now_utc=datetime(2026, 11, 11, 5, 0, tzinfo=timezone.utc))
            self.assertTrue(ac_eval.should_hold_awake_assertion)
            self.assertTrue(ac_eval.display_sleep_allowed)
            self.assertEqual(ac_eval.caffeinate_args, ["-i", "-s"])
            self.assertNotIn("-d", ac_eval.caffeinate_args)
            self.assertFalse(ac_eval.snapshot.global_pmset_modified)
            self.assertGreaterEqual(len(ac_eval.snapshot.limitations), 2)
            self.assertEqual(ac_eval.snapshot.limitations, list(MACBOOK_POWER_AND_LID_LIMITATIONS))

            # Battery power -> withholding awake assertion & flagging degradation
            def runner_batt(cmd):
                if cmd == ["pmset", "-g", "batt"]:
                    return 0, "Now drawing from 'Battery Power'\n -InternalBattery-0\t64%;", ""
                return runner_ac(cmd)

            mon_b = PowerAndSleepMonitor(Path(td) / "hb_b.json", cmd_runner=runner_batt)
            b_eval = mon_b.evaluate_policy(now_utc=datetime(2026, 11, 11, 5, 0, tzinfo=timezone.utc))
            self.assertFalse(b_eval.should_hold_awake_assertion)
            self.assertEqual(b_eval.policy_status, "DEGRADED_ON_BATTERY_NO_AWAKE_ASSERTION")

            # Outside market hours (20:00 IST) -> released even on AC
            after_eval = mon.evaluate_policy(now_utc=datetime(2026, 11, 11, 14, 30, tzinfo=timezone.utc))
            self.assertFalse(after_eval.should_hold_awake_assertion)
            self.assertEqual(after_eval.policy_status, "OUTSIDE_TRADING_HOURS")

    def test_unexpected_sleep_and_power_loss_detection(self):
        with tempfile.TemporaryDirectory() as td:
            hb_path = Path(td) / "heartbeat.json"
            clock = _MockClock()
            sleep_callbacks = []

            mon = PowerAndSleepMonitor(
                hb_path,
                policy=TradingHoursPolicy(sleep_gap_threshold_seconds=20.0),
                wall_clock=clock.now_utc,
                mono_clock=clock.now_mono,
                on_unexpected_sleep=lambda gap, ts: sleep_callbacks.append((gap, ts)),
            )
            mon.record_tick()
            # Normal 5s tick
            clock.advance(wall_sec=5.0, mono_sec=5.0)
            res_norm = mon.record_tick()
            self.assertFalse(res_norm["sleep_detected_this_tick"])

            # Unexpected sleep: wall clock jumps 240s while monotonic jumps only 2s
            clock.advance(wall_sec=240.0, mono_sec=2.0)
            res_sleep = mon.record_tick()
            self.assertTrue(res_sleep["sleep_detected_this_tick"])
            self.assertTrue(mon.unexpected_sleep_detected)
            self.assertEqual(len(sleep_callbacks), 1)
            self.assertGreater(sleep_callbacks[0][0], 200.0)

            # Simulate sudden power loss: hb_path still has clean_shutdown=False
            mon_reboot = PowerAndSleepMonitor(hb_path)
            prior = mon_reboot.inspect_previous_shutdown()
            self.assertTrue(prior["unexpected_shutdown_detected"])


class TestFailureScenariosSecurityAndSafety(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.tmp.name) / "state"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.clock = _MockClock()

    def tearDown(self):
        self.tmp.cleanup()

    def _authenticate_office_device(
        self,
        mgr: RemoteAccessManager,
        client_ip: str = "100.64.0.25",
        ttl_seconds: Optional[int] = None,
    ):
        chal_data, chal_dec = mgr.issue_auth_challenge(
            device_id="office-mac-air",
            client_ip=client_ip,
        )
        self.assertTrue(chal_dec.allowed, chal_dec.reason_code)
        signature_b64 = DeviceKeyCrypto.sign_challenge(
            self.office_priv_pem,
            chal_data["challenge_string"],
        )
        return mgr.authenticate_remote_session(
            device_id="office-mac-air",
            challenge_id=chal_data["challenge_id"],
            signature_b64=signature_b64,
            client_ip=client_ip,
            ttl_seconds=ttl_seconds,
        )

    def _build_runtime(self, require_auth_on_loopback: bool = False):
        provider = _StubBrokerProvider()
        self.office_priv_pem, self.office_pub_pem = DeviceKeyCrypto.generate_keypair()
        hmac_secret = "HomeMacTestHmacSecret-32Bytes-2026!"
        mgr = RemoteAccessManager(
            state_path=self.state_dir / "remote_auth.json",
            owner_secret="OwnerPass!2026",
            allowed_vpn_cidrs=["10.88.0.0/24"],
            session_ttl_seconds=300,
            require_auth_on_loopback=require_auth_on_loopback,
            state_hmac_secret=hmac_secret,
            clock=self.clock.now_utc,
        )
        mgr.register_device(
            device_id="office-mac-air",
            device_name="Office Temp Mac",
            public_key_pem=self.office_pub_pem,
            owner_secret="OwnerPass!2026",
        )
        health = RuntimeHealthCoordinator(freshness_ttl_seconds=15.0, clock=self.clock.now_utc)
        power = PowerAndSleepMonitor(
            self.state_dir / "heartbeat.json",
            wall_clock=self.clock.now_utc,
            mono_clock=self.clock.now_mono,
            cmd_runner=lambda c: (0, "Now drawing from 'AC Power'\n -InternalBattery-0\t100%;", "")
            if c == ["pmset", "-g", "batt"]
            else (0, "MacBookAir10,1\n", ""),
        )
        rt = TradeLensAppRuntime(
            state_dir=self.state_dir,
            provider=provider,
            remote_access=mgr,
            health_coordinator=health,
            power_monitor=power,
            allow_sample_fallback=False,
            freshness_ttl_seconds=15.0,
            environ={
                "ANGEL_API_KEY": "real_api_key_secret_abc",
                "ANGEL_CLIENT_CODE": "CLIENT_XYZ",
                "ANGEL_PASSWORD": "super_secret_password_123",
                "ANGEL_TOTP_SECRET": "JBSWY3DPEHPK3PXP",
                "TRADELENS_OWNER_SECRET": "OwnerPass!2026",
                "TRADELENS_STATE_HMAC_SECRET": hmac_secret,
                "TRADELENS_ASSUME_ONLINE_TEST": "1",
            },
        )
        # Re-link sleep callback explicitly since custom PowerAndSleepMonitor passed in
        power._on_unexpected_sleep = rt._handle_unexpected_sleep
        return rt, provider, mgr, health, power

    def test_network_bind_and_vpn_isolation_security(self):
        # Refuse public listeners
        for bad in ("0.0.0.0", "::", "8.8.8.8", "example.com", ""):
            with self.assertRaises(RefusePublicBindError):
                validate_bind_host(bad)

        # Allow loopback & Tailscale CGNAT / configured WireGuard
        self.assertEqual(validate_bind_host("127.0.0.1"), "127.0.0.1")
        self.assertEqual(validate_bind_host("100.64.10.20"), "100.64.10.20")
        self.assertEqual(validate_bind_host("10.88.0.1", allowed_vpn_cidrs=["10.88.0.0/24"]), "10.88.0.1")

        # Client IP check blocks random home Wi-Fi or public IPs while allowing Tailscale / custom WireGuard
        self.assertTrue(is_allowed_remote_client_ip("100.100.5.9")[0])
        self.assertTrue(is_allowed_remote_client_ip("10.88.0.5", ["10.88.0.0/24"])[0])
        self.assertFalse(is_allowed_remote_client_ip("192.168.1.50")[0])
        self.assertFalse(is_allowed_remote_client_ip("8.8.4.4")[0])

    def test_remote_auth_separate_from_vpn_and_revocation_and_proxy_guard(self):
        rt, provider, mgr, health, _ = self._build_runtime()
        # Even over trusted Tailscale IP 100.64.0.25, missing app token -> 401 AUTH_REQUIRED
        d_unauth = mgr.authorize_request("/api/scan", "GET", "100.64.0.25", headers={})
        self.assertFalse(d_unauth.allowed)
        self.assertEqual(d_unauth.reason_code, "AUTH_REQUIRED")

        # Proxied Tailscale Serve request over loopback MUST still require Bearer auth
        d_proxied = mgr.authorize_request(
            "/api/scan",
            "GET",
            "127.0.0.1",
            headers={"X-Forwarded-For": "100.64.0.25", "Tailscale-User-Login": "user@example.com"},
        )
        self.assertFalse(d_proxied.allowed)
        self.assertEqual(d_proxied.reason_code, "AUTH_REQUIRED")

        # Authenticate office Mac via asymmetric challenge-response -> get short-lived token
        sess_data, auth_dec = self._authenticate_office_device(
            mgr,
            client_ip="100.64.0.25",
            ttl_seconds=120,
        )
        self.assertTrue(auth_dec.allowed)
        token = sess_data["access_token"]
        self.assertNotIn("ANGEL_API_KEY", json.dumps(sess_data))

        # Allowed read-only assistance endpoint succeeds remotely
        d_scan = mgr.authorize_request("/api/scan", "GET", "100.64.0.25", headers={"Authorization": f"Bearer {token}"})
        self.assertTrue(d_scan.allowed)

        # Owner-only shutdown/broker-auth/arbitrary shell endpoints strictly rejected for remote session
        for forbidden in ("/api/runtime/shutdown", "/api/broker/authenticate", "/api/shell", "/api/health/../runtime/shutdown"):
            dec = mgr.authorize_request(forbidden, "POST", "100.64.0.25", headers={"Authorization": f"Bearer {token}"})
            self.assertFalse(dec.allowed, msg=f"Should reject {forbidden}")

        # Office Mac sleep -> session expires after TTL (120s) -> rejected SESSION_EXPIRED
        self.clock.advance(125.0)
        d_expired = mgr.authorize_request("/api/scan", "GET", "100.64.0.25", headers={"Authorization": f"Bearer {token}"})
        self.assertFalse(d_expired.allowed)
        self.assertEqual(d_expired.reason_code, "SESSION_EXPIRED")

        # Re-issue token, then revoke office device -> immediate 403 DEVICE_REVOKED
        sess2, _ = self._authenticate_office_device(mgr, client_ip="100.64.0.25")
        tok2 = sess2["access_token"]
        revoked_cnt = mgr.revoke_device("office-mac-air", owner_secret="OwnerPass!2026", reason="WORKSTATION_LOST")
        self.assertGreaterEqual(revoked_cnt, 1)
        d_revoked = mgr.authorize_request("/api/scan", "GET", "100.64.0.25", headers={"Authorization": f"Bearer {tok2}"})
        self.assertFalse(d_revoked.allowed)
        self.assertEqual(d_revoked.status_code, 403)
        self.assertEqual(d_revoked.reason_code, "DEVICE_REVOKED")

    def test_all_12_failure_scenarios_and_7_safety_signals(self):
        rt, provider, mgr, health, power = self._build_runtime()
        health.mark_vpn_status(True, "Tailscale peer reachable.")
        self._authenticate_office_device(mgr, client_ip="100.64.0.25")

        # 1. Home Mac reboot: NEVER assumes Angel One auth valid, NEVER auto-places orders, office session != LIVE data
        snap_boot = rt.get_health_payload()
        self.assertEqual(snap_boot["engine_status"], EngineState.UNAVAILABLE)
        self.assertEqual(snap_boot["data_status"], "UNAVAILABLE")
        self.assertFalse(snap_boot["order_execution_enabled"])
        self.assertEqual(snap_boot["signals"]["broker_auth_status"], BrokerAuthState.EXPLICIT_AUTH_REQUIRED)
        self.assertEqual(snap_boot["signals"]["remote_client_connectivity"]["active_remote_sessions"], 1)
        self.assertFalse(snap_boot["signals"]["remote_client_connectivity"]["proves_live_market_data"])

        # Explicit owner broker auth with valid credentials + fresh tick -> LIVE
        provider.connect_should_succeed = True
        rt.authenticate_broker(verify_fresh_tick=True)
        # Ensure tick recorded under mock clock
        health.record_market_data_tick(now_utc=self.clock.now_utc())
        snap_live = rt.get_health_payload()
        self.assertEqual(snap_live["engine_status"], EngineState.LIVE)
        self.assertEqual(snap_live["data_status"], "LIVE")
        self.assertEqual(snap_live["signals"]["market_data_freshness"], FreshnessState.FRESH)

        # 2. Stale market data NEVER shows LIVE
        self.clock.advance(20.0, 20.0)  # TTL is 15s
        snap_stale = rt.get_health_payload()
        self.assertNotEqual(snap_stale["data_status"], "LIVE")
        self.assertEqual(snap_stale["data_status"], "STALE")
        self.assertEqual(snap_stale["engine_status"], EngineState.DEGRADED)
        self.assertTrue(any("STALE_MARKET_DATA" in r for r in snap_stale["failure_reasons"]))

        # Restore fresh tick before sleep test
        health.record_market_data_tick(now_utc=self.clock.now_utc())

        # 3. Home Mac unexpected sleep -> cache cleared & feed invalidated
        self.clock.advance(wall_sec=180.0, mono_sec=1.0)
        snap_sleep = rt.get_health_payload()
        self.assertNotEqual(snap_sleep["data_status"], "LIVE")
        self.assertTrue(power.unexpected_sleep_detected)
        self.assertTrue(any("HOME_SLEEP_DETECTED" in r for r in snap_sleep["failure_reasons"]))
        self.assertEqual(provider.cached_quotes, {})

        # 4 & 12. Internet loss -> UNAVAILABLE, then internet recovery -> still requires fresh tick before LIVE!
        health.mark_internet_status(False, "ISP link down.")
        snap_net_down = rt.get_health_payload()
        self.assertEqual(snap_net_down["engine_status"], EngineState.UNAVAILABLE)
        self.assertEqual(snap_net_down["signals"]["network_connectivity"], NetworkState.DISCONNECTED)
        health.mark_internet_status(True, "ISP reconnected.")
        snap_net_recover_pending = rt.get_health_payload()
        self.assertNotEqual(snap_net_recover_pending["engine_status"], EngineState.LIVE)
        rt.authenticate_broker(verify_fresh_tick=True)
        health.record_market_data_tick(now_utc=self.clock.now_utc())
        self.assertEqual(rt.get_health_payload()["engine_status"], EngineState.LIVE)

        # 5. Angel One auth expiry -> immediately drops out of LIVE
        provider.quote_behavior = "EXPIRE_401"
        rt._refresh_live_quotes_locked()
        snap_exp = rt.get_health_payload()
        self.assertEqual(snap_exp["signals"]["broker_auth_status"], BrokerAuthState.EXPIRED)
        self.assertEqual(snap_exp["engine_status"], EngineState.UNAVAILABLE)

        # 6. Broker API outage (including swallowed stale-cache bug in provider) -> API_OUTAGE / DEGRADED
        provider.quote_behavior = "SUCCESS"
        rt.authenticate_broker(verify_fresh_tick=True)
        provider.last_fetch_time = time.time() - 5.0  # stale > 2.5s window
        provider.quote_behavior = "OUTAGE_SWALLOW"
        ok_refresh = rt._refresh_live_quotes_locked()
        self.assertFalse(ok_refresh)
        self.assertEqual(health.feed_connectivity, FeedState.API_OUTAGE)
        self.assertNotEqual(rt.get_health_payload()["engine_status"], EngineState.LIVE)

        # 7 & 8. Frontend crash surfaced cleanly; VPN disconnect surfaced cleanly
        provider.quote_behavior = "SUCCESS"
        rt.authenticate_broker(verify_fresh_tick=True)
        health.record_market_data_tick(now_utc=self.clock.now_utc())
        health.mark_frontend_status(False, "Frontend static HTTP exited 1.")
        snap_fe = rt.get_health_payload()
        self.assertEqual(snap_fe["engine_status"], EngineState.DEGRADED)
        self.assertFalse(snap_fe["services"]["frontend_healthy"])
        health.mark_frontend_status(True)

        health.mark_vpn_status(False, "Tailscale daemon down.")
        snap_vpn = rt.get_health_payload()
        self.assertFalse(snap_vpn["remote_available"])
        self.assertTrue(any("VPN_DISCONNECTED" in r for r in snap_vpn["failure_reasons"]))

    def test_real_http_handlers_redaction_cli_and_frontend_storage(self):
        rt, provider, mgr, health, _ = self._build_runtime(require_auth_on_loopback=True)
        api_handler = make_api_handler(rt)

        # Unauthenticated remote /api/scan over Tailscale IP rejected with AUTH_REQUIRED
        st_unauth, body_unauth, _ = _dispatch_handler(api_handler, "GET", "/api/scan", client_ip="100.64.0.25")
        self.assertEqual(st_unauth, 401)
        self.assertEqual(body_unauth["reason_code"], "AUTH_REQUIRED")

        # Random non-VPN Wi-Fi IP rejected at transport boundary
        st_wifi, body_wifi, _ = _dispatch_handler(api_handler, "GET", "/api/scan", client_ip="192.168.1.99")
        self.assertEqual(st_wifi, 403)
        self.assertEqual(body_wifi["reason_code"], "UNTRUSTED_NETWORK")

        # Remote auth challenge + asymmetric signature handshake returns short-lived token and ZERO broker credentials
        st_chal, body_chal, _ = _dispatch_handler(
            api_handler,
            "POST",
            "/api/remote/challenge",
            client_ip="100.64.0.25",
            body={"device_id": "office-mac-air"},
        )
        self.assertEqual(st_chal, 200, body_chal)
        sig_b64 = DeviceKeyCrypto.sign_challenge(self.office_priv_pem, body_chal["challenge_string"])
        st_auth, body_auth, resp_hdrs = _dispatch_handler(
            api_handler,
            "POST",
            "/api/remote/auth",
            client_ip="100.64.0.25",
            body={
                "device_id": "office-mac-air",
                "challenge_id": body_chal["challenge_id"],
                "signature_b64": sig_b64,
            },
        )
        self.assertEqual(st_auth, 200)
        self.assertIn("Cache-Control: no-store", resp_hdrs)
        token = body_auth["access_token"]
        raw_auth_text = json.dumps(body_auth)
        for secret in ("real_api_key_secret_abc", "super_secret_password_123", "JBSWY3DPEHPK3PXP", "OwnerPass!2026"):
            self.assertNotIn(secret, raw_auth_text)

        # /api/scan with Bearer works and stays UNAVAILABLE until broker explicitly unlocked
        st_scan, body_scan, _ = _dispatch_handler(
            api_handler,
            "GET",
            "/api/scan",
            client_ip="100.64.0.25",
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(st_scan, 200)
        self.assertEqual(body_scan["data_status"], "UNAVAILABLE")
        self.assertEqual(body_scan["engine_status"], "UNAVAILABLE")

        # Remote session attempting Home Mac owner shutdown is forbidden
        st_shut, body_shut, _ = _dispatch_handler(
            api_handler,
            "POST",
            "/api/runtime/shutdown",
            client_ip="100.64.0.25",
            headers={"Authorization": f"Bearer {token}"},
            body={},
        )
        self.assertEqual(st_shut, 403)
        self.assertEqual(body_shut["reason_code"], "HOME_OWNER_ONLY_OPERATION")

        # Heartbeat works for office client but does NOT claim market data is LIVE
        st_hb, body_hb, _ = _dispatch_handler(
            api_handler,
            "POST",
            "/api/remote/heartbeat",
            client_ip="100.64.0.25",
            headers={"Authorization": f"Bearer {token}"},
            body={},
        )
        self.assertEqual(st_hb, 200)
        self.assertFalse(body_hb["proves_live_market_data"])
        self.assertNotEqual(body_hb["health"]["data_status"], "LIVE")

        # Revoking remote session via logout rejects subsequent calls cleanly
        st_out, _, _ = _dispatch_handler(
            api_handler,
            "POST",
            "/api/remote/logout",
            client_ip="100.64.0.25",
            headers={"Authorization": f"Bearer {token}"},
            body={},
        )
        self.assertEqual(st_out, 200)
        st_after_out, body_after_out, _ = _dispatch_handler(
            api_handler,
            "GET",
            "/api/scan",
            client_ip="100.64.0.25",
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(st_after_out, 401)
        self.assertEqual(body_after_out["reason_code"], "SESSION_REVOKED")

        # Home Mac owner loopback call WITH owner secret can unlock broker explicitly
        provider.connect_should_succeed = True
        st_broker, body_broker, _ = _dispatch_handler(
            api_handler,
            "POST",
            "/api/broker/authenticate",
            client_ip="127.0.0.1",
            headers={"X-TradeLens-Owner-Secret": "OwnerPass!2026"},
            body={"verify_fresh_tick": True},
        )
        self.assertEqual(st_broker, 200)
        self.assertEqual(body_broker["signals"]["broker_auth_status"], BrokerAuthState.AUTHENTICATED)

        # Log redaction filter scrubs Angel One & bearer/owner secrets cleanly
        scrubbed = redact_runtime_text(
            f"ANGEL_API_KEY=real_api_key_secret_abc Bearer {token} owner_secret=OwnerPass!2026",
            extra_secrets=mgr.active_ephemeral_secrets(),
            environ=rt.environ,
        )
        self.assertNotIn("real_api_key_secret_abc", scrubbed)
        self.assertNotIn(token, scrubbed)
        self.assertNotIn("OwnerPass!2026", scrubbed)

        # CLI device registration/revocation/owner-shutdown flow works end-to-end
        cli_state = Path(self.tmp.name) / "cli_state"
        pub_out = Path(self.tmp.name) / "office_cli_1.pub.pem"
        _, cli_pub_pem = DeviceKeyCrypto.generate_keypair()
        pub_out.write_text(cli_pub_pem, encoding="utf-8")
        rc_reg = runtime_cli.main([
            "--state-dir", str(cli_state),
            "register-device",
            "--device-id", "office-cli-1",
            "--device-name", "Office Air",
            "--public-key-file", str(pub_out),
            "--owner-secret", "OwnerPass!2026",
        ])
        self.assertEqual(rc_reg, 0)
        rc_rev = runtime_cli.main([
            "--state-dir", str(cli_state),
            "revoke-device",
            "--device-id", "office-cli-1",
            "--owner-secret", "OwnerPass!2026",
        ])
        self.assertEqual(rc_rev, 0)
        rc_down = runtime_cli.main(["--state-dir", str(cli_state), "owner-shutdown"])
        self.assertEqual(rc_down, 0)
        self.assertTrue((cli_state / "owner_shutdown.flag").exists())
        rc_res = runtime_cli.main(["--state-dir", str(cli_state), "resume"])
        self.assertEqual(rc_res, 0)
        self.assertFalse((cli_state / "owner_shutdown.flag").exists())

        # Verify frontend/index.html contains zero hardcoded Angel One secrets and keeps tokens strictly in memory
        html_text = (REPO_ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
        self.assertNotIn("ANGEL_API_KEY", html_text)
        self.assertNotIn("localStorage.setItem('remoteSessionToken", html_text)
        self.assertNotIn("sessionStorage.setItem", html_text)
        self.assertIn("kept strictly in page memory", html_text)
        self.assertIn("let remoteSessionToken = null;", html_text)

        # Verify standalone frontend handler rejects path traversal & untrusted IPs and exposes /frontend-health
        st_fe_ok, fe_body, _ = _dispatch_handler(FrontendStaticHandler, "GET", "/frontend-health", client_ip="127.0.0.1")
        self.assertEqual(st_fe_ok, 200)
        self.assertEqual(fe_body["status"], "ok")
        st_fe_404, _, _ = _dispatch_handler(FrontendStaticHandler, "GET", "/../.env", client_ip="127.0.0.1")
        self.assertEqual(st_fe_404, 404)
        st_fe_net, fe_net_body, _ = _dispatch_handler(FrontendStaticHandler, "GET", "/", client_ip="192.168.1.40")
        self.assertEqual(st_fe_net, 403)
        self.assertEqual(fe_net_body["reason_code"], "UNTRUSTED_NETWORK")

    def test_db_corruption_fails_closed_and_redacted_file_logging(self):
        corrupt_ledger = Path(self.tmp.name) / "broken_ledger.json"
        corrupt_ledger.write_text("{invalid json", encoding="utf-8")
        missing_hist = Path(self.tmp.name) / "missing_hist"

        coord = RuntimeHealthCoordinator()
        steps = coord.verify_startup_dependencies(
            ledger_path=corrupt_ledger,
            historical_dir=missing_hist,
            environ={"ANGEL_API_KEY": ""},
        )
        step_map = {s.name: s for s in steps}
        self.assertFalse(step_map["database_and_storage_access"].passed)
        snap = coord.evaluate()
        self.assertEqual(snap["engine_status"], EngineState.UNAVAILABLE)
        self.assertTrue(any("CALCULATION_OR_DB_FAILURE" in r for r in snap["failure_reasons"]))

        log_file = Path(self.tmp.name) / "redacted_test.log"
        logger = build_redacted_logger(
            "test.redaction.file",
            log_file,
            extra_secrets_provider=lambda: ["secret_session_tok_xyz"],
        )
        logger.error("Auth failed: jwtToken=eyJabc123456.eyJdef67890.sig99 token=secret_session_tok_xyz ANGEL_PASSWORD=mysecretpw")
        for h in logger.handlers:
            h.flush()
        disk_text = log_file.read_text(encoding="utf-8")
        self.assertNotIn("secret_session_tok_xyz", disk_text)
        self.assertNotIn("mysecretpw", disk_text)
        self.assertNotIn("eyJabc123456.eyJdef67890.sig99", disk_text)


if __name__ == "__main__":
    unittest.main()
