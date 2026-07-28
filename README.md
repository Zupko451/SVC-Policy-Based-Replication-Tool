# IBM Storage Virtualize — Replication Monitor

A web-based operations dashboard for IBM FlashSystem / SVC. Connects to the IBM Storage Virtualize REST API and provides proactive, sliding-scale visibility into replication health, RPO lag, DR host mappings, and pool capacity — filling the gaps that the native management GUI leaves behind.

---

## Features

### Replication Status
- **Live replication table** — all volume groups, link status, production/recovery sites, Within-RPO flag, and policy name
- **Summary stat cards** — total / normal / warning / error counts with an overall health percentage bar
- **Auto-refresh** — configurable polling at 30 s / 1 min / 5 min with a live countdown and progress bar
- **Filter by status** — show All / Normal / Warning / Error with a single click
- **Export JSON** — download the last result for reporting or automation

### RPO Delta — Lag vs Policy Threshold
- Queries `lsvolumegroupreplication` **and** `lsreplicationpolicy` to calculate the exact lag in seconds between the current recovery point and the policy threshold
- Four colour-coded health levels per volume group — shown as fill-gauge cards:

  | Level | Trigger |
  |-------|---------|
  | 🟢 Healthy | Lag < 50 % of threshold |
  | 🟡 Caution | Lag 50–79 % of threshold |
  | 🟠 Warning | Lag 80–99 % of threshold |
  | 🔴 Critical | Lag ≥ threshold — SLA breached |

- Catches creeping WAN congestion **before** the native GUI alert fires

### DR Host Mapping Validation
- Connects independently to the **DR / target array** using its own credentials
- Calls `lshostvdiskmap` to retrieve the full host→vdisk mapping table in one query
- Cross-references every volume in every replicated Volume Group against that table
- Flags volumes that are online and replicating but **not mapped to any host** — invisible to the GUI, fatal during failover
- Per-VG accordion rows auto-expand on any unmapped volume; shows which host(s) each mapped volume is presented to

### Capacity & Pool Headroom Audit
- Connects to **both** source and DR arrays (or reuse source credentials with the **Same as Source** toggle)
- **Volume capacity delta** — compares provisioned size (GiB) of every DR volume against its source counterpart; flags mismatches and missing volumes
- **Pool headroom cards** — checks `physical_free_capacity` on every DR pool; colour-coded gauges ranked by risk:

  | Level | Trigger |
  |-------|---------|
  | ✓ Healthy | ≥ 25 % physical free |
  | ⚠ Low Headroom | 10–24 % physical free |
  | ✗ Critical | < 10 % physical free |

- Thin-provisioned target volumes go offline silently when pool physical space runs out — this audit catches it ahead of time

### General
- **Flexible auth** — username/password or API token on all forms
- **Self-signed cert support** — per-form SSL toggle with warning banner
- **Save/load config** — persist connection settings to `config.json` (password never stored)
- **CLI mode** — run headless with JSON output for Nagios, Prometheus, cron, etc.

---

## Requirements

- Python 3.8+
- Network access to your FlashSystem / SVC management IP on port **7443** (default) — editable in all forms

---

## Quick Start

```bash
# 1. Navigate to the project folder
cd "SVC-replica"

# 2. Create a virtual environment (first time only)
python3 -m venv .venv

# 3. Install dependencies (first time only)
.venv/bin/pip install -r requirements.txt

# 4. Start the web UI
.venv/bin/python app.py
```

Open **http://127.0.0.1:5000** in your browser.

## Windows:

Step	        README       Windows
Create venv	               python -m venv .venv
Install deps		           .venv\Scripts\pip install -r requirements.txt
Run App                    .venv\Scripts\python app.py

Open **http://127.0.0.1:5000** in your browser.
---

## Web UI — Workflow

### Step 1: Replication Check
1. Enter the **Host / IP** of your production FlashSystem management interface
2. Set the **port** — default is `7443`
3. Enter **Username / Password** or switch to the **API Token** tab
4. If your array uses a **self-signed certificate**, open *Advanced Options* and **uncheck** Verify SSL Certificate
5. Click **Run Check**
6. Optionally select an auto-refresh interval and click **▶ Start**

The results panel appears with the replication table and, if policy data is available, the **RPO Delta** gauge cards below it.

> **Tip:** Click **Save Config** to persist host/port/username. **Load Config** restores them on next visit.

