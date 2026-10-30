"""Display inventory and receiving-window geometry, independent of Qt/OS APIs.

Positions here arrange application windows. They never change the source OS's
desktop coordinates, input mapping, display modes, or display driver settings.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import math

MAX_STREAMS = 8
MAX_DISPLAYS = 64
MAX_COORDINATE = 1048576


def _integer(value, name, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{name}: expected an integer from {low} to {high}")
    return value


@dataclass(frozen=True)
class DisplayInfo:
    index: int
    name: str
    x: int
    y: int
    width: int
    height: int
    pixel_width: int
    pixel_height: int


def display_inventory(values) -> list[DisplayInfo]:
    if not isinstance(values, list) or not 1 <= len(values) <= MAX_DISPLAYS:
        raise ValueError("Хост должен предоставить от 1 до 64 экранов.")
    result = []
    for value in values:
        if not isinstance(value, dict):
            raise ValueError("Некорректное описание экрана хоста.")
        index = _integer(value.get("index"), "display", 0, MAX_DISPLAYS - 1)
        width = _integer(value.get("width"), "width", 1, 16384)
        height = _integer(value.get("height"), "height", 1, 16384)
        name = value.get("name", f"Экран {index + 1}")
        if not isinstance(name, str) or len(name) > 160 or any(ord(c) < 32 for c in name):
            raise ValueError("Некорректное имя экрана хоста.")
        result.append(DisplayInfo(index, name,
            _integer(value.get("x", 0), "x", -MAX_COORDINATE, MAX_COORDINATE),
            _integer(value.get("y", 0), "y", -MAX_COORDINATE, MAX_COORDINATE),
            width, height,
            _integer(width if value.get("pixel_width") is None else value["pixel_width"], "pixel width", 1, 16384),
            _integer(height if value.get("pixel_height") is None else value["pixel_height"], "pixel height", 1, 16384)))
    if len({display.index for display in result}) != len(result):
        raise ValueError("Хост вернул повторяющиеся номера экранов.")
    return result


@dataclass(frozen=True)
class Placement:
    source: int
    x: int = 0
    y: int = 0
    width: int = 1920
    height: int = 1080
    target: str = ""

    def __post_init__(self):
        _integer(self.source, "display", 0, MAX_DISPLAYS - 1)
        _integer(self.x, "x", -MAX_COORDINATE, MAX_COORDINATE)
        _integer(self.y, "y", -MAX_COORDINATE, MAX_COORDINATE)
        _integer(self.width, "width", 1, 16384)
        _integer(self.height, "height", 1, 16384)
        if not isinstance(self.target, str) or len(self.target) > 256:
            raise ValueError("Некорректный локальный монитор.")


def validate_layout(entries: list[Placement], fullscreen=False, presentation="windows"):
    if not 1 <= len(entries) <= MAX_STREAMS:
        raise ValueError("Выберите от 1 до 8 экранов.")
    if len({entry.source for entry in entries}) != len(entries):
        raise ValueError("Каждый экран хоста можно выбрать только один раз.")
    if presentation not in ("windows", "monitors"):
        raise ValueError("Неизвестный способ размещения экранов.")
    if presentation == "monitors":
        targets = [entry.target for entry in entries]
        if not all(targets) or len(set(targets)) != len(targets):
            raise ValueError("Назначьте каждому экрану отдельный локальный монитор или выберите размещение окон по схеме.")


def arrange(entries: list[Placement], mode: str, inventory=()) -> list[Placement]:
    known = {display.index: display for display in inventory}
    entries = [replace(entry, width=known[entry.source].width, height=known[entry.source].height)
               if entry.source in known else entry for entry in entries]
    if mode == "host" and all(entry.source in known for entry in entries):
        return [replace(entry, x=known[entry.source].x, y=known[entry.source].y) for entry in entries]
    result, x, y = [], 0, 0
    columns = max(1, math.ceil(math.sqrt(len(entries))))
    cell_width = max((entry.width for entry in entries), default=1920) + 80
    cell_height = max((entry.height for entry in entries), default=1080) + 80
    for index, entry in enumerate(entries):
        if mode == "grid":
            x, y = index % columns * cell_width, index // columns * cell_height
        result.append(replace(entry, x=x, y=y))
        if mode == "vertical":
            y += entry.height + 80
        else:
            x += entry.width + 80
    return result


def fit_windows(entries: list[Placement], area: tuple[int, int, int, int]) -> dict[int, tuple[int, int, int, int]]:
    """Fit the configured relative desktop into a local available screen area."""
    if not entries:
        return {}
    ax, ay, aw, ah = area
    if aw <= 0 or ah <= 0:
        raise ValueError("The local screen has no available area")
    left, top = min(e.x for e in entries), min(e.y for e in entries)
    width = max(e.x + e.width for e in entries) - left
    height = max(e.y + e.height for e in entries) - top
    usable_width, usable_height = max(1, aw - 40), max(1, ah - 80)
    scale = min(usable_width / width, usable_height / height)
    origin_x = ax + (aw - width * scale) / 2
    origin_y = ay + 40 + (usable_height - height * scale) / 2
    result = {}
    for entry in entries:
        w = min(aw, max(min(160, aw), round(entry.width * scale) - 8))
        h = min(ah, max(min(120, ah), round(entry.height * scale) - 8))
        x = max(ax, min(ax + aw - w, round(origin_x + (entry.x - left) * scale)))
        y = max(ay, min(ay + ah - h, round(origin_y + (entry.y - top) * scale)))
        result[entry.source] = (x, y, w, h)
    return result


def decode_layout(value) -> tuple[list[Placement], bool, str]:
    if not isinstance(value, dict) or type(value.get("version")) is not int or value["version"] != 1:
        raise ValueError("Неизвестный формат схемы экранов.")
    rows = value.get("entries")
    fullscreen = value.get("fullscreen", False)
    presentation = value.get("presentation", "windows")
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_STREAMS or type(fullscreen) is not bool:
        raise ValueError("Некорректная схема экранов.")
    entries = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Некорректная строка схемы экранов.")
        if "source" not in row:
            raise ValueError("Не указан экран хоста.")
        entries.append(Placement(**{key: row[key] for key in
            ("source", "x", "y", "width", "height", "target") if key in row}))
    validate_layout(entries, fullscreen, presentation)
    return entries, fullscreen, presentation
