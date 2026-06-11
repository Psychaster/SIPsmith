"""Pack engine: load YAML knowledge packs, match version, compute SANs."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

_PACKS_DIR = Path(__file__).parent


def _load_yaml(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def list_packs() -> list[dict]:
    """Return summary of all available packs (id, display_name, version_floor)."""
    result = []
    for p in sorted(_PACKS_DIR.glob("*.yaml")):
        d = _load_yaml(p)
        result.append(
            {
                "product": d.get("product", p.stem),
                "display_name": d.get("display_name", p.stem),
                "version_floor": d.get("version_floor", "0"),
            }
        )
    return result


def load_pack(product: str) -> dict:
    """Load and return the raw pack dict for a product."""
    path = _PACKS_DIR / f"{product}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"No pack for product {product!r}")
    return _load_yaml(path)


def _version_tuple(v: str) -> tuple[int, ...]:
    """Convert version string to comparable tuple. 'X14.0' → (14, 0)."""
    # Strip leading non-digits
    cleaned = re.sub(r"^[Xx]", "", v)
    parts = re.split(r"[.\-]", cleaned)
    result = []
    for p in parts:
        try:
            result.append(int(p))
        except ValueError:
            break
    return tuple(result) if result else (0,)


def _version_gte(v: str, floor: str) -> bool:
    return _version_tuple(v) >= _version_tuple(floor)


def get_pack_services(product: str, version: str) -> list[dict]:
    """
    Return the list of service rule dicts applicable to this product + version.
    Applies version_overrides on top of base service fields.
    """
    pack = load_pack(product)
    floor = pack.get("version_floor", "0")
    if not _version_gte(version, floor):
        raise ValueError(f"Version {version!r} is below {product} floor {floor!r}")

    services: list[dict] = []
    for svc in pack.get("services", []):
        merged = dict(svc)
        # Apply matching version_overrides
        for range_str, overrides in svc.get("version_overrides", {}).items():
            # range_str like ">=14.0" or "14.0-15.x"
            if _range_matches(version, range_str):
                merged.update(overrides)
        merged.pop("version_overrides", None)
        services.append(merged)
    return services


def _range_matches(version: str, range_str: str) -> bool:
    """Evaluate a simple version range like '>=14.0' or '12.5-14.x'."""
    v = _version_tuple(version)
    range_str = range_str.strip()
    if range_str.startswith(">="):
        return v >= _version_tuple(range_str[2:])
    if range_str.startswith(">"):
        return v > _version_tuple(range_str[1:])
    if range_str.startswith("<="):
        return v <= _version_tuple(range_str[2:])
    if range_str.startswith("<"):
        return v < _version_tuple(range_str[1:])
    if "-" in range_str:
        lo, hi = range_str.split("-", 1)
        return _version_tuple(lo) <= v <= _version_tuple(hi)
    # Exact
    return _version_tuple(range_str) == v


def get_dns_requirements(product: str, toggles: dict) -> list[dict]:
    """Return list of DNS requirement dicts for this product + active toggles."""
    pack = load_pack(product)
    reqs: list[dict] = list(pack.get("dns_requirements", []))
    for toggle_name, toggle_reqs in pack.get("dns_if_toggle", {}).items():
        if toggles.get(toggle_name):
            reqs.extend(toggle_reqs)
    return reqs


def compute_sans(
    service_rule: dict,
    nodes: list[Any],
    domains: list[str],
    toggles: dict,
    current_node: Any | None = None,
) -> list[str]:
    """
    Compute the SAN list for a plan item.

    san_sources items:
      all_node_fqdns        → every node's fqdn
      all_node_hostnames    → first label of each node fqdn
      node_fqdn             → current_node.fqdn (per_node scope)
      node_hostname         → first label of current_node.fqdn
      publisher_fqdn        → nodes[0].fqdn (by sort_order)
      cluster_domains       → the cluster's enterprise domains list
      mra_domains           → toggles["mra_domains"] list (if set)
    """
    seen: set[str] = set()
    result: list[str] = []

    def add(v: str) -> None:
        v = v.strip().lower()
        if v and v not in seen:
            seen.add(v)
            result.append(v)

    for source in service_rule.get("san_sources", []):
        if source == "all_node_fqdns":
            for n in nodes:
                add(n.fqdn)
        elif source == "all_node_hostnames":
            for n in nodes:
                add(n.fqdn.split(".")[0])
        elif source == "node_fqdn":
            if current_node:
                add(current_node.fqdn)
        elif source == "node_hostname":
            if current_node:
                add(current_node.fqdn.split(".")[0])
        elif source == "publisher_fqdn":
            if nodes:
                add(nodes[0].fqdn)
        elif source == "cluster_domains":
            for d in domains:
                add(d)
        elif source == "mra_domains":
            for d in toggles.get("mra_domains", []):
                add(d)

    return result
