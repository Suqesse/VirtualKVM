import ctypes as C
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from virtualkvm import native


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.device = native.NativeInput(dry_run=True)

    def test_normalized_keys_and_complete_release(self):
        for key in ("Control", "a", "RightShift"):
            self.device.inject({"type": "key", "key": key, "down": True})
        self.device.inject({"type": "button", "button": "left", "down": True})
        self.device.release_all()
        releases = self.device.events[4:]
        self.assertEqual(releases[0], {"type": "button", "button": "left", "down": False})
        self.assertEqual(releases[1], {"type": "key", "key": "a", "down": False})
        self.assertEqual({r["key"] for r in releases[2:]}, {"ctrl", "rightshift"})
        count = len(self.device.events)
        self.device.release_all()
        self.assertEqual(count, len(self.device.events))

    def test_invalid_events_cannot_enter_input_queue(self):
        bad = [None, [], {}, {"type": []}, {"type": "arbitrary"},
               {"type": "key", "key": "a", "down": 1},
               {"type": "key", "key": "unknown", "down": True},
               {"type": "key", "key": "a", "down": True, "command": "anything"},
               {"type": "button", "button": "extra", "down": True},
               {"type": "pointer", "x": float("nan"), "y": 0},
               {"type": "pointer", "x": 1.001, "y": 0},
               {"type": "pointer", "x": False, "y": 0},
               {"type": "pointer", "x": 0, "y": 0, "display": True},
               {"type": "pointer", "x": 0, "y": 0, "display": -1},
               {"type": "scroll", "dy": float("inf")},
               {"type": "scroll", "dx": 1001}]
        for event in bad:
            with self.subTest(event=event), self.assertRaises(ValueError):
                self.device.inject(event)
        self.assertEqual(self.device.events, ())

    def test_synthetic_event_history_is_bounded_and_copied(self):
        event = {"type": "pointer", "x": 0.25, "y": 1, "display": 1}
        for _ in range(4100):
            self.device.inject(event)
        self.assertEqual(len(self.device.events), 4096)
        event["x"] = 0.9
        snapshot = self.device.events
        self.assertEqual(snapshot[-1]["x"], 0.25)
        snapshot[-1]["x"] = 0.8
        self.assertEqual(self.device.events[-1]["x"], 0.25)

    def test_release_continues_after_one_native_failure(self):
        device = native.NativeInput()
        device._backend = Mock()
        for key in ("ctrl", "a"):
            device.inject({"type": "key", "key": key, "down": True})
        device._backend.send.side_effect = [native.NativeInputError("blocked key"), None]
        with self.assertRaisesRegex(native.NativeInputError, "blocked key"):
            device.release_all()
        self.assertEqual(device._pressed_keys, {"a"})
        device._backend.send.side_effect = None
        device.release_all()
        self.assertFalse(device._pressed_keys)

    @patch("virtualkvm.native.platform.system", return_value="Linux")
    def test_linux_never_claims_native_support(self, _):
        device = native.NativeInput()
        self.assertFalse(device.permission_status()["input_supported"])
        with self.assertRaises(native.NativeInputError):
            device.inject({"type": "key", "key": "a", "down": True})
        self.assertIn("Synthetic", native.list_displays()[0].name)
        self.assertTrue(self.device.permission_status()["input_allowed"])


class WindowsInputTests(unittest.TestCase):
    def setUp(self):
        self.entries = []
        self.backend = native._WindowsBackend.__new__(native._WindowsBackend)

        def send(count, pointer, size):
            self.assertEqual(count, 1)
            self.assertEqual(size, C.sizeof(native._INPUT))
            self.entries.append(native._INPUT.from_buffer_copy(C.string_at(pointer, size)))
            return 1

        self.backend.user32 = SimpleNamespace(SendInput=send)
        self.backend._displays = [native.Display(0, "Primary", 0, 0, 1920, 1080),
                                  native.Display(1, "Left", -1920, 0, 1920, 1080)]
        self.backend._display_time = native.time.monotonic()

    def test_windows_input_structure_matches_abi(self):
        self.assertEqual(C.sizeof(native._INPUT), 40 if C.sizeof(C.c_void_p) == 8 else 28)
        self.assertEqual(C.sizeof(native._MOUSEINPUT), 32 if C.sizeof(C.c_void_p) == 8 else 24)

    def test_absolute_pointer_reaches_negative_origin_monitor(self):
        self.backend.send({"type": "pointer", "display": 1, "x": 0, "y": 0})
        self.assertEqual((self.entries[-1].mi.dx, self.entries[-1].mi.dy), (0, 0))
        self.backend.send({"type": "pointer", "display": 0, "x": 1, "y": 1})
        self.assertEqual((self.entries[-1].mi.dx, self.entries[-1].mi.dy), (65535, 65535))
        self.assertEqual(self.entries[-1].mi.dwFlags, 0xC001)

    def test_keyup_preserves_extended_flag(self):
        self.backend.send({"type": "key", "key": "rightctrl", "down": False})
        self.assertEqual(self.entries[-1].ki.wVk, 163)
        self.assertEqual(self.entries[-1].ki.dwFlags, 3)
        self.backend.send({"type": "key", "key": "a", "down": True})
        self.assertEqual(self.entries[-1].ki.wVk, ord("A"))

    def test_wheel_direction_and_both_axes(self):
        self.backend.send({"type": "scroll", "dx": 1, "dy": -2})
        self.assertEqual(C.c_int32(self.entries[0].mi.mouseData).value, -240)
        self.assertEqual(self.entries[0].mi.dwFlags, 0x0800)
        self.assertEqual(self.entries[1].mi.mouseData, 120)
        self.assertEqual(self.entries[1].mi.dwFlags, 0x1000)

    def test_rejected_send_is_reported(self):
        self.backend.user32.SendInput = lambda *_: 0
        with self.assertRaisesRegex(native.NativeInputError, "rejected SendInput"):
            self.backend.send({"type": "key", "key": "a", "down": True})


