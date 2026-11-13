"""
Phase 3E.5.2.8.2G: Targeted Unit & Integration Tests for Tailscale VPN Detection
and Local vs Remote Health Classification.

Verifies:
1. Running Tailscale with a valid assigned IP bound to a local interface.
2. Running Tailscale with no valid assigned IP.
3. Running Tailscale with an assigned IP that is not locally bound.
4. Stopped daemon and CLI failure.
5. CLI timeout and malformed JSON.
6. Cache TTL prevents repeated subprocess calls.
7. Cache expiry causes a fresh probe.
8. Local-only mode does not report VPN_DISCONNECTED as a failure.
9. Remote mode with VPN disconnected remains unavailable.
10. Remote mode with VPN connected but application authorization or TLS missing remains unavailable.
11. Existing owner-shutdown and other remote security gates remain enforced.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from backend.app.main import TradeLensAppRuntime
from backend.app.runtime.health_and_safety import (
    RuntimeHealthCoordinator,
    VPNProbeResult,
    find_tailscale_bin,
    is_ip_bound_to_local_interface,
    probe_vpn_status,
)
from backend.app.runtime.security import RemoteAccessManager


class TestVPNDetectionAndLocalHealthClassification(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.tmp_dir.name)
        self.hmac_secret = "TestHmacSecret-32Bytes-LongEnough!"

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_01_running_tailscale_with_valid_ip_bound_locally(self) -> None:
        """Requirement 1: Running Tailscale with valid CGNAT/ULA IP bound to local interface succeeds."""
        status_payload = {
            "BackendState": "Running",
            "Self": {
                "TailscaleIPs": ["100.94.158.50", "fd7a:115c:a1e0::2601:9fee"],
                "DNSName": "home-mac.tailnet.ts.net",
            },
        }

        def mock_runner(cmd: List[str], timeout: float) -> Tuple[int, str, str]:
            return 0, json.dumps(status_payload), ""

        def mock_checker(ip: str) -> bool:
            return ip in {"100.94.158.50", "fd7a:115c:a1e0::2601:9fee"}

        res = probe_vpn_status(
            cmd_runner=mock_runner,
            bind_checker=mock_checker,
            cli_finder=lambda: "/mock/bin/tailscale",
        )
        self.assertTrue(res.connected)
        self.assertEqual(res.backend_state, "Running")
        self.assertIn("100.94.158.50", res.bound_ips)
        self.assertIn("100.94.158.50", res.detail)
        self.assertIn("Tailscale mesh active", res.detail)

    def test_02_running_tailscale_with_no_valid_assigned_ip(self) -> None:
        """Requirement 2: Running Tailscale with arbitrary non-CGNAT/non-ULA IPs fails closed."""
        status_payload = {
            "BackendState": "Running",
            "Self": {
                "TailscaleIPs": ["192.168.1.100", "1.2.3.4", "invalid-ip"],
            },
        }

        def mock_runner(cmd: List[str], timeout: float) -> Tuple[int, str, str]:
            return 0, json.dumps(status_payload), ""

        res = probe_vpn_status(
            cmd_runner=mock_runner,
            bind_checker=lambda ip: True,
            cli_finder=lambda: "/mock/bin/tailscale",
        )
        self.assertFalse(res.connected)
        self.assertEqual(res.backend_state, "Running")
        self.assertEqual(res.assigned_ips, [])
        self.assertEqual(res.bound_ips, [])
        self.assertIn("no valid 100.64.0.0/10 or fd7a:115c:a1e0::/48 address assigned", res.detail)

    def test_03_running_tailscale_with_assigned_ip_not_locally_bound(self) -> None:
        """Requirement 3: Tailscale reports assigned IP, but OS local bind fails -> fails closed."""
        status_payload = {
            "BackendState": "Running",
            "Self": {
                "TailscaleIPs": ["100.64.0.99"],
            },
        }

        def mock_runner(cmd: List[str], timeout: float) -> Tuple[int, str, str]:
            return 0, json.dumps(status_payload), ""

        # Bind checker returns False (simulating tunnel interface missing or suspended by OS)
        res = probe_vpn_status(
            cmd_runner=mock_runner,
            bind_checker=lambda ip: False,
            cli_finder=lambda: "/mock/bin/tailscale",
        )
        self.assertFalse(res.connected)
        self.assertEqual(res.backend_state, "Running")
        self.assertEqual(res.assigned_ips, ["100.64.0.99"])
        self.assertEqual(res.bound_ips, [])
        self.assertIn("none are bound to a local network interface", res.detail)

    def test_04_stopped_daemon_and_cli_failure(self) -> None:
        """Requirement 4: Stopped daemon state and non-zero CLI returncode fail closed."""
        # 4A: Stopped daemon
        stopped_payload = {"BackendState": "Stopped", "Self": {}}
        res_stopped = probe_vpn_status(
            cmd_runner=lambda c, t: (0, json.dumps(stopped_payload), ""),
            bind_checker=lambda ip: True,
            cli_finder=lambda: "/mock/bin/tailscale",
        )
        self.assertFalse(res_stopped.connected)
        self.assertEqual(res_stopped.backend_state, "Stopped")
        self.assertIn("BackendState=Stopped", res_stopped.detail)

        # 4B: CLI execution error (rc != 0)
        res_err = probe_vpn_status(
            cmd_runner=lambda c, t: (1, "", "Failed to connect to local tailscaled socket"),
            bind_checker=lambda ip: True,
            cli_finder=lambda: "/mock/bin/tailscale",
        )
        self.assertFalse(res_err.connected)
        self.assertEqual(res_err.backend_state, "DAEMON_UNAVAILABLE")
        self.assertIn("Failed to connect to local tailscaled socket", res_err.detail)

    def test_05_cli_timeout_and_malformed_json_and_missing_cli(self) -> None:
        """Requirement 5: Timeouts, malformed JSON, and missing CLI fail closed gracefully."""
        # 5A: Timeout
        res_to = probe_vpn_status(
            cmd_runner=lambda c, t: (-1, "", f"Command timed out after {t:.1f}s"),
            bind_checker=lambda ip: True,
            cli_finder=lambda: "/mock/bin/tailscale",
        )
        self.assertFalse(res_to.connected)
        self.assertIn("timed out", res_to.detail)

        # 5B: Malformed JSON
        res_json = probe_vpn_status(
            cmd_runner=lambda c, t: (0, "<html>Not JSON</html>", ""),
            bind_checker=lambda ip: True,
            cli_finder=lambda: "/mock/bin/tailscale",
        )
        self.assertFalse(res_json.connected)
        self.assertEqual(res_json.backend_state, "INVALID_JSON")
        self.assertIn("malformed JSON", res_json.detail)

        # 5C: Missing CLI binary
        res_no_cli = probe_vpn_status(
            cmd_runner=lambda c, t: (0, "", ""),
            bind_checker=lambda ip: True,
            cli_finder=lambda: None,
        )
        self.assertFalse(res_no_cli.connected)
        self.assertEqual(res_no_cli.backend_state, "CLI_NOT_FOUND")
        self.assertIn("CLI executable not found", res_no_cli.detail)

    def test_06_cache_ttl_prevents_repeated_subprocess_calls(self) -> None:
        """Requirement 6: Health checks within TTL reuse cached result without re-executing CLI."""
        call_count = 0
        valid_payload = {
            "BackendState": "Running",
            "Self": {"TailscaleIPs": ["100.94.158.50"]},
        }

        def counting_runner(cmd: List[str], timeout: float) -> Tuple[int, str, str]:
            nonlocal call_count
            call_count += 1
            return 0, json.dumps(valid_payload), ""

        health = RuntimeHealthCoordinator(freshness_ttl_seconds=15.0)
        health.vpn_probe_ttl_seconds = 10.0
        health._vpn_cmd_runner = counting_runner
        health._vpn_bind_checker = lambda ip: True
        health._vpn_cli_finder = lambda: "/mock/bin/tailscale"

        # 5 consecutive health checks
        for _ in range(5):
            res = health.check_vpn_status()
            self.assertTrue(res.connected)

        # Subprocess must have run exactly once
        self.assertEqual(call_count, 1)

    def test_07_cache_expiry_causes_fresh_probe(self) -> None:
        """Requirement 7: Cache expiry or force=True causes a fresh probe execution."""
        call_count = 0
        valid_payload = {
            "BackendState": "Running",
            "Self": {"TailscaleIPs": ["100.94.158.50"]},
        }

        def counting_runner(cmd: List[str], timeout: float) -> Tuple[int, str, str]:
            nonlocal call_count
            call_count += 1
            return 0, json.dumps(valid_payload), ""

        health = RuntimeHealthCoordinator(freshness_ttl_seconds=15.0)
        health.vpn_probe_ttl_seconds = 0.05  # 50ms TTL
        health._vpn_cmd_runner = counting_runner
        health._vpn_bind_checker = lambda ip: True
        health._vpn_cli_finder = lambda: "/mock/bin/tailscale"

        # Probe 1
        health.check_vpn_status()
        self.assertEqual(call_count, 1)

        # Force probe immediately
        health.check_vpn_status(force=True)
        self.assertEqual(call_count, 2)

        # Wait for TTL to expire
        time.sleep(0.06)
        health.check_vpn_status()
        self.assertEqual(call_count, 3)

    def test_08_local_only_mode_does_not_report_vpn_disconnected_as_failure(self) -> None:
        """Requirement 8: In local-only mode (remote_mode_enabled=False), VPN disconnect is NOT a failure."""
        mgr = RemoteAccessManager(
            state_path=self.state_dir / "remote.json",
            remote_mode_enabled=False,
        )
        self.assertFalse(mgr.remote_mode_enabled)

        health = RuntimeHealthCoordinator(freshness_ttl_seconds=15.0)
        health.remote_access_manager = mgr
        # Set VPN explicitly disconnected
        health.mark_vpn_status(False, "Tailscale disconnected or offline.")

        snap = health.evaluate()
        self.assertFalse(snap["signals"]["remote_client_connectivity"]["vpn_connected"])
        self.assertFalse(snap["signals"]["remote_client_connectivity"]["remote_mode_enabled"])
        self.assertFalse(snap["remote_available"])
        # Crucial check: VPN_DISCONNECTED must NOT appear in failure_reasons in local-only mode
        self.assertFalse(
            any("VPN_DISCONNECTED" in r for r in snap["failure_reasons"]),
            msg=f"Local-only mode must not fail on VPN disconnect: {snap['failure_reasons']}",
        )

    def test_09_remote_mode_with_vpn_disconnected_remains_unavailable_and_reports_failure(self) -> None:
        """Requirement 9: In remote mode (remote_mode_enabled=True), VPN disconnect is reported in failure_reasons."""
        mgr = RemoteAccessManager(
            state_path=self.state_dir / "remote.json",
            allowed_vpn_cidrs=["100.64.0.0/10"],
            state_hmac_secret=self.hmac_secret,
        )
        self.assertTrue(mgr.remote_mode_enabled)

        health = RuntimeHealthCoordinator(freshness_ttl_seconds=15.0)
        health.remote_access_manager = mgr
        health.mark_vpn_status(False, "Tailscale daemon stopped.")

        snap = health.evaluate()
        self.assertFalse(snap["signals"]["remote_client_connectivity"]["vpn_connected"])
        self.assertTrue(snap["signals"]["remote_client_connectivity"]["remote_mode_enabled"])
        self.assertFalse(snap["remote_available"])
        # VPN_DISCONNECTED must appear in failure_reasons when remote access is active
        self.assertTrue(any("VPN_DISCONNECTED" in r for r in snap["failure_reasons"]))

    def test_10_remote_mode_vpn_connected_requires_all_security_gates(self) -> None:
        """Requirement 10: Tailscale connected alone does NOT enable remote_available; TLS & owner gates required."""
        mgr = RemoteAccessManager(
            state_path=self.state_dir / "remote.json",
            allowed_vpn_cidrs=["100.64.0.0/10"],
            owner_secret="OwnerSecret!2026",
            state_hmac_secret=self.hmac_secret,
            tls_enabled=False,  # TLS NOT enabled yet
        )
        health = RuntimeHealthCoordinator(freshness_ttl_seconds=15.0)
        health.remote_access_manager = mgr
        health.mark_vpn_status(True, "Tailscale mesh active (100.94.158.50).")

        snap = health.evaluate()
        self.assertTrue(snap["signals"]["remote_client_connectivity"]["vpn_connected"])
        # remote_available must remain False because TLS is not enabled!
        self.assertFalse(snap["remote_available"])

        # When TLS is enabled, owner initialized, backend healthy, no shutdown -> remote_available is True
        mgr.tls_enabled = True
        snap_ok = health.evaluate()
        self.assertTrue(snap_ok["remote_available"])

    def test_11_owner_shutdown_and_backend_unhealthy_gates_enforced(self) -> None:
        """Requirement 11: Owner shutdown or backend unhealthy strictly forces remote_available to False."""
        mgr = RemoteAccessManager(
            state_path=self.state_dir / "remote.json",
            allowed_vpn_cidrs=["100.64.0.0/10"],
            owner_secret="OwnerSecret!2026",
            state_hmac_secret=self.hmac_secret,
            tls_enabled=True,
        )
        health = RuntimeHealthCoordinator(freshness_ttl_seconds=15.0)
        health.remote_access_manager = mgr
        health.mark_vpn_status(True, "Tailscale mesh active.")

        # Baseline: all gates satisfied
        snap_base = health.evaluate()
        self.assertTrue(snap_base["remote_available"])

        # 11A: Owner shutdown requested
        health.owner_shutdown_requested = True
        snap_shut = health.evaluate()
        self.assertFalse(snap_shut["remote_available"])
        health.owner_shutdown_requested = False

        # 11B: Backend unhealthy
        health.backend_healthy = False
        snap_back = health.evaluate()
        self.assertFalse(snap_back["remote_available"])


if __name__ == "__main__":
    unittest.main()

