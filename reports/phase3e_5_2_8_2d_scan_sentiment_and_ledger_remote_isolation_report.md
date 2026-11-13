# TradeLens AI — Phase 3E.5.2.8.2D: Scan Sentiment Fail-Closed Correction & Remote Ledger Read Isolation Report

**Phase:** Phase 3E.5.2.8.2D  
**Role:** Senior Python Backend Security Engineer & Independent Test Engineer  
**Machine:** Home Mac (`abhisheks-MacBook-Pro.local`)  
**Repository:** `/Users/abhisheksuman/Documents/GitHub/trading-assistant`  
**Branch:** `main`  
**HEAD at Execution:** `fd24424d563770684d33a37afc4f0d7b3b6969f8`  
**Execution Date:** 12 November 2026  
**Final Verdict:** `REMEDIATION_VERIFIED`

---

## 1. Executive Summary

During the Phase 3E.5.2.8.2C read-only data provenance and ledger authorization audit, two security and integrity concerns were identified:
1. **Unsubstantiated Market Sentiment Claim:** `GET /api/scan` defaulted to returning `"market_sentiment": "BULLISH"` even when the broker was disconnected, no live feeds were established, and `data_status` was `"UNAVAILABLE"`.
2. **Remote Exposure of Personal Financial Ledger:** `GET /api/ledger` was included in `ALLOWED_REMOTE_ROUTE_METHODS`, allowing any future authenticated remote Office Mac advisory session to view personal account balances, historical trades, realized P&L, and equity trajectories.

In Phase 3E.5.2.8.2D, targeted source-code remediations and automated regression tests were implemented to resolve both issues:
- **Remediation 1 (Fail-Closed Scan Sentiment):** In `backend/app/main.py`, the fallback payload returned by `TradeLensAppRuntime.get_scan_payload()` when the stock universe is empty or offline now strictly sets `"market_sentiment": "UNAVAILABLE"`. Callers cannot force a directional claim when market data is unavailable.
- **Remediation 2 (Remote Ledger Read Isolation):** In `backend/app/runtime/security.py`, `"/api/ledger": {"GET"}` was removed from `ALLOWED_REMOTE_ROUTE_METHODS`. As a result, all non-loopback requests to `/api/ledger` fail remote endpoint authorization and receive HTTP `403` with `reason_code: "ENDPOINT_NOT_AUTHORIZED_FOR_REMOTE"`.
- **Local Loopback Dashboard Preserved:** Requests originating from loopback (`127.0.0.1`) continue to be recognized as `LOOPBACK_LOCAL_AUTHORIZED`, allowing the Home Mac browser dashboard to load the ledger summary seamlessly without requiring owner credentials.
- **Ledger Mutation Routes Unchanged:** All mutation endpoints (`/api/ledger/trade`, `/api/ledger/trade/delete`, `/api/ledger/clear`, `/api/ledger/settings`, `/api/ledger/import-chat`) remain strictly protected by `HOME_OWNER_ONLY_ENDPOINTS` (`403 HOME_OWNER_ONLY_OPERATION`).
- **Full Test Suite Verification:** 4 new automated tests were added, bringing the backend test suite from 496 to **500 passing tests (500/500 OK in 76.2s)** with zero regressions across the codebase.

---

## 2. Preflight Baseline & Environment Inspection

Prior to performing any modifications, the host environment and Git baseline were verified:

| Parameter | Verified Value | Status |
| :--- | :--- | :--- |
| **Hostname** | `abhisheks-MacBook-Pro.local` | Verified Home Mac |
| **Repository Root** | `/Users/abhisheksuman/Documents/GitHub/trading-assistant` | Verified |
| **Git Branch** | `main` | Verified |
| **Git HEAD** | `fd24424d563770684d33a37afc4f0d7b3b6969f8` | Verified |
| **Python Environment** | `backend/.venv/bin/python3.12` (v3.12.13) | Verified |
| **Preserved Prior Reports** | 10 untracked phase reports (`phase3e_5_2_7_6` to `phase3e_5_2_8_2c`) | Preserved untouched |
| **Preserved Test Portability** | `backend/tests/test_gitignore_environment_protection.py` | Preserved untouched |

---

## 3. Remediation Details

### 3.1 Remediation A: Scan Sentiment Fail-Closed Correction

