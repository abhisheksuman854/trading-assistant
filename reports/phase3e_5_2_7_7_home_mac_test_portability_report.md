# Phase 3E.5.2.7.7: Home Mac Test Portability Correction & Offline Regression Verification Report

**Date:** 2026-11-12  
**Machine:** Home Mac (`abhisheks-MacBook-Pro.local`)  
**Repository:** `/Users/abhisheksuman/Documents/GitHub/trading-assistant`  
**Branch:** `main`  
**Baseline Commit:** `fd24424d563770684d33a37afc4f0d7b3b6969f8` (`fd24424`)  
**Mode:** Offline Test Correction and Regression Verification Only  
**Verdict:** `HOME_MAC_TEST_PORTABILITY_CORRECTED`

---

## 1. Repository Identity & Baseline

- **Repository Root:** `/Users/abhisheksuman/Documents/GitHub/trading-assistant`
- **Branch:** `main`
- **HEAD:** `fd24424d563770684d33a37afc4f0d7b3b6969f8`
- **Interpreter:** `backend/.venv/bin/python3.12` (`Python 3.12.13`)
- **Pre-Edit Working Tree:** No tracked files modified (`?? reports/phase3e_5_2_7_6_home_mac_local_verification_report.md` only from Phase 3E.5.2.7.6).

---

## 2. Root Cause of Original Test Failure

In `backend/tests/test_gitignore_environment_protection.py`, `test_06_no_real_environment_file_exists_or_created_in_workspace` scanned the active `REPO_ROOT` (`REPO_ROOT.rglob(".env*")` and `REPO_ROOT.rglob("*.env")`) and asserted `found_real_env == []`.

On the Office Mac client, no `.env` exists in the repository, so that assertion passed. On the Home Mac primary host, however, a real `.env` file (`0600`, gitignored, untracked) intentionally exists at `REPO_ROOT / ".env"`, causing `test_06` to fail with `AssertionError: Lists differ: ['.env', '.env'] != []`.

---

## 3. Exact Test Changes

Scoped strictly to `backend/tests/test_gitignore_environment_protection.py` (`1 file changed, 60 insertions(+), 10 deletions(-)`):

- Replaced `test_06_no_real_environment_file_exists_or_created_in_workspace` with `test_06_env_protection_on_host_and_isolated_workspace`, preserving all 5 other tests (`test_01` through `test_05`) unchanged.
- **Host workspace verification (`REPO_ROOT`):**
  - Permits an existing root `.env` on the Home Mac primary host without ever reading, printing, modifying, or deleting it.
  - If `REPO_ROOT / ".env"` exists, verifies it is a regular file (non-symlink), is ignored (`git check-ignore -q .env` == `0`), is not tracked (`git ls-files --error-unmatch .env` != `0`), and does not appear in `git status --porcelain --untracked-files=all .env`.
  - Continues to fail if any unexpected non-template env files (e.g., `.env.local`, `.env.dev`, `.env.production`, `.envrc`, or nested `*.env` files) exist anywhere in `REPO_ROOT`.
- **Isolated temporary-workspace verification (`tempfile.TemporaryDirectory()`):**
  - Initializes an isolated Git repository with `.gitignore` and verifies no `.env` is created automatically by setup.
  - Creates synthetic `.env` and `.env.example`, verifies `.env` is ignored (`git check-ignore -q .env` == `0`) and refused by `git add --dry-run .env`.
  - Verifies `.env.example` is not ignored (`git check-ignore -q .env.example` == `1`), stages and commits `.gitignore` + `.env.example`, confirms `.env.example` is tracked and `.env` is absent from `git ls-files --cached`, and verifies `git status --porcelain --untracked-files=all` remains clean.
  - Cleans up only the temporary directory via `tempfile.TemporaryDirectory()`.

---

## 4. Why the Revised Test Remains Secure

- **No blanket skip:** Test 06 runs unconditionally on both Home Mac and Office Mac.
- **No `.gitignore` weakening:** `.gitignore` is untouched; `.env` remains ignored and untracked.
- **Zero secret exposure:** `.env` contents are never opened, read, or logged.
- **Stricter host check + isolated check:** Unexpected env files (`.env.local`, nested `.env`, etc.) are still rejected on the host, while the isolated temporary Git repo proves `.env` cannot be staged/committed and `.env.example` remains trackable.

