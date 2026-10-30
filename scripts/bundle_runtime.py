"""Stage and verify the native FFmpeg runtime used by packaged applications.

No binaries are downloaded here. Release tooling supplies already verified local
FFmpeg/ffprobe installations. macOS third-party dylibs are copied and relocated;
Windows binaries and adjacent DLLs must pass a clean-PATH functional check.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import stat
import subprocess
import tempfile


class BundleError(RuntimeError):
    pass


def _run(command, *, env=None, cwd=None, timeout=60):
    try:
        result = subprocess.run([str(item) for item in command], stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, errors="replace",
                                env=env, cwd=cwd, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise BundleError(f"Cannot run {Path(command[0]).name}: {error}") from error
    if result.returncode:
        raise BundleError(f"{Path(command[0]).name} failed ({result.returncode}): {result.stdout[-4000:]}")
    return result.stdout


def _digest(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _system_library(reference):
    return str(reference).startswith(("/System/Library/", "/usr/lib/"))


def _macho_dependencies(path):
    output = _run(["otool", "-L", path])
    dependencies = []
    for line in output.splitlines():
        match = re.match(r"\s+(.+?) \(compatibility version ", line)
        if match and match.group(1) not in dependencies:
            dependencies.append(match.group(1))
    return dependencies


def _macho_rpaths(path):
    output = _run(["otool", "-l", path])
    paths = []
    pending = False
    for line in output.splitlines():
        if line.strip().startswith("cmd "):
            pending = line.strip() == "cmd LC_RPATH"
        elif pending and (match := re.match(r"\s*path (.+) \(offset \d+\)", line)):
            if match.group(1) not in paths:
                paths.append(match.group(1))
            pending = False
    return paths


def _expand_loader(value, source, executable):
    if value.startswith("@loader_path/"):
        return source.parent / value[len("@loader_path/"):]
    if value.startswith("@executable_path/"):
        return executable.parent / value[len("@executable_path/"):]
    if Path(value).is_absolute():
        return Path(value)
    raise BundleError(f"Unsupported Mach-O load path {value!r} in {source.name}")


def _resolve_dependency(reference, source, executable, rpaths):
    if reference.startswith("@rpath/"):
        suffix = reference[len("@rpath/"):]
        candidates = [_expand_loader(path, source, executable) / suffix for path in rpaths]
    else:
        candidates = [_expand_loader(reference, source, executable)]
    for candidate in candidates:
        if _system_library(candidate):
            # Modern macOS keeps many system libraries only in the dyld cache.
            return candidate
        if candidate.is_file():
            return candidate.resolve(strict=True)
    raise BundleError(f"Cannot resolve {reference!r} required by {source}; supply a complete native FFmpeg installation")


def _sign(path):
    identity = os.environ.get("MACOS_APP_SIGN_IDENTITY", "").strip()
    arguments = ["codesign", "--force", "--sign", identity or "-"]
    if identity:
        arguments += ["--options", "runtime", "--timestamp"]
    _run([*arguments, path])


def _verify_macho_layout(runtime, binaries):
    root = runtime.resolve(strict=True)
    for binary in binaries:
        for reference in _macho_dependencies(binary):
            if _system_library(reference):
                continue
            if not reference.startswith("@loader_path/"):
                raise BundleError(f"Unrelocated dependency {reference!r} remains in {binary.name}")
            target = (binary.parent / reference[len("@loader_path/"):]).resolve(strict=True)
            if not target.is_relative_to(root) or not target.is_file():
                raise BundleError(f"Dependency escaped the staged runtime: {reference}")
        if _macho_rpaths(binary):
            raise BundleError(f"Unexpected external runtime search paths remain in {binary.name}")


def _relocate_macos(runtime: Path, executables: dict[Path, Path]):
    """Resolve canonical dependency identities before changing staged copies."""
    library_dir = runtime / "lib"
    library_dir.mkdir()
    destinations = dict(executables)
    graph = {}
    original_rpaths = {}
    pending = [(source, source, ()) for source in executables]
    while pending:
        source, executable, inherited = pending.pop()
        if source in graph:
            continue
        own_rpaths = _macho_rpaths(source)
        original_rpaths[source] = own_rpaths
        # Expand inherited loader paths at the object which declares them.
        search = list(own_rpaths) + list(inherited)
        resolved_search = [str(_expand_loader(item, source, executable)) for item in search]
        edges = []
        graph[source] = edges
        for reference in _macho_dependencies(source):
            if _system_library(reference):
                continue
            dependency = _resolve_dependency(reference, source, executable, resolved_search)
            if dependency == source:  # LC_ID_DYLIB is also printed by otool -L.
                continue
            if _system_library(dependency):
                edges.append((reference, str(dependency)))
                continue
            if any(part.endswith(".framework") for part in dependency.parts):
                raise BundleError(f"Non-system framework {dependency} requires framework resources; use an FFmpeg build without third-party frameworks")
            if dependency not in destinations:
                suffix = hashlib.sha256(str(dependency).encode()).hexdigest()[:12]
                name = f"{dependency.stem}-{suffix}.dylib"
                destination = library_dir / name
                shutil.copy2(dependency, destination)
                destination.chmod(destination.stat().st_mode | stat.S_IWUSR)
                destinations[dependency] = destination
                pending.append((dependency, executable, tuple(resolved_search)))
            relative = os.path.relpath(destinations[dependency], destinations[source].parent).replace(os.sep, "/")
            edges.append((reference, "@loader_path/" + relative))
    for source, destination in destinations.items():
        # Preserve the existing signature's Mach-O layout while rewriting.
        # codesign --remove-signature can leave an invalid __LINKEDIT segment;
        # PyInstaller follows the same rewrite-then-force-sign ordering.
        command = ["install_name_tool"]
        for reference, replacement in graph[source]:
            command += ["-change", reference, replacement]
        if source not in executables:
            command += ["-id", "@loader_path/" + destination.name]
        for rpath in original_rpaths[source]:
            command += ["-delete_rpath", rpath]
        if len(command) > 1:
            _run([*command, destination])
    # Sign nested libraries first, then executable helpers. Public signing
    # identities are accepted through the same release environment as the app.
    ordered = [p for s, p in destinations.items() if s not in executables] + list(executables.values())
    _verify_macho_layout(runtime, ordered)
    for destination in ordered:
        _sign(destination)
    return destinations


def _windows_dlls(runtime, sources):
    copied = {}
    for source in sources:
        for candidate in source.parent.iterdir():
            if candidate.is_file() and candidate.suffix.lower() == ".dll":
                key = candidate.name.lower()
                checksum = _digest(candidate)
                if key in copied:
                    if copied[key][1] != checksum:
                        raise BundleError(f"Conflicting adjacent DLLs named {candidate.name}")
                    continue
                destination = runtime / candidate.name
                shutil.copy2(candidate, destination)
                copied[key] = (candidate.resolve(), checksum)
    return [entry[0] for entry in copied.values()]


def _reject_windows_shim(source):
    chocolatey_root = os.environ.get("ChocolateyInstall")
    known_directory = source.parent.name.lower() == "bin" and source.parent.parent.name.lower() == "chocolatey"
    if chocolatey_root:
        known_directory = known_directory or source.parent == (Path(chocolatey_root) / "bin").resolve()
    with source.open("rb") as stream:
        sample = stream.read(1024 * 1024)
        stream.seek(max(0, source.stat().st_size - 1024 * 1024))
        sample += stream.read(1024 * 1024)
    if known_directory or b"shimgen" in sample.lower() or "ShimGen".encode("utf-16le").lower() in sample.lower():
        raise BundleError(f"{source} is a Chocolatey/shim launcher; select the real FFmpeg executable under the ffmpeg-full tools directory")


def verify_runtime(runtime: Path):
    """Exercise staged binaries without PATH or build-machine library overrides."""
    extension = ".exe" if platform.system() == "Windows" else ""
    ffmpeg, ffprobe = runtime / ("ffmpeg" + extension), runtime / ("ffprobe" + extension)
    environment = dict(os.environ)
    environment["PATH"] = ""
    for name in ("LD_LIBRARY_PATH", "LD_PRELOAD", "DYLD_LIBRARY_PATH", "DYLD_FALLBACK_LIBRARY_PATH", "DYLD_INSERT_LIBRARIES"):
        environment.pop(name, None)
    with tempfile.TemporaryDirectory(prefix="virtualkvm-runtime-check-") as temporary:
        arguments = {"env": environment, "cwd": temporary}
        version = _run([ffmpeg, "-hide_banner", "-version"], **arguments)
        probe_version = _run([ffprobe, "-hide_banner", "-version"], **arguments)
        protocols = _run([ffmpeg, "-hide_banner", "-protocols"], **arguments)
        encoders = _run([ffmpeg, "-hide_banner", "-encoders"], **arguments)
        if not any(line.strip() == "srt" for line in protocols.splitlines()):
            raise BundleError("The staged FFmpeg has no SRT transport support")
        for encoder, media_type in (("libx264", "V"), ("aac", "A")):
            if not re.search(rf"^\s*{media_type}\S{{5}}\s+{encoder}\s", encoders, flags=re.MULTILINE):
                raise BundleError(f"The staged FFmpeg requires the {encoder} encoder")
        sample = Path(temporary) / "sample.mkv"
        _run([ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
              "-f", "lavfi", "-i", "testsrc2=size=64x64:rate=30",
              "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=48000",
              "-t", "0.15", "-c:v", "libx264", "-preset", "ultrafast", "-threads", "2",
              "-c:a", "aac", str(sample)], **arguments)
        inspection = json.loads(_run([ffprobe, "-v", "error", "-show_streams", "-of", "json", sample], **arguments))
        codecs = {(stream.get("codec_type"), stream.get("codec_name")) for stream in inspection.get("streams", [])}
        if not {("video", "h264"), ("audio", "aac")} <= codecs:
            raise BundleError("The staged media runtime failed H.264/AAC encode/probe validation")
        return {"ffmpeg_version": version, "ffprobe_version": probe_version,
                "protocols": ["srt"], "encoders": ["libx264", "aac"],
                "functional_check": "64x64 H.264 and AAC encoded and inspected with staged ffprobe", "path_empty": True}


def _copy_notices(runtime, sources, verification):
    project = Path(__file__).resolve().parents[1]
    licenses = runtime / "licenses"
    licenses.mkdir()
    for source in (project / "docs" / "licenses").glob("*.txt"):
        shutil.copy2(source, licenses / source.name)
    for name in ("GPL-2", "LGPL-2.1"):
        source = Path("/usr/share/common-licenses") / name
        if source.is_file():
            shutil.copy2(source, licenses / (name + ".txt"))
    notice = project / "docs" / "THIRD_PARTY_NOTICES.md"
    if notice.is_file():
        shutil.copy2(notice, licenses / notice.name)
    copied = []
    prefixes = set()
    for source in sources:
        parts = source.parts
        if "Cellar" in parts:
            index = parts.index("Cellar")
            if len(parts) > index + 2:
                prefixes.add(Path(*parts[:index + 3]))
        else:
            prefixes.add(source.parent)
            if source.parent.name.lower() == "bin":
                prefixes.add(source.parent.parent)
    for prefix in sorted(prefixes):
        component = prefix.name + "-" + hashlib.sha256(str(prefix).encode()).hexdigest()[:10]
        candidates = []
        for pattern in ("LICENSE*", "COPYING*", "COPYRIGHT*", "copyright", "INSTALL_RECEIPT.json", ".brew/*.rb",
                        "share/doc/*/copyright", "share/doc/*/LICENSE*", "share/doc/*/COPYING*"):
            candidates.extend(prefix.glob(pattern))
        for source in sorted(set(candidates)):
            if not source.is_file():
                continue
            relative = source.relative_to(prefix)
            destination = licenses / "components" / component / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            copied.append(str(destination.relative_to(runtime)))
    index = {"ffmpeg_source": "https://ffmpeg.org/download.html#get-sources",
             "ffmpeg_build_documentation": "https://ffmpeg.org/ffmpeg.html",
             "homebrew_source_recipes": "https://github.com/Homebrew/homebrew-core",
             "copied_license_and_recipe_files": copied,
             "complete_corresponding_source_included": False}
    (licenses / "SOURCE_INDEX.json").write_text(json.dumps(index, indent=2) + "\n", encoding="utf-8")
    (licenses / "FFMPEG_BUILD.txt").write_text(verification["ffmpeg_version"], encoding="utf-8")
    (licenses / "NOTICE.txt").write_text(
        "VirtualKVM includes FFmpeg/ffprobe and the native dependencies listed in manifest.json.\n"
        "Build versions/configuration are recorded in FFMPEG_BUILD.txt. Available local license\n"
        "texts and Homebrew receipts/source recipes are preserved under components/.\n"
        "SOURCE_INDEX.json records upstream source references separately from included files.\n",
        encoding="utf-8")


def prepare_runtime(ffmpeg: Path, ffprobe: Path, output: Path) -> Path:
    """Create output/runtime, or fail without publishing a partial runtime."""
    sources = [Path(ffmpeg).resolve(strict=True), Path(ffprobe).resolve(strict=True)]
    if not all(source.is_file() for source in sources):
        raise BundleError("FFmpeg and ffprobe must be native executable files")
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    destination = output / "runtime"
    if destination.exists() or destination.is_symlink():
        raise BundleError(f"Runtime staging destination already exists: {destination}; use a fresh staging directory")
    system = platform.system()
    if system not in {"Windows", "Darwin", "Linux"}:
        raise BundleError(f"Runtime packaging is unsupported on {system}")
    extension = ".exe" if system == "Windows" else ""
    if system == "Windows":
        for source in sources:
            _reject_windows_shim(source)
    with tempfile.TemporaryDirectory(prefix=".runtime-", dir=output) as temporary:
        runtime = Path(temporary)
        executable_map = {}
        for name, source in zip(("ffmpeg", "ffprobe"), sources):
            target = runtime / (name + extension)
            shutil.copy2(source, target)
            target.chmod(target.stat().st_mode | stat.S_IWUSR)
            executable_map[source] = target
        if len(executable_map) != 2:
            raise BundleError("FFmpeg and ffprobe must be distinct executable files")
        dependency_sources = []
        if system == "Darwin":
            dependency_sources = list(_relocate_macos(runtime, executable_map))
        elif system == "Windows":
            dependency_sources = _windows_dlls(runtime, sources)
        try:
            verification = verify_runtime(runtime)
        except BundleError as error:
            raise BundleError(f"The staged runtime is not self-contained: {error}") from error
        _copy_notices(runtime, sources + dependency_sources, verification)
        entries = [{"path": str(path.relative_to(runtime)).replace(os.sep, "/"), "sha256": _digest(path)}
                   for path in sorted(runtime.rglob("*")) if path.is_file()]
        manifest = {"platform": system, "machine": platform.machine(), "verification": verification,
                    "native_dependency_relocation": system in {"Windows", "Darwin"},
                    "linux_note": "Linux staging uses system libraries and is a build smoke test, not a portable Linux release" if system == "Linux" else None,
                    "files": entries}
        (runtime / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        runtime.rename(destination)
    return destination
