# Phase 3E.5.2.8.2F: Home Mac VPN Detection Source Audit Report

**Phase:** Phase 3E.5.2.8.2F  
**Date:** 12 November 2026  
**Role:** Senior macOS Infrastructure Engineer & Security Auditor  
**Machine:** Home Mac ONLY (`abhisheks-MacBook-Pro.local`)  
**Repository:** `/Users/abhisheksuman/Documents/GitHub/trading-assistant`  
**Branch:** `main` (`HEAD`: `fd24424d563770684d33a37afc4f0d7b3b6969f8`)  
**Mode:** Strictly Read-Only Source Inspection  
**Verdict:** `DETECTION_MISMATCH_REQUIRES_REMEDIATION`

---

## 1. Executive Summary

During Phase 3E.5.2.8.2B and subsequent runtime inspections, `GET /api/health` consistently reported:
```json
"vpn_connected": false,
"vpn_detail": "Private VPN interface not verified."
```
with `failure_reasons` listing:
```
"VPN_DISCONNECTED: Private VPN interface not verified."
```
Meanwhile, macOS independent verification confirmed that Tailscale is fully installed, signed in, and operational:
- Tailscale daemon / backend state: `Running`
- Tailscale online: `true`
- Tailscale macOS interface: `utun4`
- Tailscale IPv4: `100.94.158.50`
- Tailscale IPv6: `fd7a:115c:a1e0::2601:9fee`
- Tailscale MagicDNS FQDN: `abhisheks-macbook-pro.tailc9c809.ts.net`

This strictly read-only source audit investigated why TradeLens AI fails to recognize the active Tailscale network.

### Key Audit Finding
The discrepancy is caused by a **complete absence of runtime detection wiring in `RuntimeHealthCoordinator`**:
1. `RuntimeHealthCoordinator` initializes `self.vpn_connected = False` and `self.vpn_detail = "Private VPN interface not verified."`.
2. It provides a passive setter method `mark_vpn_status(connected, detail)`, but **zero production callers exist anywhere in the backend runtime**.
3. While a functional Tailscale CLI probe (`_probe_tailscale()`) exists in `backend/app/runtime/stage_b_setup.py:1234` (and immediately detects `100.94.158.50` when invoked), it is exclusively queried by the setup coordinator (`GET /api/setup/state`) and is never called by `RuntimeHealthCoordinator` or `TradeLensAppRuntime.get_health_payload()`.
4. Existing automated tests bypassed this missing detection by manually injecting `health.mark_vpn_status(True, ...)` directly into the mock test instance, masking the absence of production detection logic.
5. In addition, `health.evaluate()` unconditionally treats `vpn_connected: false` as a failure in `failure_reasons` even when the engine is running in purely local loopback mode (`127.0.0.1:8000`).

---

## 2. Source Code Architecture & Detection Flow Trace

### 2.1 File & Function Inventory

