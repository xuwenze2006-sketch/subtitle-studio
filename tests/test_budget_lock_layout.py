"""Offline coverage for request-lock layout; no provider calls or real ledgers."""

import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest

from subtitle_pipeline import cloud_budget as cloud


class BudgetLockLayoutTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.path = self.directory / '费用账本.json'
        self.ledger = cloud.BudgetLedger(self.path)
        self.internal = self.directory / '运行数据' / '请求锁'

    def lock_name(self, request_id, ledger_name='费用账本.json'):
        digest = hashlib.sha256(request_id.encode('utf-8')).hexdigest()
        return f'.{ledger_name}.request-{digest}.lock'

    def must_not_send(self):
        self.fail('A cached or owned request must not be sent again')

    def response(self, label='offline'):
        return cloud.HttpResponse(200, {}, json.dumps({'label': label}).encode('utf-8'))

    def test_new_request_locks_are_internal_while_global_ledger_lock_stays_at_root(self):
        self.ledger.execute('new', 'offline', 1, self.directory / 'new.json', self.response)

        self.assertTrue((self.internal / self.lock_name('new')).is_file())
        self.assertFalse(list(self.directory.glob('.*.request-*.lock')))
        self.assertTrue((self.directory / '费用账本.json.lock').is_file())
        self.assertFalse((self.internal / '费用账本.json.lock').exists())
        self.assertEqual(self.ledger.summary()['spent_cny'], 1)

    def test_new_instance_competes_with_existing_legacy_lock_without_creating_an_alternative(self):
        legacy = self.directory / self.lock_name('inflight')
        entered, release = threading.Event(), threading.Event()
        failures = []

        def legacy_owner():
            try:
                # Reproduce the old resolver by explicitly holding its root path.
                with cloud._file_lock(legacy):
                    entered.set()
                    if not release.wait(5):
                        raise RuntimeError('legacy test owner was not released')
            except Exception as error:
                failures.append(error)

        owner = threading.Thread(target=legacy_owner)
        owner.start()
        try:
            self.assertTrue(entered.wait(3))
            identity = legacy.stat().st_ino
            with self.assertRaises(cloud.SubmissionUnknown):
                cloud.BudgetLedger(self.path).execute(
                    'inflight', 'offline', 1, self.directory / 'inflight.json', self.must_not_send)
            self.assertFalse((self.internal / legacy.name).exists())
            self.assertFalse(self.path.exists())
        finally:
            release.set()
            owner.join(6)
        self.assertFalse(owner.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(legacy.stat().st_ino, identity)

    def test_partial_migration_reuses_successes_without_sending_or_changing_ledger(self):
        self.internal.mkdir(parents=True)
        records = {}
        locks = []
        for request_id, folder in [('root-cached', self.directory), ('internal-cached', self.internal)]:
            raw = self.directory / f'{request_id}.json'
            body = json.dumps({'label': request_id}).encode('utf-8')
            raw.write_bytes(body)
            lock = folder / self.lock_name(request_id)
            lock.write_bytes(b'\0')
            locks.append((lock, lock.stat().st_ino))
            records[request_id] = {
                'provider': 'offline', 'reserved_cny': 1, 'actual_cny': 0.25,
                'raw_path': str(raw), 'raw_sha256': hashlib.sha256(body).hexdigest(),
                'status': 'success',
                'attempts': [{'number': 1, 'status': 'success', 'http_status': 200}],
            }
        original = json.dumps({'version': 1, 'budget_cny': 20, 'stop_cny': 18,
                               'requests': records}, indent=2).encode('utf-8')
        self.path.write_bytes(original)

        for request_id in records:
            result = cloud.BudgetLedger(self.path).execute(
                request_id, 'offline', 1, self.directory / f'{request_id}.json', self.must_not_send)
            self.assertEqual(result, {'label': request_id})
        self.assertEqual(self.path.read_bytes(), original)
        for path, inode in locks:
            self.assertEqual(path.stat().st_ino, inode)
            self.assertEqual(path.read_bytes(), b'\0')
        self.assertFalse((self.internal / self.lock_name('root-cached')).exists())
        self.assertFalse((self.directory / self.lock_name('internal-cached')).exists())

    def test_different_ledger_names_keep_independent_locks_for_the_same_request_id(self):
        first = self.directory / '费用账本.json'
        second = self.directory / '另一账本.json'
        for index, path in enumerate([first, second]):
            ledger = cloud.BudgetLedger(path)
            raw = self.directory / f'raw-{index}.json'
            ledger.execute('same-id', 'offline', 1, raw, lambda: self.response(path.name))
            self.assertEqual(ledger.execute('same-id', 'offline', 1, raw, self.must_not_send),
                             {'label': path.name})
            self.assertTrue((self.directory / (path.name + '.lock')).is_file())
            self.assertTrue((self.internal / self.lock_name('same-id', path.name)).is_file())
        self.assertEqual(len(list(self.internal.glob('*.lock'))), 2)

    def test_lock_file_identity_survives_release_exception_and_cached_reopen(self):
        raw = self.directory / 'raw.json'
        self.ledger.execute('stable', 'offline', 1, raw, self.response)
        request = self.internal / self.lock_name('stable')
        ledger_lock = self.directory / '费用账本.json.lock'
        identities = {path: (path.stat().st_dev, path.stat().st_ino) for path in [request, ledger_lock]}
        for path in identities:
            with self.assertRaisesRegex(RuntimeError, 'local failure'):
                with cloud._file_lock(path) as acquired:
                    self.assertTrue(acquired)
                    raise RuntimeError('local failure')
        self.assertEqual(cloud.BudgetLedger(self.path).execute(
            'stable', 'offline', 1, raw, self.must_not_send), {'label': 'offline'})
        for path, identity in identities.items():
            self.assertEqual((path.stat().st_dev, path.stat().st_ino), identity)
            self.assertEqual(path.read_bytes(), b'\0')

    def test_dangling_legacy_symlink_fails_closed_instead_of_selecting_a_new_lock(self):
        legacy = self.directory / self.lock_name('linked')
        try:
            legacy.symlink_to(self.directory / 'missing-target-parent' / 'lock')
        except OSError as error:
            if os.name == 'nt' and getattr(error, 'winerror', None) == 1314:
                self.skipTest('Windows symlink privilege is unavailable')
            raise
        with self.assertRaises(OSError):
            self.ledger.execute('linked', 'offline', 1, self.directory / 'linked.json', self.must_not_send)
        self.assertFalse(self.path.exists())
        self.assertFalse((self.internal / legacy.name).exists())
        self.assertTrue(legacy.is_symlink())


if __name__ == '__main__':
    unittest.main()
