#!/usr/bin/env python3
"""
IBM Storage Virtualize Replication Status Monitor

This script authenticates to the IBM Storage Virtualize REST API and checks
the replication status of all volume groups, highlighting any groups not in
a running state as warnings.

Author: Bob
Date: 2026-06-15
"""

import os
import sys
import json
import logging
import argparse
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timezone
import urllib3

try:
    import requests
    from requests.adapters import HTTPAdapter
    from requests.packages.urllib3.util.retry import Retry
except ImportError:
    print("Error: 'requests' library is required. Install it with: pip install requests")
    sys.exit(1)

try:
    from colorama import init, Fore, Style
    init(autoreset=True)
    COLORS_AVAILABLE = True
except ImportError:
    COLORS_AVAILABLE = False
    print("Warning: 'colorama' not installed. Install for colored output: pip install colorama")


# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class Config:
    """Configuration manager for IBM Storage Virtualize connection."""
    
    def __init__(self, config_file: Optional[str] = None):
        """
        Initialize configuration from environment variables or config file.
        
        Args:
            config_file: Optional path to JSON configuration file
        """
        self.host = None
        self.username = None
        self.password = None
        self.token = None
        self.verify_ssl = True
        self.timeout = 30
        self.log_level = "INFO"
        
        if config_file:
            self._load_from_file(config_file)
        else:
            self._load_from_env()
        
        self._validate()
    
    def _load_from_env(self):
        """Load configuration from environment variables."""
        self.host = os.getenv('IBM_SV_HOST')
        self.username = os.getenv('IBM_SV_USER')
        self.password = os.getenv('IBM_SV_PASSWORD')
        self.token = os.getenv('IBM_SV_TOKEN')
        self.verify_ssl = os.getenv('IBM_SV_VERIFY_SSL', 'true').lower() == 'true'
        self.timeout = int(os.getenv('IBM_SV_TIMEOUT', '30'))
        self.log_level = os.getenv('IBM_SV_LOG_LEVEL', 'INFO')
    
    def _load_from_file(self, config_file: str):
        """Load configuration from JSON file."""
        try:
            with open(config_file, 'r') as f:
                config_data = json.load(f)
            
            self.host = config_data.get('host')
            self.username = config_data.get('username')
            self.password = config_data.get('password')
            self.token = config_data.get('token')
            self.verify_ssl = config_data.get('verify_ssl', True)
            self.timeout = config_data.get('timeout', 30)
            self.log_level = config_data.get('log_level', 'INFO')
            
            logger.info(f"Configuration loaded from {config_file}")
        except FileNotFoundError:
            logger.error(f"Configuration file not found: {config_file}")
            raise
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON in configuration file: {e}")
            raise
    
    def _validate(self):
        """Validate required configuration parameters."""
        if not self.host:
            raise ValueError("IBM Storage Virtualize host is required (IBM_SV_HOST or config file)")
        
        # Ensure host has protocol
        if not self.host.startswith(('http://', 'https://')):
            self.host = f"https://{self.host}"
        
        # Remove trailing slash
        self.host = self.host.rstrip('/')
        
        # Either username/password or token must be provided
        if not self.token and not (self.username and self.password):
            raise ValueError("Either token or username/password must be provided")
        
        logger.info(f"Configuration validated for host: {self.host}")


