"""Native display discovery and explicitly scoped remote input.

No global keyboard hook is installed. Input is injected only when the control
session calls ``inject``. Call ``release_all`` on every session teardown.
"""
from __future__ import annotations

import ctypes as C
from collections import deque
from dataclasses import dataclass
import math
import platform
import threading
import time


class NativeInputError(RuntimeError):
    """An input API or required operating-system permission is unavailable."""


@dataclass(frozen=True, slots=True)
class Display:
    index: int
    name: str
    x: int
    y: int
    width: int
    height: int
    pixel_width: int | None = None
    pixel_height: int | None = None


_ALIASES = {"control": "ctrl", "return": "enter", "esc": "escape",
            "command": "meta", "cmd": "meta", "super": "meta",
            "option": "alt", " ": "space", "backtab": "tab",
            "arrowleft": "left", "arrowright": "right",
            "arrowup": "up", "arrowdown": "down"}
_VK = {"backspace": 8, "tab": 9, "enter": 13, "shift": 16, "ctrl": 17,
       "alt": 18, "pause": 19, "capslock": 20, "escape": 27, "space": 32,
       "pageup": 33, "pagedown": 34, "end": 35, "home": 36, "left": 37,
       "up": 38, "right": 39, "down": 40, "printscreen": 44, "insert": 45,
       "delete": 46, "meta": 91, "rightmeta": 92, "menu": 93, "numlock": 144,
       "scrolllock": 145, "rightshift": 161, "rightctrl": 163, "rightalt": 165,
       ";": 186, "=": 187, ",": 188, "-": 189, ".": 190, "/": 191,
       "`": 192, "[": 219, "\\": 220, "]": 221, "'": 222,
       "decimal": 110, "multiply": 106, "add": 107, "subtract": 109, "divide": 111}
_VK.update({chr(n).lower(): n for n in range(65, 91)})
_VK.update({str(n): 48 + n for n in range(10)})
_VK.update({f"f{n}": 111 + n for n in range(1, 25)})
_VK.update({f"numpad{n}": 96 + n for n in range(10)})
_MODIFIERS = {"shift", "ctrl", "alt", "meta", "rightshift", "rightctrl",
              "rightalt", "rightmeta"}
_MAC_KEYS = dict(zip(
    ["a", "s", "d", "f", "h", "g", "z", "x", "c", "v", "b", "q", "w",
     "e", "r", "y", "t", "1", "2", "3", "4", "6", "5", "=", "9", "7",
     "-", "8", "0", "]", "o", "u", "[", "i", "p", "enter", "l", "j",
     "'", "k", ";", "\\", ",", "/", "n", "m", ".", "tab", "space", "`",
     "backspace", "escape", "meta", "shift", "capslock", "alt", "ctrl",
     "rightmeta", "rightshift", "rightalt", "rightctrl"],
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 11, 12, 13, 14, 15, 16, 17, 18, 19,
     20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35, 36,
     37, 38, 39, 40, 41, 42, 43, 44, 45, 46, 47, 48, 49, 50, 51, 53, 55,
     56, 57, 58, 59, 54, 60, 61, 62]))
_MAC_KEYS.update({"f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96,
                  "f6": 97, "f7": 98, "f8": 100, "f9": 101, "f10": 109,
                  "f11": 103, "f12": 111, "f13": 105, "f14": 107,
                  "f15": 113, "f16": 106, "f17": 64, "f18": 79,
                  "f19": 80, "f20": 90, "home": 115, "pageup": 116,
                  "delete": 117, "end": 119, "pagedown": 121,
                  "left": 123, "right": 124, "down": 125, "up": 126,
                  "numpad0": 82, "numpad1": 83, "numpad2": 84,
                  "numpad3": 85, "numpad4": 86, "numpad5": 87,
                  "numpad6": 88, "numpad7": 89, "numpad8": 91,
                  "numpad9": 92, "decimal": 65, "multiply": 67,
                  "add": 69, "divide": 75, "subtract": 78})


