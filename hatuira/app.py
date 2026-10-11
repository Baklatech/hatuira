"""Main loop: poll Home Assistant, draw the list, send what the keys ask for."""

import queue
import random
import re
import threading
import time
import unicodedata
from dataclasses import replace

from . import sync
from .hass import ApiError
from .model import COVER_STOP, entity, layout
from .term import Terminal
from .theme import Theme, contrast, hue_rgb, kelvin_rgb, mix

SETTLE = 4.0     # seconds a finished command may wait for Home Assistant to confirm it
DEBOUNCE = 0.25  # quiet time after the last ←/→ before the level is sent
MARKS = {"light": ("●", "○"), "switch": ("■", "□"), "cover": ("▭", "▬")}  # on / off
STATE_W = 7


class App:
    """Everything the list knows and does; the terminal is not involved."""

    def __init__(self, client, cfg, save, theme, wake=lambda: None):
        self.client, self.cfg, self.save, self.theme, self.wake = client, cfg, save, theme, wake
        self.settings = cfg["settings"]
        self.raw, self.areas = [], {}
        self.truth = {}     # id -> Entity as Home Assistant last reported it
        self.entities = []  # the same with optimistic changes applied
        self.rows, self.ids = [], []
        self.hidden = set()  # ids left off the list, by choice or as non-lighting switches
        self.selected = None
        self.show_hidden = False
        self.palette = False  # the keys drive the palette instead of the list
        self.cursor = (1, 0)  # palette row, column
        self.dealt = []       # the colours the last mix gave out, so the next one differs
        self.loaded = False
        self.error = ""
        self.note, self.note_until = "", 0.0
        self.overlay = {}   # id -> [expires, fields]: what the keys asked for, shown at once
        self.wanted = {}    # id -> (due, percent): levels waiting out the debounce
        self.events = queue.Queue()
        self.pending = {}   # id -> (domain, service, data): only the latest wish per device
        self.flying = None  # id whose command is with Home Assistant right now
        self.cond = threading.Condition()
        self.kick = threading.Event()
        self.want_areas = True
        self.brisk_until = 0.0
        self.threaded = False

    # --- talking to Home Assistant -------------------------------------------

    def start(self):
        self.threaded = True
        threading.Thread(target=self._poll, daemon=True, name="poll").start()
        threading.Thread(target=self._send, daemon=True, name="send").start()

    def fetch(self):
        """One poll: (states, areas); areas is None unless they were due and arrived."""
        areas = None
        if self.want_areas:
            try:
                areas = self.client.areas()
            except ApiError:
                pass  # rooms are a nicety: the list still works grouped by kind
        states = self.client.states()
        self.want_areas = False
        return states, areas

    def _poll(self):
        wait = self.settings["interval"]
        while True:
            try:
                self.events.put(("states", *self.fetch()))
                wait = self.settings["interval"]
            except ApiError as e:
                self.events.put(("error", str(e)))
                wait = min(60, wait * 2)
            self.wake()
            # Right after a command the state is still settling: look again soon.
            self.kick.wait(1.0 if time.monotonic() < self.brisk_until else wait)
            self.kick.clear()

    def _send(self):
        while True:
            with self.cond:
                while not self.pending:
                    self.cond.wait()
                key = next(iter(self.pending))
                domain, service, data = self.pending.pop(key)
                self.flying = key
            try:
                self.client.call(domain, service, data)
                self.events.put(("done", key))
            except ApiError as e:
                self.events.put(("failed", key, f"{service}: {e}"))
            with self.cond:
                self.flying = None
            self.brisk_until = time.monotonic() + SETTLE + 1
            self.kick.set()
            self.wake()

    def send(self, domain, service, data):
        if not self.threaded:  # headless: do it now and look at the result
            self.client.call(domain, service, data)
            self.take(*self.fetch())
            return
        # One command at a time, and per device only the newest: pressing a key five
        # times while the first command is on its way sends two commands, not five.
        with self.cond:
            self.pending.pop(data["entity_id"], None)
            self.pending[data["entity_id"]] = (domain, service, data)
            self.cond.notify()

    def busy(self, eid):
        """True while something for this device is still to be sent or being sent."""
        with self.cond:
            return eid in self.wanted or eid in self.pending or self.flying == eid

    def refresh(self):
        self.want_areas = True
        self.kick.set()

    def take(self, states, areas):
        self.raw = states
        if areas is not None:
            self.areas = areas
        self.loaded, self.error = True, ""
        self.rebuild()

    def drain(self):
        """Apply what the threads delivered and send the levels whose debounce ran out."""
        while True:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            if event[0] == "states":
                self.take(event[1], event[2])
            elif event[0] == "error":
                self.error = event[1]
            elif event[0] == "done":  # sent: now Home Assistant has SETTLE seconds to agree
                if event[1] in self.overlay:
                    self.overlay[event[1]][0] = time.monotonic() + SETTLE
            else:
                self.overlay.pop(event[1], None)
                self.say(event[2])
                self.rebuild()
        now = time.monotonic()
        for eid, (due, pct) in list(self.wanted.items()):
            if due <= now:
                del self.wanted[eid]
                self.commit_level(eid, pct)
        if self.note and now >= self.note_until:
            self.note = ""
        if any(held[0] <= now for held in self.overlay.values()):
            self.rebuild()

    def timeout(self):
        """Seconds until something needs doing without a key press; None when nothing does."""
        due = [d for d, _ in self.wanted.values()]
        due += [held[0] for eid, held in self.overlay.items() if not self.busy(eid)]
        if self.note:
            due.append(self.note_until)
        return max(0.0, min(due) - time.monotonic()) if due else None

    # --- state ---------------------------------------------------------------

    def rebuild(self):
        now = time.monotonic()
        self.truth = {}
        self.entities = []
        for raw in self.raw:
            e = entity(raw, self.areas.get(raw["entity_id"], ""))
            self.truth[e.id] = e
            held = self.overlay.get(e.id)
            # What was asked for stays on screen while commands are on their way, so the
            # polls in between cannot flip the row back and forth. It yields once Home
            # Assistant reports the same, or says otherwise for SETTLE seconds.
            if held and not self.busy(e.id) and (held[0] <= now or settled(e, held[1])):
                del self.overlay[e.id]
                held = None
            self.entities.append(replace(e, **held[1]) if held else e)
        self.hidden = {e.id for e in self.entities if e.id in self.cfg["hidden"]
                       or not (e.lighting or e.id in self.cfg["shown"])}
        self.rows = layout(self.entities, self.hidden, self.show_hidden)
        self.ids = [e.id for kind, e in self.rows if kind == "entity"]
        if self.selected not in self.ids:
            self.selected = self.ids[0] if self.ids else None

    def find(self, eid):
        return next((e for e in self.entities if e.id == eid), None)

    def say(self, text):
        self.note, self.note_until = text, time.monotonic() + 3

    def hold(self, e, **fields):
        """Show a change at once instead of waiting for the next poll to confirm it."""
        fields = {**self.overlay.get(e.id, (0, {}))[1], **fields}
        self.overlay[e.id] = [time.monotonic() + SETTLE, fields]
        self.rebuild()

    def move(self, step):
        if self.ids:
            i = self.ids.index(self.selected) + step if self.selected in self.ids else 0
            self.selected = self.ids[max(0, min(len(self.ids) - 1, i))]

    # --- actions -------------------------------------------------------------

    def follows(self, e):
        return e.id in self.cfg["sync"]

    def theme_colour(self):
        return sync.colours(self.theme, self.settings, 1)[0]

    def light_on(self, e, **data):
        if self.follows(e) and e.colour and not e.on:  # wake up already in a theme colour
            data = {**sync.payload(self.settings, e.id, self.theme_colour()), **data}
        self.send("light", "turn_on", {"entity_id": e.id, **data})

    def toggle(self, e):
        if not e.available:
            return self.say(f"{e.name} is unavailable")
        self.wanted.pop(e.id, None)
        target = {"entity_id": e.id}
        if e.domain == "cover":
            if e.moving and e.features & COVER_STOP:
                self.send("cover", "stop_cover", target)
                self.hold(e, state="open")
            elif e.state == "closed":
                self.send("cover", "open_cover", target)
                self.hold(e, state="opening")
            else:
                self.send("cover", "close_cover", target)
                self.hold(e, state="closing")
        elif e.on:
            self.send(e.domain, "turn_off", target)
            self.hold(e, state="off", level=None, rgb=None)
        elif e.domain == "light":
            self.light_on(self.truth.get(e.id, e))
            self.hold(e, state="on")
        else:
            self.send("switch", "turn_on", target)
            self.hold(e, state="on")

    def step(self, e, direction):
        if not e.available:
            return self.say(f"{e.name} is unavailable")
        if not e.dimmable:  # nothing to dim: ← is off / closed, → is on / open
            if (direction > 0) != e.on:
                self.toggle(e)
            return
        level = e.level if e.level is not None else (100 if e.on else 0)
        new = max(0, min(100, level + direction * self.settings["step"]))
        if new != level:
            self.set_level(e, new)

    def set_level(self, e, pct):
        if not e.available or not e.dimmable:
            return
        self.wanted[e.id] = (time.monotonic() + DEBOUNCE, pct)
        if e.domain == "light":
            self.hold(e, state="on" if pct else "off", level=pct or None)
        else:
            self.hold(e, state="open" if pct else "closed", level=pct)

    def commit_level(self, eid, pct):
        e = self.truth.get(eid)
        if not e:
            return
        if e.domain == "cover":
            self.send("cover", "set_cover_position", {"entity_id": eid, "position": pct})
        elif pct <= 0:
            self.send("light", "turn_off", {"entity_id": eid})
        else:
            self.light_on(e, brightness_pct=pct)

    def toggle_sync(self, e):
        if e.domain != "light" or not e.colour:
            return self.say("only colour lights can follow the theme")
        chosen = self.cfg["sync"]
        if e.id in chosen:
            chosen.remove(e.id)
            self.say(f"{e.name} keeps its own colour")
        else:
            chosen.append(e.id)
            self.say(f"{e.name} follows the theme")
            if e.on:
                rgb = self.theme_colour()
                self.send("light", "turn_on", sync.payload(self.settings, e.id, rgb))
                self.hold(e, rgb=rgb)
        self.save(self.cfg)
        self.rebuild()

    def next_mode(self):
        modes = sync.MODES
        mode = self.settings["mode"]
        return modes[(modes.index(mode) + 1) % len(modes)] if mode in modes else modes[0]

    def toggle_mode(self):
        """One tone for every light that follows the theme, its main colours, or a mix of
        all its hues."""
        mode = self.settings["mode"] = self.next_mode()
        self.save(self.cfg)
        self.repaint()
        self.say({"main": "theme lights: the main colours of the theme — R reshuffles",
                  "mix": "theme lights: a mix of the theme colours — R reshuffles"}
                 .get(mode, "theme lights: one tone"))

    def repaint(self):
        """Give the lit lights that follow the theme its colours again; a mix is redealt."""
        lit = [e for e in self.entities if self.follows(e) and e.on and e.colour and e.available]
        if not lit:
            return self.say("no lit lights follow the theme — s on a colour light adds it")
        for _ in range(8):  # a redeal that changes nothing would look like a dead key
            dealt = sync.colours(self.theme, self.settings, len(lit))
            if dealt != self.dealt:
                break
        self.dealt = dealt
        for e, rgb in zip(lit, dealt):
            self.send("light", "turn_on", sync.payload(self.settings, e.id, rgb))
            self.hold(e, rgb=rgb)
        self.say("theme colours reshuffled" if sync.spread(self.settings)
                 else "theme colour applied")

    def recolour(self, e):
        """Give one light another of the colours the mode deals out."""
        if e.domain != "light" or not e.colour:
            return self.say(f"{e.name} has no colour to change")
        if not e.available or not e.on:
            return self.say(f"{e.name} is {'off' if e.available else 'unavailable'}")
        deck = sync.hues(self.theme, self.settings)
        rgb = random.choice([c for c in deck if c != e.rgb] or deck)
        self.send("light", "turn_on", sync.payload(self.settings, e.id, rgb))
        self.hold(e, rgb=rgb)

    def paint(self, e, cell):
        """Give a light the colour of a palette cell: (rgb as drawn, turn_on data)."""
        rgb, data = cell
        if not e.available:
            return self.say(f"{e.name} is unavailable")
        if not e.white:
            return self.say(f"{e.name} has no colour to change")
        if "rgb_color" in data and not e.colour:
            return self.say(f"{e.name} only does shades of white")
        if "color_temp_kelvin" in data and e.kelvin:
            low, high = e.kelvin
            data = {"color_temp_kelvin": max(low, min(high, data["color_temp_kelvin"]))}
        self.wanted.pop(e.id, None)
        self.send("light", "turn_on", {"entity_id": e.id, **data})
        self.hold(e, state="on", rgb=rgb)

    def hide(self, e):
        self.set_hidden([e.id], e.id not in self.hidden)

    def hide_room(self, e):
        room, block = [], []
        for kind, item in self.rows + [("group", None)]:
            if kind == "group":
                if e.id in block:
                    room = block
                block = []
            else:
                block.append(item.id)
        self.set_hidden(room, not all(i in self.hidden for i in room))

    def set_hidden(self, ids, hidden):
        was, at = self.selected, self.ids.index(self.selected) if self.selected in self.ids else 0
        unasked = [i for i in ids if not self.truth[i].lighting]  # hidden unless listed here
        self.cfg["hidden"] = [i for i in self.cfg["hidden"] if i not in ids] + \
            (list(ids) if hidden else [])
        self.cfg["shown"] = [i for i in self.cfg["shown"] if i not in ids] + \
            ([] if hidden else unasked)
        self.save(self.cfg)
        self.rebuild()
        if was not in self.ids and self.ids:  # the row left the list: stay where it was
            self.selected = self.ids[min(at, len(self.ids) - 1)]


