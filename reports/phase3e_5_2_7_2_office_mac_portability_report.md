# TradeLens AI — Phase 3E.5.2.7.2: Office Mac Portability Corrections & Offline Test Hardening Report

**Verdict:** `OFFICE_MAC_PREPARATION_CONDITIONALLY_COMPLETE`  
**Phase / Task:** Phase 3E.5.2.7.2 (Office Mac Portability & Offline Test Hardening)  
**Machine Executed On:** Office Mac (`arm64`, macOS `26.0`)  
**Order Execution:** `False` (`order_execution_enabled=False`)  
**Broker Authentication / Phase 3F / Live Home Mac Provisioning:** **NOT AUTHORIZED & NOT EXECUTED**

---

## 1. Machine Identity & Repository Identity

| Attribute | Verified Value |
|---|---|
| Execution Machine | Office Mac (`arm64`, macOS `26.0`) |
| Office Mac Workspace Root | `/Users/abhisheksuman/Documents/GitHub/tradelens-ai` |
| Intended Home Mac Workspace Root | `/Users/abhisheksuman/Documents/GitHub/trading-assistant` *(not provisioned on this machine)* |
| Git Branch | `main` |
| Git Base `HEAD` | `713c4ce5080f64c0cde1db30438deb43240486bb` |
| Active Tooling Observed | `backend/.venv/bin/python3.12` (`3.12.13`), `/opt/homebrew/opt/python@3.12/bin/python3.12` (`3.12.13`), `mise 2026.2.19 macos-arm64 (2026-02-22)`, `/usr/bin/plutil`, `node v22.22.3` |

> **Scope Boundary:** All modifications and checks in this report were executed **offline on the Office Mac only**. Zero Home Mac provisioning, runtime startup, network calls, Keychain mutations, certificate generation, LaunchAgent installation, or Home Mac filesystem verification took place.

---

## 2. Stage A — Office Mac `.env` Security Status (No Secret Values Printed)

Metadata inspection of `/Users/abhisheksuman/Documents/GitHub/tradelens-ai/.env` was performed via filesystem `stat`, `parse_dotenv_content`, and `backend.app.runtime.stage_b_setup._inspect_dotenv_metadata(..., environ={})` without printing or logging any values:

| Check | Result |
|---|---|
| Path | `/Users/abhisheksuman/Documents/GitHub/tradelens-ai/.env` |
| Exists / Symlink | `exists: true`, `is_symlink: false` |
| File Size & Mode | `194` bytes, mode `0o644` (`100644`), `strict_0600_ok: false` |
| Dotenv Syntax | `syntax_ok: true`, `parsed_key_count: 5`, `nonempty: 5` |
| Present Key Names Only | `ANGEL_API_KEY`, `ANGEL_CLIENT_CODE`, `ANGEL_PASSWORD`, `ANGEL_TOTP_SECRET`, `PORT` |
| `_inspect_dotenv_metadata` Statuses | All four `ANGEL_*_FORMAT_STATUS` fields returned `"PRESENT_VALID_FORMAT"` (`angel_config_ready: true`) |
| Git Ignore & Index Status | Ignored by `.gitignore:7:.env`; `git ls-files .env` empty; `git diff --cached --name-only` empty |
| Action Taken | **Preserved untouched (NOT deleted, NOT copied, NOT committed)** |

**Security Finding:** Because all four Angel One keys match non-placeholder format (`PRESENT_VALID_FORMAT`) on the temporary Office Mac at permissive mode `0o644`, automatic deletion was intentionally halted per Stage A rules. **Owner approval is requested before removing `/Users/abhisheksuman/Documents/GitHub/tradelens-ai/.env`.** Never transfer this file to the Home Mac; create a fresh `0600` `.env` directly on the Home Mac instead.

---

## 3. Stage B — Portability Corrections

1. **Shared Portable Python Resolver (`backend/app/runtime/launchd_config.py`, exported in `backend/app/runtime/__init__.py`):**
   - Added `resolve_python_executable(repo_root=None, configured=None, environ=None)`.
   - Updated `build_home_engine_plist` and `build_frontend_plist` to default `python_executable: Optional[str] = None` and resolve dynamically via `resolve_python_executable(repo_root=root, configured=python_executable)`.
