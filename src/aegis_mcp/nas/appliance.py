"""Optional NAS appliance APIs: TrueNAS (REST v2.0) and Synology DSM (Web API)."""

from __future__ import annotations

import abc
from typing import Any

import httpx


class ApplianceError(Exception):
    pass


class Appliance(abc.ABC):
    name = "abstract"

    @abc.abstractmethod
    async def health(self) -> dict[str, Any]:
        """{'pools': [{'name','status','healthy','used_bytes','size_bytes'}], 'alerts': [...]}"""


class TrueNAS(Appliance):
    name = "truenas"

    def __init__(self, base_url: str, api_key: str, *, transport: httpx.AsyncBaseTransport | None = None,
                 verify: bool = True) -> None:
        self._c = httpx.AsyncClient(base_url=base_url.rstrip("/"), headers={"Authorization": f"Bearer {api_key}"},
                                    transport=transport, verify=verify, timeout=15)

    async def _get(self, path: str) -> Any:
        r = await self._c.get(path)
        if r.status_code >= 400:
            raise ApplianceError(f"TrueNAS {path}: HTTP {r.status_code}")
        return r.json()

    async def health(self) -> dict[str, Any]:
        pools = await self._get("/api/v2.0/pool")
        alerts = await self._get("/api/v2.0/alert/list")
        return {
            "appliance": self.name,
            "pools": [{"name": p.get("name"), "status": p.get("status"), "healthy": p.get("healthy"),
                       "size_bytes": p.get("size"), "used_bytes": p.get("allocated")} for p in pools],
            "alerts": [{"level": a.get("level"), "text": a.get("formatted") or a.get("text")}
                       for a in alerts if not a.get("dismissed")],
        }


class Synology(Appliance):
    name = "synology"

    def __init__(self, base_url: str, account: str, password: str, *,
                 transport: httpx.AsyncBaseTransport | None = None, verify: bool = True) -> None:
        self._c = httpx.AsyncClient(base_url=base_url.rstrip("/"), transport=transport, verify=verify, timeout=15)
        self._account, self._password = account, password
        self._sid: str | None = None

    async def _login(self) -> str:
        # POST so credentials never appear in URLs / proxy logs.
        r = await self._c.post("/webapi/auth.cgi", data={
            "api": "SYNO.API.Auth", "version": "6", "method": "login", "account": self._account,
            "passwd": self._password, "session": "aegis", "format": "sid"})
        body = r.json()
        if not body.get("success"):
            raise ApplianceError(f"Synology login failed: error {body.get('error', {}).get('code')}")
        self._sid = body["data"]["sid"]
        return self._sid

    async def health(self) -> dict[str, Any]:
        sid = self._sid or await self._login()
        r = await self._c.post("/webapi/entry.cgi", data={
            "api": "SYNO.Storage.CGI.Storage", "version": "1", "method": "load_info", "_sid": sid})
        body = r.json()
        if not body.get("success"):
            raise ApplianceError(f"Synology storage query failed: {body.get('error')}")
        data = body["data"]
        return {
            "appliance": self.name,
            "pools": [{"name": v.get("id"), "status": v.get("status"), "healthy": v.get("status") == "normal",
                       "size_bytes": int((v.get("size") or {}).get("total", 0)),
                       "used_bytes": int((v.get("size") or {}).get("used", 0))} for v in data.get("volumes", [])],
            "disks": [{"name": d.get("name"), "status": d.get("status"), "smart": d.get("smart_status"),
                       "temp_c": d.get("temp")} for d in data.get("disks", [])],
            "alerts": [],
        }
