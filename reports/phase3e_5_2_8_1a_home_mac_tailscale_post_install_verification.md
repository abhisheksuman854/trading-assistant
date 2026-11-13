# Phase 3E.5.2.8.1A: Home Mac Tailscale Post-Installation Verification Report

**Date:** 2026-11-12  
**Machine:** Home Mac (`abhisheks-MacBook-Pro.local`)  
**Repository:** `/Users/abhisheksuman/Documents/GitHub/trading-assistant`  
**Branch:** `main` (`HEAD`: `fd24424d563770684d33a37afc4f0d7b3b6969f8`)  
**Mode:** Read-Only Post-Installation Verification  
**Verdict:** `HOME_MAC_TAILSCALE_CONNECTED_POLICY_PENDING`

---

## 1. Objective A — Installation Process Reconciliation

### Process Check (PIDs 9647 and 9784)
- **Command:** `ps -p 9647,9784 -o pid,ppid,user,stat,command` (unsandboxed)
- **Result:** Neither process exists (`PID PPID USER STAT COMMAND` returned empty, exit code `0`).
- **Reconciliation Finding:** The original Homebrew task (`task-908`) was canceled cleanly by Antigravity, and no lingering `installer` or `brew` child processes remain running.

### Application Presence & Package Registration
- **Application Path:** `/Applications/Tailscale.app` (`Contents` modified Nov 11 05:41, installed Nov 12 21:04).
- **CLI Executable:** `/usr/local/bin/tailscale` (symlinked/installed by official Tailscale app).
- **Installed Version:** `1.104.6` (commit `3b273a64cef3fe6d3d495afae7bb6a553344ce64`, Go `go1.27.3`).
- **Homebrew Cask Registration:** `brew list --cask` confirms `tailscale-app` is registered in Homebrew.

---

## 2. Objective B — Tailscale Network Verification

### Node Identity & Connection
- **Command:** `tailscale status` & `tailscale status --json`
- **Backend State:** `Running` (`Online: true`, `BackendState: "Running"`, `HaveNodeKey: true`).
- **Device Name:** `abhisheks-macbook-pro` (`HostName: "abhishek’s MacBook Pro"`).
- **Account:** `abhishek.suman854@gmail.com` (User ID `274820646397988`, Display Name `"Abhishek Suman"`).
- **Assigned IPv4 Address:** `100.94.158.50` (Matches expected IP exactly).
- **Assigned IPv6 Address:** `fd7a:115c:a1e0::2601:9fee`.
- **MagicDNS Domain:** `abhisheks-macbook-pro.tailc9c809.ts.net.` (`CorpDNS: true`).
- **Relay:** `bom` (Mumbai DERP relay, active local connection).

### Exposure & Subsystem Review
- **Subnet Routing:** Disabled (`AdvertiseRoutes: null`, `RouteAll: true` client-side only).
- **Exit Node:** Disabled (`ExitNodeID: ""`, `ExitNodeIP: ""`, `ExitNode: false`).
- **Tailscale Funnel:** Inactive (`tailscale funnel status` -> `No serve config`).
- **Tailscale Serve:** Inactive (`tailscale serve status` -> `No serve config`).
- **Tailscale SSH:** Disabled (`RunSSH: false`).
- **Web Client:** Disabled (`RunWebClient: false`).
- **App Connector / Drive Shares:** Disabled (`AppConnector.Advertise: false`, `DriveShares: null`).
- **Peers:** Zero other peers currently in tailnet (`tailscale status --peers=true` lists only Home Mac self-node).

---

## 3. Objective C — TradeLens Isolation Verification

- **Supervisor, Backend & Frontend:** Stopped (`PYTHONPATH=.:backend backend/.venv/bin/python3.12 -m backend.app.runtime.cli status` confirms `running: false`, `backend` & `frontend` `STOPPED`, `supervisor_pid: null`).
- **Ports 8000 & 8080:** Closed (`lsof -nP -iTCP:8000,8080 -sTCP:LISTEN` returned empty / no listeners).
- **LaunchAgents:** None installed in `~/Library/LaunchAgents/com.tradelens.*`.
- **TLS Certificates:** None generated in `runtime_state/tls/` or `~/.tradelens/certs/`.
- **Remote Auth State:** `runtime_state/remote_auth.json` does not exist; zero remote devices or sessions registered.
- **Broker Authentication:** Untouched (`is_authenticated: false`).
- **Order Execution:** Permanently disabled (`order_execution_enabled: false`).
- **`.env` File:** Mode `0600` (`-rw-------`), regular file (`193` bytes, owned by `abhisheksuman:staff`), gitignored (`.gitignore:11:*.env`), untracked; contents were never read or modified.

---

## 4. Objective D — Tailnet Policy Review

- **Local Inspection:** Tailscale client CLI does not expose remote administrator ACL rules directly (`tailscale debug prefs` shows local client preferences only).
- **Status:** `NOT_VERIFIED` locally.
- **Admin Console Action Required:** The Tailscale Admin Console (access controls / ACLs) must be checked by the owner before Office Mac provisioning to ensure least-privilege rules (e.g. restricting Office Mac to Home Mac ports 8000/8080 rather than broad `autogroup:members` default access).

---

## 5. Git Status & Dataset Integrity

- **Git Status:**
  - ` M backend/tests/test_gitignore_environment_protection.py` (preserved from Phase 3E.5.2.7.7)
  - Untracked reports from Phases 3E.5.2.7.6, 3E.5.2.7.7, 3E.5.2.7.8, and 3E.5.2.8 preserved.
- **Dataset SHA-256 Checksums (all matched):**
  - `3be81f5e3ef7955343cadc43bc6e438285de070fd78d530610ea7b3276917adf  data/checkpoints/checkpoint_nifty_2026-11-02_2026-11-06.json`
  - `9c7008c81e2045e6fc52fc5a197cf9891d30e45d38d9f342b03a013365fa0065  data/checkpoints/raw_nifty_2026-11-02_2026-11-06_chunk_0.json`
  - `bd0ae4959ee8d3ed5c9bea5877343f74883894d71651a74bbfc19fd8d8943d34  data/historical/spot/NIFTY/manifests/manifest_nifty_69ca9b77.json`
  - `2f3010fbca42bebbd0c7ce5c8876aa7a954c1bca48a304fd2758c906ce7ebde0  data/historical/spot/NIFTY/year=2026/month=11/candles_NIFTY_2026_11.csv`

---

## 6. Explicit Actions Not Performed

- No uninstallation, process killing, or network configuration mutation was performed.
- No TLS certificate generation or installation was executed.
- No Office Mac device registration or key exchange was performed.
- No TradeLens backend, frontend, or supervisor process was started; no ports were opened.
- No broker API or live market data connection was initiated.
- No Git commits, pushes, or resets were executed.

---

## 7. Final Verdict

**Verdict:** `HOME_MAC_TAILSCALE_CONNECTED_POLICY_PENDING`

Tailscale is fully installed, authenticated, and operating in a connected state on Home Mac with private IP `100.94.158.50` and MagicDNS `abhisheks-macbook-pro.tailc9c809.ts.net.`. All TradeLens application listeners, credentials, and processes remain completely isolated and stopped. Tailnet ACL verification remains pending in the Tailscale admin console prior to Office Mac provisioning.