def settled(e, want):
    """Does the state Home Assistant reports agree with what was asked for?"""
    state = want.get("state")
    if state in ("opening", "closing"):  # on its way there or already arrived
        if e.state not in (state, "open" if state == "opening" else "closed"):
            return False
    elif state is not None and e.state != state:
        return False
    level = want.get("level")
    if level is not None and (e.level is None or abs(e.level - level) > 2):
        return False  # lights round a percentage to their own steps
    rgb = want.get("rgb")
    if rgb and e.on and (not e.rgb or max(abs(a - b) for a, b in zip(e.rgb, rgb)) > 30):
        return False
    return True


# --- drawing ------------------------------------------------------------------

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def width(s):
    return sum(0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in "WF"
               else 1 for c in s)


def clip(s, n):
    """`s` cut or padded to exactly `n` cells."""
    if width(s) > n:
        while s and width(s) > n - 1:
            s = s[:-1]
        s += "…"
    return s + " " * max(0, n - width(s))


class Line:
    def __init__(self):
        self.parts, self.n = [], 0

    def add(self, text, colour, bold=False, back=None):
        code = f"\x1b[0;{'1;' if bold else ''}38;2;{colour[0]};{colour[1]};{colour[2]}"
        if back:
            code += f";48;2;{back[0]};{back[1]};{back[2]}"
        self.parts.append(f"{code}m{text}")
        self.n += width(text)
        return self

    def pad(self, to):
        if to > self.n:
            self.parts.append("\x1b[0m" + " " * (to - self.n))
            self.n = to
        return self

    def done(self, w):
        return "".join(self.pad(w).parts) + "\x1b[0m"