| File | Lines | Component / Function | Purpose |
| :--- | :--- | :--- | :--- |
| `backend/app/runtime/health_and_safety.py` | 122–123 | `RuntimeHealthCoordinator.__init__` | Default state: `self.vpn_connected = False`, `self.vpn_detail = "Private VPN interface not verified."` |
| `backend/app/runtime/health_and_safety.py` | 359–361 | `RuntimeHealthCoordinator.mark_vpn_status` | Setter to update `self.vpn_connected` and `self.vpn_detail`. Has **0 production callers**. |
| `backend/app/runtime/health_and_safety.py` | 421–428 | `RuntimeHealthCoordinator.evaluate` | Injects `self.vpn_connected` and `self.vpn_detail` into `remote_summary`. |
| `backend/app/runtime/health_and_safety.py` | 482–483 | `RuntimeHealthCoordinator.evaluate` | Unconditionally appends `VPN_DISCONNECTED` to `failure_reasons` when `not self.vpn_connected`. |
| `backend/app/runtime/health_and_safety.py` | 509–513 | `RuntimeHealthCoordinator.evaluate` | Forces `remote_available = False` when `not self.vpn_connected`. |
| `backend/app/main.py` | 210–240 | `TradeLensAppRuntime.__init__` | Instantiates `RuntimeHealthCoordinator` and `RemoteAccessManager`. Never calls `mark_vpn_status`. |
| `backend/app/main.py` | 400–425 | `TradeLensAppRuntime.get_health_payload` | Calls `self.health.evaluate()`, wraps result into HTTP payload for `GET /api/health`. |
| `backend/app/runtime/stage_b_setup.py` | 1221–1231 | `_find_tailscale_bin()` | Searches candidates: `shutil.which("tailscale")`, `/opt/homebrew/bin/tailscale`, `/usr/local/bin/tailscale`, `/Applications/Tailscale.app/...`. |
| `backend/app/runtime/stage_b_setup.py` | 1234–1300 | `_probe_tailscale()` | Executes `tailscale status --json` and parses `BackendState`, `Self.TailscaleIPs`, peers. |
| `backend/app/runtime/stage_b_setup.py` | 2132 | `StageBSetupCoordinator._collect_live_probes` | **The ONLY caller** of `_probe_tailscale()`, exposed strictly via `GET /api/setup/state`. |
| `backend/app/runtime/security.py` | 345–395 | `_is_tailscale_or_custom_vpn_ip()` | Validates whether an incoming socket IP is in `100.64.0.0/10` or `fd7a:115c:a1e0::/48`. |
| `backend/tests/test_home_mac_runtime_and_remote_availability.py` | 631, 711 | Test cases | Manually injects `health.mark_vpn_status(True, ...)` to pass health assertions. |

---

### 2.2 Complete Health Evaluation Flow

```
HTTP GET /api/health
  │
  ▼
ApiHandler.do_GET()  [backend/app/main.py:614]
  │
  ▼
TradeLensAppRuntime.get_health_payload()  [backend/app/main.py:400]
  │
  ▼
RuntimeHealthCoordinator.evaluate()  [backend/app/runtime/health_and_safety.py:414]
  │
  ├─► Reads self.vpn_connected (Value: False) [Set at init, line 122]
  ├─► Reads self.vpn_detail (Value: "Private VPN interface not verified.") [Set at init, line 123]
  │
  ├─► remote_summary["vpn_connected"] = False
  ├─► remote_summary["vpn_detail"] = "Private VPN interface not verified."
  │
  ├─► if not self.vpn_connected:  [Line 482]
  │     failure_reasons.append("VPN_DISCONNECTED: Private VPN interface not verified.")
  │
  └─► remote_available = (self.backend_healthy and not self.owner_shutdown_requested and self.vpn_connected)
        └─► Evaluates to FALSE
```

**Observation:** There is no conditional check, no subprocess execution, no socket inspection, and no network probe anywhere in this path. The returned values are purely the default uninitialized variables from object instantiation.

---

## 3. Detailed Audit Findings Against Scope Questions

### 3.1 What Does VPN Detection Actually Check?

| Mechanism | Checked by `/api/health`? | Checked by `/api/setup/state`? | Checked by `security.py`? |
| :--- | :---: | :---: | :---: |
| **Interface Names (`utun*`)** | **NO** | **NO** | **NO** |
| **Tailscale IPv4/IPv6 Addresses** | **NO** | **YES** (`Self.TailscaleIPs`) | **YES** (Incoming client IP only) |
| **Tailscale CLI Status** | **NO** | **YES** (`tailscale status --json`) | **NO** |
| **Network Routing Tables** | **NO** | **NO** | **NO** |
| **macOS System Configuration (`scutil`)** | **NO** | **NO** | **NO** |

**Conclusion:** Neither `/api/health` nor any other TradeLens module checks `utun*` interface names. The notion that TradeLens is failing because it looks for `utun0` instead of `utun4` is incorrect: TradeLens does not inspect OS network interfaces at all.