**File Modified:** `backend/app/main.py` (lines 456–466)

#### Problem
In `get_scan_payload(sentiment="BULLISH", top_n=5)`, when `stocks, snap = self._active_stock_universe()` returned an empty list `stocks = []` (due to missing broker feed, sleep invalidation, or offline mode), the fallback block previously constructed the response with `"market_sentiment": sentiment`. Because the parameter defaulted to `"BULLISH"`, the API advertised bullish sentiment on an empty, disconnected universe.

#### Code Change
```diff
--- a/backend/app/main.py
+++ b/backend/app/main.py
@@ -455,7 +455,7 @@ class TradeLensAppRuntime:
             result["all_watchlist_stocks"] = all_watch
         else:
             result = {
-                "market_sentiment": sentiment,
+                "market_sentiment": "UNAVAILABLE",
                 "top_movers": [],
                 "top_swing_setups": [],
                 "top_options_setups": [],
```

#### Behavior & Verification
- When market data is unavailable (`snap["data_status"] == "UNAVAILABLE"` and `stocks == []`), `payload["market_sentiment"]` is strictly `"UNAVAILABLE"`.
- Explicit caller arguments such as `get_scan_payload(sentiment="BULLISH")` or `sentiment="BEARISH"` cannot bypass this gate when market data is offline.
- When an active stock universe is present (e.g. live Angel One feed or simulated benchmark universe), `scan_and_shortlist_universe(stocks, top_n=top_n, market_sentiment=sentiment)` executes normally and preserves the requested market sentiment in its technical evaluation.

---

### 3.2 Remediation B: Remote Ledger Read Isolation

**File Modified:** `backend/app/runtime/security.py` (lines 60–70)

#### Problem
`ALLOWED_REMOTE_ROUTE_METHODS` contained `"/api/ledger": {"GET"}`. While ledger mutation routes (`/api/ledger/trade`, etc.) were protected as owner-only, a remote device with a valid ECDSA challenge-response bearer token (such as the Office Mac) was permitted to read personal financial ledger data via `GET /api/ledger`.

#### Code Change
```diff
--- a/backend/app/runtime/security.py
+++ b/backend/app/runtime/security.py
@@ -63,7 +63,6 @@ ALLOWED_REMOTE_ROUTE_METHODS: Dict[str, Set[str]] = {
     "/api/positions/monitor": {"POST"},
     "/api/verify-tip": {"POST"},
     "/api/universe": {"GET"},
-    "/api/ledger": {"GET"},
     "/api/remote/heartbeat": {"POST"},
     "/api/remote/logout": {"POST"},
     "/api/setup/state": {"GET"},
```

#### Enforcement Architecture
1. **Remote Request Pipeline:**
   When a request arrives from a remote workstation (e.g. `client_ip = "100.64.0.25"`):
   - `RemoteAccessManager.authorize_request(path, method, client_ip, ...)` checks `is_endpoint_allowed_for_remote(raw_path, method)`.
   - Because `"/api/ledger"` is absent from `ALLOWED_REMOTE_ROUTE_METHODS`, `is_endpoint_allowed_for_remote("/api/ledger", "GET")` returns `False`.
   - Authorization immediately fails with HTTP `403` and payload:
     ```json
     {
       "error": "Endpoint '/api/ledger' (GET) is not authorized for remote sessions.",
       "reason_code": "ENDPOINT_NOT_AUTHORIZED_FOR_REMOTE",
       "remote_available": false,
       "storage_policy": "MEMORY_ONLY"
     }
     ```
   - This check executes before any handler logic or ledger file access occurs. Even a cryptographically authenticated session cannot access the ledger.
2. **Local Loopback Preservation:**
   When a request arrives from the Home Mac local loopback (`client_ip = "127.0.0.1"`):
   - `RemoteAccessManager.authorize_request` verifies `client_ip == "127.0.0.1"` and `not self.remote_enabled`.
   - It returns `allowed = True` with `reason_code = "LOOPBACK_LOCAL_AUTHORIZED"` and HTTP `200`.
   - The Home Mac local frontend (`http://127.0.0.1:8080`) can read the ledger summary and populate its balance and P&L widgets without requiring owner passwords.
