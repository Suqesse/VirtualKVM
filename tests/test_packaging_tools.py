import os
from pathlib import Path
import plistlib
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from scripts.icons import generate_icons
from scripts.package import configure_macos_privacy
from virtualkvm.media import FFmpegLocator, _popen_options


class PackagingToolsTests(unittest.TestCase):
    def test_multiresolution_ico_contains_valid_png_payloads(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = generate_icons(Path(__file__).resolve().parents[1] / 'docs/assets/logo.svg', Path(temporary))
            data = result['ico'].read_bytes()
            self.assertEqual(struct.unpack('<HHH', data[:6]), (0, 1, 7))
            for index in range(7):
                width,height,_,_,planes,bits,length,offset=struct.unpack('<BBBBHHII', data[6+16*index:22+16*index])
                self.assertEqual((planes,bits),(1,32))
                image=data[offset:offset+length]
                self.assertTrue(image.startswith(b'\x89PNG\r\n\x1a\n'))
                self.assertEqual(struct.unpack('>II',image[16:24]),(width or 256,height or 256))

    def test_macos_metadata_is_written_before_bundle_creation(self):
        with tempfile.TemporaryDirectory() as temporary:
            spec=Path(temporary)/'app.spec'
            spec.write_text("app=BUNDLE('app', info_plist={'Old':'value'})\n")
            configure_macos_privacy(spec,'1.2.3')
            captured={}
            exec(compile(spec.read_text(),str(spec),'exec'),{'BUNDLE':lambda *a,**kw:captured.update(kw)})
            self.assertEqual(captured['info_plist']['CFBundleShortVersionString'],'1.2.3')
            self.assertIn('NSMicrophoneUsageDescription',captured['info_plist'])

    def test_media_subprocess_restores_original_loader_environment_only_when_frozen(self):
        values={'LD_LIBRARY_PATH':'/bundle','LD_LIBRARY_PATH_ORIG':'/original','DYLD_LIBRARY_PATH':'/bundle'}
        with mock.patch.dict(os.environ,values,clear=True), mock.patch.object(sys,'frozen',True,create=True):
            options=_popen_options()
            self.assertEqual(options['env']['LD_LIBRARY_PATH'],'/original')
            self.assertNotIn('DYLD_LIBRARY_PATH',options['env'])
        with mock.patch.object(sys,'frozen',False,create=True):
            self.assertNotIn('env',_popen_options())

    def test_chocolatey_launcher_is_resolved_to_real_media_executable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)/'Chocolatey'
            shim=root/'bin/ffmpeg.exe'; shim.parent.mkdir(parents=True);shim.write_bytes(b'shim')
            binary=root/'lib/ffmpeg-full/tools/native/bin/ffmpeg.exe'
            binary.parent.mkdir(parents=True);binary.write_bytes(b'MZpayload')
            with mock.patch('virtualkvm.media.platform.system',return_value='Windows'), mock.patch('virtualkvm.media.shutil.which',return_value=str(shim)):
                self.assertEqual(FFmpegLocator._locate('ffmpeg.exe'),str(binary))

    def test_ci_command_failure_retains_exit_status_and_annotation(self):
        root=Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            result=subprocess.run([sys.executable,str(root/'scripts/ci_run.py'),'--log',str(Path(temporary)/'run.log'),'--',sys.executable,'-c','print("failed check");raise SystemExit(7)'],capture_output=True,text=True)
            self.assertEqual(result.returncode,7)
            self.assertIn('::error title=Native build check failed::',result.stdout)


if __name__=='__main__':
    unittest.main()
