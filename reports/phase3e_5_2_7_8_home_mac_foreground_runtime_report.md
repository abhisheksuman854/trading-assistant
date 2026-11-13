# Phase 3E.5.2.7.8: Home Mac Foreground Runtime Safety Audit & Controlled Loopback Verification Report

**Date:** 2026-11-12  
**Machine:** Home Mac (`abhisheks-MacBook-Pro.local`)  
**Repository:** `/Users/abhisheksuman/Documents/GitHub/trading-assistant`  
**Branch:** `main`  
**HEAD:** `fd24424d563770684d33a37afc4f0d7b3b6969f8` (`fd24424`)  
**Mode:** Offline-First Source Audit + Controlled Foreground Loopback Verification  
**Verdict:** `HOME_MAC_FOREGROUND_RUNTIME_VERIFIED`

---

## 1. Workspace Identity & Git Baseline

- **Repository Root:** `/Users/abhisheksuman/Documents/GitHub/trading-assistant`
- **Branch:** `main` (`HEAD`: `fd24424d563770684d33a37afc4f0d7b3b6969f8`)
- **Pre-Phase Git Status:**
  - `M backend/tests/test_gitignore_environment_protection.py` (`1 file changed, 60 insertions(+), 10 deletions(-)` from Phase 3E.5.2.7.7)
  - `?? reports/phase3e_5_2_7_6_home_mac_local_verification_report.md`
  - `?? reports/phase3e_5_2_7_7_home_mac_test_portability_report.md`
- All existing modifications and untracked reports were preserved untouched.

---

## 2. Runtime Entry-Point Source Audit

Inspected `backend/app/runtime/cli.py`, `backend/app/runtime/supervisor.py`, `backend/app/runtime/health_and_safety.py`, `backend/app/runtime/frontend_server.py`, `backend/app/main.py`, `backend/app/data/angel_one.py`, and `mise.toml`:

1. **`.env` loading:**
   - `cmd_supervise` (`cli.py:316`) calls `_load_cli_dotenv(strict_perms=prelim_remote)`, which only populates `TRADELENS_*` and `PORT` environment variables (never `ANGEL_*`).
   - `run_server` (`main.py:1213,1221`) calls `read_env_dict(require_strict_perms=False)` (which does not read `.env` when `require_strict_perms=False`) and `load_env(require_strict_perms=remote_mode)`.
2. **Broker login / authentication:** `TradeLensAppRuntime.__init__` (`main.py:157-162`) instantiates `AngelOneSmartAPIProvider(...)`, which only stores fields and sets `self.is_authenticated = False` (`angel_one.py:62`) with zero network or TOTP calls.
3. **Broker REST / WebSocket requests:** None occur during startup, health checks, or frontend serving.
4. **Historical acquisition / sample data generation:** None triggered on startup.
5. **Simulated fallback:** Disabled (`TRADELENS_ALLOW_SIMULATED_FALLBACK=0`; default `"0"` in `main.py:221`).
6. **Order routes / execution:** Hardcoded `order_execution_enabled = False` (`main.py:212`, `supervisor.py:611`, `health_and_safety.py:93,255,519`); no `/api/orders` or `/api/trade` endpoints exist.
7. **Bind addresses:** `cmd_supervise` with `--bind-host 127.0.0.1` binds `backend.app.main` to `127.0.0.1:8000` and `cli.py serve-frontend` to `127.0.0.1:8080`, validated by `validate_bind_host`.
8. **Remote access:** Disabled (`TRADELENS_REMOTE_ACCESS_ENABLED=0`, no VPN CIDRs or peer IPs, `remote_mode=False`).
9. **Background workers / scheduled jobs:** Only the two supervised child processes (`backend` and `frontend`) are started; `--caffeinate` is `False`, no LaunchAgents or cron jobs are started.
10. **Graceful shutdown:** `SIGINT` / `SIGTERM` handlers (`cli.py:353-357`) call `supervisor.shutdown(owner_initiated=False, reason=f"SIGNAL_{signum}")`, terminating `frontend` and `backend` in reverse order, recording clean shutdown, writing `supervisor_status.json`, and releasing `SingleInstanceLock`.

---

## 3. Startup Call Graph & Broker Isolation

`cli.py:cmd_supervise` -> `_load_cli_dotenv` -> `_resolve_remote_and_tls_options` -> `validate_bind_host("127.0.0.1")` -> `TradeLensSupervisor.startup()` -> spawns:
- `backend`: `python3.12 -m backend.app.main` -> `run_server(port=8000, bind_host="127.0.0.1")` -> `build_api_server` -> `TradeLensAppRuntime()` -> `ThreadingTradeLensServer(("127.0.0.1", 8000), ...)`
- `frontend`: `python3.12 -m backend.app.runtime.cli serve-frontend --bind-host 127.0.0.1 --port 8080 --backend-port 8000` -> `run_frontend_server` -> `build_frontend_server`

At no point in this call graph is `provider.authenticate()`, `provider.connect()`, `getCandleData`, or any order placement method invoked.

---

## 4. Bind-Address Verification

Verified via `lsof -nP -iTCP:8000,8080 -sTCP:LISTEN` while running:
- `python3.1` PID `6860`: `TCP 127.0.0.1:8000 (LISTEN)`
- `python3.1` PID `6861`: `TCP 127.0.0.1:8080 (LISTEN)`
- Zero listeners on `0.0.0.0`, `::`, LAN IPs, or Tailscale IPs.

---

## 5. Execution-Gate Decision (Step C)

