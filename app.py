#!/usr/bin/env python3
"""
IBM Storage Virtualize Replication Monitor - Web Frontend
Flask application that provides a browser-based UI to configure and run
the replication status check against IBM FlashSystem / Storage Virtualize arrays.
"""

import os
import json
import logging
from datetime import datetime, timezone
from flask import Flask, render_template, request, jsonify

import urllib3

try:
    import requests
    from requests.adapters import HTTPAdapter
    from requests.packages.urllib3.util.retry import Retry
except ImportError:
    raise SystemExit("Error: 'requests' library is required. Run: pip install requests flask colorama")

# Inline the core client logic so the web app is self-contained
from ibm_storage_replication_check import (
    IBMStorageVirtualizeClient,
    StatusAnalyzer,
    CapacityAuditor,
    DRMappingAnalyzer,
    Config,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.urandom(24)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

class WebConfig:
    """Lightweight config object built from a form POST (no env/file required)."""

    def __init__(self, form: dict):
        host = form.get("host", "").strip()
        port = form.get("port", "").strip()

        # Normalise the host — strip any scheme and any port the user may have
        # typed (e.g. pasting "https://192.168.1.1:7443" into the host field).
        # We always reconstruct the full URL ourselves so there is a single,
        # unambiguous code path for every input format.
        for prefix in ("https://", "http://"):
            if host.lower().startswith(prefix):
                host = host[len(prefix):]
                break
        # Remove any trailing :port that was embedded in the host string
        if ":" in host:
            host = host.rsplit(":", 1)[0]

        if port and port not in ("7443", ""):
            self.host = f"https://{host}:{port}"
        else:
            self.host = f"https://{host}:{port}" if port else f"https://{host}"

        self.host = self.host.rstrip("/")
        self.username = form.get("username", "").strip()
        self.password = form.get("password", "")
        self.token = form.get("token", "").strip() or None
        self.verify_ssl = form.get("verify_ssl", "true").lower() == "true"
        self.timeout = int(form.get("timeout", 30))
        self.log_level = "INFO"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/check", methods=["POST"])
def run_check():
    """Run replication check and return JSON results."""
    data = request.get_json(force=True)

    # Basic validation
    host = (data.get("host") or "").strip()
    if not host:
        return jsonify({"success": False, "error": "Host / IP address is required."}), 400

    use_token = bool((data.get("token") or "").strip())
    if not use_token:
        if not (data.get("username") or "").strip():
            return jsonify({"success": False, "error": "Username is required when not using a token."}), 400
        if not data.get("password"):
            return jsonify({"success": False, "error": "Password is required when not using a token."}), 400

    try:
        logger.info("POST /api/check payload: host=%s port=%s user=%s token_present=%s verify_ssl=%s",
                    data.get("host"), data.get("port"), data.get("username"),
                    bool((data.get("token") or "").strip()), data.get("verify_ssl"))

        cfg = WebConfig(data)
        logger.info("WebConfig resolved: host=%s verify_ssl=%s", cfg.host, cfg.verify_ssl)

        if not cfg.verify_ssl:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

        client = IBMStorageVirtualizeClient(cfg)

        # Authenticate
        if not client.authenticate():
            # Give a specific hint when the failure is SSL-related
            ssl_hint = (
                " The array uses a self-signed certificate — uncheck "
                "\"Verify SSL Certificate\" in Advanced Options and retry."
            ) if cfg.verify_ssl else ""
            return jsonify({"success": False, "error": f"Authentication failed. Check credentials and host.{ssl_hint}"}), 401

        # Fetch relationships and policies in sequence (same session)
        relationships = client.get_rc_relationships()
        policies = client.get_replication_policies()  # best-effort; may be None
        client.close()

        if relationships is None:
            return jsonify({"success": False, "error": "Failed to retrieve replication relationships from the array."}), 502

        summary = StatusAnalyzer.analyze_relationships(relationships, policies=policies or [])

        return jsonify({
            "success": True,
            "timestamp": datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
            "host": cfg.host,
            "summary": {
                "total": summary["total"],
                "normal": summary["normal"],
                "warning": summary["warning"],
                "error": summary["error"],
            },
            "relationships": [
                {
                    "name": d["name"],
                    "state": d["state"],
                    "category": d["category"],
                    "primary_vdisk": d["primary_vdisk"],
                    "secondary_vdisk": d["secondary_vdisk"],
                    "within_rpo": d.get("within_rpo", ""),
                    "policy": d.get("policy", "N/A"),
                    "rpo_delta": d.get("rpo_delta"),
                }
                for d in summary["details"]
            ],
        })

    except Exception as exc:
        logger.exception("Error during replication check")
        return jsonify({"success": False, "error": str(exc)}), 500


@app.route("/api/dr-mapping", methods=["POST"])
def dr_mapping_check():
    """
    Connect to the DR (target) array and verify that every volume in each
    replicated Volume Group has at least one host mapping.

    Expects a JSON body with:
      - Standard connection fields (host, port, username, password / token,
        verify_ssl, timeout) pointing at the DR array.
      - ``vg_names``: list[str]  — Volume Group names to check (from the
        source array's replication relationships, already known by the UI).
    """
    data = request.get_json(force=True)

    host = (data.get("host") or "").strip()
    if not host:
        return jsonify({"success": False, "error": "DR host / IP address is required."}), 400

    vg_names = data.get("vg_names") or []
    if not isinstance(vg_names, list) or not vg_names:
        return jsonify({"success": False, "error": "At least one Volume Group name is required."}), 400

    use_token = bool((data.get("token") or "").strip())
    if not use_token:
        if not (data.get("username") or "").strip():
            return jsonify({"success": False, "error": "Username is required when not using a token."}), 400
        if not data.get("password"):
            return jsonify({"success": False, "error": "Password is required when not using a token."}), 400

    try:
        cfg = WebConfig(data)
        logger.info(
            "DR mapping check: host=%s verify_ssl=%s user=%s token_present=%s",
            cfg.host, cfg.verify_ssl,
            cfg.username or "(none)",
            bool(cfg.token),
        )
        if not cfg.verify_ssl:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

        dr_client = IBMStorageVirtualizeClient(cfg)

        if not dr_client.authenticate():
            if cfg.verify_ssl:
                msg = (
                    "Authentication to DR array failed. If the array uses a "
                    "self-signed certificate, uncheck \"Verify SSL Certificate\" "
                    "and retry. Check the Flask console for the exact error."
                )
            else:
                msg = (
                    "Authentication to DR array failed. Verify the host/IP, port, "
                    "username, and password. Check the Flask console for details."
                )
            return jsonify({"success": False, "error": msg}), 401

        # Fetch the full vdisk→host mapping table once
        dr_host_map = dr_client.get_vdisk_host_map()
        if dr_host_map is None:
            dr_client.close()
            return jsonify({"success": False, "error": "Failed to retrieve vdisk host map from DR array."}), 502

        # Fetch volumes for each VG from the DR array
        dr_volumes: dict = {}
        for vg in vg_names:
            vols = dr_client.get_volume_group_volumes(vg)
            dr_volumes[vg] = vols if vols is not None else []

        dr_client.close()

        result = DRMappingAnalyzer.check_dr_host_mappings(
            vg_names=vg_names,
            dr_volumes=dr_volumes,
            dr_host_map=dr_host_map,
        )

        return jsonify({
            "success": True,
            "timestamp": datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
            "dr_host": cfg.host,
            "total_volumes": result["total_volumes"],
            "mapped_volumes": result["mapped_volumes"],
            "unmapped_volumes": result["unmapped_volumes"],
            "vg_results": result["vg_results"],
        })

    except Exception as exc:
        logger.exception("Error during DR mapping check")
        return jsonify({"success": False, "error": str(exc)}), 500


@app.route("/api/capacity-audit", methods=["POST"])
def capacity_audit():
    """
    Asymmetric Capacity & Pool Headroom Audit.

    Connects to BOTH the source and DR arrays, then:
      1. Fetches all vdisks + pools from each side.
      2. Computes per-volume capacity delta (source vs DR).
      3. Computes per-pool physical headroom on the DR side.

    Expects a JSON body with:
      source_*  fields  — connection details for the production array
      dr_*      fields  — connection details for the DR array
      vg_names          — list of VG names to scope the volume delta check
    """
    data = request.get_json(force=True)

    vg_names = data.get("vg_names") or []
    if not isinstance(vg_names, list) or not vg_names:
        return jsonify({"success": False, "error": "At least one Volume Group name is required."}), 400

    def _build_cfg(prefix):
        """Extract prefixed fields (source_ or dr_) into a WebConfig-compatible dict."""
        return {
            "host":       (data.get(f"{prefix}host") or "").strip(),
            "port":       (data.get(f"{prefix}port") or "7443").strip(),
            "username":   (data.get(f"{prefix}username") or "").strip(),
            "password":   data.get(f"{prefix}password") or "",
            "token":      (data.get(f"{prefix}token") or "").strip(),
            "verify_ssl": data.get(f"{prefix}verify_ssl", "false"),
            "timeout":    data.get("timeout", 30),
        }

    same_as_dr = bool(data.get("same_as_dr"))
    src_form   = _build_cfg("source_")
    # When same_as_dr is set, mirror source fields into dr_ so WebConfig
    # builds an identical host URL — no second auth round-trip needed.
    dr_form    = src_form if same_as_dr else _build_cfg("dr_")

    if not src_form["host"]:
        return jsonify({"success": False, "error": "Source array host is required."}), 400
    if not same_as_dr and not dr_form["host"]:
        return jsonify({"success": False, "error": "DR array host is required."}), 400

    try:
        src_cfg = WebConfig(src_form)
        dr_cfg  = src_cfg if same_as_dr else WebConfig(dr_form)

        for cfg in set([src_cfg, dr_cfg]):   # set() deduplicates when same object
            if not cfg.verify_ssl:
                urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

        logger.info("Capacity audit: source=%s dr=%s same_as_dr=%s vgs=%s",
                    src_cfg.host, dr_cfg.host, same_as_dr, vg_names)

        # ── Source array ──────────────────────────────────────────────────
        src_client = IBMStorageVirtualizeClient(src_cfg)
        if not src_client.authenticate():
            return jsonify({"success": False, "error": "Authentication to source array failed."}), 401

        src_vdisks_raw = src_client.get_all_vdisks()

        if src_vdisks_raw is None:
            src_client.close()
            return jsonify({"success": False, "error": "Failed to fetch vdisks from source array."}), 502

        # ── DR array (reuse source client when same_as_dr) ────────────────
        if same_as_dr:
            dr_client = src_client          # same session, no second auth
        else:
            dr_client = IBMStorageVirtualizeClient(dr_cfg)
            if not dr_client.authenticate():
                src_client.close()
                return jsonify({"success": False, "error": "Authentication to DR array failed."}), 401

        dr_vdisks_raw = dr_client.get_all_vdisks()
        dr_pools_raw  = dr_client.get_mdiskgrps()

        if dr_vdisks_raw is None:
            src_client.close()
            if not same_as_dr:
                dr_client.close()
            return jsonify({"success": False, "error": "Failed to fetch vdisks from DR array."}), 502
        if dr_pools_raw is None:
            src_client.close()
            if not same_as_dr:
                dr_client.close()
            return jsonify({"success": False, "error": "Failed to fetch pools from DR array."}), 502

        # ── Build VG→volumes map from the full vdisk list we already have ──
        vg_name_set = set(vg_names)
        dr_volumes_by_vg: dict = {vg: [] for vg in vg_names}
        for v in dr_vdisks_raw:
            vg = v.get('volume_group_name') or v.get('VG_name', '')
            if vg in vg_name_set:
                dr_volumes_by_vg[vg].append(v)

        # ── Enrich: fetch detail records to get real capacity values ───────
        # The IBM lsvdisk / lsmdiskgrp LIST response returns capacity=""
        # for VG-member volumes and all pools. Real values require a
        # per-record GET to /rest/v1/lsvdisk/<id> and /rest/v1/lsmdiskgrp/<id>.

        # Collect the vdisk IDs we need: VG members on DR + their source twins
        vg_member_names: set = set()
        for vols in dr_volumes_by_vg.values():
            for v in vols:
                vg_member_names.add(v.get('name', ''))

        # Enrich source vdisks for the VG members only
        src_idx_raw = CapacityAuditor.build_vdisk_index(src_vdisks_raw)
        for vname in vg_member_names:
            src_rec = src_idx_raw.get(vname)
            if src_rec and not src_rec.get('capacity'):
                vid = src_rec.get('id') or vname
                detail = src_client.get_vdisk_detail(vid)
                if detail:
                    src_idx_raw[vname] = detail

        # Enrich DR vdisks for the same VG members
        dr_idx_raw = CapacityAuditor.build_vdisk_index(dr_vdisks_raw)
        for vname in vg_member_names:
            dr_rec = dr_idx_raw.get(vname)
            if dr_rec and not dr_rec.get('capacity'):
                vid = dr_rec.get('id') or vname
                detail = dr_client.get_vdisk_detail(vid)
                if detail:
                    dr_idx_raw[vname] = detail

        # Enrich DR pools — only pools that contain VG-member volumes
        relevant_pools: set = {
            dr_idx_raw[n].get('mdisk_grp_name', '')
            for n in vg_member_names if n in dr_idx_raw
        }
        pool_idx_raw = CapacityAuditor.build_pool_index(dr_pools_raw)
        for pname in relevant_pools:
            prec = pool_idx_raw.get(pname)
            if prec and not prec.get('capacity'):
                pid = prec.get('id') or pname
                detail = dr_client.get_mdiskgrp_detail(pid)
                if detail:
                    pool_idx_raw[pname] = detail

        logger.info("Enrichment complete: %d vdisk(s), %d pool(s)",
                    len(vg_member_names), len(relevant_pools))

        # Close clients now that all API calls are done
        src_client.close()
        if not same_as_dr:
            dr_client.close()

        # ── Analysis ──────────────────────────────────────────────────────
        src_idx  = src_idx_raw
        dr_idx   = dr_idx_raw
        pool_idx = pool_idx_raw

        vol_deltas    = CapacityAuditor.audit_volume_capacity_delta(
            vg_names, src_idx, dr_idx, dr_volumes_by_vg
        )
        pool_headroom = CapacityAuditor.audit_pool_headroom(dr_idx, pool_idx)

        mismatch_count   = sum(1 for v in vol_deltas    if v["status"] != "ok")
        pool_alert_count = sum(1 for p in pool_headroom if p["status"] in ("warning", "critical"))

        return jsonify({
            "success":          True,
            "timestamp":        datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
            "source_host":      src_cfg.host,
            "dr_host":          dr_cfg.host,
            "same_as_dr":       same_as_dr,
            "mismatch_count":   mismatch_count,
            "pool_alert_count": pool_alert_count,
            "volume_deltas":    vol_deltas,
            "pool_headroom":    pool_headroom,
        })

    except Exception as exc:
        logger.exception("Error during capacity audit")
        return jsonify({"success": False, "error": str(exc)}), 500


@app.route("/api/save-config", methods=["POST"])
def save_config():
    """Save connection settings to config.json (password excluded)."""
    data = request.get_json(force=True)
    config_out = {
        "host": (data.get("host") or "").strip(),
        "port": (data.get("port") or "7443").strip(),
        "username": (data.get("username") or "").strip(),
        "verify_ssl": data.get("verify_ssl", True),
        "timeout": int(data.get("timeout", 30)),
        "log_level": "INFO",
    }
    try:
        with open("config.json", "w") as f:
            json.dump(config_out, f, indent=2)
        return jsonify({"success": True, "message": "config.json saved (password not stored)."})
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)}), 500


@app.route("/api/load-config")
def load_config():
    """Load saved connection settings from config.json."""
    try:
        with open("config.json") as f:
            cfg = json.load(f)
        # Strip any stored password for safety
        cfg.pop("password", None)
        return jsonify({"success": True, "config": cfg})
    except FileNotFoundError:
        return jsonify({"success": False, "error": "No saved config found."})
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)}), 500


if __name__ == "__main__":
    print("\n  IBM Storage Virtualize Replication Monitor")
    print("  Open your browser at: http://127.0.0.1:5000\n")
    app.run(debug=True, host="0.0.0.0", port=5000)
