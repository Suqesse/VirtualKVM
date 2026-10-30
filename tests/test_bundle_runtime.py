"""Runtime packaging checks, including mocked Mach-O dependency relocation."""
import json
import os
from pathlib import Path
import platform
import shutil
import tempfile
import unittest
from unittest.mock import patch

from scripts.bundle_runtime import (BundleError, _macho_rpaths, _relocate_macos,
    _reject_windows_shim, _resolve_dependency, _windows_dlls, prepare_runtime)
from virtualkvm.media import FFmpegLocator, MediaError


class LocatorBundleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.extension = ".exe" if os.name == "nt" else ""
        environment = patch.dict(os.environ, {}, clear=False)
        environment.start()
        self.addCleanup(environment.stop)
        for name in ("VIRTUALKVM_FFMPEG", "VIRTUALKVM_FFPROBE"):
            os.environ.pop(name, None)

    def tearDown(self):
        self.temporary.cleanup()

    def tool(self, directory, name):
        path = directory / (name + self.extension)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fixture")
        path.chmod(0o755)
        return str(path)

    def test_bundle_pair_precedes_path_and_needs_no_system_path(self):
        runtime = self.root / "runtime"
        ffmpeg, ffprobe = self.tool(runtime, "ffmpeg"), self.tool(runtime, "ffprobe")
        with patch.object(FFmpegLocator, "_bundle_directories", return_value=[runtime]), \
                patch.dict(os.environ, {"PATH": ""}), \
                patch.dict(os.environ, {}, clear=False):
            previous = {name: os.environ.pop(name, None) for name in ("VIRTUALKVM_FFMPEG", "VIRTUALKVM_FFPROBE")}
            try:
                locator = FFmpegLocator()
            finally:
                for name, value in previous.items():
                    if value is not None:
                        os.environ[name] = value
        self.assertEqual((locator.ffmpeg, locator.ffprobe), (ffmpeg, ffprobe))

    def test_explicit_override_is_authoritative(self):
        custom = self.root / "custom"
        ffmpeg, ffprobe = self.tool(custom, "ffmpeg"), self.tool(custom, "ffprobe")
        with patch.object(FFmpegLocator, "_bundled_pair", side_effect=AssertionError("Override must avoid bundle fallback")):
            locator = FFmpegLocator(ffmpeg, ffprobe)
        self.assertEqual((locator.ffmpeg, locator.ffprobe), (ffmpeg, ffprobe))
        with self.assertRaises(MediaError):
            FFmpegLocator(str(custom / "missing"), ffprobe)

    def test_incomplete_bundle_does_not_silently_use_external_probe(self):
        runtime = self.root / "runtime"
        self.tool(runtime, "ffmpeg")
        with patch.object(FFmpegLocator, "_bundle_directories", return_value=[runtime]):
            with self.assertRaisesRegex(MediaError, "incomplete"):
                FFmpegLocator()

    def test_bundle_directories_cover_frozen_windows_and_mac_layouts(self):
        with patch("virtualkvm.media.sys.executable", str(self.root / "VirtualKVM.app/Contents/MacOS/VirtualKVM")), \
                patch("virtualkvm.media.sys._MEIPASS", str(self.root / "extracted"), create=True):
            directories = FFmpegLocator._bundle_directories()
        self.assertIn(self.root / "extracted/runtime", directories)
        self.assertIn(self.root / "VirtualKVM.app/Contents/Resources/runtime", directories)


class MacRelocationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.runtime = self.root / "runtime"
        self.runtime.mkdir()
        self.ffmpeg = self.file("brew/bin/ffmpeg")
        self.ffprobe = self.file("brew/bin/ffprobe")
        self.executables = {self.ffmpeg: self.runtime / "ffmpeg", self.ffprobe: self.runtime / "ffprobe"}
        for source, destination in self.executables.items():
            shutil.copy2(source, destination)
        self.graph = {self.ffmpeg: [], self.ffprobe: []}
        self.rpaths = {}
        self.commands = []

    def tearDown(self):
        self.temporary.cleanup()

    def file(self, relative):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"Mach-O fixture " + relative.encode())
        return path.resolve()

    def fake_run(self, command, **kwargs):
        command = [str(item) for item in command]
        self.commands.append(command)
        if command[:2] == ["otool", "-L"]:
            source = Path(command[2])
            original = source if source in self.graph else next(path for path in self.graph if path.read_bytes() == source.read_bytes())
            references = list(self.graph[original])
            for change in self.commands:
                if change[:2] == ["install_name_tool", "-change"] and change[-1] == str(source):
                    references = [change[3] if item == change[2] else item for item in references]
                elif change[:2] == ["install_name_tool", "-id"] and change[-1] == str(source):
                    references = [change[2] if item == str(original) else item for item in references]
            return str(source) + ":\n" + "\n".join(f"\t{item} (compatibility version 1.0.0, current version 1.0.0)" for item in references)
        if command[:2] == ["otool", "-l"]:
            source = Path(command[2])
            original = source if source in self.graph else next(path for path in self.graph if path.read_bytes() == source.read_bytes())
            removed = [item[2] for item in self.commands if item[:2] == ["install_name_tool", "-delete_rpath"] and item[-1] == str(source)]
            return "\n".join(f"Load command 1\n cmd LC_RPATH\n cmdsize 48\n path {value} (offset 12)" for value in self.rpaths.get(original, []) if value not in removed)
        return ""

    def test_recursive_cycle_relocated_and_system_libraries_preserved(self):
        first = self.file("brew/lib/libfirst.dylib")
        second = self.file("brew/lib/libsecond.dylib")
        self.graph.update({self.ffmpeg: [str(first), "/usr/lib/libSystem.B.dylib"],
                           self.ffprobe: [str(second)], first: [str(first), str(second)], second: [str(first)]})
        with patch("scripts.bundle_runtime._run", side_effect=self.fake_run), \
                patch.dict(os.environ, {"MACOS_APP_SIGN_IDENTITY": ""}):
            mapping = _relocate_macos(self.runtime, self.executables)
        self.assertEqual(len(mapping), 4)
        changes = [command for command in self.commands if command[:2] == ["install_name_tool", "-change"]]
        self.assertEqual(len(changes), 4)
        self.assertTrue(all(command[3].startswith("@loader_path/") for command in changes))
        self.assertFalse(any("/usr/lib/libSystem.B.dylib" in command for command in changes))
        self.assertEqual(len([command for command in self.commands if command[:3] == ["codesign", "--force", "--sign"]]), 4)
        self.assertTrue(all(path.is_file() for path in mapping.values()))

    def test_rpath_expansion_and_basename_collisions(self):
        first = self.file("brew/lib/libsame.dylib")
        second = self.file("other/lib/libsame.dylib")
        self.graph.update({self.ffmpeg: ["@rpath/libsame.dylib", str(second)], first: [], second: []})
        self.rpaths[self.ffmpeg] = ["@loader_path/../lib"]
        with patch("scripts.bundle_runtime._run", side_effect=self.fake_run):
            mapping = _relocate_macos(self.runtime, self.executables)
        self.assertNotEqual(mapping[first].name, mapping[second].name)
        self.assertTrue(any(command[:2] == ["install_name_tool", "-delete_rpath"] for command in self.commands))

    def test_missing_dependency_and_non_system_framework_fail(self):
        with self.assertRaisesRegex(BundleError, "Cannot resolve"):
            _resolve_dependency("@rpath/missing.dylib", self.ffmpeg, self.ffmpeg, [])
        framework = self.file("Custom.framework/Versions/A/Custom")
        self.graph[self.ffmpeg] = [str(framework)]
        with patch("scripts.bundle_runtime._run", side_effect=self.fake_run):
            with self.assertRaisesRegex(BundleError, "framework resources"):
                _relocate_macos(self.runtime, self.executables)

    def test_signing_identity_enables_hardened_runtime_timestamp(self):
        with patch("scripts.bundle_runtime._run", side_effect=self.fake_run), \
                patch.dict(os.environ, {"MACOS_APP_SIGN_IDENTITY": "Developer ID Application: Example"}):
            _relocate_macos(self.runtime, self.executables)
        signing = [command for command in self.commands if command[:3] == ["codesign", "--force", "--sign"]]
        self.assertEqual(len(signing), 2)
        self.assertTrue(all("--timestamp" in command and "runtime" in command for command in signing))


