"""
Phase 3E.5.2.4: Independent Remote Access Security Audit & Hardening Verification Suite.

Strictly offline tests (no Tailscale/WireGuard installation, no LaunchAgent bootstrap,
no live Angel One network calls, no public port binding, no order execution).
Covers asymmetric ECDSA P-256 device authentication, challenge-response single-use & expiry,
device approval & revocation lifecycle persistence, rate limiting, session IP binding,
HTTP Host/Origin/CORS/proxy/URL-token/method/path-traversal guards, CSP nonce & XSS escaping,
public bind refusal, credential/JWT/PEM redaction, Angel One bounded non-recursive reconnect,
macOS Keychain command formatting (mocked), and TLS server wrapping.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import re
import socket
import ssl
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.error import HTTPError

from backend.app.data.angel_one import AngelOneSmartAPIProvider
from backend.app.main import TradeLensAppRuntime, build_api_server, make_api_handler
from backend.app.runtime.frontend_server import FrontendStaticHandler, build_frontend_server
from backend.app.runtime.health_and_safety import RuntimeHealthCoordinator
from backend.app.runtime.power_policy import PowerAndSleepMonitor
from backend.app.runtime.security import (
    AuthRateLimiter,
    DEVICE_STATUS_ACTIVE,
    DEVICE_STATUS_PENDING,
    DEVICE_STATUS_REVOKED,
    DeviceKeyCrypto,
    MacOSKeychainKeyStore,
    RedactingLogFilter,
    RefusePublicBindError,
    RemoteAccessManager,
    redact_payload,
    redact_runtime_text,
    wrap_server_tls,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
INDEX_HTML = REPO_ROOT / "frontend" / "index.html"


class _FakeClock:
    def __init__(self, start_iso: str = "2026-11-11T04:30:00+00:00") -> None:
        self._now = datetime.fromisoformat(start_iso)

    def now_utc(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> datetime:
        self._now = self._now + timedelta(seconds=seconds)
        return self._now


class _InMemoryHTTPRequest:
    def __init__(self, raw_request: bytes) -> None:
        self._rfile = io.BytesIO(raw_request)
        self._wfile = io.BytesIO()

    def makefile(self, mode: str, *args: Any, **kwargs: Any) -> io.BytesIO:
        if "r" in mode:
            return self._rfile
        return self._wfile

    def sendall(self, data: bytes) -> None:
        self._wfile.write(data)

    def close(self) -> None:
        return


class _DummyServer:
    server_name = "127.0.0.1"
    server_port = 8000


def _dispatch_http(
    handler_cls: type,
    method: str,
    path: str,
    client_ip: str = "127.0.0.1",
    headers: Optional[Dict[str, str]] = None,
    body: Optional[Dict[str, Any]] = None,
) -> Tuple[int, Any, Dict[str, str], List[str]]:
    hdrs = dict(headers or {})
    if not any(k.lower() == "host" for k in hdrs):
        hdrs["Host"] = "127.0.0.1:8000"

    raw_body = b""
    if body is not None:
        raw_body = json.dumps(body).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
        hdrs["Content-Length"] = str(len(raw_body))

    lines = [f"{method} {path} HTTP/1.1"]
    for k, v in hdrs.items():
        lines.append(f"{k}: {v}")
    raw_req = ("\r\n".join(lines) + "\r\n\r\n").encode("utf-8") + raw_body

    sock = _InMemoryHTTPRequest(raw_req)
    handler_cls(sock, (client_ip, 54321), _DummyServer())
    raw_resp = sock._wfile.getvalue().decode("utf-8", errors="replace")
    head, _, resp_body = raw_resp.partition("\r\n\r\n")
    head_lines = head.split("\r\n")
    status_code = int(head_lines[0].split(" ", 2)[1])
    resp_header_lines = head_lines[1:]
    resp_headers: Dict[str, str] = {}
    for line in resp_header_lines:
        if ":" in line:
            hk, hv = line.split(":", 1)
            resp_headers[hk.strip().lower()] = hv.strip()

    content_type = resp_headers.get("content-type", "")
    parsed_body: Any = resp_body
    if "application/json" in content_type and resp_body.strip():
        parsed_body = json.loads(resp_body)
    return status_code, parsed_body, resp_headers, resp_header_lines


class _DummyProvider:
    broker_name = "AngelOneSmartAPI"
    api_key = "BROKER_KEY_XYZ"
    client_code = "BROKER_CLIENT_99"
    password = "BROKER_PASS_SECRET"
    totp_secret = "JBSWY3DPEHPK3PXP"
    jwt_token = "eyJ_TEST_JWT_SECRET"
    refresh_token = "eyJ_TEST_REFRESH_SECRET"
    feed_token = "eyJ_TEST_FEED_SECRET"
    connected = False
    last_error: Optional[str] = None

    def connect(self) -> bool:
        self.connected = True
        return True

    def get_indices(self) -> List[Dict[str, Any]]:
        return [{"name": "NIFTY 50", "ltp": 24500.0, "data_status": "UNAVAILABLE"}]

    def get_all_stocks(self) -> List[Dict[str, Any]]:
        return []

    def get_live_positions(self) -> Dict[str, Any]:
        return {"positions": [], "data_status": "UNAVAILABLE"}

    def get_quote(self, symbol: str) -> Dict[str, Any]:
        return {"symbol": symbol, "ltp": 0.0}

    def get_candles(self, symbol: str, interval: str = "ONE_DAY", limit: int = 60) -> List[Dict[str, Any]]:
        return []


class TestRemoteAccessSecurityAudit(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.tmp.name)
        self.clock = _FakeClock()
        self.office_priv, self.office_pub = DeviceKeyCrypto.generate_keypair()
        self.other_priv, self.other_pub = DeviceKeyCrypto.generate_keypair()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _new_manager(
        self,
        *,
        owner_secret: str = "StrongOwnerPass!2026",
        allowed_vpn_cidrs: Tuple[str, ...] = ("100.64.0.0/10",),
        allowed_hosts: Tuple[str, ...] = ("home-mac.tailnet.ts.net",),
        allowed_origins: Tuple[str, ...] = ("http://100.64.0.10:8080", "https://home-mac.tailnet.ts.net"),
        state_hmac_secret: Optional[str] = "AuditTestStateHmacSecret-2026-32Bytes!",
        remote_mode_enabled: Optional[bool] = None,
        max_auth_failures: int = 5,
        auth_failure_window_seconds: int = 300,
        lockout_seconds: int = 300,
    ) -> RemoteAccessManager:
        return RemoteAccessManager(
            state_path=self.state_dir / "remote_auth.json",
            owner_secret=owner_secret,
            allowed_vpn_cidrs=list(allowed_vpn_cidrs),
            allowed_hosts=list(allowed_hosts),
            allowed_origins=list(allowed_origins),
            session_ttl_seconds=300,
            challenge_ttl_seconds=60,
            max_auth_failures=max_auth_failures,
            auth_failure_window_seconds=auth_failure_window_seconds,
            lockout_seconds=lockout_seconds,
            clock=self.clock.now_utc,
            state_hmac_secret=state_hmac_secret,
            remote_mode_enabled=remote_mode_enabled,
        )

    def _register_office(self, mgr: RemoteAccessManager) -> None:
        mgr.register_device(
            device_id="office-mac-air",
            device_name="Office MacBook Air",
            public_key_pem=self.office_pub,
            owner_secret="StrongOwnerPass!2026",
            approve=True,
        )

    def _issue_and_sign(
        self,
        mgr: RemoteAccessManager,
        *,
        device_id: str = "office-mac-air",
        client_ip: str = "100.64.0.25",
        priv_pem: Optional[str] = None,
    ) -> Tuple[Dict[str, Any], str]:
        chal, dec = mgr.issue_auth_challenge(device_id=device_id, client_ip=client_ip)
        self.assertTrue(dec.allowed, dec.reason_code)
        sig = DeviceKeyCrypto.sign_challenge(priv_pem or self.office_priv, chal["challenge_string"])
        return chal, sig

    def _build_api_and_frontend_handlers(self, mgr: RemoteAccessManager):
        provider = _DummyProvider()
        health = RuntimeHealthCoordinator(freshness_ttl_seconds=15.0, clock=self.clock.now_utc)
        power = PowerAndSleepMonitor(
            self.state_dir / "hb.json",
            wall_clock=self.clock.now_utc,
            cmd_runner=lambda c: (0, "Now drawing from 'AC Power'\n", ""),
        )
        rt = TradeLensAppRuntime(
            state_dir=self.state_dir,
            provider=provider,
            remote_access=mgr,
            health_coordinator=health,
            power_monitor=power,
            allow_sample_fallback=False,
            environ={
                "ANGEL_API_KEY": provider.api_key,
                "ANGEL_CLIENT_CODE": provider.client_code,
                "ANGEL_PASSWORD": provider.password,
                "ANGEL_TOTP_SECRET": provider.totp_secret,
                "TRADELENS_OWNER_SECRET": "StrongOwnerPass!2026",
            },
        )
        api_handler = make_api_handler(rt)

        class _TestFrontendHandler(FrontendStaticHandler):
            allowed_vpn_cidrs = list(mgr.allowed_vpn_cidrs)
            allowed_hosts = list(mgr.allowed_hosts)
            allowed_origins = list(mgr.allowed_origins)
            backend_port = 8000
            index_path = INDEX_HTML

        return rt, provider, api_handler, _TestFrontendHandler

    def test_01_invalid_device_signature_rejected(self) -> None:
        mgr = self._new_manager()
        self._register_office(mgr)
        chal, dec = mgr.issue_auth_challenge("office-mac-air", "100.64.0.25")
        self.assertTrue(dec.allowed)

        # Signed with attacker's unrelated ECDSA keypair
        wrong_sig = DeviceKeyCrypto.sign_challenge(self.other_priv, chal["challenge_string"])
        sess1, d1 = mgr.authenticate_remote_session(
            device_id="office-mac-air",
            challenge_id=chal["challenge_id"],
            signature_b64=wrong_sig,
            client_ip="100.64.0.25",
        )
        self.assertIsNone(sess1)
        self.assertFalse(d1.allowed)
        self.assertEqual(d1.status_code, 401)
        self.assertEqual(d1.reason_code, "INVALID_DEVICE_SIGNATURE")

        # Tampered base64 signature rejected on fresh challenge
        chal2, good_sig = self._issue_and_sign(mgr)
        tampered = base64.b64encode(b"tampered-signature-bytes").decode("ascii")
        sess2, d2 = mgr.authenticate_remote_session(
            device_id="office-mac-air",
            challenge_id=chal2["challenge_id"],
            signature_b64=tampered,
            client_ip="100.64.0.25",
        )
        self.assertNotEqual(good_sig, tampered)
        self.assertIsNone(sess2)
        self.assertEqual(d2.status_code, 401)
        self.assertEqual(d2.reason_code, "INVALID_DEVICE_SIGNATURE")

    def test_02_unknown_and_pending_devices_rejected_until_owner_approved(self) -> None:
        mgr = self._new_manager()
        _, api_handler, = None, None
        _, _, api_handler, _ = self._build_api_and_frontend_handlers(mgr)

        # Unregistered device rejected for challenge and auth
        chal_unk, d_unk = mgr.issue_auth_challenge("unknown-mac", "100.64.0.25")
        self.assertIsNone(chal_unk)
        self.assertEqual(d_unk.status_code, 403)
        self.assertEqual(d_unk.reason_code, "UNREGISTERED_DEVICE")

        # Pending device cannot issue challenges until explicit owner approval
        pending_rec = mgr.request_device_registration("pending-mac", "Pending Office", self.office_pub)
        self.assertEqual(pending_rec.status, DEVICE_STATUS_PENDING)
        chal_pend, d_pend = mgr.issue_auth_challenge("pending-mac", "100.64.0.25")
        self.assertIsNone(chal_pend)
        self.assertEqual(d_pend.status_code, 403)
        self.assertEqual(d_pend.reason_code, "DEVICE_PENDING_APPROVAL")

        # Server must NEVER accept private key material on device registration
        with self.assertRaises(ValueError):
            mgr.register_device("bad-dev", "Bad", self.office_priv, "StrongOwnerPass!2026")

        # Remote peer CANNOT approve or register devices
        st_rem, body_rem, _, _ = _dispatch_http(
            api_handler,
            "POST",
            "/api/remote/devices/approve",
            client_ip="100.64.0.25",
            body={"device_id": "pending-mac", "owner_secret": "StrongOwnerPass!2026"},
        )
        self.assertEqual(st_rem, 403)
        self.assertEqual(body_rem["reason_code"], "HOME_OWNER_ONLY_OPERATION")

        # Home owner loopback WITH owner secret approves device -> becomes ACTIVE
        st_app, body_app, _, _ = _dispatch_http(
            api_handler,
            "POST",
            "/api/remote/devices/approve",
            client_ip="127.0.0.1",
            headers={"X-TradeLens-Owner-Secret": "StrongOwnerPass!2026"},
            body={"device_id": "pending-mac", "activate": True},
        )
        self.assertEqual(st_app, 200)
        self.assertEqual(body_app["device_status"], DEVICE_STATUS_ACTIVE)

    def test_03_replayed_and_bound_challenge_nonce_rejected(self) -> None:
        mgr = self._new_manager()
        self._register_office(mgr)
        mgr.register_device("office-mac-2", "Office 2", self.other_pub, "StrongOwnerPass!2026", approve=True)

        chal, sig = self._issue_and_sign(mgr, client_ip="100.64.0.25")

        # Challenge bound to client IP 100.64.0.25 fails if redeemed from 100.64.0.26
        sess_wrong_ip, d_wrong_ip = mgr.authenticate_remote_session(
            device_id="office-mac-air",
            challenge_id=chal["challenge_id"],
            signature_b64=sig,
            client_ip="100.64.0.26",
        )
        self.assertIsNone(sess_wrong_ip)
        self.assertEqual(d_wrong_ip.status_code, 401)
        self.assertEqual(d_wrong_ip.reason_code, "CHALLENGE_BINDING_MISMATCH")

        # Failed binding mismatch also burns the challenge; issue fresh challenge for valid redemption
        chal_ok, sig_ok = self._issue_and_sign(mgr, client_ip="100.64.0.25")
        sess_ok, d_ok = mgr.authenticate_remote_session(
            device_id="office-mac-air",
            challenge_id=chal_ok["challenge_id"],
            signature_b64=sig_ok,
            client_ip="100.64.0.25",
        )
        self.assertTrue(d_ok.allowed)
        self.assertIsNotNone(sess_ok)

        # Immediate replay of exact same challenge_id rejected CHALLENGE_REPLAYED
        sess_replay, d_replay = mgr.authenticate_remote_session(
            device_id="office-mac-air",
            challenge_id=chal_ok["challenge_id"],
            signature_b64=sig_ok,
            client_ip="100.64.0.25",
        )
        self.assertIsNone(sess_replay)
        self.assertEqual(d_replay.status_code, 401)
        self.assertEqual(d_replay.reason_code, "CHALLENGE_REPLAYED")

    def test_04_expired_challenge_rejected(self) -> None:
        mgr = self._new_manager()
        self._register_office(mgr)
        chal, sig = self._issue_and_sign(mgr)
        self.clock.advance(61.0)

        sess, dec = mgr.authenticate_remote_session(
            device_id="office-mac-air",
            challenge_id=chal["challenge_id"],
            signature_b64=sig,
            client_ip="100.64.0.25",
        )
        self.assertIsNone(sess)
        self.assertEqual(dec.status_code, 401)
        self.assertEqual(dec.reason_code, "CHALLENGE_EXPIRED")

    def test_05_expired_session_rejected(self) -> None:
        mgr = self._new_manager()
        self._register_office(mgr)
        chal, sig = self._issue_and_sign(mgr)
        sess, dec = mgr.authenticate_remote_session(
            device_id="office-mac-air",
            challenge_id=chal["challenge_id"],
            signature_b64=sig,
            client_ip="100.64.0.25",
            ttl_seconds=60,
        )
        self.assertTrue(dec.allowed)
        token = sess["access_token"]

        ok_now = mgr.authorize_request("/api/scan", "GET", "100.64.0.25", headers={"Authorization": f"Bearer {token}"})
        self.assertTrue(ok_now.allowed)

        self.clock.advance(65.0)
        expired = mgr.authorize_request("/api/scan", "GET", "100.64.0.25", headers={"Authorization": f"Bearer {token}"})
        self.assertFalse(expired.allowed)
        self.assertEqual(expired.status_code, 401)
        self.assertEqual(expired.reason_code, "SESSION_EXPIRED")

    def test_06_revoked_device_blocks_sessions_challenges_and_silent_reenroll(self) -> None:
        mgr = self._new_manager()
        self._register_office(mgr)
        chal, sig = self._issue_and_sign(mgr)
        sess, _ = mgr.authenticate_remote_session(
            device_id="office-mac-air",
            challenge_id=chal["challenge_id"],
            signature_b64=sig,
            client_ip="100.64.0.25",
        )
        token = sess["access_token"]

        revoked_count = mgr.revoke_device("office-mac-air", "StrongOwnerPass!2026", reason="LOST_DEVICE")
        self.assertGreaterEqual(revoked_count, 1)

        # Active session blocked immediately
        d_sess = mgr.authorize_request("/api/scan", "GET", "100.64.0.25", headers={"Authorization": f"Bearer {token}"})
        self.assertFalse(d_sess.allowed)
        self.assertEqual(d_sess.status_code, 403)
        self.assertEqual(d_sess.reason_code, "DEVICE_REVOKED")

        # New challenge blocked for revoked device
        chal2, d_chal = mgr.issue_auth_challenge("office-mac-air", "100.64.0.25")
        self.assertIsNone(chal2)
        self.assertEqual(d_chal.status_code, 403)
        self.assertEqual(d_chal.reason_code, "DEVICE_REVOKED")

        # Silent re-enrollment without allow_re_enroll=True blocked
        with self.assertRaises(PermissionError):
            mgr.register_device("office-mac-air", "Attacker Re-enroll", self.other_pub, "StrongOwnerPass!2026")

    def test_07_revoked_state_persists_across_restart_and_tamper_fails_closed(self) -> None:
        hmac_key = "state-integrity-hmac-key-2026-secure"
        mgr1 = self._new_manager(state_hmac_secret=hmac_key)
        self._register_office(mgr1)
        chal, sig = self._issue_and_sign(mgr1)
        sess, _ = mgr1.authenticate_remote_session(
            device_id="office-mac-air",
            challenge_id=chal["challenge_id"],
            signature_b64=sig,
            client_ip="100.64.0.25",
        )
        token = sess["access_token"]
        mgr1.revoke_device("office-mac-air", "StrongOwnerPass!2026", reason="ROTATE_KEY")

        # Restart simulation: instantiate fresh manager from disk
        mgr2 = self._new_manager(state_hmac_secret=hmac_key)
        self.assertEqual(mgr2.devices["office-mac-air"].status, DEVICE_STATUS_REVOKED)
        d_after_boot = mgr2.authorize_request("/api/scan", "GET", "100.64.0.25", headers={"Authorization": f"Bearer {token}"})
        self.assertFalse(d_after_boot.allowed)
        self.assertEqual(d_after_boot.reason_code, "DEVICE_REVOKED")

        # Tampering persisted JSON status triggers fail-closed PermissionError
        raw = json.loads((self.state_dir / "remote_auth.json").read_text(encoding="utf-8"))
        raw["devices"]["office-mac-air"]["status"] = DEVICE_STATUS_ACTIVE
        (self.state_dir / "remote_auth.json").write_text(json.dumps(raw), encoding="utf-8")
        with self.assertRaises(PermissionError):
            self._new_manager(state_hmac_secret=hmac_key)

        # Persisted file containing private key material always fails closed even without HMAC (local mode)
        raw.pop("state_hmac", None)
        raw["devices"]["office-mac-air"]["public_key_pem"] = "-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----"
        (self.state_dir / "remote_auth.json").write_text(json.dumps(raw), encoding="utf-8")
        with self.assertRaises(PermissionError):
            self._new_manager(allowed_vpn_cidrs=(), remote_mode_enabled=False, state_hmac_secret=None)

    def test_08_owner_authentication_failures_rejected(self) -> None:
        mgr = self._new_manager()
        self._register_office(mgr)
        _, _, api_handler, _ = self._build_api_and_frontend_handlers(mgr)
        chal, sig = self._issue_and_sign(mgr, client_ip="100.64.0.25")

        # Remote login MUST NOT accept or transmit owner_secret over /api/remote/auth
        st_owner_in_auth, b_owner_in_auth, _, _ = _dispatch_http(
            api_handler,
            "POST",
            "/api/remote/auth",
            client_ip="100.64.0.25",
            body={
                "device_id": "office-mac-air",
                "challenge_id": chal["challenge_id"],
                "signature": sig,
                "owner_secret": "WrongOwnerPass",
            },
        )
        self.assertEqual(st_owner_in_auth, 400)
        self.assertEqual(b_owner_in_auth["reason_code"], "OWNER_SECRET_NOT_PERMITTED_IN_REMOTE_AUTH")

        # Owner endpoints reject missing or invalid X-TradeLens-Owner-Secret
        for method_name, owner_ep, payload in (
            ("POST", "/api/broker/authenticate", {}),
            ("POST", "/api/runtime/shutdown", {}),
            ("GET", "/api/remote/devices", None),
            ("POST", "/api/remote/devices/register", {"device_id": "x", "public_key_pem": self.other_pub}),
            ("POST", "/api/remote/devices/approve", {"device_id": "x"}),
            ("POST", "/api/remote/devices/remove", {"device_id": "x"}),
        ):
            st, body, _, _ = _dispatch_http(
                api_handler,
                method_name,
                owner_ep,
                client_ip="127.0.0.1",
                headers={"X-TradeLens-Owner-Secret": "BadOwnerSecret"},
                body=payload,
            )
            self.assertEqual(st, 401, msg=f"Expected 401 on {owner_ep}, got {st}: {body}")
            self.assertEqual(body["reason_code"], "OWNER_AUTH_REQUIRED")

    def test_09_bruteforce_rate_limiter_locks_out_and_recovers(self) -> None:
        mgr = self._new_manager(max_auth_failures=3, auth_failure_window_seconds=300, lockout_seconds=120)
        self.assertIsInstance(mgr.rate_limiter, AuthRateLimiter)
        self._register_office(mgr)

        for _ in range(3):
            chal, _ = self._issue_and_sign(mgr)
            bad_sig = DeviceKeyCrypto.sign_challenge(self.other_priv, chal["challenge_string"])
            sess, dec = mgr.authenticate_remote_session(
                device_id="office-mac-air",
                challenge_id=chal["challenge_id"],
                signature_b64=bad_sig,
                client_ip="100.64.0.25",
            )
            self.assertIsNone(sess)
            self.assertEqual(dec.reason_code, "INVALID_DEVICE_SIGNATURE")

        # 4th attempt is rate-limited even for challenge issuance and auth
        chal_locked, dec_locked = mgr.issue_auth_challenge("office-mac-air", "100.64.0.25")
        self.assertIsNone(chal_locked)
        self.assertEqual(dec_locked.status_code, 429)
        self.assertEqual(dec_locked.reason_code, "AUTH_RATE_LIMITED")

        # Advance past lockout duration -> legitimate key-only auth works again
        self.clock.advance(125.0)
        chal_ok, sig_ok = self._issue_and_sign(mgr)
        sess_ok, dec_ok = mgr.authenticate_remote_session(
            device_id="office-mac-air",
            challenge_id=chal_ok["challenge_id"],
            signature_b64=sig_ok,
            client_ip="100.64.0.25",
        )
        self.assertTrue(dec_ok.allowed)
        self.assertIsNotNone(sess_ok)

    def test_10_session_ip_binding_legacy_secret_and_url_token_guards(self) -> None:
        mgr = self._new_manager()
        self._register_office(mgr)
        _, _, api_handler, frontend_handler = self._build_api_and_frontend_handlers(mgr)

        chal, sig = self._issue_and_sign(mgr, client_ip="100.64.0.25")
        sess, _ = mgr.authenticate_remote_session(
            device_id="office-mac-air",
            challenge_id=chal["challenge_id"],
            signature_b64=sig,
            client_ip="100.64.0.25",
        )
        token = sess["access_token"]

        # Replaying bearer token from another VPN peer IP rejected SESSION_IP_MISMATCH
        st_ip, body_ip, _, _ = _dispatch_http(
            api_handler,
            "GET",
            "/api/scan",
            client_ip="100.64.0.88",
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(st_ip, 403)
        self.assertEqual(body_ip["reason_code"], "SESSION_IP_MISMATCH")

        # Legacy shared device_secret auth body explicitly rejected
        st_leg, body_leg, _, _ = _dispatch_http(
            api_handler,
            "POST",
            "/api/remote/auth",
            client_ip="100.64.0.25",
            body={"device_id": "office-mac-air", "device_secret": "old-secret"},
        )
        self.assertEqual(st_leg, 401)
        self.assertEqual(body_leg["reason_code"], "LEGACY_SHARED_SECRET_NOT_SUPPORTED")

        # URL query parameters carrying token/secret rejected on both API & Frontend
        for bad_q in ("/api/scan?token=abc", "/?access_token=abc", "/?owner_secret=xyz"):
            st_api_q, b_api_q, _, _ = _dispatch_http(api_handler, "GET", bad_q, client_ip="127.0.0.1")
            self.assertEqual(st_api_q, 400)
            self.assertEqual(b_api_q["reason_code"], "URL_TOKEN_FORBIDDEN")

            st_fe_q, b_fe_q, _, _ = _dispatch_http(frontend_handler, "GET", bad_q, client_ip="127.0.0.1")
            self.assertEqual(st_fe_q, 400)
            self.assertEqual(b_fe_q["reason_code"], "URL_TOKEN_FORBIDDEN")

    def test_11_frontend_html_xss_csp_cdn_and_storage_hardening(self) -> None:
        html_src = INDEX_HTML.read_text(encoding="utf-8")
        # Third-party unpinned CDNs removed
        self.assertNotIn("cdn.tailwindcss.com", html_src)
        self.assertNotIn("cdnjs.cloudflare.com", html_src)
        self.assertNotIn("http://unpkg.com", html_src)
        # No inline DOM event attributes or persistent browser token storage
        self.assertIsNone(re.search(r"\son(?:click|submit|change|load)\s*=", html_src, re.IGNORECASE))
        self.assertNotIn("localStorage.setItem('remoteSessionToken", html_src)
        self.assertNotIn("sessionStorage.setItem", html_src)
        # XSS escape helper used on API/user-supplied strings in DOM renderers
        self.assertIn("function escapeHtml(", html_src)
        self.assertIn("escapeHtml(idx.symbol)", html_src)
        self.assertIn("escapeHtml(s.symbol)", html_src)
        self.assertIn("escapeHtml(s.name || s.symbol)", html_src)

        mgr = self._new_manager()
        _, _, api_handler, frontend_handler = self._build_api_and_frontend_handlers(mgr)

        # Fetch HTML via both FrontendStaticHandler and TradeLensAPIHandler and verify unique CSP nonces
        seen_nonces = set()
        for handler in (frontend_handler, api_handler):
            st, body_html, hdrs, _ = _dispatch_http(handler, "GET", "/", client_ip="127.0.0.1")
            self.assertEqual(st, 200)
            csp = hdrs.get("content-security-policy", "")
            self.assertNotIn("__CSP_NONCE__", body_html)
            self.assertIn("object-src 'none'", csp)
            self.assertIn("frame-ancestors 'none'", csp)
            self.assertIn("base-uri 'self'", csp)
            script_src_match = re.search(r"script-src\s+([^;]+)", csp)
            self.assertIsNotNone(script_src_match)
            script_src = script_src_match.group(1)
            self.assertNotIn("'unsafe-inline'", script_src)
            self.assertNotIn("'unsafe-eval'", script_src)
            nonce_match = re.search(r"'nonce-([A-Za-z0-9_-]+)'", script_src)
            self.assertIsNotNone(nonce_match)
            nonce = nonce_match.group(1)
            seen_nonces.add(nonce)
            self.assertIn(f'<script nonce="{nonce}">', body_html)
            self.assertEqual(hdrs.get("x-frame-options"), "DENY")
            self.assertEqual(hdrs.get("x-content-type-options"), "nosniff")
            self.assertEqual(hdrs.get("referrer-policy"), "no-referrer")
            self.assertEqual(hdrs.get("cross-origin-resource-policy"), "same-origin")
            self.assertEqual(hdrs.get("cache-control"), "no-store")
        self.assertEqual(len(seen_nonces), 2, "Each HTML response must receive a fresh CSP nonce")

    def test_12_invalid_host_header_dns_rebinding_rejected(self) -> None:
        mgr = self._new_manager()
        _, _, api_handler, frontend_handler = self._build_api_and_frontend_handlers(mgr)

        for bad_host in ("evil.attacker.com:8000", "169.254.169.254", "bad host:8000", ""):
            st_api, body_api, _, _ = _dispatch_http(
                api_handler,
                "GET",
                "/api/health",
                client_ip="127.0.0.1",
                headers={"Host": bad_host},
            )
            self.assertEqual(st_api, 403, msg=f"API should reject Host={bad_host!r}")
            self.assertIn(
                body_api["reason_code"],
                {"UNAPPROVED_HOSTNAME_HOST_HEADER", "UNAPPROVED_IP_HOST_HEADER", "INVALID_HOST_HEADER"},
            )

            st_fe, body_fe, _, _ = _dispatch_http(
                frontend_handler,
                "GET",
                "/",
                client_ip="127.0.0.1",
                headers={"Host": bad_host},
            )
            self.assertEqual(st_fe, 403, msg=f"Frontend should reject Host={bad_host!r}")
            self.assertIn(
                body_fe["reason_code"],
                {"UNAPPROVED_HOSTNAME_HOST_HEADER", "UNAPPROVED_IP_HOST_HEADER", "INVALID_HOST_HEADER"},
            )

        # Configured MagicDNS host & Tailscale IP allowed (checked after host check, /api/health returns 200 on loopback)
        st_ok, _, _, _ = _dispatch_http(
            api_handler,
            "GET",
            "/api/health",
            client_ip="127.0.0.1",
            headers={"Host": "home-mac.tailnet.ts.net:8000"},
        )
        self.assertEqual(st_ok, 200)

    def test_13_invalid_origin_cors_never_wildcard(self) -> None:
        mgr = self._new_manager()
        _, _, api_handler, frontend_handler = self._build_api_and_frontend_handlers(mgr)

        for bad_origin in ("https://evil.attacker.example", "null", "javascript:alert(1)"):
            st_api, body_api, hdrs_api, _ = _dispatch_http(
                api_handler,
                "GET",
                "/api/health",
                client_ip="127.0.0.1",
                headers={"Origin": bad_origin},
            )
            self.assertEqual(st_api, 403)
            self.assertIn("ORIGIN", body_api["reason_code"])
            self.assertNotEqual(hdrs_api.get("access-control-allow-origin"), "*")

            st_fe, _, hdrs_fe, _ = _dispatch_http(
                frontend_handler,
                "GET",
                "/",
                client_ip="127.0.0.1",
                headers={"Origin": bad_origin},
            )
            self.assertEqual(st_fe, 403)
            self.assertNotEqual(hdrs_fe.get("access-control-allow-origin"), "*")

        # Approved Tailscale origin echoed cleanly (never '*')
        st_pre, _, hdrs_pre, _ = _dispatch_http(
            api_handler,
            "OPTIONS",
            "/api/scan",
            client_ip="100.64.0.25",
            headers={"Host": "100.64.0.10:8000", "Origin": "http://100.64.0.10:8080"},
        )
        self.assertEqual(st_pre, 200)
        self.assertEqual(hdrs_pre.get("access-control-allow-origin"), "http://100.64.0.10:8080")
        self.assertNotIn("access-control-allow-origin: *", "\n".join(hdrs_pre.values()))

    def test_14_forged_proxy_and_forwarded_headers_rejected(self) -> None:
        mgr = self._new_manager()
        self._register_office(mgr)
        chal, sig = self._issue_and_sign(mgr, client_ip="100.64.0.25")
        sess, _ = mgr.authenticate_remote_session(
            "office-mac-air", chal["challenge_id"], sig, "100.64.0.25"
        )
        token = sess["access_token"]
        _, _, api_handler, frontend_handler = self._build_api_and_frontend_handlers(mgr)

        for hdr_name, hdr_val in (
            ("X-Forwarded-For", "127.0.0.1"),
            ("Forwarded", "for=127.0.0.1"),
            ("Tailscale-User-Login", "victim@example.com"),
            ("X-Real-Ip", "127.0.0.1"),
            ("Via", "1.1 spoof-proxy"),
        ):
            st_api, body_api, _, _ = _dispatch_http(
                api_handler,
                "GET",
                "/api/scan",
                client_ip="100.64.0.25",
                headers={"Authorization": f"Bearer {token}", hdr_name: hdr_val},
            )
            self.assertEqual(st_api, 403)
            self.assertEqual(body_api["reason_code"], "FORGED_PROXY_HEADERS")

            st_fe, body_fe, _, _ = _dispatch_http(
                frontend_handler,
                "GET",
                "/",
                client_ip="100.64.0.25",
                headers={hdr_name: hdr_val},
            )
            self.assertEqual(st_fe, 403)
            self.assertEqual(body_fe["reason_code"], "FORGED_PROXY_HEADERS")

        # Loopback peer sending X-Forwarded-For claiming 127.0.0.1 or untrusted IP also rejected
        st_loop, body_loop, _, _ = _dispatch_http(
            api_handler,
            "GET",
            "/api/health",
            client_ip="127.0.0.1",
            headers={"X-Forwarded-For": "127.0.0.1"},
        )
        self.assertEqual(st_loop, 403)
        self.assertEqual(body_loop["reason_code"], "FORGED_PROXY_HEADERS")

    def test_15_default_deny_endpoint_allowlist_methods_and_traversal(self) -> None:
        mgr = self._new_manager()
        self._register_office(mgr)
        chal, sig = self._issue_and_sign(mgr, client_ip="100.64.0.25")
        sess, _ = mgr.authenticate_remote_session(
            "office-mac-air", chal["challenge_id"], sig, "100.64.0.25"
        )
        token = sess["access_token"]
        _, _, api_handler, frontend_handler = self._build_api_and_frontend_handlers(mgr)
        auth_hdr = {"Authorization": f"Bearer {token}"}

        # Owner-only mutation & admin endpoints strictly rejected for remote workstations
        for owner_only in (
            "/api/runtime/shutdown",
            "/api/broker/authenticate",
            "/api/remote/devices/register",
            "/api/remote/devices/approve",
            "/api/remote/devices/revoke",
            "/api/ledger/trade",
            "/api/ledger/trade/delete",
            "/api/ledger/clear",
            "/api/ledger/import-chat",
            "/api/ledger/settings",
        ):
            st, b, _, _ = _dispatch_http(api_handler, "POST", owner_only, client_ip="100.64.0.25", headers=auth_hdr, body={})
            self.assertEqual(st, 403, msg=f"Remote access must fail 403 on {owner_only}")
            self.assertEqual(b["reason_code"], "HOME_OWNER_ONLY_OPERATION")

        # Wrong HTTP methods rejected 405 METHOD_NOT_ALLOWED
        st_m1, b_m1, _, _ = _dispatch_http(api_handler, "POST", "/api/scan", client_ip="100.64.0.25", headers=auth_hdr, body={})
        self.assertEqual(st_m1, 405)
        self.assertEqual(b_m1["reason_code"], "METHOD_NOT_ALLOWED")

        st_m2, b_m2, _, _ = _dispatch_http(api_handler, "GET", "/api/verify-tip", client_ip="100.64.0.25", headers=auth_hdr)
        self.assertEqual(st_m2, 405)
        self.assertEqual(b_m2["reason_code"], "METHOD_NOT_ALLOWED")

        # Path traversal & arbitrary endpoints denied
        for bad_path in ("/api/admin/exec", "/api/health/../runtime/shutdown", "/../.env"):
            st_p, b_p, _, _ = _dispatch_http(api_handler, "GET", bad_path, client_ip="100.64.0.25", headers=auth_hdr)
            self.assertIn(st_p, {400, 403})
            self.assertIn(b_p["reason_code"], {"INVALID_PATH", "ENDPOINT_NOT_AUTHORIZED_FOR_REMOTE"})

        st_fe_trav, b_fe_trav, _, _ = _dispatch_http(frontend_handler, "GET", "/../.env", client_ip="127.0.0.1")
        self.assertEqual(st_fe_trav, 404)
        self.assertEqual(b_fe_trav["reason_code"], "NOT_FOUND")

    def test_16_public_bind_addresses_refused(self) -> None:
        mgr = self._new_manager()
        rt, _, _, _ = self._build_api_and_frontend_handlers(mgr)
        for bad_bind in ("0.0.0.0", "::", "8.8.8.8", "192.168.1.50"):
            with self.assertRaises(RefusePublicBindError):
                build_api_server(port=0, bind_host=bad_bind, runtime=rt)
            with self.assertRaises(RefusePublicBindError):
                build_frontend_server(bind_host=bad_bind, port=0, allowed_vpn_cidrs=["100.64.0.0/10"])

    def test_17_broker_credential_leakage_and_redaction(self) -> None:
        mgr = self._new_manager()
        self._register_office(mgr)
        _, _, api_handler, _ = self._build_api_and_frontend_handlers(mgr)

        # Remote challenge, auth, and health responses must contain zero broker or owner secrets
        st_c, body_c, _, _ = _dispatch_http(
            api_handler, "POST", "/api/remote/challenge", client_ip="100.64.0.25", body={"device_id": "office-mac-air"}
        )
        self.assertEqual(st_c, 200)
        sig = DeviceKeyCrypto.sign_challenge(self.office_priv, body_c["challenge_string"])
        st_a, body_a, _, _ = _dispatch_http(
            api_handler,
            "POST",
            "/api/remote/auth",
            client_ip="100.64.0.25",
            body={
                "device_id": "office-mac-air",
                "challenge_id": body_c["challenge_id"],
                "signature_b64": sig,
            },
        )
        self.assertEqual(st_a, 200)
        st_h, body_h, _, _ = _dispatch_http(
            api_handler,
            "GET",
            "/api/health",
            client_ip="100.64.0.25",
            headers={"Authorization": f"Bearer {body_a['access_token']}"},
        )
        self.assertEqual(st_h, 200)
        combined = json.dumps({"chal": body_c, "auth": body_a, "health": body_h})
        for secret in ("BROKER_KEY_XYZ", "BROKER_CLIENT_99", "BROKER_PASS_SECRET", "JBSWY3DPEHPK3PXP", "StrongOwnerPass!2026", "BEGIN EC PRIVATE KEY", "BEGIN PRIVATE KEY"):
            self.assertNotIn(secret, combined)

    def test_18_jwt_feed_token_pem_key_redaction(self) -> None:
        raw_text = (
            'jwtToken="eyJ_HEADER.eyJ_PAYLOAD.SIG" refreshToken=ref_tok_99 feedToken=feed_tok_88 '
            "Authorization: Bearer tl_sess_token_123 X-TradeLens-Owner-Secret=OwnerPass99 "
            f"{self.office_priv}"
        )
        scrubbed = redact_runtime_text(raw_text, extra_secrets=["tl_sess_token_123", "OwnerPass99"])
        for sensitive in ("eyJ_HEADER.eyJ_PAYLOAD.SIG", "ref_tok_99", "feed_tok_88", "tl_sess_token_123", "OwnerPass99", "BEGIN PRIVATE KEY", "BEGIN EC PRIVATE KEY"):
            self.assertNotIn(sensitive, scrubbed)
        self.assertIn("[REDACTED_PRIVATE_KEY]", scrubbed)

        nested = redact_payload({
            "jwtToken": "secret_jwt",
            "feed_token": "secret_feed",
            "private_key_pem": self.office_priv,
            "ok": "visible",
        })
        self.assertEqual(nested["jwtToken"], "[REDACTED]")
        self.assertEqual(nested["feed_token"], "[REDACTED]")
        self.assertEqual(nested["private_key_pem"], "[REDACTED]")
        self.assertEqual(nested["ok"], "visible")

        # Verify RedactingLogFilter scrubs LogRecord.msg and args
        log_filter = RedactingLogFilter(lambda: ["secret_bearer_tok"])
        rec = logging.LogRecord("test", logging.INFO, "x.py", 1, "Auth fail token=%s", ("secret_bearer_tok",), None)
        self.assertTrue(log_filter.filter(rec))
        formatted = rec.getMessage()
        self.assertNotIn("secret_bearer_tok", formatted)

    def test_19_no_order_execution_surface_anywhere(self) -> None:
        provider = AngelOneSmartAPIProvider(
            api_key="AK", client_code="CC", password="PW", totp_secret="JBSWY3DPEHPK3PXP"
        )
        for forbidden_attr in ("placeOrder", "place_order", "modifyOrder", "cancelOrder", "exitPosition", "createGttRule"):
            self.assertFalse(hasattr(provider, forbidden_attr), f"Forbidden order method {forbidden_attr} found!")

        mgr = self._new_manager()
        self._register_office(mgr)
        rt, _, api_handler, _ = self._build_api_and_frontend_handlers(mgr)
        self.assertFalse(rt.get_health_payload()["order_execution_enabled"])

        chal, sig = self._issue_and_sign(mgr)
        sess, _ = mgr.authenticate_remote_session(
            "office-mac-air", chal["challenge_id"], sig, "100.64.0.25"
        )
        for order_path in ("/api/order", "/api/orders/place", "/api/trade/execute"):
            st_rem, b_rem, _, _ = _dispatch_http(
                api_handler, "POST", order_path, client_ip="100.64.0.25", headers={"Authorization": f"Bearer {sess['access_token']}"}, body={}
            )
            self.assertEqual(st_rem, 403)
            self.assertEqual(b_rem["reason_code"], "ENDPOINT_NOT_AUTHORIZED_FOR_REMOTE")

            st_loc, _, _, _ = _dispatch_http(api_handler, "POST", order_path, client_ip="127.0.0.1", body={})
            self.assertEqual(st_loc, 404)

    def test_20_angel_one_bounded_reconnect_tls_and_keychain_store(self) -> None:
        # 20A. AngelOneSmartAPIProvider never infinitely recurses on HTTP 401/403 and scrubs client_code/secrets
        provider = AngelOneSmartAPIProvider(
            api_key="AK_SECRET_001",
            client_code="CLIENT_SECRET_002",
            password="PW_SECRET_003",
            totp_secret="JBSWY3DPEHPK3PXP",
        )
        provider.jwt_token = "EXPIRED_JWT_004"
        provider.is_authenticated = True
        connect_calls = 0

        def _counting_connect() -> bool:
            nonlocal connect_calls
            connect_calls += 1
            if connect_calls > 3:
                raise AssertionError("Unbounded connect recursion detected")
            provider.is_authenticated = True
            provider.jwt_token = "REFRESHED_JWT_005"
            return True

        provider.connect = _counting_connect  # type: ignore[method-assign]

        def _fake_urlopen_401(req, timeout=10):
            raise HTTPError(req.full_url, 401, "Unauthorized CLIENT_SECRET_002 AK_SECRET_001", {}, io.BytesIO(b"Unauthorized"))

        import backend.app.data.angel_one as angel_mod
        import contextlib
        orig_urlopen = angel_mod.urllib.request.urlopen
        angel_mod.urllib.request.urlopen = _fake_urlopen_401
        try:
            err_buf = io.StringIO()
            with contextlib.redirect_stdout(err_buf):
                quotes = provider.fetch_live_quotes()
            self.assertEqual(quotes, {})
            self.assertEqual(connect_calls, 1, "fetch_live_quotes must attempt at most 1 bounded reconnect")
            self.assertNotIn("CLIENT_SECRET_002", err_buf.getvalue())
            self.assertNotIn("AK_SECRET_001", err_buf.getvalue())
            scrubbed_exc = provider._redact(
                "Error for client CLIENT_SECRET_002 key AK_SECRET_001 pw PW_SECRET_003 jwt EXPIRED_JWT_004"
            )
            for s in ("CLIENT_SECRET_002", "AK_SECRET_001", "PW_SECRET_003", "EXPIRED_JWT_004"):
                self.assertNotIn(s, scrubbed_exc)

            # Even when starting unauthenticated, get_live_positions reconnects at most once
            connect_calls = 0
            provider.is_authenticated = False
            with contextlib.redirect_stdout(err_buf):
                positions = provider.get_live_positions()
            self.assertFalse(positions["has_live_positions"])
            self.assertEqual(positions["positions"], [])
            self.assertEqual(connect_calls, 1, "get_live_positions must attempt at most 1 bounded reconnect")

            # Also verify real connect() does not allow nested prefetch reconnect recursion when _allow_reconnect=False
            provider_real = AngelOneSmartAPIProvider(
                api_key="AK_SECRET_001",
                client_code="CLIENT_SECRET_002",
                password="PW_SECRET_003",
                totp_secret="JBSWY3DPEHPK3PXP",
            )
            orig_fetch = provider_real.fetch_live_quotes
            calls_in_prefetch: List[bool] = []

            def _spy_fetch(_allow_reconnect: bool = True):
                calls_in_prefetch.append(_allow_reconnect)
                return orig_fetch(_allow_reconnect=_allow_reconnect)

            provider_real.fetch_live_quotes = _spy_fetch  # type: ignore[method-assign]

            class _FakeLoginResp:
                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    return False

                def read(self):
                    return json.dumps({
                        "status": True,
                        "data": {"jwtToken": "J1", "refreshToken": "R1", "feedToken": "F1"},
                    }).encode("utf-8")

            def _login_then_401(req, timeout=10):
                if "loginByPassword" in req.full_url:
                    return _FakeLoginResp()
                raise HTTPError(req.full_url, 401, "Unauthorized CLIENT_SECRET_002", {}, io.BytesIO(b"401"))

            angel_mod.urllib.request.urlopen = _login_then_401
            buf = io.StringIO()
            import contextlib
            with contextlib.redirect_stdout(buf):
                ok = provider_real.connect()
            self.assertTrue(ok)
            self.assertEqual(calls_in_prefetch, [False], "connect prefetch must pass _allow_reconnect=False")
            self.assertNotIn("CLIENT_SECRET_002", buf.getvalue())
            self.assertNotIn("J1", buf.getvalue())
        finally:
            angel_mod.urllib.request.urlopen = orig_urlopen

        # 20B. MacOSKeychainKeyStore roundtrip with mocked /usr/bin/security commands
        fake_kc: Dict[Tuple[str, str], str] = {}

        def _fake_security(cmd: List[str]) -> Tuple[int, str, str]:
            self.assertEqual(cmd[0], "/usr/bin/security")
            sub = cmd[1]
            if sub == "add-generic-password":
                acct = cmd[cmd.index("-a") + 1]
                svc = cmd[cmd.index("-s") + 1]
                val = cmd[cmd.index("-w") + 1]
                fake_kc[(svc, acct)] = val
                return 0, "", ""
            if sub == "find-generic-password":
                acct = cmd[cmd.index("-a") + 1]
                svc = cmd[cmd.index("-s") + 1]
                if (svc, acct) not in fake_kc:
                    return 44, "", "not found"
                return 0, fake_kc[(svc, acct)] + "\n", ""
            if sub == "delete-generic-password":
                acct = cmd[cmd.index("-a") + 1]
                svc = cmd[cmd.index("-s") + 1]
                fake_kc.pop((svc, acct), None)
                return 0, "", ""
            return 1, "", "unsupported"

        kc_store = MacOSKeychainKeyStore(cmd_runner=_fake_security)
        kc_store.store_private_key("office-mac-air", self.office_priv)
        loaded_priv = kc_store.load_private_key("office-mac-air")
        self.assertEqual(loaded_priv.strip(), self.office_priv.strip())
        kc_store.delete_private_key("office-mac-air")
        self.assertIsNone(kc_store.load_private_key("office-mac-air"))

        # 20C. wrap_server_tls configures TLSv1.2+ SSL socket and rejects partial cert/key config
        with self.assertRaises(ValueError):
            wrap_server_tls(object(), tls_certfile=self.state_dir / "only.crt", tls_keyfile=None)

        cert_path = self.state_dir / "srv.crt"
        key_path = self.state_dir / "srv.key"
        subprocess.run(
            [
                "/usr/bin/openssl",
                "req",
                "-x509",
                "-newkey",
                "ec",
                "-pkeyopt",
                "ec_paramgen_curve:prime256v1",
                "-nodes",
                "-keyout",
                str(key_path),
                "-out",
                str(cert_path),
                "-days",
                "1",
                "-subj",
                "/CN=127.0.0.1",
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        s1 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        dummy_httpd = None
        try:
            class _FakeHTTPD:
                socket = s1

            dummy_httpd = _FakeHTTPD()
            enabled = wrap_server_tls(dummy_httpd, tls_certfile=cert_path, tls_keyfile=key_path)
            self.assertTrue(enabled)
            self.assertIsInstance(dummy_httpd.socket, ssl.SSLSocket)
            self.assertGreaterEqual(dummy_httpd.socket.context.minimum_version, ssl.TLSVersion.TLSv1_2)
        finally:
            try:
                if dummy_httpd is not None:
                    dummy_httpd.socket.close()  # type: ignore[has-type]
                else:
                    s1.close()
            except Exception:
                s1.close()


if __name__ == "__main__":
    unittest.main()
