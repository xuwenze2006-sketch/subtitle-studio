"""Offline main() launch selection persistence; no user state or listening port."""
from contextlib import ExitStack, nullcontext, redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from subtitle_pipeline import studio


class StudioStartupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='studio-startup-state-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.data = self.root / 'isolated-runtime'
        self.data.mkdir()
        self.store = self.data / 'studio.json'
        static = self.root / 'frontend' / 'dist'
        static.mkdir(parents=True)
        (static / 'index.html').write_text('Synthetic frontend fixture', encoding='utf-8')
        self.old = {name: str(self.root / ('old-' + name)) for name in ('source', 'campaign', 'baseline')}
        self.new = {name: str(self.root / ('new-' + name)) for name in ('source', 'campaign', 'baseline')}
        for selection in (self.old, self.new):
            Path(selection['campaign']).mkdir()
            Path(selection['source']).write_text('Synthetic source', encoding='utf-8')
            Path(selection['baseline']).write_text('Synthetic baseline', encoding='utf-8')
        self.history = {'b' * 64: {'action': 'prepare', 'status': 'prepared', 'busy': False,
            'message': 'Earlier completed task', 'total': 2, 'recognized': 2, 'translated': 0,
            'logs': ['Earlier task log'], 'interrupted': False}}
        self.saved = {**self.old, 'recent': [
            {'path': self.old['campaign'], 'source': self.old['source'], 'baseline': self.old['baseline'],
             'title': 'Older project', 'status': 'prepared', 'created': 11, 'updated': 22}],
            'last_runs': deepcopy(self.history)}
        self.store.write_text(json.dumps(self.saved, ensure_ascii=False), encoding='utf-8')

    def launch(self, args, *, existing=None, bind_error=None, fail_state_write=False):
        outcome = {'events': [], 'controller': None, 'persist_calls': 0, 'served': False, 'closed': False}
        original_persist = studio.StudioController._persist
        original_atomic = studio.atomic_json

        def persist(controller):
            outcome['events'].append('persist')
            outcome['persist_calls'] += 1
            return original_persist(controller)

        def atomic(path, value):
            if fail_state_write and Path(path) == self.store:
                raise OSError('Injected startup state write failure')
            if Path(path) == self.data / 'runtime.json':
                outcome['events'].append('runtime')
            return original_atomic(path, value)

        class Server:
            server_port = 43210
            def serve_forever(inner, **_kwargs):
                outcome['events'].append('serve')
                outcome['served'] = True
                outcome['state_at_serve'] = outcome['controller'].state()
                outcome['stored_at_serve'] = json.loads(self.store.read_text(encoding='utf-8'))
            def server_close(inner):
                outcome['closed'] = True

        def bind(*_args, **_kwargs):
            outcome['events'].append('bind')
            if bind_error is not None:
                raise bind_error
            return Server()

        def handler(controller, *_args):
            outcome['controller'] = controller
            return object

        with ExitStack() as stack:
            stack.enter_context(patch.object(studio, 'ROOT', self.root))
            stack.enter_context(patch.object(studio, 'data_directory', return_value=self.data))
            stack.enter_context(patch.object(studio, 'ProjectLock', side_effect=lambda _path: nullcontext()))
            stack.enter_context(patch.object(studio, 'running_instance', return_value=existing))
            stack.enter_context(patch.object(studio, 'ThreadingHTTPServer', side_effect=bind))
            stack.enter_context(patch.object(studio, 'make_handler', side_effect=handler))
            stack.enter_context(patch.object(studio, 'account_environment', return_value={}))
            stack.enter_context(patch.object(studio, 'encrypted_names', return_value=set()))
            stack.enter_context(patch.object(studio, 'engine_available', return_value=False))
            stack.enter_context(patch.object(studio, 'open_desktop_window',
                side_effect=AssertionError('This test must not launch a browser')))
            stack.enter_context(patch.object(studio.threading, 'Thread', return_value=Mock()))
            stack.enter_context(patch.object(studio.StudioController, '_persist', persist))
            stack.enter_context(patch.object(studio, 'atomic_json', side_effect=atomic))
            stack.enter_context(redirect_stdout(io.StringIO()))
            if bind_error is not None:
                with self.assertRaises(OSError) as caught:
                    studio.main(['--no-browser', *args])
                self.assertIs(caught.exception, bind_error)
            else:
                self.assertEqual(studio.main(['--no-browser', *args]), 0)
        return outcome

    def explicit_args(self):
        return [value for name, path in self.new.items() for value in ('--' + name, path)]

    def test_new_cli_selection_is_saved_after_bind_before_serve_and_default_reopens_it(self):
        result = self.launch(self.explicit_args())
        self.assertEqual(result['events'], ['bind', 'runtime', 'persist', 'serve'])
        self.assertTrue(result['closed'])
        for name, path in self.new.items():
            self.assertEqual(result['stored_at_serve'][name], path)
        with patch.object(studio, 'data_directory', return_value=self.data):
            reopened = studio.StudioController()
        for name, path in self.new.items():
            self.assertEqual(getattr(reopened, name), path)

    def test_startup_save_preserves_existing_recent_projects_and_run_history(self):
        result = self.launch(self.explicit_args())
        self.assertEqual(result['persist_calls'], 1)
        saved = json.loads(self.store.read_text(encoding='utf-8'))
        self.assertEqual(saved['last_runs'], self.history)
        self.assertEqual(saved['recent'][0]['path'], self.new['campaign'])
        self.assertIn(self.saved['recent'][0], saved['recent'])

    def test_each_nonempty_cli_field_persists_with_other_saved_fields_preserved(self):
        for name in self.new:
            with self.subTest(field=name):
                self.store.write_text(json.dumps(self.saved), encoding='utf-8')
                result = self.launch(['--' + name, self.new[name]])
                self.assertEqual(result['persist_calls'], 1)
                expected = {**self.old, name: self.new[name]}
                for field, value in expected.items():
                    self.assertEqual(result['stored_at_serve'][field], value)

    def test_no_nonempty_cli_override_does_not_write_existing_state(self):
        before = self.store.read_bytes()
        for args in ([], ['--source', '', '--campaign', '', '--baseline', '']):
            with self.subTest(args=args):
                result = self.launch(args)
                self.assertEqual(result['persist_calls'], 0)
                self.assertTrue(result['served'])
                self.assertEqual(self.store.read_bytes(), before)

    def test_reused_live_instance_does_not_persist_cli_selection(self):
        before = self.store.read_bytes()
        existing = {'url': 'http://127.0.0.1:43210/#token=synthetic-existing-instance', 'pid': 4242}
        result = self.launch(self.explicit_args(), existing=existing)
        self.assertEqual(result['events'], [])
        self.assertEqual(result['persist_calls'], 0)
        self.assertIsNone(result['controller'])
        self.assertEqual(self.store.read_bytes(), before)

    def test_failed_server_bind_does_not_persist_cli_selection(self):
        before = self.store.read_bytes()
        result = self.launch(self.explicit_args(), bind_error=OSError('Port unavailable'))
        self.assertEqual(result['events'], ['bind'])
        self.assertEqual(result['persist_calls'], 0)
        self.assertFalse(result['served'])
        self.assertEqual(self.store.read_bytes(), before)

    def test_persistence_failure_keeps_server_usable_and_exposes_warning(self):
        before = self.store.read_bytes()
        result = self.launch(self.explicit_args(), fail_state_write=True)
        self.assertTrue(result['served'])
        self.assertTrue(result['closed'])
        self.assertEqual(result['persist_calls'], 1)
        self.assertTrue(result['state_at_serve']['persistence_warning'])
        self.assertFalse(result['state_at_serve']['job']['busy'])
        for name, path in self.new.items():
            self.assertEqual(result['state_at_serve'][name], path)
        self.assertEqual(self.store.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