class RuntimeStagingTests(unittest.TestCase):
    def test_windows_chocolatey_shims_are_not_treated_as_portable_binaries(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "Chocolatey/bin/ffmpeg.exe"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"launcher fixture")
            with self.assertRaisesRegex(BundleError, "real FFmpeg executable"):
                _reject_windows_shim(source)
            renamed = Path(temporary) / "renamed.exe"
            renamed.write_bytes(b"MZ" + "ShimGen".encode("utf-16le"))
            with self.assertRaises(BundleError):
                _reject_windows_shim(renamed)

    def test_failed_verification_never_publishes_partial_runtime(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sources = []
            for name in ("ffmpeg", "ffprobe"):
                source = root / name
                source.write_bytes(b"unverified fixture")
                source.chmod(0o555)
                sources.append(source)
            with patch("scripts.bundle_runtime.platform.system", return_value="Linux"), \
                    patch("scripts.bundle_runtime.verify_runtime", side_effect=BundleError("missing library")):
                with self.assertRaisesRegex(BundleError, "not self-contained"):
                    prepare_runtime(*sources, root / "output")
            self.assertFalse((root / "output/runtime").exists())
            self.assertFalse(list((root / "output").iterdir()))
            self.assertEqual(sources[0].read_bytes(), b"unverified fixture")
            if os.name == "posix":
                self.assertEqual(sources[0].stat().st_mode & 0o777, 0o555)

    def test_windows_adjacent_dlls_preserved_and_collision_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = root / "runtime"
            runtime.mkdir()
            sources = []
            for directory in (root / "one", root / "two"):
                directory.mkdir()
                source = directory / "ffmpeg.exe"
                source.write_bytes(b"fixture")
                (directory / "avcodec.dll").write_bytes(b"same")
                sources.append(source)
            self.assertEqual(len(_windows_dlls(runtime, sources)), 1)
            (sources[1].parent / "avcodec.dll").write_bytes(b"different")
            with self.assertRaisesRegex(BundleError, "Conflicting"):
                _windows_dlls(runtime, sources)

    @unittest.skipUnless(platform.system() == "Linux" and shutil.which("ffmpeg") and shutil.which("ffprobe"), "Uses the Linux build host's real FFmpeg")
    def test_real_staged_runtime_encodes_and_probes_without_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            runtime = prepare_runtime(Path(shutil.which("ffmpeg")), Path(shutil.which("ffprobe")), output)
            manifest = json.loads((runtime / "manifest.json").read_text())
            self.assertTrue(manifest["verification"]["path_empty"])
            self.assertEqual(manifest["verification"]["encoders"], ["libx264", "aac"])
            self.assertIsNotNone(manifest["linux_note"])
            self.assertTrue((runtime / "licenses/SOURCE_INDEX.json").is_file())
            with patch.object(FFmpegLocator, "_bundle_directories", return_value=[runtime]), \
                    patch.dict(os.environ, {"PATH": ""}):
                locator = FFmpegLocator()
                self.assertEqual(Path(locator.ffmpeg), runtime / "ffmpeg")
                self.assertTrue(locator.check_ready()["srt"])
            with self.assertRaisesRegex(BundleError, "already exists"):
                prepare_runtime(Path(shutil.which("ffmpeg")), Path(shutil.which("ffprobe")), output)
