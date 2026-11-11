# TradeLens AI — Phase 3E.5.2.7.1 Home Mac Toolchain Correction & Local Runtime Verification Report

**Date:** 2026-11-11  
**Host:** Home Mac (`macOS 26.0` Build `25A354`, `arm64`, hardware model `Mac15,6`)  
**Status Summary:**  
- **Local Toolchain, Path Corrections, Regression Suite & Loopback Runtime Verification:** `VERIFIED_LOCAL`  
- **Remote Provisioning (Stage 2+ Tailscale, TLS, Office Mac, LaunchAgents):** `PENDING_OWNER_APPROVAL` / `NOT_STARTED` (strict stop respected)

---

## Stage A — Repository Identity, `mise` Trust & Python Toolchain

| Item | Observed Value |
| --- | --- |
| Active Repository Root | `/Users/abhisheksuman/Documents/GitHub/trading-assistant` |
| Git Branch / HEAD Commit | `main` @ `713c4ce5080f64c0cde1db30438deb43240486bb` |
| Git Remote (`origin`) | `https://github.com/abhisheksuman854/trading-assistant.git` |
| `mise` Version | `2026.4.25 macos-arm64 (2026-04-28)` |
| `mise` Trust Status | Trusted **only** `/Users/abhisheksuman/Documents/GitHub/trading-assistant` after complete `mise.toml` inspection and owner confirmation |
| `mise.toml` Safety Audit | `[env]` sets `PYTHONPATH` and `_.path` (`backend/.venv/bin`, `/opt/homebrew/bin`); no `[tools]`, `[hooks]`, or activation commands; all 16 tasks run `python3.12 -m backend.app.runtime.cli stage-b --action ...` |
| Project Python Runtime | `/Users/abhisheksuman/Documents/GitHub/trading-assistant/backend/.venv/bin/python3.12` (`Python 3.12.13` via `uv` standalone CPython `cpython-3.12.13-macos-aarch64-none`) |
| Existing System Pythons Preserved | `/opt/homebrew/bin/python3` (`Python 3.14.8`), `/opt/homebrew/bin/python3.13` (`Python 3.13.14`), `/opt/homebrew/bin/python3.14` (`Python 3.14.8`) — none replaced or modified |
| Dependencies | Runtime, CLI, HTTP servers, and all 489 backend unit tests execute cleanly on Python 3.12 stdlib (`yfinance` in `yfinance_provider.py` is lazily/optionally imported and unused offline) |
| `.env` Security Check | Regular non-symlink gitignored file, mode verified `600` (secrets never printed or modified) |
| `mise run tradelens-stage-b-home-preflight` | **PASSED** (`exit 0`, `step_01_home_mac_preflight = VERIFIED`, `verified_step_count = 1/10`, `stage_verdict = STAGE_B_PARTIALLY_COMPLETE`) |

*Note on Homebrew `python@3.12`:* An initial check showed `brew install python@3.12` proposed downgrading `openssl@3` (`3.6.5 -> 3.5.9`), so installation was cancelled before confirmation to preserve existing system libraries; `uv` standalone Python `3.12.13` + `backend/.venv` aligned Python `3.12` cleanly without touching Homebrew packages.

---

## Stage B — Repository-Specific Path Corrections

