import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from hatuira import config, sync
from hatuira.app import App, View, handle
from hatuira.hass import ApiError, Demo, Hass
from hatuira.model import entity, layout
from hatuira.term import _LAYOUT
from hatuira.theme import Theme
from hatuira.wizard import normalise


def light(state="on", **attrs):
    return entity({"entity_id": "light.x", "state": state, "attributes": attrs})


def make(hidden=(), synced=()):
    cfg = {"url": "", "hidden": list(hidden), "shown": [], "sync": list(synced),
           "settings": dict(config.DEFAULTS)}
    saved = []
    app = App(Demo(), cfg, lambda c: saved.append(json.loads(json.dumps(c))), Theme(path=""))
    app.take(*app.fetch())
    return app, saved


def flush(app):
    """Send the debounced levels now instead of a quarter of a second later."""
    for eid, (_, pct) in list(app.wanted.items()):
        del app.wanted[eid]
        app.commit_level(eid, pct)


class Keys(unittest.TestCase):
    def test_every_letter_key_works_on_a_russian_layout(self):
        self.assertEqual("йкчфыолрдсгЧЫК".translate(_LAYOUT), "qrxasjkhlcuXSR")


class Model(unittest.TestCase):
    def test_light(self):
        e = light(brightness=128, rgb_color=[255, 0, 10], supported_color_modes=["hs"])
        self.assertEqual((e.level, e.rgb, e.dimmable, e.colour), (50, (255, 0, 10), True, True))
        self.assertEqual(light("off", brightness=None, supported_color_modes=["onoff"]).dimmable,
                         False)
        self.assertFalse(light(supported_color_modes=["color_temp"]).colour)
        self.assertFalse(light("unavailable").available)

    def test_cover(self):
        e = entity({"entity_id": "cover.c", "state": "opening",
                    "attributes": {"supported_features": 15, "current_position": 40}})
        self.assertTrue(e.on and e.moving and e.dimmable)
        self.assertEqual(e.level, 40)

    def test_layout_groups_by_room_then_falls_back_to_kind(self):
        demo = Demo()
        rooms = demo.areas()
        ents = [entity(s, rooms.get(s["entity_id"], "")) for s in demo.states()]
        titles = [item for kind, item in layout(ents) if kind == "group"]
        self.assertEqual(titles, ["Bedroom", "Hallway", "Kitchen", "Living room", "other"])
        ents = [entity(s) for s in demo.states()]
        self.assertEqual([i for k, i in layout(ents) if k == "group"],
                         ["lights", "switches", "covers"])

    def test_hidden_rows(self):
        ents = [entity(s) for s in Demo().states()]
        ids = lambda rows: {e.id for k, e in rows if k == "entity"}
        self.assertNotIn("light.porch", ids(layout(ents, {"light.porch"})))
        self.assertIn("light.porch", ids(layout(ents, {"light.porch"}, True)))