### Step 2: DR Host Mapping Validation *(appears after first run)*
1. Enter the **DR array** host, port, and credentials (independent of the production array)
2. Supports **Username/Password** or **API Token** via tabs — same as the main form
3. Click **Validate DR Mappings**

Results show per-VG accordion rows. Any VG with unmapped volumes auto-expands. Each volume row shows mapped host names or a **NOT MAPPED** warning.

### Step 3: Capacity & Pool Headroom Audit *(appears after first run)*
1. Source array fields are **pre-filled** from the main Connection Settings form
2. For the DR array: enter separate credentials, or toggle **Same as Source** to reuse the source connection without re-authenticating
3. Click **Run Capacity Audit**

Results show:
- **DR Pool Headroom cards** — one per pool, sorted critical-first
- **Volume Capacity Delta table** — source vs DR GiB, with delta column and status

---

## Connection Settings Reference

| Field | Description |
|-------|-------------|
| Host / IP | Management IP or FQDN — no scheme needed, added automatically |
| Port | Default `7443`; enter `443` if your firmware uses the HTTPS default port |
| Username | Local or LDAP account with at least *Monitor* role |
| API Token | Alternative to username/password — generate via `POST /rest/v1/auth` |
| Verify SSL | Uncheck for self-signed certificates (typical on FlashSystem) |
| Timeout | Request timeout in seconds (default 30) |

---

## Replication States

Source: `link1_status` field from `/rest/v1/lsvolumegroupreplication`.

| Status | Category | Meaning |
|--------|----------|---------|
| `running` | ✅ Normal | Replication active and healthy |
| `degraded` | ⚠ Warning | Replication degraded but continuing |
| `syncing` | ⚠ Warning | Re-synchronising after gap |
| `waiting_for_sync` | ⚠ Warning | Waiting to begin sync |
| `stopped` | ✗ Error | Replication stopped |
| `disconnected` | ✗ Error | Link to remote system lost |
| `error` / `failed` | ✗ Error | Hard failure |

Any unrecognised state is treated as Warning.

---

## CLI Usage

```bash
# Using environment variables
export IBM_SV_HOST="https://192.168.0.1:7443"
export IBM_SV_USER="admin"
export IBM_SV_PASSWORD="password"
export IBM_SV_VERIFY_SSL="false"    # for self-signed certs
python ibm_storage_replication_check.py

# Using a config file
python ibm_storage_replication_check.py --config config.json

# JSON output (for automation / Nagios / Prometheus)
python ibm_storage_replication_check.py --output json > report.json

# Disable SSL verification
python ibm_storage_replication_check.py --no-verify-ssl

# Verbose logging
python ibm_storage_replication_check.py --verbose
```

### Exit Codes

| Code | Meaning |
|------|---------|
| `0` | All relationships normal |
| `1` | Warnings detected (or config/connection error) |
| `2` | Errors detected |
| `130` | Interrupted (Ctrl+C) |

---

## REST API Endpoints Used

| Feature | Method | Endpoint | Notes |
|---------|--------|----------|-------|
| Authenticate | `POST` | `/rest/v1/auth` | Returns `X-Auth-Token` |
| Replication status | `POST` | `/rest/v1/lsvolumegroupreplication` | Main replication table |
| Replication policies | `POST` | `/rest/v1/lsreplicationpolicy` | RPO thresholds |
| Host→vdisk mappings | `POST` | `/rest/v1/lshostvdiskmap` | DR mapping validation |
| All vdisks | `POST` | `/rest/v1/lsvdisk` | Capacity delta (source & DR) |
| Vdisks by VG | `POST` | `/rest/v1/lsvdisk` + `filtervalue` body | Scoped volume list |
| Storage pools | `POST` | `/rest/v1/lsmdiskgrp` | Pool headroom audit |

> **Auth note:** IBM Storage Virtualize requires credentials as `X-Auth-Username` / `X-Auth-Password` request headers — not HTTP Basic Auth. All subsequent requests carry `X-Auth-Token`.
>
> **Method note:** This firmware uses `POST` for all `ls*` read commands. `GET` returns `405 Method Not Allowed`.

---

## Configuration File

Copy `config.json.example` to `config.json` and edit:

```json
{
  "host": "192.168.0.1",
  "port": "7443",
  "username": "admin",
  "verify_ssl": false,
  "timeout": 30
}
```