class PhysicalKeyTests(unittest.TestCase):
    def test_non_latin_text_does_not_change_physical_key_identity(self):
        # Russian А is on the physical US F key; Б is on comma. The receiver
        # chooses the resulting text using its current keyboard layout.
        self.assertEqual(native.native_key_name("Windows", 0x46), "f")
        self.assertEqual(native.native_key_name("Darwin", 3), "f")
        self.assertEqual(native.native_key_name("win32", 0xBC), ",")
        self.assertEqual(native.native_key_name("macos", 43), ",")

    def test_mac_keycode_zero_is_a_real_key(self):
        self.assertEqual(native.native_key_name("Darwin", 0), "a")

    def test_left_windows_modifiers_are_canonical(self):
        for code, expected in ((160, "shift"), (162, "ctrl"), (164, "alt")):
            self.assertEqual(native.native_key_name("Windows", code), expected)

    def test_unknown_codes_do_not_become_arbitrary_keys(self):
        for system, code in (("Linux", 3), ("Darwin", -1), ("Windows", True),
                             ("Windows", 9999), ("Darwin", 9999)):
            self.assertIsNone(native.native_key_name(system, code))


class FakeFunction:
    def __init__(self, function):
        self.function = function

    def __call__(self, *args):
        return self.function(*args)


class MonitorTests(unittest.TestCase):
    @patch("virtualkvm.native.platform.system", return_value="Windows")
    @patch("virtualkvm.native._windows_monitors", return_value=[(10, 0, 0, 1920, 1080)])
    def test_ddc_handles_are_destroyed_after_switch_failure(self, *_):
        destroyed = []
        def count(_handle, pointer):
            C.cast(pointer, C.POINTER(C.c_uint32))[0] = 2
            return 1

        def monitors(_handle, _count, array):
            array[0].handle = 11
            array[1].handle = 12
            return 1

        dxva2 = SimpleNamespace(
            GetNumberOfPhysicalMonitorsFromHMONITOR=FakeFunction(count),
            GetPhysicalMonitorsFromHMONITOR=FakeFunction(monitors),
            SetVCPFeature=FakeFunction(lambda *_: 0),
            DestroyPhysicalMonitors=FakeFunction(lambda count, array: destroyed.append(count) or 1))
        with patch.object(C, "WinDLL", return_value=dxva2, create=True):
            with self.assertRaisesRegex(native.NativeInputError, "DDC/CI input switching failed"):
                native.set_monitor_input(0, 15)
        self.assertEqual(destroyed, [2])

    def test_monitor_source_validation(self):
        for display, source in ((-1, 15), (True, 15), (0, 0), (0, 256), (0, True)):
            with self.subTest(display=display, source=source), self.assertRaises(ValueError):
                native.set_monitor_input(display, source)

    @patch("virtualkvm.native.platform.system", return_value="Darwin")
    def test_mac_display_coordinates_remain_logical_points(self, _):
        def bounds(identifier):
            return SimpleNamespace(origin=SimpleNamespace(x=0 if identifier == 9 else -1440, y=0),
                                   size=SimpleNamespace(width=1440, height=900))

        quartz = SimpleNamespace(CGGetActiveDisplayList=lambda *_: (0, [4, 9], 2),
                                 CGMainDisplayID=lambda: 9, CGDisplayBounds=bounds,
                                 CGDisplayCopyDisplayMode=lambda _: object(),
                                 CGDisplayModeGetPixelWidth=lambda _: 2880,
                                 CGDisplayModeGetPixelHeight=lambda _: 1800)
        with patch("virtualkvm.native._quartz", return_value=quartz):
            displays = native.list_displays()
        self.assertEqual(displays[0].name, "Display 9")
        self.assertEqual((displays[1].x, displays[1].width), (-1440, 1440))
        self.assertEqual((displays[1].pixel_width, displays[1].pixel_height), (2880, 1800))

    @patch("virtualkvm.native.platform.system", return_value="Darwin")
    def test_mac_portrait_retina_pixels_follow_desktop_orientation(self, _):
        bounds = SimpleNamespace(origin=SimpleNamespace(x=-900, y=120),
                                 size=SimpleNamespace(width=900, height=1440))
        quartz = SimpleNamespace(CGGetActiveDisplayList=lambda *_: (0, [9], 1),
                                 CGMainDisplayID=lambda: 9, CGDisplayBounds=lambda _: bounds,
                                 CGDisplayCopyDisplayMode=lambda _: object(),
                                 CGDisplayModeGetPixelHeight=lambda _: 1800)
        for raw_width, raw_height in ((2880, 1800), (1800, 2880)):
            with self.subTest(mode=(raw_width, raw_height)):
                quartz.CGDisplayModeGetPixelWidth = lambda _: raw_width
                quartz.CGDisplayModeGetPixelHeight = lambda _: raw_height
                with patch("virtualkvm.native._quartz", return_value=quartz):
                    display = native.list_displays()[0]
                self.assertEqual((display.x, display.y, display.width, display.height),
                                 (-900, 120, 900, 1440))
                self.assertEqual((display.pixel_width, display.pixel_height), (1800, 2880))


