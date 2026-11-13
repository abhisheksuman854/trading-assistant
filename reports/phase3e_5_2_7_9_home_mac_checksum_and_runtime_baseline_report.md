# Phase 3E.5.2.7.9: Home Mac Checksum Reconciliation & Runtime Baseline Closure Report

**Date:** 2026-11-12  
**Machine:** Home Mac (`abhisheks-MacBook-Pro.local`)  
**Repository:** `/Users/abhisheksuman/Documents/GitHub/trading-assistant`  
**Mode:** Offline Verification Only  
**Verdict:** `HOME_MAC_BASELINE_VERIFIED`

---

## 1. Step A — Dataset SHA-256 Checksum Reconciliation

### Command Executed
```bash
shasum -a 256 \
  data/checkpoints/checkpoint_nifty_2026-11-02_2026-11-06.json \
  data/checkpoints/raw_nifty_2026-11-02_2026-11-06_chunk_0.json \
  data/historical/spot/NIFTY/manifests/manifest_nifty_69ca9b77.json \
  data/historical/spot/NIFTY/year=2026/month=11/candles_NIFTY_2026_11.csv

stat -f '%z %N' \
  data/checkpoints/checkpoint_nifty_2026-11-02_2026-11-06.json \
  data/checkpoints/raw_nifty_2026-11-02_2026-11-06_chunk_0.json \
  data/historical/spot/NIFTY/manifests/manifest_nifty_69ca9b77.json \
  data/historical/spot/NIFTY/year=2026/month=11/candles_NIFTY_2026_11.csv
```

### Reconciliation Table

| File Path | Exists / Size (bytes) | Actual SHA-256 | Phase 3E.5.2.7.6 Reported | Phase 3E.5.2.7.8 Reported | Result |
|---|---:|---|---|---|---|
| `data/checkpoints/checkpoint_nifty_2026-11-02_2026-11-06.json` | Yes / `569` | `3be81f5e3ef7955343cadc43bc6e438285de070fd78d530610ea7b3276917adf` | `3be81f5e3ef7955343cadc43bc6e438285de070fd78d530610ea7b3276917adf` | `3be81f5e3ef7955343cadc43bc6e438285de070fd78d530610ea7b3276917adf` | **MATCH** |
| `data/checkpoints/raw_nifty_2026-11-02_2026-11-06_chunk_0.json` | Yes / `129,933` | `9c7008c81e2045e6fc52fc5a197cf9891d30e45d38d9f342b03a013365fa0065` | `9c7008c81e2045e6fc52fc5a197cf9891d30e45d38d9f342b03a013365fa0065` | `9c7008c81e2045e6fc52fc5a197cf9891d30e45d38d9f342b03a013365fa0065` | **MATCH** |
| `data/historical/spot/NIFTY/manifests/manifest_nifty_69ca9b77.json` | Yes / `4,091` | `bd0ae4959ee8d3ed5c9bea5877343f74883894d71651a74bbfc19fd8d8943d34` | `bd0ae4959ee8d3ed5c9bea5877343f74883894d71651a74bbfc19fd8d8943d34` | `bd0ae4959ee8d3ed5c9bea5877343f74883894d71651a74bbfc19fd8d8943d34` | **MATCH** |
| `data/historical/spot/NIFTY/year=2026/month=11/candles_NIFTY_2026_11.csv` | Yes / `341,381` | `2f3010fbca42bebbd0c7ce5c8876aa7a954c1bca48a304fd2758c906ce7ebde0` | `2f3010fbca42bebbd0c7ce5c8876aa7a954c1bca48a304fd2758c906ce7ebde0` | `2f3010fbca42bebbd0c7ce5c8876aa7a954c1bca48a304fd2758c906ce7ebde0` | **MATCH** |

**Discrepancies Found:** None. All 4 files match bit-for-bit across disk, Phase 3E.5.2.7.6, and Phase 3E.5.2.7.8.

---

## 2. Step B — Git Baseline

- **Branch:** `main`
- **HEAD:** `fd24424d563770684d33a37afc4f0d7b3b6969f8`
- **`git status --short`:**
  - ` M backend/tests/test_gitignore_environment_protection.py`
  - `?? reports/phase3e_5_2_7_6_home_mac_local_verification_report.md`
  - `?? reports/phase3e_5_2_7_7_home_mac_test_portability_report.md`
  - `?? reports/phase3e_5_2_7_8_home_mac_foreground_runtime_report.md`
- **`git diff --stat`:**
  - `backend/tests/test_gitignore_environment_protection.py | 70 ++++++++++++++++++----` (`1 file changed, 60 insertions(+), 10 deletions(-)`)
- All preserved changes and untracked phase reports remain intact.

---

## 3. Step C — Runtime Stopped & Host Hygiene Verification

- `lsof -nP -iTCP:8000,8080 -sTCP:LISTEN`: Exit code 1 / no listeners on ports `8000` or `8080`.
- `ls -la ~/Library/LaunchAgents/com.tradelens.*`: No matches (`No TradeLens LaunchAgents installed`).
- `ls -la runtime_state/remote_auth.json`: Not present (`No remote_auth.json state file`; no remote sessions).
- `PYTHONPATH=.:backend backend/.venv/bin/python3.12 -m backend.app.runtime.cli status`:
  - `supervisor.running`: `false` (`supervisor_pid: null`)
  - `supervisor.services.backend.status`: `"STOPPED"` (`pid: null`)
  - `supervisor.services.frontend.status`: `"STOPPED"` (`pid: null`)
  - `supervisor.order_execution_enabled`: `false`
  - `supervisor.proves_live_market_data`: `false`
  - `owner_shutdown_flag`: `false`

---

## 4. Step D — Credential-File Metadata

- `stat -f '%Mp%Lp %Su:%Sg %z %HT %N' .env`: `0600 abhisheksuman:staff 193 Regular File .env`
- `git check-ignore -v .env`: `.gitignore:11:*.env	.env`
- `git ls-files .env`: Empty (untracked).
- `.env` contents were never read, printed, or modified.

---

## 5. Limitations & Safety Boundaries

- This verification establishes a reproducible offline/local baseline only; it does **not** establish or claim remote access, live market data, broker authentication, or trading readiness.
- All strict safety boundaries were upheld: no `.env` reading/modification, no Angel One auth/network calls, no market-data changes, no supervisor/server startup, no Tailscale/TLS/Keychain/LaunchAgent changes, and no Git commits/pushes/resets.

