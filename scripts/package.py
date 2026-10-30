"""Build VirtualKVM with PyInstaller, optionally including native media tools."""

from __future__ import annotations

import argparse
import ast
import importlib.util
from importlib import metadata
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib

if __package__:
    from .icons import generate_icons
else:
    from icons import generate_icons


def prepare_notices(root: Path, packaging_dir: Path) -> Path:
    """Keep licenses next to the application without adding third-party code."""
    notices = packaging_dir / "licenses"
    notices.mkdir(parents=True, exist_ok=True)
    shutil.copy2(root / "LICENSE", notices / "VirtualKVM-MIT.txt")
    shutil.copy2(root / "docs" / "THIRD_PARTY_NOTICES.md", notices / "THIRD_PARTY_NOTICES.md")
    # Some PySide6 wheels declare LGPL terms but omit full license texts from
    # their distribution metadata. Keep those terms in our packaged notices.
    for source in (root / "docs" / "licenses").glob("*.txt"):
        shutil.copy2(source, notices / source.name)
    distributions = ("PySide6-Essentials", "PySide6-Addons", "shiboken6", "cryptography",
                     "cffi", "pycparser", "pyobjc-core", "pyobjc-framework-Cocoa",
                     "pyobjc-framework-Quartz", "pyinstaller")
    for name in distributions:
        try:
            distribution = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            continue
        for entry in distribution.files or ():
            if not any("license" in part.lower() or "copying" in part.lower()
                       for part in entry.parts):
                continue
            # Distribution metadata may include ../bin entries; never use them
            # as destinations, even if an unusual filename contains 'license'.
            if entry.is_absolute() or ".." in entry.parts:
                continue
            # PyObjC test/debug binaries named copying*.so are not licenses.
            if any(part.lower().endswith(".dsym") for part in entry.parts) or entry.suffix.lower() in {
                ".so", ".dylib", ".pyd", ".dll", ".a", ".o", ".py", ".pyc", ".c", ".h"
            }:
                continue
            source = Path(distribution.locate_file(entry))
            if not source.is_file():
                continue
            destination = notices / name / Path(*entry.parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
    return notices


def configure_macos_privacy(spec: Path, version: str = "0.1.0") -> None:
    """Set purpose strings before BUNDLE creates/seals the .app, never after."""
    tree = ast.parse(spec.read_text(encoding="utf-8"))
    bundles = [node for node in ast.walk(tree)
               if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
               and node.func.id == "BUNDLE"]
    if len(bundles) != 1:
        raise RuntimeError("Expected one generated macOS BUNDLE in the PyInstaller spec")
    purposes = {
        "CFBundleShortVersionString": version,
        "CFBundleVersion": version,
        "CFBundleName": "VirtualKVM",
        "CFBundleDisplayName": "VirtualKVM",
        "NSHighResolutionCapable": True,
        "NSMicrophoneUsageDescription": "VirtualKVM streams your selected audio input to your paired computer over an encrypted LAN connection.",
        "NSScreenCaptureUsageDescription": "VirtualKVM streams your selected displays to your paired computer over an encrypted LAN connection.",
        "NSInputMonitoringUsageDescription": "VirtualKVM handles keyboard and mouse input when you explicitly enable remote control.",
        "NSAccessibilityUsageDescription": "VirtualKVM sends keyboard and mouse input from your paired computer when remote control is enabled.",
    }
    bundle = bundles[0]
    bundle.keywords = [item for item in bundle.keywords if item.arg != "info_plist"]
    bundle.keywords.append(ast.keyword(arg="info_plist", value=ast.parse(repr(purposes), mode="eval").body))
    ast.fix_missing_locations(tree)
    spec.write_text(ast.unparse(tree) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--console", action="store_true", help="Keep a console for diagnostics")
    parser.add_argument("--bundle-ffmpeg", action="store_true", help="Include FFmpeg/ffprobe and native dependencies; required for installers")
    parser.add_argument("--ffmpeg", type=Path, help="Explicit FFmpeg binary to include")
    parser.add_argument("--ffprobe", type=Path, help="Explicit ffprobe binary to include")
    args = parser.parse_args(argv)
    if importlib.util.find_spec("PyInstaller") is None:
        parser.exit(2, 'PyInstaller is missing. Install with: python -m pip install -e ".[build]"\n')

    root = Path(__file__).resolve().parents[1]
    if not (root / "virtualkvm" / "__main__.py").is_file():
        parser.exit(2, "Run this helper from a complete VirtualKVM source checkout.\n")
    packaging_dir = root / "build" / "packaging"
    packaging_dir.mkdir(parents=True, exist_ok=True)
    notices = prepare_notices(root, packaging_dir)
    version = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        parser.error("A three-part numeric application version is required")
    icons = generate_icons(root / "docs/assets/logo.svg", packaging_dir / "icons", macos=sys.platform == "darwin")
    # An absolute-import launcher also works when __main__.py uses package imports.
    with tempfile.TemporaryDirectory(prefix="launcher-", dir=packaging_dir) as temporary:
        launcher = Path(temporary) / "launcher.py"
        launcher.write_text(
            "from virtualkvm.__main__ import main\n"
            "if __name__ == '__main__':\n"
            "    raise SystemExit(main())\n",
            encoding="utf-8",
        )
        specification_options = [
            "--name", "VirtualKVM", "--paths", str(root),
            "--collect-all", "virtualkvm", "--add-data", f"{notices}:licenses",
            "--specpath", str(packaging_dir),
            "--console" if args.console else "--windowed",
        ]
        if args.bundle_ffmpeg:
            if __package__:
                from .bundle_runtime import prepare_runtime
            else:
                from bundle_runtime import prepare_runtime
            ffmpeg = args.ffmpeg or shutil.which("ffmpeg")
            ffprobe = args.ffprobe or shutil.which("ffprobe")
            if not ffmpeg or not ffprobe:
                parser.error("Install FFmpeg/ffprobe or provide --ffmpeg and --ffprobe before bundling")
            runtime = prepare_runtime(Path(ffmpeg), Path(ffprobe), Path(temporary))
            specification_options += ["--add-data", f"{runtime}:runtime"]
        if sys.platform == "win32":
            specification_options += ["--icon", str(icons["ico"])]
            version_file = Path(temporary) / "version.txt"
            numeric = tuple(int(part) for part in version.split(".")) + (0,)
            version_file.write_text(
                f"VSVersionInfo(ffi=FixedFileInfo(filevers={numeric!r}, prodvers={numeric!r}, mask=0x3f, flags=0, OS=0x40004, fileType=1, subtype=0, date=(0,0)), "
                f"kids=[StringFileInfo([StringTable('040904B0', [StringStruct('FileDescription','VirtualKVM'), StringStruct('ProductName','VirtualKVM'), StringStruct('FileVersion',{version!r}), StringStruct('ProductVersion',{version!r}), StringStruct('OriginalFilename','VirtualKVM.exe')])]), VarFileInfo([VarStruct('Translation',[1033,1200])])])",
                encoding="utf-8")
            specification_options += ["--version-file", str(version_file)]
        build_options = ["--noconfirm", "--clean", "--distpath", str(root / "dist"),
                         "--workpath", str(packaging_dir / "work")]
        environment = dict(os.environ)
        environment.setdefault("PYINSTALLER_CONFIG_DIR", str(packaging_dir / "cache"))
        if sys.platform == "darwin" and not args.console:
            specification_options += ["--osx-bundle-identifier", "io.github.suqesse.virtualkvm", "--icon", str(icons["icns"])]
            identity = environment.get("MACOS_APP_SIGN_IDENTITY")
            if identity:
                specification_options += ["--codesign-identity", identity,
                    "--osx-entitlements-file", str(root / "packaging/macos/entitlements.plist")]
            # Usage descriptions belong in the generated BUNDLE configuration.
            # Editing Info.plist after PyInstaller seals the app invalidates it.
            generate = [sys.executable, "-m", "PyInstaller.utils.cliutils.makespec",
                        *specification_options, str(launcher)]
            prepared = subprocess.run(generate, cwd=root, env=environment, check=False)
            if prepared.returncode:
                return prepared.returncode
            spec = packaging_dir / "VirtualKVM.spec"
            configure_macos_privacy(spec, version)
            command = [sys.executable, "-m", "PyInstaller", *build_options, str(spec)]
        else:
            command = [sys.executable, "-m", "PyInstaller", *build_options,
                       *specification_options, str(launcher)]
        result = subprocess.run(command, cwd=root, env=environment, check=False)
    if result.returncode == 0:
        print(f"Built in {root / 'dist'}")
        print("Bundled FFmpeg/ffprobe included." if args.bundle_ffmpeg else "FFmpeg/ffprobe must be installed separately and available in PATH.")
        print("Installer generation: python scripts/installer.py")
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
