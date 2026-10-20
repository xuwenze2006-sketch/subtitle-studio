"""Synthetic keys must fail locally without poisoning paid request recovery."""
from datetime import date
import http.client
import io
import json
from pathlib import Path
import sys
import tempfile
import traceback
import unittest
from unittest.mock import patch
import wave

from subtitle_pipeline import azure_asr, cloud_settings, credential_store, deepseek_translate, qwen_asr, siliconflow_pilot
from subtitle_pipeline.cloud_budget import BudgetLedger


class HttpResult(io.BytesIO):
    status = 200
    headers = {}


class ApiKeyPreflightTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.audio = self.root / 'one-second.wav'
        with wave.open(str(self.audio), 'wb') as wav:
            wav.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
            wav.writeframes(b'\0\0' * 16000)
        # Use urllib's real header preparation, without proxy/registry discovery
        # or any possibility of opening a remote connection.
        proxies = patch('urllib.request.getproxies', return_value={})
        proxies.start()
        self.addCleanup(proxies.stop)
        network = patch.object(http.client.HTTPSConnection, 'connect',
                               side_effect=AssertionError('Real connections are disabled'))
        self.connect = network.start()
        self.addCleanup(network.stop)

    def invoke(self, provider, key, ledger, folder):
        if provider == 'qwen':
            return qwen_asr.transcribe(self.audio, key=key, ledger=ledger,
                                      request_id='one-second', raw_path=None)
        if provider == 'azure':
            return azure_asr.transcribe(self.audio,
                endpoint='https://fixture.cognitiveservices.azure.com', key=key,
                ledger=ledger, request_id='one-second', raw_path=None, hourly_rate_cny=10)
        if provider == 'deepseek':
            return deepseek_translate.translate_texts(['hello'], 'en', 'zh-CN',
                folder / 'translations.json', ledger=ledger, key=key)
        return siliconflow_pilot.transcribe(self.audio, key=key, ledger=ledger,
                                           price_per_second=.00022)

    def response(self, provider):
        if provider == 'qwen':
            payload = {'output': {'text': 'Hello.', 'sentence': {
                'text': 'Hello.', 'begin_time': 0, 'end_time': 800,
                'sentence_id': 1, 'sentence_end': True, 'channel_id': 0}},
                'usage': {'duration': 1, 'input_tokens': 1, 'output_tokens': 1, 'total_tokens': 2}}
        elif provider == 'azure':
            payload = {'durationMilliseconds': 1000, 'phrases': []}
        elif provider == 'deepseek':
            payload = {'choices': [{'finish_reason': 'stop', 'message': {'content':
                json.dumps({'translations': [{'id': 0, 'zh': 'translated'}]})}}]}
        else:
            payload = {'text': 'Hello.'}
        return HttpResult(json.dumps(payload).encode('utf-8'))

    def assert_preflight_and_recovery(self, provider):
        invalid = ['synthetic-secret-' + char for char in
                   ('\u4e2d', '\U0001f600', '\x80', ' ', '\t', '\r', '\n', '\x00', '\x7f')]
        for index, key in enumerate(invalid):
            with self.subTest(provider=provider, invalid_index=index):
                self.connect.reset_mock()
                folder = self.root / provider / str(index)
                ledger = BudgetLedger(folder / 'ledger.json')
                with patch.object(ledger, 'execute', wraps=ledger.execute) as execute:
                    try:
                        self.invoke(provider, key, ledger, folder)
                    except Exception as error:
                        expected_error = deepseek_translate.TranslationError if provider == 'deepseek' else ValueError
                        self.assertIsInstance(error, expected_error,
                            'Unusable keys must be local validation failures, never uncertain paid submissions')
                        self.assertNotIn(key, ''.join(traceback.format_exception(error)))
                    else:
                        self.fail('An unusable key was accepted')
                    execute.assert_not_called()
                self.assertEqual(ledger.summary()['requests'], {})
                self.assertFalse(ledger.path.exists())
                self.assertEqual(self.connect.call_count, 0)
                # Correcting a local typo must leave this exact request usable.
                with patch('urllib.request.OpenerDirector.open', return_value=self.response(provider)):
                    self.invoke(provider, 'corrected-opaque-key-!$%&*+._~', ledger, folder)
                records = list(ledger.summary()['requests'].values())
                self.assertEqual(len(records), 1)
                self.assertEqual(records[0]['status'], 'success')
                self.assertEqual(len(records[0]['attempts']), 1)
                self.assertEqual(self.connect.call_count, 0)

    def test_qwen_invalid_key_does_not_poison_paid_request(self):
        self.assert_preflight_and_recovery('qwen')

    def test_azure_invalid_key_does_not_poison_paid_request(self):
        self.assert_preflight_and_recovery('azure')

    def test_deepseek_invalid_key_does_not_poison_paid_request(self):
        self.assert_preflight_and_recovery('deepseek')

    def test_siliconflow_invalid_key_does_not_poison_paid_request(self):
        self.assert_preflight_and_recovery('siliconflow')

    @unittest.skipUnless(sys.platform == 'win32', 'Requires the supported Windows credential entry point')
    def test_saved_key_rejects_unencodable_text_before_encryption_or_files(self):
        for name in credential_store.SUPPORTED_KEYS:
            with self.subTest(provider=name):
                key = 'synthetic-secret-' + chr(0x4e2d)
                path = self.root / name / 'credentials.json'
                with patch.object(credential_store, '_protect', return_value=b'fake-ciphertext') as protect:
                    with self.assertRaises(credential_store.CredentialStoreError) as raised:
                        credential_store.save_secrets({name: key}, path)
                    self.assertNotIn(key, ''.join(traceback.format_exception(raised.exception)))
                    protect.assert_not_called()
                self.assertFalse(path.parent.exists())

    @unittest.skipUnless(sys.platform == 'win32', 'Requires real Windows DPAPI')
    def test_legacy_unencodable_saved_key_can_be_merged_then_corrected(self):
        path = self.root / 'legacy' / 'credentials.json'
        path.parent.mkdir()
        legacy = {'DASHSCOPE_API_KEY': 'legacy-synthetic-' + chr(0x4e2d),
                  'DEEPSEEK_API_KEY': 'existing-translation-fixture'}
        # Reproduce a store written by the old version, using real DPAPI and
        # exclusively synthetic credentials in the temporary test directory.
        credential_store._write_unlocked(path, legacy)
        try:
            loaded = credential_store.read_saved_secrets(path)
        except credential_store.CredentialStoreError:
            self.fail('An old untransmittable key must remain readable so it can be corrected')
        self.assertEqual(loaded, legacy)
        pricing = {'CLOUD_PRICING_VERIFIED_ON': '2026-10-20',
                   'CLOUD_PRICING_REFERENCE': 'offline fixture'}
        with self.assertRaises(cloud_settings.ConfigurationRequired):
            cloud_settings.load_settings({**loaded, **pricing}, today=date(2026, 10, 20))
        credential_store.save_secrets({'SILICONFLOW_API_KEY': 'new-siliconflow-fixture'}, path)
        self.assertEqual(credential_store.read_saved_secrets(path), {
            **legacy, 'SILICONFLOW_API_KEY': 'new-siliconflow-fixture'})
        credential_store.save_secrets({'DASHSCOPE_API_KEY': 'corrected-asr-fixture'}, path)
        loaded = credential_store.read_saved_secrets(path)
        self.assertEqual(loaded, {'DASHSCOPE_API_KEY': 'corrected-asr-fixture',
            'DEEPSEEK_API_KEY': 'existing-translation-fixture',
            'SILICONFLOW_API_KEY': 'new-siliconflow-fixture'})
        settings = cloud_settings.load_settings({**loaded, **pricing}, today=date(2026, 10, 20))
        self.assertEqual(settings.asr_key, 'corrected-asr-fixture')
        self.assertEqual(settings.deepseek_key, 'existing-translation-fixture')

    def test_settings_reject_unencodable_keys_before_reporting_readiness(self):
        values = {'DASHSCOPE_API_KEY': 'fixture-asr', 'DEEPSEEK_API_KEY': 'fixture-translation',
                  'CLOUD_PRICING_VERIFIED_ON': '2026-10-20', 'CLOUD_PRICING_REFERENCE': 'offline fixture'}
        for name in ('DASHSCOPE_API_KEY', 'DEEPSEEK_API_KEY'):
            with self.subTest(provider=name), self.assertRaises(cloud_settings.ConfigurationRequired):
                cloud_settings.load_settings({**values, name: 'synthetic-' + chr(0x4e2d)},
                                             today=date(2026, 10, 20))
        siliconflow = {'SILICONFLOW_API_KEY': 'synthetic-' + chr(0x4e2d),
            'SILICONFLOW_ASR_CNY_PER_SECOND': '.00022',
            'SILICONFLOW_PRICING_VERIFIED_ON': '2026-10-20',
            'SILICONFLOW_PRICING_REFERENCE': 'offline fixture'}
        with self.assertRaises(cloud_settings.ConfigurationRequired):
            cloud_settings.load_siliconflow_settings(siliconflow, today=date(2026, 10, 20))

    def test_transport_encodable_printable_keys_do_not_require_a_provider_pattern(self):
        key = 'opaque-' + chr(0xe9) + '-!$%&*+._~:/=?@[]{}()'
        self.assertEqual(credential_store._validated({'DEEPSEEK_API_KEY': key}),
                         {'DEEPSEEK_API_KEY': key})
        values = {'DASHSCOPE_API_KEY': key, 'DEEPSEEK_API_KEY': key,
                  'CLOUD_PRICING_VERIFIED_ON': '2026-10-20', 'CLOUD_PRICING_REFERENCE': 'offline fixture'}
        settings = cloud_settings.load_settings(values, today=date(2026, 10, 20))
        self.assertEqual(settings.asr_key, key)
        self.assertEqual(settings.deepseek_key, key)

    def test_asr_only_does_not_validate_unused_legacy_translation_key(self):
        values = {'DASHSCOPE_API_KEY': 'fixture-asr', 'DEEPSEEK_API_KEY': 'legacy-' + chr(0x4e2d),
                  'CLOUD_PRICING_VERIFIED_ON': '2026-10-20', 'CLOUD_PRICING_REFERENCE': 'offline fixture'}
        try:
            settings = cloud_settings.load_settings(values, require_deepseek=False, today=date(2026, 10, 20))
        except cloud_settings.ConfigurationRequired:
            self.fail('An unused legacy translation key blocked independently configured ASR')
        self.assertEqual(settings.asr_key, values['DASHSCOPE_API_KEY'])
        with self.assertRaises(cloud_settings.ConfigurationRequired):
            cloud_settings.load_settings(values, require_deepseek=True, today=date(2026, 10, 20))


if __name__ == '__main__':
    unittest.main()