class IBMStorageVirtualizeClient:
    """Client for IBM Storage Virtualize REST API."""
    
    # API endpoints
    AUTH_ENDPOINT = "/rest/v1/auth"
    RC_VOLGROUP_ENDPOINT = "/rest/v1/lsvolumegroupreplication"
    REPLICATION_POLICY_ENDPOINT = "/rest/v1/lsreplicationpolicy"
    VDISK_HOST_MAP_ENDPOINT = "/rest/v1/lshostvdiskmap"
    VOLUME_GROUP_ENDPOINT = "/rest/v1/lsvolumegroup"
    VDISK_ENDPOINT = "/rest/v1/lsvdisk"
    MDISKGRP_ENDPOINT = "/rest/v1/lsmdiskgrp"
    PARTNERSHIP_ENDPOINT = "/rest/v1/lspartnership"
    PORTIP_ENDPOINT = "/rest/v1/lsportip"
    NODE_PORT_STAT_ENDPOINT = "/rest/v1/lsnodeportstat"

    # link1_status values returned by lsvolumegroupreplication
    NORMAL_STATES = {
        'running',
    }

    WARNING_STATES = {
        'degraded',
        'syncing',
        'waiting_for_sync',
    }

    ERROR_STATES = {
        'stopped',
        'disconnected',
        'error',
        'failed',
    }
    
    def __init__(self, config: Config):
        """
        Initialize IBM Storage Virtualize API client.
        
        Args:
            config: Configuration object
        """
        self.config = config
        self.auth_token = config.token
        self.base_url = config.host

        # Disable SSL warnings if verification is disabled
        if not config.verify_ssl:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            logger.warning("SSL verification is disabled")

        self.session = self._create_session()

    def _create_session(self) -> requests.Session:
        """Create requests session with retry logic."""
        session = requests.Session()

        # Set verify at the session level so every request — including retries
        # and any followed redirects — inherits the correct SSL behaviour.
        session.verify = self.config.verify_ssl

        # Configure retry strategy — only retry on HTTP status codes, never on
        # connection/SSL failures (connect=0) which are not transient.
        retry_strategy = Retry(
            total=3,
            connect=0,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["HEAD", "GET", "OPTIONS", "POST"],
        )
        
        adapter = HTTPAdapter(max_retries=retry_strategy)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        
        return session
    
    def authenticate(self) -> bool:
        """
        Authenticate to IBM Storage Virtualize API.
        
        Returns:
            True if authentication successful, False otherwise
        """
        if self.auth_token:
            logger.info("Using provided authentication token")
            return True
        
        try:
            url = f"{self.base_url}{self.AUTH_ENDPOINT}"

            # IBM Storage Virtualize expects credentials as X-Auth-* headers,
            # not HTTP Basic Auth — sending Basic Auth causes a 403
            # "Invalid Username Header" response.
            response = self.session.post(
                url,
                headers={
                    "X-Auth-Username": self.config.username,
                    "X-Auth-Password": self.config.password,
                },
                verify=self.config.verify_ssl,
                timeout=self.config.timeout
            )
            
            if response.status_code == 200:
                # Extract token from response
                auth_data = response.json()
                self.auth_token = auth_data.get('token')
                
                if self.auth_token:
                    logger.info("Authentication successful")
                    return True
                else:
                    logger.error("No token received in authentication response")
                    return False
            else:
                logger.error(
                    "Authentication failed: %s - %s",
                    response.status_code,
                    response.text[:400],   # cap to avoid flooding logs
                )
                return False

        except requests.exceptions.ConnectionError as e:
            logger.error("Authentication connection error (wrong host/port or network): %s", e)
            return False
        except requests.exceptions.SSLError as e:
            logger.error("Authentication SSL error (try disabling SSL verification): %s", e)
            return False
        except requests.exceptions.RequestException as e:
            logger.error("Authentication request failed: %s", e)
            return False
    
    def _make_request(self, endpoint: str, method: str = "GET", **kwargs) -> Optional[Dict]:
        """
        Make authenticated request to API.
        
        Args:
            endpoint: API endpoint path
            method: HTTP method (GET, POST, etc.)
            **kwargs: Additional arguments for requests
            
        Returns:
            Response data as dictionary or None on failure
        """
        url = f"{self.base_url}{endpoint}"
        
        headers = kwargs.pop('headers', {})
        headers['X-Auth-Token'] = self.auth_token
        
        try:
            response = self.session.request(
                method,
                url,
                headers=headers,
                verify=self.config.verify_ssl,
                timeout=self.config.timeout,
                **kwargs
            )
            
            logger.debug("API %s %s -> %s", method, url, response.status_code)
            if response.status_code == 200:
                return response.json()
            elif response.status_code == 204:
                return []  # no content — valid empty response
            elif response.status_code == 401:
                logger.error("Authentication token expired or invalid")
                return None
            else:
                logger.error(f"API request failed: {response.status_code} - {response.text}")
                return None
                
        except requests.exceptions.RequestException as e:
            logger.error(f"API request failed: {e}")
            return None
    
    def get_volume_groups(self) -> Optional[List[Dict]]:
        """
        Get all volume groups.
        
        Returns:
            List of volume group dictionaries or None on failure
        """
        logger.info("Fetching volume groups...")
        data = self._make_request(self.VOLUME_GROUPS_ENDPOINT, method="POST")

        if data is None:
            return None  # request failed

        volume_groups = data if isinstance(data, list) else data.get('volumegroups', [])
        logger.info(f"Retrieved {len(volume_groups)} volume groups")
        return volume_groups
    
    def get_replication_policies(self) -> Optional[List[Dict]]:
        """
        Get all replication policies (lsreplicationpolicy).

        Returns:
            List of policy dictionaries or None on failure
        """
        logger.info("Fetching replication policies...")
        data = self._make_request(self.REPLICATION_POLICY_ENDPOINT, method="POST")
        if data is None:
            return None
        policies = data if isinstance(data, list) else []
        logger.info("Retrieved %d replication policies", len(policies))
        return policies

    def get_rc_relationships(self) -> Optional[List[Dict]]:
        """
        Get all remote copy (replication) relationships.
        
        Returns:
            List of RC relationship dictionaries or None on failure
        """
        logger.info("Fetching remote copy relationships...")

        data = self._make_request(self.RC_VOLGROUP_ENDPOINT, method="POST")
        if data is None:
            return None

        relationships = data if isinstance(data, list) else []
        logger.info("Retrieved %d RC relationships", len(relationships))
        return relationships
    
    def get_vdisk_host_map(self) -> Optional[List[Dict]]:
        """
        Fetch the full host→vdisk mapping table from the DR array.
        Equivalent to CLI: svcinfo lshostvdiskmap

        lshostvdiskmap (not lsvdiskhostmap) is used because it accepts a
        POST with no required parameters and returns a flat list of all
        mappings across every host. lsvdiskhostmap requires a specific vdisk
        name argument and cannot return the full table in one call.

        Records are normalised to always contain ``vdisk_name`` and
        ``host_name`` keys so the DRMappingAnalyzer needs no changes.
        lshostvdiskmap returns ``name`` for the vdisk — we remap it here.

        Returns:
            List of normalised mapping records with ``vdisk_name`` and
            ``host_name`` keys. Empty list when nothing is mapped.
            None on request failure.
        """
        logger.info("Fetching host-vdisk map (lshostvdiskmap)...")
        data = self._make_request(self.VDISK_HOST_MAP_ENDPOINT, method="POST", json={})
        if data is None:
            return None
        raw = data if isinstance(data, list) else []
        # Normalise: lshostvdiskmap uses 'name' for the vdisk; remap to 'vdisk_name'
        mappings = []
        for r in raw:
            mappings.append({
                'vdisk_name': r.get('name') or r.get('vdisk_name', ''),
                'host_name':  r.get('host_name', ''),
                # preserve all original fields for debugging
                **{k: v for k, v in r.items() if k not in ('name',)},
            })
        logger.info("Retrieved %d host-vdisk map entries", len(mappings))
        return mappings

    def get_volume_group_volumes(self, vg_name: str) -> Optional[List[Dict]]:
        """
        Fetch the list of volumes (vdisks) that belong to a specific
        volume group.
        Equivalent to CLI: svcinfo lsvdisk -filtervalue volume_group_name=<vg>

        Filter is passed as a JSON body field in the POST request,
        consistent with how other lsXXX endpoints work on this firmware.

        Returns:
            List of vdisk summary dicts (name, id, volume_group_name, …)
            or None on failure.
        """
        logger.info("Fetching volumes for volume group: %s", vg_name)
        data = self._make_request(
            self.VDISK_ENDPOINT,
            method="POST",
            json={"filtervalue": f"volume_group_name={vg_name}"},
        )
        if data is None:
            return None
        volumes = data if isinstance(data, list) else []
        logger.info("Retrieved %d volumes for VG %s", len(volumes), vg_name)
        return volumes

    def get_all_vdisks(self) -> Optional[List[Dict]]:
        """
        Fetch summary records for every vdisk on the array.
        Equivalent to CLI: svcinfo lsvdisk

        NOTE: The IBM list response returns capacity as "" for VG-member
        volumes. Use get_vdisk_detail() to enrich individual records.

        Returns:
            List of vdisk summary dicts (name, id, volume_group_name,
            mdisk_grp_name, …) or None on failure.
        """
        logger.info("Fetching all vdisks...")
        data = self._make_request(self.VDISK_ENDPOINT, method="POST", json={})
        if data is None:
            return None
        vdisks = data if isinstance(data, list) else []
        logger.info("Retrieved %d vdisks", len(vdisks))
        return vdisks

    def get_vdisk_detail(self, vdisk_id: str) -> Optional[Dict]:
        """
        Fetch the detailed record for a single vdisk by name or numeric ID.
        Equivalent to CLI: svcinfo lsvdisk <id>

        Uses POST with the ID in the body — this firmware returns 405 on GET
        for all ls* endpoints. The detail response includes real 'capacity'.
        """
        data = self._make_request(
            self.VDISK_ENDPOINT, method="POST", json={"id": vdisk_id}
        )
        if data is None:
            return None
        # Single-record POST returns a list with one element on this firmware
        if isinstance(data, list):
            return data[0] if data else None
        return data

    def get_mdiskgrps(self) -> Optional[List[Dict]]:
        """
        Fetch all storage pool (MDisk group) records.
        Equivalent to CLI: svcinfo lsmdiskgrp

        NOTE: The IBM list response returns capacity fields as "" for pools.
        Use get_mdiskgrp_detail() to enrich individual records.

        Returns:
            List of pool summary dicts or None on failure.
        """
        logger.info("Fetching MDisk groups (pools)...")
        data = self._make_request(self.MDISKGRP_ENDPOINT, method="POST", json={})
        if data is None:
            return None
        pools = data if isinstance(data, list) else []
        logger.info("Retrieved %d pools", len(pools))
        return pools

    def get_mdiskgrp_detail(self, pool_id: str) -> Optional[Dict]:
        """
        Fetch the detailed record for a single pool by name or numeric ID.
        Equivalent to CLI: svcinfo lsmdiskgrp <id>

        Uses POST with the ID in the body — this firmware returns 405 on GET.
        The detail response includes real capacity / free_capacity /
        physical_free_capacity values absent from the list response.
        """
        data = self._make_request(
            self.MDISKGRP_ENDPOINT, method="POST", json={"id": pool_id}
        )
        if data is None:
            return None
        if isinstance(data, list):
            return data[0] if data else None
        return data

    def get_partnerships(self) -> Optional[List[Dict]]:
        """
        Fetch all cluster-to-cluster partnership records.
        Equivalent to CLI: svcinfo lspartnership

        Returns:
            List of partnership dicts (name, status, type, bandwidth,
            partnersystem_name, …) or None on failure.
        """
        logger.info("Fetching partnerships...")
        data = self._make_request(self.PARTNERSHIP_ENDPOINT, method="POST", json={})
        if data is None:
            return None
        partnerships = data if isinstance(data, list) else []
        logger.info("Retrieved %d partnerships", len(partnerships))
        return partnerships

    def get_portip(self) -> Optional[List[Dict]]:
        """
        Fetch all IP replication port records.
        Equivalent to CLI: svcinfo lsportip

        Returns:
            List of port dicts (id, node_name, IP_address, state,
            partner_node_name, portset_name, …) or None on failure.
        """
        logger.info("Fetching IP port records...")
        data = self._make_request(self.PORTIP_ENDPOINT, method="POST", json={})
        if data is None:
            return None
        ports = data if isinstance(data, list) else []
        logger.info("Retrieved %d IP port records", len(ports))
        return ports

    def get_node_port_stats(self) -> Optional[List[Dict]]:
        """
        Fetch node port statistics for live throughput data.
        Equivalent to CLI: svcinfo lsnodeportstat

        This endpoint is not available on all firmware versions.  A 404 or
        405 response is treated as "feature unavailable" rather than an
        error — callers receive None and should skip throughput display
        gracefully.

        Returns:
            List of port-stat dicts (node_name, port_id, bytes_sent,
            bytes_received, frame_errors, …), None if unavailable, or
            None on request failure.
        """
        logger.info("Fetching node port stats...")
        url = f"{self.base_url}{self.NODE_PORT_STAT_ENDPOINT}"
        headers = {'X-Auth-Token': self.auth_token}
        try:
            response = self.session.request(
                "POST", url,
                headers=headers,
                json={},
                verify=self.config.verify_ssl,
                timeout=self.config.timeout,
            )
            logger.debug("API POST %s -> %s", url, response.status_code)
            if response.status_code == 200:
                data = response.json()
                stats = data if isinstance(data, list) else []
                logger.info("Retrieved %d node port stat records", len(stats))
                return stats
            elif response.status_code in (404, 405):
                logger.info("lsnodeportstat not available on this firmware — skipping throughput")
                return None
            elif response.status_code == 204:
                return []
            else:
                logger.warning(
                    "lsnodeportstat returned %s — skipping throughput",
                    response.status_code,
                )
                return None
        except requests.exceptions.RequestException as e:
            logger.warning("lsnodeportstat request failed (%s) — skipping throughput", e)
            return None

    def close(self):
        """Close the session."""
        self.session.close()
        logger.info("Session closed")


