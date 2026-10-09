"""Home Assistant over its REST API, authorised by a long-lived access token."""

import json
import ssl
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from .model import DOMAINS

# Rooms are not part of /api/states; a template is the only REST way to ask for them.
AREAS = ("{% for s in states.light | list + states.switch | list + states.cover | list %}"
         "{{ s.entity_id }}|{{ area_name(s.entity_id) or '' }}\n{% endfor %}")


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(f"{status} {message}" if status else message)
        self.status = status


class _SameOriginRedirects(urllib.request.HTTPRedirectHandler):
    # urllib replays request headers on redirect, which would hand the token
    # to whatever host the server points at.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlsplit(newurl).netloc != urlsplit(req.full_url).netloc:
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Hass:
    def __init__(self, url, token, timeout=10, insecure=False):
        self.base = url.rstrip("/") + "/api"
        self.headers = {"Authorization": f"Bearer {token}", "User-Agent": "hatuira",
                        "Content-Type": "application/json"}
        self.timeout = timeout
        handlers = [_SameOriginRedirects]
        if insecure:  # a self-signed certificate on the local network
            handlers.append(urllib.request.HTTPSHandler(context=ssl._create_unverified_context()))
        self.opener = urllib.request.build_opener(*handlers)

    def _request(self, path, payload=None):
        data = None if payload is None else json.dumps(payload).encode()
        req = urllib.request.Request(self.base + path, data=data, headers=self.headers)
        try:
            with self.opener.open(req, timeout=self.timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            with e:
                reason = "token rejected" if e.code == 401 else _reason(e)
            raise ApiError(e.code, reason) from None
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise ApiError(0, str(getattr(e, "reason", e))) from None

    def _json(self, path, payload=None):
        try:
            return json.loads(self._request(path, payload))
        except ValueError:
            raise ApiError(0, "not a Home Assistant API") from None

    def ping(self):
        """Raises ApiError unless the url answers as Home Assistant and takes the token."""
        if not isinstance(self._json("/"), dict):
            raise ApiError(0, "not a Home Assistant API")

    def states(self):
        """Raw state records of every light, switch and cover."""
        return [s for s in self._json("/states")
                if s.get("entity_id", "").split(".", 1)[0] in DOMAINS]

    def areas(self):
        """entity_id -> room name, for the entities that are assigned to one."""
        text = self._request("/template", {"template": AREAS}).decode("utf-8", "replace")
        pairs = (line.strip().split("|", 1) for line in text.splitlines() if "|" in line)
        return {eid: area for eid, area in pairs if area}

    def call(self, domain, service, data):
        self._request(f"/services/{domain}/{service}", data)


def _reason(e):
    try:
        msg = json.loads(e.read() or b"{}").get("message")
        if msg:
            return str(msg)[:120]
    except (ValueError, AttributeError, OSError):
        pass
    return e.reason or "error"


class Demo:
    """A made-up home that answers like Hass, for `hatuira --demo` and the tests."""

    def __init__(self):
        rgb, white, onoff = ["hs", "color_temp"], ["color_temp"], ["onoff"]
        self.rooms = {}
        self.items = {}
        for eid, name, room, state, attrs in (
            ("light.floor_lamp", "Floor lamp", "Living room", "on",
             {"supported_color_modes": rgb, "brightness": 178, "rgb_color": [255, 170, 90]}),
            ("light.ceiling", "Ceiling", "Living room", "off", {"supported_color_modes": white}),
            ("light.tv_strip", "TV strip", "Living room", "on",
             {"supported_color_modes": rgb, "brightness": 90, "rgb_color": [120, 110, 255]}),
            ("cover.living_curtains", "Curtains", "Living room", "open",
             {"supported_features": 15, "current_position": 60}),
            ("light.bedside", "Bedside", "Bedroom", "off", {"supported_color_modes": rgb}),
            ("cover.bedroom_blinds", "Blinds", "Bedroom", "closed",
             {"supported_features": 15, "current_position": 0}),
            ("light.kitchen_spots", "Spots", "Kitchen", "on",
             {"supported_color_modes": white, "brightness": 255}),
            ("switch.kitchen_counter", "Counter light", "Kitchen", "on", {}),
            ("switch.hallway", "Hallway", "Hallway", "off", {"icon": "mdi:ceiling-light"}),
            ("light.porch", "Porch", "", "unavailable", {"supported_color_modes": onoff}),
            ("switch.adguard_filtering", "AdGuard filtering", "", "on", {}),
            ("switch.mikrotik_port_ether2", "MikroTik port ether2", "Hallway", "on", {}),
            ("switch.phone_do_not_disturb", "Phone Do not disturb", "", "off", {}),
        ):
            self.items[eid] = {"entity_id": eid, "state": state,
                               "attributes": {"friendly_name": name, **attrs}}
            if room:
                self.rooms[eid] = room
        self.calls = []

    def ping(self):
        pass

    def states(self):
        return [json.loads(json.dumps(s)) for s in self.items.values()]

    def areas(self):
        return dict(self.rooms)

    def call(self, domain, service, data):
        self.calls.append((domain, service, data))
        ids = data["entity_id"]
        for eid in [ids] if isinstance(ids, str) else ids:
            item = self.items[eid]
            attrs = item["attributes"]
            if service == "toggle":
                service = ("turn_off" if item["state"] == "on" else "turn_on") \
                    if domain != "cover" else \
                    ("open_cover" if item["state"] == "closed" else "close_cover")
            if service == "turn_on":
                item["state"] = "on"
                if domain == "light" and attrs["supported_color_modes"] != ["onoff"]:
                    if "brightness_pct" in data:
                        attrs["brightness"] = round(data["brightness_pct"] * 2.55)
                    attrs.setdefault("brightness", 255)
                    if "rgb_color" in data:
                        attrs["rgb_color"] = list(data["rgb_color"])
            elif service == "turn_off":
                item["state"] = "off"
            elif service in ("open_cover", "close_cover", "set_cover_position"):
                pos = data.get("position", 100 if service == "open_cover" else 0)
                attrs["current_position"] = pos
                item["state"] = "open" if pos else "closed"
