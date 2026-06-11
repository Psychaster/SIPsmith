"""Zone file and named.conf generation for the DNS plugin."""

from __future__ import annotations

import datetime
import ipaddress
import json
import re

from sipsmith_dns.models import DnsRecord, DnsSettings, DnsZone

# ── Serial management ─────────────────────────────────────────────────────────

_ZONE_NAME_RE = re.compile(r"^(?!\-)(?:[a-zA-Z0-9\-]{1,63}\.)*[a-zA-Z0-9\-]{1,63}\.?$")

# Allow labels, @, *, wildcards for record names
_RECORD_NAME_RE = re.compile(r"^(@|\*|\*\.[a-zA-Z0-9\-\.]+|(?:[a-zA-Z0-9\-\_\.]+))$")


def validate_zone_name(name: str) -> bool:
    """Return True if name is a valid DNS zone name (absolute or relative)."""
    n = name.rstrip(".")
    if not n or len(n) > 253:
        return False
    return bool(_ZONE_NAME_RE.match(n + "."))


def validate_record_name(name: str) -> bool:
    """Return True if name is acceptable as a DNS record owner name."""
    if not name or len(name) > 253:
        return False
    return bool(_RECORD_NAME_RE.match(name))


def validate_ipv4(s: str) -> bool:
    try:
        ipaddress.IPv4Address(s)
        return True
    except ValueError:
        return False


def validate_ipv6(s: str) -> bool:
    try:
        ipaddress.IPv6Address(s)
        return True
    except ValueError:
        return False


def next_serial(current: int) -> int:
    """Return next SOA serial in YYYYMMDDnn format."""
    today = datetime.datetime.now(datetime.UTC)
    date_prefix = int(today.strftime("%Y%m%d")) * 100
    if current >= date_prefix and current < date_prefix + 99:
        return current + 1
    return date_prefix + 1


# ── Zone file rendering ───────────────────────────────────────────────────────


def _abs(name: str, origin: str) -> str:
    """Make a DNS name absolute relative to origin."""
    if name == "@" or name.endswith("."):
        return name
    return f"{name}.{origin.rstrip('.')}."


def render_zone_file(zone: DnsZone, records: list[DnsRecord]) -> str:
    """Render a BIND9 zone file string for the given zone and records."""
    origin = zone.name.rstrip(".") + "."
    ns = zone.ns_primary if zone.ns_primary.endswith(".") else zone.ns_primary + "."
    email = zone.admin_email.replace("@", ".").rstrip(".") + "."
    lines: list[str] = [
        f"$ORIGIN {origin}",
        f"$TTL {zone.ttl}",
        "",
        f"@ IN SOA {ns} {email} (",
        f"    {zone.serial}  ; serial",
        f"    {zone.refresh} ; refresh",
        f"    {zone.retry}   ; retry",
        f"    {zone.expire}  ; expire",
        f"    {zone.ttl}     ; minimum TTL",
        ")",
        "",
        f"@ IN NS {ns}",
        "",
    ]

    for rec in records:
        if not rec.enabled:
            continue
        ttl_str = str(rec.ttl) if rec.ttl is not None else ""
        rtype = rec.record_type.upper()
        name = rec.name

        if rtype == "SRV":
            pri = rec.priority if rec.priority is not None else 10
            wt = rec.weight if rec.weight is not None else 10
            pt = rec.port if rec.port is not None else 443
            rdata = rec.rdata if rec.rdata.endswith(".") else rec.rdata + "."
            lines.append(f"{name} {ttl_str} IN {rtype} {pri} {wt} {pt} {rdata}")
        elif rtype == "MX":
            pri = rec.priority if rec.priority is not None else 10
            rdata = rec.rdata if rec.rdata.endswith(".") else rec.rdata + "."
            lines.append(f"{name} {ttl_str} IN {rtype} {pri} {rdata}")
        elif rtype in ("CNAME", "NS", "PTR"):
            rdata = rec.rdata if rec.rdata.endswith(".") else rec.rdata + "."
            lines.append(f"{name} {ttl_str} IN {rtype} {rdata}")
        elif rtype == "TXT":
            # Ensure TXT data is quoted
            rdata = rec.rdata if rec.rdata.startswith('"') else f'"{rec.rdata}"'
            lines.append(f"{name} {ttl_str} IN {rtype} {rdata}")
        elif rtype == "CAA":
            lines.append(f"{name} {ttl_str} IN {rtype} {rec.rdata}")
        else:
            lines.append(f"{name} {ttl_str} IN {rtype} {rec.rdata}")

    return "\n".join(lines) + "\n"


