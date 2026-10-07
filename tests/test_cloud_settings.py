import unittest
import tempfile
from datetime import date
from pathlib import Path
from unittest.mock import patch
from subtitle_pipeline.cloud_settings import load_settings, ConfigurationRequired
from subtitle_pipeline.runner import PipelineConfig, cloud_context


class SettingsTests(unittest.TestCase):
    def test_persistent_metering_hold_blocks_asr_and_translation_context_on_restart(self):
        with tempfile.TemporaryDirectory() as temporary:
            campaign=Path(temporary)
            (campaign/'billing-review-required.json').write_text('{}',encoding='utf-8')
            config=PipelineConfig(campaign/'source.wav',campaign/'job',asr_provider='qwen_asr',
                translation_provider='deepseek',budget_ledger=campaign/'费用账本.json')
            with patch('subtitle_pipeline.cloud_settings.load_settings') as load, \
                 patch('subtitle_pipeline.cloud_budget.BudgetLedger') as ledger:
                for _ in range(2):
                    with self.assertRaisesRegex(ValueError,'计量异常'):
                        cloud_context(config)
                load.assert_not_called()
                ledger.assert_not_called()

    def settings(self, **extra):
        values={'DASHSCOPE_API_KEY':'fake-asr-secret','DEEPSEEK_API_KEY':'fake-ds-secret',
                'CLOUD_PRICING_VERIFIED_ON':'2026-09-28','CLOUD_PRICING_REFERENCE':'account quotation'}
        values.update(extra)
        return load_settings(values,today=date(2026,9,28))

    def test_beijing_defaults_and_no_secrets_in_public_or_repr(self):
        result=self.settings()
        self.assertEqual(result.asr_endpoint,'https://dashscope.aliyuncs.com/api/v1')
        self.assertEqual((result.asr_input_rate,result.asr_output_rate),(.8,2.7))
        self.assertNotIn('fake-asr-secret',repr(result)+str(result.public_config()))
        self.assertNotIn('fake-ds-secret',repr(result)+str(result.public_config()))

    def test_credentials_missing_are_local_error(self):
        with self.assertRaisesRegex(ConfigurationRequired,'DASHSCOPE_API_KEY'):
            self.settings(DASHSCOPE_API_KEY='')

    def test_qianwen_official_endpoint_is_allowed_but_lookalikes_are_not(self):
        endpoint='https://maas.qianwenaiapi.com/api/v1'
        self.assertEqual(self.settings(QWEN_ASR_ENDPOINT=endpoint).asr_endpoint,endpoint)
        for invalid in ('http://maas.qianwenaiapi.com/api/v1',
                        'https://maas.qianwenaiapi.com.evil.test/api/v1',
                        'https://evil.maas.qianwenaiapi.com/api/v1',
                        'https://secret@maas.qianwenaiapi.com/api/v1',
                        'https://maas.qianwenaiapi.com:444/api/v1'):
            with self.subTest(endpoint=invalid), self.assertRaises(ConfigurationRequired):
                self.settings(QWEN_ASR_ENDPOINT=invalid)

    def test_refuse_stale_future_or_invalid_prices(self):
        for extra in [{'CLOUD_PRICING_VERIFIED_ON':'2026-09-01'},
                      {'CLOUD_PRICING_VERIFIED_ON':'2026-09-29'},
                      {'QWEN_ASR_INPUT_CNY_PER_MILLION':'NaN'},
                      {'QWEN_ASR_OUTPUT_CNY_PER_MILLION':'-1'}]:
            with self.subTest(extra=extra), self.assertRaises(ConfigurationRequired):self.settings(**extra)

    def test_only_beijing_official_endpoint(self):
        for endpoint in ['https://example.com/api/v1','http://dashscope.aliyuncs.com/api/v1',
            'https://dashscope.aliyuncs.com.attacker.test/api/v1',
            'https://key@dashscope.aliyuncs.com/api/v1','https://dashscope.aliyuncs.com/api/v1?key=secret']:
            with self.subTest(endpoint=endpoint), self.assertRaises(ConfigurationRequired):
                self.settings(QWEN_ASR_ENDPOINT=endpoint)
        self.settings(QWEN_ASR_ENDPOINT='https://123456.cn-beijing.maas.aliyuncs.com/api/v1')


if __name__=='__main__':unittest.main()
