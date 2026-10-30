"""Exercise native install, offline operation, upgrade and uninstall in CI."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import plistlib
import subprocess
import sys
import tempfile
import time
import uuid


def run(command, *, env=None, timeout=120):
    print("Running:", Path(command[0]).name, *command[1:2], flush=True)
    subprocess.run([str(item) for item in command], env=env, check=True, timeout=timeout)


def offline_environment():
    environment = dict(os.environ)
    environment["PATH"] = ""
    for key in ("VIRTUALKVM_FFMPEG", "VIRTUALKVM_FFPROBE", "PYTHONPATH", "LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH", "DYLD_FALLBACK_LIBRARY_PATH"):
        environment.pop(key, None)
    environment["QT_QPA_PLATFORM"] = "offscreen"
    return environment


def check_windows(installer, staging):
    import winreg
    from installers.windows import APP_ID
    key = f"Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\{APP_ID}_is1"
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, key):
                raise RuntimeError("Refusing to replace an existing VirtualKVM installation")
        except FileNotFoundError:
            pass
    target = staging / "Installed VirtualKVM"
    environment = offline_environment()
    install = [str(installer), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-", f"/DIR={target}"]
    binary = target / "VirtualKVM.exe"
    state = Path(os.environ["LOCALAPPDATA"]) / "VirtualKVM"
    state.mkdir(parents=True, exist_ok=True)
    sentinel = state / ("installer-test-" + uuid.uuid4().hex)
    sentinel.write_text("preserve-user-state")
    try:
        run(install)
        if not binary.is_file():
            raise RuntimeError("Installed executable missing")
        run([str(binary), "doctor"], env=environment)
        run([str(binary), "smoke", "--fps", "240", "--audio"], env=environment)
        run(install)  # Upgrade using the same application ID and directory.
        if sentinel.read_text() != "preserve-user-state":
            raise RuntimeError("Upgrade changed user state")
        run([str(binary), "doctor"], env=environment)
    finally:
        uninstallers = sorted(target.glob("unins*.exe"))
        if uninstallers:
            run([str(uninstallers[0]), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"])
        if not sentinel.is_file():
            raise RuntimeError("Uninstall removed user state")
        sentinel.unlink()
    deadline = time.monotonic() + 30
    while binary.exists() and time.monotonic() < deadline:
        time.sleep(0.2)
    if binary.exists():
        raise RuntimeError("Uninstaller left the application executable behind")
    return {"install": True, "offline_media": True, "upgrade": True, "uninstall": True, "user_state_preserved": True}


def check_macos(package, image):
    from installers.macos import verify_package_payload
    details = verify_package_payload(package)
    app = Path("/Applications/VirtualKVM.app")
    if app.exists() or app.is_symlink():
        raise RuntimeError("Refusing to replace an existing /Applications/VirtualKVM.app")
    environment = offline_environment()
    identifier = details["identifier"]
    state = Path.home() / ".virtualkvm"
    state.mkdir(parents=True, exist_ok=True)
    sentinel = state / ("installer-test-" + uuid.uuid4().hex)
    sentinel.write_text("preserve-user-state")
    try:
        run(["/usr/bin/sudo", "/usr/sbin/installer", "-pkg", str(package), "-target", "/"])
        with (app / "Contents/Info.plist").open("rb") as source:
            info = plistlib.load(source)
        if info["CFBundleShortVersionString"] != details["version"]:
            raise RuntimeError("Installed bundle version differs from its package")
        binary = app / "Contents/MacOS" / info["CFBundleExecutable"]
        run([str(binary), "doctor"], env=environment)
        run([str(binary), "smoke", "--fps", "240", "--audio"], env=environment)
        run(["/usr/bin/sudo", "/usr/sbin/installer", "-pkg", str(package), "-target", "/"])
        run([str(binary), "doctor"], env=environment)
        run(["/usr/bin/hdiutil", "verify", str(image)])
        with tempfile.TemporaryDirectory(prefix="virtualkvm-dmg-") as temporary:
            mount = Path(temporary) / "mount"
            mount.mkdir()
            run(["/usr/bin/hdiutil", "attach", str(image), "-readonly", "-nobrowse", "-mountpoint", str(mount)])
            try:
                if not (mount / "VirtualKVM.app").is_dir():
                    raise RuntimeError("DMG does not contain the app")
                if not (mount / "Applications").is_symlink() or os.readlink(mount / "Applications") != "/Applications":
                    raise RuntimeError("DMG Applications shortcut is invalid")
            finally:
                run(["/usr/bin/hdiutil", "detach", str(mount)])
    finally:
        if app.is_dir() and not app.is_symlink():
            run(["/usr/bin/sudo", "/bin/rm", "-rf", str(app)])
        subprocess.run(["/usr/bin/sudo", "/usr/sbin/pkgutil", "--forget", identifier], check=False)
        if not sentinel.is_file():
            raise RuntimeError("Installer lifecycle changed user state")
        sentinel.unlink()
    return {"install": True, "offline_media": True, "upgrade": True, "dmg_verified": True, "dmg_layout": True, "user_state_preserved": True}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=Path("dist/installers"))
    parser.add_argument("--allow-install", action="store_true", help="Required: install only on a disposable CI machine")
    args = parser.parse_args(argv)
    if not args.allow_install or os.environ.get("CI", "").lower() != "true":
        parser.error("Native installation checks require --allow-install and CI=true on a disposable runner")
    directory = args.directory.resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix="virtualkvm-installer-test-") as temporary:
        if platform.system() == "Windows":
            installers = list(directory.glob("VirtualKVM-*-windows-x64-setup.exe"))
            if len(installers) != 1:
                raise RuntimeError("Expected one fresh Windows installer")
            report = check_windows(installers[0], Path(temporary))
        elif platform.system() == "Darwin":
            packages, images = list(directory.glob("*.pkg")), list(directory.glob("*.dmg"))
            if len(packages) != 1 or len(images) != 1:
                raise RuntimeError("Expected one fresh macOS package and disk image")
            report = check_macos(packages[0], images[0])
        else:
            parser.error("Native installation checks require Windows or macOS")
    (directory / "installation-check.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
