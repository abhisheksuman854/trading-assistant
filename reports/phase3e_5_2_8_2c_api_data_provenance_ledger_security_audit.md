# Phase 3E.5.2.8.2C: API Data Provenance & Ledger Authorization Audit Report

**Date:** 2026-11-12  
**Machine:** Home Mac ONLY (`abhisheks-MacBook-Pro.local`)  
**Repository:** `/Users/abhisheksuman/Documents/GitHub/trading-assistant`  
**Branch:** `main` (`HEAD`: `fd24424d563770684d33a37afc4f0d7b3b6969f8`)  
**Mode:** Strictly Read-Only Source Inspection  
**Verdict:** `FINDINGS_REQUIRE_REMEDIATION`

---

## 1. Executive Summary

During Phase 3E.5.2.8.2B local integration testing, two data anomalies were recorded:
1. `GET /api/scan` emitted `"market_sentiment": "BULLISH"` despite `"data_status": "UNAVAILABLE"`.
2. `GET /api/ledger` emitted `"current_balance": 84910.0` and `"overall_pnl": 34910.0`.

This read-only audit traces the exact code paths, storage structures, and security access controls governing both endpoints.

### Key Audit Findings
1. **Misleading Default Market Sentiment:** `"BULLISH"` is an uncalculated hardcoded fallback parameter (`backend/app/main.py:624`, `get_scan_payload(sentiment: str = "BULLISH")`). When `stocks` is empty (as when `data_status == "UNAVAILABLE"`), line 458 simply echoes this default parameter as current sentiment. While `all_watchlist_stocks`, `top_swing_setups`, and `indices` are correctly empty, the presence of `"market_sentiment": "BULLISH"` when market data is unavailable is semantically misleading.
2. **Ledger Data Provenance:** `current_balance: 84910.0` and `overall_pnl: 34910.0` originate from `backend/app/data/ledger_store.json` (tracked in Git commit `eaeb2cd`). This file contains 19 historical trades (Oct 14 – Nov 4, 2026) reconstructed from user/co-partner trade logs with an initial capital of ₹50,000. It is loaded by `CapitalLedgerManager` (`backend/app/core/ledger.py:291-446`) which computes dynamic net P&L and daily circuit-breaker metrics.
3. **Ledger Authorization Gap for Remote Clients:** While all state-mutating ledger routes (`/api/ledger/trade`, `/api/ledger/clear`, etc.) are locked down in `HOME_OWNER_ONLY_ENDPOINTS` (`security.py:86-91`), the read-only route `GET /api/ledger` is currently listed in `ALLOWED_REMOTE_ROUTE_METHODS` (`security.py:66`). Any authenticated remote Office Mac session would have full read access to the user's trading journal and financial balance.

---

## 2. Scan Endpoint Provenance & Semantic Analysis

### Source Files & Functions
- **Handler:** `backend/app/main.py:623-630` (`TradeLensAPIHandler.do_GET`)
- **Payload Method:** `backend/app/main.py:427-475` (`TradeLensAppRuntime.get_scan_payload`)
- **Universe Evaluation:** `backend/app/main.py:372-398` (`TradeLensAppRuntime._active_stock_universe`)
- **Scoring Engine:** `backend/app/core/shortlist_engine.py:329-370` (`scan_and_shortlist_universe`)

### Provenance Trace
1. In `do_GET` (`main.py:624`):
   ```python
   sentiment = query.get("sentiment", ["BULLISH"])[0]
   self._send_json(200, runtime.get_scan_payload(sentiment=sentiment, top_n=top_n), ...)
   ```
2. In `get_scan_payload` (`main.py:428-466`):
   - `stocks, snap = self._active_stock_universe()`
   - When broker authentication is missing, `stocks` is `[]` and `snap["data_status"]` is `"UNAVAILABLE"`.
   - Lines 456–466 execute the `else` branch:
     ```python
     result = {
         "market_sentiment": sentiment,
         "top_movers": [],
         "top_swing_setups": [],
         "top_options_setups": [],
         "avoid_or_vetoed": [],
         "indices": [],
         "live_portfolio": {},
         "all_watchlist_stocks": [],
     }
     ```
3. Lines 468–475 append metadata:
   - `result["data_source"] = "UNAVAILABLE"`
   - `result["data_status"] = "UNAVAILABLE"`