class StatusAnalyzer:
    """Analyzer for replication status data."""
    
    @staticmethod
    def categorize_state(state: str) -> str:
        """
        Categorize replication state.
        
        Args:
            state: Replication state string
            
        Returns:
            Category: 'normal', 'warning', or 'error'
        """
        state_lower = state.lower()
        
        if state_lower in IBMStorageVirtualizeClient.NORMAL_STATES:
            return 'normal'
        elif state_lower in IBMStorageVirtualizeClient.WARNING_STATES:
            return 'warning'
        elif state_lower in IBMStorageVirtualizeClient.ERROR_STATES:
            return 'error'
        else:
            # Unknown state - treat as warning
            return 'warning'
    
    @staticmethod
    def _parse_rpo_threshold(policy_name: str, policies: List[Dict]) -> Optional[int]:
        """
        Look up rpo_alert_threshold (seconds) for a given policy name.

        Returns the threshold in seconds, or None if not found / not set.
        """
        for p in policies:
            if p.get('name') == policy_name:
                raw = p.get('rpo_alert_threshold') or p.get('rpo_alert_threshold_seconds')
                if raw is not None:
                    try:
                        return int(raw)
                    except (ValueError, TypeError):
                        pass
        return None

    @staticmethod
    def _calculate_rpo_lag(rel: Dict) -> Optional[int]:
        """
        Derive the current replication lag in seconds from a volume group
        replication record.

        IBM Storage Virtualize surfaces the recovery point as an ISO-8601
        timestamp in ``running_recovery_point``.  We subtract that from
        *now* to get the lag.  Returns None when the field is absent or
        cannot be parsed.
        """
        rp_str = rel.get('running_recovery_point') or rel.get('last_recovery_point')
        if not rp_str or rp_str in ('', 'none', 'N/A'):
            return None
        try:
            # Handle both "2024-06-15T12:00:00Z" and "2024-06-15T12:00:00+00:00"
            rp_str_clean = rp_str.replace('Z', '+00:00')
            rp_dt = datetime.fromisoformat(rp_str_clean)
            now = datetime.now(timezone.utc)
            lag = int((now - rp_dt).total_seconds())
            return max(0, lag)
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _rpo_health(lag_seconds: int, threshold_seconds: int) -> Dict:
        """
        Return colour-coded RPO health info based on lag vs. threshold.

        Levels:
          healthy   — lag < 50 % of threshold
          caution   — 50 % ≤ lag < 80 %
          warning   — 80 % ≤ lag < 100 %
          critical  — lag ≥ threshold (SLA breached)
        """
        pct = (lag_seconds / threshold_seconds * 100) if threshold_seconds > 0 else 0
        if lag_seconds >= threshold_seconds:
            level = 'critical'
        elif pct >= 80:
            level = 'warning'
        elif pct >= 50:
            level = 'caution'
        else:
            level = 'healthy'
        return {
            'lag_seconds': lag_seconds,
            'threshold_seconds': threshold_seconds,
            'pct_of_threshold': round(pct, 1),
            'level': level,
        }

    @staticmethod
    def analyze_relationships(relationships: List[Dict], policies: Optional[List[Dict]] = None) -> Dict:
        """
        Analyze replication relationships and generate summary.

        Args:
            relationships: List of RC relationship dictionaries
            policies: Optional list of replication policy dictionaries
                      (from lsreplicationpolicy) used for RPO delta analysis.

        Returns:
            Analysis summary dictionary
        """
        summary = {
            'total': len(relationships),
            'normal': 0,
            'warning': 0,
            'error': 0,
            'details': []
        }

        for rel in relationships:
            name  = rel.get('name', rel.get('id', 'Unknown'))
            state = rel.get('link1_status', 'unknown')

            loc1 = rel.get('location1_system_name', 'N/A')
            mode1 = rel.get('location1_replication_mode', '')
            loc2 = rel.get('location2_system_name', 'N/A')
            mode2 = rel.get('location2_replication_mode', '')
            within_rpo = rel.get('location2_within_rpo', '')
            policy = rel.get('replication_policy_name', 'N/A')

            primary_vdisk   = f"{loc1} ({mode1})" if mode1 else loc1
            secondary_vdisk = f"{loc2} ({mode2})" if mode2 else loc2

            category = StatusAnalyzer.categorize_state(state)

            # ── RPO delta ──────────────────────────────────────────────────
            rpo_delta = None
            lag = StatusAnalyzer._calculate_rpo_lag(rel)
            if lag is not None and policies:
                threshold = StatusAnalyzer._parse_rpo_threshold(policy, policies)
                if threshold is not None:
                    rpo_delta = StatusAnalyzer._rpo_health(lag, threshold)
            elif lag is not None:
                # No policy data — report raw lag only
                rpo_delta = {'lag_seconds': lag, 'threshold_seconds': None,
                             'pct_of_threshold': None, 'level': 'unknown'}
            # ──────────────────────────────────────────────────────────────

            detail = {
                'name': name,
                'state': state,
                'category': category,
                'primary_vdisk': primary_vdisk,
                'secondary_vdisk': secondary_vdisk,
                'within_rpo': within_rpo,
                'policy': policy,
                'rpo_delta': rpo_delta,
                'raw_data': rel
            }

            summary['details'].append(detail)
            summary[category] += 1

        return summary


