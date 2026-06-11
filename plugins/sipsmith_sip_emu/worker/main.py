"""Worker subprocess entry point for the SIP Endpoint Emulator.

Run as: python -m sipsmith_sip_emu.worker.main [--db-url <url>]

Protocol:
- Reads JSON-line commands from stdin.
- Writes JSON-line events to stdout (flush after every line).
- Stderr is reserved for Python tracebacks (not parsed by the control plane).
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import sys
import threading
import time
from typing import Any

# ---------------------------------------------------------------------------
# Attempt pjsua2 import — fail fast if unavailable
# ---------------------------------------------------------------------------

try:
    import pjsua2 as pj  # type: ignore[import-untyped]
except ImportError:
    print(json.dumps({"event": "error", "message": "pjsua2 not available"}), flush=True)
    sys.exit(1)

from sipsmith_sip_emu.worker.endpoint import EndpointFarm

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SIP_UDP_PORT = 5080
SIP_TCP_PORT = 5080
RTP_STATS_INTERVAL = 5.0  # seconds between per-call RTP snapshots


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def emit(event: dict[str, Any]) -> None:
    """Print a JSON event line to stdout immediately."""
    print(json.dumps(event), flush=True)


def _stdin_reader(cmd_queue: queue.Queue[str]) -> None:
    """Daemon thread: read stdin line-by-line and enqueue each line."""
    for raw in sys.stdin:
        line = raw.strip()
        if line:
            cmd_queue.put(line)
    # EOF — signal shutdown
    cmd_queue.put(json.dumps({"id": "0", "method": "shutdown", "params": {}}))


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------


def _handle_command(
    method: str,
    params: dict[str, Any],
    farm: EndpointFarm,
    running_flag: list[bool],
) -> None:
    try:
        if method == "register":
            farm.register(params)

        elif method == "unregister":
            ep_id = int(params["ep_id"])
            farm.unregister(ep_id)

        elif method == "call":
            ep_id = int(params["ep_id"])
            dest_uri: str = params["dest_uri"]
            import datetime

            pjsip_call_id = farm.make_call(ep_id, dest_uri)
            emit(
                {
                    "event": "call_created",
                    "ep_id": ep_id,
                    "pjsip_call_id": pjsip_call_id,
                    "direction": "outbound",
                    "remote_uri": dest_uri,
                    "ts": datetime.datetime.now(datetime.UTC).isoformat(),
                }
            )

        elif method == "hangup":
            farm.hangup(str(params["call_id"]))

        elif method == "hold":
            farm.hold(str(params["call_id"]))

        elif method == "unhold":
            farm.unhold(str(params["call_id"]))

        elif method == "mute":
            farm.mute(
                str(params["call_id"]),
                audio=bool(params.get("audio", False)),
                video=bool(params.get("video", False)),
            )

        elif method == "dtmf":
            farm.dtmf(str(params["call_id"]), str(params["digits"]))

        elif method == "status":
            emit(
                {
                    "event": "worker_ready",
                    "account_count": len(farm.accounts),
                }
            )

        elif method == "shutdown":
            running_flag[0] = False

        else:
            emit({"event": "error", "message": f"Unknown method: {method}"})

    except Exception as exc:  # noqa: BLE001
        emit({"event": "error", "message": str(exc)})


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="SIP Endpoint Emulator worker")
    parser.add_argument("--db-url", default=os.environ.get("SIPSMITH_DB_URL", ""))
    parser.parse_args()

    # ── pjsua2 Endpoint init ─────────────────────────────────────────────

    ep = pj.Endpoint()
    ep_cfg = pj.EpConfig()
    ep_cfg.uaConfig.threadCnt = 1
    ep_cfg.logConfig.level = 1
    ep_cfg.logConfig.consoleLevel = 0  # suppress pjsua2 logs to stderr

    ep.libCreate()
    ep.libInit(ep_cfg)

    # UDP transport
    try:
        udp_cfg = pj.TransportConfig()
        udp_cfg.port = SIP_UDP_PORT
        ep.transportCreate(pj.PJSIP_TRANSPORT_UDP, udp_cfg)
    except Exception as exc:  # noqa: BLE001
        emit({"event": "error", "message": f"UDP transport failed: {exc}"})

    # TCP transport
    try:
        tcp_cfg = pj.TransportConfig()
        tcp_cfg.port = SIP_TCP_PORT
        ep.transportCreate(pj.PJSIP_TRANSPORT_TCP, tcp_cfg)
    except Exception as exc:  # noqa: BLE001
        emit({"event": "error", "message": f"TCP transport failed: {exc}"})

    ep.libStart()

    farm = EndpointFarm(event_emitter=emit)
    emit({"event": "worker_ready", "account_count": 0})

    # ── Stdin reader thread ──────────────────────────────────────────────

    cmd_queue: queue.Queue[str] = queue.Queue()
    t = threading.Thread(target=_stdin_reader, args=(cmd_queue,), daemon=True)
    t.start()

    # ── Main loop ────────────────────────────────────────────────────────

    running: list[bool] = [True]
    last_rtp = time.monotonic()

    while running[0]:
        ep.libHandleEvents(10)  # 10 ms tick

        # Drain pending commands (non-blocking)
        while True:
            try:
                raw_cmd = cmd_queue.get_nowait()
            except queue.Empty:
                break
            try:
                data = json.loads(raw_cmd)
                method = str(data.get("method", ""))
                params: dict[str, Any] = data.get("params") or {}
                _handle_command(method, params, farm, running)
            except json.JSONDecodeError as exc:
                emit({"event": "error", "message": f"Bad command JSON: {exc}"})

        # Periodic RTP stats
        now = time.monotonic()
        if now - last_rtp >= RTP_STATS_INTERVAL:
            last_rtp = now
            for call in list(farm.calls.values()):
                try:
                    call.emit_rtp_stats()
                except Exception:  # noqa: BLE001, S110
                    pass

    # ── Shutdown ─────────────────────────────────────────────────────────

    try:
        ep.libDestroy()
    except Exception:  # noqa: BLE001, S110
        pass


if __name__ == "__main__":
    main()
