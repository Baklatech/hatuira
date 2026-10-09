"""What hatuira shows: lights, switches and covers, grouped by room."""

import re
from dataclasses import dataclass

DOMAINS = ("light", "switch", "cover")
TITLES = {"light": "lights", "switch": "switches", "cover": "covers"}
COLOUR_MODES = {"hs", "rgb", "rgbw", "rgbww", "xy"}
COVER_SET_POSITION, COVER_STOP = 4, 8  # CoverEntityFeature bits
# Home Assistant does not say what a switch switches, and most of them are settings
# of routers, phones and add-ons. Only the ones that read like lighting are listed
# unasked; the rest wait among the hidden rows.
_LIGHT_WORDS = re.compile(
    r"\b(lights?|lamps?|bulbs?|leds?|spots?|chandelier|sconce|backlight|nightlight)\b"
    r"|свет|ламп|люстр|торшер|ночник|гирлянд|\bбра\b|\bспот")
_LIGHT_ICONS = re.compile(r"light|lamp|bulb|led|sconce|chandelier|string-lights")


@dataclass(frozen=True)
class Entity:
    id: str
    domain: str
    name: str
    area: str
    state: str
    level: int | None = None  # brightness or cover position, percent
    rgb: tuple | None = None
    dimmable: bool = False    # takes a level
    colour: bool = False      # can show any colour
    white: bool = False       # can show shades of white
    kelvin: tuple | None = None  # the (warmest, coldest) white it reports
    features: int = 0
    lighting: bool = True     # a switch only when it reads like a light switch

    @property
    def available(self):
        return self.state not in ("unavailable", "unknown")

    @property
    def on(self):
        if self.domain == "cover":
            return self.state in ("open", "opening", "closing")
        return self.state == "on"

    @property
    def moving(self):
        return self.state in ("opening", "closing")


def entity(raw, area=""):
    """One /api/states record as an Entity."""
    eid = raw["entity_id"]
    domain = eid.split(".", 1)[0]
    attrs = raw.get("attributes") or {}
    state = raw.get("state") or "unknown"
    features = attrs.get("supported_features") or 0
    level = rgb = None
    dimmable = colour = white = False
    kelvin = None
    if domain == "light":
        modes = set(attrs.get("supported_color_modes") or ())
        dimmable = bool(modes - {"onoff"})
        colour = bool(modes & COLOUR_MODES)
        white = colour or "color_temp" in modes  # colour lights mix their own white
        if attrs.get("min_color_temp_kelvin") and attrs.get("max_color_temp_kelvin"):
            kelvin = (int(attrs["min_color_temp_kelvin"]), int(attrs["max_color_temp_kelvin"]))
        if state == "on":
            if attrs.get("brightness") is not None:
                level = max(1, round(attrs["brightness"] / 255 * 100))
            if attrs.get("rgb_color"):
                rgb = tuple(int(v) for v in attrs["rgb_color"][:3])
    elif domain == "cover":
        dimmable = bool(features & COVER_SET_POSITION)
        if attrs.get("current_position") is not None:
            level = int(attrs["current_position"])
    name = str(attrs.get("friendly_name") or eid)
    lighting = domain != "switch" or bool(
        _LIGHT_WORDS.search(f"{name} {eid.split('.', 1)[-1]}".replace("_", " ").lower())
        or _LIGHT_ICONS.search(str(attrs.get("icon") or "")))
    return Entity(eid, domain, name, area or "", state, level, rgb, dimmable, colour, white,
                  kelvin, features, lighting)


def layout(entities, hidden=(), show_hidden=False):
    """Rows of the list: ("group", title) headers and ("entity", Entity) lines."""
    shown = [e for e in entities if show_hidden or e.id not in hidden]
    by_room = any(e.area for e in shown)

    def group(e):  # without rooms in Home Assistant the kinds are the groups
        return (not e.area, e.area.casefold()) if by_room else (DOMAINS.index(e.domain),)

    rows, last = [], None
    for e in sorted(shown, key=lambda e: (group(e), DOMAINS.index(e.domain), e.name.casefold())):
        if group(e) != last:
            last = group(e)
            rows.append(("group", (e.area or "other") if by_room else TITLES[e.domain]))
        rows.append(("entity", e))
    return rows