3. **Owner-Only Mutation Endpoints:**
   Mutation routes remain in `HOME_OWNER_ONLY_ENDPOINTS`:
   - `/api/ledger/trade`
   - `/api/ledger/trade/delete`
   - `/api/ledger/clear`
   - `/api/ledger/import-chat`
   - `/api/ledger/settings`
   Any remote attempt to POST to these routes is rejected with HTTP `403` and `reason_code: "HOME_OWNER_ONLY_OPERATION"`.
4. **Legitimate Remote Read Endpoints Preserved:**
   Advisory endpoints remain fully authorized for authenticated remote sessions:
   - `/api/scan` (`GET`)
   - `/api/health` (`GET`)
   - `/api/indices` (`GET`)
   - `/api/universe` (`GET`)

---

## 4. Automated Regression Test Implementation

### 4.1 New Tests in `backend/tests/test_broker_integration_and_data_status.py`

Three test methods were added to verify scan sentiment fail-closed semantics:

1. `test_scan_sentiment_unavailable_when_market_data_empty`:
   Instantiates `TradeLensAppRuntime(allow_sample_fallback=False)` and confirms that `get_scan_payload()` returns `data_status == "UNAVAILABLE"` and `market_sentiment == "UNAVAILABLE"`.
2. `test_scan_sentiment_fail_closed_cannot_be_overridden_when_data_unavailable`:
   Iterates through `"BULLISH"`, `"BEARISH"`, and `"NEUTRAL"` overrides and verifies that `get_scan_payload(sentiment=...)` strictly returns `market_sentiment: "UNAVAILABLE"` when data is unavailable.
3. `test_scan_sentiment_preserved_when_universe_active`:
   Verifies that when stock data is present (e.g. `allow_sample_fallback=True` or live data), `get_scan_payload(sentiment="BEARISH")` preserves `"BEARISH"` in the scanner payload.

### 4.2 New Test in `backend/tests/test_remote_access_security_audit.py`

`test_21_ledger_remote_read_isolation_and_loopback_access` was added to verify end-to-end HTTP pipeline enforcement:

- **Step 1:** Verifies local loopback `GET /api/ledger` (`127.0.0.1`) returns HTTP `200` with `current_balance`, `overall_pnl`, and `trades`.
- **Step 2:** Verifies remote unauthenticated `GET /api/ledger` (`100.64.0.25`) returns HTTP `403` with `reason_code: "ENDPOINT_NOT_AUTHORIZED_FOR_REMOTE"`.
- **Step 3:** Verifies remote authenticated `GET /api/ledger` with valid ECDSA P-256 session token returns HTTP `403` with `reason_code: "ENDPOINT_NOT_AUTHORIZED_FOR_REMOTE"`.
- **Step 4:** Verifies remote authenticated `GET` to `/api/scan`, `/api/health`, `/api/indices`, and `/api/universe` returns HTTP `200`.
- **Step 5:** Verifies remote POST to `/api/ledger/trade`, `/api/ledger/trade/delete`, `/api/ledger/clear`, `/api/ledger/settings`, and `/api/ledger/import-chat` returns HTTP `403` with `reason_code: "HOME_OWNER_ONLY_OPERATION"`.

---

## 5. Test Verification Evidence

### 5.1 Targeted Test Suite Results

```
======================================================================
1. test_broker_integration_and_data_status.py
----------------------------------------------------------------------
Ran 7 tests in 0.143s
OK

2. test_remote_access_security_audit.py
----------------------------------------------------------------------
Ran 21 tests in 3.996s
OK

3. test_pre_provisioning_security_gate_stage_a1.py
----------------------------------------------------------------------
Ran 16 tests in 4.433s
OK

4. test_stage_b_setup_and_mise.py
----------------------------------------------------------------------
Ran 17 tests in 5.935s
OK

5. test_home_mac_runtime_and_remote_availability.py
----------------------------------------------------------------------
Ran 10 tests in 1.038s
OK

6. test_gitignore_environment_protection.py
----------------------------------------------------------------------
Ran 6 tests in 0.745s
OK

7. test_private_network_provisioning_stage_a.py
----------------------------------------------------------------------
Ran 8 tests in 1.645s
OK

8. test_api.py (Core Engine Smoke Test)
----------------------------------------------------------------------
  ✔ Black-Scholes Greeks calculation: PASSED (ATM Delta ~0.50)
  ✔ Universe shortlisting scanner: PASSED (0 swing, 0 options, 5 vetoed)
  ✔ 'Why NOT to Trade' Veto Gate: PASSED (Blocked earnings event risk)
ALL TESTS PASSED! 🚀
```

