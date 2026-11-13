# Phase 3E.5.2.7.6: Home Mac Repository Synchronization & Local Environment Verification Report

**Date:** 2026-11-12  
**Machine:** Home Mac (`abhisheks-MacBook-Pro.local`) — Primary TradeLens AI Host  
**Repository:** `/Users/abhisheksuman/Documents/GitHub/trading-assistant`  
**Mode:** Controlled Git Synchronization + Offline Local Verification Only  
**Verdict:** `HOME_MAC_SYNC_AND_OFFLINE_VERIFICATION_PASSED`

---

## 1. Executive Summary

The Home Mac repository at `/Users/abhisheksuman/Documents/GitHub/trading-assistant` is cleanly synchronized with `origin/main` at commit `fd24424d563770684d33a37afc4f0d7b3b6969f8` (which includes `9d1329e` and `802b559`), with a clean working tree and zero history divergence.

All Home Mac historical NIFTY data and checkpoints remain intact and bit-for-bit unchanged. `.env` credential hygiene is compliant (`0600`, regular file, gitignored, untracked), `backend/.venv/bin/python3.12` (`Python 3.12.13`) initializes `TradeLensAppRuntime` offline with `is_authenticated=False` and `order_execution_enabled=False`, and LaunchAgent plist previews render cleanly to a temporary directory and pass `plutil -lint`.

In offline testing, `backend/test_api.py` (3/3), `py_compile` across 7 core runtime modules, and 98 of 99 targeted unit tests passed. The single unit-test failure (`test_06_no_real_environment_file_exists_or_created_in_workspace` in `backend/tests/test_gitignore_environment_protection.py`) is expected on the Home Mac primary host because it intentionally retains a local `.env` file, whereas that check enforces a `.env`-free workspace for the Office Mac client.

---

## 2. Step A — Home Mac Identity

- **Hostname:** `abhisheks-MacBook-Pro.local`
- **Working Directory:** `/Users/abhisheksuman/Documents/GitHub/trading-assistant`
- **Git Root:** `/Users/abhisheksuman/Documents/GitHub/trading-assistant`
- **Branch:** `main`
- **HEAD:** `fd24424d563770684d33a37afc4f0d7b3b6969f8` (`fd24424 Rport added`)
- **Origin Remote:** `https://github.com/abhisheksuman854/trading-assistant.git` (fetch/push)
- **Working Tree:** Clean (`git status --porcelain` empty; `## main...origin/main`)
- **Python Paths:**
  - `/opt/homebrew/bin/python3.12`: Not present (`No such file or directory`; Homebrew has `python3.13` -> `3.13.14` and `python3.14` -> `3.14.8`)
  - `backend/.venv/bin/python3.12`: Present (`Python 3.12.13`, symlink to `python`)

---

## 3. Step B — Git Synchronization

- `git fetch origin main`: Succeeded (`From https://github.com/abhisheksuman854/trading-assistant`, `* branch main -> FETCH_HEAD`).
- `git log --oneline HEAD..FETCH_HEAD` and `git log --oneline FETCH_HEAD..HEAD`: Both empty (zero divergence).
- `git log -10 --oneline` confirmed required commits are present in `HEAD`:
  - `fd24424 (HEAD -> main, origin/main, origin/HEAD) Rport added`
  - `802b559 chore(security): harden environment file protection`
  - `9d1329e new changes update on office mac`
- `git pull --ff-only origin main`: Succeeded (`Already up to date.`).

---

## 4. Step C — Home Mac Data Preservation

All 4 baseline files under `data/` are present, tracked in Git, and match their prior SHA-256 checksums and byte sizes:

| File | Size (bytes) | SHA-256 |
|---|---:|---|
| `data/checkpoints/checkpoint_nifty_2026-11-02_2026-11-06.json` | 569 | `3be81f5e3ef7955343cadc43bc6e438285de070fd78d530610ea7b3276917adf` |
| `data/checkpoints/raw_nifty_2026-11-02_2026-11-06_chunk_0.json` | 129,933 | `9c7008c81e2045e6fc52fc5a197cf9891d30e45d38d9f342b03a013365fa0065` |
| `data/historical/spot/NIFTY/manifests/manifest_nifty_69ca9b77.json` | 4,091 | `bd0ae4959ee8d3ed5c9bea5877343f74883894d71651a74bbfc19fd8d8943d34` |
| `data/historical/spot/NIFTY/year=2026/month=11/candles_NIFTY_2026_11.csv` | 341,381 | `2f3010fbca42bebbd0c7ce5c8876aa7a954c1bca48a304fd2758c906ce7ebde0` |

---

## 5. Step D — `.env` Credential Hygiene

- `.env` exists as a regular file (`193` bytes), owned by `abhisheksuman:staff`, with strict permissions `0600` (`-rw-------`).
- `git check-ignore -v .env`: `.gitignore:11:*.env	.env`.
- `git ls-files '*.env*'`: Only `.env.example` is tracked (`.env` is NOT tracked).
- `.env.example` (`0644`, `1586` bytes) contains placeholder values only (`your_api_key_here`, `your_client_code_here`, `your_password_or_mpin_here`, `your_totp_secret_here`).
- No `.env` secret values were read, printed, or logged.

---

## 6. Step E — Python Environment & Offline Runtime Check

- `backend/.venv/bin/python3.12 --version` -> `Python 3.12.13`.
- `/opt/homebrew/bin/python3.12` is absent; `backend/.venv/bin/python3.12` (backed by `uv` CPython 3.12.13) is the active project interpreter.
- Offline import and instantiation of `backend.app.main.TradeLensAppRuntime` with `TRADELENS_ALLOW_SIMULATED_FALLBACK=0` and empty `ANGEL_*` environment variables succeeded without auto-authentication:
  - `is_authenticated= False`
  - `order_execution_enabled= False`
  - Attempting `rt.provider.authenticate()` failed closed (`AttributeError`).

