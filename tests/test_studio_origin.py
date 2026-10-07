"""Stable browser origins, including Windows exclusive loopback binding."""
import errno
from http.server import BaseHTTPRequestHandler
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from subtitle_pipeline import studio_origin
from subtitle_pipeline.studio_http import StudioHTTPServer


class StudioOriginTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path=Path(self.temporary.name)/'browser-port.json'

    def test_default_launch_reuses_successful_port_after_server_restart(self):
        server=Mock(server_port=43210)
        factory=Mock(return_value=server)
        self.assertEqual(studio_origin.create_server(factory,object,self.path),(server,''))
        factory.assert_called_once_with(('127.0.0.1',0),object)
        self.assertEqual(json.loads(self.path.read_text()),{'port':43210})
        factory.reset_mock()
        studio_origin.create_server(factory,object,self.path)
        factory.assert_called_once_with(('127.0.0.1',43210),object)

    def test_occupied_preferred_port_falls_back_and_explains_storage_scope(self):
        self.path.write_text('{"port":43210}')
        server=Mock(server_port=43211)
        factory=Mock(side_effect=[OSError(errno.EADDRINUSE,'owned fixture'),server])
        result,warning=studio_origin.create_server(factory,object,self.path)
        self.assertIs(result,server)
        self.assertIn('草稿',warning)
        self.assertEqual(factory.call_args_list[1].args,(('127.0.0.1',0),object))
        self.assertEqual(json.loads(self.path.read_text()),{'port':43211})

    def test_explicit_port_is_honored_and_never_changes_default_origin(self):
        self.path.write_text('{"port":43210}')
        factory=Mock(return_value=Mock(server_port=43212))
        studio_origin.create_server(factory,object,self.path,port=43212)
        factory.assert_called_once_with(('127.0.0.1',43212),object)
        self.assertEqual(json.loads(self.path.read_text()),{'port':43210})
        factory.side_effect=OSError(errno.EADDRINUSE,'explicit port occupied')
        with self.assertRaises(OSError):
            studio_origin.create_server(factory,object,self.path,port=43212)

    def test_invalid_records_use_fresh_port_and_unexpected_bind_errors_propagate(self):
        for content in ('bad JSON','{"port":true}','{"port":65536}','[]'):
            with self.subTest(content=content):
                self.path.write_text(content)
                factory=Mock(return_value=Mock(server_port=43210))
                studio_origin.create_server(factory,object,self.path)
                factory.assert_called_once_with(('127.0.0.1',0),object)
        self.path.write_text('{"port":43210}')
        factory=Mock(side_effect=OSError(errno.ENOMEM,'out of memory'))
        with self.assertRaises(OSError):
            studio_origin.create_server(factory,object,self.path)
        self.assertEqual(factory.call_count,1)

    def test_failed_port_record_does_not_hide_usable_server(self):
        server=Mock(server_port=43210)
        with patch.object(studio_origin,'atomic_json',side_effect=OSError('write denied')):
            result,warning=studio_origin.create_server(Mock(return_value=server),object,self.path)
        self.assertIs(result,server)
        self.assertIn('端口',warning)

    @unittest.skipUnless(os.name=='nt','Windows bind semantics')
    def test_saved_port_held_by_real_studio_listener_falls_back(self):
        first=StudioHTTPServer(('127.0.0.1',0),BaseHTTPRequestHandler)
        self.addCleanup(first.server_close)
        self.path.write_text(json.dumps({'port':first.server_port}))
        second,warning=studio_origin.create_server(StudioHTTPServer,BaseHTTPRequestHandler,self.path)
        self.addCleanup(second.server_close)
        self.assertNotEqual(second.server_port,first.server_port)
        self.assertIn('草稿',warning)