class Actions(unittest.TestCase):
    def test_toggle_and_level(self):
        app, _ = make()
        app.toggle(app.find("light.ceiling"))
        self.assertTrue(app.find("light.ceiling").on)
        app.step(app.find("light.floor_lamp"), -1)
        self.assertEqual(app.find("light.floor_lamp").level, 60)  # shown before it is sent
        self.assertEqual(app.client.calls[-1][1], "turn_on")      # still the ceiling
        flush(app)
        self.assertEqual(app.client.calls[-1],
                         ("light", "turn_on", {"entity_id": "light.floor_lamp", "brightness_pct": 60}))

    def test_level_down_to_zero_switches_off(self):
        app, _ = make()
        app.set_level(app.find("light.tv_strip"), 0)
        flush(app)
        self.assertEqual(app.client.calls[-1][1], "turn_off")
        self.assertFalse(app.find("light.tv_strip").on)

    def test_cover(self):
        app, _ = make()
        app.toggle(app.find("cover.bedroom_blinds"))
        self.assertEqual(app.client.calls[-1][1], "open_cover")
        app.set_level(app.find("cover.living_curtains"), 20)
        flush(app)
        self.assertEqual(app.client.calls[-1][2], {"entity_id": "cover.living_curtains", "position": 20})

    def test_switch_arrows_and_unavailable(self):
        app, _ = make()
        app.step(app.find("switch.hallway"), 1)
        self.assertTrue(app.find("switch.hallway").on)
        calls = len(app.client.calls)
        app.toggle(app.find("light.porch"))
        self.assertEqual(len(app.client.calls), calls)
        self.assertIn("unavailable", app.note)

    def test_hide_and_room(self):
        app, saved = make()
        app.selected = "light.bedside"
        app.hide(app.find("light.bedside"))
        self.assertEqual(saved[-1]["hidden"], ["light.bedside"])
        self.assertEqual(app.selected, "cover.bedroom_blinds")
        app.selected = "light.floor_lamp"  # hiding mid-list keeps the cursor where it was
        app.hide(app.find("light.floor_lamp"))
        self.assertEqual(app.selected, "light.tv_strip")
        app.hide(app.find("cover.living_curtains"))
        self.assertEqual(app.selected, "light.tv_strip")
        app.hide_room(app.find("light.tv_strip"))
        self.assertEqual(app.selected, "light.porch")
        self.assertNotIn("light.ceiling", app.ids)
        self.assertEqual(len(app.cfg["hidden"]), 5)
        app.show_hidden = True
        app.rebuild()
        app.hide_room(app.find("light.floor_lamp"))  # all of the room hidden: bring it back
        self.assertEqual(app.cfg["hidden"], ["light.bedside"])

    def test_only_lighting_switches_are_listed_unasked(self):
        app, saved = make()
        self.assertIn("switch.kitchen_counter", app.ids)  # "Counter light"
        self.assertIn("switch.hallway", app.ids)          # a lamp icon
        for eid in ("switch.mikrotik_port_ether2", "switch.phone_do_not_disturb",
                    "switch.adguard_filtering"):
            self.assertNotIn(eid, app.ids)
        app.show_hidden = True
        app.rebuild()
        app.hide(app.find("switch.adguard_filtering"))  # x on a hidden row brings it back
        self.assertEqual((saved[-1]["shown"], saved[-1]["hidden"]), (["switch.adguard_filtering"], []))
        app.show_hidden = False
        app.rebuild()
        self.assertIn("switch.adguard_filtering", app.ids)
        app.hide(app.find("switch.adguard_filtering"))
        self.assertEqual((saved[-1]["shown"], saved[-1]["hidden"]), ([], ["switch.adguard_filtering"]))

    def test_light_words(self):
        def sw(name, eid="switch.x", **attrs):
            return entity({"entity_id": eid, "state": "on",
                           "attributes": {"friendly_name": name, **attrs}}).lighting
        for name in ("Свет в коридоре", "Kitchen light", "Настольная лампа", "LED strip", "Люстра"):
            self.assertTrue(sw(name), name)
        self.assertTrue(sw("Relay 2", "switch.bedroom_lamp_relay"))
        self.assertTrue(sw("Relay 2", icon="mdi:lightbulb"))
        for name in ("Do not disturb", "MikroTik NAT rule", "Disabled", "Flight mode", "Светлана VPN"):
            self.assertEqual(sw(name), name == "Светлана VPN", name)

    def test_keys_and_clicks(self):
        app, _ = make()
        view = View(app.theme, "demo")
        view.resize(80, 30)
        view.lines(app)
        handle(app, view, "down")
        self.assertEqual(app.selected, app.ids[1])
        row = next(r for r, hit in view.hits.items() if hit[0] == "light.floor_lamp")
        _, bar_x, bar_w = view.hits[row]
        handle(app, view, ("click", bar_x + bar_w - 1, row))
        self.assertEqual((app.selected, app.find("light.floor_lamp").level), ("light.floor_lamp", 100))
        handle(app, view, ("click", 2, row))
        self.assertFalse(app.find("light.floor_lamp").on)
        self.assertFalse(handle(app, view, "q"))

    def test_frame_fits_the_window(self):
        app, _ = make(hidden=["light.porch"], synced=["light.tv_strip"])
        from hatuira.app import width
        for size in ((120, 40), (80, 24), (44, 10), (20, 6)):
            view = View(app.theme, "homeassistant.local:8123")
            view.resize(*size)
            lines = view.plain(app, colour=True).split("\n")
            self.assertEqual(len(lines), size[1])
            import re
            for line in lines:
                self.assertEqual(width(re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", line)), size[0])


class ThemeSync(unittest.TestCase):
    def theme(self, accent="#102030"):
        f = tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False)
        f.write(f'mode = "dark"\naccent = "{accent}"\nred = "#ff0000"\n')
        f.close()
        return Theme(path=f.name)

    def test_only_lit_colour_lights_are_repainted(self):
        demo = Demo()
        cfg = {"sync": ["light.tv_strip", "light.bedside", "light.kitchen_spots"],
               "settings": dict(config.DEFAULTS)}
        self.assertEqual(sync.apply(demo, cfg, self.theme()), ["light.tv_strip"])
        self.assertEqual(demo.calls, [("light", "turn_on", {
            "entity_id": ["light.tv_strip"], "rgb_color": [16, 32, 48], "transition": 1})])
        self.assertEqual(demo.items["light.bedside"]["state"], "off")

    def test_colour_key_setting(self):
        demo = Demo()
        cfg = {"sync": ["light.tv_strip"], "settings": {**config.DEFAULTS, "color": "red"}}
        sync.apply(demo, cfg, self.theme())
        self.assertEqual(demo.calls[0][2]["rgb_color"], [255, 0, 0])

    def test_synced_light_wakes_in_theme_colour(self):
        app, saved = make()
        app.toggle_sync(app.find("light.bedside"))
        self.assertEqual(saved[-1]["sync"], ["light.bedside"])
        self.assertEqual(app.client.calls, [])  # it is off: nothing to paint yet
        app.toggle(app.find("light.bedside"))
        self.assertEqual(app.client.calls[-1][2]["rgb_color"], list(app.theme.accent))
        app.toggle_sync(app.find("light.kitchen_spots"))
        self.assertIn("only colour lights", app.note)


class Colours(unittest.TestCase):
    def test_mix_deals_out_the_theme_hues(self):
        import random
        demo = Demo()
        cfg = {"sync": ["light.tv_strip", "light.floor_lamp"],
               "settings": {**config.DEFAULTS, "mode": "mix"}}
        theme = Theme(path="")
        self.assertEqual(len(sync.apply(demo, cfg, theme, random.Random(1))), 2)
        sent = [tuple(c[2]["rgb_color"]) for c in demo.calls]
        self.assertEqual(len(set(sent)), 2)
        self.assertTrue(set(sent) <= set(theme.palette()))

    def theme(self, body):
        f = tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False)
        f.write(body)
        f.close()
        return Theme(path=f.name)

    def test_hues_come_from_the_terminal_colours_when_unnamed(self):
        theme = self.theme('accent = "#00a8ff"\ncolor1 = "#ff004d"\ncolor2 = "#00ff88"\n')
        self.assertEqual(theme.palette()[:2], [(0, 168, 255), (255, 0, 77)])
        self.assertEqual(theme.green, (0, 255, 136))
        named = self.theme('accent = "#00a8ff"\nred = "#aa0000"\ncolor1 = "#ff004d"\n')
        self.assertEqual(named.red, (170, 0, 0))

    def test_main_colours_of_the_theme(self):
        # the accent and a foreground of its own; the cursor is a shade of the accent,
        # the selection too dark to be a colour
        theme = self.theme('accent = "#00BCD4"\ncursor = "#00E5FF"\n'
                           'selection_background = "#003333"\nforeground = "#FF69B4"\n')
        self.assertEqual(theme.main(), [(0, 188, 212), (255, 105, 180)])
        # nothing but the accent stands out: it is paired with the hue furthest from it
        plain = self.theme('accent = "#c86a24"\nforeground = "#dddddd"\n'
                           'color1 = "#8e2f22"\ncolor5 = "#3c8f95"\ncolor3 = "#b88a42"\n')
        self.assertEqual(plain.main(), [(200, 106, 36), (60, 143, 149)])
        three = self.theme('accent = "#0000ff"\nactive_border_color = "#ffff00"\n'
                           'cursor = "#ff0000"\nforeground = "#00ff00"\n')
        self.assertEqual(len(three.main()), 3)

    def test_main_mode_deals_out_only_the_main_colours(self):
        import random
        theme = self.theme('accent = "#00BCD4"\nforeground = "#FF69B4"\ncolor1 = "#ff0000"\n')
        settings = {**config.DEFAULTS, "mode": "main"}
        dealt = sync.colours(theme, settings, 5, random.Random(3))
        self.assertEqual(set(dealt), {(0, 188, 212), (255, 105, 180)})
        self.assertNotEqual(dealt[0], dealt[1])

    def test_r_recolours_only_the_selected_light(self):
        app, _ = make(synced=["light.tv_strip", "light.floor_lamp"])
        app.settings["mode"] = "mix"
        view = View(app.theme, "demo")
        view.resize(80, 30)
        view.lines(app)
        app.selected = "light.tv_strip"
        for _ in range(6):  # never the colour it already has
            before = app.find("light.tv_strip").rgb
            handle(app, view, "r")
            call = app.client.calls[-1][2]
            self.assertEqual(call["entity_id"], "light.tv_strip")
            self.assertIn(tuple(call["rgb_color"]), app.theme.palette())
            self.assertNotEqual(tuple(call["rgb_color"]), before)
        self.assertEqual({c[2]["entity_id"] for c in app.client.calls}, {"light.tv_strip"})
        handle(app, view, "R")
        self.assertEqual({c[2]["entity_id"] for c in app.client.calls[-2:]},
                         {"light.tv_strip", "light.floor_lamp"})
        app.selected = "light.bedside"
        handle(app, view, "r")
        self.assertIn("is off", app.note)

    def test_mode_toggle_repaints_lit_synced_lights(self):
        app, saved = make(synced=["light.tv_strip", "light.floor_lamp", "light.bedside"])
        app.toggle_mode()
        self.assertEqual(saved[-1]["settings"]["mode"], "main")
        self.assertTrue({tuple(c[2]["rgb_color"]) for c in app.client.calls}
                        <= set(app.theme.main()))
        app.toggle_mode()
        self.assertEqual(saved[-1]["settings"]["mode"], "mix")
        del app.client.calls[:-2]
        self.assertEqual(sorted(c[2]["entity_id"] for c in app.client.calls),
                         ["light.floor_lamp", "light.tv_strip"])
        for _ in range(6):  # R deals the hues again, never the same hand twice in a row
            before = [c[2]["rgb_color"] for c in app.client.calls[-2:]]
            app.repaint()
            self.assertNotEqual([c[2]["rgb_color"] for c in app.client.calls[-2:]], before)
        app.toggle_mode()
        self.assertEqual(app.client.calls[-1][2]["rgb_color"], list(app.theme.accent))

    def test_palette(self):
        app, _ = make()
        view = View(app.theme, "demo")
        view.resize(120, 30)
        app.selected = "light.floor_lamp"
        view.lines(app)
        self.assertEqual(len(view.grid), 7)
        row = next(r for r, (_, g) in view.swatches.items() if g == 1)
        x = view.swatches[row][0]
        handle(app, view, ("click", x + 2 * 8 + 1, row))  # a third of the way round: green
        self.assertEqual(app.client.calls[-1][2], {"entity_id": "light.floor_lamp", "rgb_color": [0, 255, 0]})
        self.assertEqual(app.selected, "light.floor_lamp")
        app.selected = "light.kitchen_spots"  # white only
        calls = len(app.client.calls)
        handle(app, view, ("click", x, row))
        self.assertEqual(len(app.client.calls), calls)
        self.assertIn("shades of white", app.note)
        handle(app, view, "c")
        self.assertTrue(app.palette)
        for key in ("left",) * 30 + ("enter",):  # the lamp follows the cursor
            handle(app, view, key)
        self.assertEqual(app.client.calls[-1][2], {"entity_id": "light.kitchen_spots", "color_temp_kelvin": 2200})
        self.assertFalse(app.palette)
        app.selected = "switch.kitchen_counter"
        handle(app, view, "c")
        self.assertFalse(app.palette)

    def test_narrow_window_shows_the_palette_in_place_of_the_list(self):
        app, _ = make()
        view = View(app.theme, "demo")
        view.resize(60, 20)
        view.lines(app)
        self.assertFalse(view.swatches)
        app.selected = "light.floor_lamp"
        handle(app, view, "c")
        view.lines(app)
        self.assertTrue(view.swatches and not view.hits)