def native_key_name(platform_name: str, virtual_key: int) -> str | None:
    """Translate a source OS physical key code, independently of text layout.

    Qt reports non-ASCII key values for layouts such as Russian. Its native
    Windows virtual key / macOS CGEvent key code still identifies the physical
    key, which the receiving computer interprets using its own active layout.
    """
    if type(virtual_key) is not int or virtual_key < 0:
        return None
    system = platform_name.lower()
    if system in ("windows", "win32"):
        virtual_key = {160: 16, 162: 17, 164: 18}.get(virtual_key, virtual_key)
        table = _VK
    elif system in ("darwin", "macos"):
        table = _MAC_KEYS
    else:
        return None
    return next((key for key, code in table.items() if code == virtual_key), None)


def _number(value, name: str, lower: float, upper: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number")
    if not math.isfinite(value) or not lower <= value <= upper:
        raise ValueError(f"{name} must be finite and between {lower} and {upper}")
    return float(value)


def _validate(event: dict) -> dict:
    if not isinstance(event, dict):
        raise ValueError("Input event must be an object")
    kind = event.get("type")
    fields = {"key": {"type", "key", "down"},
              "pointer": {"type", "x", "y", "display"},
              "button": {"type", "button", "down"},
              "scroll": {"type", "dx", "dy"}}
    if not isinstance(kind, str) or kind not in fields:
        raise ValueError("Unknown input event type")
    if set(event) - fields[kind]:
        raise ValueError("Unknown input event fields")
    result = dict(event)
    if kind in ("key", "button"):
        if type(event.get("down")) is not bool:
            raise ValueError("down must be a boolean")
    if kind == "key":
        key = event.get("key")
        if not isinstance(key, str):
            raise ValueError("key must be a supported key name")
        key = _ALIASES.get(key.lower(), key.lower())
        if key not in _VK:
            raise ValueError(f"Unsupported key name: {key!r}")
        result["key"] = key
    elif kind == "button":
        if event.get("button") not in ("left", "right", "middle"):
            raise ValueError("Unsupported mouse button")
    elif kind == "pointer":
        result["x"] = _number(event.get("x"), "x", 0, 1)
        result["y"] = _number(event.get("y"), "y", 0, 1)
        display = event.get("display", 0)
        if type(display) is not int or not 0 <= display < 64:
            raise ValueError("display must be an integer between 0 and 63")
        result["display"] = display
    elif kind == "scroll":
        result["dx"] = _number(event.get("dx", 0), "dx", -1000, 1000)
        result["dy"] = _number(event.get("dy", 0), "dy", -1000, 1000)
    return result


# Use explicit Windows scalar widths: ctypes.wintypes follows the host ABI
# when imported on Linux, where unsigned long is eight bytes.
class _MOUSEINPUT(C.Structure):
    _fields_ = [("dx", C.c_int32), ("dy", C.c_int32), ("mouseData", C.c_uint32),
                ("dwFlags", C.c_uint32), ("time", C.c_uint32), ("dwExtraInfo", C.c_size_t)]


class _KEYBDINPUT(C.Structure):
    _fields_ = [("wVk", C.c_uint16), ("wScan", C.c_uint16),
                ("dwFlags", C.c_uint32), ("time", C.c_uint32), ("dwExtraInfo", C.c_size_t)]


class _HARDWAREINPUT(C.Structure):
    _fields_ = [("uMsg", C.c_uint32), ("wParamL", C.c_uint16), ("wParamH", C.c_uint16)]


class _INPUTUNION(C.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT), ("hi", _HARDWAREINPUT)]


class _INPUT(C.Structure):
    _anonymous_ = ("data",)
    _fields_ = [("type", C.c_uint32), ("data", _INPUTUNION)]


class _RECT(C.Structure):
    _fields_ = [(n, C.c_int32) for n in ("left", "top", "right", "bottom")]