All 9 Step C safety conditions were proven by source inspection and configuration prior to launch. **Gate Decision:** `PROCEED_TO_CONTROLLED_FOREGROUND_STARTUP`.

---

## 6. Startup Command & Process Information

- **Command:** `PYTHONPATH=.:backend TRADELENS_ALLOW_SIMULATED_FALLBACK=0 TRADELENS_REMOTE_ACCESS_ENABLED=0 TRADELENS_BIND_HOST=127.0.0.1 backend/.venv/bin/python3.12 -m backend.app.runtime.cli supervise --bind-host 127.0.0.1 --backend-port 8000 --frontend-port 8080`
- **Supervisor PID:** `6859` (recovered prior stale PID `12640`)
- **Backend PID:** `6860` (`RUNNING`)
- **Frontend PID:** `6861` (`RUNNING`)
- **Startup Warnings:** Battery power warning (`BATTERY_POWER`, `51%`, `OUTSIDE_TRADING_HOURS`); prior unclean heartbeat from earlier test boot noted in power audit. No exceptions.

---

## 7. API & Frontend Health Results

- `GET http://127.0.0.1:8080/` -> **HTTP 200** (`html_len=202183`, `has_title=True`)
- `GET http://127.0.0.1:8080/frontend-health` -> **HTTP 200** (`{"status": "ok", "service": "TradeLens AI Frontend Dashboard"}`)
- `GET http://127.0.0.1:8000/api/health` -> **HTTP 200**:
  - `engine_status`: `"UNAVAILABLE"`
  - `data_status`: `"UNAVAILABLE"`
  - `remote_available`: `false`
  - `order_execution_enabled`: `false`
  - `signals.calculation_health`: `"HEALTHY"` (`"Deterministic engines & storage verified."`)
  - `signals.network_connectivity`: `"UNKNOWN"` (`"Network probe pending."`)
  - `signals.remote_client_connectivity`: `active_remote_sessions: 0`, `trusted_devices_total: 0`, `transport_tls_enabled: false`
- `cli.py status`: `owner_shutdown_flag: false`, `supervisor.running: true`, `proves_live_market_data: false`.

---

## 8. Market-Data & Authentication State

- `signals.broker_auth_status`: `"EXPLICIT_AUTH_REQUIRED"` (`"Post-reboot explicit Angel One authentication or approved session required."`)
- `signals.feed_connectivity`: `"UNINITIALIZED"`
- `signals.market_data_freshness`: `"UNAVAILABLE"` (`last_market_data_timestamp: null`, `market_data_age_seconds: null`)
- `data_status`: `"UNAVAILABLE"`
- `proves_live_market_data`: `false`

---

## 9. Order-Execution State

- `order_execution_enabled`: `false` in `/api/health`, `signals.remote_client_connectivity`, and `cli.py status`.

---

## 10. Shutdown & Port-Release Verification

- Sent `SIGTERM` (`kill -TERM 6859`) to supervisor PID `6859`.
- Foreground supervisor exited cleanly with **exit code 0**.
- `lsof -nP -iTCP:8000,8080 -sTCP:LISTEN` confirmed **ports 8000 and 8080 released**.
- `cli.py status` post-shutdown confirmed:
  - `owner_shutdown_flag`: `false`
  - `supervisor.running`: `false`, `supervisor_pid`: `null`
  - `backend.status`: `"STOPPED"`, `frontend.status`: `"STOPPED"`
  - No LaunchAgent installed or started; no broker or remote session created.

---

## 11. Post-Shutdown Regression Results

Executed with `backend/.venv/bin/python3.12`:
- `test_gitignore_environment_protection.py`, `test_home_mac_runtime_and_remote_availability.py`, `test_remote_access_security_audit.py`, `test_stage_b_setup_and_mise.py`:
  - **Result:** `Ran 53 tests in 10.240s` — **OK** (0 failures, 0 errors, 0 skips, exit code `0`).

---

## 12. Dataset & Credential-File Integrity

- **`.env`:** `0600 abhisheksuman:staff 193 Regular File .env`, gitignored (`.gitignore:11:*.env`), untracked (`git ls-files .env` empty); never read or modified.
- **Dataset SHA-256 Checksums (unchanged):**
  - `3be81f5e3ef7955343cadc43bc6e438285de070fd78d530610ea7b3276917adf  data/checkpoints/checkpoint_nifty_2026-11-02_2026-11-06.json`
  - `9c7008c81e2045e6fc52fc5a197cf9891d30e45d38d9f342b03a013365fa0065  data/checkpoints/raw_nifty_2026-11-02_2026-11-06_chunk_0.json`
  - `bd0ae4959ee8d3ed5c9bea5877343f74883894d71651a74bbfc19fd8d8943d34  data/historical/spot/NIFTY/manifests/manifest_nifty_69ca9b77.json`
  - `2f3010fbca42bebbd0c7ce5c8876aa7a954c1bca48a304fd2758c906ce7ebde0  data/historical/spot/NIFTY/year=2026/month=11/candles_NIFTY_2026_11.csv`
- **Git Status:** Only `M backend/tests/test_gitignore_environment_protection.py` and the untracked phase reports (`phase3e_5_2_7_6`, `phase3e_5_2_7_7`, `phase3e_5_2_7_8`). No commits or pushes were made.

---

## 13. Blockers or Warnings

- **No blockers.**
- **Operational note:** Home Mac was on battery power (`51%`) outside trading hours; for extended market-hours operation, connect AC power and keep the lid open or in clamshell mode with external display/HID.