class View:
    def __init__(self, theme, host):
        self.theme, self.host = theme, host
        self.top = 0
        self.hits = {}      # screen row -> (entity id, first bar column, bar width)
        self.swatches = {}  # screen row -> (first column, palette row)
        self.grid = []      # palette rows of (rgb, light.turn_on data)
        self.resize(80, 24)

    def resize(self, w, h):
        self.full, self.h = max(20, w), max(6, h)
        # The list is a column on the left; a wide window shows the palette beside it,
        # a narrow one shows it in place of the list while a colour is being picked.
        self.side = self.full >= 84
        self.w = max(56, min(76, self.full - 52)) if self.side else min(self.full, 76)

    def hit(self, col, row):
        """What a click landed on: (entity id, "mark" | "bar" | "row", percent),
        or (None, "swatch", (palette row, column))."""
        if row in self.swatches and col >= self.swatches[row][0]:
            x, r = self.swatches[row]
            return (None, "swatch", (r, (col - x) // 2)) if (col - x) // 2 < len(self.grid[r]) \
                else None
        if row not in self.hits:
            return None
        eid, bar_x, bar_w = self.hits[row]
        if bar_w and bar_x <= col < bar_x + bar_w:
            return eid, "bar", round((col - bar_x + 1) / bar_w * 100)
        return eid, "mark" if col <= 3 else "row", 0

    def frame(self, app):
        return "\x1b[?2026h\x1b[H" + "\r\n".join(self.lines(app)) + "\x1b[?2026l"

    def plain(self, app, colour=False):
        text = "\n".join(self.lines(app))
        return text if colour else "\n".join(s.rstrip() for s in _ANSI.sub("", text).split("\n"))

    def lines(self, app):
        t, w = self.theme, self.w
        blank = Line().done(w)
        body, at = [], {}  # at: body line -> entity
        for kind, item in app.rows:
            if kind == "group":
                if body:
                    body.append(blank)
                body.append(Line().add("  " + clip(item, w - 4), t.muted).done(w))
            else:
                at[len(body)] = item
                body.append(None)  # drawn below, once the viewport is known
        if not body:
            body.append(Line().add("  " + clip(self.empty(app), w - 4), t.dim).done(w))

        room = self.h - 4
        chosen = next((i for i, e in at.items() if e.id == app.selected), 0)
        # Keep the selection on screen, with its room caption when that is the line above.
        self.top = min(self.top, max(0, chosen - 2))
        self.top = max(self.top, chosen - room + 1)
        self.top = max(0, min(self.top, len(body) - room))
        self.hits = {}
        out = [self.header(app), blank]
        for i in range(self.top, self.top + room):
            if i >= len(body):
                out.append(blank)
            elif i in at:
                out.append(self.entity(app, at[i], 2 + i - self.top))
            else:
                out.append(body[i])
        out += [blank, self.footer(app)]

        rest = self.full - w
        self.swatches = {}
        if self.side:
            panel = self.colours(app, rest, w, room)
        elif app.palette:
            out[2:2 + room] = self.colours(app, w, 0, room)
            self.hits = {}
        if not self.side:
            panel = [" " * rest] * room
        gap = [" " * rest]
        return [left + right for left, right in zip(out, gap * 2 + panel + gap + [""])]

    def colours(self, app, pw, x0, room):
        """The palette as `room` lines of `pw` cells; its first cell is screen column `x0`."""
        t = self.theme
        e = app.find(app.selected)
        n = max(4, min(24, (pw - 4) // 2))
        self.grid = [[(c, {"rgb_color": list(c)}) for c in t.palette()[:n]]]
        for saturation in (1.0, 0.8, 0.6, 0.4, 0.22):
            row = [hue_rgb(i / n, saturation) for i in range(n)]
            self.grid.append([(c, {"rgb_color": list(c)}) for c in row])
        whites = [round((2200 + (6500 - 2200) * i / (n - 1)) / 50) * 50 for i in range(n)]
        self.grid.append([(kelvin_rgb(k), {"color_temp_kelvin": k}) for k in whites])

        can_white = bool(e and e.available and e.white and e.id not in app.hidden)
        can_colour = can_white and e.colour
        blank = Line().done(pw)
        if not e:
            caption, tone = "", t.rim
        elif can_white:
            caption, tone = e.name, t.bright if app.palette else t.dim
        else:
            caption, tone = f"{e.name} — no colour", t.rim
        lines = [Line().add("  " + clip(caption, pw - 4), tone, app.palette and can_white).done(pw)]
        for r, row in enumerate(self.grid):
            if r in (0, 1, len(self.grid) - 1):  # theme hues, the rainbow and the whites apart
                lines.append(blank)
            live = can_white if r == len(self.grid) - 1 else can_colour
            if len(lines) < room:
                self.swatches[2 + len(lines)] = (x0 + 2, r)
            line = Line().add("  ", t.fg)
            for c, (rgb, _) in enumerate(row):
                shown = rgb if live else mix(t.bg, rgb, 0.22)
                if app.palette and app.cursor == (r, c):
                    line.add("▐▌", contrast(shown), back=shown)
                else:
                    line.add("  ", t.fg, back=shown)
            lines.append(line.done(pw))
        return (lines + [blank] * room)[:room]

    def empty(self, app):
        if not app.loaded:
            return "cannot reach Home Assistant" if app.error else "connecting…"
        if app.entities:
            return "everything is hidden — a shows it, x brings a row back"
        return "no lights, switches or covers here"

    def header(self, app):
        t, w = self.theme, self.w
        line = Line().add("  hatuira", t.accent, True)
        right = Line()
        if app.loaded and app.cfg["sync"] and w >= 40:
            if sync.spread(app.settings):
                for c in t.main() if app.settings["mode"] == "main" else t.palette()[:5]:
                    right.add("◈", c)
                right.add(" ", t.fg)
            else:
                right.add("◈ ", t.colour(app.settings["color"]))
            right.add("theme   ", t.muted)
        if app.error:
            right.add("offline", t.red)
        elif app.loaded:
            lit = sum(e.on for e in app.entities
                      if e.domain != "cover" and e.id not in app.hidden)
            right.add(f"{lit} on", t.dim)
        else:
            right.add("connecting…", t.muted)
        if w - 12 - right.n > 3:
            line.add("  " + clip(self.host, w - 12 - right.n).rstrip(), t.muted)
        line.pad(w - 2 - right.n)
        line.parts += right.parts
        line.n += right.n
        return line.done(w)

    def entity(self, app, e, row):
        t, w = self.theme, self.w
        chosen = e.id == app.selected
        back = t.selection if chosen else None
        hidden = e.id in app.hidden
        live = e.available and not hidden
        lit = (e.rgb or t.yellow) if e.domain == "light" else t.fg

        bar_w = 14 if w >= 64 else 8 if w >= 48 else 0
        name_w = max(1, w - 10 - STATE_W - (bar_w + 2 if bar_w else 0))
        if chosen:
            name = t.bright
        else:
            name = t.rim if hidden else t.fg if e.on and live else t.dim

        line = Line().add(" ", t.fg).add(" ", t.fg, back=back)
        line.add(MARKS[e.domain][not (e.on and e.available)],
                 lit if e.on and live else t.muted if live else t.rim, back=back)
        line.add(" " + clip(e.name, name_w), name, chosen, back)
        if bar_w:
            line.add("  ", t.fg, back=back)
            if e.dimmable and live:
                self.hits[row] = (e.id, line.n, bar_w)
                level = e.level if e.level is not None and e.on else 0
                full = min(bar_w, max(1, round(level / 100 * bar_w))) if level else 0
                line.add("━" * full, lit, back=back).add("─" * (bar_w - full), t.rim, back=back)
            else:
                line.add(" " * bar_w, t.fg, back=back)
        self.hits.setdefault(row, (e.id, 0, 0))
        line.add(self.state(e).rjust(STATE_W), t.fg if e.on and live else t.muted if live else t.rim,
                 back=back)
        if hidden:
            line.add(" × ", t.muted, back=back)
        elif app.follows(e):
            tone = e.rgb or t.muted if sync.spread(app.settings) else \
                t.colour(app.settings["color"])
            line.add(" ◈ ", tone, back=back)
        else:
            line.add("   ", t.fg, back=back)
        return line.add(" ", t.fg).done(w)

    @staticmethod
    def state(e):
        if not e.available:
            return "—"
        if e.domain == "cover":
            if e.moving:
                return ("↑ " if e.state == "opening" else "↓ ") + \
                    (f"{e.level}%" if e.level is not None else "")
            if e.state == "closed":
                return "closed"
            return f"{e.level}%" if e.level is not None and e.level < 100 else "open"
        if not e.on:
            return "off"
        return f"{e.level}%" if e.level is not None else "on"

    def footer(self, app):
        t, w = self.theme, self.full  # the hints may run under the palette
        if app.note or app.error:
            text, colour = (app.note, t.fg) if app.note else (app.error, t.red)
            return Line().add("  " + clip(text, w - 4), colour).done(w)
        e = app.find(app.selected)
        keys = []
        if app.palette:
            keys = [("←↑↓→", "colour"), ("enter", "done")]
        elif e:
            keys.append(("space", "open / close" if e.domain == "cover" else "on / off"))
            if e.dimmable:
                keys.append(("←→", "position" if e.domain == "cover" else "brightness"))
            if e.white:
                keys.append(("c", "colour"))
            if e.colour:
                keys.append(("s", "theme"))
                keys.append(("r", "recolour"))
            if app.cfg["sync"]:
                keys.append(("S", {"single": "one tone", "main": "main colours",
                                   "mix": "mix"}[app.next_mode()]))
            keys.append(("x", "show" if e.id in app.hidden else "hide"))
        if app.hidden and not app.palette:
            keys.append(("a", "fewer" if app.show_hidden else f"{len(app.hidden)} hidden"))
        if app.cfg["sync"] and not app.palette:
            keys.append(("R", "reshuffle" if sync.spread(app.settings) else "repaint"))
        keys += [("q", "quit"), ("u", "refresh")]
        line = Line().add(" ", t.fg)
        for key, label in keys:
            if line.n + width(key) + width(label) + 3 > w - 2:
                break
            line.add(" " + key, t.dim).add(" " + label + " ", t.muted)
        return line.done(w)


# --- running ------------------------------------------------------------------

def handle(app, view, key):
    """Act on one input event; False ends the program."""
    e = app.find(app.selected)
    if key == "q":
        return False
    if isinstance(key, tuple):
        hit = view.hit(key[1], key[2])
        if hit and hit[1] == "swatch":
            app.cursor = r, c = hit[2]
            if e:
                app.paint(e, view.grid[r][c])
        elif hit:
            eid, part, pct = hit
            app.selected = eid
            if part == "mark":
                app.toggle(app.find(eid))
            elif part == "bar":
                app.set_level(app.find(eid), pct)
    elif app.palette:
        r, c = app.cursor
        if key in ("esc", "c", "enter") or not view.grid or e is None:
            app.palette = False
            return True
        if key in ("up", "k", "down", "j"):
            r = max(0, min(len(view.grid) - 1, r + (-1 if key in ("up", "k") else 1)))
        elif key in ("left", "h", "right", "l"):
            c += -1 if key in ("left", "h") else 1
        elif key != "space":
            return True
        # The lamp follows the cursor: every move is sent, no key to confirm it.
        app.cursor = r, c = (r, max(0, min(len(view.grid[r]) - 1, c)))
        app.paint(e, view.grid[r][c])
    elif key in ("up", "k"):
        app.move(-1)
    elif key in ("down", "j"):
        app.move(1)
    elif key == "S":
        app.toggle_mode()
    elif key == "a":
        app.show_hidden = not app.show_hidden
        app.rebuild()
    elif key == "R":
        app.repaint()
    elif key == "u":
        app.refresh()
    elif e is None:
        pass
    elif key in ("space", "enter"):
        app.toggle(e)
    elif key in ("left", "h"):
        app.step(e, -1)
    elif key in ("right", "l"):
        app.step(e, 1)
    elif key == "s":
        app.toggle_sync(e)
    elif key == "r":
        app.recolour(e)
    elif key == "c":
        if e.white and e.available:
            app.palette = True
            r = len(view.grid) - 1 if view.grid and not e.colour else app.cursor[0]
            app.cursor = (r, app.cursor[1])  # a white-only light starts on the row it can use
        else:
            app.say(f"{e.name} has no colour to change")
    elif key == "x":
        app.hide(e)
    elif key == "X":
        app.hide_room(e)
    return True


def run(client, cfg, save, host):
    theme = Theme()
    with Terminal() as term:
        app = App(client, cfg, save, theme, term.wake)
        view = View(theme, host)
        app.start()
        shown = None
        # The process sleeps until a key, a resize or the poller wakes it, and looks at
        # the theme file twice a second so a new Omarchy theme shows at once.
        while True:
            if term.resized:
                term.resized = False
                view.resize(*term.size())
                shown = None
            if theme.changed():  # the hook is repainting the lamps: watch them change
                app.brisk_until = time.monotonic() + SETTLE + 2
                app.kick.set()
            app.drain()
            frame = view.frame(app)
            if frame != shown:
                term.write(frame)
                shown = frame
            due = app.timeout()
            for key in term.keys(0.5 if due is None else min(0.5, due)):
                if not handle(app, view, key):
                    return


def snapshot(client, cfg, size, colour, host):
    """Headless: fetch once and print one frame."""
    app = App(client, cfg, lambda _: None, Theme())
    app.take(*app.fetch())
    view = View(app.theme, host)
    view.resize(*size)
    print(view.plain(app, colour))