class CapacityAuditor:
    """
    Asymmetric Capacity & Pool Headroom Auditor.

    Compares source and DR arrays to detect:
      1. Volumes whose provisioned capacity differs between source and target.
      2. DR pools whose physical_free_capacity is dangerously low relative to
         the total provisioned capacity of target volumes in that pool.

    All byte values from the IBM API are string-encoded integers (bytes).
    This class converts them to GiB for display.
    """

    GiB = 1024 ** 3

    # Headroom thresholds (% of pool physical capacity that is free)
    HEADROOM_CRITICAL = 10   # < 10 % free  →  critical
    HEADROOM_WARNING  = 25   # < 25 % free  →  warning

    # Unit multipliers to GiB
    _UNIT_TO_GIB: Dict[str, float] = {
        'B':   1 / (1024 ** 3),
        'KB':  1 / (1024 ** 2),
        'MB':  1 / 1024,
        'GB':  1.0,
        'TB':  1024.0,
        'PB':  1024.0 ** 2,
    }

    @staticmethod
    def _to_gib(value) -> Optional[float]:
        """
        Convert an IBM API capacity value to GiB.

        Handles two formats returned by different firmware versions:
          - Raw byte integer string: '107374182400'  (bytes)
          - Human-readable string:   '100.00GB', '1.00TB', '248.44TB'
        Returns None for None, empty string, or unparseable values.
        """
        if value is None or value == '':
            return None
        s = str(value).strip()
        # Try human-readable suffix first (e.g. '248.44TB', '1.00GB')
        for unit, multiplier in CapacityAuditor._UNIT_TO_GIB.items():
            if s.upper().endswith(unit):
                try:
                    num = float(s[: -len(unit)].strip())
                    return round(num * multiplier, 2)
                except ValueError:
                    return None
        # Fall back to raw byte integer
        try:
            return round(int(s) / CapacityAuditor.GiB, 2)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def build_vdisk_index(vdisks: List[Dict]) -> Dict[str, Dict]:
        """Return a dict keyed by vdisk name for O(1) lookups."""
        return {v.get('name', ''): v for v in vdisks if v.get('name')}

    @staticmethod
    def build_pool_index(pools: List[Dict]) -> Dict[str, Dict]:
        """Return a dict keyed by pool name."""
        return {p.get('name', ''): p for p in pools if p.get('name')}

    @staticmethod
    def audit_volume_capacity_delta(
        vg_names: List[str],
        source_vdisks: Dict[str, Dict],
        dr_vdisks: Dict[str, Dict],
        dr_volumes_by_vg: Dict[str, List[Dict]],
    ) -> List[Dict]:
        """
        For each volume in each replicated VG, compare the source provisioned
        capacity against the DR target provisioned capacity.

        Returns a list of per-volume dicts:
          vg_name, vol_name,
          source_capacity_gib, dr_capacity_gib,
          delta_gib,   (DR − source; negative = DR is undersized)
          status       ('ok' | 'mismatch' | 'missing')
        """
        results = []
        for vg in vg_names:
            for vol in dr_volumes_by_vg.get(vg, []):
                vname = vol.get('name', '')
                dr_rec    = dr_vdisks.get(vname)
                src_rec   = source_vdisks.get(vname)

                dr_cap  = CapacityAuditor._to_gib(dr_rec.get('capacity')  if dr_rec  else None)
                src_cap = CapacityAuditor._to_gib(src_rec.get('capacity') if src_rec else None)

                if dr_cap is None:
                    status = 'missing'
                    delta  = None
                elif src_cap is None:
                    # Volume exists on DR but not found on source index — note it
                    status = 'ok'
                    delta  = None
                elif dr_cap < src_cap:
                    status = 'mismatch'
                    delta  = round(dr_cap - src_cap, 2)
                else:
                    status = 'ok'
                    delta  = round(dr_cap - src_cap, 2)

                results.append({
                    'vg_name':            vg,
                    'vol_name':           vname,
                    'source_capacity_gib': src_cap,
                    'dr_capacity_gib':    dr_cap,
                    'delta_gib':          delta,
                    'status':             status,
                })
        return results

    @staticmethod
    def audit_pool_headroom(
        dr_vdisks: Dict[str, Dict],
        dr_pools: Dict[str, Dict],
    ) -> List[Dict]:
        """
        For every DR storage pool, calculate:
          - total provisioned capacity of all volumes in that pool
          - physical free capacity remaining
          - headroom % = physical_free / pool_capacity * 100

        Returns a list of per-pool dicts:
          pool_name,
          total_capacity_gib,
          physical_free_gib,
          provisioned_gib,        (sum of all vdisk capacities in this pool)
          headroom_pct,
          status                  ('ok' | 'warning' | 'critical' | 'unknown')
        """
        # Aggregate provisioned GiB per pool from all vdisks on DR.
        # capacity may be a raw-byte string ('107374182400') or a human-readable
        # string ('248.44TB') — use _to_gib() to handle both formats.
        pool_provisioned: Dict[str, float] = {}
        for v in dr_vdisks.values():
            pool = v.get('mdisk_grp_name', '')
            gib  = CapacityAuditor._to_gib(v.get('capacity'))
            if gib is not None:
                pool_provisioned[pool] = pool_provisioned.get(pool, 0.0) + gib

        results = []
        for pool_name, pool in dr_pools.items():
            total_cap = CapacityAuditor._to_gib(pool.get('capacity'))

            # IBM API field name varies by firmware version and request type:
            # detailed view  → physical_free_capacity
            # summary list   → free_capacity (logical) or physical_free_capacity
            # Fall through candidates in priority order.
            phys_free = CapacityAuditor._to_gib(
                pool.get('physical_free_capacity')
                or pool.get('free_capacity')
                or pool.get('real_free_capacity')
            )

            provisioned = round(pool_provisioned.get(pool_name, 0.0), 2)

            logger.debug("Pool %s: capacity=%s free=%s provisioned=%.2f GiB",
                         pool_name,
                         pool.get('capacity'), pool.get('free_capacity'), provisioned)

            if total_cap and total_cap > 0 and phys_free is not None:
                headroom_pct = round((phys_free / total_cap) * 100, 1)
                if headroom_pct < CapacityAuditor.HEADROOM_CRITICAL:
                    status = 'critical'
                elif headroom_pct < CapacityAuditor.HEADROOM_WARNING:
                    status = 'warning'
                else:
                    status = 'ok'
            else:
                headroom_pct = None
                status = 'unknown'

            results.append({
                'pool_name':        pool_name,
                'total_capacity_gib': total_cap,
                'physical_free_gib':  phys_free,
                'provisioned_gib':    provisioned,
                'headroom_pct':       headroom_pct,
                'status':             status,
            })

        # Sort: critical first, then warning, then ok
        order = {'critical': 0, 'warning': 1, 'ok': 2, 'unknown': 3}
        results.sort(key=lambda r: order.get(r['status'], 9))
        return results