### 5.2 Full Backend Regression Suite (Unittest Discover)

```
----------------------------------------------------------------------
Ran 500 tests in 76.191s

OK
```
**Total Passing Tests:** 500 / 500 (100% pass rate, 0 failures, 0 errors).

---

## 6. Dataset Integrity & Environment Hygiene

### 6.1 Spot Dataset Checksums

All 4 NIFTY spot dataset and checkpoint files were re-verified via SHA-256:

| File Path | SHA-256 Hash | Integrity Status |
| :--- | :--- | :--- |
| `data/checkpoints/checkpoint_nifty_2026-11-02_2026-11-06.json` | `3be81f5e3ef7955343cadc43bc6e438285de070fd78d530610ea7b3276917adf` | **MATCH** |
| `data/checkpoints/raw_nifty_2026-11-02_2026-11-06_chunk_0.json` | `9c7008c81e2045e6fc52fc5a197cf9891d30e45d38d9f342b03a013365fa0065` | **MATCH** |
| `data/historical/spot/NIFTY/manifests/manifest_nifty_69ca9b77.json` | `bd0ae4959ee8d3ed5c9bea5877343f74883894d71651a74bbfc19fd8d8943d34` | **MATCH** |
| `data/historical/spot/NIFTY/year=2026/month=11/candles_NIFTY_2026_11.csv` | `2f3010fbca42bebbd0c7ce5c8876aa7a954c1bca48a304fd2758c906ce7ebde0` | **MATCH** |

### 6.2 Environment & Secrets Hygiene

- `.env` file attributes: Mode `0600` (`-rw-------@`), size `193` bytes, last modified Nov 11 21:32:04 2026.
- The `.env` file was not read, printed, copied, or modified.
- No live broker connections, TOTP authentications, or network calls were initiated.
- No persistent background services, LaunchAgents, or Tailscale changes were introduced.

---

## 7. Git Workspace Status

```
$ git status --porcelain
 M backend/app/main.py
 M backend/app/runtime/security.py
 M backend/tests/test_broker_integration_and_data_status.py
 M backend/tests/test_gitignore_environment_protection.py
 M backend/tests/test_remote_access_security_audit.py
?? reports/phase3e_5_2_7_6_home_mac_local_verification_report.md
?? reports/phase3e_5_2_7_7_home_mac_test_portability_report.md
?? reports/phase3e_5_2_7_8_home_mac_foreground_runtime_report.md
?? reports/phase3e_5_2_7_9_home_mac_checksum_and_runtime_baseline_report.md
?? reports/phase3e_5_2_8_1a_home_mac_tailscale_post_install_verification.md
?? reports/phase3e_5_2_8_2_home_mac_tailscale_policy_connectivity_verification.md
?? reports/phase3e_5_2_8_2a_home_mac_local_frontend_launch_report.md
?? reports/phase3e_5_2_8_2b_home_mac_full_local_integration_report.md
?? reports/phase3e_5_2_8_2c_api_data_provenance_ledger_security_audit.md
?? reports/phase3e_5_2_8_home_mac_stage_b_readiness_report.md
```

- Exactly 2 production source files modified (`backend/app/main.py`, `backend/app/runtime/security.py`).
- Exactly 2 test files modified for regression verification (`backend/tests/test_broker_integration_and_data_status.py`, `backend/tests/test_remote_access_security_audit.py`).
- 1 previously modified portability test preserved (`backend/tests/test_gitignore_environment_protection.py`).
- 10 untracked phase audit reports preserved untouched.
- Zero commits, pushes, stashes, or resets were executed.

---

## 8. Conclusion & Sign-off

The two findings from Phase 3E.5.2.8.2C are now fully remediated and validated:
1. `/api/scan` no longer hallucinates directional sentiment when market data is unavailable.
2. `/api/ledger` is completely blocked from remote access while remaining functional on the local Home Mac dashboard.
3. The test suite has expanded to 500 passing tests with zero regressions.

**Final Phase Verdict:** `REMEDIATION_VERIFIED`

