from datetime import date
import importlib
import json
from pathlib import Path
import subprocess
import tempfile
import tkinter as tk
from tkinter import ttk
import unittest
from unittest.mock import Mock, patch


class CloudGUIHelpersTests(unittest.TestCase):
    def setUp(self):
        try:
            self.gui = importlib.import_module('subtitle_pipeline.cloud_gui')
        except ModuleNotFoundError:
            self.fail('Cloud subtitle GUI is not implemented')
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.campaign = Path(self.temp.name) / 'campaign with spaces'
        self.addCleanup(patch.stopall)
        patch('socket.create_connection', side_effect=AssertionError('GUI tests must not access the network')).start()
        patch('socket.socket.connect', side_effect=AssertionError('GUI tests must not access the network')).start()

    def values(self):
        return {
            'QWEN_ASR_ENDPOINT': 'https://dashscope.aliyuncs.com/api/v1',
            'DASHSCOPE_API_KEY': 'bailian-test-secret', 'DEEPSEEK_API_KEY': 'deepseek-test-secret',
            'QWEN_ASR_INPUT_CNY_PER_MILLION': '0.8',
            'QWEN_ASR_OUTPUT_CNY_PER_MILLION': '2.7',
            'CLOUD_PRICING_VERIFIED_ON': date.today().isoformat(),
            'CLOUD_PRICING_REFERENCE': 'Qwen Beijing official CNY prices; DeepSeek official prices',
            'DEEPSEEK_INPUT_CNY_PER_MILLION': '2',
            'DEEPSEEK_OUTPUT_CNY_PER_MILLION': '8',
        }

    def test_commands_preserve_literal_paths_without_shell_or_credentials(self):
        source = Path('D:/字幕 & 文件/电影;测试.mp4')
        baseline = Path('D:/字幕 & 文件/原字幕.srt')
        for action in ('prepare', 'samples'):
            command = self.gui.build_command(action, campaign=self.campaign,
                source=source, baseline=baseline, executable='python.exe')
            self.assertEqual(command, ['python.exe', '-m', 'subtitle_pipeline.cloud_workflow',
                action, '--campaign', str(self.campaign), '--source', str(source),
                '--baseline', str(baseline)])
            self.assertFalse(any('KEY' in part or 'secret' in part for part in command))

    def test_approval_validates_counts_and_explicit_content_review(self):
        self.assertEqual(self.gui.validate_review('20', '18', True), (20, 18))
        for reviewed, passed, content in [('19', '19', True), ('20', '17', True),
                ('20', '21', True), ('20', '18', False), ('2.0', '2', True),
                (True, 1, True), ('', '', False), ('21', '18', True)]:
            with self.subTest(reviewed=reviewed, passed=passed, content=content):
                with self.assertRaises(ValueError):
                    self.gui.validate_review(reviewed, passed, content)

    def test_prepare_command_carries_selected_language_pair_only_for_prepare(self):
        command=self.gui.build_command('prepare',campaign=self.campaign,source='video.mp4',
                                      language='zh',target='ja')
        self.assertEqual(command[-4:],['--language','zh','--target','ja'])
        command=self.gui.build_command('prepare',campaign=self.campaign,source='video.mp4',language='zh')
        self.assertEqual(command[-4:],['--language','zh','--target','en'])
        command=self.gui.build_command('samples',campaign=self.campaign,source='video.mp4',
                                      language='zh',target='ja')
        self.assertNotIn('--language',command)
        with self.assertRaises(ValueError):
            self.gui.build_command('prepare',campaign=self.campaign,source='video.mp4',language='fr')

    def test_prepare_and_samples_commands_allow_no_comparison_subtitle(self):
        for action in ('prepare', 'samples'):
            for baseline in (None, '', '   '):
                with self.subTest(action=action, baseline=baseline):
                    command = self.gui.build_command(action, campaign=self.campaign,
                        source='short video.mp4', baseline=baseline, executable='python.exe')
                    self.assertEqual(command, ['python.exe', '-m', 'subtitle_pipeline.cloud_workflow',
                        action, '--campaign', str(self.campaign), '--source', 'short video.mp4'])

    def test_desktop_launch_accepts_existing_video_without_baseline(self):
        source = Path(self.temp.name) / 'short video.mp4'
        source.write_bytes(b'video')
        app = self.gui.CloudSubtitleApp.__new__(self.gui.CloudSubtitleApp)
        app.process = None
        app.campaign = Mock(get=Mock(return_value=str(self.campaign)))
        app.source = Mock(get=Mock(return_value=str(source)))
        app.baseline = Mock(get=Mock(return_value=''))
        app.status = Mock()
        app._append = Mock()
        app._refresh = Mock()
        app.events = Mock()
        app.root = None
        process = Mock()
        with patch.object(self.gui, 'start_process', return_value=process) as start, \
                patch.object(self.gui.cloud_settings, 'environment', return_value={}), \
                patch.object(self.gui, 'siliconflow_environment', return_value={}), \
                patch.object(self.gui.threading, 'Thread'), \
                patch.object(self.gui.messagebox, 'showerror') as error:
            app._launch('prepare')
        error.assert_not_called()
        self.assertIs(app.process, process)
        self.assertNotIn('--baseline', start.call_args.args[0])

    def test_review_command_includes_only_human_entered_results(self):
        command = self.gui.build_command('approve', campaign=self.campaign, reviewed='20',
            timing_passed='18', content_passed=True, executable='python.exe')
        self.assertEqual(command[-5:], ['--reviewed', '20', '--timing-passed', '18', '--content-passed'])
        with self.assertRaises(ValueError):
            self.gui.build_command('accept-final', campaign=self.campaign)
        self.assertEqual(self.gui.build_command('accept-final', campaign=self.campaign,
            content_passed=True)[-1], '--content-passed')

    def test_unknown_action_and_missing_paths_do_not_create_commands(self):
        for action, arguments in [('shell', {}), ('prepare', {}),
                ('samples', {}), ('full', {'campaign': ''}),
                ('full', {'campaign': None})]:
            with self.subTest(action=action):
                kwargs = {'campaign': self.campaign, **arguments}
                with self.assertRaises(ValueError):
                    self.gui.build_command(action, **kwargs)

    def test_settings_must_be_confirmed_and_valid_before_any_write(self):
        writer = Mock()
        with self.assertRaises(ValueError):
            self.gui.save_settings(self.values(), confirmed=False, writer=writer)
        values = self.values()
        values['QWEN_ASR_INPUT_CNY_PER_MILLION'] = '-2'
        with self.assertRaises(ValueError):
            self.gui.save_settings(values, confirmed=True, writer=writer)
        writer.assert_not_called()

    def test_settings_save_only_known_environment_fields_and_return_no_keys(self):
        values = self.values()
        values['OTHER_SECRET'] = 'unrelated'
        writer = Mock()
        result = self.gui.save_settings(values, confirmed=True, writer=writer)
        writer.assert_called_once()
        saved = writer.call_args.args[0]
        self.assertEqual(saved['QWEN_ASR_ENDPOINT'], 'https://dashscope.aliyuncs.com/api/v1')
        self.assertEqual(saved['DASHSCOPE_API_KEY'], 'bailian-test-secret')
        self.assertEqual(saved['QWEN_ASR_INPUT_CNY_PER_MILLION'], '0.8')
        self.assertEqual(saved['QWEN_ASR_OUTPUT_CNY_PER_MILLION'], '2.7')
        self.assertNotIn('OTHER_SECRET', saved)
        self.assertNotIn('secret', json.dumps(result))
        self.assertNotIn('KEY', json.dumps(result))
        self.assertEqual(result['asr_input_rate'], 0.8)
        self.assertEqual(result['asr_output_rate'], 2.7)
        self.assertFalse(any(name.startswith('AZURE_') for name in saved))

    def test_environment_write_failure_does_not_expose_credentials(self):
        with self.assertRaises(ValueError) as raised:
            self.gui.save_settings(self.values(), confirmed=True,
                writer=Mock(side_effect=OSError('bailian-test-secret')))
        self.assertNotIn('bailian-test-secret', str(raised.exception))

    def test_configuration_dialog_uses_bailian_fields_masks_keys_and_requires_manual_confirmation(self):
        root = tk.Tk()
        root.withdraw()
        self.addCleanup(root.destroy)
        actual_toplevel = tk.Toplevel
        def hidden_dialog(*args, **kwargs):
            dialog = actual_toplevel(*args, **kwargs)
            dialog.withdraw()
            return dialog
        with patch.object(self.gui.cloud_settings, 'environment', return_value=self.values()), \
                patch.object(self.gui.cloud_settings, 'readiness', return_value={'ready': False, 'message': 'not configured'}), \
                patch.object(self.gui, 'siliconflow_environment', return_value={}, create=True), \
                patch.object(self.gui.tk, 'Toplevel', side_effect=hidden_dialog):
            app = self.gui.CloudSubtitleApp(root)
            app._configure()
        def descendants(parent):
            for widget in parent.winfo_children():
                yield widget
                yield from descendants(widget)
        widgets = list(descendants(root))
        entries = {entry.get(): entry for entry in widgets if isinstance(entry, ttk.Entry)}
        self.assertIn('bailian-test-secret', entries)
        self.assertEqual('*', entries['bailian-test-secret'].cget('show'))
        self.assertEqual('*', entries['deepseek-test-secret'].cget('show'))
        self.assertIn('https://dashscope.aliyuncs.com/api/v1', entries)
        self.assertIn('0.8', entries)
        self.assertIn('2.7', entries)
        confirmations = [widget for widget in widgets if isinstance(widget, ttk.Checkbutton)]
        self.assertEqual(1, len(confirmations))
        self.assertFalse(root.tk.getboolean(root.getvar(confirmations[0].cget('variable'))))
        save = next(widget for widget in widgets if isinstance(widget, ttk.Button) and widget.cget('text') == '保存已核实配置')
        with patch.object(self.gui, '_write_environment') as writer, patch.object(self.gui.messagebox, 'showerror'):
            save.invoke()
        writer.assert_not_called()

    def test_siliconflow_key_save_is_independent_of_bailian_and_price_confirmation(self):
        writer = Mock()
        with patch.object(self.gui.cloud_settings, 'load_settings', side_effect=AssertionError('must not require Bailian')):
            try:
                self.gui.save_siliconflow_settings('sf-test-secret', confirmed=False, writer=writer)
            except ValueError:
                self.fail('Saving a Key must not require price confirmation')
        self.assertEqual({'SILICONFLOW_API_KEY':'sf-test-secret'},writer.call_args.args[0])

    def test_siliconflow_price_save_requires_rate_and_source(self):
        writer=Mock()
        with self.assertRaises(ValueError):
            self.gui.save_siliconflow_settings('sf-test-secret',confirmed=True,writer=writer)
        writer.assert_not_called()
        with patch.object(self.gui.cloud_settings, 'load_settings', side_effect=AssertionError('must not require Bailian')):
            result = self.gui.save_siliconflow_settings('sf-test-secret', confirmed=True,
                price_per_second='0.000220',pricing_reference='offline account quotation',
                writer=writer, today=date(2026, 9, 28))
        self.assertEqual({'SILICONFLOW_API_KEY': 'sf-test-secret',
                          'SILICONFLOW_ASR_CNY_PER_SECOND':'0.00022',
                          'SILICONFLOW_PRICING_VERIFIED_ON': '2026-09-28',
                          'SILICONFLOW_PRICING_REFERENCE':'offline account quotation'}, writer.call_args.args[0])
        self.assertNotIn('sf-test-secret', repr(result))

    def test_siliconflow_pilot_command_has_no_official_workflow_approval_or_secret(self):
        command = self.gui.build_command('siliconflow-pilot', campaign=self.campaign, executable='python.exe')
        self.assertEqual(['python.exe', '-m', 'subtitle_pipeline.siliconflow_pilot',
                          '--campaign', str(self.campaign)], command)
        enabled = self.gui.available_actions('prepared', approval_exists=False, ready=False,
                                              siliconflow_ready=True, busy=False)
        self.assertTrue(enabled['siliconflow-pilot'])
        self.assertFalse(enabled['samples'])
        self.assertFalse(enabled['approve'])
        self.assertFalse(enabled['full'])
        for status, ready, busy in (('preparing', True, False), ('prepared', False, False), ('prepared', True, True)):
            self.assertFalse(self.gui.available_actions(status, approval_exists=False, ready=False,
                                                        siliconflow_ready=ready, busy=busy)['siliconflow-pilot'])

    def test_siliconflow_ready_requires_recent_verified_pricing(self):
        for stamp, expected in (('2026-09-28', True), ('2026-09-21', True),
                                ('2026-09-20', False), ('2026-09-29', False), ('', False)):
            values = {'SILICONFLOW_API_KEY': 'test-key', 'SILICONFLOW_ASR_CNY_PER_SECOND':'0.000220',
                'SILICONFLOW_PRICING_VERIFIED_ON':stamp,'SILICONFLOW_PRICING_REFERENCE':'offline quote'}
            self.assertEqual(expected, self.gui.siliconflow_ready(values, today=date(2026, 9, 28)))
        self.assertFalse(self.gui.siliconflow_ready({'SILICONFLOW_FREE_ASR_VERIFIED_ON': '2026-09-28'},
                                                  today=date(2026, 9, 28)))
        self.assertFalse(self.gui.siliconflow_ready({'SILICONFLOW_API_KEY':'test-key',
            'SILICONFLOW_FREE_ASR_VERIFIED_ON':'2026-09-28'},today=date(2026,9,28)))

    def test_siliconflow_result_can_be_opened_without_unlocking_formal_approval(self):
        enabled = self.gui.available_actions('prepared', approval_exists=False, ready=False,
            siliconflow_ready=True, siliconflow_result_exists=True, busy=False)
        self.assertTrue(enabled['siliconflow-review'])
        self.assertFalse(enabled['review'])
        self.assertFalse(enabled['approve'])
        self.assertFalse(enabled['full'])

    def test_siliconflow_view_is_independent_of_formal_stage_and_routes_to_own_html(self):
        for status in ('prepared','samples_ready','samples_incomplete','full_ready',''):
            enabled=self.gui.available_actions(status,approval_exists=False,ready=False,busy=False,
                siliconflow_result_exists=True)
            self.assertTrue(enabled['siliconflow-review'])
        enabled=self.gui.available_actions('prepared',approval_exists=False,ready=False,busy=False,
                                           siliconflow_result_exists=False)
        self.assertFalse(enabled['siliconflow-review'])
        app=self.gui.CloudSubtitleApp.__new__(self.gui.CloudSubtitleApp)
        app.campaign=Mock(get=Mock(return_value=str(self.campaign)))
        app._open_path=Mock()
        app._siliconflow_review()
        app._open_path.assert_called_once_with(self.campaign/'硅基流动识别试听.html')
        app._open_path.reset_mock()
        app._review()
        app._open_path.assert_called_once_with(self.campaign/'review.html')

    def test_siliconflow_dialog_masks_key_and_does_not_preconfirm_pricing(self):
        root = tk.Tk(); root.withdraw(); self.addCleanup(root.destroy)
        real_toplevel = tk.Toplevel
        def hidden(*args, **kwargs):
            dialog = real_toplevel(*args, **kwargs); dialog.withdraw(); return dialog
        values = {'SILICONFLOW_API_KEY': 'sf-secret', 'SILICONFLOW_FREE_ASR_VERIFIED_ON': date.today().isoformat()}
        with patch.object(self.gui, 'siliconflow_environment', return_value=values), \
                patch.object(self.gui.cloud_settings, 'readiness', return_value={'ready': False, 'message': 'Bailian absent'}), \
                patch.object(self.gui.tk, 'Toplevel', side_effect=hidden):
            app = self.gui.CloudSubtitleApp(root); app._configure_siliconflow()
        dialog = next(child for child in root.winfo_children() if isinstance(child, real_toplevel))
        widgets = dialog.winfo_children()[0].winfo_children()
        entry = next(w for w in widgets if isinstance(w, ttk.Entry))
        self.assertEqual('sf-secret', entry.get()); self.assertEqual('*', entry.cget('show'))
        checkbox = next(w for w in widgets if isinstance(w, ttk.Checkbutton))
        self.assertFalse(root.tk.getboolean(root.getvar(checkbox.cget('variable'))))
        buttons=[w for w in widgets if isinstance(w,ttk.Button)]
        self.assertEqual(len(buttons),2)
        button=next(w for w in buttons if '价格' in w.cget('text'))
        with patch.object(self.gui, '_write_environment') as writer, \
                patch.object(self.gui.cloud_settings, 'load_settings', side_effect=AssertionError('Bailian not needed')), \
                patch.object(self.gui.messagebox, 'showerror'):
            button.invoke()
        writer.assert_not_called()

    def test_process_is_hidden_uses_argument_list_and_inherits_injected_environment(self):
        command = self.gui.build_command('full', campaign=self.campaign)
        factory = Mock()
        self.gui.start_process(command, process_factory=factory, environ={'PATH': 'test-path'})
        args, kwargs = factory.call_args
        self.assertEqual(args[0], command)
        self.assertFalse(kwargs['shell'])
        self.assertEqual(kwargs['stdout'], subprocess.PIPE)
        self.assertEqual(kwargs['stderr'], subprocess.STDOUT)
        self.assertEqual(kwargs['env']['PYTHONUTF8'], '1')
        self.assertEqual(kwargs['env']['PATH'], 'test-path')
        self.assertEqual(kwargs['creationflags'], getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.assertNotIn('secret', repr(args))

    def test_progress_uses_json_message_only_and_masks_known_secrets(self):
        self.assertEqual(self.gui.progress_message('{"message":"识别完成"}'), '识别完成')
        self.assertIsNone(self.gui.progress_message('plain debug secret'))
        self.assertIsNone(self.gui.progress_message('{"DASHSCOPE_API_KEY":"secret"}'))
        self.assertIsNone(self.gui.progress_message('{"message":123}'))
        self.assertEqual(self.gui.progress_message('{"message":"bad secret"}', ('secret',)), 'bad [已隐藏]')

    def test_campaign_controls_require_complete_samples_and_approval(self):
        controls = self.gui.available_actions('prepared', approval_exists=False, ready=False, busy=False)
        self.assertTrue(controls['prepare'])
        self.assertFalse(controls['samples'])
        self.assertFalse(controls['approve'])
        controls = self.gui.available_actions('samples_incomplete', approval_exists=True, ready=True, busy=False)
        self.assertFalse(controls['approve'])
        controls = self.gui.available_actions('samples_ready', approval_exists=False, ready=True, busy=False)
        self.assertTrue(controls['approve'])
        self.assertFalse(controls['full'])
        controls = self.gui.available_actions('samples_ready', approval_exists=True, ready=True, busy=False)
        self.assertTrue(controls['full'])
        controls = self.gui.available_actions('full_ready', approval_exists=True, ready=True, busy=False)
        self.assertTrue(controls['accept-final'])
        self.assertFalse(controls['export'])
        controls = self.gui.available_actions('final_reviewed', approval_exists=True, ready=True, busy=False)
        self.assertTrue(controls['export'])
        controls = self.gui.available_actions('samples_ready', approval_exists=True, ready=True, busy=True)
        self.assertTrue(controls['stop'])
        self.assertFalse(any(enabled for action, enabled in controls.items() if action != 'stop'))

    def test_stop_writes_only_marker_and_does_not_kill_process(self):
        self.campaign.mkdir()
        (self.campaign / 'campaign.json').write_text('{"status":"prepared"}', encoding='utf-8')
        marker = self.gui.request_stop(self.campaign)
        self.assertEqual(marker, self.campaign / 'STOP.flag')
        self.assertTrue(marker.is_file())
        self.assertEqual(json.loads((self.campaign / 'campaign.json').read_text())['status'], 'prepared')
        with self.assertRaises(ValueError):
            self.gui.request_stop(self.campaign / 'missing')


if __name__ == '__main__':
    unittest.main()
