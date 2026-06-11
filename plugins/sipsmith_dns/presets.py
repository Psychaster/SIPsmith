"""Cisco UC SRV presets for the DNS plugin."""

from __future__ import annotations

# Each preset dict: name, record_type, priority, weight, port, rdata, ttl, description


def cisco_cucm_presets(cucm_fqdn: str) -> list[dict]:
    """SRV records pointing to a CUCM node for UC client discovery."""
    target = cucm_fqdn if cucm_fqdn.endswith(".") else cucm_fqdn + "."
    return [
        {
            "name": "_cisco-uds._tcp",
            "record_type": "SRV",
            "priority": 10,
            "weight": 5,
            "port": 8443,
            "rdata": target,
            "ttl": 3600,
            "description": "CUCM UDS (Unified Directory Service) — Jabber/Webex client discovery",
        },
        {
            "name": "_cuplogin._tcp",
            "record_type": "SRV",
            "priority": 10,
            "weight": 5,
            "port": 443,
            "rdata": target,
            "ttl": 3600,
            "description": "CUCM IM&P login (Presence/Jabber)",
        },
        {
            "name": "_sip._tcp",
            "record_type": "SRV",
            "priority": 10,
            "weight": 5,
            "port": 5060,
            "rdata": target,
            "ttl": 3600,
            "description": "SIP over TCP",
        },
        {
            "name": "_sips._tcp",
            "record_type": "SRV",
            "priority": 10,
            "weight": 5,
            "port": 5061,
            "rdata": target,
            "ttl": 3600,
            "description": "Secure SIP (TLS) over TCP",
        },
        {
            "name": "_sip._udp",
            "record_type": "SRV",
            "priority": 10,
            "weight": 5,
            "port": 5060,
            "rdata": target,
            "ttl": 3600,
            "description": "SIP over UDP",
        },
    ]


def cisco_expressway_presets(exp_edge_fqdn: str) -> list[dict]:
    """SRV records for Expressway Edge MRA and XMPP federation."""
    target = exp_edge_fqdn if exp_edge_fqdn.endswith(".") else exp_edge_fqdn + "."
    return [
        {
            "name": "_collab-edge._tls",
            "record_type": "SRV",
            "priority": 10,
            "weight": 5,
            "port": 8443,
            "rdata": target,
            "ttl": 3600,
            "description": "Expressway Edge MRA — Jabber/Webex off-premises discovery",
        },
        {
            "name": "_xmpp-server._tcp",
            "record_type": "SRV",
            "priority": 10,
            "weight": 5,
            "port": 5269,
            "rdata": target,
            "ttl": 3600,
            "description": "XMPP server federation (Jabber B2B)",
        },
    ]


def cisco_imp_presets(imp_fqdn: str) -> list[dict]:
    """SRV records for CUCM IM&P (Cisco Presence)."""
    target = imp_fqdn if imp_fqdn.endswith(".") else imp_fqdn + "."
    return [
        {
            "name": "_cisco-uds._tcp",
            "record_type": "SRV",
            "priority": 20,
            "weight": 5,
            "port": 8443,
            "rdata": target,
            "ttl": 3600,
            "description": "IM&P UDS (secondary/fallback)",
        },
    ]


# Catalog for the GUI "Apply Preset" picker
PRESET_CATALOG: dict[str, dict] = {
    "cucm": {
        "label": "CUCM (UDS, SIP, IM&P login)",
        "params": [{"name": "cucm_fqdn", "label": "CUCM FQDN", "placeholder": "cucm.lab.local"}],
        "fn": "cisco_cucm_presets",
    },
    "expressway": {
        "label": "Expressway Edge (MRA, XMPP)",
        "params": [
            {
                "name": "exp_edge_fqdn",
                "label": "Expressway Edge FQDN",
                "placeholder": "exp-e.lab.local",
            }
        ],
        "fn": "cisco_expressway_presets",
    },
    "imp": {
        "label": "IM&P (secondary UDS)",
        "params": [{"name": "imp_fqdn", "label": "IM&P FQDN", "placeholder": "imp.lab.local"}],
        "fn": "cisco_imp_presets",
    },
}
