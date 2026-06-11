"""WorkerManager — manages the pjsua2 subprocess from the control plane."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any

log = logging.getLogger("sipsmith.sip_emu.worker_manager")

# Module-level singleton
_manager: WorkerManager | None = None


def get_manager() -> WorkerManager | None:
    """Return the module-level WorkerManager singleton, or None if not initialised."""
    return _manager


def init_manager(db_url: str) -> WorkerManager:
    """Initialise (or replace) the module-level singleton and return it."""
    global _manager  # noqa: PLW0603
    _manager = WorkerManager(db_url=db_url)
    return _manager


class WorkerManager:
    """Manage the pjsua2 subprocess and relay events via an asyncio Queue."""

    # Monotonically-increasing command ID counter
    _cmd_counter: int = 0

    def __init__(self, db_url: str) -> None:
        self._proc: asyncio.subprocess.Process | None = None
        self._db_url = db_url
        self._running = False
        # stopped / starting / running / error / pjsua2_unavailable
        self._status = "stopped"
        self._event_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1000)
        self._reader_task: asyncio.Task[None] | None = None

    # ── Public interface ──────────────────────────────────────────────────

    @property
    def status(self) -> str:
        return self._status

    @property
    def is_available(self) -> bool:
        return self._status == "running"

    async def start(self) -> None:
        """Launch the worker subprocess and begin reading its stdout events."""
        if self._running:
            return

        self._status = "starting"
        self._running = True

        # Locate the worker package relative to this file so the import works
        # regardless of how the control plane is started.
        worker_pkg = "sipsmith_sip_emu.worker.main"
        python = sys.executable

        try:
            self._proc = await asyncio.create_subprocess_exec(
                python,
                "-m",
                worker_pkg,
                "--db-url",
                self._db_url,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                # Ensure the plugins directory is importable
                env=_build_env(),
            )
        except Exception:
            log.exception("Failed to launch worker subprocess")
            self._status = "error"
            self._running = False
            return

        self._reader_task = asyncio.create_task(self._event_reader(), name="sip-emu-event-reader")
        log.info("SIP-emu worker subprocess started (pid=%s)", self._proc.pid)

    async def stop(self) -> None:
        """Send shutdown command, then wait for the process to exit."""
        if not self._running or self._proc is None:
            return

        try:
            await self.send_command("shutdown", {})
        except Exception:
            log.debug("Could not send shutdown command; killing process")

        try:
            await asyncio.wait_for(self._proc.wait(), timeout=5.0)
        except TimeoutError:
            log.warning("Worker did not exit cleanly; terminating")
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=3.0)
            except TimeoutError:
                self._proc.kill()

        self._running = False
        self._status = "stopped"
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
        log.info("SIP-emu worker stopped")

    async def send_command(self, method: str, params: dict[str, Any]) -> None:
        """Write a JSON command line to the worker's stdin."""
        if self._proc is None or self._proc.stdin is None:
            raise RuntimeError("Worker subprocess is not running")

        self._cmd_counter += 1
        payload = json.dumps({"id": str(self._cmd_counter), "method": method, "params": params})
        line = (payload + "\n").encode()
        self._proc.stdin.write(line)
        await self._proc.stdin.drain()

    async def next_event(self, timeout: float = 1.0) -> dict[str, Any] | None:
        """Return the next event from the queue, or None if the timeout expires."""
        try:
            return await asyncio.wait_for(self._event_queue.get(), timeout=timeout)
        except TimeoutError:
            return None

    # ── Internal ──────────────────────────────────────────────────────────

    async def _event_reader(self) -> None:
        """Read stdout lines from the subprocess and enqueue them as dicts."""
        assert self._proc is not None  # noqa: S101
        assert self._proc.stdout is not None  # noqa: S101

        try:
            async for raw_line in self._proc.stdout:
                line = raw_line.decode(errors="replace").strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    log.debug("Non-JSON stdout from worker: %s", line)
                    continue

                # Check for "pjsua2 not available" error
                if (
                    event.get("event") == "error"
                    and "pjsua2" in event.get("message", "").lower()
                    and "not available" in event.get("message", "").lower()
                ):
                    self._status = "pjsua2_unavailable"
                    log.warning("pjsua2 is not available; worker degraded")

                elif event.get("event") == "worker_ready":
                    self._status = "running"
                    log.info("SIP-emu worker reported ready")

                try:
                    self._event_queue.put_nowait(event)
                except asyncio.QueueFull:
                    log.warning("Event queue full; dropping oldest event")
                    try:
                        self._event_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                    self._event_queue.put_nowait(event)

        except Exception:
            log.exception("Error reading from worker stdout")

        # Process exited
        if self._proc.returncode is not None and self._proc.returncode != 0:
            log.error("Worker exited with code %s", self._proc.returncode)
            if self._status not in ("pjsua2_unavailable",):
                self._status = "error"
        elif self._status == "running":
            self._status = "stopped"

        self._running = False


def _build_env() -> dict[str, str]:
    """Build environment for the worker subprocess, preserving PYTHONPATH."""
    import os

    env = dict(os.environ)
    # Ensure the plugins directory is on the path
    plugins_dir = str(Path(__file__).parent.parent.parent)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{plugins_dir}:{existing}" if existing else plugins_dir
    return env
