import unittest
from dataclasses import asdict

from virtualkvm.display_layout import (
    Placement, arrange, decode_layout, display_inventory, fit_windows, validate_layout,
)


class DisplayLayoutTests(unittest.TestCase):
    def test_host_topology_keeps_portrait_retina_and_negative_coordinates(self):
        payload = [dict(index=7, name="Portrait", x=-1080, y=-200, width=1080, height=1920),
                   dict(index=2, name="Retina", x=0, y=0, width=1440, height=900,
                        pixel_width=2880, pixel_height=1800)]
        inventory = display_inventory(payload)
        entries = arrange([Placement(2), Placement(7)], "host", inventory)
        self.assertEqual([(e.source, e.x, e.y, e.width, e.height) for e in entries],
                         [(2, 0, 0, 1440, 900), (7, -1080, -200, 1080, 1920)])
        self.assertEqual(inventory[1].pixel_width, 2880)
        self.assertEqual(payload[0]["x"], -1080)

    def test_inventory_rejects_unsafe_or_ambiguous_remote_geometry(self):
        valid = dict(index=0, name="Display", x=0, y=0, width=1920, height=1080)
        for payload in ([], [valid] * 2, [valid] * 65,
                        [{**valid, "index": True}], [{**valid, "width": 0}],
                        [{**valid, "pixel_width": False}], [{**valid, "pixel_height": 0}],
                        [{**valid, "x": 2 ** 60}], [{**valid, "name": "bad\nname"}]):
            with self.subTest(payload=str(payload)[:100]), self.assertRaises(ValueError):
                display_inventory(payload)

    def test_eight_screens_can_be_arranged_without_rewriting_source_ids(self):
        entries = [Placement(i, width=1280, height=720) for i in range(8)]
        validate_layout(entries)
        grid = arrange(entries, "grid")
        self.assertEqual([e.source for e in grid], list(range(8)))
        self.assertEqual(len({(e.x, e.y) for e in grid}), 8)
        self.assertTrue(any(e.y > 0 for e in grid))
        with self.assertRaises(ValueError):
            validate_layout(entries + [Placement(8)])
        with self.assertRaises(ValueError):
            validate_layout([Placement(2), Placement(2)])

    def test_negative_relative_desktop_fits_a_left_hand_local_monitor(self):
        entries = [Placement(5, -1200, -900, 1080, 1920), Placement(2, 0, 0, 1920, 1080)]
        rectangles = fit_windows(entries, (-1600, 40, 1600, 900))
        self.assertLess(rectangles[5][0], rectangles[2][0])
        self.assertLess(rectangles[5][1], rectangles[2][1])
        for x, y, width, height in rectangles.values():
            self.assertGreaterEqual(x, -1600)
            self.assertGreaterEqual(y, 40)
            self.assertLessEqual(x + width, 0)
            self.assertLessEqual(y + height, 940)
        self.assertEqual(entries[0].x, -1200)

    def test_fullscreen_mapping_requires_distinct_local_outputs(self):
        with self.assertRaises(ValueError):
            validate_layout([Placement(0, target="local"), Placement(1, target="local")], True, "monitors")
        with self.assertRaises(ValueError):
            validate_layout([Placement(0)], True, "monitors")
        validate_layout([Placement(0, target="left"), Placement(4, target="right")], True, "monitors")

    def test_preferences_roundtrip_and_reject_corrupt_layouts(self):
        entries = [Placement(3, -300, 100, 1080, 1920, "left"), Placement(7, target="right")]
        data = dict(version=1, presentation="monitors", fullscreen=True,
                    entries=[asdict(entry) for entry in entries])
        self.assertEqual(decode_layout(data), (entries, True, "monitors"))
        for bad in ({**data, "version": True}, {**data, "entries": [{}]},
                    {**data, "entries": [{"source": 65}]}, {**data, "fullscreen": "yes"},
                    {**data, "entries": [{"source": 0, "x": 2 ** 40}]}):
            with self.subTest(value=bad), self.assertRaises(ValueError):
                decode_layout(bad)


if __name__ == "__main__":
    unittest.main()