class DRMappingAnalyzer:
    """
    Cross-reference volumes inside replicated Volume Groups against the
    DR array's vdisk→host mapping table to surface any target volumes
    that are online but not mapped to any host or host cluster.

    Usage
    -----
    1. On the *source* array: collect the list of replication relationships
       (lsvolumegroupreplication) to know which Volume Groups exist.
    2. On the *DR/target* array: call get_vdisk_host_map() and, optionally,
       get_volume_group_volumes() for each VG of interest.
    3. Call DRMappingAnalyzer.check_dr_host_mappings() with those datasets.
    """

    @staticmethod
    def check_dr_host_mappings(
        vg_names: List[str],
        dr_volumes: Dict[str, List[Dict]],
        dr_host_map: List[Dict],
    ) -> Dict:
        """
        Check whether every volume in each Volume Group has at least one
        host mapping on the DR array.

        Parameters
        ----------
        vg_names:
            Ordered list of Volume Group names to check (from the source array
            replication relationships).
        dr_volumes:
            Mapping of { vg_name: [vdisk records] } fetched from the DR array.
            Each vdisk record must contain at minimum a ``name`` key.
        dr_host_map:
            Full output of lsvdiskhostmap from the DR array — a flat list of
            records each containing ``vdisk_name`` and ``host_name``.

        Returns
        -------
        dict with keys:
          total_volumes     int
          mapped_volumes    int
          unmapped_volumes  int
          vg_results        list[dict] — one entry per VG with per-volume detail
        """
        # Build a set of vdisk names that have ≥1 host mapping for O(1) lookup
        mapped_vdisk_names: set = {
            r.get('vdisk_name', '') for r in dr_host_map if r.get('vdisk_name')
        }

        # Also capture host names per vdisk for display
        vdisk_hosts: Dict[str, List[str]] = {}
        for r in dr_host_map:
            vname = r.get('vdisk_name', '')
            hname = r.get('host_name', '')
            if vname and hname:
                vdisk_hosts.setdefault(vname, [])
                if hname not in vdisk_hosts[vname]:
                    vdisk_hosts[vname].append(hname)

        total = 0
        mapped = 0
        unmapped = 0
        vg_results = []

        for vg in vg_names:
            volumes = dr_volumes.get(vg, [])
            vg_detail = {
                'vg_name': vg,
                'volumes': [],
                'total': len(volumes),
                'mapped': 0,
                'unmapped': 0,
                'status': 'ok',          # ok | warning | error
            }

            if not volumes:
                # Could not fetch volumes for this VG (API failure or empty)
                vg_detail['status'] = 'unknown'
                vg_results.append(vg_detail)
                continue

            for vol in volumes:
                vname = vol.get('name', vol.get('id', ''))
                is_mapped = vname in mapped_vdisk_names
                hosts = vdisk_hosts.get(vname, [])
                vol_entry = {
                    'name': vname,
                    'mapped': is_mapped,
                    'hosts': hosts,
                }
                vg_detail['volumes'].append(vol_entry)
                if is_mapped:
                    vg_detail['mapped'] += 1
                    mapped += 1
                else:
                    vg_detail['unmapped'] += 1
                    unmapped += 1
                total += 1

            if vg_detail['unmapped'] > 0:
                vg_detail['status'] = 'error' if vg_detail['mapped'] == 0 else 'warning'

            vg_results.append(vg_detail)

        return {
            'total_volumes': total,
            'mapped_volumes': mapped,
            'unmapped_volumes': unmapped,
            'vg_results': vg_results,
        }


