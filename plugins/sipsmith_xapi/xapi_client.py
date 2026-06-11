"""Async HTTP REST client for Cisco RoomOS xAPI."""

from __future__ import annotations

import httpx


class XapiClient:
    """HTTP REST client for Cisco RoomOS xAPI."""

    def __init__(
        self,
        ip: str,
        username: str = "admin",
        password: str = "",
        transport: str = "https",
    ) -> None:
        self._base = f"{transport}://{ip}"
        self._auth = (username, password)

    async def _get(self, path: str) -> dict:
        async with httpx.AsyncClient(verify=False, auth=self._auth, timeout=10) as c:  # noqa: S501
            r = await c.get(f"{self._base}/api/v1/{path.lstrip('/')}")
        r.raise_for_status()
        return r.json() if r.text.strip() else {}

    async def _post(self, path: str, body: dict | None = None) -> dict:
        async with httpx.AsyncClient(verify=False, auth=self._auth, timeout=15) as c:  # noqa: S501
            r = await c.post(
                f"{self._base}/api/v1/{path.lstrip('/')}",
                json=body or {},
            )
        r.raise_for_status()
        return r.json() if r.text.strip() else {}

    async def _put(self, path: str, body: dict) -> dict:
        async with httpx.AsyncClient(verify=False, auth=self._auth, timeout=10) as c:  # noqa: S501
            r = await c.put(
                f"{self._base}/api/v1/{path.lstrip('/')}",
                json=body,
            )
        r.raise_for_status()
        return r.json() if r.text.strip() else {}

    async def status(self, path: str = "") -> dict:
        return await self._get(f"status/{path}" if path else "status")

    async def command(self, path: str, **params) -> dict:
        return await self._post(f"command/{path}", params or None)

    async def config_get(self, path: str) -> dict:
        return await self._get(f"configuration/{path}")

    async def config_set(self, path: str, value: str) -> dict:
        return await self._put(f"configuration/{path}", {"Value": value})

    # ── Convenience wrappers ────────────────────────────────────────────────

    async def dial(
        self,
        number: str,
        protocol: str = "SIP",
        call_rate: int = 768,
    ) -> dict:
        return await self.command("Dial", Number=number, Protocol=protocol, CallRate=call_rate)

    async def hangup(self, call_id: int | None = None) -> dict:
        if call_id is not None:
            return await self.command("Call/Disconnect", CallId=call_id)
        return await self.command("Call/Disconnect")

    async def hold(self, call_id: int) -> dict:
        return await self.command("Call/Hold", CallId=call_id)

    async def resume(self, call_id: int) -> dict:
        return await self.command("Call/Resume", CallId=call_id)

    async def dtmf(self, dtmf_string: str, call_id: int | None = None) -> dict:
        if call_id is not None:
            return await self.command("Call/DTMFSend", CallId=call_id, DTMFString=dtmf_string)
        return await self.command("Call/DTMFSend", DTMFString=dtmf_string)

    async def volume_set(self, level: int) -> dict:
        return await self.command("Audio/Volume/Set", Level=max(0, min(100, level)))

    async def standby_activate(self) -> dict:
        return await self.command("Standby/Activate")

    async def standby_deactivate(self) -> dict:
        return await self.command("Standby/Deactivate")

    async def macro_activate(self, name: str) -> dict:
        return await self.command("Macros/Macro/Activate", Name=name)

    async def macro_deactivate(self, name: str) -> dict:
        return await self.command("Macros/Macro/Deactivate", Name=name)

    async def get_system_info(self) -> dict:
        """Fetch model, sw version, SIP URI, and registration state in one call."""
        try:
            s = await self.status()
            sys_unit = s.get("Status", {}).get("SystemUnit", {})
            sip = s.get("Status", {}).get("SIP", {})
            reg = sip.get("Registration", [{}])
            first_reg = reg[0] if isinstance(reg, list) and reg else {}
            return {
                "model": sys_unit.get("ProductId", ""),
                "sw_version": sys_unit.get("Software", {}).get("Version", ""),
                "sip_uri": first_reg.get("URI", ""),
                "reg_state": first_reg.get("Status", "unknown"),
            }
        except Exception:  # noqa: BLE001
            return {"model": "", "sw_version": "", "sip_uri": "", "reg_state": "unknown"}

    async def get_active_calls(self) -> list[dict]:
        """Return a list of active call dicts from the device."""
        try:
            s = await self.status("Call")
            calls = s.get("Status", {}).get("Call", [])
            if isinstance(calls, dict):
                calls = [calls]
            return [
                {
                    "call_id": c.get("id"),
                    "remote_number": c.get("RemoteNumber", ""),
                    "status": c.get("Status", ""),
                    "direction": c.get("Direction", ""),
                    "duration": c.get("Duration", 0),
                }
                for c in (calls or [])
            ]
        except Exception:  # noqa: BLE001
            return []