---

## 7. Step F — `mise.toml` Review (No `mise trust` Executed)

- Inspected all 123 lines of `mise.toml`.
- `[env]` sets `PYTHONPATH = "{{config_root}}:{{config_root}}/backend"` and prepends `{{config_root}}/backend/.venv/bin` ahead of `/opt/homebrew/bin` and `/usr/local/bin`, so `python3.12` resolves to `backend/.venv/bin/python3.12`.
- All tasks are explicit local runtime (`tradelens-*`) and Stage B (`tradelens-stage-b-*`) commands; no automatic shell hooks or hidden execution triggers are present.
- `mise trust` was **not** executed in this phase.

---

## 8. Step G — Offline Test & Compilation Verification

1. **Core Smoke Test (`backend/test_api.py`):** **PASSED** (3/3 checks: Black-Scholes Greeks, Universe shortlisting scanner, Veto Gate).
2. **Bytecode Compilation (`py_compile`):** **PASSED** (exit code 0 on `backend/app/main.py`, `backend/app/runtime/cli.py`, `backend/app/runtime/stage_b_setup.py`, `backend/app/runtime/launchd_config.py`, `backend/app/runtime/supervisor.py`, `backend/app/runtime/security.py`, `backend/app/runtime/health_and_safety.py`).
3. **Targeted Offline Unit Tests (7 suites, 99 tests total):** **98 PASSED, 1 FAILED** (`Ran 99 tests in 16.026s`):
   - `test_stage_b_setup_and_mise.py`: Passed
   - `test_provider_probe_audit.py`: Passed
   - `test_home_mac_runtime_and_remote_availability.py`: Passed
   - `test_remote_access_security_audit.py`: Passed
   - `test_pre_provisioning_security_gate_stage_a1.py`: Passed
   - `test_private_network_provisioning_stage_a.py`: Passed
   - `test_gitignore_environment_protection.py`: 5/6 passed; **1 failed**:
     - `test_06_no_real_environment_file_exists_or_created_in_workspace`: Expected `[]`, found `['.env']` because the Home Mac primary host legitimately holds a local `.env` file (`0600`, gitignored).

---

## 9. Step H — LaunchAgent Portability Preview (Temp Directory Only)

- Rendered `com.tradelens.home-engine.plist` and `com.tradelens.frontend-dashboard.plist` into a temporary directory via `backend.app.runtime.launchd_config.render_launchd_plists`.
- Both plists passed `plutil -lint` (`OK`, exit code 0).
- Both plists reference:
  - Working directory / repo root: `/Users/abhisheksuman/Documents/GitHub/trading-assistant`
  - Python interpreter: `/Users/abhisheksuman/Documents/GitHub/trading-assistant/backend/.venv/bin/python3.12`
  - Loopback bind host: `127.0.0.1` (ports `8000` / `8080`)
- Zero Office Mac paths and zero secrets appear in the rendered plists. No production plists were written or installed.

---

## 10. Step I — Local Runtime Readiness Assessment

- `python3.12 -m backend.app.runtime.cli status`:
  - `state_dir`: `/Users/abhisheksuman/Documents/GitHub/trading-assistant/runtime_state`
  - `owner_shutdown_flag`: `false`
  - `power_audit`: `Mac15,6`, `macOS 26.0`, `BATTERY_POWER` (`53%`), `policy_status: OUTSIDE_TRADING_HOURS`, `caffeinate_active: false`, `global_pmset_modified: false`
  - `supervisor`: `order_execution_enabled: false`, `proves_live_market_data: false` (prior boot state from `2026-11-11T17:11:35.059530+00:00`)
- `python3.12 -m backend.app.runtime.cli stage-b --action status --no-save`:
  - `stage_verdict`: `STAGE_B_PARTIALLY_COMPLETE` (`verified_step_count: 1` of `10`, `state_freshness: STALE_PRIOR_BOOT`)
  - `step_01_home_mac_preflight`: `VERIFIED`
  - `step_02_tailscale_home_office`: `REQUIRES_OWNER_ACTION` (`Tailscale is not installed on Home Mac.`)
  - Steps 3–10: `BLOCKED` by prerequisite lock.

---

## 11. Safety Boundaries Confirmation

- No Angel One authentication, login, TOTP generation, or broker API requests were executed.
- No live market quotes or historical candles were fetched; `data/` was not modified.
- Order execution remains permanently disabled (`order_execution_enabled=False`).
- Tailscale, WireGuard, TLS certificates, Keychain entries, `mise trust`, and LaunchAgents were not installed or modified.
- No supervisor process was started and no TCP ports were bound.

---

## 12. Recommended Next Steps

1. **(Optional) Scope `test_06_no_real_environment_file_exists_or_created_in_workspace`:** Decide whether `test_gitignore_environment_protection.py` should treat a `0600` gitignored `.env` as expected on the Home Mac primary host (e.g., gated by an Office-client check) so the full regression suite passes cleanly on both machines.
2. **Proceed to Controlled Local Runtime Verification:** When ready, run `tradelens-start` (or `tradelens-start-sim` for offline sample UI preview) on `127.0.0.1:8000` / `127.0.0.1:8080` to verify live loopback UI rendering.
3. **Stage B Remote Provisioning (When Ready):** Install/authenticate Tailscale on Home Mac (`step_02_tailscale_home_office`) and follow Steps 2–10 before enabling remote Office Mac access.

