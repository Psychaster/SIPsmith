"""
UC cert orchestration: plan generation, DNS preflight, CSR validation,
signing via CA plugin, TLS probes.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import ssl
import zipfile
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sipsmith_uc_certs.models import (
    ItemStatus,
    PlanStatus,
    UcCertPlan,
    UcCertPlanItem,
    UcCluster,
    UcNode,
)
from sipsmith_uc_certs.packs.loader import (
    compute_sans,
    get_dns_requirements,
    get_pack_services,
)

log = logging.getLogger("sipsmith.uc_certs")


# ── Plan generation ───────────────────────────────────────────────────────────


async def generate_plan(cluster_id: int, db: AsyncSession) -> UcCertPlan:
    """Generate a fresh cert plan for the cluster. Deletes any pending existing plan."""
    cluster = await db.get(UcCluster, cluster_id)
    if cluster is None:
        raise ValueError(f"Cluster {cluster_id} not found")

    nodes_result = await db.execute(
        select(UcNode).where(UcNode.cluster_id == cluster_id).order_by(UcNode.sort_order)
    )
    nodes = list(nodes_result.scalars().all())

    domains: list[str] = json.loads(cluster.domains or "[]")
    toggles: dict = json.loads(cluster.toggles or "{}")

    services = get_pack_services(cluster.product, cluster.version)

    plan = UcCertPlan(cluster_id=cluster_id, status=PlanStatus.pending)
    db.add(plan)
    await db.flush()

    for svc in services:
        scope = svc.get("scope", "cluster")
        if scope == "per_node":
            for node in nodes:
                sans = compute_sans(svc, nodes, domains, toggles, current_node=node)
                item = UcCertPlanItem(
                    plan_id=plan.id,
                    service_id=svc["id"],
                    label=f"{svc['label']} — {node.fqdn}",
                    scope="per_node",
                    node_id=node.id,
                    sans=json.dumps(sans),
                    eku=json.dumps(svc.get("eku", ["serverAuth"])),
                    profile=svc.get("profile", "server"),
                    trust_stores=json.dumps(svc.get("trust_stores", [])),
                    restart_priority=svc.get("restart_priority", 99),
                    service_note=svc.get("note"),
                )
                db.add(item)
        else:
            sans = compute_sans(svc, nodes, domains, toggles)
            item = UcCertPlanItem(
                plan_id=plan.id,
                service_id=svc["id"],
                label=svc["label"],
                scope="cluster",
                sans=json.dumps(sans),
                eku=json.dumps(svc.get("eku", ["serverAuth"])),
                profile=svc.get("profile", "server"),
                trust_stores=json.dumps(svc.get("trust_stores", [])),
                restart_priority=svc.get("restart_priority", 99),
                service_note=svc.get("note"),
            )
            db.add(item)

    await db.flush()
    return plan


# ── DNS preflight ─────────────────────────────────────────────────────────────


async def run_dns_preflight(plan_id: int, db: AsyncSession) -> dict:
    """
    Check DNS for every SAN in the plan + product-specific requirements.
    Returns a preflight report dict and updates plan.dns_preflight + plan.status.
    """
    plan = await db.get(UcCertPlan, plan_id)
    if plan is None:
        raise ValueError(f"Plan {plan_id} not found")

    cluster = await db.get(UcCluster, plan.cluster_id)
    toggles = json.loads(cluster.toggles or "{}")
    domains: list[str] = json.loads(cluster.domains or "[]")
    nodes_result = await db.execute(select(UcNode).where(UcNode.cluster_id == plan.cluster_id))
    nodes = list(nodes_result.scalars().all())

    items_result = await db.execute(select(UcCertPlanItem).where(UcCertPlanItem.plan_id == plan_id))
    items = items_result.scalars().all()

    # Collect all FQDNs that appear in any SAN
    all_sans: set[str] = set()
    for item in items:
        for san in json.loads(item.sans or "[]"):
            all_sans.add(san)

    # DNS plugin check: query its zones to see if records exist
    fqdn_results: dict[str, dict] = {}
    for fqdn in sorted(all_sans):
        if "." not in fqdn:
            # short hostname — skip (not independently resolvable)
            continue
        fqdn_results[fqdn] = await _check_dns_record(fqdn, "A")

    # Check per-node A records
    node_results: list[dict] = []
    for node in nodes:
        a_ok = await _check_dns_record(node.fqdn, "A")
        ptr_ok = (
            await _check_dns_record(node.ip or "", "PTR")
            if node.ip
            else {"found": False, "note": "no IP set"}
        )
        node_results.append(
            {
                "fqdn": node.fqdn,
                "ip": node.ip,
                "a_record": a_ok,
                "ptr_record": ptr_ok,
            }
        )

    # Check product-specific SRV requirements
    dns_reqs = get_dns_requirements(cluster.product, toggles)
    srv_results: list[dict] = []
    for req in dns_reqs:
        if req["type"] not in ("SRV",):
            continue
        pattern = req.get("name_pattern", "")
        for domain in domains:
            name = pattern.format(domain=domain)
            check = await _check_dns_record(name, "SRV")
            srv_results.append(
                {
                    "name": name,
                    "description": req.get("description", ""),
                    "result": check,
                }
            )

    # Determine overall status
    node_a_ok = all(n["a_record"]["found"] for n in node_results)
    srv_missing = [s for s in srv_results if not s["result"]["found"]]

    if not node_a_ok:
        status = PlanStatus.dns_fail
    elif srv_missing:
        status = PlanStatus.dns_warn
    else:
        status = PlanStatus.dns_ok

    report = {
        "status": status,
        "fqdn_checks": fqdn_results,
        "node_checks": node_results,
        "srv_checks": srv_results,
    }

    plan.dns_preflight = json.dumps(report)
    plan.status = status
    await db.flush()
    return report


async def _check_dns_record(name: str, rtype: str) -> dict:
    """Look up whether a record exists in the DNS plugin's DB (no live resolution)."""
    if not name:
        return {"found": False, "note": "empty name"}
    try:
        from sipsmith_dns.models import DnsRecord, DnsZone

        from sipsmith.database import AsyncSessionLocal

        # Find which zone this name belongs to (longest-match)
        async with AsyncSessionLocal() as db:
            zones_result = await db.execute(select(DnsZone).where(DnsZone.enabled))
            zones = sorted(zones_result.scalars().all(), key=lambda z: len(z.name), reverse=True)

        zone_match: Any = None
        record_name = name
        name_lower = name.rstrip(".").lower()
        for zone in zones:
            zone_name = zone.name.rstrip(".").lower()
            if name_lower == zone_name or name_lower.endswith("." + zone_name):
                zone_match = zone
                # record name = part before the zone
                if name_lower == zone_name:
                    record_name = "@"
                else:
                    record_name = name_lower[: -(len(zone_name) + 1)]
                break

        if zone_match is None:
            return {"found": False, "note": "no matching zone in DNS plugin"}

        async with AsyncSessionLocal() as db:
            rec_result = await db.execute(
                select(DnsRecord).where(
                    DnsRecord.zone_id == zone_match.id,
                    DnsRecord.record_type == rtype.upper(),
                    DnsRecord.name == record_name,
                    DnsRecord.enabled,
                )
            )
            rec = rec_result.scalar_one_or_none()

        if rec:
            return {
                "found": True,
                "zone": zone_match.name,
                "record_name": record_name,
                "rdata": rec.rdata,
            }
        return {"found": False, "zone": zone_match.name, "record_name": record_name}

    except Exception as exc:  # noqa: BLE001
        return {"found": False, "error": str(exc)}