- **Updated LaunchAgent Template Generation (non-installing):**
  - Added [`_default_python_executable`](file:///Users/abhisheksuman/Documents/GitHub/trading-assistant/backend/app/runtime/stage_b_setup.py) and updated [`_validate_python_executable`](file:///Users/abhisheksuman/Documents/GitHub/trading-assistant/backend/app/runtime/stage_b_setup.py) and [`cli.py`](file:///Users/abhisheksuman/Documents/GitHub/trading-assistant/backend/app/runtime/cli.py) so template rendering prefers the active repository `backend/.venv/bin/python3.12` when present instead of assuming `/opt/homebrew/bin/python3.12`.
  - Executed `mise run tradelens-stage-b-render-launchd` (`installed_to_launchagents = false`, `templates_only = true`) followed by `mise run tradelens-stage-b-launchd-lint` (`exit 0`, `plutil -lint: OK`).
  - Inspected generated [`com.tradelens.home-engine.plist`](file:///Users/abhisheksuman/Documents/GitHub/trading-assistant/deploy/launchd/com.tradelens.home-engine.plist) and [`com.tradelens.frontend-dashboard.plist`](file:///Users/abhisheksuman/Documents/GitHub/trading-assistant/deploy/launchd/com.tradelens.frontend-dashboard.plist): stale `/Users/abhisheksuman/Documents/GitHub/tradelens-ai` and `/opt/homebrew/bin/python3.12` entries are now replaced with:
    - Wrapper: `/Users/abhisheksuman/Documents/GitHub/trading-assistant/deploy/scripts/run_launchd_service.sh`
    - Python: `/Users/abhisheksuman/Documents/GitHub/trading-assistant/backend/.venv/bin/python3.12`
    - WorkingDirectory / PYTHONPATH: `/Users/abhisheksuman/Documents/GitHub/trading-assistant`
    - State Dir: `/Users/abhisheksuman/Documents/GitHub/trading-assistant/runtime_state`
    - Logs: `/Users/abhisheksuman/Documents/GitHub/trading-assistant/runtime_logs/launchd.*.log`
  - **LaunchAgent installation check:** `~/Library/LaunchAgents` untouched (`any_installed = false`).
- **Updated Documentation Paths:**
  - Updated [`README.md`](file:///Users/abhisheksuman/Documents/GitHub/trading-assistant/README.md) Quick Start commands (`backend/.venv/bin/python3.12`) and project structure root label (`trading-assistant/`).
- **Verified Other Paths:**
  - Dashboard static asset: [`frontend_server.py`](file:///Users/abhisheksuman/Documents/GitHub/trading-assistant/backend/app/runtime/frontend_server.py) resolves dynamically to `/Users/abhisheksuman/Documents/GitHub/trading-assistant/frontend/index.html`.
  - Setup progress store: `.ops_state/stage_b_progress.json` (`0600`, secret-free).
  - Operations documentation ([`STAGE_B_SETUP.md`](file:///Users/abhisheksuman/Documents/GitHub/trading-assistant/docs/operations/STAGE_B_SETUP.md)) uses `mise` tasks and relative `runtime_state/` / `runtime_logs/` paths.

---

## Stage C — Test Fixture Isolation & Regression Results

- **Fixture Fixes Applied:**
  1. [`test_stage_b_setup_and_mise.py`](file:///Users/abhisheksuman/Documents/GitHub/trading-assistant/backend/tests/test_stage_b_setup_and_mise.py) (`test_06_mise_toml_and_docs_completeness`): Isolated `backend.app.runtime.cli.REPO_ROOT` onto `self.root / "isolated_repo"` and scrubbed `ANGEL_*` / `TRADELENS_*` from `os.environ` during the test. Verified both:
     - Insecure temporary `.env` (`0o644`) exits `2` (`step_01_home_mac_preflight = REQUIRES_OWNER_ACTION`).
     - Hardened temporary `.env` (`0o600`) exits `0` (`step_01_home_mac_preflight = VERIFIED`).
     - Real repository `.env` remained untouched at mode `600`.
  2. [`test_pre_provisioning_security_gate_stage_a1.py`](file:///Users/abhisheksuman/Documents/GitHub/trading-assistant/backend/tests/test_pre_provisioning_security_gate_stage_a1.py) (`test_04` and `test_13`): Replaced hardcoded `"/opt/homebrew/bin/python3.12"` subprocess executable with `sys.executable` so wrapper parity tests run cleanly against the active Python 3.12 virtualenv.
- **Test Execution Results (all run under `backend/.venv/bin/python3.12`):**
  - Targeted runtime & security suite (`test_stage_b_setup_and_mise.py`, `test_pre_provisioning_security_gate_stage_a1.py`, `test_home_mac_runtime_and_remote_availability.py`, `test_remote_access_security_audit.py`, `test_private_network_provisioning_stage_a.py`): **70 tests passed in 15.846s (`OK`)**.
  - Core API smoke script (`PYTHONPATH=.:./backend backend/.venv/bin/python3.12 backend/test_api.py`): **ALL TESTS PASSED**.
  - Full backend regression (`PYTHONPATH=.:./backend backend/.venv/bin/python3.12 -m unittest discover -s backend/tests -p "test_*.py"`): **489 tests passed in 75.299s (`OK`)**, zero failures, zero skips.

---

## Stage D — Local Loopback Runtime Verification (`VERIFIED_LOCAL`)

Started temporary foreground supervisor cleanly (`backend/.venv/bin/python3.12 -m backend.app.runtime.cli supervise --bind-host 127.0.0.1 --backend-port 8000 --frontend-port 8080`) with owner confirmation outside sandbox restrictions:

- **Listener Addresses (While Running):**
  - `TCP 127.0.0.1:8000 (LISTEN)` (backend PID `10604`)
  - `TCP 127.0.0.1:8080 (LISTEN)` (frontend PID `10605`)
  - No `0.0.0.0`, wildcard, Tailscale, or public listeners bound.
- **CLI Health & Status Checks:**
  - `health-check --bind-host 127.0.0.1 --backend-port 8000 --frontend-port 8080`: `overall_healthy = true`, backend `HTTP_200`, frontend `HTTP_200`, `proves_live_market_data = false`.
  - `cli.py status`: `running = true`, `supervisor_pid = 10603`, `order_execution_enabled = false`.
- **HTTP Endpoint & Security Header Verification:**
  - `GET http://127.0.0.1:8000/api/health` -> `200`, `order_execution_enabled = false`, `data_status = "UNAVAILABLE"`, `proves_live_market_data = false`, `failure_reasons` includes `"BROKER_AUTH_EXPLICIT_AUTH_REQUIRED: Post-reboot explicit Angel One authentication or approved session required."`
  - `GET http://127.0.0.1:8000/api/runtime/status` -> `200`, `order_execution_enabled = False`, `data_status = "UNAVAILABLE"`.
  - `GET http://127.0.0.1:8000/api/setup/state` -> `200`, `stage_verdict = "STAGE_B_PARTIALLY_COMPLETE"`, `verified_step_count = 1`, `step_1 = "VERIFIED"`, `step_2 = "REQUIRES_OWNER_ACTION"`.
  - `GET http://127.0.0.1:8080/` -> `200` (`202166` bytes), contains `id="tab-setup"` and `id="panel-setup"`, with headers:
    - `Content-Security-Policy: default-src 'self'; script-src 'nonce-...'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self' data:; connect-src 'self' http://127.0.0.1:8000 https://127.0.0.1:8000; object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'`
    - `X-Frame-Options: DENY`
    - `X-Content-Type-Options: nosniff`
    - `Referrer-Policy: no-referrer`
    - `Cache-Control: no-store`
  - `POST http://127.0.0.1:8000/api/order` -> `HTTP 404` (no order-placement endpoint exposed).
- **PID Lock & Duplicate-Start Rejection:**
  - Concurrent `supervise` invocation exited `1`: `"Another active TradeLens supervisor instance already holds /Users/abhisheksuman/Documents/GitHub/trading-assistant/runtime_state/supervisor.lock (pid=10603)."`
- **Clean Shutdown & Port Release:**
  - `cli.py stop` -> `{"stopped": true, "status": "STOPPED_GRACEFULLY", "pid": 10603}`; supervisor exited `0`.
  - Post-stop `lsof -nP -iTCP:8000 -iTCP:8080 -sTCP:LISTEN`: empty (all listeners released).
  - Post-stop `cli.py status`: `running = false`, `backend = STOPPED`, `frontend = STOPPED`, `supervisor_pid = null`.

---

## Safety & Dataset Integrity Verification

- **Broker Authentication:** Never executed (`is_authenticated = False`; runtime reported `BROKER_AUTH_EXPLICIT_AUTH_REQUIRED`).
- **Order Execution:** Permanently `False` (`order_execution_enabled = False`).
- **Phase 3F / Historical Data Acquisition:** Not started; zero files created or altered under `data/`.
- **Dataset SHA-256 Checksum Comparison (Identical Before & After):**
  - `3be81f5e3ef7955343cadc43bc6e438285de070fd78d530610ea7b3276917adf` `data/checkpoints/checkpoint_nifty_2026-11-02_2026-11-06.json`
  - `9c7008c81e2045e6fc52fc5a197cf9891d30e45d38d9f342b03a013365fa0065` `data/checkpoints/raw_nifty_2026-11-02_2026-11-06_chunk_0.json`
  - `bd0ae4959ee8d3ed5c9bea5877343f74883894d71651a74bbfc19fd8d8943d34` `data/historical/spot/NIFTY/manifests/manifest_nifty_69ca9b77.json`
  - `2f3010fbca42bebbd0c7ce5c8876aa7a954c1bca48a304fd2758c906ce7ebde0` `data/historical/spot/NIFTY/year=2026/month=11/candles_NIFTY_2026_11.csv`

---

## Remaining Stage 2+ Prerequisites & Stop Conditions Observed

- **Remaining Stage 2 Prerequisite:** Tailscale CLI/app is not installed on this Home Mac (`step_02_tailscale_home_office = REQUIRES_OWNER_ACTION`).
- **Not Executed (By Policy / Strict Stop):**
  - Tailscale not installed or configured (`Stage 2`).
  - TLS certificates not generated (`Stage 3`).
  - Owner/HMAC secrets (`TRADELENS_OWNER_SECRET`, `TRADELENS_STATE_HMAC_SECRET`) not created (`Stage 3/5`).
  - macOS Keychain not modified and Office Mac not registered (`Stage 5`).
  - LaunchAgents not installed or bootstrapped in `~/Library/LaunchAgents` (`Stage 7`).
  - Angel One authentication, historical acquisition, and Phase 3F not started.