class MacInputTests(unittest.TestCase):
    def setUp(self):
        self.posted = []
        quartz = SimpleNamespace(kCGEventFlagMaskShift=1, kCGEventFlagMaskControl=2,
                                  kCGEventFlagMaskAlternate=4, kCGEventFlagMaskCommand=8,
                                  kCGEventFlagMaskAlphaShift=16, kCGKeyboardEventAutorepeat=1,
                                  kCGHIDEventTap=0)
        quartz.CGEventCreateKeyboardEvent = lambda _source, code, down: {"code": code, "down": down}
        quartz.CGEventSetFlags = lambda event, flags: event.update(flags=flags)
        quartz.CGEventSetIntegerValueField = lambda event, _field, value: event.update(repeat=value)
        quartz.CGEventPost = lambda _tap, event: self.posted.append(event.copy())
        self.backend = native._MacBackend.__new__(native._MacBackend)
        self.backend.q = quartz
        self.backend._keys = set()
        self.backend._buttons = set()
        self.backend._caps_lock = False
        self.backend._permission_checked = 0
        self.backend._last_click = None
        self.backend._click_count = 0

    @patch("virtualkvm.native._mac_trusted", return_value=True)
    def test_modifier_flags_and_repeat_accompany_key_events(self, _):
        for key, down in (("ctrl", True), ("a", True), ("a", True), ("a", False), ("ctrl", False)):
            self.backend.send({"type": "key", "key": key, "down": down})
        self.assertEqual(self.posted[1]["code"], 0)
        self.assertEqual(self.posted[1]["flags"], 2)
        self.assertEqual(self.posted[2]["repeat"], 1)
        self.assertEqual(self.posted[-1]["flags"], 0)

    @patch("virtualkvm.native._mac_trusted", return_value=False)
    def test_accessibility_denial_prevents_posting(self, _):
        with self.assertRaisesRegex(native.NativeInputError, "Accessibility"):
            self.backend.send({"type": "key", "key": "a", "down": True})
        self.assertEqual(self.posted, [])

    @patch("virtualkvm.native._mac_trusted", return_value=True)
    def test_unavailable_mac_key_does_not_get_an_arbitrary_mapping(self, _):
        with self.assertRaisesRegex(native.NativeInputError, "not supported by macOS"):
            self.backend.send({"type": "key", "key": "f24", "down": True})
        self.assertEqual(self.posted, [])

    @patch("virtualkvm.native._mac_trusted", return_value=True)
    def test_mac_double_click_count_is_preserved(self, _):
        q = self.backend.q
        names = ("kCGEventLeftMouseDown", "kCGEventLeftMouseUp", "kCGMouseButtonLeft",
                 "kCGEventRightMouseDown", "kCGEventRightMouseUp", "kCGMouseButtonRight",
                 "kCGEventOtherMouseDown", "kCGEventOtherMouseUp", "kCGMouseButtonCenter",
                 "kCGMouseEventClickState")
        for i, name in enumerate(names):
            setattr(q, name, i)
        q.CGEventCreate = lambda _: object()
        q.CGEventGetLocation = lambda _: SimpleNamespace(x=100, y=200)
        q.CGEventCreateMouseEvent = lambda _source, kind, point, button: {"kind": kind, "button": button}
        q.CGEventSetIntegerValueField = lambda event, _field, value: event.update(clicks=value)
        for down in (True, False, True, False):
            self.backend.send({"type": "button", "button": "left", "down": down})
        self.assertEqual([event["clicks"] for event in self.posted], [1, 1, 2, 2])


if __name__ == "__main__":
    unittest.main()