class PartnershipAnalyzer:
    """
    Aggregate network health analyzer for cluster-to-cluster partnerships.

    Ingests raw records from lspartnership, lsportip, and (optionally)
    lsnodeportstat to produce a structured result dict for the output layer.
    No printing happens here — analysis only.
    """

    @staticmethod
    def _parse_bandwidth(raw) -> Optional[float]:
        """
        Convert lspartnership 'bandwidth' field to float MB/s.

        IBM returns this as a plain integer string (MB/s) or empty string
        when uncapped.  Returns None for empty / unparseable values.
        """
        if raw is None or str(raw).strip() in ('', '0', 'none', 'N/A'):
            return None
        try:
            return float(str(raw).strip())
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _match_ports_to_partnership(partnership: Dict, portip_records: List[Dict]) -> List[Dict]:
        """
        Return the subset of lsportip records that belong to this partnership.

        Matching strategy (in priority order):
          1. partner_node_name == partnership['partnersystem_name']
          2. portset_name contains the partnership name (partial match)
        Returns an empty list when portip_records is empty or no match found.
        """
        partner_sys = partnership.get('partnersystem_name', '')
        p_name = partnership.get('name', '')
        matched = []
        for port in portip_records:
            if partner_sys and port.get('partner_node_name', '') == partner_sys:
                matched.append(port)
            elif p_name and p_name.lower() in port.get('portset_name', '').lower():
                matched.append(port)
        # Deduplicate by port id
        seen = set()
        unique = []
        for p in matched:
            key = p.get('id') or p.get('name', '')
            if key not in seen:
                seen.add(key)
                unique.append(p)
        return unique

    @staticmethod
    def _extract_throughput(
        port_records: List[Dict],
        node_port_stats: Optional[List[Dict]],
    ) -> Optional[float]:
        """
        Sum live throughput (MB/s) across the given ports from node port stats.

        Matches on node_name + port_id.  bytes_sent + bytes_received are
        cumulative counters — we report the raw summed value converted to MB/s
        as a point-in-time snapshot (suitable for a single-run health check).

        Returns None when node_port_stats is None (endpoint unavailable).
        """
        if node_port_stats is None:
            return None
        if not port_records:
            return None

        # Build lookup: (node_name, port_id) -> stat record
        stat_index: Dict[tuple, Dict] = {}
        for s in node_port_stats:
            key = (s.get('node_name', ''), str(s.get('port_id', '')))
            stat_index[key] = s

        total_bytes = 0.0
        matched_any = False
        for port in port_records:
            key = (port.get('node_name', ''), str(port.get('id', '')))
            stat = stat_index.get(key)
            if stat:
                matched_any = True
                try:
                    total_bytes += int(stat.get('bytes_sent', 0) or 0)
                    total_bytes += int(stat.get('bytes_received', 0) or 0)
                except (ValueError, TypeError):
                    pass

        if not matched_any:
            return None
        # Convert bytes to MB/s (cumulative snapshot — informational only)
        return round(total_bytes / (1024 * 1024), 2)

    @staticmethod
    def _extract_dropped_frames(port_records: List[Dict], node_port_stats: Optional[List[Dict]]) -> Optional[int]:
        """
        Sum frame_errors across matched port stat records.
        Returns None when stats are unavailable.
        """
        if node_port_stats is None or not port_records:
            return None
        stat_index: Dict[tuple, Dict] = {
            (s.get('node_name', ''), str(s.get('port_id', ''))): s
            for s in node_port_stats
        }
        total = 0
        matched_any = False
        for port in port_records:
            key = (port.get('node_name', ''), str(port.get('id', '')))
            stat = stat_index.get(key)
            if stat:
                matched_any = True
                try:
                    total += int(stat.get('frame_errors', 0) or 0)
                except (ValueError, TypeError):
                    pass
        return total if matched_any else None

    @staticmethod
    def _port_redundancy_status(ports: List[Dict]) -> str:
        """
        Evaluate redundancy across a partnership's IP ports.

        Returns:
          'ok'       — all ports online
          'degraded' — ≥1 port offline, but ≥1 still online
          'offline'  — all ports offline
          'unknown'  — no port data available
        """
        if not ports:
            return 'unknown'
        online = sum(1 for p in ports if p.get('state', '').lower() == 'configured')
        offline = len(ports) - online
        if offline == 0:
            return 'ok'
        elif online > 0:
            return 'degraded'
        else:
            return 'offline'

    @staticmethod
    def analyze_partnerships(
        partnerships: List[Dict],
        portip_records: Optional[List[Dict]],
        node_port_stats: Optional[List[Dict]],
    ) -> Dict:
        """
        Analyze partnership and link health data.

        Parameters
        ----------
        partnerships:
            Raw list from lspartnership.
        portip_records:
            Raw list from lsportip, or None if unavailable.
        node_port_stats:
            Raw list from lsnodeportstat, or None if unavailable.

        Returns
        -------
        dict with keys:
          total / healthy / degraded / offline counts,
          throughput_available  bool — whether live throughput data was obtained,
          details               list[dict] — one entry per partnership
        """
        portip_records = portip_records or []
        summary: Dict = {
            'total': len(partnerships),
            'healthy': 0,
            'degraded': 0,
            'offline': 0,
            'throughput_available': node_port_stats is not None,
            'details': [],
        }

        for p in partnerships:
            name        = p.get('name', p.get('id', 'Unknown'))
            state       = p.get('status', p.get('state', 'unknown')).lower()
            link_type   = p.get('type', 'unknown')
            partner_sys = p.get('partnersystem_name', 'N/A')
            bandwidth   = PartnershipAnalyzer._parse_bandwidth(p.get('bandwidth'))

            ports       = PartnershipAnalyzer._match_ports_to_partnership(p, portip_records)
            redundancy  = PartnershipAnalyzer._port_redundancy_status(ports)
            throughput  = PartnershipAnalyzer._extract_throughput(ports, node_port_stats)
            dropped     = PartnershipAnalyzer._extract_dropped_frames(ports, node_port_stats)

            # Roll up to a simple health category
            if state in ('fully_configured', 'fully_connected', 'connected', 'online'):
                health = 'healthy'
                summary['healthy'] += 1
            elif state in ('partially_configured', 'degraded', 'connecting'):
                health = 'degraded'
                summary['degraded'] += 1
            else:
                health = 'offline'
                summary['offline'] += 1

            # Upgrade health if port redundancy is worse than partnership state
            if redundancy == 'degraded' and health == 'healthy':
                health = 'degraded'
                summary['healthy'] -= 1
                summary['degraded'] += 1
            elif redundancy == 'offline' and health != 'offline':
                if health == 'healthy':
                    summary['healthy'] -= 1
                else:
                    summary['degraded'] -= 1
                health = 'offline'
                summary['offline'] += 1

            # Build per-port display records
            port_details = [
                {
                    'name':         port.get('name') or port.get('id', ''),
                    'state':        port.get('state', 'unknown'),
                    'ip_address':   port.get('IP_address', port.get('ip_address', '')),
                    'node_name':    port.get('node_name', ''),
                    'partner_node': port.get('partner_node_name', ''),
                }
                for port in ports
            ]

            summary['details'].append({
                'name':              name,
                'state':             state,
                'health':            health,
                'link_type':         link_type,
                'partner_system':    partner_sys,
                'bandwidth_mbps':    bandwidth,
                'throughput_mbps':   throughput,
                'dropped_frames':    dropped,
                'redundancy_status': redundancy,
                'ports':             port_details,
            })

        return summary



