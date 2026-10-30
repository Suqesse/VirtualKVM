from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
MODULE_SPEC = importlib.util.spec_from_file_location(
    "virtualkvm_windows_installer", ROOT / "scripts" / "installers" / "windows.py")
windows = importlib.util.module_from_spec(MODULE_SPEC)
MODULE_SPEC.loader.exec_module(windows)


class WindowsInstallerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.app = self.root / "App payload; with spaces"
        self.app.mkdir()
        (self.app / "VirtualKVM.exe").write_bytes(b"MZ-test-payload")
        legal = self.app / "_internal" / "licenses"
        legal.mkdir(parents=True)
        (legal / "VirtualKVM-MIT.txt").write_text("MIT")
        (legal / "THIRD_PARTY_NOTICES.md").write_text("Runtime notices")
        media = self.app / "_internal" / "media"
        media.mkdir()
        for name in ("ffmpeg.exe", "ffprobe.exe"):
            (media / name).write_bytes(b"MZ-offline-media")
        self.output = self.root / "Installers; with spaces"
        self.icon = self.root / "App logo.ico"
        self.icon.write_bytes(b"icon-fixture")

    @staticmethod
    def compile_output(command, **kwargs):
        values = {argument[2:].split("=", 1)[0]: argument.split("=", 1)[1]
                  for argument in command if argument.startswith("/D")}
        staging = Path(values["SetupOutputDir"])
        destination = staging / f"VirtualKVM-{values['AppVersion']}-windows-x64-setup.exe"
        destination.write_bytes(b"MZ-current-installer")
        return subprocess.CompletedProcess(command, 0)

    def test_compiles_payload_with_literal_path_arguments_and_optional_icon(self):
        with patch.object(windows, "_compiler", return_value="C:/Program Files/Inno Setup 6/ISCC.exe"), \
             patch.object(windows.subprocess, "run", side_effect=self.compile_output) as compiled:
            paths = windows.build_installer(self.app, "0.1.0", self.output, icon=self.icon)
        self.assertEqual(paths, [self.output / "VirtualKVM-0.1.0-windows-x64-setup.exe"])
        self.assertEqual(paths[0].read_bytes(), b"MZ-current-installer")
        command = compiled.call_args.args[0]
        self.assertEqual(command[0], "C:/Program Files/Inno Setup 6/ISCC.exe")
        self.assertIn(f"/DAppSourceDir={self.app.resolve()}", command)
        self.assertIn(f"/DAppIconFile={self.icon.resolve()}", command)
        self.assertEqual(command[-1], str(ROOT / "packaging/windows/VirtualKVM.iss"))
        self.assertIs(compiled.call_args.kwargs["check"], True)
        self.assertIs(compiled.call_args.kwargs["shell"], False)
        self.assertFalse(list(self.output.glob(".virtualkvm-inno-*")))

    def test_omits_optional_icon_and_accepts_four_component_windows_version(self):
        with patch.object(windows, "_compiler", return_value="ISCC.exe"), \
             patch.object(windows.subprocess, "run", side_effect=self.compile_output) as compiled:
            result = windows.build_installer(self.app, "1.2.3.4", self.output)
        self.assertTrue(result[0].is_file())
        self.assertFalse(any(arg.startswith("/DAppIconFile") for arg in compiled.call_args.args[0]))

    def test_requires_complete_offline_payload(self):
        for name in ("VirtualKVM.exe", "_internal/media/ffmpeg.exe",
                     "_internal/media/ffprobe.exe", "_internal/licenses/VirtualKVM-MIT.txt",
                     "_internal/licenses/THIRD_PARTY_NOTICES.md"):
            with self.subTest(name=name):
                path = self.app / name
                saved = path.read_bytes()
                path.unlink()
                try:
                    with self.assertRaises(FileNotFoundError), patch.object(windows.subprocess, "run") as called:
                        windows.build_installer(self.app, "0.1.0", self.output)
                    called.assert_not_called()
                finally:
                    path.write_bytes(saved)

    def test_rejects_invalid_or_injecting_versions_before_compiler(self):
        for version in ("v1.2.3", "1.2", "1.2.3-rc1", "1.2.65536", "1.2.3\n[Run]", "1.2.3\"", "1.2.3;exec", None):
            with self.subTest(version=version), self.assertRaises(ValueError), \
                 patch.object(windows.subprocess, "run") as called:
                windows.build_installer(self.app, version, self.output)
            called.assert_not_called()

    def test_refuses_template_field_injection_but_supports_spaces_and_semicolons(self):
        self.assertEqual(windows._define("Path", "C:/App path;here"), "/DPath=C:/App path;here")
        for value in ("", 'C:/x\"; Flags: external', "C:/x\n[Run]", "C:/{app}/x", "C:/x\x00", "C:/x\x7f"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                windows._define("AppSourceDir", value)

    def test_rejects_unsafe_output_path_before_creating_directories(self):
        bad = self.root / "{app}" / "output"
        with self.assertRaises(ValueError), patch.object(windows.subprocess, "run") as called:
            windows.build_installer(self.app, "0.1.0", bad)
        called.assert_not_called()
        self.assertFalse(bad.exists())

    def test_compiler_failure_preserves_previous_installer_and_removes_staging(self):
        self.output.mkdir()
        old = self.output / "VirtualKVM-0.1.0-windows-x64-setup.exe"
        old.write_bytes(b"previous installer")
        with patch.object(windows, "_compiler", return_value="ISCC.exe"), \
             patch.object(windows.subprocess, "run", side_effect=subprocess.CalledProcessError(1, ["ISCC.exe"])):
            with self.assertRaises(subprocess.CalledProcessError):
                windows.build_installer(self.app, "0.1.0", self.output)
        self.assertEqual(old.read_bytes(), b"previous installer")
        self.assertFalse(list(self.output.glob(".virtualkvm-inno-*")))

    def test_zero_exit_without_fresh_output_cannot_reuse_a_stale_installer(self):
        self.output.mkdir()
        old = self.output / "VirtualKVM-0.1.0-windows-x64-setup.exe"
        old.write_bytes(b"previous installer")
        with patch.object(windows, "_compiler", return_value="ISCC.exe"), \
             patch.object(windows.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)):
            with self.assertRaisesRegex(RuntimeError, "nonempty installer"):
                windows.build_installer(self.app, "0.1.0", self.output)
        self.assertEqual(old.read_bytes(), b"previous installer")

    def test_empty_fresh_output_is_rejected(self):
        def empty(command, **kwargs):
            result = self.compile_output(command, **kwargs)
            staging = next(arg.split("=", 1)[1] for arg in command if arg.startswith("/DSetupOutputDir="))
            (Path(staging) / "VirtualKVM-0.1.0-windows-x64-setup.exe").write_bytes(b"")
            return result
        with patch.object(windows, "_compiler", return_value="ISCC.exe"), \
             patch.object(windows.subprocess, "run", side_effect=empty):
            with self.assertRaisesRegex(RuntimeError, "nonempty installer"):
                windows.build_installer(self.app, "0.1.0", self.output)

    def test_rejects_bad_icon(self):
        bad = self.root / "image.png"
        bad.write_bytes(b"image")
        with self.assertRaises(ValueError), patch.object(windows.subprocess, "run") as called:
            windows.build_installer(self.app, "0.1.0", self.output, icon=bad)
        called.assert_not_called()

    def test_non_executable_compiler_output_is_rejected(self):
        def corrupt(command, **kwargs):
            result = self.compile_output(command, **kwargs)
            staging = next(arg.split("=", 1)[1] for arg in command if arg.startswith("/DSetupOutputDir="))
            (Path(staging) / "VirtualKVM-0.1.0-windows-x64-setup.exe").write_bytes(b"not an executable")
            return result
        with patch.object(windows, "_compiler", return_value="ISCC.exe"), \
             patch.object(windows.subprocess, "run", side_effect=corrupt):
            with self.assertRaisesRegex(RuntimeError, "not a Windows executable"):
                windows.build_installer(self.app, "0.1.0", self.output)

    def test_rejects_payload_symlink_escaping_source(self):
        outside = self.root / "private.txt"
        outside.write_text("not payload")
        try:
            (self.app / "escaping-link").symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("This runner does not permit symlink creation")
        with self.assertRaisesRegex(ValueError, "link outside"), patch.object(windows.subprocess, "run") as called:
            windows.build_installer(self.app, "0.1.0", self.output)
        called.assert_not_called()

    def test_explicit_compiler_must_exist_and_does_not_fall_back(self):
        with patch.object(windows.shutil, "which", return_value=None):
            with self.assertRaises(FileNotFoundError):
                windows._compiler(str(self.root / "missing-ISCC.exe"))

    def test_compiler_discovery_uses_path_then_standard_install_locations(self):
        with patch.object(windows.shutil, "which", return_value="from-path/ISCC.exe"):
            self.assertEqual(windows._compiler(None), "from-path/ISCC.exe")
        program_files = self.root / "Program Files (x86)"
        iscc = program_files / "Inno Setup 6" / "ISCC.exe"
        iscc.parent.mkdir(parents=True)
        iscc.write_bytes(b"compiler")
        with patch.object(windows.shutil, "which", return_value=None), \
             patch.dict(os.environ, {"ProgramFiles(x86)": str(program_files)}, clear=True):
            self.assertEqual(windows._compiler(None), str(iscc))

    def test_template_has_stable_identity_per_user_defaults_and_safe_uninstall(self):
        template = (ROOT / "packaging/windows/VirtualKVM.iss").read_text()
        self.assertIn("AppId={" + windows.APP_ID, template)
        for directive in ("PrivilegesRequired=lowest", "DefaultDirName={localappdata}\\Programs\\VirtualKVM",
                          "Uninstallable=yes", "CreateUninstallRegKey=yes", "UsePreviousAppDir=yes",
                          "CloseApplications=yes", "RestartApplications=no", 'Name: "russian"',
                          'Name: "english"', "Flags: nowait postinstall skipifsilent unchecked"):
            self.assertIn(directive, template)
        uncommented = "\n".join(line for line in template.splitlines() if not line.lstrip().startswith(";"))
        for forbidden in ("[UninstallDelete]", "[InstallDelete]", "[Registry]", "[Code]",
                          "RunOnce", "Startup", "AppData", "Downloads", "http://", "https://"):
            self.assertNotIn(forbidden, uncommented)
        self.assertIn('Name: "desktopicon"', template)
        self.assertIn('Tasks: desktopicon', template)


if __name__ == "__main__":
    unittest.main()
