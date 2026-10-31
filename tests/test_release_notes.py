"""Release-page updates must retain validated, tag-matching native installers."""
import copy
import json
from pathlib import Path
import re
import subprocess
import unittest
from unittest.mock import patch

from scripts import release_notes


class ReleasePageTests(unittest.TestCase):
    def setUp(self):
        self.version = '0.2.0'
        self.source = 'a' * 40
        self.names = release_notes.installer_names(self.version)
        self.assets = [{'name': name} for name in self.names | {'SHA256SUMS', 'installation-checks.json'}]
        shared = dict(install=True, offline_media=True, interface=True, upgrade=True, user_state_preserved=True)
        self.report = {'commit': self.source, 'targets': {
            'VirtualKVM-installer-windows-x64': dict(shared, uninstall=True),
            'VirtualKVM-installer-macos-arm64': dict(shared, dmg_verified=True, dmg_layout=True),
            'VirtualKVM-installer-macos-x86_64': dict(shared, dmg_verified=True, dmg_layout=True),
        }}
        self.checksums = '\n'.join('b' * 64 + '  ' + name for name in sorted(self.names))

    def validate(self, **changes):
        args = dict(version=self.version, assets=self.assets, report=self.report,
                    checksums=self.checksums, source=self.source)
        release_notes.validate_publication(**(args | changes))

    def test_existing_native_reports_and_all_five_installers_are_accepted(self):
        self.validate()

    def test_missing_installer_bad_checksums_and_changed_source_are_rejected(self):
        for changes in ({'assets': self.assets[1:]}, {'source': 'c' * 40},
                        {'checksums': self.checksums + '\n' + self.checksums.splitlines()[0]},
                        {'checksums': self.checksums.replace('b' * 64, 'not-a-sha256')}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.validate(**changes)

    def test_incomplete_or_failed_native_install_checks_cannot_be_promoted(self):
        for target in self.report['targets']:
            for change in ('missing', 'false', 'nonboolean'):
                report = copy.deepcopy(self.report)
                checks = report['targets'][target]
                if change == 'missing':
                    del checks['interface']
                else:
                    checks['interface'] = False if change == 'false' else 1
                with self.subTest(target=target, change=change), self.assertRaises(ValueError):
                    self.validate(report=report)

    def test_download_table_links_follow_the_release_version(self):
        text = release_notes.render('example/VirtualKVM', '1.2.3')
        links = re.findall(r'https://github.com/example/VirtualKVM/releases/download/v1\.2\.3/([^\s)]+)', text)
        installers = {name for name in links if name.endswith(('.exe', '.pkg', '.dmg'))}
        self.assertEqual(installers, release_notes.installer_names('1.2.3'))
        self.assertNotIn('{{', text)
        for repository, version in [('bad owner/repo', '1.2.3'), ('owner/repo', '../bad')]:
            with self.assertRaises(ValueError):
                release_notes.render(repository, version)

    def test_invalid_publication_never_changes_the_live_release_page(self):
        commands = []
        def gh(command, **kwargs):
            commands.append(command)
            if command[2] == 'view':
                return subprocess.CompletedProcess(command, 0, stdout=json.dumps({
                    'tagName': 'v0.2.0', 'isDraft': False, 'assets': self.assets}), stderr='')
            if command[2] == 'download':
                directory = Path(command[command.index('--dir') + 1])
                (directory / 'installation-checks.json').write_text(json.dumps(self.report))
                (directory / 'SHA256SUMS').write_text(self.checksums)
                return subprocess.CompletedProcess(command, 0, stdout='', stderr='')
            self.fail('The release page must not be edited after validation fails')
        with patch.object(release_notes.subprocess, 'run', side_effect=gh), \
             patch.object(release_notes.subprocess, 'check_output', return_value='wrong-commit'):
            with self.assertRaisesRegex(ValueError, 'source commit'):
                release_notes.update_existing('Suqesse/VirtualKVM', '0.2.0', Path('notes.md'))
        self.assertFalse(any(command[2] == 'edit' for command in commands))

    def test_successful_update_preserves_assets_and_marks_the_page_latest(self):
        commands = []
        def gh(command, **kwargs):
            commands.append(command)
            if command[2] == 'view':
                output = json.dumps({'tagName': 'v0.2.0', 'isDraft': False, 'assets': self.assets})
            else:
                output = ''
            if command[2] == 'download':
                directory = Path(command[command.index('--dir') + 1])
                (directory / 'installation-checks.json').write_text(json.dumps(self.report))
                (directory / 'SHA256SUMS').write_text(self.checksums)
            return subprocess.CompletedProcess(command, 0, stdout=output, stderr='')
        with patch.object(release_notes.subprocess, 'run', side_effect=gh), \
             patch.object(release_notes.subprocess, 'check_output', return_value=self.source):
            release_notes.update_existing('Suqesse/VirtualKVM', '0.2.0', Path('notes.md'))
        edits = [command for command in commands if command[2] == 'edit']
        self.assertEqual(len(edits), 1)
        self.assertIn('--latest', edits[0])
        self.assertIn('--prerelease=false', edits[0])
        self.assertFalse(any(command[2] in {'upload', 'delete', 'create'} for command in commands))