class OutputFormatter:
    """Formatter for console output."""
    
    @staticmethod
    def print_colored(text: str, color: str = 'white', bold: bool = False):
        """
        Print colored text to console.
        
        Args:
            text: Text to print
            color: Color name (green, yellow, red, white)
            bold: Whether to make text bold
        """
        if not COLORS_AVAILABLE:
            print(text)
            return
        
        color_map = {
            'green': Fore.GREEN,
            'yellow': Fore.YELLOW,
            'red': Fore.RED,
            'white': Fore.WHITE,
            'cyan': Fore.CYAN,
            'magenta': Fore.MAGENTA
        }
        
        color_code = color_map.get(color.lower(), Fore.WHITE)
        style = Style.BRIGHT if bold else ''
        
        print(f"{style}{color_code}{text}{Style.RESET_ALL}")
    
    @staticmethod
    def print_header(text: str):
        """Print section header."""
        OutputFormatter.print_colored(f"\n{'=' * 80}", 'cyan')
        OutputFormatter.print_colored(text, 'cyan', bold=True)
        OutputFormatter.print_colored('=' * 80, 'cyan')
    
    @staticmethod
    def print_relationship(detail: Dict):
        """
        Print replication relationship details.
        
        Args:
            detail: Relationship detail dictionary
        """
        category = detail['category']
        name = detail['name']
        state = detail['state']
        primary = detail['primary_vdisk']
        secondary = detail['secondary_vdisk']
        
        # Choose color based on category
        if category == 'normal':
            color = 'green'
            symbol = '✓'
        elif category == 'warning':
            color = 'yellow'
            symbol = '⚠'
        else:
            color = 'red'
            symbol = '✗'
        
        OutputFormatter.print_colored(
            f"{symbol} {name}: {state}",
            color,
            bold=(category != 'normal')
        )
        OutputFormatter.print_colored(
            f"  Primary: {primary} → Secondary: {secondary}",
            'white'
        )
    
    @staticmethod
    def print_summary(summary: Dict):
        """
        Print summary statistics.
        
        Args:
            summary: Summary dictionary from StatusAnalyzer
        """
        OutputFormatter.print_header("REPLICATION STATUS SUMMARY")
        
        total = summary['total']
        normal = summary['normal']
        warning = summary['warning']
        error = summary['error']
        
        print(f"\nTotal Relationships: {total}")
        OutputFormatter.print_colored(f"  ✓ Normal: {normal}", 'green')
        
        if warning > 0:
            OutputFormatter.print_colored(f"  ⚠ Warnings: {warning}", 'yellow', bold=True)
        else:
            OutputFormatter.print_colored(f"  ⚠ Warnings: {warning}", 'white')
        
        if error > 0:
            OutputFormatter.print_colored(f"  ✗ Errors: {error}", 'red', bold=True)
        else:
            OutputFormatter.print_colored(f"  ✗ Errors: {error}", 'white')
        
        # Calculate health percentage
        if total > 0:
            health_pct = (normal / total) * 100
            if health_pct == 100:
                color = 'green'
            elif health_pct >= 80:
                color = 'yellow'
            else:
                color = 'red'
            
            OutputFormatter.print_colored(f"\nOverall Health: {health_pct:.1f}%", color, bold=True)


    @staticmethod
    def print_partnership_detail(detail: Dict):
        """
        Print one partnership entry.

        Compact when healthy (ports collapsed to a single line).
        Verbose when degraded/offline (per-port breakdown shown).
        """
        health = detail['health']
        name   = detail['name']
        state  = detail['state']

        if health == 'healthy':
            color, symbol = 'green', '✓'
        elif health == 'degraded':
            color, symbol = 'yellow', '⚠'
        else:
            color, symbol = 'red', '✗'

        OutputFormatter.print_colored(
            f"{symbol} {name}: {state}  [{detail['link_type']}]  ↔  {detail['partner_system']}",
            color,
            bold=(health != 'healthy'),
        )

        # Bandwidth / throughput line
        bw = detail.get('bandwidth_mbps')
        tp = detail.get('throughput_mbps')
        if bw is not None and tp is not None:
            pct = round((tp / bw) * 100, 1) if bw > 0 else 0
            OutputFormatter.print_colored(
                f"  Bandwidth: {tp:.1f} MB/s used / {bw:.0f} MB/s ceiling ({pct}%)",
                'white',
            )
        elif bw is not None:
            OutputFormatter.print_colored(
                f"  Bandwidth ceiling: {bw:.0f} MB/s  (live throughput unavailable)",
                'white',
            )
        elif tp is not None:
            OutputFormatter.print_colored(
                f"  Throughput: {tp:.1f} MB/s  (no ceiling configured)",
                'white',
            )

        # Dropped frames
        dropped = detail.get('dropped_frames')
        if dropped is not None:
            df_color = 'red' if dropped > 0 else 'white'
            OutputFormatter.print_colored(
                f"  Dropped frames: {dropped}",
                df_color,
                bold=(dropped > 0),
            )

        # Port redundancy
        redundancy = detail.get('redundancy_status', 'unknown')
        ports = detail.get('ports', [])

        if redundancy == 'ok' and ports:
            OutputFormatter.print_colored(
                f"  ✓ All {len(ports)} path(s) online",
                'green',
            )
        elif redundancy == 'unknown':
            OutputFormatter.print_colored(
                "  Path redundancy: unknown (no portip data)",
                'white',
            )
        else:
            # Expand per-port detail when degraded/offline
            for port in ports:
                p_state = port.get('state', 'unknown').lower()
                p_color = 'green' if p_state == 'configured' else 'red'
                p_sym   = '✓' if p_state == 'configured' else '✗'
                OutputFormatter.print_colored(
                    f"  ↳ {p_sym} {port['name']}  {port['ip_address']}"
                    f"  node:{port['node_name']}  ({p_state})",
                    p_color,
                )

    @staticmethod
    def print_partnership_summary(result: Dict):
        """
        Print the aggregate partnership health header and per-partnership details.
        """
        OutputFormatter.print_header("PARTNERSHIP & LINK HEALTH")

        total    = result['total']
        healthy  = result['healthy']
        degraded = result['degraded']
        offline  = result['offline']

        print(f"\nTotal Partnerships: {total}")
        OutputFormatter.print_colored(f"  ✓ Healthy:  {healthy}", 'green')

        if degraded > 0:
            OutputFormatter.print_colored(f"  ⚠ Degraded: {degraded}", 'yellow', bold=True)
        else:
            OutputFormatter.print_colored(f"  ⚠ Degraded: {degraded}", 'white')

        if offline > 0:
            OutputFormatter.print_colored(f"  ✗ Offline:  {offline}", 'red', bold=True)
        else:
            OutputFormatter.print_colored(f"  ✗ Offline:  {offline}", 'white')

        if not result.get('throughput_available'):
            OutputFormatter.print_colored(
                "  (live throughput unavailable — lsnodeportstat not supported on this firmware)",
                'white',
            )

        print()
        for detail in result['details']:
            OutputFormatter.print_partnership_detail(detail)



