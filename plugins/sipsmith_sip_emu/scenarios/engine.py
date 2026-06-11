"""Async scenario executor for the SIP Endpoint Emulator."""

from __future__ import annotations

import asyncio
import datetime
import io
import logging
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sipsmith_sip_emu.models import EndpointCall, Scenario, ScenarioRun
from sipsmith_sip_emu.scenarios.schema import ScenarioDefinition, ScenarioStep

if TYPE_CHECKING:
    from sipsmith_sip_emu.worker.manager import WorkerManager

log = logging.getLogger("sipsmith.sip_emu.scenario_engine")

# Poll interval when waiting for state changes
_POLL_INTERVAL = 0.5


async def run_scenario(
    scenario_id: int,
    run_id: int,
    db: AsyncSession,
    manager: WorkerManager,
) -> None:
    """Execute a scenario and update the ScenarioRun row in the database."""
    buf = io.StringIO()

    def _log(msg: str) -> None:
        ts = datetime.datetime.now(datetime.UTC).isoformat()
        line = f"[{ts}] {msg}"
        log.info(line)
        buf.write(line + "\n")

    async def _save_run(
        run: ScenarioRun,
        *,
        status: str | None = None,
        ended: bool = False,
    ) -> None:
        if status:
            run.status = status
        if ended:
            run.ended_at = datetime.datetime.now(datetime.UTC)
        run.log_output = buf.getvalue()
        await db.commit()

    # ── Load run and scenario ────────────────────────────────────────────

    run = await db.get(ScenarioRun, run_id)
    if run is None:
        log.error("ScenarioRun %s not found", run_id)
        return

    scenario = await db.get(Scenario, scenario_id)
    if scenario is None:
        run.status = "failed"
        run.log_output = f"Scenario {scenario_id} not found\n"
        await db.commit()
        return

    try:
        definition = ScenarioDefinition.from_yaml(scenario.yaml_content)
    except Exception as exc:  # noqa: BLE001
        run.status = "failed"
        run.log_output = f"YAML parse error: {exc}\n"
        await db.commit()
        return

    run.status = "running"
    run.started_at = datetime.datetime.now(datetime.UTC)
    run.steps_total = len(definition.steps)
    await db.commit()

    _log(f"Scenario '{definition.name}' started — {len(definition.steps)} step(s)")

    # Map endpoint name → endpoint DB id (lazy lookup cache)
    _ep_name_to_id: dict[str, int] = {}
    # Track active pjsip_call_id per endpoint name (updated via DB poll)
    _ep_active_call: dict[str, str] = {}

    async def _resolve_ep_id(ep_name: str) -> int | None:
        if ep_name in _ep_name_to_id:
            return _ep_name_to_id[ep_name]
        from sipsmith_sip_emu.models import Endpoint

        result = await db.execute(select(Endpoint.id).where(Endpoint.name == ep_name))
        row = result.scalar_one_or_none()
        if row is not None:
            _ep_name_to_id[ep_name] = row
        return row

    async def _active_call_for_ep(ep_name: str) -> str | None:
        """Return pjsip_call_id of the most recent non-disconnected call."""
        ep_id = await _resolve_ep_id(ep_name)
        if ep_id is None:
            return None
        result = await db.execute(
            select(EndpointCall.pjsip_call_id)
            .where(
                EndpointCall.endpoint_id == ep_id,
                EndpointCall.call_state != "disconnected",
                EndpointCall.pjsip_call_id.is_not(None),
            )
            .order_by(EndpointCall.started_at.desc())
            .limit(1)
        )
        row = result.scalar_one_or_none()
        return row

    async def _wait_for_call_state(
        ep_name: str,
        desired_state: str,
        timeout: float,
    ) -> bool:
        """Poll DB until call reaches desired_state or timeout expires."""
        ep_id = await _resolve_ep_id(ep_name)
        if ep_id is None:
            return False
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            result = await db.execute(
                select(EndpointCall.call_state, EndpointCall.pjsip_call_id)
                .where(
                    EndpointCall.endpoint_id == ep_id,
                    EndpointCall.call_state != "disconnected",
                )
                .order_by(EndpointCall.started_at.desc())
                .limit(1)
            )
            row = result.one_or_none()
            if row and row[0] == desired_state:
                if row[1]:
                    _ep_active_call[ep_name] = row[1]
                return True
            await asyncio.sleep(_POLL_INTERVAL)
        return False

    # ── Execute steps ────────────────────────────────────────────────────

    steps_passed = 0
    steps_failed = 0

    for idx, step in enumerate(definition.steps):
        step_num = idx + 1
        _log(f"Step {step_num}/{len(definition.steps)}: {step.action}")

        try:
            await _execute_step(
                step=step,
                manager=manager,
                resolve_ep_id=_resolve_ep_id,
                active_call_for_ep=_active_call_for_ep,
                wait_for_call_state=_wait_for_call_state,
                ep_active_call=_ep_active_call,
                db=db,
                log_fn=_log,
            )
            steps_passed += 1
            _log(f"Step {step_num}: OK")
        except Exception as exc:  # noqa: BLE001
            steps_failed += 1
            _log(f"Step {step_num}: FAILED — {exc}")

        run.steps_passed = steps_passed
        run.steps_failed = steps_failed
        await _save_run(run)

    final_status = "passed" if steps_failed == 0 else "failed"
    _log(f"Scenario finished — {steps_passed} passed, {steps_failed} failed → {final_status}")
    await _save_run(run, status=final_status, ended=True)


