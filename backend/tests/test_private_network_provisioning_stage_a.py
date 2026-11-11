"""
Phase 3E.5.2.5 Stage A — Private Network Provisioning & Security Verification Tests (Offline).

Exercises strictly local, mocked, and loopback-only verification for:
- Key-only Office Mac ECDSA P-256 auth without TRADELENS_OWNER_SECRET (and rejection of owner_secret on remote auth)
- Home Mac owner-only protection on device list/register/approve/revoke/remove & runtime control routes
- MacOSKeychainKeyStore ACL (-T ""), duplicate fallback, has_private_key, delete, and failure handling (mocked security CLI)
- Mandatory TRADELENS_STATE_HMAC_SECRET in remote mode, 0600 file-permission check, unsigned rejection, restart persistence
- PBKDF2-HMAC-SHA256 600,000 iterations + transparent 120,000 -> 600,000 upgrade
- Full device lifecycle (register -> list -> approve -> revoke -> remove) across Python, HTTP, and CLI
- OpenSSL SAN TLS certificate generation, TLS 1.2+ loopback API+Frontend servers, HTTPS probe, Host/Origin/CORS checks
- launchd Tailscale TLS plist rendering (plutil-validated, zero secrets, never installed to ~/Library/LaunchAgents)
- Runtime CLI start/stop/restart/health-check/single-instance checks
"""

from __future__ import annotations

import io
import json
import os
import re
import socket
import ssl
import stat
import subprocess
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import patch

from backend.app.main import TradeLensAppRuntime, build_api_server, make_api_handler
from backend.app.runtime import cli as runtime_cli
from backend.app.runtime.frontend_server import build_frontend_server
from backend.app.runtime.health_and_safety import RuntimeHealthCoordinator
from backend.app.runtime.launchd_config import HOME_ENGINE_LABEL, FRONTEND_SERVER_LABEL, render_launchd_plists
from backend.app.runtime.power_policy import PowerAndSleepMonitor
from backend.app.runtime.security import (
    DEVICE_STATUS_ACTIVE,
    DEVICE_STATUS_PENDING,
    DEVICE_STATUS_REVOKED,
    PBKDF2_DEFAULT_ITERATIONS,
    PBKDF2_LEGACY_ITERATIONS,
    DeviceCryptoError,
    DeviceKeyCrypto,
    MacOSKeychainKeyStore,
    RemoteAccessManager,
    _pbkdf2_hash,
    wrap_server_tls,
)
from backend.app.runtime.supervisor import default_http_health_probe, stop_running_supervisor


REPO_ROOT = Path(__file__).resolve().parents[2]
INDEX_HTML = REPO_ROOT / "frontend" / "index.html"
STRONG_HMAC = "stage_a_state_hmac_secret_min_32_bytes_2026!"
STRONG_OWNER = "HomeMacOwner!2026#Secure"


class _FakeClock:
    def __init__(self) -> None:
        self._now = datetime(2026, 11, 11, 9, 15, 0, tzinfo=timezone.utc)

    def now_utc(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)


class _FakeSocket:
    def makefile(self, *_args: Any, **_kwargs: Any) -> io.BytesIO:
        return io.BytesIO()


class _FakeServer:
    server_port = 8000


def _dispatch_http(
    handler_cls: Any,
    method: str,
    path: str,
    *,
    client_ip: str = "127.0.0.1",
    headers: Optional[Dict[str, str]] = None,
    body: Optional[Dict[str, Any]] = None,
) -> Tuple[int, Any, Dict[str, str]]:
    req_bytes = json.dumps(body).encode("utf-8") if body is not None else b""
    hdr_map = dict(headers or {})
    hdr_map.setdefault("Host", "127.0.0.1:8000")
    if body is not None:
        hdr_map.setdefault("Content-Type", "application/json")
        hdr_map["Content-Length"] = str(len(req_bytes))

    raw_out = io.BytesIO()

    class _MockHandler(handler_cls):
        def __init__(self) -> None:
            self.rfile = io.BytesIO(req_bytes)
            self.wfile = raw_out
            self.client_address = (client_ip, 51234)
            self.request_version = "HTTP/1.1"
            self.command = method
            self.path = path
            self.close_connection = True
            self.server = _FakeServer()
            self.request = _FakeSocket()
            from http.client import HTTPMessage

            msg = HTTPMessage()
            for k, v in hdr_map.items():
                msg[k] = v
            self.headers = msg
            self._headers_buffer: List[bytes] = []

        def send_response(self, code: int, message: Optional[str] = None) -> None:
            self._status_code = code
            self._resp_headers: Dict[str, str] = {}

        def send_header(self, keyword: str, value: str) -> None:
            self._resp_headers[keyword.lower()] = value

        def end_headers(self) -> None:
            return None

    h = _MockHandler()
    getattr(h, f"do_{method}")()
    text = raw_out.getvalue().decode("utf-8", errors="replace")
    try:
        parsed = json.loads(text) if text else None
    except json.JSONDecodeError:
        parsed = text
    return getattr(h, "_status_code", 500), parsed, getattr(h, "_resp_headers", {})