def parse_arguments():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description='IBM Storage Virtualize Replication Status Monitor',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Using environment variables
  export IBM_SV_HOST="https://storage.example.com"
  export IBM_SV_USER="admin"
  export IBM_SV_PASSWORD="password"
  python ibm_storage_replication_check.py
  
  # Using config file
  python ibm_storage_replication_check.py --config config.json
  
  # With JSON output
  python ibm_storage_replication_check.py --output json > report.json
  
  # Disable SSL verification (for testing only)
  python ibm_storage_replication_check.py --no-verify-ssl
        """
    )
    
    parser.add_argument(
        '--config',
        type=str,
        help='Path to JSON configuration file'
    )
    
    parser.add_argument(
        '--output',
        choices=['console', 'json'],
        default='console',
        help='Output format (default: console)'
    )
    
    parser.add_argument(
        '--no-verify-ssl',
        action='store_true',
        help='Disable SSL certificate verification (not recommended for production)'
    )
    
    parser.add_argument(
        '--verbose',
        action='store_true',
        help='Enable verbose logging'
    )

    parser.add_argument(
        '--partnerships',
        action='store_true',
        help='Include partnership & link health section (lspartnership, lsportip, lsnodeportstat)'
    )
    
    return parser.parse_args()


def main():
    """Main execution function."""
    args = parse_arguments()
    
    # Configure logging level
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    
    try:
        # Load configuration
        config = Config(config_file=args.config)
        
        # Override SSL verification if specified
        if args.no_verify_ssl:
            config.verify_ssl = False
        
        # Initialize API client
        client = IBMStorageVirtualizeClient(config)
        
        # Authenticate
        OutputFormatter.print_header("IBM STORAGE VIRTUALIZE REPLICATION MONITOR")
        print(f"\nConnecting to: {config.host}")
        
        if not client.authenticate():
            OutputFormatter.print_colored("✗ Authentication failed", 'red', bold=True)
            return 1
        
        OutputFormatter.print_colored("✓ Authentication successful", 'green')
        
        # Get replication relationships
        relationships = client.get_rc_relationships()
        
        if relationships is None:
            OutputFormatter.print_colored("✗ Failed to retrieve replication relationships", 'red', bold=True)
            return 1
        
        if len(relationships) == 0:
            OutputFormatter.print_colored("⚠ No replication relationships found", 'yellow')
            return 0
        
        # Analyze status
        summary = StatusAnalyzer.analyze_relationships(relationships)

        # Optionally fetch partnership & link health data
        partnership_result = None
        if args.partnerships:
            raw_partnerships = client.get_partnerships()
            if raw_partnerships is not None:
                portip_records  = client.get_portip()
                node_port_stats = client.get_node_port_stats()
                partnership_result = PartnershipAnalyzer.analyze_partnerships(
                    raw_partnerships,
                    portip_records,
                    node_port_stats,
                )
            else:
                OutputFormatter.print_colored(
                    "⚠ Could not retrieve partnership data — skipping partnership section",
                    'yellow',
                )

        # Output results
        if args.output == 'json':
            # JSON output
            output_data = {
                'timestamp': datetime.utcnow().isoformat(),
                'host': config.host,
                'summary': {
                    'total': summary['total'],
                    'normal': summary['normal'],
                    'warning': summary['warning'],
                    'error': summary['error']
                },
                'relationships': [
                    {
                        'name': d['name'],
                        'state': d['state'],
                        'category': d['category'],
                        'primary_vdisk': d['primary_vdisk'],
                        'secondary_vdisk': d['secondary_vdisk']
                    }
                    for d in summary['details']
                ]
            }
            if partnership_result is not None:
                output_data['partnerships'] = partnership_result
            print(json.dumps(output_data, indent=2))
        else:
            # Console output
            OutputFormatter.print_header("REPLICATION RELATIONSHIPS")
            
            # Print all relationships
            for detail in summary['details']:
                OutputFormatter.print_relationship(detail)
            
            # Print summary
            OutputFormatter.print_summary(summary)

            # Partnership & link health (opt-in)
            if partnership_result is not None:
                OutputFormatter.print_partnership_summary(partnership_result)

            # Print warnings if any
            if summary['warning'] > 0 or summary['error'] > 0:
                OutputFormatter.print_header("ATTENTION REQUIRED")
                
                for detail in summary['details']:
                    if detail['category'] in ['warning', 'error']:
                        OutputFormatter.print_relationship(detail)
        
        # Close client
        client.close()
        
        # Return exit code based on status
        if summary['error'] > 0:
            return 2  # Errors found
        elif summary['warning'] > 0:
            return 1  # Warnings found
        else:
            return 0  # All OK
        
    except ValueError as e:
        OutputFormatter.print_colored(f"✗ Configuration error: {e}", 'red', bold=True)
        return 1
    except KeyboardInterrupt:
        OutputFormatter.print_colored("\n✗ Interrupted by user", 'yellow')
        return 130
    except Exception as e:
        logger.exception("Unexpected error occurred")
        OutputFormatter.print_colored(f"✗ Unexpected error: {e}", 'red', bold=True)
        return 1


if __name__ == '__main__':
    sys.exit(main())

# Made with Bob
