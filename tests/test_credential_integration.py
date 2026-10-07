"""Credential integration uses fake stores and a fake Windows registry only."""

from datetime import date
import os
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, patch

from subtitle_pipeline import cloud_gui, cloud_settings, siliconflow_pilot


class CredentialIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.store = SimpleNamespace(
            SUPPORTED_KEYS=('SILICONFLOW_API_KEY', 'DASHSCOPE_API_KEY', 'DEEPSEEK_API_KEY'),
            CredentialStoreError=type('CredentialStoreError', (ValueError,), {}),
            read_saved_secrets=Mock(return_value={}),
            save_secrets=Mock(),
        )
        self.registry_values = {}
        self.registry_writes = {}
        self.registry = MagicMock()
        self.registry.OpenKey.return_value.__enter__.return_value = 'read-handle'
        self.registry.CreateKeyEx.return_value.__enter__.return_value = 'write-handle'
        def query(_handle, name):
            if name not in self.registry_values:
                raise FileNotFoundError
            return self.registry_values[name], 1
        def write(_handle, name, _reserved, _kind, value):
            self.registry_writes[name] = value
        self.registry.QueryValueEx.side_effect = query
        self.registry.SetValueEx.side_effect = write
        for target, attribute, value in (
            (cloud_settings, 'credential_store', self.store),
            (cloud_gui, 'credential_store', self.store),
            (os, 'name', 'nt'),
        ):
            guard = patch.object(target, attribute, value, create=True)
            guard.start()
            self.addCleanup(guard.stop)
        for guard in (
            patch.dict(os.environ, {}, clear=True),
            patch.dict('sys.modules', {'winreg': self.registry}),
            patch('socket.socket.connect', side_effect=AssertionError('offline credentials test')),
            patch('socket.create_connection', side_effect=AssertionError('offline credentials test')),
        ):
            guard.start()
            self.addCleanup(guard.stop)

    def prices(self):
        return {'CLOUD_PRICING_VERIFIED_ON': date.today().isoformat(),
                'CLOUD_PRICING_REFERENCE': 'offline fixture'}

    def test_saved_keys_override_legacy_keys_without_changing_environment(self):
        os.environ.update(self.prices(), DASHSCOPE_API_KEY='process-fixture',
                          DEEPSEEK_API_KEY='process-deepseek-fixture')
        self.registry_values.update(DASHSCOPE_API_KEY='registry-fixture')
        self.store.read_saved_secrets.return_value = {
            'DASHSCOPE_API_KEY': 'encrypted-fixture',
            'DEEPSEEK_API_KEY': 'encrypted-deepseek-fixture'}
        settings = cloud_settings.load_settings()
        self.assertEqual(settings.asr_key, 'encrypted-fixture')
        self.assertEqual(settings.deepseek_key, 'encrypted-deepseek-fixture')
        self.assertEqual(os.environ['DASHSCOPE_API_KEY'], 'process-fixture')
        self.assertEqual(self.registry_values['DASHSCOPE_API_KEY'], 'registry-fixture')

    def test_missing_saved_keys_keep_legacy_process_and_registry_configuration(self):
        os.environ.update(self.prices(), DASHSCOPE_API_KEY='process-fixture',
                          DEEPSEEK_API_KEY='process-deepseek-fixture')
        self.registry_values.update(DASHSCOPE_API_KEY='registry-fixture',
                                    CLOUD_PRICING_REFERENCE='registry price fixture')
        settings = cloud_settings.load_settings()
        self.assertEqual(settings.asr_key, 'registry-fixture')
        self.assertEqual(settings.deepseek_key, 'process-deepseek-fixture')
        self.assertEqual(settings.pricing_reference, 'registry price fixture')

    def test_shared_reader_does_not_return_unrelated_environment_secrets(self):
        reader = getattr(cloud_settings, 'read_environment', None)
        self.assertTrue(callable(reader), 'The shared allowed-fields reader is missing')
        os.environ.update(UNRELATED_SECRET='unrelated-fixture', SILICONFLOW_API_KEY='process-fixture')
        self.store.read_saved_secrets.return_value = {
            'SILICONFLOW_API_KEY': 'encrypted-fixture', 'DEEPSEEK_API_KEY': 'other-fixture'}
        values = reader(('SILICONFLOW_API_KEY', 'UNRELATED_SECRET'))
        self.assertEqual(values, {'SILICONFLOW_API_KEY': 'encrypted-fixture'})

    def test_siliconflow_cli_and_gui_use_saved_key_after_restart(self):
        os.environ.update(SILICONFLOW_ASR_CNY_PER_SECOND='0.000220',
                          SILICONFLOW_PRICING_VERIFIED_ON=date.today().isoformat(),
                          SILICONFLOW_PRICING_REFERENCE='offline account quotation')
        self.registry_values['SILICONFLOW_API_KEY'] = 'old-fixture'
        self.store.read_saved_secrets.return_value = {'SILICONFLOW_API_KEY': 'encrypted-fixture'}
        self.assertEqual(siliconflow_pilot.configuration(), 'encrypted-fixture')
        self.assertEqual(cloud_gui.siliconflow_environment()['SILICONFLOW_API_KEY'], 'encrypted-fixture')
        self.assertNotIn('SILICONFLOW_API_KEY', os.environ)

    def test_damaged_store_blocks_legacy_key_fallback_in_all_readers(self):
        os.environ.update(self.prices(), DASHSCOPE_API_KEY='legacy-fixture',
                          DEEPSEEK_API_KEY='legacy-deepseek-fixture',
                          SILICONFLOW_API_KEY='legacy-siliconflow-fixture',
                          SILICONFLOW_FREE_ASR_VERIFIED_ON=date.today().isoformat())
        self.store.read_saved_secrets.side_effect = self.store.CredentialStoreError('本机加密密钥无法读取，请重新保存。')
        for read in (cloud_settings.load_settings, siliconflow_pilot.configuration,
                     cloud_gui.siliconflow_environment):
            with self.subTest(reader=read.__name__), self.assertRaisesRegex(ValueError, '加密'):
                read()

    def test_damaged_store_readiness_reports_safe_message_without_legacy_credentials(self):
        os.environ.update(self.prices(), DASHSCOPE_API_KEY='legacy-fixture',
                          DEEPSEEK_API_KEY='legacy-deepseek-fixture')
        self.store.read_saved_secrets.side_effect = self.store.CredentialStoreError('本机加密密钥无法读取，请重新保存。')
        report = cloud_settings.readiness()
        self.assertFalse(report['ready'])
        self.assertEqual(report['credentials'], {'DASHSCOPE_API_KEY': False, 'DEEPSEEK_API_KEY': False})
        self.assertIn('加密', report['message'])
        self.assertNotIn('fixture', repr(report))

    def test_gui_saves_keys_only_to_encrypted_store_and_keeps_plain_configuration(self):
        os.environ['SILICONFLOW_API_KEY'] = 'legacy-fixture'
        cloud_gui.save_siliconflow_settings('new-fixture', confirmed=True, today=date(2026, 10, 2),
            price_per_second='0.000220',pricing_reference='offline account quotation')
        self.assertEqual(self.registry_writes, {'SILICONFLOW_ASR_CNY_PER_SECOND':'0.00022',
            'SILICONFLOW_PRICING_VERIFIED_ON':'2026-10-02',
            'SILICONFLOW_PRICING_REFERENCE':'offline account quotation'})
        self.assertEqual(os.environ['SILICONFLOW_API_KEY'], 'legacy-fixture')
        self.assertEqual(os.environ['SILICONFLOW_PRICING_VERIFIED_ON'], '2026-10-02')
        self.assertEqual(self.store.save_secrets.call_args.args[0], {'SILICONFLOW_API_KEY': 'new-fixture'})

    def test_encryption_failure_does_not_write_plaintext_or_report_key(self):
        self.store.save_secrets.side_effect = self.store.CredentialStoreError('new-fixture')
        with self.assertRaises(ValueError) as raised:
            cloud_gui.save_siliconflow_settings('new-fixture')
        self.assertEqual(self.registry_writes, {})
        self.assertNotIn('SILICONFLOW_API_KEY', os.environ)
        self.assertNotIn('new-fixture', str(raised.exception))
        self.assertNotIn('环境变量', str(raised.exception))

    def test_siliconflow_pricing_validates_rate_date_source_and_hides_key(self):
        load=getattr(cloud_settings,'load_siliconflow_settings',None)
        self.assertTrue(callable(load),'Verified SiliconFlow pricing loader is missing')
        values={'SILICONFLOW_API_KEY':'offline-fixture','SILICONFLOW_ASR_CNY_PER_SECOND':'0.000220',
            'SILICONFLOW_PRICING_VERIFIED_ON':'2026-10-02',
            'SILICONFLOW_PRICING_REFERENCE':'offline account quotation'}
        settings=load(values,today=date(2026,10,2))
        self.assertEqual(settings.price_per_second,0.00022)
        self.assertNotIn('offline-fixture',repr(settings)+repr(settings.public_config()))
        self.assertEqual(load({**values,'SILICONFLOW_ASR_CNY_PER_SECOND':'0'},today=date(2026,10,2)).price_per_second,0)
        for field,invalid in [('SILICONFLOW_ASR_CNY_PER_SECOND',''),
                ('SILICONFLOW_ASR_CNY_PER_SECOND','nan'),('SILICONFLOW_ASR_CNY_PER_SECOND','inf'),
                ('SILICONFLOW_ASR_CNY_PER_SECOND','-0.001'),('SILICONFLOW_ASR_CNY_PER_SECOND',True),
                ('SILICONFLOW_PRICING_VERIFIED_ON','2026-09-24'),('SILICONFLOW_PRICING_VERIFIED_ON','2026-10-03'),
                ('SILICONFLOW_PRICING_REFERENCE',''),('SILICONFLOW_API_KEY','bad key')]:
            with self.subTest(field=field,invalid=invalid),self.assertRaises(ValueError):
                load({**values,field:invalid},today=date(2026,10,2))

    def test_gui_refresh_disables_cloud_actions_when_secret_store_is_unreadable(self):
        app = cloud_gui.CloudSubtitleApp.__new__(cloud_gui.CloudSubtitleApp)
        app.configure_button = Mock()
        app.siliconflow_configure_button = Mock()
        app.config_status = Mock()
        app.siliconflow_status = Mock()
        app.status = Mock()
        app.campaign = Mock(get=Mock(return_value=''))
        app.process = None
        app.entries = []
        app.buttons = {name: Mock() for name in ('samples', 'full', 'siliconflow-pilot')}
        app._manifest = Mock(return_value={'status': 'prepared'})
        with patch.object(cloud_settings, 'readiness', return_value={'ready': False, 'message': '加密密钥无法读取'}), \
                patch.object(cloud_gui, 'siliconflow_environment', side_effect=cloud_settings.ConfigurationRequired('加密密钥无法读取')):
            try:
                app._refresh()
            except ValueError:
                self.fail('Credential errors must be displayed without crashing GUI refresh')
        for button in app.buttons.values():
            button.configure.assert_called_with(state='disabled')
        self.assertIn('加密', app.siliconflow_status.set.call_args.args[0])

    def test_unreadable_settings_do_not_leave_broken_configuration_dialogs(self):
        app = cloud_gui.CloudSubtitleApp.__new__(cloud_gui.CloudSubtitleApp)
        app.root = Mock()
        for configure, module, reader in (
            (app._configure, cloud_settings, 'environment'),
            (app._configure_siliconflow, cloud_gui, 'siliconflow_environment'),
        ):
            with self.subTest(dialog=configure.__name__), \
                    patch.object(module, reader, side_effect=cloud_settings.ConfigurationRequired('加密密钥无法读取')), \
                    patch.object(cloud_gui.tk, 'Toplevel') as dialog, \
                    patch.object(cloud_gui.messagebox, 'showerror') as show_error:
                try:
                    configure()
                except (ValueError, TypeError):
                    self.fail('Credential errors must be shown before opening a configuration dialog')
                dialog.assert_not_called()
                self.assertIn('加密', show_error.call_args.args[1])


if __name__ == '__main__':
    unittest.main()