2. **CLI & Service Builder Portability (`backend/app/runtime/cli.py`):**
   - Updated `_build_services` and `cmd_render_launchd` to resolve Python through `resolve_python_executable`.
   - Changed `--python-exe` defaults on both `render-launchd` and `stage-b` subparsers from hardcoded `/opt/homebrew/bin/python3.12` to `None`.
   - Updated `cmd_render_launchd` informational note to warn against copying Office Mac plist templates or treating templates as installed services.
3. **Stage B Validator & Setup Catalog (`backend/app/runtime/stage_b_setup.py`):**
   - Updated `_validate_python_executable(candidate, *, repo_root=None, environ=None)` to resolve omitted candidates via `resolve_python_executable`, reject `".."` path segments, support `/opt/homebrew/opt/` Cellar/opt prefixes, and validate `<repo_root>/backend/.venv/bin` against both raw and `.resolve()` repository paths.
   - Updated `tradelens-stage-b-render-launchd` execution path to pass `repo_root=self.repo_root` and `environ=self.environ`.
   - Updated `cmd_render_launchd` catalog description to explicitly target the active Home Mac checkout.
4. **Toolchain, UI & Docs (`mise.toml`, `frontend/index.html`, `docs/operations/STAGE_B_SETUP.md`, `README.md`):**
   - Updated `mise.toml` `_.path` to `["{{config_root}}/backend/.venv/bin", "/opt/homebrew/bin", "/usr/local/bin"]`.
   - Removed hardcoded `/opt/homebrew/bin/python3.12` from the Path A Keychain CLI hint in `frontend/index.html` (`line 1788`), keeping `python3.12`.
   - Updated `docs/operations/STAGE_B_SETUP.md` with portable Python precedence, Home Mac example path (`/Users/abhisheksuman/Documents/GitHub/trading-assistant`), and explicit prohibition on copying Office Mac `.env`, TLS keys, or `deploy/launchd/*.plist` files.
   - Updated `README.md` Quick Start examples to invoke `python3.12` via `mise`/`PATH`. *(Line 87 keeps `tradelens-ai/` purely as an illustrative directory-tree label.)*

---

## 4. LaunchAgent Template Portability

- Deleted stale Office-Mac-hardcoded templates from the working tree (unstaged `D` awaiting owner commit):
  - `deploy/launchd/com.tradelens.home-engine.plist`
  - `deploy/launchd/com.tradelens.frontend-dashboard.plist`
- Added ignore rules to `.gitignore`:
  - `deploy/launchd/*.plist`
  - `*.crt`
  - `*.key`
  - `*.pem`
- Added tracked documentation `deploy/launchd/README.md` instructing the Home Mac operator to generate and validate fresh machine-specific templates locally:
  ```zsh
  mise run tradelens-stage-b-render-launchd
  mise run tradelens-stage-b-launchd-lint
  ```
- Confirmed `git check-ignore --no-index -v deploy/launchd/com.tradelens.home-engine.plist deploy/launchd/com.tradelens.frontend-dashboard.plist` matches `.gitignore:13:deploy/launchd/*.plist`.

---

## 5. Python Interpreter Handling

`resolve_python_executable(repo_root=None, configured=None, environ=None)` uses strict, deterministic precedence across both Apple Silicon (`/opt/homebrew/bin`) and Intel/standard macOS layouts (`/usr/local/bin`, `/usr/bin`) without hardcoding a single environment:

1. Explicit `configured` executable argument (if non-empty).
2. `TRADELENS_PYTHON_EXECUTABLE` from `environ` (or `os.environ`).
3. Repository virtualenv: `<repo_root>/backend/.venv/bin/python3.12`, then `<repo_root>/backend/.venv/bin/python3`.
4. Active `sys.executable` when absolute, executable, and `Path(cur_py).name.startswith("python3")`.
5. `shutil.which("python3.12")`, `/opt/homebrew/bin/python3.12`, `/usr/local/bin/python3.12`, `shutil.which("python3")`, `/usr/bin/python3`.
6. Fallback: `cur_py or "python3.12"`.

