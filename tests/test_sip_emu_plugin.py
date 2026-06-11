"""Phase 6 SIP Endpoint Emulator tests."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "plugins"))


# ── Scenario schema validation ────────────────────────────────────────────────


def test_scenario_parse_valid():
    from sipsmith_sip_emu.scenarios.schema import ScenarioDefinition

    yaml_content = """
name: Basic call test
description: emu-001 calls 2000, 30s, normal release
steps:
  - action: call
    endpoint: emu-001
    to: "sip:2000@cucm.lab"
    wait_for: connected
    timeout: 15
  - action: wait
    duration: 30
  - action: hangup
    endpoint: emu-001
"""
    sc = ScenarioDefinition.from_yaml(yaml_content)
    assert sc.name == "Basic call test"
    assert len(sc.steps) == 3
    assert sc.steps[0].action == "call"
    assert sc.steps[0].to == "sip:2000@cucm.lab"
    assert sc.steps[1].duration == 30


def test_scenario_parse_mute():
    from sipsmith_sip_emu.scenarios.schema import ScenarioDefinition

    yaml_content = """
name: Mute test
steps:
  - action: call
    endpoint: emu-001
    to: "sip:2001@cucm.lab"
    wait_for: connected
  - action: mute_audio
    endpoint: emu-001
    muted: true
  - action: wait
    duration: 5
  - action: mute_audio
    endpoint: emu-001
    muted: false
  - action: hangup
    endpoint: emu-001
"""
    sc = ScenarioDefinition.from_yaml(yaml_content)
    mute_step = sc.steps[1]
    assert mute_step.action == "mute_audio"
    assert mute_step.muted is True


def test_scenario_validate_missing_to():
    from sipsmith_sip_emu.scenarios.schema import ScenarioDefinition

    yaml_content = """
name: Bad scenario
steps:
  - action: call
    endpoint: emu-001
"""
    sc = ScenarioDefinition.from_yaml(yaml_content)
    errors = sc.validate_actions()
    assert any("to" in e for e in errors), f"Expected missing 'to' error, got: {errors}"


def test_scenario_validate_unknown_action():
    from sipsmith_sip_emu.scenarios.schema import ScenarioDefinition

    yaml_content = """
name: Bad scenario
steps:
  - action: fly_to_moon
    endpoint: emu-001
"""
    sc = ScenarioDefinition.from_yaml(yaml_content)
    errors = sc.validate_actions()
    assert any("fly_to_moon" in e for e in errors)


def test_scenario_validate_dtmf_missing_digits():
    from sipsmith_sip_emu.scenarios.schema import ScenarioDefinition

    yaml_content = """
name: Bad dtmf
steps:
  - action: dtmf
    endpoint: emu-001
"""
    sc = ScenarioDefinition.from_yaml(yaml_content)
    errors = sc.validate_actions()
    assert any("digits" in e for e in errors)


def test_scenario_assert_cdr_step():
    from sipsmith_sip_emu.scenarios.schema import ScenarioDefinition

    yaml_content = """
name: CDR assertion
steps:
  - action: assert_cdr
    endpoint: emu-001
    cause_code: 16
    wait_timeout: 30
"""
    sc = ScenarioDefinition.from_yaml(yaml_content)
    errors = sc.validate_actions()
    assert not errors
    assert sc.steps[0].cause_code == 16


# ── Worker protocol ───────────────────────────────────────────────────────────


def test_protocol_command_parse():
    import json

    from sipsmith_sip_emu.worker.protocol import Command

    line = json.dumps(
        {"id": "42", "method": "call", "params": {"ep_id": 1, "dest_uri": "sip:2000@cucm.lab"}}
    )
    cmd = Command.from_json(line)
    assert cmd.id == "42"
    assert cmd.method == "call"
    assert cmd.params["ep_id"] == 1


def test_protocol_event_serialise():
    import json

    from sipsmith_sip_emu.worker.protocol import RegStateEvent

    ev = RegStateEvent(ep_id=1, state="registered", expires=3600, contact="sip:x@y")
    data = json.loads(ev.to_json())
    assert data["event"] == "reg_state"
    assert data["state"] == "registered"
    assert data["ep_id"] == 1


def test_protocol_sip_msg_event():
    import json

    from sipsmith_sip_emu.worker.protocol import SipMsgEvent

    ev = SipMsgEvent(
        pjsip_call_id="abc123",
        direction="tx",
        method="INVITE",
        status_code=None,
        from_participant="emu-001",
        to_participant="CUCM",
        cseq="1 INVITE",
    )
    d = json.loads(ev.to_json())
    assert d["event"] == "sip_msg"
    assert d["method"] == "INVITE"
    assert d["direction"] == "tx"


def test_protocol_rtp_stats_event():
    import json

    from sipsmith_sip_emu.worker.protocol import RtpStatsEvent

    ev = RtpStatsEvent(
        pjsip_call_id="abc",
        audio_tx_pkt=1000,
        audio_rx_pkt=998,
        jitter_ms=2.1,
        rtt_ms=15.0,
        loss_pct=0.2,
        mos=4.2,
    )
    d = json.loads(ev.to_json())
    assert d["mos"] == 4.2
    assert d["jitter_ms"] == 2.1


# ── Models ────────────────────────────────────────────────────────────────────


def test_models_importable():
    from sipsmith_sip_emu.models import (
        Endpoint,
        EndpointCall,
        RtpStatsSample,
        Scenario,
        ScenarioRun,
        SipEvent,
    )

    assert "endpoint" in Endpoint.__tablename__
    assert "call" in EndpointCall.__tablename__
    assert "event" in SipEvent.__tablename__
    assert "rtp" in RtpStatsSample.__tablename__
    assert "scenario" in Scenario.__tablename__
    assert "run" in ScenarioRun.__tablename__


# ── Air-gap: no external URLs ─────────────────────────────────────────────────


def test_no_external_urls():
    import re

    pkg = Path(__file__).parent.parent / "plugins" / "sipsmith_sip_emu"
    pattern = re.compile(r"https?://(?!localhost|127\.0\.0\.1|0\.0\.0\.0)")
    violations = []
    for pyfile in pkg.rglob("*.py"):
        for i, line in enumerate(pyfile.read_text().splitlines(), 1):
            if pattern.search(line) and "noqa" not in line:
                violations.append(f"{pyfile.relative_to(pkg)}:{i}: {line.strip()}")
    assert not violations, "External URL(s) found:\n" + "\n".join(violations)
