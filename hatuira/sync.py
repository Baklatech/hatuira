"""Theme sync: the chosen lights take the colours of the omarchy theme."""

import random

from .model import entity


def colours(theme, settings, n, rng=random):
    """`n` colours for `n` lights: one tone for all, or the hues of the theme dealt out."""
    if settings["mode"] != "mix":
        return [theme.colour(settings["color"])] * n
    hues = theme.palette()
    rng.shuffle(hues)  # a new deal every time; neighbours differ while the hues last
    return [hues[i % len(hues)] for i in range(n)]


def payload(settings, ids, rgb):
    """light.turn_on data that paints `ids` in `rgb`."""
    data = {"entity_id": ids, "rgb_color": list(rgb)}
    if settings["transition"]:
        data["transition"] = settings["transition"]
    return data


def apply(client, cfg, theme, rng=random):
    """Recolour the synced lights that are on; returns their ids.

    Lights that are off are left alone: Home Assistant can only set a colour by
    switching the light on. They get a theme colour when hatuira turns them on.
    """
    chosen = set(cfg["sync"])
    if not chosen:
        return []
    lit = [e.id for e in map(entity, client.states())
           if e.id in chosen and e.domain == "light" and e.on and e.colour]
    settings = cfg["settings"]
    if lit and settings["mode"] != "mix":
        client.call("light", "turn_on", payload(settings, lit, theme.colour(settings["color"])))
    elif lit:
        for eid, rgb in zip(lit, colours(theme, settings, len(lit), rng)):
            client.call("light", "turn_on", payload(settings, eid, rgb))
    return lit