`mise.toml` sets `PYTHONPATH = "{{config_root}}:{{config_root}}/backend"` and `_.path = ["{{config_root}}/backend/.venv/bin", "/opt/homebrew/bin", "/usr/local/bin"]`, allowing every `mise run tradelens-stage-b-*` task to invoke `python3.12 -m backend.app.runtime.cli ...` consistently across Home Mac and Office Mac without relying on Office-specific paths.

---

## 6. Test Fixture Isolation Improvements

- **`backend/tests/test_stage_b_setup_and_mise.py`:**
  - Rewrote `test_06_mise_toml_and_docs_completeness` to run completely isolated from the Office Mac repository `.env` using `isolated_root`, `patch("backend.app.runtime.cli.REPO_ROOT", isolated_root)`, and a sanitized `os.environ` (`ANGEL_*` and `TRADELENS_*` stripped). Verified:
    1. Missing `.env` fails Step 1 preflight (`rc=2`).
    2. Permissive `0644` `.env` fails closed (`rc=2`).
    3. Symlinked `.env` fails closed (`rc=2`).
    4. Placeholder Angel One values fail preflight (`rc=2`).
    5. Malformed `.env` fails without leaking canary secret values in output.
    6. Remote mode (`supervise` + `deploy/scripts/run_launchd_service.sh`) with missing `TRADELENS_STATE_HMAC_SECRET` fails closed (`exit 78`).
    7. Valid-format `0600` dummy `.env` passes `home-preflight` (`exit 0`, Step 1 `VERIFIED`, zero dummy secret echoes) while Steps 2–10 gates return `2`.
    8. Spy guard confirms `REPO_ROOT / ".env"` is never inspected by `test_06`.
  - Updated `test_07_frontend_setup_ui_accessibility_and_csp_compliance` to locate `node` portably instead of hardcoding `/Users/abhisheksuman/.nvm/...`.
  - Isolated `test_09_python_executable_validation_and_manual_confirmation_gates` by patching `backend.app.runtime.cli.REPO_ROOT` to `self.root`.
  - Added `test_17_portable_launchd_rendering_and_python_resolution` covering `resolve_python_executable` precedence, `TRADELENS_PYTHON_EXECUTABLE`, temp target-repo `.venv` rendering + `plutil -lint`, and mocked `/Users/example_home/Documents/GitHub/trading-assistant` plist generation with zero stale `Documents/GitHub/tradelens-ai` paths.
- **`backend/tests/test_pre_provisioning_security_gate_stage_a1.py`:**
  - Replaced hardcoded `/opt/homebrew/bin/python3.12` wrapper arguments at lines `559` and `1315` with `sys.executable`.

---

## 7. Targeted & Full Offline Verification Results (Office Mac)

| Check | Command / Scope | Observed Result |
|---|---|---|
| Targeted Stage A / A.1 / Stage B Suites (`backend/.venv`) | `PYTHONPATH=.:backend backend/.venv/bin/python3.12 -m unittest backend.tests.test_stage_b_setup_and_mise backend.tests.test_pre_provisioning_security_gate_stage_a1 backend.tests.test_home_mac_runtime_and_remote_availability backend.tests.test_private_network_provisioning_stage_a -v` | **`Ran 51 tests in 17.145s` — `OK`** (`17` Stage B, `16` Stage A.1, `10` Home Runtime, `8` Stage A; `0` skipped, `0` failed) |
| Full `unittest` Discovery (`PYTHONPATH=.:backend`) | `PYTHONPATH=.:backend /opt/homebrew/bin/python3.12 -m unittest discover -s backend/tests -v` | **`Ran 490 tests in 106.131s` — `OK`** (`0` skipped, `0` failed, `0` errors) |
| Full `unittest` Discovery without `PYTHONPATH` *(diagnostic)* | `/opt/homebrew/bin/python3.12 -m unittest discover -s backend/tests -v` | `410` discovered tests ran with `errors=11` (`ModuleNotFoundError: No module named 'app'` in older `app.*` imports), confirming `PYTHONPATH=.:backend` (as set in `mise.toml` `[env]`) is required for manual `unittest` invocations |
| Core Engine Smoke Suite | `PYTHONPATH=.:backend /opt/homebrew/bin/python3.12 backend/test_api.py` | **Exited `0`** (`ALL TESTS PASSED!`) |
| Python Bytecode Syntax Check | `/opt/homebrew/bin/python3.12 -m compileall -q backend/app backend/tests backend/cli_scanner.py backend/test_api.py` | **Exited `0`** |
| Offline `mise.toml` TOML Parse & Task Catalog | Python `tomllib.loads` inspection of `mise.toml` | **Exited `0`** — valid TOML, `16` `tradelens-stage-b-*` tasks defined with portable `python3.12` + `_.path` + `PYTHONPATH` |
| Live `mise` Task Listing in Sandbox | `/opt/homebrew/bin/mise tasks --json` | **Failed inside sandbox** (`mise ERROR Operation not permitted (os error 1)` due to sandbox blocking `mise` state/config paths outside workspace); `mise trust` was **not** invoked |
| LaunchAgent XML Validation | `plutil -lint` inside `test_17` and Stage A tests | **Passed (`rc=0`)** on isolated temporary renders; no system LaunchAgents installed |

