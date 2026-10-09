"""Palette from the active omarchy theme, reloaded when the theme changes."""

import colorsys
import math
import os
import re

PATHS = (
    "~/.local/state/omarchy/current/theme/colors.toml",
    "~/.config/omarchy/current/theme/colors.toml",
)
FALLBACK = {
    "background": "#1a1b26", "foreground": "#c0caf5", "bright_foreground": "#e6e9f5",
    "muted": "#565f89", "accent": "#7aa2f7", "red": "#f7768e", "green": "#9ece6a",
    "yellow": "#e0af68", "cyan": "#7dcfff",
}
# colors.toml is flat `key = "#rrggbb"` lines, so no TOML parser is needed.
_LINE = re.compile(r'^\s*([A-Za-z0-9_-]+)\s*=\s*["\'](#[0-9A-Fa-f]{6})["\']', re.M)


def rgb(s):
    s = s.lstrip("#")
    return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))


def hue_rgb(hue, saturation):
    """A fully bright colour; hue and saturation in 0..1."""
    return tuple(round(v * 255) for v in colorsys.hsv_to_rgb(hue, saturation, 1.0))


def kelvin_rgb(k):
    """Roughly what white of this colour temperature looks like (Tanner Helland's fit)."""
    t = k / 100
    r = 255 if t <= 66 else 329.698727446 * (t - 60) ** -0.1332047592
    g = 99.4708025861 * math.log(t) - 161.1195681661 if t <= 66 else \
        288.1221695283 * (t - 60) ** -0.0755148492
    b = 255 if t >= 66 else 0 if t <= 19 else 138.5177312231 * math.log(t - 10) - 305.0447927307
    return tuple(max(0, min(255, round(v))) for v in (r, g, b))


def contrast(c):
    """Black or white, whichever shows on top of `c`."""
    return (0, 0, 0) if c[0] * 0.299 + c[1] * 0.587 + c[2] * 0.114 > 140 else (255, 255, 255)


def mix(a, b, f):
    return (int(a[0] + (b[0] - a[0]) * f), int(a[1] + (b[1] - a[1]) * f),
            int(a[2] + (b[2] - a[2]) * f))


class Theme:
    def __init__(self, path=None):
        self.path = path or next(
            (p for p in map(os.path.expanduser, PATHS) if os.path.exists(p)), None)
        self.mtime = None
        self.load()

    def load(self):
        raw = dict(FALLBACK)
        if self.path:
            try:
                self.mtime = os.stat(self.path).st_mtime
                with open(self.path, encoding="utf-8") as f:
                    raw.update(_LINE.findall(f.read()))
            except (OSError, ValueError):
                pass
        self.colours = c = {k: rgb(v) for k, v in raw.items()}
        self.bg, self.fg = c["background"], c["foreground"]
        self.bright = c["bright_foreground"]
        self.muted, self.accent = c["muted"], c["accent"]
        self.red, self.green = c["red"], c["green"]
        self.yellow, self.cyan = c["yellow"], c["cyan"]
        self.selection = c.get("selection") or mix(self.bg, self.fg, 0.16)
        self.rim = mix(self.bg, self.muted, 0.7)
        self.dim = mix(self.bg, self.fg, 0.55)

    def palette(self):
        """The distinct hues of the theme, accent first."""
        out = []
        for key in ("accent", "red", "orange", "yellow", "green", "cyan", "blue", "magenta"):
            c = self.colours.get(key)
            if c and c not in out:
                out.append(c)
        return out

    def colour(self, key):
        """A palette entry by its colors.toml name; the accent when the theme lacks it."""
        return self.colours.get(key, self.accent)

    def changed(self):
        if not self.path:
            return False
        try:
            if os.stat(self.path).st_mtime == self.mtime:
                return False
        except OSError:
            return False
        self.load()
        return True