---

### 3.2 Verification of Independent Tailscale Detection

To verify whether the existing `_probe_tailscale` in `stage_b_setup.py` is capable of detecting Tailscale on this Home Mac, it was executed directly in a Python shell during this audit:

```python
>>> from backend.app.runtime.stage_b_setup import _probe_tailscale
>>> res = _probe_tailscale()
>>> for k, v in res.items(): print(f"{k}: {v}")
installed: True
app_present: True
cli_path: /usr/local/bin/tailscale
running: True
backend_state: Running
self_ips: ['100.94.158.50', 'fd7a:115c:a1e0::2601:9fee']
self_dns_name: abhisheks-macbook-pro.tailc9c809.ts.net
peer_count: 0
peers: []
detail: Tailscale BackendState=Running with 2 local tailnet IP(s).
```

**Findings:**
1. Tailscale CLI is located at `/usr/local/bin/tailscale`.
2. Tailscale IPC responds in **33ms**.
3. Tailscale reports `BackendState: Running`.
4. Both IPv4 (`100.94.158.50`) and IPv6 (`fd7a:115c:a1e0::2601:9fee`) are present and valid within official Tailscale CGNAT and ULA prefixes.
5. **The code to detect Tailscale already exists in the repository, but is completely unlinked from the health coordinator.**

---

### 3.3 Root Cause Analysis

Why is the confirmed Tailscale interface reported as disconnected?

1. **Architectural Separation without Integration:**
   - In Stage A, `health_and_safety.py` was authored with an event-driven hook (`mark_vpn_status`), expecting an external supervisor or monitoring loop to push VPN status changes into the coordinator.
   - In Stage B, `_probe_tailscale()` was created inside `stage_b_setup.py` specifically for the interactive Stage B setup wizard (`GET /api/setup/state`).
   - The loop was never closed: `RuntimeHealthCoordinator` was never connected to `_probe_tailscale()`, leaving `self.vpn_connected` perpetually `False`.

2. **Test Masking / Artificial Verification:**
   - In `backend/tests/test_home_mac_runtime_and_remote_availability.py:631`, the test setup manually calls:
     ```python
     health.mark_vpn_status(True, "Tailscale peer reachable.")
     ```
   - This caused all 10 tests in that file to pass green, giving the false appearance that VPN status was being evaluated dynamically, when in fact the production runtime never calls `mark_vpn_status()`.

3. **Overly Broad Failure Classification in Local Mode:**
   - In `RuntimeHealthCoordinator.evaluate()` (line 482):
     ```python
     if not self.vpn_connected:
         failure_reasons.append(f"VPN_DISCONNECTED: {self.vpn_detail}")
     ```
   - When TradeLens is run as a local-only application (e.g. `python -m backend.app.main` with `--bind-host 127.0.0.1`), `remote_mode_enabled` is `False`. The user is not attempting remote access.
   - Yet `failure_reasons` unconditionally reports `VPN_DISCONNECTED: Private VPN interface not verified.`, falsely presenting a local system as degraded or unhealthy.

---

## 4. Safety Invariants & Guardrails for Remediation

Per the strict safety boundaries:
1. **Never treat `utun*` presence alone as Tailscale:** An interface named `utun4` could be created by other VPN clients or Apple Private Relay. Checking `utun*` alone is unsafe.
2. **Never treat control-plane connectivity alone as interface proof:** A Tailscale daemon might be connected to `login.tailscale.com` while its local network tunnel interface failed to bind or is suspended by macOS network management.
3. **Corroboration Requirement:** Safe detection requires:
   - Control plane confirms: `BackendState == "Running"`.
   - Node has assigned IP in Tailscale range (`100.64.0.0/10` or `fd7a:115c:a1e0::/48`).
   - The assigned IP is bound to a local OS interface (verified via OS network query or bindability).