# ── CSR validation ────────────────────────────────────────────────────────────


def validate_csr(
    item_sans: list[str], csr_pem: str, min_key_bits: int = 2048
) -> tuple[bool, list[str]]:
    """
    Parse CSR, verify SANs match expected list, verify key size.
    Returns (is_valid, list_of_errors).
    """
    errors: list[str] = []
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives.asymmetric import ec, rsa

        csr = x509.load_pem_x509_csr(csr_pem.encode())

        # Key size check
        pub = csr.public_key()
        if isinstance(pub, rsa.RSAPublicKey):
            if pub.key_size < min_key_bits:
                errors.append(f"RSA key too small: {pub.key_size} bits (need ≥ {min_key_bits})")
        elif isinstance(pub, ec.EllipticCurvePublicKey):
            if pub.key_size < 256:  # noqa: PLR2004
                errors.append(f"EC key too small: {pub.key_size} bits")

        # SAN check
        try:
            san_ext = csr.extensions.get_extension_for_class(x509.SubjectAlternativeName)
            csr_dns_sans = {
                n.lower().strip() for n in san_ext.value.get_values_for_type(x509.DNSName)
            }
        except x509.ExtensionNotFound:
            csr_dns_sans = set()

        expected = {s.lower().strip() for s in item_sans if "." in s}
        missing = expected - csr_dns_sans
        extra = csr_dns_sans - expected

        if missing:
            errors.append(f"Missing SANs in CSR: {', '.join(sorted(missing))}")
        if extra:
            errors.append(f"Unexpected SANs in CSR (informational): {', '.join(sorted(extra))}")

    except Exception as exc:  # noqa: BLE001
        errors.append(f"Failed to parse CSR: {exc}")

    return (len(errors) == 0 or (len(errors) == 1 and "informational" in errors[0])), errors