# ── named.conf rendering ──────────────────────────────────────────────────────

_SIPSMITH_CONF_HEADER = """\
// SIPsmith-managed BIND9 configuration.
// Generated automatically — do not edit by hand.
"""

_NAMED_CONF_LOCAL_INCLUDE = 'include "/etc/bind/sipsmith.conf";\n'


def render_named_conf(zones: list[DnsZone], settings: DnsSettings) -> str:
    """Render the full /etc/bind/sipsmith.conf content."""
    forwarders: list[str] = json.loads(settings.forwarders or "[]")
    allow_recursion: list[str] = json.loads(settings.allow_recursion or '["any"]')

    parts: list[str] = [_SIPSMITH_CONF_HEADER]

    # ACL for recursion
    acl_entries = "; ".join(allow_recursion) + ";"
    parts.append(f'acl "sipsmith-recursion" {{ {acl_entries} }};\n')

    # Forwarder zone for root domain
    if forwarders:
        fw_list = " ".join(f"{ip};" for ip in forwarders)
        parts.append(
            f'zone "." {{\n'
            f"    type forward;\n"
            f"    forwarders {{ {fw_list} }};\n"
            f"    forward only;\n"
            f"}};\n"
        )

    # Authoritative zone declarations
    for zone in zones:
        if not zone.enabled:
            continue
        if zone.zone_type == ZoneType.primary if hasattr(zone, "zone_type") else True:
            parts.append(
                f'zone "{zone.name}" {{\n'
                f"    type master;\n"
                f'    file "/var/lib/sipsmith/dns/zones/{zone.name}.zone";\n'
                f"    allow-update {{ none; }};\n"
                f"    allow-query {{ any; }};\n"
                f"    allow-transfer {{ none; }};\n"
                f"}};\n"
            )

    # Samba BIND9_DLZ slot (D9)
    if settings.dlz_enabled and settings.dlz_module_path:
        parts.append(
            f'dlz "AD DNS" {{\n'
            f'    database "dlopen {settings.dlz_module_path} /etc/samba/smb.conf";\n'
            f"}};\n"
        )

    return "\n".join(parts)


# ── Auto-PTR helper ───────────────────────────────────────────────────────────


def expand_auto_ptr(ip: str) -> tuple[str, str] | None:
    """
    Given an IPv4 or IPv6 address, return (reverse_zone_name, ptr_record_name).
    E.g. "10.1.2.3" → ("2.1.10.in-addr.arpa", "3")
    Returns None if ip is not a valid address.
    """
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return None

    if isinstance(addr, ipaddress.IPv4Address):
        octets = ip.split(".")
        if len(octets) != 4:  # noqa: PLR2004
            return None
        # /24 reverse zone by convention
        zone = f"{octets[2]}.{octets[1]}.{octets[0]}.in-addr.arpa"
        record_name = octets[3]
        return zone, record_name

    # IPv6: full nibble expansion
    exploded = addr.exploded.replace(":", "")
    nibbles = list(reversed(exploded))
    # /48 reverse zone (12 nibbles)
    zone = ".".join(nibbles[16:]) + ".ip6.arpa"
    record_name = ".".join(nibbles[:16])
    return zone, record_name


# ── Zone type import guard ────────────────────────────────────────────────────
# Keep ZoneType accessible for render_named_conf without circular import
try:
    from sipsmith_dns.models import ZoneType  # noqa: F401
except ImportError:
    pass
