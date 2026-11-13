# Phase 3E.5.2.8.2: Home Mac Tailscale Policy & Connectivity Verification Report

**Date:** 2026-11-12  
**Machine:** Home Mac ONLY (`abhisheks-MacBook-Pro.local`)  
**Repository:** `/Users/abhisheksuman/Documents/GitHub/trading-assistant`  
**Branch:** `main` (`HEAD`: `fd24424d563770684d33a37afc4f0d7b3b6969f8`)  
**Mode:** Strictly Read-Only Audit  
**Verdict:** `HOME_MAC_CONNECTED_POLICY_VERIFICATION_PENDING`

---

## 1. Machine Identity & Audit Timestamp

- **Audit Timestamp:** 2026-11-12 21:46:15 IST
- **Host:** `abhisheks-MacBook-Pro.local` (macOS 26.0 arm64)
- **Repository Root:** `/Users/abhisheksuman/Documents/GitHub/trading-assistant`
- **Branch / HEAD:** `main` / `fd24424d563770684d33a37afc4f0d7b3b6969f8`

---

## 2. Section 1 — Home Mac Tailscale Status

### Commands Executed
```bash
tailscale version
tailscale status
tailscale ip -4
tailscale ip -6
tailscale netcheck
tailscale status --peers=true
tailscale debug prefs
```

### Observed Evidence
- **Installed Version:** `1.104.6` (commit `3b273a64cef3fe6d3d495afae7bb6a553344ce64`, Go `go1.27.3`).
- **Device Hostname:** `abhisheks-macbook-pro` (Display: `HostName: "abhishek’s MacBook Pro"`).
- **Tailscale IPv4 Address:** `100.94.158.50`.
- **Tailscale IPv6 Address:** `fd7a:115c:a1e0::2601:9fee`.
- **Connection & Relay State:** Connected, healthy (`BackendState: "Running"`, `Online: true`, UDP active, nearest DERP: Mumbai `bom` 74.8ms).
- **Subnet Routes:** Disabled (`AdvertiseRoutes: null`, `RouteAll: true` client-side only).
- **Exit Node:** Disabled (`ExitNode: false`, `ExitNodeID: ""`, `ExitNodeIP: ""`).
- **Tailscale Funnel:** Inactive (`No serve config`).
- **Tailscale Serve:** Inactive (`No serve config`).
- **Tailscale SSH:** Disabled (`RunSSH: false`).
- **Web Client / App Connector / Drive Shares:** Disabled (`RunWebClient: false`, `AppConnector.Advertise: false`, `DriveShares: null`).
- **Peers:** Zero other peers in the tailnet (`tailscale status --peers=true` lists only Home Mac self-node).

*(Note: Account email address and personal user profile identifiers are strictly redacted from this report.)*

---

## 3. Section 2 — Saved Access Policy Verification

- **Mechanism Used:** Read-only inspection via local CLI and environment checks.
- **Result:** `POLICY_VERIFICATION_NOT_INDEPENDENTLY_CONFIRMED`
- **Audit Explanation:** The Tailscale client CLI does not expose server-side tailnet ACL rules/grants locally. Inspecting `tailscale debug prefs` only reveals node-local client preferences. No authenticated browser session or Tailscale API token was used to query the Admin Console programmatically.
- **Requirement:** As established in the audit rules, server-side policy state cannot be claimed based on local CLI state. The owner's confirmation that `grants` is empty, `ssh` is empty, and no active broad allow-all rule exists in the Tailscale Admin Console remains a server-side control that requires independent Admin Console verification prior to enrolling any remote client.

---

## 4. Section 3 — Home Mac Exposure Boundaries

### Network Listener Inspection
Executed `lsof -nP -iTCP -sTCP:LISTEN`:
- Port `8000`: **No listener** (TradeLens backend stopped).
- Port `8080`: **No listener** (TradeLens frontend stopped).
- Ports `7000`, `5000`: macOS system AirPlay/ControlCenter services (bound by macOS).
- Port `49152`: macOS system `rapportd`.
- Port `11434`: Local loopback `127.0.0.1:11434` (`ollama`).
- Local ephemeral loopback listeners: Electron, GitHub Desktop, Antigravity (`127.0.0.1` only).
- Zero remote TradeLens or unexpected third-party listeners exposed on `100.94.158.50` or `0.0.0.0`.