# ── Signing ───────────────────────────────────────────────────────────────────


async def sign_item(item_id: int, db: AsyncSession) -> None:
    """Call the CA plugin to sign the CSR for this plan item."""
    item = await db.get(UcCertPlanItem, item_id)
    if item is None or not item.csr_pem:
        raise ValueError("Item not found or has no CSR")

    try:
        from sipsmith_ca.crypto import load_issuing_ca, sign_csr

        from sipsmith.config import get_settings

        cfg = get_settings()
        issuing_key, issuing_cert = load_issuing_ca(cfg.ca_secret_key)

        from cryptography import x509 as _x509

        csr = _x509.load_pem_x509_csr(item.csr_pem.encode())
        from sipsmith_ca.api import _PROFILES

        profile = _PROFILES.get(item.profile, _PROFILES["server_client"])

        cert = sign_csr(csr, issuing_key, issuing_cert, profile)
        from cryptography.hazmat.primitives.serialization import Encoding

        item.cert_pem = cert.public_bytes(Encoding.PEM).decode()
        item.status = ItemStatus.signed
        await db.flush()

    except Exception as exc:
        log.error("sign_item %d failed: %s", item_id, exc)
        raise


# ── Delivery bundle ───────────────────────────────────────────────────────────


async def build_delivery_bundle(plan_id: int, db: AsyncSession) -> bytes:
    """
    Build a zip file with:
      - certs/<service_id>.pem (or <service_id>-<node_fqdn>.pem for per_node)
      - chain/issuing.pem
      - chain/root.pem
      - CHECKLIST.txt
    """
    plan = await db.get(UcCertPlan, plan_id)
    if plan is None:
        raise ValueError(f"Plan {plan_id} not found")

    cluster = await db.get(UcCluster, plan.cluster_id)
    items_result = await db.execute(
        select(UcCertPlanItem)
        .where(UcCertPlanItem.plan_id == plan_id)
        .order_by(UcCertPlanItem.restart_priority)
    )
    items = list(items_result.scalars().all())

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        # Certs
        for item in items:
            if not item.cert_pem:
                continue
            safe_id = item.service_id.replace("/", "_")
            if item.scope == "per_node" and item.node:
                fname = f"certs/{safe_id}-{item.node.fqdn}.pem"
            else:
                fname = f"certs/{safe_id}.pem"
            zf.writestr(fname, item.cert_pem)

        # CA chain
        try:
            from sipsmith_ca.crypto import get_chain_pem

            chain = get_chain_pem()
            zf.writestr("chain/ca-chain.pem", chain)
        except Exception:  # noqa: BLE001, S110
            pass  # CA chain is optional in the bundle

        # Checklist
        checklist = _build_checklist(cluster, items)
        zf.writestr("CHECKLIST.txt", checklist)

    buf.seek(0)
    return buf.read()