---

## 8. Frontend Syntax & Static Analysis Results

- Extracted inline `<script>` block (`js_scripts: 1`) from `frontend/index.html` and checked with `/Users/abhisheksuman/.nvm/versions/node/v22.22.3/bin/node --check`: **`node_check_rc: 0`**, zero syntax errors.
- `test_stage_b_setup_and_mise.TestStageBSetupAndMise.test_07_frontend_setup_ui_accessibility_and_csp_compliance`: **Passed** (`OK`).
- Repository text grep (`grep -rnE --exclude-dir=__pycache__ "/Users/|/opt/homebrew/bin/python|tradelens-ai" backend/app backend/tests deploy docs frontend mise.toml README.md .gitignore`) confirmed:
  - Zero `/Users/` or `/opt/homebrew/bin/python` strings remain in `frontend/index.html`, `mise.toml`, `backend/app/runtime/cli.py`, `backend/app/runtime/stage_b_setup.py`, or `backend/tests/test_pre_provisioning_security_gate_stage_a1.py`.
  - Remaining intentional references only: standard macOS fallback list entry `"/opt/homebrew/bin/python3.12"` in `backend/app/runtime/launchd_config.py:64`, mocked target-path test assertions in `backend/tests/test_stage_b_setup_and_mise.py`, Home Mac example paths in `docs/operations/STAGE_B_SETUP.md`, and tree diagram label `tradelens-ai/` in `README.md:87`.

---

## 9. Stage E — Dataset Transfer Manifest & Checksums

Inspected `data/` offline on Office Mac without modifying, deleting, moving, or committing any dataset files:

| File Path | Size (Bytes) | SHA-256 Checksum | Git Status (`HEAD 713c4ce`) |
|---|---:|---|---|
| `data/checkpoints/checkpoint_nifty_2026-11-02_2026-11-06.json` | `569` | `3be81f5e3ef7955343cadc43bc6e438285de070fd78d530610ea7b3276917adf` | Tracked (`H`, blob `6d1ecc184260cf998fba34b1863c97363a00e661`) |
| `data/checkpoints/raw_nifty_2026-11-02_2026-11-06_chunk_0.json` | `129933` | `9c7008c81e2045e6fc52fc5a197cf9891d30e45d38d9f342b03a013365fa0065` | Tracked (`H`, blob `0392d5b900bd7056aab9450c44cfdb420059b93f`) |
| `data/historical/spot/NIFTY/manifests/manifest_nifty_69ca9b77.json` | `4091` | `bd0ae4959ee8d3ed5c9bea5877343f74883894d71651a74bbfc19fd8d8943d34` | Tracked (`H`, blob `d7bb26cb12fa123d3bdae4a4df91d3659f12bd2c`) |
| `data/historical/spot/NIFTY/year=2026/month=11/candles_NIFTY_2026_11.csv` | `341381` | `2f3010fbca42bebbd0c7ce5c8876aa7a954c1bca48a304fd2758c906ce7ebde0` | Tracked (`H`, blob `42869175f61db6ef9fa860f0b0d174d0335b2c22`) |

