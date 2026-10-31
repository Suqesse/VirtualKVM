"""Owner approval and immediate video windows through the actual Qt interface."""
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from pathlib import Path
from types import SimpleNamespace
import tempfile
import time
import unittest
from unittest.mock import patch

from PySide6.QtWidgets import QApplication
from common_qt import wait_ms
from virtualkvm.discovery import Device
from virtualkvm.gui import MainWindow
from virtualkvm.pairing import LEGACY_PERMISSIONS


class LanGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def wait_for(self, predicate, timeout=20):
        deadline=time.monotonic()+timeout
        while not predicate() and time.monotonic()<deadline:wait_ms(20)
        self.assertTrue(predicate())

    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory()
        self.root=Path(self.temporary.name)
        self.host=MainWindow(self.root/'host')
        self.host.synthetic.setChecked(True)
        self.viewer=MainWindow(self.root/'viewer')
        self.host.show();self.viewer.show()

    def tearDown(self):
        for window in (self.viewer,self.host):window.close()
        self.wait_for(lambda:self.viewer._shutdown_complete and self.host._shutdown_complete,timeout=30)
        self.temporary.cleanup()

    def test_pick_computer_owner_approves_code_then_real_video_and_input_without_manual_fields(self):
        self.host.host_bind.setText('127.0.0.1')
        self.host._start_host()
        self.wait_for(lambda:self.host._host_running)
        controller=self.host._host
        device=Device('a'*32,'Host test PC','127.0.0.1',controller.server.address[1],controller.fingerprint,True)
        self.viewer.lan_panel.set_devices([device],self.viewer._trust_store)
        self.viewer.lan_panel.list.setCurrentRow(0)
        self.viewer.video_width.setValue(160);self.viewer.video_height.setValue(90)
        self.viewer.frame_rate.setValue(30);self.viewer.encoder.setCurrentText('libx264')
        self.viewer.lan_panel.connect_button.click()
        self.wait_for(lambda:bool(self.host._approval_dialogs))
        self.wait_for(lambda:bool(self.viewer._approval_dialogs))
        dialog=self.host._approval_dialogs[0]
        self.assertIn(dialog.request.code,self.viewer.lan_panel.code.text())
        self.assertFalse(self.viewer.peer_fingerprint.text())
        self.assertFalse(self.viewer.peer_pin.text())
        self.assertFalse(self.viewer.peer_host.text())
        self.assertFalse(self.viewer.video_windows,'No capture before owner approval')
        dialog._allow()
        self.assertFalse(self.viewer.video_windows,'No capture before the viewer verifies the first connection')
        self.viewer._approval_dialogs[0]._allow()
        self.wait_for(lambda:bool(self.viewer.video_windows))
        self.wait_for(lambda:not self.viewer.video_windows[0].canvas._image.isNull())
        self.assertTrue(self.viewer.video_windows[0].isVisible())
        self.assertTrue(self.viewer.control_enabled.isChecked())
        self.assertFalse(self.viewer.send_file_button.isEnabled())
        self.assertEqual(self.viewer._trust_store.state(device),'known')
        self.viewer._viewer.send_event({'type':'key','key':'shift','down':True})
        self.wait_for(lambda:any(event.get('key')=='shift' and event.get('down') for event in controller.native.events))
        self.host._update_stats()
        self.assertTrue(self.host.host_disconnect.isEnabled())
        self.host.host_disconnect.click()
        self.wait_for(lambda:self.viewer._viewer is None)
        self.assertTrue(any(event.get('key')=='shift' and event.get('down') is False for event in controller.native.events))

    def test_windows_open_before_frames_and_missing_video_has_a_visible_reason(self):
        class SilentViewer:
            selected_displays=[0]
            displays=[dict(index=0,name='Host',x=0,y=0,width=1920,height=1080)]
            receivers=[]
            shared_info={'enabled':False}
            client=SimpleNamespace(permissions=LEGACY_PERMISSIONS)
            connected=True
            audio_displays=set()
            def send_event(self,event):pass
            def disconnect(self):self.connected=False
        owner=SilentViewer()
        window=self.viewer
        window._viewer=owner;window._connecting=True;window._requested_displays=[0]
        window._finished('viewer-connect',window._viewer_epoch,owner,None)
        self.assertTrue(window.video_windows[0].isVisible())
        self.assertTrue(window.video_windows[0].canvas._image.isNull())
        self.assertIn('ожидаем',window.status_label.text().lower())
        window._awaiting_video_at=time.monotonic()-21
        window._update_stats()
        self.wait_for(lambda:window._viewer is None)
        self.assertIn('изображение не пришло',window.status_label.text())

    def test_short_pin_is_explained_before_launch_and_new_pin_restores_start(self):
        window=self.host
        window.host_pin.setText('1234')
        self.assertFalse(window.host_start.isEnabled())
        self.assertIn('8',window.host_pin_hint.text())
        window.new_pin.click()
        self.assertTrue(window.host_start.isEnabled())
        self.assertFalse(window.host_pin_hint.text())

    def test_manual_ip_fallback_does_not_require_pin_or_fingerprint(self):
        self.host.host_bind.setText('127.0.0.1');self.host._start_host()
        self.wait_for(lambda:self.host._host_running)
        self.viewer.manual_toggle.setChecked(True)
        self.viewer.peer_host.setText('127.0.0.1')
        self.viewer.peer_port.setValue(self.host._host.server.address[1])
        self.viewer.video_width.setValue(160);self.viewer.video_height.setValue(90)
        self.viewer.frame_rate.setValue(30);self.viewer.encoder.setCurrentText('libx264')
        self.viewer.connect_button.click()
        self.wait_for(lambda:bool(self.host._approval_dialogs) and bool(self.viewer._approval_dialogs))
        self.assertEqual(self.host._approval_dialogs[0].request.code,self.viewer._approval_dialogs[0].request.code)
        self.host._approval_dialogs[0]._allow();self.viewer._approval_dialogs[0]._allow()
        self.wait_for(lambda:bool(self.viewer.video_windows) and not self.viewer.video_windows[0].canvas._image.isNull())
        self.assertFalse(self.viewer.peer_pin.text())
        self.assertFalse(self.viewer.peer_fingerprint.text())
