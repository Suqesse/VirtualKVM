"""Native tool invocation and staging contracts; actual packages need macOS CI."""
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock
import xml.etree.ElementTree as ET

from scripts.installers import macos


class MacInstallerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.bundle = self.directory / "VirtualKVM.app"
        contents = self.bundle / "Contents"
        (contents / "MacOS").mkdir(parents=True)
        self.info = {
            "CFBundleIdentifier": macos.BUNDLE_IDENTIFIER,
            "CFBundleExecutable": "VirtualKVM",
            "CFBundleShortVersionString": "0.1.0",
            "CFBundleVersion": "0.1.0",
            "LSMinimumSystemVersion": "12.0",
        }
        self._write_info()
        executable = contents / "MacOS" / "VirtualKVM"
        executable.write_bytes(b"\xcf\xfa\xed\xfe" + b"fixture main executable")
        executable.chmod(0o755)
        framework = contents / "Frameworks" / "QtCore.framework"
        version = framework / "Versions" / "A"
        version.mkdir(parents=True)
        (version / "QtCore").write_bytes(b"\xcf\xfa\xed\xfe" + b"fixture Qt executable")
        (framework / "Versions" / "Current").symlink_to("A", target_is_directory=True)
        (framework / "QtCore").symlink_to("Versions/Current/QtCore")
        self.output = self.directory / "installers"
        self.commands = []
        self.observations = {}
        self.notary_status = "Accepted"
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(macos.platform, "system", return_value="Darwin").start()
        mock.patch.dict(os.environ, {"MACOS_APP_SIGN_IDENTITY": "", "MACOS_INSTALLER_SIGN_IDENTITY": "",
                                     "MACOS_NOTARY_PROFILE": ""}).start()
        mock.patch.object(macos, "_run", side_effect=self._native_tool).start()

    def _write_info(self):
        with (self.bundle / "Contents" / "Info.plist").open("wb") as target:
            plistlib.dump(self.info, target)

    def _native_tool(self, command, *, capture=False):
        self.commands.append(command)
        output = ""
        if command[0] == "otool":
            minimum = "14.0" if "QtCore" in command[-1] else "12.0"
            output = f"Load command 1\n      cmd LC_BUILD_VERSION\n  cmdsize 32\n platform 1\n    minos {minimum}\n      sdk 15.0\n"
        elif command[:2] == ["lipo", "-archs"]:
            output = self.observations.get("architectures", "arm64")
        elif command[:2] == ["pkgbuild", "--analyze"]:
            root = Path(command[command.index("--root") + 1])
            staged = root / "Applications" / "VirtualKVM.app"
            self.assertTrue((staged / "Contents/Frameworks/QtCore.framework/Versions/Current").is_symlink())
            self.assertEqual(os.readlink(staged / "Contents/Frameworks/QtCore.framework/Versions/Current"), "A")
            with Path(command[-1]).open("wb") as target:
                plistlib.dump([{"RootRelativeBundlePath": "Applications/VirtualKVM.app",
                               "BundleIsRelocatable": True}], target)
            self.observations["staging_preserved_framework_symlinks"] = True
        elif command[0] == "pkgbuild":
            path = Path(command[command.index("--component-plist") + 1])
            with path.open("rb") as source:
                self.observations["component_plist"] = plistlib.load(source)
            self.assertEqual(command[command.index("--install-location") + 1], "/")
            Path(command[-1]).write_bytes(b"xar!component fixture")
        elif command[0] == "productbuild":
            distribution = Path(command[command.index("--distribution") + 1])
            self.observations["distribution"] = ET.parse(distribution).getroot()
            resources = Path(command[command.index("--resources") + 1])
            self.assertTrue((resources / "welcome.html").is_file())
            self.assertTrue((resources / "conclusion.html").is_file())
            self.assertTrue((resources / "license.txt").is_file())
            Path(command[-1]).write_bytes(b"xar!product fixture")
        elif command[0] == "productsign":
            shutil.copy2(command[-2], command[-1])
        elif command[:2] == ["pkgutil", "--expand-full"]:
            destination = Path(command[-1]) / "VirtualKVM-component.pkg" / "Payload" / "Applications" / "VirtualKVM.app"
            destination.parent.mkdir(parents=True)
            shutil.copytree(self.bundle, destination, symlinks=True)
            (destination.parent.parent.parent / "PackageInfo").write_text(
                f'<pkg-info identifier="{macos.BUNDLE_IDENTIFIER}" version="0.1.0" install-location="/"/>', encoding="utf-8")
            if self.observations.get("unexpected_payload"):
                (destination.parent.parent / "Library").mkdir()
        elif command[:2] == ["hdiutil", "create"]:
            staging = Path(command[command.index("-srcfolder") + 1])
            self.assertTrue((staging / "Applications").is_symlink())
            self.assertEqual(os.readlink(staging / "Applications"), "/Applications")
            self.assertTrue((staging / "VirtualKVM.app/Contents/Frameworks/QtCore.framework/Versions/Current").is_symlink())
            self.assertTrue((staging / "README.txt").is_file())
            Path(command[-1]).write_bytes(b"native disk-image fixture")
            self.observations["dmg_drag_drop_links"] = True
        elif command[:3] == ["xcrun", "notarytool", "submit"]:
            output = json.dumps({"status": self.notary_status, "id": "fixture-submission"})
        return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

    def test_native_package_and_drag_drop_image_preserve_bundle(self):
        before = (self.bundle / "Contents/Info.plist").read_bytes()
        artifacts = macos.build_installers(self.bundle, "0.1.0", self.output)
        self.assertEqual([path.name for path in artifacts], ["VirtualKVM-0.1.0-macos-arm64.pkg", "VirtualKVM-0.1.0-macos-arm64.dmg"])
        self.assertTrue(all(path.is_file() for path in artifacts))
        self.assertEqual(before, (self.bundle / "Contents/Info.plist").read_bytes())
        self.assertTrue(self.observations["staging_preserved_framework_symlinks"])
        self.assertTrue(self.observations["dmg_drag_drop_links"])
        self.assertFalse(self.observations["component_plist"][0]["BundleIsRelocatable"])
        distribution = self.observations["distribution"]
        self.assertEqual(distribution.find("volume-check/allowed-os-versions/os-version").get("min"), "14.0")
        self.assertEqual(distribution.find("pkg-ref").get("id"), macos.BUNDLE_IDENTIFIER)
        self.assertFalse(any(command[0] in {"productsign", "codesign", "xcrun"} for command in self.commands))
        self.assertTrue(any(command[:2] == ["pkgutil", "--expand-full"] for command in self.commands))

    def test_signing_verifies_app_without_resigning_or_mutating_it(self):
        with mock.patch.dict(os.environ, {"MACOS_APP_SIGN_IDENTITY": "Developer ID Application: Example",
                                         "MACOS_INSTALLER_SIGN_IDENTITY": "Developer ID Installer: Example",
                                         "MACOS_NOTARY_PROFILE": "virtualkvm-release"}):
            macos.build_installers(self.bundle, "0.1.0", self.output)
        app_commands = [command for command in self.commands if command[0] == "codesign" and command[-1].endswith(".app")]
        self.assertTrue(app_commands)
        self.assertTrue(all("--verify" in command and "--sign" not in command for command in app_commands))
        self.assertTrue(any(command[0] == "productsign" for command in self.commands))
        submissions = [command for command in self.commands if command[:3] == ["xcrun", "notarytool", "submit"]]
        self.assertEqual(len(submissions), 2)
        self.assertTrue(all("--wait" in command and "--keychain-profile" in command for command in submissions))
        self.assertEqual(sum(command[:3] == ["xcrun", "stapler", "validate"] for command in self.commands), 2)

    def test_invalid_notarization_does_not_publish_artifacts(self):
        self.notary_status = "Invalid"
        with mock.patch.dict(os.environ, {"MACOS_APP_SIGN_IDENTITY": "app identity",
                                         "MACOS_INSTALLER_SIGN_IDENTITY": "installer identity",
                                         "MACOS_NOTARY_PROFILE": "profile"}), \
             self.assertRaisesRegex(RuntimeError, "not accepted"):
            macos.build_installers(self.bundle, "0.1.0", self.output)
        self.assertEqual(list(self.output.iterdir()), [])

    def test_notarization_requires_real_signing_configuration(self):
        with mock.patch.dict(os.environ, {"MACOS_NOTARY_PROFILE": "profile"}), self.assertRaisesRegex(ValueError, "requires both"):
            macos.build_installers(self.bundle, "0.1.0", self.output)

    def test_output_inside_bundle_is_rejected_before_native_tools(self):
        with self.assertRaisesRegex(ValueError, "outside the app bundle"):
            macos.build_installers(self.bundle, "0.1.0", self.bundle / "installers")
        self.assertEqual(self.commands, [])

    def test_output_can_share_the_app_parent_without_recursing(self):
        artifacts = macos.build_installers(self.bundle, "0.1.0", self.bundle.parent)
        self.assertTrue(all(artifact.parent == self.bundle.parent for artifact in artifacts))

    def test_versions_are_canonical_and_match_bundle_metadata(self):
        for version in ("1.0", "01.0.0", "0.1.0/../bad", "0.1.0;echo bad", "0.1.0rc1"):
            with self.subTest(version=version), self.assertRaises(ValueError):
                macos.build_installers(self.bundle, version, self.output)
        with self.assertRaisesRegex(ValueError, "CFBundleShortVersionString"):
            macos.build_installers(self.bundle, "0.2.0", self.output)

    def test_foreign_bundle_identity_is_not_packaged(self):
        self.info["CFBundleIdentifier"] = "com.other.application"
        self._write_info()
        with self.assertRaisesRegex(ValueError, "identifier"):
            macos.build_installers(self.bundle, "0.1.0", self.output)

    def test_universal_architecture_is_derived_from_executable(self):
        self.observations["architectures"] = "x86_64 arm64"
        artifacts = macos.build_installers(self.bundle, "0.1.0", self.output)
        self.assertTrue(all("universal2" in artifact.name for artifact in artifacts))
        self.assertEqual(self.observations["distribution"].find("options").get("hostArchitectures"), "arm64,x86_64")

    def test_payload_verifier_rejects_unexpected_install_paths(self):
        package = self.directory / "fixture.pkg"
        package.write_bytes(b"xar!")
        self.observations["unexpected_payload"] = True
        with self.assertRaisesRegex(ValueError, "outside /Applications"):
            macos.verify_package_payload(package, expected_version="0.1.0")

    def test_non_macos_build_is_an_explicit_error(self):
        with mock.patch.object(macos.platform, "system", return_value="Linux"), self.assertRaisesRegex(RuntimeError, "must be built on macOS"):
            macos.build_installers(self.bundle, "0.1.0", self.output)

    def test_dmg_icon_is_applied_to_staging_only(self):
        icon = self.directory / "VirtualKVM.icns"
        icon.write_bytes(b"icns fixture")
        with mock.patch.object(macos.os, "setxattr") as attribute:
            artifacts = macos.build_installers(self.bundle, "0.1.0", self.output, icon=icon)
        self.assertEqual(len(artifacts), 2)
        self.assertEqual(attribute.call_args.args[1], "com.apple.FinderInfo")
        self.assertEqual(attribute.call_args.args[2][8:10], b"\x04\x00")
        self.assertFalse((self.bundle / ".VolumeIcon.icns").exists())


if __name__ == "__main__":
    unittest.main()