### TradeLens Isolation Verification
- **Supervisor Status:** `running: false`, `supervisor_pid: null`.
- **Services:** `backend.status: STOPPED`, `frontend.status: STOPPED`.
- **LaunchAgents:** Zero installed in `~/Library/LaunchAgents/com.tradelens.*`.
- **TLS Certificates:** None generated in `runtime_state/tls/` or `~/.tradelens/certs/`.
- **Remote Sessions:** `runtime_state/remote_auth.json` does not exist; zero remote sessions or devices registered.
- **Broker & Order Execution:** `is_authenticated = false`, `order_execution_enabled = false`.
- **Credential Hygiene:** `.env` exists as a regular file (`193` bytes), mode `0600`, gitignored (`.gitignore:11:*.env`), untracked; contents were never read or modified.

---

## 5. Section 4 — Git Status & Dataset Integrity

### Git Status (Before & After)
Unchanged throughout this read-only audit:
- ` M backend/tests/test_gitignore_environment_protection.py` (preserved from Phase 3E.5.2.7.7)
- Preserved untracked reports:
  - `?? reports/phase3e_5_2_7_6_home_mac_local_verification_report.md`
  - `?? reports/phase3e_5_2_7_7_home_mac_test_portability_report.md`
  - `?? reports/phase3e_5_2_7_8_home_mac_foreground_runtime_report.md`
  - `?? reports/phase3e_5_2_7_9_home_mac_checksum_and_runtime_baseline_report.md`
  - `?? reports/phase3e_5_2_8_home_mac_stage_b_readiness_report.md`
  - `?? reports/phase3e_5_2_8_1a_home_mac_tailscale_post_install_verification.md`

### Dataset SHA-256 Checksums (Bit-for-bit matched)
- `3be81f5e3ef7955343cadc43bc6e438285de070fd78d530610ea7b3276917adf  data/checkpoints/checkpoint_nifty_2026-11-02_2026-11-06.json`
- `9c7008c81e2045e6fc52fc5a197cf9891d30e45d38d9f342b03a013365fa0065  data/checkpoints/raw_nifty_2026-11-02_2026-11-06_chunk_0.json`
- `bd0ae4959ee8d3ed5c9bea5877343f74883894d71651a74bbfc19fd8d8943d34  data/historical/spot/NIFTY/manifests/manifest_nifty_69ca9b77.json`
- `2f3010fbca42bebbd0c7ce5c8876aa7a954c1bca48a304fd2758c906ce7ebde0  data/historical/spot/NIFTY/year=2026/month=11/candles_NIFTY_2026_11.csv`

---

## 6. Explicit Actions Not Performed

- No configuration changes were made to Tailscale, macOS, network interfaces, firewall, or TradeLens.
- No Tailscale ACLs, grants, tags, or settings were modified.
- No TLS certificates were generated or installed.
- No Office Mac device was registered or approved.
- No TradeLens supervisor, backend, or frontend process was started; ports 8000/8080 were never opened.
- No broker authentication, quotes, or market-data operations were executed.
- `.env` contents were never read or printed.
- No Git commits, pushes, resets, or stash operations were performed.

---

## 7. Final Verdict

**Verdict:** `HOME_MAC_CONNECTED_POLICY_VERIFICATION_PENDING`

Tailscale is fully connected on the Home Mac (`100.94.158.50`, relay `bom`), with subnet routing, exit nodes, Funnel, Serve, and SSH confirmed disabled locally, zero external peers present, and TradeLens completely stopped and isolated. Because Tailscale access control rules cannot be queried via the local client CLI, independent verification of the server-side Admin Console policy remains explicitly designated as pending.

