"""Evidence must describe the state bytes that passed verification, offline only."""
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from subtitle_pipeline import integrity as i
from tests import test_integrity_cancel as fixture


class SampleEvidenceSnapshotTests(unittest.TestCase):
    setUp = fixture.IntegrityCancellationTests.setUp
    part_folder = fixture.IntegrityCancellationTests.part_folder
    sample = fixture.IntegrityCancellationTests.sample

    def test_state_replaced_before_final_hash_never_gets_unverified_proof(self):
        original = i._evidence_sha256
        for valid_replacement in (False, True):
            with self.subTest(valid_replacement=valid_replacement):
                self.state_path.write_text(json.dumps(self.state, ensure_ascii=False), encoding='utf-8')

                def hashed(path, stop):
                    if Path(path) == self.state_path:
                        replacement = dict(self.state, snapshot_revision=2)
                        if not valid_replacement:
                            replacement['status'] = 'asr_incomplete'
                        self.state_path.write_text(json.dumps(replacement, ensure_ascii=False), encoding='utf-8')
                    return original(path, stop)

                with patch.object(i, '_evidence_sha256', side_effect=hashed):
                    with self.assertRaisesRegex(ValueError, '状态.*改变'):
                        self.sample()

    def test_state_replaced_after_initial_read_is_detected(self):
        original = json.loads

        def parsed(*args, **kwargs):
            state = original(*args, **kwargs)
            changed = dict(self.state, status='asr_incomplete')
            self.state_path.write_text(json.dumps(changed, ensure_ascii=False), encoding='utf-8')
            return state

        with patch.object(i.json, 'loads', side_effect=parsed):
            with self.assertRaises(ValueError):
                self.sample()

    def test_unchanged_proof_hashes_exact_utf8_lf_or_crlf_bytes(self):
        for newline in ('\n', '\r\n'):
            for stop in (None, self.stop):
                with self.subTest(newline=repr(newline), cancellable=stop is not None):
                    raw = json.dumps(self.state, ensure_ascii=False, indent=2).replace('\n', newline).encode('utf-8')
                    self.state_path.write_bytes(raw)
                    proof = self.sample(stop=stop)
                    self.assertEqual(proof['sha256'], hashlib.sha256(raw).hexdigest())
                    self.assertEqual(proof['source_sha256'], self.state['identity']['source']['sha256'])
                    self.assertEqual(self.state_path.read_bytes(), raw)


if __name__ == '__main__':
    unittest.main()
