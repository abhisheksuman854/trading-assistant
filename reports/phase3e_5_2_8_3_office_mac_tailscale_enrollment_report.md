# Phase 3E.5.2.8.3 — Office Mac Tailscale Installation & Private Network Enrollment Report

- **Audit & Enrollment Date:** 2026-11-13
- **Machine:** Office Mac ONLY (`Abhisheks-MacBook-Air.local`)
- **Target Role:** Private-network client for later read-only assistance connection to Home Mac
- **Tailnet Account:** `abhishek.suman854@gmail.com`
- **Assigned Office Mac Tailscale IPv4:** `100.95.157.49`
- **Assigned Office Mac Tailscale IPv6:** `fd7a:115c:a1e0::ea2c:9d32`
- **Home Mac Reference Peer:** `abhisheks-macbook-pro` (`100.94.158.50`)
- **Final Verdict:** **`OFFICE_MAC_TAILSCALE_ENROLLED_ISOLATED`**

---

## 1. Executive Summary

Tailscale standalone macOS application (`1.104.7`) has been verified and installed on the Office Mac using the official signed Apple-notarized package (`Tailscale-1.104.7-macos.pkg`). The application was launched, enrolled into the existing personal tailnet (`abhishek.suman854@gmail.com`), and verified as online with its dedicated Tailscale IP address (`100.95.157.49`, IPv6 `fd7a:115c:a1e0::ea2c:9d32`).

Strict network isolation and default-deny access policies remain intact:
- Tailscale SSH is **disabled** (`RunSSH: false`).
- Funnel and Serve are **inactive** (`No serve config`).
- Exit node routing is **disabled** (`ExitNodeID: ""`).
- Subnet route advertising is **disabled** (`AdvertiseRoutes: null`).
- Non-invasive socket probes to Home Mac ports `8000` and `8080` confirmed connections are **blocked / inaccessible**.
- **Zero changes** were made to the Home Mac, and **zero ACLs or grants** were modified in the Tailscale admin console.

---

## 2. Pre-Installation Host State

| Parameter | Observed Value |
|---|---|
| Hostname | `Abhisheks-MacBook-Air.local` |
| macOS Version | macOS `26.0` (Build `25A354`) |
| Architecture | `arm64` |
| Current User | `abhisheksuman` |
| Prior Tailscale Installation | None (`which tailscale` -> not found, `/Applications/Tailscale.app` absent) |
| Prior Tailscale Processes | None running |
| Primary Physical Interface | `en0` (`192.168.0.128/24`, gateway `192.168.0.1`) |

---

## 3. Installation Verification

- **Package Source:** Official Tailscale standalone distribution (`Tailscale-1.104.7-macos.pkg`), signed by `Developer ID Installer: Tailscale Inc. (W5364U7YZB)` and notarized by Apple.
- **Application Path:** `/Applications/Tailscale.app`
- **CLI Wrapper:** `/usr/local/bin/tailscale` -> `/Applications/Tailscale.app/Contents/MacOS/Tailscale`
- **CLI Version Output (`tailscale version`):**
  ```
  1.104.7
    tailscale commit: fb3b9b2964ace04490fbd65d4f26a0eaf362131f
    long version: 1.104.7-tfb3b9b296-g0651cc45e
    other commit: 0651cc45e79e308b0b3b808f4b42e7f3a1a8c221
    go version: go1.27.4 (tailscale/go 1635316a95)
  ```

---

## 4. Enrollment & Identity Verification

- **Tailnet:** `abhishek.suman854@gmail.com`
- **MagicDNS Domain:** `tailc9c809.ts.net`
- **Backend State:** `Running`
- **Node Self Status:**
  - HostName: `Abhishek’s MacBook Air`
  - DNSName: `abhisheks-macbook-air.tailc9c809.ts.net.`
  - Online: `True`
  - IPv4: `100.95.157.49`
  - IPv6: `fd7a:115c:a1e0::ea2c:9d32`
  - AllowedIPs: `['100.95.157.49/32', 'fd7a:115c:a1e0::ea2c:9d32/128']` (strictly node IPs only)
