"""Render the project's vector mark into native application icons."""
from __future__ import annotations

import os
from pathlib import Path
import struct
import subprocess
import sys


def generate_icons(svg: Path, output: Path, *, macos: bool = False) -> dict[str, Path]:
    if sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QByteArray, QBuffer, QIODevice, Qt
    from PySide6.QtGui import QImage, QPainter
    from PySide6.QtSvg import QSvgRenderer
    svg, output = Path(svg), Path(output)
    if not svg.is_file():
        raise ValueError("The application SVG icon is missing")
    output.mkdir(parents=True, exist_ok=True)
    renderer = QSvgRenderer(str(svg))
    if not renderer.isValid():
        raise ValueError("The application SVG icon is invalid")

    def render(size):
        image = QImage(size, size, QImage.Format.Format_ARGB32)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        renderer.render(painter)
        painter.end()
        data = QByteArray()
        buffer = QBuffer(data)
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        if not image.save(buffer, "PNG"):
            raise RuntimeError("Cannot encode application icon")
        buffer.close()
        return bytes(data)

    sizes = (16, 24, 32, 48, 64, 128, 256)
    images = [render(size) for size in sizes]
    offset = 6 + len(sizes) * 16
    directory = []
    for size, image in zip(sizes, images):
        directory.append(struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(image), offset))
        offset += len(image)
    ico = output / "VirtualKVM.ico"
    ico.write_bytes(struct.pack("<HHH", 0, 1, len(images)) + b"".join(directory) + b"".join(images))
    png = output / "VirtualKVM.png"
    png.write_bytes(images[-1])
    result = {"ico": ico, "png": png}
    if macos:
        iconset = output / "VirtualKVM.iconset"
        iconset.mkdir(exist_ok=True)
        for size in (16, 32, 128, 256, 512):
            (iconset / f"icon_{size}x{size}.png").write_bytes(render(size))
            (iconset / f"icon_{size}x{size}@2x.png").write_bytes(render(size * 2))
        icns = output / "VirtualKVM.icns"
        subprocess.run(["iconutil", "--convert", "icns", "--output", str(icns), str(iconset)], check=True)
        result["icns"] = icns
    return result
