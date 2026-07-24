# Partnership & Link Health — Feature Plan

## Overview

Add an **opt-in** Partnership & Link Performance section to `ibm_storage_replication_check.py`,
controlled by a `--partnerships` CLI flag. The default run produces zero new output — existing
users see nothing different. When the flag is supplied, a new section is appended after the
existing REPLICATION STATUS SUMMARY block showing:

1. Per-partnership aggregate health (state, link type, bandwidth ceiling vs. live throughput)
2. IP portset path redundancy (per-port online/offline status, alert if any path goes offline
   before the entire partnership fails)
3. Inter-cluster latency / dropped frames sourced from port IP stats

All three sub-features follow the same "best-effort" pattern already established in
`CapacityAuditor` and `DRMappingAnalyzer` — data is fetched, parsed, and rendered only when
available; missing or unsupported endpoints are silently skipped with a single warning line.

---

## Sub-Tasks

---

### Sub-Task 1 — Add API Client Methods

**Intent**
Add three new fetch methods to `IBMStorageVirtualizeClient` for the endpoints needed by
partnership analysis. This keeps all API surface area in one class (the existing pattern).

**Expected Outcomes**
- `get_partnerships()` calls `lspartnership` and returns the full list
- `get_portip()` calls `lsportip` and returns the full list of IP port records
- `get_node_port_stats()` calls `lsnodeportstat` and returns records, or `None` if the
  endpoint returns a 404 / 405 (graceful degradation for older firmware)
- Three new endpoint constants added alongside existing ones at lines 126–133

**Todo List**
1. Add constants `PARTNERSHIP_ENDPOINT`, `PORTIP_ENDPOINT`, `NODE_PORT_STAT_ENDPOINT`
   to `IBMStorageVirtualizeClient`
2. Add `get_partnerships() -> Optional[List[Dict]]` — POST to `lspartnership`, same
   structure as `get_rc_relationships()`
3. Add `get_portip() -> Optional[List[Dict]]` — POST to `lsportip`
4. Add `get_node_port_stats() -> Optional[List[Dict]]` — POST to `lsnodeportstat`;
   return `None` (not an error) if the API returns 404 or 405 so callers can treat it
   as "unavailable"

**Relevant Context**
- Existing fetch methods: `get_rc_relationships()` (line 326), `get_mdiskgrps()` (line 442)
- `_make_request()` pattern at line 251 — returns `None` on any non-200/204 status;
  sub-task 4 needs a variant or a `None` return from `get_node_port_stats()` to be
  treated as "feature unavailable" rather than an error

**Status** — `[x] done`

---

### Sub-Task 2 — PartnershipAnalyzer Class

**Intent**
Create a new `PartnershipAnalyzer` static class (parallel to `StatusAnalyzer`) that ingests
the raw API data from sub-task 1 and produces structured result dicts for the output layer.
No printing happens here — analysis only.

**Expected Outcomes**
- `analyze_partnerships(partnerships, portip_records, node_port_stats)` returns a single
  dict with:
  - `total` / `healthy` / `degraded` / `offline` counts
  - `details` list — one entry per partnership containing:
    - `name`, `state`, `type` (FC/IP), `bandwidth_mbps` (configured ceiling)
    - `throughput_mbps` (live, from node port stats — `None` if unavailable)
    - `ports` list — one entry per IP port with `name`, `status`, `ip`, `partner_node`
    - `redundancy_status`: `'ok'` if all ports online, `'degraded'` if ≥1 port offline
      but partnership still connected, `'unknown'` if port data unavailable
    - `dropped_frames` and `rtt_ms` sourced from `lsnodeportstat` fields where present

**Todo List**
1. Add `PartnershipAnalyzer` class below `DRMappingAnalyzer` (keeping file ordering consistent)
2. Add `_parse_bandwidth(raw)` helper — converts IBM's MB/s string to float
3. Add `_match_ports_to_partnership(partnership, portip_records)` helper — links ports by
   `partner_node_name` or `portset_name` field
4. Add `_extract_throughput(port_names, node_port_stats)` helper — sums throughput MB/s
   across the matched ports; returns `None` if `node_port_stats` is `None`
5. Add main `analyze_partnerships()` static method assembling the above into the result dict

**Relevant Context**
- `StatusAnalyzer.analyze_relationships()` at line 580 — follow the same
  input→summary-dict output pattern
