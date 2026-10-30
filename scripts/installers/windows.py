"""Compile the complete Windows payload into a per-user Inno Setup installer.

Run on Windows with official Inno Setup 6's ISCC compiler installed. The helper
never downloads tools, requests elevation, signs files, or starts the host.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile


APP_ID = "{D7A9C831-9513-4B4F-B9C1-65AE1EC77B3F}"


def _define(name: str, value: str | Path) -> str:
    """One literal /D value; refuse syntax that could escape an ISS field.

    Spaces and semicolons stay inside the single argv element and the quoted
    template field. Quotes, control characters and braces are deliberately
    unsupported: brace expansion is Inno syntax even in otherwise valid paths.
    """
    text = os.fspath(value)
    if not text or any(ord(char) < 32 or ord(char) == 127 for char in text):
        raise ValueError(f"{name} cannot contain empty/control-character values")
    if any(char in text for char in '\"{}'):
        raise ValueError(f"{name} cannot contain quotes or Inno constant braces")
    return f"/D{name}={text}"


def _version(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:\.[0-9]+)?", value):
        raise ValueError("Installer version must contain three or four numeric components, e.g. 0.1.0")
    if len(value) > 32 or any(int(part) > 65535 for part in value.split(".")):
        raise ValueError("Installer version components must be at most 65535")
    return value


def _compiler(explicit: str | None) -> str:
    if explicit:
        found = shutil.which(explicit)
        if not found and Path(explicit).is_file():
            found = str(Path(explicit).resolve())
        if found:
            return found
        raise FileNotFoundError(f"Inno Setup compiler was not found: {explicit}")
    for name in ("ISCC.exe", "iscc"):
        if found := shutil.which(name):
            return found
    locations = [
        Path(value) / relative / "ISCC.exe"
        for variable, relative in (
            ("ProgramFiles(x86)", "Inno Setup 6"),
            ("ProgramFiles", "Inno Setup 6"),
            ("LOCALAPPDATA", "Programs/Inno Setup 6"),
        )
        if (value := os.environ.get(variable))
    ]
    for path in locations:
        if path.is_file():
            return str(path)
    raise FileNotFoundError("Inno Setup 6 compiler ISCC.exe is missing. Install official Inno Setup 6 or pass compiler=.")


def _payload(app_dir: Path) -> Path:
    source = app_dir.expanduser().resolve(strict=True)
    if not source.is_dir():
        raise ValueError("Windows application payload must be a directory")
    if not (source / "VirtualKVM.exe").is_file():
        raise FileNotFoundError("Windows payload must contain VirtualKVM.exe from a native Windows build")
    internal = source / "_internal"
    if not internal.is_dir():
        raise FileNotFoundError("Windows payload is missing its PyInstaller _internal runtime directory")
    for name in ("VirtualKVM-MIT.txt", "THIRD_PARTY_NOTICES.md"):
        if not (internal / "licenses" / name).is_file():
            raise FileNotFoundError(f"Windows payload is missing _internal/licenses/{name}")
    binaries: set[str] = set()
    for path in source.rglob("*"):
        # Do not silently package a link/junction's target outside the approved
        # application directory. Native PyInstaller links inside it are valid.
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
        reparse = bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
        if path.is_symlink() or reparse or (hasattr(path, "is_junction") and path.is_junction()):
            try:
                path.resolve(strict=True).relative_to(source)
            except (OSError, ValueError, RuntimeError) as error:
                raise ValueError(f"Application payload contains a link outside its root: {path.name}") from error
        if path.is_file():
            binaries.add(path.name.lower())
    missing = {"ffmpeg.exe", "ffprobe.exe"} - binaries
    if missing:
        raise FileNotFoundError("Windows payload must bundle offline media tools: " + ", ".join(sorted(missing)))
    return source


def build_installer(app_dir: Path, version: str, output_dir: Path, *,
                    compiler: str | None = None, icon: Path | None = None) -> list[Path]:
    """Return the newly compiled Setup.exe, preserving old output on failure.

    Signing, if required for distribution, is an explicit pipeline operation.
    A zero-exit compiler without a newly created nonempty output is a failure.
    """
    release = _version(version)
    source = _payload(Path(app_dir))
    source_define = _define("AppSourceDir", source)
    output = Path(output_dir).expanduser().resolve()
    if output == source or output.is_relative_to(source):
        raise ValueError("Installer output must be outside the application payload")
    _define("SetupOutputDir", output)  # Validate before creating any output.
    artwork = None
    if icon is not None:
        artwork = Path(icon).expanduser().resolve(strict=True)
        if not artwork.is_file() or artwork.suffix.lower() != ".ico":
            raise ValueError("Installer icon must be an existing .ico file")
        _define("AppIconFile", artwork)
    template = Path(__file__).resolve().parents[2] / "packaging" / "windows" / "VirtualKVM.iss"
    if not template.is_file():
        raise FileNotFoundError(f"Inno Setup template is missing: {template}")
    tool = _compiler(compiler)
    output.mkdir(parents=True, exist_ok=True)
    filename = f"VirtualKVM-{release}-windows-x64-setup.exe"
    # Compiler output must belong to this run, not an older successful build.
    with tempfile.TemporaryDirectory(prefix=".virtualkvm-inno-", dir=output) as temporary:
        staging = Path(temporary)
        command = [tool, "/Qp", _define("AppVersion", release), source_define,
                   _define("SetupOutputDir", staging)]
        if artwork is not None:
            command.append(_define("AppIconFile", artwork))
        command.append(str(template))
        subprocess.run(command, cwd=template.parent, check=True, shell=False)
        installer = staging / filename
        if not installer.is_file() or installer.stat().st_size == 0:
            raise RuntimeError("ISCC succeeded without creating the expected nonempty installer")
        with installer.open("rb") as binary:
            if binary.read(2) != b"MZ":
                raise RuntimeError("ISCC output is not a Windows executable")
        destination = output / filename
        os.replace(installer, destination)
    return [destination]