class Slow(Demo):
    def call(self, domain, service, data):
        import time
        time.sleep(0.15)
        super().call(domain, service, data)


class Settling(unittest.TestCase):
    def test_rapid_presses_send_only_the_first_and_the_last(self):
        import time
        app, _ = make()
        app.client = Slow()
        app.start()
        stale = app.client.states()
        for _ in range(7):
            app.toggle(app.find("light.ceiling"))
        self.assertTrue(app.find("light.ceiling").on)
        app.take(stale, None)  # a poll from before the commands must not flip the row back
        self.assertTrue(app.find("light.ceiling").on)
        deadline = time.time() + 5
        while app.busy("light.ceiling") and time.time() < deadline:
            time.sleep(0.02)
        sent = [c[1] for c in app.client.calls]  # the first press and the last wish at most
        self.assertTrue(sent in (["turn_on"], ["turn_on", "turn_on"]), sent)
        app.drain()
        app.take(app.client.states(), None)  # confirmed: the real state takes over
        self.assertTrue(app.find("light.ceiling").on)
        self.assertEqual(app.overlay, {})

    def test_unconfirmed_change_gives_way_after_the_timeout(self):
        import time
        from hatuira import app as module
        app, _ = make()
        stale = app.client.states()
        app.hold(app.find("light.ceiling"), state="on")
        app.take(stale, None)
        self.assertTrue(app.find("light.ceiling").on)
        app.overlay["light.ceiling"][0] = time.monotonic() - 1
        app.take(stale, None)
        self.assertFalse(app.find("light.ceiling").on)