def _windows_monitors():
    user32 = C.WinDLL("user32", use_last_error=True)
    # Coordinates from the same physical-pixel space as FFmpeg gdigrab.
    try:
        user32.SetProcessDpiAwarenessContext.argtypes = [C.c_void_p]
        user32.SetProcessDpiAwarenessContext(C.c_void_p(-4))
    except AttributeError:
        user32.SetProcessDPIAware()
    callback_type = C.WINFUNCTYPE(C.c_int32, C.c_void_p, C.c_void_p,
                                 C.POINTER(_RECT), C.c_ssize_t)
    monitors = []

    def visit(handle, _dc, bounds, _data):
        r = bounds.contents
        monitors.append((handle, r.left, r.top, r.right - r.left, r.bottom - r.top))
        return 1

    callback = callback_type(visit)
    user32.EnumDisplayMonitors.argtypes = [C.c_void_p, C.c_void_p, callback_type, C.c_ssize_t]
    user32.EnumDisplayMonitors.restype = C.c_int32
    if not user32.EnumDisplayMonitors(None, None, callback, 0):
        raise NativeInputError("Windows monitor enumeration failed")
    # Keep the primary desktop at index zero, then order by position.
    monitors.sort(key=lambda m: (not (m[1] == 0 and m[2] == 0), m[1], m[2]))
    return monitors


def _quartz():
    try:
        import Quartz
    except ImportError as error:
        raise NativeInputError("macOS support requires pyobjc-framework-Quartz") from error
    return Quartz