async def _execute_step(  # noqa: PLR0912, PLR0913, C901
    step: ScenarioStep,
    manager: WorkerManager,
    resolve_ep_id: object,
    active_call_for_ep: object,
    wait_for_call_state: object,
    ep_active_call: dict[str, str],
    db: AsyncSession,
    log_fn: object,
) -> None:
    """Dispatch a single scenario step."""
    action = step.action

    if action == "wait":
        await asyncio.sleep(step.duration or 0)

    elif action == "call":
        ep_name = step.endpoint or ""
        ep_id = await resolve_ep_id(ep_name)  # type: ignore[operator]
        if ep_id is None:
            raise ValueError(f"Endpoint '{ep_name}' not found")
        await manager.send_command(  # type: ignore[union-attr]
            "call", {"ep_id": ep_id, "dest_uri": step.to}
        )
        if step.wait_for in ("connected", "answered"):
            ok = await wait_for_call_state(ep_name, "confirmed", step.timeout)  # type: ignore[operator]
            if not ok:
                raise TimeoutError(
                    f"Call from '{ep_name}' did not reach 'confirmed' within {step.timeout}s"
                )

    elif action == "answer":
        ep_name = step.endpoint or ""
        pjsip_id = await active_call_for_ep(ep_name)  # type: ignore[operator]
        if pjsip_id is None:
            raise ValueError(f"No active call to answer for endpoint '{ep_name}'")
        await manager.send_command("answer", {"call_id": pjsip_id})  # type: ignore[union-attr]

    elif action == "hangup":
        ep_name = step.endpoint or ""
        pjsip_id = ep_active_call.get(ep_name) or await active_call_for_ep(ep_name)  # type: ignore[operator]
        if pjsip_id is None:
            raise ValueError(f"No active call to hang up for endpoint '{ep_name}'")
        await manager.send_command("hangup", {"call_id": pjsip_id})  # type: ignore[union-attr]

    elif action == "hold":
        ep_name = step.endpoint or ""
        pjsip_id = ep_active_call.get(ep_name) or await active_call_for_ep(ep_name)  # type: ignore[operator]
        if pjsip_id is None:
            raise ValueError(f"No active call for endpoint '{ep_name}'")
        await manager.send_command("hold", {"call_id": pjsip_id})  # type: ignore[union-attr]

    elif action == "unhold":
        ep_name = step.endpoint or ""
        pjsip_id = ep_active_call.get(ep_name) or await active_call_for_ep(ep_name)  # type: ignore[operator]
        if pjsip_id is None:
            raise ValueError(f"No active call for endpoint '{ep_name}'")
        await manager.send_command("unhold", {"call_id": pjsip_id})  # type: ignore[union-attr]

    elif action == "mute_audio":
        ep_name = step.endpoint or ""
        pjsip_id = ep_active_call.get(ep_name) or await active_call_for_ep(ep_name)  # type: ignore[operator]
        if pjsip_id is None:
            raise ValueError(f"No active call for endpoint '{ep_name}'")
        muted_audio = step.muted if step.muted is not None else True
        await manager.send_command(  # type: ignore[union-attr]
            "mute",
            {"call_id": pjsip_id, "audio": muted_audio, "video": False},
        )

    elif action == "mute_video":
        ep_name = step.endpoint or ""
        pjsip_id = ep_active_call.get(ep_name) or await active_call_for_ep(ep_name)  # type: ignore[operator]
        if pjsip_id is None:
            raise ValueError(f"No active call for endpoint '{ep_name}'")
        muted_video = step.muted if step.muted is not None else True
        await manager.send_command(  # type: ignore[union-attr]
            "mute",
            {"call_id": pjsip_id, "audio": False, "video": muted_video},
        )

    elif action == "dtmf":
        ep_name = step.endpoint or ""
        pjsip_id = ep_active_call.get(ep_name) or await active_call_for_ep(ep_name)  # type: ignore[operator]
        if pjsip_id is None:
            raise ValueError(f"No active call for endpoint '{ep_name}'")
        await manager.send_command(  # type: ignore[union-attr]
            "dtmf",
            {"call_id": pjsip_id, "digits": step.digits or ""},
        )

    elif action == "assert_cdr":
        ep_name = step.endpoint or ""
        ep_id = await resolve_ep_id(ep_name)  # type: ignore[operator]
        if ep_id is None:
            raise ValueError(f"Endpoint '{ep_name}' not found")
        # Poll EndpointCall for matching cause_code within wait_timeout
        deadline = asyncio.get_event_loop().time() + step.wait_timeout
        matched = False
        while asyncio.get_event_loop().time() < deadline:
            result = await db.execute(
                select(EndpointCall.cause_code)
                .where(
                    EndpointCall.endpoint_id == ep_id,
                    EndpointCall.call_state == "disconnected",
                )
                .order_by(EndpointCall.ended_at.desc())
                .limit(1)
            )
            row = result.scalar_one_or_none()
            if row is not None and (step.cause_code is None or row == step.cause_code):
                matched = True
                break
            await asyncio.sleep(_POLL_INTERVAL)
        if not matched:
            raise AssertionError(
                f"assert_cdr: expected cause_code={step.cause_code} for '{ep_name}' "
                f"within {step.wait_timeout}s"
            )

    else:
        raise ValueError(f"Unrecognised action: {action}")
