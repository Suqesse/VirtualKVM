"""Discovery data is untrusted; pairing identities are remembered without PINs."""
import json
import ipaddress
from pathlib import Path
import socket
import tempfile
from types import SimpleNamespace
import unittest

from virtualkvm.discovery import Device, DiscoveryService, TrustStore, decode_device, device_identifier


class DiscoveryTests(unittest.TestCase):
    def info(self, **changes):
        values = dict(port=24800, properties={b'id':b'a'*32,b'name':'Рабочий ПК'.encode(),b'fingerprint':b'b'*64,b'quick':b'1'},
                      parsed_addresses=lambda:['192.168.1.20'])
        values.update(changes)
        return SimpleNamespace(**values)

    def test_unicode_computer_name_and_public_identity_are_decoded(self):
        device=decode_device(self.info())
        self.assertEqual(device.name,'Рабочий ПК')
        self.assertEqual(device.address,'192.168.1.20')
        self.assertTrue(device.quick_pairing)

    def test_direct_lan_address_is_preferred_over_an_unreachable_vpn_address(self):
        info=self.info(parsed_addresses=lambda:['10.50.0.20','192.168.1.20'])
        device=decode_device(info,[ipaddress.ip_network('192.168.1.0/24')])
        self.assertEqual(device.address,'192.168.1.20')

    def test_malformed_txt_urls_ports_and_control_characters_are_rejected(self):
        for info in (self.info(port=0),self.info(port=True),self.info(port=70000),
                     self.info(parsed_addresses=lambda:['https://malicious.invalid']),
                     self.info(properties={b'id':b'a'*32,b'name':b'bad\nname',b'fingerprint':b'b'*64}),
                     self.info(properties={b'id':b'bad',b'name':b'PC',b'fingerprint':b'b'*64}),
                     self.info(properties={b'id':b'a'*32,b'name':b'PC',b'fingerprint':b'bad'})):
            with self.subTest(info=info):self.assertIsNone(decode_device(info))

    def test_device_identity_persists_without_reusing_an_invalid_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            self.assertEqual(device_identifier(temporary),device_identifier(temporary))
            (Path(temporary)/'device-id').write_text('invalid')
            with self.assertRaises(ValueError):device_identifier(temporary)

    def test_known_identity_and_changed_certificate_are_distinguished(self):
        with tempfile.TemporaryDirectory() as temporary:
            store=TrustStore(temporary)
            device=decode_device(self.info())
            self.assertEqual(store.state(device),'new')
            store.remember(device)
            self.assertEqual(TrustStore(temporary).state(device),'known')
            changed=Device(device.identifier,device.name,device.address,device.port,'c'*64,True)
            self.assertEqual(store.state(changed),'changed')
            saved=json.loads(store.path.read_text())
            self.assertEqual(saved,{device.identifier:device.fingerprint})
            self.assertNotIn('pin',store.path.read_text())

    def test_discovery_ignores_self_removes_stopped_hosts_and_never_trusts_advertisements(self):
        snapshots=[]
        service=DiscoveryService('c'*32,'Self',['127.0.0.1'],24800,'d'*64,snapshots.append)
        info=self.info()
        resolver=SimpleNamespace(get_service_info=lambda *args,**kwargs:info)
        service.add_service(resolver,'_virtualkvm._tcp.local.','peer')
        self.assertEqual(len(snapshots[-1]),1)
        info.port=0
        service.update_service(resolver,'_virtualkvm._tcp.local.','peer')
        self.assertEqual(snapshots[-1],[])
        info.port=24800;info.properties[b'id']=b'c'*32
        service.add_service(resolver,'_virtualkvm._tcp.local.','self')
        self.assertEqual(snapshots[-1],[])