### Semantic Flaw & Risk Assessment
- **Cause:** When market data is unavailable, `market_sentiment` is not calculated from market indices, advance-decline ratios, or technical structures; it is purely reflecting the unvalidated HTTP query parameter default (`"BULLISH"`).
- **Risk:** An automated client, downstream service, or human trader looking only at `response["market_sentiment"]` might interpret this as an active bullish market condition when the feed is entirely disconnected.
- **Remediation Required:** When `stocks` is empty or `snap["data_status"] != "LIVE"`, `market_sentiment` must be set to `"UNAVAILABLE"` (or `None`), never echoing a synthetic or default directional bias.

---

## 3. Ledger Endpoint Provenance & Authorization

### Source Files & Functions
- **Handler:** `backend/app/main.py:691-693` (`TradeLensAPIHandler.do_GET`)
- **Engine:** `backend/app/core/ledger.py:291-446` (`CapitalLedgerManager`)
- **Storage Path:** `backend/app/data/ledger_store.json` (also fallback hardcoded `CHAT_TRADE_HISTORY` in `ledger.py:21-288`)
- **Access Policy:** `backend/app/runtime/security.py:66, 86-91, 2469-2525`

### Data Classification & Provenance
- **Contents of `ledger_store.json`:**
  - `initial_capital`: `50000.0`
  - `max_daily_loss_pct`: `5.0`
  - `trades`: 19 historical F&O options/equity trades from October 14, 2026 to November 4, 2026.
  - Reconstructed from user/co-partner trade records and chat logs.
- **Computed Metrics:**
  - `current_balance`: `initial_capital + sum(pnl) = 50000.0 + 34910.0 = 84910.0`.
  - `overall_pnl`: `34910.0` (`overall_pnl_pct`: `69.82%`).
  - `today_pnl`: `0.0`, `today_trades_count`: `0` (clean reset for today).
  - `max_daily_loss_rupees`: `2500.0`, `circuit_breaker_triggered`: `false`.
- **Classification:** **Sensitive User Financial Data**. While it does not contain broker passwords, bank account numbers, or PANs, it exposes actual trading capital, personal trade history, entry/exit prices, profit/loss records, and risk rules.

### Authorization Architecture & Evaluation
- **Local Loopback (`127.0.0.1`):** `GET /api/ledger` allows unauthenticated read access from loopback so the local frontend dashboard can render the journal immediately without logging in.
- **Mutating Endpoints:** `POST /api/ledger/trade`, `POST /api/ledger/trade/delete`, `POST /api/ledger/clear`, `POST /api/ledger/import-chat`, and `POST /api/ledger/settings` are strictly guarded under `HOME_OWNER_ONLY_ENDPOINTS` (`security.py:86-91`):
  - Rejected with `403 HOME_OWNER_ONLY_OPERATION` from any non-loopback IP.
  - Require valid Home Mac owner secret authentication even on loopback (`security.py:2481-2490`).
- **Remote Read Access Gap:**
  - In `backend/app/runtime/security.py:66`:
    `ALLOWED_REMOTE_ROUTE_METHODS = { ... "/api/ledger": {"GET"}, ... }`
  - Under this policy, any approved Office Mac remote session holding an authenticated bearer token can execute `GET /api/ledger` and read the full financial journal and capital balance.

---

## 4. Remote Access Implications & Risk Analysis

| Scenario | Local Home Mac | Remote Office Mac (via Tailscale) | Risk Level |
|---|---|---|---|
| Read Ledger Balance & Trades (`GET /api/ledger`) | Allowed (Loopback SPA) | Allowed (if device authenticated) | **MEDIUM** (Data exposure: Office Mac receives personal financial history) |
| Mutate Ledger Records (`POST /api/ledger/*`) | Owner-only (`x-tradelens-owner-secret`) | **Forbidden (`403`)** | **LOW** (Properly blocked by `HOME_OWNER_ONLY_ENDPOINTS`) |
| Directional Bias on Stale Feed (`GET /api/scan`) | `"market_sentiment": "BULLISH"` | `"market_sentiment": "BULLISH"` | **MEDIUM** (Misleading directional signal while feed is down) |

---

## 5. Test Coverage Audit