- **Tailnet Peer Visibility:**
  - Home Mac Peer: `abhishek’s MacBook Pro` (`abhisheks-macbook-pro.tailc9c809.ts.net.`)
  - Home Mac Tailscale IP: `100.94.158.50` (IPv6 `fd7a:115c:a1e0::2601:9fee`)
  - Status reported by control plane: `offline, last seen 15h ago`

---

## 5. Network Interface & Routing Verification

- **Assigned Network Interface:** `utun4`
  ```
  inet 100.95.157.49 --> 100.95.157.49 netmask 0xffffffff
  inet6 fd7a:115c:a1e0::ea2c:9d32 prefixlen 48
  ```
- **IPv4 Routing Table Entries for Tailnet:**
  - `100.95.157.49` (Self host route) -> `utun4`
  - `100.94.158.50/32` (Home Mac peer route) -> `utun4`
  - `100.100.100.100/32` (MagicDNS quad-100 resolver) -> `utun4`
- **No Rogue Interfaces or Broad Subnets:** No foreign routes or default gateway hijacking was introduced.

---

## 6. Service & Boundary Isolation Verification

| Feature / Setting | Inspected Property | Verified State | Compliant? |
|---|---|---|---|
| Tailscale SSH | `RunSSH` | `false` | Yes |
| Tailscale Funnel | `tailscale funnel status` | `No serve config` (inactive) | Yes |
| Tailscale Serve | `tailscale serve status` | `No serve config` (inactive) | Yes |
| Exit Node Client | `ExitNodeID`, `ExitNodeIP` | `""` (disabled) | Yes |
| Exit Node Advertising | `ExitNodeOption` | `false` (disabled) | Yes |
| Subnet Route Advertising | `AdvertiseRoutes` | `null` (none advertised) | Yes |
| App Connector | `AppConnector.Advertise` | `false` | Yes |
| Web Client Daemon | `RunWebClient` | `false` | Yes |

---

## 7. Verification of Network Isolation (Default Deny)

Non-invasive TCP connection tests were conducted from the Office Mac to the Home Mac reference IP (`100.94.158.50`):

| Target Endpoint | Socket Probe Result | Network Isolation Status |
|---|---|---|
| `100.94.158.50:8000` (TradeLens Backend) | `connect_ex() = 35` (Resource temporarily unavailable / Timeout) | **BLOCKED / INACCESSIBLE** (Expected) |
| `100.94.158.50:8080` (TradeLens Frontend) | `connect_ex() = 35` (Resource temporarily unavailable / Timeout) | **BLOCKED / INACCESSIBLE** (Expected) |

---

## 8. Compliance with Strict Execution Boundaries

- **Home Mac:** Zero commands, network connections, or modifications were made on the Home Mac.
- **Tailscale ACLs / Policy:** No ACLs, grants, tag assignments, or security settings were altered in the Tailscale admin console.
- **TradeLens Repository:** Zero files were committed, pushed, stashed, reset, or modified in `/Users/abhisheksuman/Documents/GitHub/tradelens-ai`.
- **Credentials & Security:** No passwords, authentication tokens, session cookies, or private keys were printed, logged, or requested.
- **Broker / Runtime Isolation:** Angel One APIs were not contacted; TradeLens supervisor and runtime servers were not started.

---

## 9. Recorded Information for Subsequent Access-Policy Phase

When the explicit, separate access-policy phase is authorized:
- **Office Mac Device Name:** `abhisheks-macbook-air` (`abhisheks-macbook-air.tailc9c809.ts.net`)
- **Office Mac Tailscale IPv4:** `100.95.157.49`
- **Office Mac Tailscale IPv6:** `fd7a:115c:a1e0::ea2c:9d32`

---

## 10. Final Verdict

**`OFFICE_MAC_TAILSCALE_ENROLLED_ISOLATED`**