---

## 5. Offline Test, Smoke & Compilation Results

All commands were executed with `backend/.venv/bin/python3.12`:

1. **`test_gitignore_environment_protection.py` (6 tests):**
   - `PYTHONPATH=.:backend backend/.venv/bin/python3.12 -m unittest -v backend/tests/test_gitignore_environment_protection.py`
   - **Result:** `Ran 6 tests in 0.750s` — **OK** (0 failures, 0 errors, 0 skips, exit code `0`).
2. **Targeted Stage B, Probe, Runtime & Remote Security Suites (7 suites, 99 tests):**
   - `test_gitignore_environment_protection.py`, `test_stage_b_setup_and_mise.py`, `test_provider_probe_audit.py`, `test_home_mac_runtime_and_remote_availability.py`, `test_remote_access_security_audit.py`, `test_pre_provisioning_security_gate_stage_a1.py`, `test_private_network_provisioning_stage_a.py`
   - **Result:** `Ran 99 tests in 16.292s` — **OK** (0 failures, 0 errors, 0 skips, exit code `0`).
3. **Full Backend Test Suite:**
   - `PYTHONPATH=.:backend backend/.venv/bin/python3.12 -m unittest discover -s backend/tests -p 'test_*.py' -v`
   - **Result:** `Ran 496 tests in 75.156s` — **OK** (0 failures, 0 errors, 0 skips, exit code `0`).
4. **Core Smoke Test:**
   - `PYTHONPATH=.:backend backend/.venv/bin/python3.12 backend/test_api.py`
   - **Result:** `ALL TESTS PASSED!` (3/3 checks, exit code `0`).
5. **Python Bytecode Compilation (`py_compile`):**
   - `backend/tests/test_gitignore_environment_protection.py`, `backend/app/main.py`, `backend/app/runtime/cli.py`, `backend/app/runtime/stage_b_setup.py`, `backend/app/runtime/launchd_config.py`, `backend/app/runtime/supervisor.py`, `backend/app/runtime/security.py`, `backend/app/runtime/health_and_safety.py`
   - **Result:** Exit code `0` (clean compilation).

---

## 6. `.env` Metadata, Dataset Preservation & Git Diff Summary

- **`.env` Metadata (post-test):** `0600 abhisheksuman:staff 193 Regular File .env`; `git check-ignore -v .env` -> `.gitignore:11:*.env	.env`; `git ls-files .env` -> empty (untracked).
- **`.env.example` Metadata (post-test):** `0644 abhisheksuman:staff 1586 Regular File .env.example`; unchanged.
- **Dataset SHA-256 Checksums (post-test):**
  - `3be81f5e3ef7955343cadc43bc6e438285de070fd78d530610ea7b3276917adf  data/checkpoints/checkpoint_nifty_2026-11-02_2026-11-06.json`
  - `9c7008c81e2045e6fc52fc5a197cf9891d30e45d38d9f342b03a013365fa0065  data/checkpoints/raw_nifty_2026-11-02_2026-11-06_chunk_0.json`
  - `bd0ae4959ee8d3ed5c9bea5877343f74883894d71651a74bbfc19fd8d8943d34  data/historical/spot/NIFTY/manifests/manifest_nifty_69ca9b77.json`
  - `2f3010fbca42bebbd0c7ce5c8876aa7a954c1bca48a304fd2758c906ce7ebde0  data/historical/spot/NIFTY/year=2026/month=11/candles_NIFTY_2026_11.csv`
- **Git Diff Summary:** Only `backend/tests/test_gitignore_environment_protection.py` is modified (`1 file changed, 60 insertions(+), 10 deletions(-)`). No other tracked files were modified; no commits or pushes were made.
- **Remaining Failures/Warnings:** None (`0` failures, `0` errors, `0` skips across all 496 backend tests).

---

## 7. Safety Boundaries Confirmation

- `.env` was never read, printed, copied, modified, or deleted, and its `0600` permissions were unchanged.
- `.env.example` and `.gitignore` were not modified.
- `order_execution_enabled` remains `False`.
- No Angel One authentication, broker API calls, live market data fetches, supervisor startup, TCP port binding, Tailscale/WireGuard setup, TLS/Keychain changes, LaunchAgent installation, `mise trust`, or Git commits/pushes were performed.