### Existing Test Coverage
- `backend/tests/test_remote_access_security_audit.py`:
  - Verifies that mutating ledger routes (`/api/ledger/trade`, etc.) return `403` from remote peers and require owner secrets.
  - Verifies `ALLOWED_REMOTE_ROUTE_METHODS` enforcement.
- `backend/tests/test_broker_integration_and_data_status.py`:
  - Verifies broker disconnection returns empty data and data status tags.

### Missing Test Coverage
1. **Scan Sentiment with Unavailable Data:** No test currently asserts that `/api/scan` suppresses `"market_sentiment": "BULLISH"` when `data_status == "UNAVAILABLE"`.
2. **Ledger Remote Read Authorization:** No test verifies whether the Home Mac owner explicitly intended to share the capital ledger with the remote Office Mac workstation, or whether `GET /api/ledger` should be restricted to loopback or require owner-tier permission.
3. **Sentiment Query Parameter Validation:** No test verifies that invalid or unexpected `?sentiment=` query parameters are validated or rejected.

---

## 6. Prioritized Remediation Recommendations

### Priority 1: High — Fix Scan Sentiment when Data is Unavailable
- **Location:** `backend/app/main.py:456-466`
- **Fix:** In `get_scan_payload`, when `not stocks` or `snap["data_status"] != "LIVE"`:
  Set `"market_sentiment": "UNAVAILABLE"` (or `"DISCONNECTED"`), rather than reflecting `sentiment`.
- **Add Unit Test:** In `backend/tests/test_broker_integration_and_data_status.py`, verify `get_scan_payload()["market_sentiment"] == "UNAVAILABLE"` when `data_status == "UNAVAILABLE"`.

### Priority 2: Medium — Policy Decision on Remote Ledger Visibility
- **Decision Required from Owner:** Determine whether the remote Office Mac should be permitted to read the Home Mac capital ledger (`GET /api/ledger`).
  - **Option A (Strict Privacy):** Move `/api/ledger` from `ALLOWED_REMOTE_ROUTE_METHODS` to `HOME_OWNER_ONLY_ENDPOINTS`. The Office Mac will receive trading recommendations and technical shortlists only, with zero capital/P&L visibility.
  - **Option B (Authorized Remote View):** Keep `/api/ledger` in `ALLOWED_REMOTE_ROUTE_METHODS`, but require explicit device-level entitlement or mask trade notes/amounts.
- **Recommendation:** Implement **Option A** (least-privilege). The Office Mac is an advisory terminal and does not require historical capital accounting records.

---

## 7. Integrity Verification

- **Git Status (Before & After):**
  - ` M backend/tests/test_gitignore_environment_protection.py` (preserved from Phase 3E.5.2.7.7)
  - All 8 untracked phase reports preserved.
  - Zero files modified; zero code changes made; no commits or pushes made.
- **Dataset SHA-256 Checksums (Bit-for-bit matched):**
  - `3be81f5e3ef7955343cadc43bc6e438285de070fd78d530610ea7b3276917adf  data/checkpoints/checkpoint_nifty_2026-11-02_2026-11-06.json`
  - `9c7008c81e2045e6fc52fc5a197cf9891d30e45d38d9f342b03a013365fa0065  data/checkpoints/raw_nifty_2026-11-02_2026-11-06_chunk_0.json`
  - `bd0ae4959ee8d3ed5c9bea5877343f74883894d71651a74bbfc19fd8d8943d34  data/historical/spot/NIFTY/manifests/manifest_nifty_69ca9b77.json`
  - `2f3010fbca42bebbd0c7ce5c8876aa7a954c1bca48a304fd2758c906ce7ebde0  data/historical/spot/NIFTY/year=2026/month=11/candles_NIFTY_2026_11.csv`
- **`.env` File:** Mode `0600`, regular file (`193` bytes), untracked, untouched.

---

## 8. Final Verdict

**Verdict:** `FINDINGS_REQUIRE_REMEDIATION`

The audit traced both values to concrete sources: `/api/scan` echoes a default parameter rather than calculated market data when disconnected, and `/api/ledger` correctly computes dynamic balances from `ledger_store.json`. However, remediation is required before remote access is provisioned: (1) market sentiment must report `"UNAVAILABLE"` when data feeds are offline, and (2) read access to `/api/ledger` should be evaluated and preferably restricted to Home Mac owner-only sessions under least-privilege principles.

