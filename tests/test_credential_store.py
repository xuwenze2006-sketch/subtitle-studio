"""Credential persistence tests use only synthetic keys and temporary stores."""
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import traceback
import unittest
from unittest.mock import patch

from subtitle_pipeline import credential_store as store


class CredentialStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / 'settings' / 'credentials.json'

    def assert_safe_error(self, operation, secret='synthetic-secret-must-not-appear'):
        with self.assertRaises(store.CredentialStoreError) as raised:
            operation()
        rendered = ''.join(traceback.format_exception(raised.exception))
        self.assertNotIn(secret, rendered)

    def test_default_path_is_under_local_appdata(self):
        with patch.dict(os.environ, {'LOCALAPPDATA': str(self.root)}):
            self.assertEqual(store.default_path(), self.root / 'SubtitlePipeline' / 'credentials.json')

    def test_non_windows_is_explicitly_unsupported(self):
        with patch.object(store.sys, 'platform', 'linux'):
            for operation in [lambda: store.read_saved_secrets(self.path),
                              lambda: store.save_secrets({'DEEPSEEK_API_KEY': 'fake-secret'}, self.path)]:
                with self.subTest(operation=operation), self.assertRaisesRegex(store.CredentialStoreError, 'Windows'):
                    operation()
        self.assertFalse(self.path.exists())

    @unittest.skipUnless(sys.platform == 'win32', 'Requires real Windows DPAPI')
    def test_missing_store_returns_empty_without_creating_files(self):
        self.assertEqual(store.read_saved_secrets(self.path), {})
        self.assertFalse(self.path.parent.exists())

    @unittest.skipUnless(sys.platform == 'win32', 'Requires real Windows DPAPI')
    def test_dpapi_round_trip_merges_keys_and_disk_contains_only_ciphertext(self):
        store.save_secrets({'SILICONFLOW_API_KEY': 'fake-siliconflow-first'}, self.path)
        store.save_secrets({'DASHSCOPE_API_KEY': 'fake-dashscope-secret',
                           'SILICONFLOW_API_KEY': 'fake-siliconflow-updated'}, self.path)
        self.assertEqual(store.read_saved_secrets(self.path), {
            'SILICONFLOW_API_KEY': 'fake-siliconflow-updated',
            'DASHSCOPE_API_KEY': 'fake-dashscope-secret',
        })
        envelope = json.loads(self.path.read_text(encoding='utf-8'))
        self.assertEqual(set(envelope), {'version', 'payload'})
        ciphertext = base64.b64decode(envelope['payload'], validate=True)
        self.assertTrue(ciphertext)
        for file in self.path.parent.iterdir():
            disk = file.read_bytes()
            self.assertNotIn(b'fake-siliconflow', disk)
            self.assertNotIn(b'fake-dashscope', disk)
            self.assertNotIn(b'API_KEY', disk)
        self.assertNotIn(b'fake-', ciphertext)

    @unittest.skipUnless(sys.platform == 'win32', 'Requires real Windows DPAPI')
    def test_new_process_can_read_saved_keys(self):
        store.save_secrets({'DEEPSEEK_API_KEY': 'fake-restart-secret'}, self.path)
        child = subprocess.run([
            sys.executable, '-c',
            "from subtitle_pipeline.credential_store import read_saved_secrets; import sys; "
            "assert read_saved_secrets(sys.argv[1]) == {'DEEPSEEK_API_KEY': 'fake-restart-secret'}; print('ok')",
            str(self.path),
        ], cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=15,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertEqual(child.stdout.strip(), 'ok')

    @unittest.skipUnless(sys.platform == 'win32', 'Requires real Windows DPAPI')
    def test_invalid_values_and_unknown_names_are_rejected_without_leaking(self):
        invalid = [None, [], {'UNKNOWN_synthetic-secret-must-not-appear': 'fake-key'},
                   {'DEEPSEEK_API_KEY': None}, {'DEEPSEEK_API_KEY': 123},
                   {'DEEPSEEK_API_KEY': ''}, {'DEEPSEEK_API_KEY': 'x' * 4097}]
        invalid += [{'DEEPSEEK_API_KEY': 'synthetic-secret-must-not-appear' + char}
                    for char in [' ', '\n', '\t', '\x00', '\x7f', '\u0085', '\u200b']]
        for values in invalid:
            with self.subTest(case=invalid.index(values)):
                self.assert_safe_error(lambda: store.save_secrets(values, self.path))
        self.assertFalse(self.path.exists())

    @unittest.skipUnless(sys.platform == 'win32', 'Requires real Windows DPAPI')
    def test_corrupt_envelopes_raise_safe_errors(self):
        self.path.parent.mkdir()
        corrupt = ['synthetic-secret-must-not-appear', '{}', '[]',
                   json.dumps({'version': 999, 'payload': 'YWJj'}),
                   json.dumps({'version': True, 'payload': 'YWJj'}),
                   json.dumps({'version': 1, 'payload': '%%%'}),
                   json.dumps({'version': 1, 'payload': ''}),
                   json.dumps({'version': 1, 'payload': 'YWJj'}),
                   json.dumps({'version': 1, 'payload': 3}),
                   json.dumps({'version': 1, 'payload': 'YWJj', 'plaintext': 'synthetic-secret-must-not-appear'})]
        for index, content in enumerate(corrupt):
            with self.subTest(index=index):
                self.path.write_text(content, encoding='utf-8')
                self.assert_safe_error(lambda: store.read_saved_secrets(self.path))

    @unittest.skipUnless(sys.platform == 'win32', 'Requires real Windows DPAPI')
    def test_decrypted_payload_must_have_supported_valid_keys(self):
        store.save_secrets({'DEEPSEEK_API_KEY': 'fake-original'}, self.path)
        payloads = [b'not-json-synthetic-secret-must-not-appear', b'[]',
                    b'{"OTHER":"synthetic-secret-must-not-appear"}',
                    b'{"DEEPSEEK_API_KEY":"synthetic-secret-must-not-appear\\n"}']
        for payload in payloads:
            with self.subTest(payload_index=payloads.index(payload)), patch.object(store, '_unprotect', return_value=payload):
                self.assert_safe_error(lambda: store.read_saved_secrets(self.path))

    @unittest.skipUnless(sys.platform == 'win32', 'Requires real Windows DPAPI')
    def test_tampered_ciphertext_is_rejected(self):
        store.save_secrets({'DEEPSEEK_API_KEY': 'fake-original'}, self.path)
        envelope = json.loads(self.path.read_text(encoding='utf-8'))
        ciphertext = bytearray(base64.b64decode(envelope['payload']))
        ciphertext[-1] ^= 0xFF
        envelope['payload'] = base64.b64encode(ciphertext).decode('ascii')
        self.path.write_text(json.dumps(envelope), encoding='utf-8')
        self.assert_safe_error(lambda: store.read_saved_secrets(self.path))

    @unittest.skipUnless(sys.platform == 'win32', 'Requires real Windows DPAPI')
    def test_corrupt_existing_file_is_not_silently_replaced(self):
        self.path.parent.mkdir()
        self.path.write_text('broken', encoding='utf-8')
        self.assert_safe_error(lambda: store.save_secrets({'DEEPSEEK_API_KEY': 'fake-new'}, self.path))
        self.assertEqual(self.path.read_text(encoding='utf-8'), 'broken')

    @unittest.skipUnless(sys.platform == 'win32', 'Requires real Windows DPAPI')
    def test_dpapi_failures_do_not_expose_provider_secrets(self):
        store.save_secrets({'DEEPSEEK_API_KEY': 'fake-original'}, self.path)
        for method, operation in [
            ('_protect', lambda: store.save_secrets({'DEEPSEEK_API_KEY': 'fake-new'}, self.path)),
            ('_unprotect', lambda: store.read_saved_secrets(self.path)),
        ]:
            with self.subTest(method=method), patch.object(store, method, side_effect=RuntimeError('synthetic-secret-must-not-appear')):
                self.assert_safe_error(operation)
        self.assertEqual(store.read_saved_secrets(self.path), {'DEEPSEEK_API_KEY': 'fake-original'})

    @unittest.skipUnless(sys.platform == 'win32', 'Requires real Windows DPAPI')
    def test_replace_failure_preserves_previous_keys_and_removes_temporary_file(self):
        store.save_secrets({'DEEPSEEK_API_KEY': 'fake-original'}, self.path)
        before = set(self.path.parent.iterdir())
        with patch.object(store.os, 'replace', side_effect=OSError('synthetic-secret-must-not-appear')):
            self.assert_safe_error(lambda: store.save_secrets({'DEEPSEEK_API_KEY': 'fake-new'}, self.path))
        self.assertEqual(store.read_saved_secrets(self.path), {'DEEPSEEK_API_KEY': 'fake-original'})
        self.assertEqual(set(self.path.parent.iterdir()), before)

    @unittest.skipUnless(sys.platform == 'win32', 'Requires real Windows DPAPI')
    def test_save_does_not_modify_environment(self):
        with patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'fake-environment-key'}):
            store.save_secrets({'DEEPSEEK_API_KEY': 'fake-saved-key'}, self.path)
            self.assertEqual(os.environ['DEEPSEEK_API_KEY'], 'fake-environment-key')

    @unittest.skipUnless(sys.platform == 'win32', 'Requires real Windows DPAPI')
    def test_parallel_process_updates_do_not_lose_other_provider_keys(self):
        script = '''
import sys, time
from pathlib import Path
from subtitle_pipeline import credential_store as store
path, barrier, name = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
real_protect = store._protect
def delayed_protect(payload):
    time.sleep(0.25)
    return real_protect(payload)
store._protect = delayed_protect
barrier.with_name(name + '.ready').write_text('ready')
deadline = time.monotonic() + 10
while not barrier.exists():
    if time.monotonic() > deadline:
        raise RuntimeError('Test barrier timeout')
    time.sleep(0.02)
store.save_secrets({name: 'fake-parallel-' + name}, path)
'''
        names = ('SILICONFLOW_API_KEY', 'DASHSCOPE_API_KEY', 'DEEPSEEK_API_KEY')
        barrier = self.root / 'start'
        children = []
        try:
            for name in names:
                children.append(subprocess.Popen(
                    [sys.executable, '-c', script, str(self.path), str(barrier), name],
                    cwd=Path(__file__).resolve().parents[1], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)))
            deadline = time.monotonic() + 10
            while not all((self.root / (name + '.ready')).exists() for name in names):
                self.assertLess(time.monotonic(), deadline, 'Test subprocesses failed to start')
                time.sleep(0.02)
            barrier.write_text('start')
            for child in children:
                _, stderr = child.communicate(timeout=15)
                self.assertEqual(child.returncode, 0, stderr)
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                child.communicate()
        self.assertEqual(store.read_saved_secrets(self.path), {
            'SILICONFLOW_API_KEY': 'fake-parallel-SILICONFLOW_API_KEY',
            'DASHSCOPE_API_KEY': 'fake-parallel-DASHSCOPE_API_KEY',
            'DEEPSEEK_API_KEY': 'fake-parallel-DEEPSEEK_API_KEY',
        })


if __name__ == '__main__':
    unittest.main()