- **Total Dataset Size:** `475,974` bytes across `4` files (`find data -type f | sort`).
- **Gitignore Finding:** `data/` is **not** in `.gitignore`; all four NIFTY files are already tracked in Git at `713c4ce`. When Home Mac pulls `main`, Git should materialize them cleanly unless local checkout divergence exists. Direct inspection of `/Users/abhisheksuman/Documents/GitHub/trading-assistant/data` was **not** performed from this Office Mac session (`Operation not permitted` outside workspace sandbox).

---

## 10. Git Staging & Secret-Leak Audit

- `git diff --cached --name-only`: **empty** (nothing staged; no commit or push executed).
- `git ls-files .env`: **empty**; `.env` is ignored via `.gitignore:7:.env`.
- `git check-ignore --no-index -v` verified ignore coverage for `.env`, `deploy/launchd/*.plist`, `*.crt`, `*.key`, and `*.pem`. (`runtime_state/` and `runtime_logs/` are already present in `.gitignore` lines 11–12.)
- `.ops_state/stage_b_progress.json` and `.ops_state/.stage_b_progress.lock` remain tracked at `HEAD` (`713c4ce`) and unmodified (`stage_verdict: None`, `has_secret_like: False`, `has_office_path: False`).
- Full `git diff` reviewed: zero private keys, TLS certs, broker tokens, or hardcoded credentials present in modified files.

---

## 11. Files Changed (Working Tree, Uncommitted)

| Status | Path |
|---|---|
| `M` | `.gitignore` |
| `M` | `README.md` |
| `M` | `backend/app/runtime/__init__.py` |
| `M` | `backend/app/runtime/cli.py` |
| `M` | `backend/app/runtime/launchd_config.py` |
| `M` | `backend/app/runtime/stage_b_setup.py` |
| `M` | `backend/tests/test_pre_provisioning_security_gate_stage_a1.py` |
| `M` | `backend/tests/test_stage_b_setup_and_mise.py` |
| `D` | `deploy/launchd/com.tradelens.frontend-dashboard.plist` |
| `D` | `deploy/launchd/com.tradelens.home-engine.plist` |
| `A` | `deploy/launchd/README.md` *(currently untracked `??`)* |
| `M` | `docs/operations/STAGE_B_SETUP.md` |
| `M` | `frontend/index.html` |
| `M` | `mise.toml` |
| `A` | `reports/phase3e_5_2_7_2_office_mac_portability_report.md` *(this report)* |

---

## 12. Outstanding Owner Actions

### On Office Mac (`/Users/abhisheksuman/Documents/GitHub/tradelens-ai`)
1. **Explicitly decide on `.env` removal:** `.env` (`194` bytes, mode `0o644`) contains valid-format Angel One keys (`ANGEL_API_KEY`, `ANGEL_CLIENT_CODE`, `ANGEL_PASSWORD`, `ANGEL_TOTP_SECRET`). Confirm deletion (for example `rm .env`) once no longer needed on Office Mac.
2. **Review diff, stage, commit, and push cleanly:**
   - Review `git status` and `git diff`.
   - Stage intended changes (never `git add -f .env`, certs, or `.plist` files), commit, and push `main` using GitHub Desktop or clean credential helper (do **not** embed personal access tokens directly in shell history).

### On Home Mac (`/Users/abhisheksuman/Documents/GitHub/trading-assistant`) — *Pending*
1. `git pull` the pushed portability commits on `main`.
2. Inspect `mise.toml` and, only after owner approval, run `mise trust` in `/Users/abhisheksuman/Documents/GitHub/trading-assistant` and confirm `backend/.venv/bin/python3.12` (`python3.12`) has project dependencies installed.
3. Verify the 4 NIFTY dataset files under `data/` match the SHA-256 checksums in Section 9 (`shasum -a 256 ...`).
4. Provision owner-only `0600` `.env` directly on Home Mac (with `TRADELENS_STATE_HMAC_SECRET` before remote mode) — never copy `.env` from Office Mac.
5. Run local Stage B preflight, tests (`PYTHONPATH=.:backend python3.12 -m unittest discover -s backend/tests -v`), and Home Mac `render-launchd` / `launchd-lint` directly inside `/Users/abhisheksuman/Documents/GitHub/trading-assistant`.