class FakeHass(BaseHTTPRequestHandler):
    seen = []

    def reply(self, code, body):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.headers.get("Authorization") != "Bearer good":
            return self.reply(401, b"401: Unauthorized")
        if self.path == "/api/":
            return self.reply(200, {"message": "API running."})
        self.reply(200, Demo().states() + [{"entity_id": "sensor.t", "state": "21", "attributes": {}}])

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeHass.seen.append((self.path, body))
        if self.path == "/api/template":
            return self.reply(200, b"light.ceiling|Living room\nlight.porch|\n")
        if self.path.endswith("/nope"):
            return self.reply(400, {"message": "Service light.nope not found."})
        self.reply(200, [])

    def log_message(self, *_):
        pass


class Rest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), FakeHass)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_round_trip(self):
        hass = Hass(self.url + "/", "good")
        hass.ping()
        self.assertEqual({s["entity_id"].split(".")[0] for s in hass.states()},
                         {"light", "switch", "cover"})
        self.assertEqual(hass.areas(), {"light.ceiling": "Living room"})
        hass.call("light", "turn_on", {"entity_id": "light.ceiling", "brightness_pct": 40})
        self.assertEqual(FakeHass.seen[-1], ("/api/services/light/turn_on",
                                             {"entity_id": "light.ceiling", "brightness_pct": 40}))

    def test_errors(self):
        with self.assertRaisesRegex(ApiError, "token rejected"):
            Hass(self.url, "bad").ping()
        with self.assertRaisesRegex(ApiError, "not found"):
            Hass(self.url, "good").call("light", "nope", {})
        with self.assertRaises(ApiError):
            Hass("http://127.0.0.1:1", "good", timeout=2).states()

    def test_url(self):
        self.assertEqual(normalise("ha.local:8123/lovelace/0"), "http://ha.local:8123")
        self.assertEqual(normalise("https://ha.example.org/"), "https://ha.example.org")


if __name__ == "__main__":
    unittest.main()
