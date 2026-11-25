"""3x-ui v3.11.0 reads ClientRecord but writes model.Client (different schemas)."""
import asyncio
from dataclasses import dataclass
from urllib.parse import quote

import aiohttp

DAY = 86_400_000


class PanelError(Exception):
    """Safe to show in Telegram. Never include request URLs, headers or bodies."""


class WriteRejected(PanelError):
    """The panel explicitly rejected the write. An operator must review it."""


def valid_email(value: str) -> bool:
    return (0 < len(value) <= 254 and value not in {".", ".."}
            and value.isprintable() and "/" not in value and "\\" not in value)


@dataclass
class Client:
    fields: dict
    used: int = 0

    @property
    def email(self):
        return self.fields["email"]

    @property
    def id(self):
        return self.fields["id"]

    @property
    def owner(self):
        return int(self.fields["tgId"] or 0)

    @property
    def expiry(self):
        return self.fields["expiryTime"]

    @property
    def exception(self):
        if not self.fields["enable"]:
            return "Клиент отключён в панели"
        return self.limit_exception

    @property
    def limit_exception(self):
        if self.expiry == 0:
            return "Бессрочная подписка"
        if self.expiry < 0:
            return "Срок начнётся при первом подключении"
        if any(self.fields.get(key) for key in ("reset", "resetDay", "resetWeekday")):
            return "В панели настроено автоматическое продление"
        if self.fields["totalGB"] and self.used >= self.fields["totalGB"]:
            return "Исчерпан лимит трафика"
        return ""

    def write_body(self, field: str, value: int, *, activate=False):
        if field not in {"tgId", "expiryTime"}:
            raise ValueError("Unsupported client field")
        # Peer credentials, keys, keep-alive and tunnel IPs may differ per inbound.
        # v3.11.0 carries them forward locally when omitted. The shared DB id
        # is an integer; the API's wire 'id' would be a protocol UUID.
        names = ("email", "enable", "tgId", "expiryTime", "security", "flow", "reverse",
                 "limitIp", "limitHwid", "totalGB", "subId", "group", "comment",
                 "reset", "resetMax", "resetDay", "resetWeekday", "trafficReset",
                 "trafficResetDay", "clientHostRuleIds", "adTag", "forwardedPorts")
        body = {key: self.fields[key] for key in names if key in self.fields}
        body[field] = value
        if activate:
            if field != "expiryTime":
                raise ValueError("Activation requires a renewal")
            body["enable"] = True
        return body


class Panel:
    def __init__(self, session: aiohttp.ClientSession, url: str, token: str):
        self.session = session
        self.url = url.rstrip("/") + "/panel/api/clients/"
        self.headers = {"Authorization": "Bearer " + token, "Accept": "application/json"}

    async def _request(self, method, route, body=None, *, missing_ok=False):
        try:
            async with self.session.request(method, self.url + route, headers=self.headers,
                                            json=body, allow_redirects=False) as response:
                if response.status != 200:
                    raise PanelError(f"3x-ui вернула HTTP {response.status}. Проверьте подключение к панели.")
                payload = await response.json(content_type=None)
            if not isinstance(payload, dict) or not isinstance(payload.get("success"), bool):
                raise PanelError("Неожиданный формат ответа 3x-ui.")
            if not payload["success"]:
                if missing_ok and "record not found" in str(payload.get("msg")).lower():
                    return None
                if method == "POST":
                    raise WriteRejected("Панель отклонила запись. Проверьте карточку и подключения в 3x-ui.")
                raise PanelError("Панель отклонила чтение. Проверьте доступ к API.")
            return payload.get("obj")
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            raise PanelError("Нет корректного ответа от 3x-ui.") from exc

    @staticmethod
    def parse(record):
        try:
            fields = record["client"]
            for name in ("id", "tgId", "expiryTime", "email", "enable", "totalGB"):
                fields[name]
            if not isinstance(fields["id"], int) or not valid_email(fields["email"]):
                raise ValueError()
            return Client(fields, int(record.get("usedTraffic", 0)))
        except (KeyError, TypeError, ValueError) as exc:
            raise PanelError("Несовместимый формат карточки клиента.") from exc

    async def get(self, email):
        if not valid_email(email):
            raise ValueError("Недопустимый Email")
        obj = await self._request("GET", "get/" + quote(email, safe=""), missing_ok=True)
        return self.parse(obj) if obj is not None else None

    async def owned(self, user):
        rows = await self._request("GET", f"get/tgId/{user}")
        if not isinstance(rows, list):
            raise PanelError("Несовместимый ответ при поиске по Telegram ID.")
        return [client for row in rows if (client := self.parse(row)).owner == user]

    async def list_bindings(self) -> list[tuple[int, str, int]]:
        # In 3.11.0 /list returns flat ClientRecords. /list/paged omits tgId.
        rows = await self._request("GET", "list")
        if not isinstance(rows, list):
            raise PanelError("Панель вернула некорректный список клиентов.")
        bindings = []
        try:
            for row in rows:
                owner = row["tgId"]
                if isinstance(owner, str):
                    owner = int(owner or 0)
                if owner is None:
                    continue
                if type(owner) is not int or owner > 2**63 - 1:
                    raise ValueError()
                if owner <= 0:
                    continue
                cid, email = row["id"], row["email"]
                if type(cid) is not int or not 0 < cid < 2**63 or not isinstance(email, str) or not valid_email(email):
                    raise ValueError()
                bindings.append((cid, email, owner))
        except (KeyError, ValueError, TypeError) as exc:
            raise PanelError("Некорректная привязка в списке клиентов 3x-ui.") from exc
        return bindings

    async def change(self, client, field, value, *, activate=False):
        await self._request("POST", "update/" + quote(client.email, safe=""),
                            client.write_body(field, value, activate=activate))

