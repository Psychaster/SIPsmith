"""Chrony config rendering tests."""

from __future__ import annotations

from sipsmith.core.chrony import render_chrony_conf


def test_render_chrony_conf_contains_stratum():
    conf = render_chrony_conf(stratum=4, allow_networks=["10.0.0.0/8"])
    assert "local stratum 4" in conf


def test_render_chrony_conf_has_all_networks():
    networks = ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"]
    conf = render_chrony_conf(stratum=3, allow_networks=networks)
    for net in networks:
        assert f"allow {net}" in conf


def test_render_chrony_conf_has_managed_comment():
    conf = render_chrony_conf(3, [])
    assert "Managed by SIPsmith" in conf


def test_render_chrony_conf_no_external_refs():
    conf = render_chrony_conf(3, ["10.0.0.0/8"])
    # Must not reference any external NTP pool or server
    assert "pool.ntp.org" not in conf
    assert "time.cloudflare.com" not in conf
    assert "time.google.com" not in conf