- `CapacityAuditor._to_gib()` at line 677 — example of safe numeric conversion from IBM
  string fields
- IBM `lspartnership` fields of interest: `name`, `status`, `type`, `bandwidth`,
  `partnersystem_name`
- IBM `lsportip` fields: `id`, `node_name`, `IP_address`, `state`, `partner_node_name`
- IBM `lsnodeportstat` fields: `node_name`, `port_id`, `bytes_sent`, `bytes_received`,
  `frame_errors` (if present)

**Status** — `[x] done`

---

### Sub-Task 3 — OutputFormatter Methods for Partnership Section

**Intent**
Add rendering methods to `OutputFormatter` that print partnership results in the same visual
style as the existing relationship output (symbol + color, 2-space-indented detail lines).
Keeps all display logic in `OutputFormatter` per existing convention.

**Expected Outcomes**
- `print_partnership_detail(detail)` renders one partnership entry using the existing
  `✓ / ⚠ / ✗` + color pattern
- `print_partnership_summary(result)` renders the aggregate counts header and health tile
  (mirrors `print_summary()`)
- Bandwidth utilisation displayed as `used X MB/s / ceiling Y MB/s (Z%)` when throughput
  is available; `ceiling Y MB/s (throughput unavailable)` when not
- Per-port redundancy lines indented under the partnership with `  ↳ port: status`
- If all ports are online, ports are collapsed to a single `  ✓ All N paths online` line
  to keep output compact

**Todo List**
1. Add `print_partnership_detail(detail: Dict)` to `OutputFormatter`
2. Add `print_partnership_summary(result: Dict)` to `OutputFormatter`
3. Ensure graceful handling when `detail['throughput_mbps']` is `None` (sub-task 1 note)
4. Ensure graceful handling when `detail['ports']` is empty or `redundancy_status` is
   `'unknown'`

**Relevant Context**
- `print_relationship()` at line 992 — exact indentation and color pattern to match
- `print_summary()` at line 1027 — exact summary block structure to match
- "Compact healthy / verbose unhealthy" approach: only expand port list when
  `redundancy_status != 'ok'`, to avoid cluttering output when everything is healthy

**Status** — `[x] done`

---

### Sub-Task 4 — CLI Flag and main() Wiring

**Intent**
Wire the new flag, API calls, analyzer, and output methods into `main()` with zero impact
on the existing default run. The partnerships section slots in after the REPLICATION STATUS
SUMMARY block and before the ATTENTION REQUIRED block.

**Expected Outcomes**
- `--partnerships` flag added to `parse_arguments()`
- When flag is absent: `main()` is byte-for-byte identical in behaviour to today
- When flag is present:
  - `get_partnerships()`, `get_portip()`, `get_node_port_stats()` called after existing data fetch
  - `PartnershipAnalyzer.analyze_partnerships()` called with results
  - Console: `print_partnership_summary()` + `print_partnership_detail()` per entry rendered
  - JSON: `partnerships` key added to `output_data` with the analyzer result dict
- Exit code logic unchanged (partnership degraded/offline states do not change the exit code
  in this first pass — replication status remains the sole exit code driver)

**Todo List**
1. Add `--partnerships` flag to `parse_arguments()` as `action='store_true'`
2. In `main()`, after the existing `relationships` fetch block, add a guarded block:
   `if args.partnerships: fetch partnerships + portip + node_port_stats`
3. In console output path, after `print_summary(summary)`, add:
   `if args.partnerships and partnership_result: print_partnership_summary + detail loop`
4. In JSON output path, add `'partnerships': partnership_result` to `output_data` when flag set
5. Ensure `client.close()` still runs on all paths (no new early-return paths introduced)

**Relevant Context**
- `parse_arguments()` at line 1067 — add flag alongside existing `--output`, `--verbose`
- `main()` output branch at line 1163 — both JSON and console paths need the new block
- `CapacityAuditor` and `DRMappingAnalyzer` serve as the reference pattern for "built but
  wired only when called" — the same strategy is used here

**Status** — `[x] done`

---

## Non-Goals

- No changes to default (no-flag) behaviour
- No dashboard or persistent storage of metrics — single-run point-in-time snapshot
- No exit code changes based on partnership status in this pass
- No modifications to `CapacityAuditor` or `DRMappingAnalyzer` (they remain unwired)