def _mac_trusted() -> bool:
    framework = C.CDLL("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
    framework.AXIsProcessTrusted.restype = C.c_bool
    framework.AXIsProcessTrusted.argtypes = []
    return bool(framework.AXIsProcessTrusted())


def list_displays() -> list[Display]:
    """Enumerate native screens; Linux returns a clearly identified test screen."""
    system = platform.system()
    if system == "Windows":
        return [Display(i, f"Display {i + 1}", x, y, w, h)
                for i, (_, x, y, w, h) in enumerate(_windows_monitors())]
    if system == "Darwin":
        q = _quartz()
        error, identifiers, count = q.CGGetActiveDisplayList(64, None, None)
        if error:
            raise NativeInputError(f"macOS display enumeration failed ({error})")
        ids = sorted(identifiers[:count], key=lambda d: d != q.CGMainDisplayID())
        result = []
        for index, identifier in enumerate(ids):
            bounds = q.CGDisplayBounds(identifier)
            pixel_width, pixel_height = int(bounds.size.width), int(bounds.size.height)
            if hasattr(q, "CGDisplayCopyDisplayMode"):
                mode = q.CGDisplayCopyDisplayMode(identifier)
                if mode:
                    pixel_width = int(q.CGDisplayModeGetPixelWidth(mode))
                    pixel_height = int(q.CGDisplayModeGetPixelHeight(mode))
            # A rotated display mode may report the panel's unrotated pixels.
            # Match desktop orientation without swapping already-oriented modes.
            logical_width, logical_height = int(bounds.size.width), int(bounds.size.height)
            if (logical_width != logical_height and pixel_width != pixel_height
                    and (logical_width > logical_height) != (pixel_width > pixel_height)):
                pixel_width, pixel_height = pixel_height, pixel_width
            # CGEvent coordinates use logical desktop points on Retina displays.
            result.append(Display(index, f"Display {identifier}", int(bounds.origin.x),
                                  int(bounds.origin.y), int(bounds.size.width), int(bounds.size.height),
                                  pixel_width, pixel_height))
        return result
    return [Display(0, "Synthetic display (no native capture device)", 0, 0, 1920, 1080)]


def _display_at(index: int, displays: list[Display]) -> Display:
    for display in displays:
        if display.index == index:
            return display
    raise ValueError(f"Display {index} is unavailable")


def _windows_absolute(display: Display, x: float, y: float,
                      displays: list[Display]) -> tuple[int, int]:
    left, top = min(d.x for d in displays), min(d.y for d in displays)
    right = max(d.x + d.width for d in displays)
    bottom = max(d.y + d.height for d in displays)
    px = display.x + round(x * (display.width - 1))
    py = display.y + round(y * (display.height - 1))
    return (round((px - left) * 65535 / max(1, right - left - 1)),
            round((py - top) * 65535 / max(1, bottom - top - 1)))


class _WindowsBackend:
    def __init__(self):
        self.user32 = C.WinDLL("user32", use_last_error=True)
        self.user32.SendInput.argtypes = [C.c_uint32, C.POINTER(_INPUT), C.c_int32]
        self.user32.SendInput.restype = C.c_uint32
        self._displays = []
        self._display_time = 0.0

    def _send(self, entry):
        if self.user32.SendInput(1, C.byref(entry), C.sizeof(_INPUT)) != 1:
            raise NativeInputError("Windows rejected SendInput; elevated and secure desktops "
                                   "cannot be controlled from an ordinary process")

    def send(self, event):
        kind = event["type"]
        if kind == "key":
            flags = 0 if event["down"] else 2
            if event["key"] in {"insert", "delete", "home", "end", "pageup", "pagedown",
                                "left", "right", "up", "down", "rightctrl", "rightalt",
                                "meta", "rightmeta", "numlock", "printscreen", "divide"}:
                flags |= 1
            self._send(_INPUT(type=1, ki=_KEYBDINPUT(wVk=_VK[event["key"]], dwFlags=flags)))
        elif kind == "pointer":
            if not self._displays or time.monotonic() - self._display_time > 1:
                self._displays = list_displays()
                self._display_time = time.monotonic()
            display = _display_at(event["display"], self._displays)
            x, y = _windows_absolute(display, event["x"], event["y"], self._displays)
            self._send(_INPUT(type=0, mi=_MOUSEINPUT(dx=x, dy=y, dwFlags=0x8000 | 0x4000 | 1)))
        elif kind == "button":
            down, up = {"left": (2, 4), "right": (8, 16), "middle": (32, 64)}[event["button"]]
            self._send(_INPUT(type=0, mi=_MOUSEINPUT(dwFlags=down if event["down"] else up)))
        elif kind == "scroll":
            for axis, flag in (("dy", 0x0800), ("dx", 0x1000)):
                amount = round(event[axis] * 120)
                if amount:
                    self._send(_INPUT(type=0, mi=_MOUSEINPUT(mouseData=amount & 0xFFFFFFFF, dwFlags=flag)))


class _MacBackend:
    def __init__(self):
        self.q = _quartz()
        self._keys = set()
        self._buttons = set()
        self._caps_lock = False
        self._permission_checked = 0.0
        self._last_click = None
        self._click_count = 0

    def send(self, event):
        q = self.q
        now = time.monotonic()
        if now - self._permission_checked > 1:
            if not _mac_trusted():
                raise NativeInputError("Enable Accessibility for VirtualKVM in macOS Privacy & Security")
            self._permission_checked = now
        kind = event["type"]
        if kind == "key":
            key, down = event["key"], event["down"]
            if key not in _MAC_KEYS:
                raise NativeInputError(f"Key {key!r} is not supported by macOS")
            native_event = q.CGEventCreateKeyboardEvent(None, _MAC_KEYS[key], down)
            repeated = down and key in self._keys
            (self._keys.add if down else self._keys.discard)(key)
            if key == "capslock" and down and not repeated:
                self._caps_lock = not self._caps_lock
            flags = 0
            for names, flag in (({"shift", "rightshift"}, q.kCGEventFlagMaskShift),
                                ({"ctrl", "rightctrl"}, q.kCGEventFlagMaskControl),
                                ({"alt", "rightalt"}, q.kCGEventFlagMaskAlternate),
                                ({"meta", "rightmeta"}, q.kCGEventFlagMaskCommand)):
                if names & self._keys:
                    flags |= flag
            if self._caps_lock:
                flags |= q.kCGEventFlagMaskAlphaShift
            q.CGEventSetFlags(native_event, flags)
            q.CGEventSetIntegerValueField(native_event, q.kCGKeyboardEventAutorepeat, int(repeated))
        elif kind == "pointer":
            display = _display_at(event["display"], list_displays())
            point = (display.x + event["x"] * (display.width - 1),
                     display.y + event["y"] * (display.height - 1))
            action, button = q.kCGEventMouseMoved, q.kCGMouseButtonLeft
            for name, drag, native_button in (("left", q.kCGEventLeftMouseDragged, q.kCGMouseButtonLeft),
                    ("right", q.kCGEventRightMouseDragged, q.kCGMouseButtonRight),
                    ("middle", q.kCGEventOtherMouseDragged, q.kCGMouseButtonCenter)):
                if name in self._buttons:
                    action, button = drag, native_button
                    break
            native_event = q.CGEventCreateMouseEvent(None, action, point, button)
        elif kind == "button":
            button = event["button"]
            down_type, up_type, number = {
                "left": (q.kCGEventLeftMouseDown, q.kCGEventLeftMouseUp, q.kCGMouseButtonLeft),
                "right": (q.kCGEventRightMouseDown, q.kCGEventRightMouseUp, q.kCGMouseButtonRight),
                "middle": (q.kCGEventOtherMouseDown, q.kCGEventOtherMouseUp, q.kCGMouseButtonCenter),
            }[button]
            point = q.CGEventGetLocation(q.CGEventCreate(None))
            native_event = q.CGEventCreateMouseEvent(None, down_type if event["down"] else up_type, point, number)
            # CGEventPost does not synthesize double-click metadata for us.
            # Preserve native double/triple-click behavior for remote clicks.
            if event["down"]:
                previous = self._last_click
                close = (previous is not None and previous[0] == button
                         and now - previous[1] <= 0.5
                         and abs(point.x - previous[2]) <= 4
                         and abs(point.y - previous[3]) <= 4)
                self._click_count = min(3, self._click_count + 1) if close else 1
                self._last_click = (button, now, point.x, point.y)
            q.CGEventSetIntegerValueField(native_event, q.kCGMouseEventClickState, self._click_count or 1)
            (self._buttons.add if event["down"] else self._buttons.discard)(button)
        else:
            native_event = q.CGEventCreateScrollWheelEvent(None, q.kCGScrollEventUnitPixel,
                                                           2, round(event["dy"] * 40), round(event["dx"] * 40))
        if native_event is None:
            raise NativeInputError("macOS could not create an input event")
        q.CGEventPost(q.kCGHIDEventTap, native_event)


class NativeInput:
    """Validate/inject input, tracking held keys and buttons for safe teardown.

    ``dry_run`` performs all protocol validation and retains up to 4096 events,
    but intentionally permits simulated display indexes without physical screens.
    """
    def __init__(self, dry_run: bool = False):
        self.dry_run = bool(dry_run)
        self._events = deque(maxlen=4096)
        self._pressed_keys = set()
        self._pressed_buttons = set()
        self._backend = None
        self._lock = threading.RLock()

    @property
    def events(self) -> tuple[dict, ...]:
        with self._lock:
            return tuple(dict(event) for event in self._events)

    def inject(self, event: dict) -> None:
        event = _validate(event)
        with self._lock:
            if not self.dry_run:
                if self._backend is None:
                    system = platform.system()
                    if system == "Windows":
                        self._backend = _WindowsBackend()
                    elif system == "Darwin":
                        self._backend = _MacBackend()
                    else:
                        raise NativeInputError("Native input is supported only on Windows and macOS; use dry_run for tests")
                self._backend.send(event)
            if event["type"] in ("key", "button"):
                values = self._pressed_keys if event["type"] == "key" else self._pressed_buttons
                value = event["key"] if event["type"] == "key" else event["button"]
                (values.add if event["down"] else values.discard)(value)
            self._events.append(event)

    def release_all(self) -> None:
        with self._lock:
            failures = []
            # Release ordinary keys before modifiers to avoid unintended chords.
            events = [{"type": "button", "button": b, "down": False}
                      for b in sorted(self._pressed_buttons)]
            events += [{"type": "key", "key": k, "down": False}
                       for k in sorted(self._pressed_keys, key=lambda key: (key in _MODIFIERS, key))]
            for event in events:
                try:
                    self.inject(event)
                except Exception as error:
                    failures.append(str(error))
            if failures:
                raise NativeInputError("Could not release all held input: " + "; ".join(failures))

    def permission_status(self) -> dict:
        system = platform.system()
        result = {"platform": system, "dry_run": self.dry_run,
                  "input_supported": system in ("Windows", "Darwin"),
                  "input_allowed": False, "screen_recording_allowed": None}
        if self.dry_run:
            result.update(input_allowed=True, detail="Synthetic input; no events reach the operating system")
        elif system == "Darwin":
            try:
                q = _quartz()
                result["input_allowed"] = _mac_trusted()
                if hasattr(q, "CGPreflightScreenCaptureAccess"):
                    result["screen_recording_allowed"] = bool(q.CGPreflightScreenCaptureAccess())
                result["detail"] = "Enable Accessibility and Screen Recording in macOS Privacy & Security"
            except (NativeInputError, OSError) as error:
                result["detail"] = str(error)
        elif system == "Windows":
            result.update(input_allowed=True, screen_recording_allowed=True,
                          detail="Active user desktop only; elevated applications and secure desktops are restricted by Windows")
        else:
            result["detail"] = "Native capture/input requires Windows or macOS; synthetic mode is available"
        return result


def set_monitor_input(display: int, source: int) -> None:
    """Switch a Windows monitor's DDC/CI VCP 0x60 input (device-specific value)."""
    if type(display) is not int or display < 0:
        raise ValueError("display must be a nonnegative integer")
    if type(source) is not int or not 1 <= source <= 255:
        raise ValueError("source must be an integer between 1 and 255")
    if platform.system() != "Windows":
        raise NativeInputError("Automatic DDC/CI switching is supported only on Windows; switch the monitor manually")
    monitors = _windows_monitors()
    if display >= len(monitors):
        raise ValueError(f"Display {display} is unavailable")
    dxva2 = C.WinDLL("dxva2", use_last_error=True)

    class PhysicalMonitor(C.Structure):
        _fields_ = [("handle", C.c_void_p), ("description", C.c_wchar * 128)]

    dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR.argtypes = [C.c_void_p, C.POINTER(C.c_uint32)]
    dxva2.GetPhysicalMonitorsFromHMONITOR.argtypes = [C.c_void_p, C.c_uint32, C.POINTER(PhysicalMonitor)]
    dxva2.SetVCPFeature.argtypes = [C.c_void_p, C.c_ubyte, C.c_uint32]
    dxva2.DestroyPhysicalMonitors.argtypes = [C.c_uint32, C.POINTER(PhysicalMonitor)]
    for name in ("GetNumberOfPhysicalMonitorsFromHMONITOR", "GetPhysicalMonitorsFromHMONITOR",
                 "SetVCPFeature", "DestroyPhysicalMonitors"):
        getattr(dxva2, name).restype = C.c_int32
    count = C.c_uint32()
    handle = monitors[display][0]
    if not dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR(handle, C.byref(count)) or not count.value:
        raise NativeInputError("Monitor does not expose a DDC/CI physical device")
    physical = (PhysicalMonitor * count.value)()
    if not dxva2.GetPhysicalMonitorsFromHMONITOR(handle, count.value, physical):
        raise NativeInputError("Cannot open DDC/CI physical monitor handles")
    try:
        for monitor in physical:
            if not dxva2.SetVCPFeature(monitor.handle, 0x60, source):
                raise NativeInputError("DDC/CI input switching failed; enable DDC/CI in the monitor settings and verify its input code")
    finally:
        dxva2.DestroyPhysicalMonitors(count.value, physical)