4. **Subprocess Throttling / Rate-Limiting:** `GET /api/health` is polled frequently (e.g. every 1–2 seconds) by dashboards and monitoring scripts. Running `tailscale status --json` on every single HTTP GET would create unnecessary CPU and process churn. The probe result must be cached with a sensible TTL (e.g. 5–10 seconds).

---

## 5. Test Gaps Identified

1. **No Automated VPN Detection Test:** No test exists that verifies whether `RuntimeHealthCoordinator` or `TradeLensAppRuntime` can discover Tailscale status without manual test intervention.
2. **Missing Local vs Remote Mode Distinction Test:** No test checks whether `failure_reasons` omits `VPN_DISCONNECTED` when running in local loopback mode (`remote_mode_enabled=False`).
3. **Missing TTL / Probe Throttling Test:** No test verifies that repeated calls to `get_health_payload()` do not spam subprocess invocations.
4. **Missing Offline / Stopped Tailscale Simulation:** No test verifies that when `tailscale status` exits non-zero or reports `Stopped`, `vpn_connected` cleanly fails closed to `False` with an informative detail message.

---

## 6. Recommended Remediation Plan (Smallest Safe Changes)

When authorized in a subsequent phase, the following minimal remediation should be applied:

### Step 1: Promote Tailscale Probing to a Shared, Cached Runtime Utility
- Move or expose `_probe_tailscale()` from `stage_b_setup.py` so it can be utilized by `RuntimeHealthCoordinator`.
- Add a cached wrapper (e.g., `check_vpn_status(ttl_seconds=10.0)`) that avoids executing subprocesses on consecutive requests within the TTL window.
- Ensure the probe verifies both `BackendState == "Running"` AND `Self.TailscaleIPs` matches `100.64.0.0/10` or `fd7a:115c:a1e0::/48`.

### Step 2: Wire Detection into `RuntimeHealthCoordinator.evaluate()`
- In `RuntimeHealthCoordinator.evaluate()`, if `self.remote_access_manager` indicates `remote_mode_enabled` (or on scheduled health checks), call the cached VPN probe to update `self.vpn_connected` and `self.vpn_detail`.
- If Tailscale is running and bound to `100.94.158.50`, update:
  - `self.vpn_connected = True`
  - `self.vpn_detail = "Tailscale mesh active (100.94.158.50)."`

### Step 3: Scope `failure_reasons` to Remote Mode
- Modify line 482 of `backend/app/runtime/health_and_safety.py`:
  ```python
  remote_enabled = getattr(self.remote_access_manager, "remote_mode_enabled", False)
  if remote_enabled and not self.vpn_connected:
      failure_reasons.append(f"VPN_DISCONNECTED: {self.vpn_detail}")
  ```
  In local loopback mode, `vpn_connected` and `vpn_detail` remain accurately reported under `signals.remote_client_connectivity`, but do not pollute `failure_reasons` as an active engine failure.

### Step 4: Add Automated Unit & Integration Tests
- Add tests in `backend/tests/test_home_mac_runtime_and_remote_availability.py` using a mock subprocess runner to verify:
  1. `BackendState == "Running"` with `100.x` IP -> `vpn_connected: True`, `remote_available: True`.
  2. `BackendState != "Running"` -> `vpn_connected: False`, `remote_available: False`.
  3. `remote_mode_enabled == False` -> `failure_reasons` does NOT contain `VPN_DISCONNECTED`.
  4. Cache TTL prevents rapid subprocess respawning.

---

## 7. Audit Verdict

`DETECTION_MISMATCH_REQUIRES_REMEDIATION`

**Sign-off:** The Home Mac Tailscale daemon and tunnel interface (`utun4`, `100.94.158.50`) are fully functional. The `vpn_connected: false` report is caused exclusively by absent runtime detection wiring and an unlinked probe function in the TradeLens backend.