class _DummyProvider:
    api_key = "BROKER_KEY_STAGE_A"
    client_code = "BROKER_CLIENT_STAGE_A"
    password = "BROKER_PASSWORD_STAGE_A"
    totp_secret = "JBSWY3DPEHPK3PXP"

    def __init__(self) -> None:
        self.is_authenticated = False
        self.last_fetch_time = None
        self.cached_quotes: Dict[str, Any] = {}

    def connect(self) -> bool:
        self.is_authenticated = False
        return False

    def get_all_stocks(self) -> List[Dict[str, Any]]:
        return []

    def get_indices(self) -> List[Dict[str, Any]]:
        return []


class TestPrivateNetworkProvisioningStageA(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.tmp.name)
        self.clock = _FakeClock()
        self.office_priv, self.office_pub = DeviceKeyCrypto.generate_keypair()
        self.other_priv, self.other_pub = DeviceKeyCrypto.generate_keypair()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _new_remote_mgr(
        self,
        *,
        hmac_secret: Optional[str] = STRONG_HMAC,
        owner_secret: Optional[str] = STRONG_OWNER,
        vpn_cidrs: Tuple[str, ...] = ("100.64.0.0/10",),
    ) -> RemoteAccessManager:
        return RemoteAccessManager(
            state_path=self.state_dir / "remote_auth.json",
            owner_secret=owner_secret,
            allowed_vpn_cidrs=list(vpn_cidrs),
            allowed_hosts=["home-mac.tailnet.ts.net"],
            allowed_origins=["https://100.64.0.10:8080", "https://home-mac.tailnet.ts.net:8080"],
            state_hmac_secret=hmac_secret,
            remote_mode_enabled=True,
            clock=self.clock.now_utc,
        )

    def _make_runtime(self, mgr: RemoteAccessManager) -> TradeLensAppRuntime:
        prov = _DummyProvider()
        health = RuntimeHealthCoordinator(freshness_ttl_seconds=15.0, clock=self.clock.now_utc)
        power = PowerAndSleepMonitor(
            self.state_dir / "heartbeat.json",
            wall_clock=self.clock.now_utc,
            cmd_runner=lambda _c: (0, "Now drawing from 'AC Power'\n", ""),
        )
        return TradeLensAppRuntime(
            state_dir=self.state_dir,
            provider=prov,
            remote_access=mgr,
            health_coordinator=health,
            power_monitor=power,
            allow_sample_fallback=False,
            environ={
                "ANGEL_API_KEY": prov.api_key,
                "ANGEL_CLIENT_CODE": prov.client_code,
                "ANGEL_PASSWORD": prov.password,
                "ANGEL_TOTP_SECRET": prov.totp_secret,
                "TRADELENS_OWNER_SECRET": STRONG_OWNER,
                "TRADELENS_STATE_HMAC_SECRET": STRONG_HMAC,
                "TRADELENS_REMOTE_ACCESS_ENABLED": "1",
            },
        )

    def test_01_key_only_remote_auth_without_owner_secret_and_frontend_contract(self) -> None:
        mgr = self._new_remote_mgr()
        mgr.register_device("office-mac-air", "Office Mac", self.office_pub, STRONG_OWNER, approve=True)
        rt = self._make_runtime(mgr)
        api_handler = make_api_handler(rt)

        # Step 1: Request challenge over Tailscale IP (no owner secret needed)
        st_c, body_c, _ = _dispatch_http(
            api_handler,
            "POST",
            "/api/remote/challenge",
            client_ip="100.64.0.25",
            headers={"Host": "100.64.0.10:8000", "Origin": "https://100.64.0.10:8080"},
            body={"device_id": "office-mac-air"},
        )
        self.assertEqual(st_c, 200)
        sig = DeviceKeyCrypto.sign_challenge(self.office_priv, body_c["challenge_string"])

        # Step 2: Sending owner_secret in /api/remote/auth is explicitly rejected so remote client never leaks it
        st_bad_owner, body_bad_owner, _ = _dispatch_http(
            api_handler,
            "POST",
            "/api/remote/auth",
            client_ip="100.64.0.25",
            headers={"Host": "100.64.0.10:8000", "Origin": "https://100.64.0.10:8080"},
            body={
                "device_id": "office-mac-air",
                "challenge_id": body_c["challenge_id"],
                "signature": sig,
                "owner_secret": STRONG_OWNER,
            },
        )
        self.assertEqual(st_bad_owner, 400)
        self.assertEqual(body_bad_owner["reason_code"], "OWNER_SECRET_NOT_PERMITTED_IN_REMOTE_AUTH")

        # Step 3: Key-only challenge signature authentication succeeds WITHOUT owner_secret
        st_ok, body_ok, hdrs_ok = _dispatch_http(
            api_handler,
            "POST",
            "/api/remote/auth",
            client_ip="100.64.0.25",
            headers={"Host": "100.64.0.10:8000", "Origin": "https://100.64.0.10:8080"},
            body={
                "device_id": "office-mac-air",
                "challenge_id": body_c["challenge_id"],
                "signature": sig,
            },
        )
        self.assertEqual(st_ok, 200, body_ok)
        self.assertEqual(hdrs_ok.get("access-control-allow-origin"), "https://100.64.0.10:8080")
        token = body_ok["access_token"]
        self.assertNotIn(STRONG_OWNER, json.dumps(body_ok))

        # Step 4: Remote bearer token can access read-only trading endpoints, NOT owner routes
        st_scan, _, _ = _dispatch_http(
            api_handler,
            "GET",
            "/api/scan",
            client_ip="100.64.0.25",
            headers={"Host": "100.64.0.10:8000", "Authorization": f"Bearer {token}"},
        )
        self.assertEqual(st_scan, 200)

        for owner_route in ("/api/remote/devices", "/api/runtime/shutdown", "/api/remote/devices/remove"):
            m = "GET" if owner_route == "/api/remote/devices" else "POST"
            st_deny, body_deny, _ = _dispatch_http(
                api_handler,
                m,
                owner_route,
                client_ip="100.64.0.25",
                headers={"Host": "100.64.0.10:8000", "Authorization": f"Bearer {token}"},
                body={} if m == "POST" else None,
            )
            self.assertEqual(st_deny, 403)
            self.assertEqual(body_deny["reason_code"], "HOME_OWNER_ONLY_OPERATION")

        # Step 5: Frontend modal and JS MUST NOT ask for or transmit owner_secret
        html = INDEX_HTML.read_text(encoding="utf-8")
        self.assertNotIn("remoteOwnerSecret", html)
        self.assertNotIn("owner_secret:", html)
        self.assertIn("no Home Mac owner secret needed", html)

    def test_02_macos_keychain_acl_duplicate_and_failure_handling_mocked(self) -> None:
        calls: List[List[str]] = []
        store_map: Dict[Tuple[str, str], str] = {}
        force_dup_once = {"flag": True}
        deny_load = {"flag": False}

        def _mock_security(cmd: List[str]) -> Tuple[int, str, str]:
            calls.append(list(cmd))
            sub = cmd[1]
            acct = cmd[cmd.index("-a") + 1]
            svc = cmd[cmd.index("-s") + 1]
            if sub == "add-generic-password":
                if force_dup_once["flag"]:
                    force_dup_once["flag"] = False
                    return 45, "", "The specified item already exists in the keychain."
                store_map[(svc, acct)] = cmd[cmd.index("-w") + 1]
                return 0, "", ""
            if sub == "find-generic-password":
                if deny_load["flag"]:
                    return 128, "", "User interaction is not allowed."
                if (svc, acct) not in store_map:
                    return 44, "", "The specified item could not be found in the keychain."
                if "-w" not in cmd:
                    return 0, "keychain: \"login.keychain-db\"\n", ""
                return 0, store_map[(svc, acct)] + "\n", ""
            if sub == "delete-generic-password":
                existed = (svc, acct) in store_map
                store_map.pop((svc, acct), None)
                return (0, "", "") if existed else (44, "", "not found")
            return 1, "", "unsupported"

        kc = MacOSKeychainKeyStore(cmd_runner=_mock_security, require_prompt_acl=True)
        # Stores key with -T "" ACL restriction and transparently recovers from rc=45 duplicate item error
        kc.store_private_key("office-mac-air", self.office_priv)
        add_cmds = [c for c in calls if c[1] == "add-generic-password"]
        self.assertGreaterEqual(len(add_cmds), 2)
        self.assertIn("-T", add_cmds[0])
        self.assertEqual(add_cmds[0][add_cmds[0].index("-T") + 1], "")

        self.assertTrue(kc.has_private_key("office-mac-air"))
        self.assertEqual(kc.load_private_key("office-mac-air").strip(), self.office_priv.strip())

        # Empty or non-PEM keys rejected cleanly
        with self.assertRaises(DeviceCryptoError):
            kc.store_private_key("office-mac-air", "not-a-private-key")

        # Denied/locked keychain fails explicitly (does NOT silently look like missing key)
        deny_load["flag"] = True
        with self.assertRaises(RuntimeError):
            kc.load_private_key("office-mac-air")
        deny_load["flag"] = False

        # Corrupted non-PEM entry raises DeviceCryptoError
        store_map[(kc.service_name, "office-mac-air")] = "Zm9vYmFy"  # base64("foobar")
        with self.assertRaises(DeviceCryptoError):
            kc.load_private_key("office-mac-air")

        # Delete removes item and reports deleted=True cleanly
        self.assertTrue(kc.delete_private_key("office-mac-air"))
        self.assertFalse(kc.has_private_key("office-mac-air"))
        self.assertIsNone(kc.load_private_key("office-mac-air"))

    def test_03_mandatory_hmac_permissions_and_tamper_in_remote_mode(self) -> None:
        # Missing or short TRADELENS_STATE_HMAC_SECRET fails closed in remote mode
        for bad_hmac in (None, "", "   ", "short-15-chars!"):
            with self.assertRaises(PermissionError):
                self._new_remote_mgr(hmac_secret=bad_hmac)

        # TradeLensAppRuntime fails closed if remote mode enabled without TRADELENS_STATE_HMAC_SECRET
        with self.assertRaises(PermissionError):
            TradeLensAppRuntime(
                state_dir=self.state_dir,
                provider=_DummyProvider(),
                environ={
                    "TRADELENS_REMOTE_ACCESS_ENABLED": "1",
                    "TRADELENS_ALLOWED_VPN_CIDRS": "100.64.0.0/10",
                    "TRADELENS_OWNER_SECRET": STRONG_OWNER,
                },
            )

        # Valid remote manager writes state file strictly with 0600 permissions
        mgr = self._new_remote_mgr()
        mgr.register_device("office-mac-air", "Office Mac", self.office_pub, STRONG_OWNER, approve=True)
        state_file = self.state_dir / "remote_auth.json"
        self.assertEqual(stat.S_IMODE(state_file.stat().st_mode), 0o600)

        # Permissive file mode (0644) must fail closed on load in remote mode
        os.chmod(state_file, 0o644)
        with self.assertRaises(PermissionError):
            self._new_remote_mgr()
        os.chmod(state_file, 0o600)

        # Removing state_hmac (unsigned registry) must fail closed in remote mode
        data = json.loads(state_file.read_text(encoding="utf-8"))
        data.pop("state_hmac", None)
        state_file.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(PermissionError):
            self._new_remote_mgr()

    def test_04_pbkdf2_600k_iterations_and_legacy_120k_migration(self) -> None:
        # Weak owner secrets rejected
        with self.assertRaises(ValueError):
            RemoteAccessManager(self.state_dir / "weak.json", owner_secret="short123!")
        with self.assertRaises(ValueError):
            RemoteAccessManager(self.state_dir / "weak2.json", owner_secret="aaaaaaaaaaaa")

        # Legacy 120,000-iteration state verifies AND transparently upgrades to 600,000 iterations
        legacy_state = self.state_dir / "legacy_auth.json"
        legacy_salt, legacy_hash = _pbkdf2_hash(STRONG_OWNER, iterations=PBKDF2_LEGACY_ITERATIONS)
        legacy_payload = {
            "version": 2,
            "owner": {"salt_hex": legacy_salt, "hash_hex": legacy_hash},  # omitted iterations => 120_000
            "devices": {},
            "sessions": {},
        }
        legacy_state.write_text(json.dumps(legacy_payload), encoding="utf-8")
        os.chmod(legacy_state, 0o600)

        # Compute valid HMAC over canonical core for remote mode
        tmp_mgr = RemoteAccessManager(
            state_path=self.state_dir / "tmp_unused.json",
            allowed_vpn_cidrs=["100.64.0.0/10"],
            state_hmac_secret=STRONG_HMAC,
            remote_mode_enabled=True,
        )
        legacy_payload["state_hmac"] = tmp_mgr._compute_state_hmac(legacy_payload)
        legacy_state.write_text(json.dumps(legacy_payload), encoding="utf-8")

        mgr = RemoteAccessManager(
            state_path=legacy_state,
            allowed_vpn_cidrs=["100.64.0.0/10"],
            state_hmac_secret=STRONG_HMAC,
            remote_mode_enabled=True,
        )
        self.assertEqual(mgr.owner_kdf_iterations, PBKDF2_LEGACY_ITERATIONS)
        self.assertFalse(mgr.verify_owner_secret("WrongOwnerPass!2026"))
        self.assertTrue(mgr.verify_owner_secret(STRONG_OWNER))
        self.assertEqual(mgr.owner_kdf_iterations, PBKDF2_DEFAULT_ITERATIONS)
        self.assertGreaterEqual(PBKDF2_DEFAULT_ITERATIONS, 600_000)

        # Verify persisted upgrade reloads cleanly under HMAC and accepts owner secret at 600,000 iterations
        reloaded = RemoteAccessManager(
            state_path=legacy_state,
            allowed_vpn_cidrs=["100.64.0.0/10"],
            state_hmac_secret=STRONG_HMAC,
            remote_mode_enabled=True,
        )
        self.assertEqual(reloaded.owner_kdf_iterations, 600_000)
        self.assertNotEqual(reloaded.owner_salt_hex, legacy_salt)
        self.assertTrue(reloaded.verify_owner_secret(STRONG_OWNER))

    def test_05_full_device_lifecycle_list_revoke_remove_and_cli(self) -> None:
        mgr = self._new_remote_mgr()
        rt = self._make_runtime(mgr)
        api_handler = make_api_handler(rt)

        # Register pending -> list -> approve -> authenticate -> revoke -> remove
        pending = mgr.request_device_registration("office-mac-2", "Office Second Mac", self.office_pub)
        self.assertEqual(pending.status, DEVICE_STATUS_PENDING)

        # HTTP owner GET /api/remote/devices
        st_ls, body_ls, _ = _dispatch_http(
            api_handler,
            "GET",
            "/api/remote/devices",
            client_ip="127.0.0.1",
            headers={"X-TradeLens-Owner-Secret": STRONG_OWNER},
        )
        self.assertEqual(st_ls, 200)
        self.assertEqual([d["device_id"] for d in body_ls["devices"]], ["office-mac-2"])
        self.assertEqual(body_ls["devices"][0]["status"], DEVICE_STATUS_PENDING)

        mgr.approve_device("office-mac-2", STRONG_OWNER, activate=True)
        chal, dec = mgr.issue_auth_challenge("office-mac-2", "100.64.0.25")
        self.assertTrue(dec.allowed)
        sig = DeviceKeyCrypto.sign_challenge(self.office_priv, chal["challenge_string"])
        sess, auth_dec = mgr.authenticate_remote_session("office-mac-2", chal["challenge_id"], sig, "100.64.0.25")
        self.assertTrue(auth_dec.allowed)
        tok = sess["access_token"]

        # Removal of ACTIVE device blocked until revoked when require_revoked=True
        with self.assertRaises(PermissionError):
            mgr.remove_device("office-mac-2", STRONG_OWNER, require_revoked=True)

        # HTTP remove while active returns 400
        st_rm_act, _, _ = _dispatch_http(
            api_handler,
            "POST",
            "/api/remote/devices/remove",
            client_ip="127.0.0.1",
            headers={"X-TradeLens-Owner-Secret": STRONG_OWNER},
            body={"device_id": "office-mac-2"},
        )
        self.assertEqual(st_rm_act, 400)

        # Revoke then permanently remove via HTTP owner route
        mgr.revoke_device("office-mac-2", STRONG_OWNER, reason="DECOMMISSIONED")
        st_rm, body_rm, _ = _dispatch_http(
            api_handler,
            "POST",
            "/api/remote/devices/remove",
            client_ip="127.0.0.1",
            headers={"X-TradeLens-Owner-Secret": STRONG_OWNER},
            body={"device_id": "office-mac-2"},
        )
        self.assertEqual(st_rm, 200, body_rm)
        self.assertTrue(body_rm["removed"])
        self.assertEqual(mgr.list_devices(), [])

        # Previously issued token and new challenges both fail closed after removal
        d_tok = mgr.authorize_request("/api/scan", "GET", "100.64.0.25", headers={"Authorization": f"Bearer {tok}"})
        self.assertFalse(d_tok.allowed)
        self.assertEqual(d_tok.reason_code, "INVALID_SESSION_TOKEN")
        chal_after, d_after = mgr.issue_auth_challenge("office-mac-2", "100.64.0.25")
        self.assertIsNone(chal_after)
        self.assertEqual(d_after.reason_code, "UNREGISTERED_DEVICE")

        # CLI list-devices & remove-device with env HMAC secret
        cli_state = self.state_dir / "cli_lifecycle"
        pub_file = self.state_dir / "cli_dev.pub.pem"
        pub_file.write_text(self.other_pub, encoding="utf-8")
        old_env = os.environ.get("TRADELENS_STATE_HMAC_SECRET")
        os.environ["TRADELENS_STATE_HMAC_SECRET"] = STRONG_HMAC
        try:
            self.assertEqual(
                runtime_cli.main([
                    "--state-dir", str(cli_state),
                    "register-device",
                    "--remote-access",
                    "--device-id", "office-cli-99",
                    "--public-key-file", str(pub_file),
                    "--owner-secret", STRONG_OWNER,
                ]),
                0,
            )
            self.assertEqual(
                runtime_cli.main(["--state-dir", str(cli_state), "list-devices", "--remote-access"]),
                0,
            )
            self.assertEqual(
                runtime_cli.main([
                    "--state-dir", str(cli_state),
                    "revoke-device",
                    "--remote-access",
                    "--device-id", "office-cli-99",
                    "--owner-secret", STRONG_OWNER,
                ]),
                0,
            )
            self.assertEqual(
                runtime_cli.main([
                    "--state-dir", str(cli_state),
                    "remove-device",
                    "--remote-access",
                    "--device-id", "office-cli-99",
                    "--owner-secret", STRONG_OWNER,
                ]),
                0,
            )
        finally:
            if old_env is None:
                os.environ.pop("TRADELENS_STATE_HMAC_SECRET", None)
            else:
                os.environ["TRADELENS_STATE_HMAC_SECRET"] = old_env

    def test_06_tls_cert_gen_live_loopback_https_probe_and_fail_closed_http(self) -> None:
        cert_file = self.state_dir / "certs" / "home.crt"
        key_file = self.state_dir / "certs" / "home.key"
        rc_cert = runtime_cli.main([
            "generate-tls-cert",
            "--bind-host", "127.0.0.1",
            "--hostname", "home-mac.tailnet.ts.net",
            "--cert-out", str(cert_file),
            "--key-out", str(key_file),
            "--days", "30",
        ])
        self.assertEqual(rc_cert, 0)
        self.assertEqual(stat.S_IMODE(key_file.stat().st_mode), 0o600)

        # Verify SAN IP + DNS present via openssl x509 -text
        x509_out = subprocess.run(
            ["/usr/bin/openssl", "x509", "-in", str(cert_file), "-noout", "-text"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        self.assertIn("IP Address:127.0.0.1", x509_out)
        self.assertIn("DNS:home-mac.tailnet.ts.net", x509_out)

        # Non-loopback bind without TLS MUST fail closed on both backend and frontend servers
        mgr = self._new_remote_mgr()
        rt = self._make_runtime(mgr)
        with self.assertRaises(ValueError):
            build_api_server(port=0, bind_host="100.64.0.10", runtime=rt)
        with self.assertRaises(ValueError):
            build_frontend_server(bind_host="100.64.0.10", port=0, allowed_vpn_cidrs=["100.64.0.0/10"])

        # Verify wrap_server_tls enforces TLS 1.2+ and performs real OpenSSL handshake over in-process socketpair
        dummy_listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)

        class _FakeHTTPD:
            socket = dummy_listener

        wrap_server_tls(_FakeHTTPD, tls_certfile=cert_file, tls_keyfile=key_file)
        server_ctx = _FakeHTTPD.socket.context
        self.assertGreaterEqual(server_ctx.minimum_version, ssl.TLSVersion.TLSv1_2)
        _FakeHTTPD.socket.close()

        srv_sock, cli_sock = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        handshake_errors: List[Exception] = []
        negotiated: Dict[str, Any] = {}

        def _server_accept() -> None:
            try:
                tls_srv = server_ctx.wrap_socket(srv_sock, server_side=True)
                data = tls_srv.recv(1024)
                negotiated["recv"] = data
                tls_srv.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
                tls_srv.close()
            except Exception as exc:
                handshake_errors.append(exc)

        t_srv = threading.Thread(target=_server_accept, daemon=True)
        t_srv.start()
        cli_ctx = ssl.create_default_context(cafile=str(cert_file))
        cli_ctx.verify_flags &= ~getattr(ssl, "VERIFY_X509_STRICT", 0)
        tls_cli = cli_ctx.wrap_socket(cli_sock, server_hostname="home-mac.tailnet.ts.net")
        try:
            tls_cli.sendall(b"GET /api/health HTTP/1.1\r\nHost: home-mac.tailnet.ts.net\r\n\r\n")
            resp_bytes = tls_cli.recv(1024)
            negotiated["version"] = tls_cli.version()
        finally:
            tls_cli.close()
        t_srv.join(timeout=3.0)
        self.assertFalse(handshake_errors, f"TLS handshake errors: {handshake_errors}")
        self.assertIn(b"HTTP/1.1 200 OK", resp_bytes)
        self.assertIn(negotiated.get("version"), {"TLSv1.2", "TLSv1.3"})

        # Verify default_http_health_probe & CLI health-check construct verified HTTPS SSLContext
        captured_urls: List[str] = []
        captured_contexts: List[Optional[ssl.SSLContext]] = []

        class _FakeResp:
            status = 200

            def __enter__(self) -> "_FakeResp":
                return self

            def __exit__(self, *_a: Any) -> None:
                return None

        def _fake_urlopen(req: Any, timeout: float = 2.0, context: Optional[ssl.SSLContext] = None) -> _FakeResp:
            captured_urls.append(req.full_url)
            captured_contexts.append(context)
            return _FakeResp()

        with patch("backend.app.runtime.supervisor.urllib.request.urlopen", side_effect=_fake_urlopen):
            ok_api, detail_api = default_http_health_probe(
                "https://100.64.0.10:8000/api/health",
                probe_secret="probe-123",
                tls_ca_file=cert_file,
            )
            self.assertTrue(ok_api, detail_api)
            self.assertEqual(detail_api, "HTTP_200")
            self.assertIsInstance(captured_contexts[-1], ssl.SSLContext)
            self.assertEqual(captured_contexts[-1].verify_mode, ssl.CERT_REQUIRED)

            rc_hc = runtime_cli.main([
                "health-check",
                "--bind-host", "100.64.0.10",
                "--backend-port", "8000",
                "--frontend-port", "8080",
                "--tls-cert", str(cert_file),
            ])
            self.assertEqual(rc_hc, 0)
            self.assertIn("https://100.64.0.10:8000/api/health", captured_urls)
            self.assertIn("https://100.64.0.10:8080/frontend-health", captured_urls)

    def test_08_https_frontend_origin_cors_and_tailscale_serve_spoof_guards(self) -> None:
        mgr = self._new_remote_mgr()
        mgr.register_device("office-mac-air", "Office Mac", self.office_pub, STRONG_OWNER, approve=True)
        chal, dec = mgr.issue_auth_challenge("office-mac-air", "100.64.0.25")
        self.assertTrue(dec.allowed)
        sig = DeviceKeyCrypto.sign_challenge(self.office_priv, chal["challenge_string"])
        sess, auth_dec = mgr.authenticate_remote_session("office-mac-air", chal["challenge_id"], sig, "100.64.0.25")
        self.assertTrue(auth_dec.allowed)
        token = sess["access_token"]
        rt = self._make_runtime(mgr)
        api_handler = make_api_handler(rt)

        # Spoofed Tailscale Serve identity/forwarding headers fail closed with 403 FORGED_PROXY_HEADERS
        for hdr_name, hdr_val in (
            ("Tailscale-User-Login", "owner@example.com"),
            ("X-Forwarded-For", "127.0.0.1"),
            ("X-Real-Ip", "100.64.0.10"),
        ):
            st_p, body_p, _ = _dispatch_http(
                api_handler,
                "GET",
                "/api/scan",
                client_ip="100.64.0.25",
                headers={
                    "Host": "100.64.0.10:8000",
                    "Authorization": f"Bearer {token}",
                    hdr_name: hdr_val,
                },
            )
            self.assertEqual(st_p, 403)
            self.assertEqual(body_p["reason_code"], "FORGED_PROXY_HEADERS")

        # Disallowed Origin & wildcard CORS absence
        st_bad_org, body_bad_org, hdrs_bad_org = _dispatch_http(
            api_handler,
            "GET",
            "/api/scan",
            client_ip="100.64.0.25",
            headers={
                "Host": "100.64.0.10:8000",
                "Origin": "https://attacker.example.com",
                "Authorization": f"Bearer {token}",
            },
        )
        self.assertEqual(st_bad_org, 403)
        self.assertIn("ORIGIN", body_bad_org["reason_code"])
        self.assertNotIn("access-control-allow-origin", hdrs_bad_org)

        # Frontend JS resolves https://<host>:8000 cleanly when served over https://<host>:8080
        html = INDEX_HTML.read_text(encoding="utf-8")
        self.assertIn("const proto = window.location.protocol === 'https:' ? 'https:' : 'http:';", html)
        self.assertIn("`${proto}//${fmtHost}:8000${cleanPath}`", html)

    def test_07_supervisor_tls_launchd_no_install_and_lifecycle_cli(self) -> None:
        cert_file = self.state_dir / "srv.crt"
        key_file = self.state_dir / "srv.key"
        cert_file.write_text("dummy-cert", encoding="utf-8")
        key_file.write_text("dummy-key", encoding="utf-8")

        # 1. Supervisor _build_services generates HTTPS health probes & passes TLS/VPN/HMAC env without leaking secrets
        parser = runtime_cli.build_parser()
        args = parser.parse_args([
            "--state-dir", str(self.state_dir / "sup"),
            "supervise",
            "--bind-host", "100.64.0.10",
            "--backend-port", "8000",
            "--frontend-port", "8080",
            "--vpn-cidr", "100.64.0.0/10",
            "--allowed-host", "home-mac.tailnet.ts.net",
            "--allowed-origin", "https://100.64.0.10:8080",
            "--tls-cert", str(cert_file),
            "--tls-key", str(key_file),
            "--remote-access",
        ])
        specs = runtime_cli._build_services(args, probe_secret="probe_xyz")
        self.assertEqual(specs[0].health_url, "https://100.64.0.10:8000/api/health")
        self.assertEqual(specs[1].health_url, "https://100.64.0.10:8080/frontend-health")
        self.assertEqual(specs[0].env.get("TRADELENS_REMOTE_ACCESS_ENABLED"), "1")
        self.assertEqual(specs[0].env.get("TRADELENS_TLS_CERT_FILE"), str(cert_file))
        self.assertIn("--tls-cert", specs[1].command)
        self.assertIn("--allowed-origin", specs[1].command)

        # cmd_supervise fails closed if remote access enabled without TRADELENS_STATE_HMAC_SECRET
        old_hmac = os.environ.pop("TRADELENS_STATE_HMAC_SECRET", None)
        try:
            with self.assertRaises(PermissionError):
                runtime_cli.cmd_supervise(args)
        finally:
            if old_hmac is not None:
                os.environ["TRADELENS_STATE_HMAC_SECRET"] = old_hmac

        # 2. render-launchd outputs plutil-valid Tailscale TLS templates in staging dir ONLY (zero secrets in plist!)
        launchd_out = self.state_dir / "launchd_stage"
        plists = render_launchd_plists(
            output_dir=launchd_out,
            repo_root=REPO_ROOT,
            bind_host="100.64.0.10",
            vpn_cidrs=["100.64.0.0/10"],
            allowed_hosts=["home-mac.tailnet.ts.net"],
            allowed_origins=["https://100.64.0.10:8080"],
            tls_cert_file=str(cert_file),
            tls_key_file=str(key_file),
            remote_access_enabled=True,
        )
        for label in (HOME_ENGINE_LABEL, FRONTEND_SERVER_LABEL):
            p = plists[label]
            self.assertTrue(p.exists())
            xml_text = p.read_text(encoding="utf-8")
            self.assertIn("--tls-cert", xml_text)
            self.assertNotIn("ANGEL_API_KEY", xml_text)
            self.assertNotIn("TRADELENS_OWNER_SECRET", xml_text)
            self.assertNotIn("TRADELENS_STATE_HMAC_SECRET", xml_text)
            res = subprocess.run(["plutil", "-lint", str(p)], capture_output=True, text=True, check=False)
            self.assertEqual(res.returncode, 0, res.stdout + res.stderr)

        # 3. Verify ~/Library/LaunchAgents does NOT contain TradeLens plists during Stage A
        user_la = Path.home() / "Library" / "LaunchAgents"
        self.assertFalse((user_la / f"{HOME_ENGINE_LABEL}.plist").exists())
        self.assertFalse((user_la / f"{FRONTEND_SERVER_LABEL}.plist").exists())

        # 4. stop / restart respect stopped and owner_shutdown.flag cleanly
        sup_state = self.state_dir / "ops_state"
        stop_info = stop_running_supervisor(sup_state)
        self.assertEqual(stop_info["status"], "NOT_RUNNING")
        self.assertEqual(runtime_cli.main(["--state-dir", str(sup_state), "owner-shutdown"]), 0)
        self.assertEqual(runtime_cli.main(["--state-dir", str(sup_state), "restart"]), 0)
        self.assertTrue((sup_state / "owner_shutdown.flag").exists())


if __name__ == "__main__":
    unittest.main()
