"""Build a native macOS Installer product and a drag-to-Applications disk image.

The input is a finished, sealed app bundle. This module never changes its
contents, grants privacy permissions, or touches application preferences.
"""
from __future__ import annotations

from html import escape
import json
import os
from pathlib import Path
import platform
import plistlib
import re
import shutil
import subprocess
from string import Template
import tempfile
import xml.etree.ElementTree as ET


BUNDLE_IDENTIFIER = "io.github.suqesse.virtualkvm"
ROOT = Path(__file__).resolve().parents[2]
MACOS_RESOURCES = ROOT / "packaging" / "macos"
_VERSION = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\Z")
_OS_VERSION = re.compile(r"[0-9]+(?:\.[0-9]+){1,2}\Z")
_MACHO_MAGICS = {b"\xfe\xed\xfa\xce", b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xcf",
                  b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca",
                  b"\xca\xfe\xba\xbf", b"\xbf\xba\xfe\xca"}


def _run(arguments: list[str], *, capture: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(arguments, check=True, text=True, capture_output=capture, shell=False)


def _read_plist(path: Path) -> dict:
    try:
        with path.open("rb") as source:
            value = plistlib.load(source)
    except (OSError, plistlib.InvalidFileException) as error:
        raise ValueError(f"Cannot read macOS bundle metadata: {path}") from error
    if not isinstance(value, dict):
        raise ValueError(f"Expected a dictionary in {path}")
    return value


def _validate_bundle(bundle: Path, version: str | None = None) -> dict:
    if not bundle.is_dir() or bundle.suffix.lower() != ".app":
        raise ValueError("Expected an existing macOS .app bundle")
    info = _read_plist(bundle / "Contents" / "Info.plist")
    if info.get("CFBundleIdentifier") != BUNDLE_IDENTIFIER:
        raise ValueError(f"App bundle identifier must be {BUNDLE_IDENTIFIER}")
    executable = info.get("CFBundleExecutable")
    if (not isinstance(executable, str) or executable in {"", ".", ".."}
            or re.search(r"[/\\\x00-\x1f]", executable)):
        raise ValueError("Invalid CFBundleExecutable")
    path = bundle / "Contents" / "MacOS" / executable
    if not path.is_file() or not path.resolve().is_relative_to(bundle.resolve()):
        raise ValueError("The application executable must exist inside the app bundle")
    if version is not None and info.get("CFBundleShortVersionString") != version:
        raise ValueError("CFBundleShortVersionString must match the requested installer version")
    return info


def _macho_minimum_versions(output: str):
    """Read deployment versions, excluding SDK and linker-tool versions."""
    command, target_platform, minimum = None, None, None
    for line in [*output.splitlines(), "cmd END"]:
        match = re.match(r"\s*cmd\s+(\S+)", line)
        if match:
            if command == "LC_BUILD_VERSION" and target_platform in {"1", "macos"} and minimum:
                yield minimum
            command, target_platform, minimum = match.group(1), None, None
        elif command == "LC_BUILD_VERSION":
            if match := re.match(r"\s*platform\s+(\S+)", line):
                target_platform = match.group(1).lower()
            elif match := re.match(r"\s*minos\s+([0-9]+(?:\.[0-9]+){1,2})\s*$", line):
                minimum = match.group(1)
        elif command == "LC_VERSION_MIN_MACOSX":
            if match := re.match(r"\s*version\s+([0-9]+(?:\.[0-9]+){1,2})\s*$", line):
                yield match.group(1)


def _minimum_os(bundle: Path) -> str:
    """Use actual bundle/framework metadata and Mach-O deployment targets."""
    versions: list[str] = []
    for plist in bundle.rglob("Info.plist"):
        if plist.is_symlink():
            continue
        value = _read_plist(plist).get("LSMinimumSystemVersion")
        if value is not None:
            if not isinstance(value, str) or not _OS_VERSION.fullmatch(value):
                raise ValueError(f"Invalid LSMinimumSystemVersion in {plist}")
            versions.append(value)
    # Qt's deployment target can be newer than PyInstaller's top-level plist.
    # Inspect real binary headers rather than inventing a minimum OS version.
    for binary in bundle.rglob("*"):
        if binary.is_symlink() or not binary.is_file():
            continue
        with binary.open("rb") as source:
            magic = source.read(4)
        if magic not in _MACHO_MAGICS:
            continue
        output = _run(["otool", "-l", str(binary)], capture=True).stdout
        versions.extend(_macho_minimum_versions(output))
    if not versions:
        raise ValueError("No minimum macOS deployment target found in bundle metadata or Mach-O headers")
    return max(versions, key=lambda value: tuple(int(part) for part in value.split(".")) +
               (0,) * (3 - len(value.split("."))))


def _architectures(bundle: Path, info: dict) -> tuple[str, str]:
    executable = bundle / "Contents" / "MacOS" / info["CFBundleExecutable"]
    names = set(_run(["lipo", "-archs", str(executable)], capture=True).stdout.split())
    if not names or names - {"arm64", "x86_64"}:
        raise ValueError("The app must contain arm64 and/or x86_64 macOS executables")
    return ("universal2" if len(names) == 2 else next(iter(names)), ",".join(sorted(names)))


def _setting(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if len(value) > 1024 or any(ord(character) < 32 for character in value):
        raise ValueError(f"Invalid {name} setting")
    return value


def verify_package_payload(package: Path, *, expected_version: str | None = None) -> dict:
    """Expand a real flat product and verify the installed application payload.

    This runs Apple's pkgutil, not a filename/extension check. Native CI can
    additionally invoke Installer on a macOS runner to exercise installation.
    The extraction is temporary and never executes package scripts.
    """
    if platform.system() != "Darwin":
        raise RuntimeError("macOS package verification requires macOS and pkgutil")
    package = Path(package).resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix="virtualkvm-pkg-check-") as directory:
        expanded = Path(directory) / "expanded"
        _run(["pkgutil", "--expand-full", str(package), str(expanded)])
        manifests = [path for path in expanded.rglob("PackageInfo") if path.is_file() and
                     (path.parent == expanded or
                      (path.parent.suffix == ".pkg" and path.parent.parent == expanded))]
        if len(manifests) != 1:
            raise ValueError("Installer must contain exactly one application component")
        try:
            manifest = ET.parse(manifests[0]).getroot()
        except ET.ParseError as error:
            raise ValueError("Invalid installer component metadata") from error
        if manifest.get("identifier") != BUNDLE_IDENTIFIER or manifest.get("install-location") != "/":
            raise ValueError("Installer component identity or install location is incorrect")
        payload = manifests[0].parent / "Payload"
        app = payload / "Applications" / "VirtualKVM.app"
        if not app.is_dir():
            raise ValueError("Installer payload must contain exactly one /Applications/VirtualKVM.app")
        if {item.name for item in payload.iterdir()} != {"Applications"}:
            raise ValueError("Installer payload contains files outside /Applications")
        if {item.name for item in (payload / "Applications").iterdir()} != {"VirtualKVM.app"}:
            raise ValueError("Installer payload contains unexpected applications")
        scripts = manifests[0].parent / "Scripts"
        if scripts.is_dir() and any(scripts.iterdir()):
            raise ValueError("VirtualKVM's installer must not run install scripts")
        info = _validate_bundle(app, expected_version)
        if manifest.get("version") != info.get("CFBundleShortVersionString"):
            raise ValueError("Installer component and app versions do not match")
        return {"identifier": info["CFBundleIdentifier"],
                "version": info.get("CFBundleShortVersionString"),
                "install_path": "/Applications/VirtualKVM.app"}


def _notarize(path: Path, profile: str) -> None:
    result = _run(["xcrun", "notarytool", "submit", str(path), "--keychain-profile", profile,
                   "--wait", "--output-format", "json"], capture=True)
    try:
        status = json.loads(result.stdout).get("status")
    except (ValueError, AttributeError) as error:
        raise RuntimeError("Apple's notarization response was not valid JSON") from error
    if status != "Accepted":
        raise RuntimeError(f"Apple notarization was not accepted (status: {status})")
    _run(["xcrun", "stapler", "staple", str(path)])
    _run(["xcrun", "stapler", "validate", str(path)])


def build_installers(app_bundle: Path, version: str, output_dir: Path, *,
                     icon: Path | None = None) -> list[Path]:
    """Build .pkg/.dmg artifacts without modifying the supplied .app bundle."""
    if platform.system() != "Darwin":
        raise RuntimeError("macOS installers must be built on macOS with Apple's native packaging tools")
    if not isinstance(version, str) or not _VERSION.fullmatch(version):
        raise ValueError("Installer version must be canonical numeric X.Y.Z")
    app_bundle = Path(app_bundle).resolve(strict=True)
    output_dir = Path(output_dir).resolve()
    if output_dir == app_bundle or output_dir.is_relative_to(app_bundle):
        raise ValueError("Installer output must be outside the app bundle")
    info = _validate_bundle(app_bundle, version)
    minimum_os = _minimum_os(app_bundle)
    architecture, host_architectures = _architectures(app_bundle, info)
    application_identity = _setting("MACOS_APP_SIGN_IDENTITY")
    installer_identity = _setting("MACOS_INSTALLER_SIGN_IDENTITY")
    notary_profile = _setting("MACOS_NOTARY_PROFILE")
    if notary_profile and (not application_identity or not installer_identity):
        raise ValueError("Notarization requires both MACOS_APP_SIGN_IDENTITY and MACOS_INSTALLER_SIGN_IDENTITY")
    if application_identity:
        # Signing belongs in PyInstaller before the bundle is sealed. Verify;
        # never repair or re-sign an app here, which can break nested signatures.
        _run(["codesign", "--verify", "--deep", "--strict", "--verbose=2", str(app_bundle)])
    if icon is not None:
        icon = Path(icon).resolve(strict=True)
        if not icon.is_file() or icon.suffix.lower() != ".icns":
            raise ValueError("The disk image icon must be an existing .icns file")
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = f"VirtualKVM-{version}-macos-{architecture}"
    final_package, final_image = output_dir / f"{stem}.pkg", output_dir / f"{stem}.dmg"
    with tempfile.TemporaryDirectory(prefix=".virtualkvm-macos-", dir=output_dir) as temporary:
        work = Path(temporary)
        payload = work / "root"
        installed = payload / "Applications" / "VirtualKVM.app"
        installed.parent.mkdir(parents=True)
        shutil.copytree(app_bundle, installed, symlinks=True)
        if application_identity:
            _run(["codesign", "--verify", "--deep", "--strict", "--verbose=2", str(installed)])
        component_plist = work / "components.plist"
        _run(["pkgbuild", "--analyze", "--root", str(payload), str(component_plist)])
        with component_plist.open("rb") as source:
            components = plistlib.load(source)
        if not isinstance(components, list) or not components or not all(isinstance(item, dict) for item in components):
            raise ValueError("pkgbuild did not produce a valid component manifest")
        if not any(item.get("RootRelativeBundlePath") == "Applications/VirtualKVM.app" for item in components):
            raise ValueError("pkgbuild did not detect the staged application")
        for component in components:
            component["BundleIsRelocatable"] = False
            component["BundleHasStrictIdentifier"] = True
            component["BundleIsVersionChecked"] = True
            component["BundleOverwriteAction"] = "upgrade"
        with component_plist.open("wb") as target:
            plistlib.dump(components, target)
        component = work / "VirtualKVM-component.pkg"
        _run(["pkgbuild", "--root", str(payload), "--component-plist", str(component_plist),
              "--identifier", BUNDLE_IDENTIFIER, "--version", version,
              "--install-location", "/", "--ownership", "recommended", str(component)])
        resources = work / "resources"
        shutil.copytree(MACOS_RESOURCES / "resources", resources)
        shutil.copy2(ROOT / "LICENSE", resources / "license.txt")
        distribution = work / "Distribution.xml"
        distribution.write_text(Template((MACOS_RESOURCES / "Distribution.xml").read_text(encoding="utf-8")).substitute(
            VERSION=escape(version, quote=True), MINIMUM_OS=escape(minimum_os, quote=True),
            ARCHITECTURES=escape(host_architectures, quote=True), IDENTIFIER=BUNDLE_IDENTIFIER), encoding="utf-8")
        unsigned_package = work / "unsigned.pkg"
        _run(["productbuild", "--distribution", str(distribution), "--resources", str(resources),
              "--package-path", str(work), str(unsigned_package)])
        package = work / f"{stem}.pkg"
        if installer_identity:
            _run(["productsign", "--sign", installer_identity, str(unsigned_package), str(package)])
            _run(["pkgutil", "--check-signature", str(package)])
        else:
            unsigned_package.replace(package)
        verify_package_payload(package, expected_version=version)
        disk_root = work / "disk"
        disk_root.mkdir()
        shutil.copytree(app_bundle, disk_root / "VirtualKVM.app", symlinks=True)
        (disk_root / "Applications").symlink_to("/Applications", target_is_directory=True)
        (disk_root / "README.txt").write_text(
            "VirtualKVM\n\nDrag VirtualKVM.app to Applications, then open it from Applications.\n"
            "The separate .pkg download offers the macOS Installer wizard instead.\n"
            "Pair your computers using the host's PIN and certificate fingerprint.\n"
            "Grant Screen Recording, Accessibility, or Microphone access in System Settings\n"
            "when using the corresponding feature. Existing preferences are preserved.\n",
            encoding="utf-8")
        if icon is not None:
            shutil.copy2(icon, disk_root / ".VolumeIcon.icns")
            finder_info = bytearray(32)
            finder_info[8:10] = (0x0400).to_bytes(2, "big")  # kHasCustomIcon
            _run(["/usr/bin/xattr", "-wx", "com.apple.FinderInfo", bytes(finder_info).hex(), str(disk_root)])
        image = work / f"{stem}.dmg"
        _run(["hdiutil", "create", "-volname", "VirtualKVM", "-srcfolder", str(disk_root),
              "-format", "UDZO", str(image)])
        if application_identity:
            _run(["codesign", "--sign", application_identity, "--timestamp", str(image)])
            _run(["codesign", "--verify", "--strict", "--verbose=2", str(image)])
        if notary_profile:
            _notarize(package, notary_profile)
            _notarize(image, notary_profile)
        _run(["hdiutil", "verify", str(image)])
        for artifact in (package, image):
            if not artifact.is_file() or artifact.stat().st_size == 0:
                raise RuntimeError(f"Native packaging tool did not create {artifact.name}")
        # Publish only after both native artifacts have passed validation.
        package.replace(final_package)
        image.replace(final_image)
    return [final_package, final_image]
