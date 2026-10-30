"""Create native, self-contained Windows and macOS installers."""
from __future__ import annotations

import argparse
import hashlib
import platform
from pathlib import Path
import sys
import tomllib

if __package__:
    from .icons import generate_icons
else:
    from icons import generate_icons


def main(argv=None):
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", type=Path, help="Previously built self-contained application")
    parser.add_argument("--output", type=Path, default=root / "dist" / "installers")
    parser.add_argument("--compiler", help="Inno Setup ISCC path (Windows)")
    args = parser.parse_args(argv)
    version = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    system = platform.system()
    try:
        if system == "Windows":
            if __package__:
                from .installers.windows import build_installer
            else:
                from installers.windows import build_installer
            icons = generate_icons(root / "docs/assets/logo.svg", root / "build/installer-icons")
            artifacts = build_installer(args.app or root / "dist/VirtualKVM", version,
                args.output, compiler=args.compiler, icon=icons["ico"])
        elif system == "Darwin":
            if __package__:
                from .installers.macos import build_installers
            else:
                from installers.macos import build_installers
            icon = root / "build/packaging/icons/VirtualKVM.icns"
            artifacts = build_installers(args.app or root / "dist/VirtualKVM.app", version, args.output,
                icon=icon if icon.is_file() else None)
        else:
            parser.error("Native installers must be built on Windows or macOS; use the native CI jobs")
        args.output.mkdir(parents=True, exist_ok=True)
        lines = [hashlib.sha256(path.read_bytes()).hexdigest() + "  " + path.name for path in artifacts]
        (args.output / "SHA256SUMS").write_text("\n".join(lines) + "\n")
        for path in artifacts:
            print(path)
        return 0
    except (OSError, ValueError, RuntimeError) as error:
        print(f"Installer build failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
