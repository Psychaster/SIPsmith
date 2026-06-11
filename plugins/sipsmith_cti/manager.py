"""CTI sidecar process manager — one SidecarManager per CUCM cluster."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger("sipsmith.cti.manager")

_SIDECAR_JAR = "/opt/sipsmith/lib/sipsmith-cti-sidecar.jar"
_JTAPI_DIR = "/var/lib/sipsmith/cti"


class SidecarManager:
    """Manages one Java CTI sidecar subprocess per cluster."""

    def __init__(
        self,
        cluster_id: int,
        cucm_host: str,
        cucm_version: str,
        app_user: str,
        app_password: str,
    ) -> None:
        self.cluster_id = cluster_id
        self._proc: asyncio.subprocess.Process | None = None
        self._events: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=500)
        self._running = False
        self._status = "stopped"
        self._cucm_host = cucm_host
        self._app_user = app_user
        self._app_password = app_password
        self._cucm_version = cucm_version
        self._reader_task: asyncio.Task[None] | None = None

    @property
    def status(self) -> str:
        return self._status

    @property
    def is_available(self) -> bool:
        return self._status == "connected"

    async def start(self) -> None:
        """Launch the sidecar subprocess and begin reading its stdout events."""
        jar = Path(_SIDECAR_JAR)
        if not jar.exists():
            self._status = "sidecar_unavailable"
            log.warning(
                "CTI sidecar jar not found at %s (cluster %d)",
                _SIDECAR_JAR,
                self.cluster_id,
            )
            return

        jtapi_jar = self._find_jtapi_jar()
        if jtapi_jar is None:
            self._status = "jtapi_unavailable"
            log.warning(
                "No jtapi jar found for cluster %d in %s/%d",
                self.cluster_id,
                _JTAPI_DIR,
                self.cluster_id,
            )
            return

        cp = f"{jtapi_jar}:{jar}"
        try:
            self._proc = await asyncio.create_subprocess_exec(
                "/usr/bin/java",
                "-cp",
                cp,
                "com.sipsmith.cti.Main",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except Exception:
            log.exception("Failed to launch CTI sidecar for cluster %d", self.cluster_id)
            self._status = "error"
            return

        self._running = True
        self._status = "starting"
        self._reader_task = asyncio.create_task(
            self._read_stdout(),
            name=f"cti-sidecar-reader-{self.cluster_id}",
        )
        log.info(
            "CTI sidecar started for cluster %d (pid=%s)",
            self.cluster_id,
            self._proc.pid,
        )
        await self.send_command(
            "connect",
            {
                "host": self._cucm_host,
                "user": self._app_user,
                "password": self._app_password,
            },
        )

    async def stop(self) -> None:
        """Gracefully shut down the sidecar subprocess."""
        self._running = False
        if self._proc is not None:
            try:
                await self.send_command("shutdown", {})
            except Exception:  # noqa: BLE001
                log.debug(
                    "Could not send shutdown to sidecar for cluster %d",
                    self.cluster_id,
                )
            try:
                self._proc.terminate()
                await asyncio.wait_for(self._proc.wait(), timeout=5.0)
            except Exception:  # noqa: BLE001
                self._proc.kill()
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
        self._status = "stopped"
        self._proc = None
        log.info("CTI sidecar stopped for cluster %d", self.cluster_id)

    async def send_command(self, method: str, params: dict[str, Any]) -> None:
        """Write a JSON command line to the sidecar's stdin."""
        if self._proc is None or self._proc.stdin is None:
            return
        line = json.dumps({"method": method, "params": params}) + "\n"
        self._proc.stdin.write(line.encode())
        await self._proc.stdin.drain()

    async def next_event(self, timeout: float = 1.0) -> dict[str, Any] | None:
        """Return the next event from the queue, or None if the timeout expires."""
        try:
            return await asyncio.wait_for(self._events.get(), timeout=timeout)
        except TimeoutError:
            return None

    async def _read_stdout(self) -> None:
        """Read stdout lines from the sidecar and dispatch to the event queue."""
        if self._proc is None or self._proc.stdout is None:
            return

        try:
            async for raw_line in self._proc.stdout:
                if not self._running:
                    break
                line = raw_line.decode(errors="replace").strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    log.debug(
                        "Non-JSON stdout from CTI sidecar (cluster %d): %s",
                        self.cluster_id,
                        line,
                    )
                    continue

                event_type = data.get("event", "")
                if event_type == "ready":
                    self._status = "connected"
                    log.info("CTI sidecar connected for cluster %d", self.cluster_id)
                elif event_type == "jtapi_unavailable":
                    self._status = "jtapi_unavailable"
                    log.warning(
                        "CTI sidecar reports jtapi_unavailable for cluster %d",
                        self.cluster_id,
                    )
                elif event_type == "error" and self._status == "starting":
                    self._status = "error"
                    log.error(
                        "CTI sidecar error during startup (cluster %d): %s",
                        self.cluster_id,
                        data.get("message", ""),
                    )

                # Evict oldest entry if queue is full to avoid blocking
                if self._events.full():
                    try:
                        self._events.get_nowait()
                    except Exception:  # noqa: BLE001, S110
                        pass
                await self._events.put(data)

        except Exception:
            log.exception("Error reading from CTI sidecar stdout (cluster %d)", self.cluster_id)

        # Process exited
        if (
            self._proc is not None
            and self._proc.returncode is not None
            and self._proc.returncode != 0
        ):
            log.error(
                "CTI sidecar exited with code %s (cluster %d)",
                self._proc.returncode,
                self.cluster_id,
            )
            if self._status not in ("jtapi_unavailable",):
                self._status = "error"
        elif self._status == "connected":
            self._status = "stopped"

        self._running = False

    def _find_jtapi_jar(self) -> str | None:
        """Return the path to the most recent jtapi jar for this cluster, or None."""
        d = Path(_JTAPI_DIR) / str(self.cluster_id)
        if not d.exists():
            return None
        jars = sorted(d.glob("jtapi-*.jar"))
        return str(jars[-1]) if jars else None


# ── Module-level registry: cluster_id → SidecarManager ───────────────────────

_managers: dict[int, SidecarManager] = {}


def get_manager(cluster_id: int) -> SidecarManager | None:
    """Return the SidecarManager for a cluster, or None."""
    return _managers.get(cluster_id)


def get_all_managers() -> dict[int, SidecarManager]:
    """Return a shallow copy of the manager registry."""
    return dict(_managers)


async def start_manager(
    cluster_id: int,
    cucm_host: str,
    cucm_version: str,
    app_user: str,
    app_password: str,
) -> SidecarManager:
    """(Re-)create and start a SidecarManager for the given cluster."""
    if cluster_id in _managers:
        await _managers[cluster_id].stop()
    mgr = SidecarManager(
        cluster_id=cluster_id,
        cucm_host=cucm_host,
        cucm_version=cucm_version,
        app_user=app_user,
        app_password=app_password,
    )
    _managers[cluster_id] = mgr
    await mgr.start()
    return mgr


async def stop_manager(cluster_id: int) -> None:
    """Stop and remove the SidecarManager for the given cluster."""
    mgr = _managers.pop(cluster_id, None)
    if mgr:
        await mgr.stop()