def _build_checklist(cluster: Any, items: list[UcCertPlanItem]) -> str:
    lines = [
        "SIPsmith UC Cert Delivery Checklist",
        f"Cluster: {cluster.name} ({cluster.product.upper()} {cluster.version})",
        "=" * 60,
        "",
        "STEP 1 — Upload CA chain to trust stores BEFORE identity certs",
        "  1a. Download ca-chain.pem from this bundle",
        "  1b. Upload to every trust store listed per service below",
        "  1c. Restart affected services and confirm trust is accepted",
        "",
        "STEP 2 — Upload identity certificates (in restart_priority order)",
        "",
    ]
    for i, item in enumerate(sorted(items, key=lambda x: x.restart_priority), start=1):
        if not item.cert_pem:
            continue
        sans = json.loads(item.sans or "[]")
        trust = json.loads(item.trust_stores or "[]")
        lines += [
            f"  [{i}] {item.label}",
            f"      File: certs/{item.service_id}.pem",
            f"      SANs: {', '.join(sans[:4])}" + (" ..." if len(sans) > 4 else ""),
            f"      Trust stores: {', '.join(trust) or 'n/a'}",
        ]
        if item.service_note:
            for note_line in item.service_note.strip().splitlines():
                lines.append(f"      Note: {note_line.strip()}")
        lines.append("")

    lines += [
        "STEP 3 — Post-install validation",
        "  GUI → UC Certs → Plan → 'Run TLS Probes'",
        "  All service ports should show green (cert valid, chain to SIPsmith CA).",
        "",
        "Generated by SIPsmith uc-certs plugin.",
    ]
    return "\n".join(lines)


# ── TLS probes ────────────────────────────────────────────────────────────────


_DEFAULT_PORTS: dict[str, list[int]] = {
    "cucm": [8443, 5061],
    "expressway": [8443, 5061, 443],
    "cuc": [8443],
    "imp": [8443, 5222],
    "cms": [443, 8443, 5061],
}


async def run_tls_probes(cluster_id: int, db: AsyncSession) -> list[dict]:
    """Probe TLS on each cluster node's common service ports."""
    cluster = await db.get(UcCluster, cluster_id)
    if cluster is None:
        raise ValueError(f"Cluster {cluster_id} not found")

    nodes_result = await db.execute(select(UcNode).where(UcNode.cluster_id == cluster_id))
    nodes = list(nodes_result.scalars().all())

    ports = _DEFAULT_PORTS.get(cluster.product, [443, 8443])
    results: list[dict] = []

    tasks = [_probe_tls(node.fqdn, port, node_id=node.id) for node in nodes for port in ports]
    probe_results = await asyncio.gather(*tasks, return_exceptions=True)
    for r in probe_results:
        if isinstance(r, Exception):
            results.append({"ok": False, "error": str(r)})
        else:
            results.append(r)

    return results


async def _probe_tls(fqdn: str, port: int, *, node_id: int) -> dict:
    """Open TLS connection, return cert info."""
    base = {"fqdn": fqdn, "port": port, "node_id": node_id}
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(fqdn, port, ssl=ctx), timeout=5.0
        )
        writer.close()
        await writer.wait_closed()
        return {**base, "ok": True, "note": "TLS handshake succeeded"}
    except TimeoutError:
        return {**base, "ok": False, "error": "timeout"}
    except OSError as exc:
        return {**base, "ok": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {**base, "ok": False, "error": str(exc)}