> Passwords are never written to `config.json`. Enter them each session via the web UI or environment variable.

---

## Code Architecture

```
.
├── app.py                            # Flask web server + REST API routes
├── ibm_storage_replication_check.py  # Core classes + CLI entry point
├── templates/
│   └── index.html                    # Single-page browser dashboard
├── requirements.txt
├── config.json.example               # Template — copy to config.json
└── README_IBM_Storage_Monitor.md
```

### Class Summary (`ibm_storage_replication_check.py`)

| Class | Responsibility |
|-------|---------------|
| `Config` | Load connection settings from env vars or JSON file |
| `IBMStorageVirtualizeClient` | Authenticated REST API client — session management, retry logic, all `ls*` queries |
| `StatusAnalyzer` | Categorise replication states; calculate RPO lag delta vs policy threshold |
| `CapacityAuditor` | Volume capacity delta (source vs DR) and pool physical headroom analysis |
| `DRMappingAnalyzer` | Cross-reference VG volumes against the DR host mapping table |
| `OutputFormatter` | Coloured console output for CLI mode |

### Flask Routes (`app.py`)

| Route | Method | Description |
|-------|--------|-------------|
| `/` | `GET` | Serve the dashboard |
| `/api/check` | `POST` | Run replication check + RPO delta on source array |
| `/api/dr-mapping` | `POST` | DR host mapping validation against target array |
| `/api/capacity-audit` | `POST` | Capacity delta + pool headroom across source and DR |
| `/api/save-config` | `POST` | Persist connection settings to `config.json` |
| `/api/load-config` | `GET` | Load saved settings from `config.json` |

---

## Troubleshooting

**`SSL: CERTIFICATE_VERIFY_FAILED`**
Uncheck *Verify SSL Certificate* in the relevant form (or use `--no-verify-ssl` in CLI). FlashSystem arrays typically ship with self-signed certificates.

**`Authentication failed` on DR or Capacity forms**
Each widget has its own independent credentials. Check the Flask console — the exact HTTP status code and response body are logged. Common causes: wrong port (try `7443`), self-signed cert (uncheck SSL verify), or wrong credentials.

**`CMMVC5707E Required parameters are missing` (409)**
An `ls*` endpoint was called without the expected POST body. All query methods now send at minimum `{}` — if you see this in new code, ensure `json={}` is passed to `_make_request`.

**`405 Method Not Allowed`**
IBM SV REST API requires `POST` for all `ls*` query commands on this firmware. Do not use `GET`.

**`host=https://https://...` in logs**
The host field contained a full URL (with scheme). `WebConfig` now strips any leading `http://` or `https://` and any embedded port before reconstructing the URL.

**Results panel shows 0 relationships**
Ensure your account has at least *Monitor* role and that Metro Mirror / Global Mirror replication is configured on the array.

**RPO Delta panel does not appear**
The array did not return a `running_recovery_point` timestamp on any volume group. This field is only populated when the replication relationship is in `running` state.

**DR mapping shows all volumes unmapped**
Verify you connected to the correct DR array IP and that the target volumes exist. Also confirm the account has *Monitor* role on the DR system.

**Capacity audit shows all volumes missing**
The Volume Group names from the source array must match volume group names on the DR array. If the DR array uses different VG naming, the per-VG filter will return empty lists.

---

## Security Notes

- Never commit `config.json` with real credentials — it is already listed in `.gitignore`
- Restrict file permissions: `chmod 600 config.json`
- Use a dedicated read-only service account with *Monitor* role on both source and DR arrays
- Passwords entered in the web UI are transmitted to the local Flask server only — never logged or persisted
- Enable SSL verification in production once a valid certificate is installed on the array

---

## Version History

| Version | Date | Notes |
|---------|------|-------|
| 2.0.0 | 2026-07-23 | DR Host Mapping Validation, RPO Delta gauges, Capacity & Pool Headroom Audit, Same-as-Source toggle, default port 7443, host URL normalisation |
| 1.2.0 | 2026-06-19 | Auto-refresh (30s/1m/5m), progress bar, pause button |
| 1.1.0 | 2026-06-19 | Web UI; fixed auth headers, SSL toggle, correct endpoint |
| 1.0.0 | 2026-06-15 | Initial CLI release |

---

*Made with IBM Bob*
