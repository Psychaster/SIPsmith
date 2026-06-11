"""AXL (Administrative XML Layer) client for CUCM provisioning."""

from __future__ import annotations

import httpx

_HEADERS = {"Content-Type": "text/xml; charset=utf-8", "SOAPAction": '""'}


class AXLClient:
    """Thin httpx-based SOAP client for the CUCM AXL API."""

    def __init__(
        self,
        host: str,
        user: str,
        password: str,
        version: str = "14.0",
    ) -> None:
        self._base = f"https://{host}:8443/axl/"
        self._auth = (user, password)
        self._ns = f"http://www.cisco.com/AXL/API/{version}"

    def _envelope(self, body: str) -> str:
        return (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"'
            f' xmlns:ns="{self._ns}">'
            "<soapenv:Header/>"
            f"<soapenv:Body>{body}</soapenv:Body>"
            "</soapenv:Envelope>"
        )

    async def _post(self, body: str) -> httpx.Response:
        async with httpx.AsyncClient(
            verify=False,  # noqa: S501
            auth=self._auth,
            timeout=30,
        ) as c:
            return await c.post(self._base, content=self._envelope(body), headers=_HEADERS)

    async def get_app_user(self, user_id: str) -> dict[str, object]:
        body = f"<ns:getAppUser><name>{user_id}</name></ns:getAppUser>"
        r = await self._post(body)
        return {"status_code": r.status_code, "text": r.text}

    async def add_app_user(
        self, user_id: str, password: str, devices: list[str]
    ) -> dict[str, object]:
        device_lines = "".join(f"<device><name>{d}</name></device>" for d in devices)
        body = (
            "<ns:addAppUser>"
            f"<appUser><userid>{user_id}</userid><password>{password}</password>"
            f"<associatedDevices>{device_lines}</associatedDevices>"
            "<presenceGroupName>Standard Presence group</presenceGroupName>"
            "</appUser></ns:addAppUser>"
        )
        r = await self._post(body)
        return {"status_code": r.status_code, "text": r.text}

    async def update_app_user_cti(self, user_id: str) -> dict[str, object]:
        body = (
            "<ns:updateAppUser>"
            f"<userid>{user_id}</userid>"
            "<ctiControlledDeviceProfiles>"
            '<profileName operation="add">'
            "Standard CTI Allow Control of Phones supporting Connected Xfer and conf"
            "</profileName>"
            '<profileName operation="add">Standard CTI Enabled</profileName>'
            "</ctiControlledDeviceProfiles>"
            "</ns:updateAppUser>"
        )
        r = await self._post(body)
        return {"status_code": r.status_code, "text": r.text}

    async def list_phones(self, search_pattern: str = "%") -> dict[str, object]:
        body = (
            "<ns:listPhone>"
            f"<searchCriteria><name>{search_pattern}</name></searchCriteria>"
            "<returnedTags>"
            "<name/><description/><model/><product/>"
            "</returnedTags>"
            "</ns:listPhone>"
        )
        r = await self._post(body)
        return {"status_code": r.status_code, "text": r.text}
