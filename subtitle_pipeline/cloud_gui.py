"""Desktop control panel for the explicit, review-gated cloud workflow."""

from __future__ import annotations

import argparse
from datetime import date
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import webbrowser

from . import cloud_settings, credential_store
from .languages import default_target, validate_languages


_ACTIONS = ('prepare', 'samples', 'approve', 'full', 'accept-final', 'export', 'export-draft', 'siliconflow-pilot')
_GUIDE = Path(__file__).resolve().parents[1] / 'docs' / '云端字幕配置与使用.md'
_KEY_GUIDE = 'https://help.aliyun.com/zh/model-studio/get-api-key'


def validate_review(reviewed, timing_passed, content_passed) -> tuple[int, int]:
    """Require human-entered counts and an explicit content review result."""
    try:
        if isinstance(reviewed, bool) or isinstance(timing_passed, bool):
            raise ValueError
        count, passed = int(str(reviewed)), int(str(timing_passed))
    except (TypeError, ValueError):
        raise ValueError('请填写实际抽检句数和时间合格句数（整数）。') from None
    if count < 20 or passed < 0 or passed > count or passed * 10 < count * 9:
        raise ValueError('样片至少抽检 20 句，时间合格比例须达到 90%，合格数不能超过抽检数。')
    if content_passed is not True:
        raise ValueError('请完成内容检查，并明确确认内容通过。')
    return count, passed


def build_command(action, *, campaign, source=None, baseline=None, reviewed=None,
                  timing_passed=None, content_passed=False, executable=None,
                  language=None,target=None) -> list[str]:
    if action not in _ACTIONS or campaign is None or not str(campaign).strip():
        raise ValueError('请选择有效操作和项目目录。')
    if action == 'siliconflow-pilot':
        return [executable or sys.executable, '-m', 'subtitle_pipeline.siliconflow_pilot',
                '--campaign', str(campaign)]
    command = [executable or sys.executable, '-m', 'subtitle_pipeline.cloud_workflow',
               action, '--campaign', str(campaign)]
    if action in ('prepare', 'samples'):
        if not source or not str(source).strip():
            raise ValueError('请选择源视频；用于对照的原字幕可以留空。')
        command += ['--source', str(source)]
        if baseline is not None and str(baseline).strip():
            command += ['--baseline', str(baseline)]
        if action=='prepare' and (language is not None or target is not None):
            language='ja' if language is None else language
            target=default_target(language) if target is None else target
            validate_languages(language,target)
            command += ['--language',language,'--target',target]
    elif action == 'approve':
        count, passed = validate_review(reviewed, timing_passed, content_passed)
        command += ['--reviewed', str(count), '--timing-passed', str(passed), '--content-passed']
    elif action == 'accept-final':
        if content_passed is not True:
            raise ValueError('请先完成最终抽检并明确确认通过。')
        command.append('--content-passed')
    return command


def _write_environment(values: dict[str, str]) -> None:
    if os.name != 'nt':
        raise OSError('Persistent account configuration requires Windows.')
    secrets = {name: value for name, value in values.items()
               if name in credential_store.SUPPORTED_KEYS}
    plain = {name: value for name, value in values.items()
             if name not in credential_store.SUPPORTED_KEYS and
             name in (*cloud_settings.ENV_NAMES, *cloud_settings.SILICONFLOW_ENV_NAMES)}
    if secrets:
        credential_store.save_secrets(secrets)
    if not plain:
        return
    import winreg
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, 'Environment', 0, winreg.KEY_SET_VALUE) as key:
        for name, value in plain.items():
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
    os.environ.update(plain)


def save_settings(values, *, confirmed=False, writer=None) -> dict:
    if confirmed is not True:
        raise ValueError('请先核实当前资源地区和服务价格，再勾选核价确认。')
    settings = cloud_settings.load_settings({name: values.get(name, '') for name in cloud_settings.ENV_NAMES})
    normalized = {
        'QWEN_ASR_ENDPOINT': settings.asr_endpoint,
        'DASHSCOPE_API_KEY': settings.asr_key, 'DEEPSEEK_API_KEY': settings.deepseek_key,
        'QWEN_ASR_INPUT_CNY_PER_MILLION': str(settings.asr_input_rate),
        'QWEN_ASR_OUTPUT_CNY_PER_MILLION': str(settings.asr_output_rate),
        'CLOUD_PRICING_VERIFIED_ON': settings.verified_on,
        'CLOUD_PRICING_REFERENCE': settings.pricing_reference,
        'DEEPSEEK_INPUT_CNY_PER_MILLION': str(settings.deepseek_input_rate),
        'DEEPSEEK_OUTPUT_CNY_PER_MILLION': str(settings.deepseek_output_rate),
    }
    try:
        (writer or _write_environment)(normalized)
    except Exception:
        raise ValueError('配置保存失败，请检查当前 Windows 用户的本机加密存储和配置目录后重试。') from None
    return settings.public_config()


def siliconflow_environment():
    return cloud_settings.read_environment(cloud_settings.SILICONFLOW_ENV_NAMES)


def siliconflow_ready(values, *, today=None):
    try:
        cloud_settings.load_siliconflow_settings(values,today=today)
        return True
    except (ValueError,TypeError):return False


def save_siliconflow_settings(key, *, price_per_second=None, pricing_reference='',
                             confirmed=False, writer=None, today=None):
    if not isinstance(key,str) or not key.strip() or any(char.isspace() or ord(char)<32 or ord(char)==127 for char in key):
        raise ValueError('请填写有效的硅基流动 API Key。')
    values={'SILICONFLOW_API_KEY':key}
    public={'key_saved':True,'model':'Qwen/Qwen3-ASR-1.7B'}
    if confirmed is True:
        values.update(SILICONFLOW_ASR_CNY_PER_SECOND=price_per_second,
                      SILICONFLOW_PRICING_VERIFIED_ON=(today or date.today()).isoformat(),
                      SILICONFLOW_PRICING_REFERENCE=pricing_reference)
        settings=cloud_settings.load_siliconflow_settings(values,today=today)
        values.update(SILICONFLOW_ASR_CNY_PER_SECOND=str(settings.price_per_second),
                      SILICONFLOW_PRICING_REFERENCE=settings.pricing_reference)
        public.update(settings.public_config())
    try:
        (writer or _write_environment)(values)
    except Exception:
        raise ValueError('硅基流动配置保存失败，请检查本机加密存储和配置目录后重试。') from None
    return public


def start_process(command: list[str], *, process_factory=None, environ=None):
    if not isinstance(command, list) or not all(isinstance(part, str) for part in command):
        raise ValueError('工作流命令必须是参数列表。')
    child_environment = dict(os.environ if environ is None else environ)
    child_environment.update(PYTHONUTF8='1', PYTHONUNBUFFERED='1')
    return (process_factory or subprocess.Popen)(command, shell=False,
        cwd=str(Path(__file__).resolve().parents[1]), env=child_environment,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        encoding='utf-8', errors='replace', bufsize=1,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))


def progress_message(line: str, secrets=()) -> str | None:
    try:
        value = json.loads(line)
    except (ValueError, TypeError):
        return None
    message = value.get('message') if isinstance(value, dict) else None
    if not isinstance(message, str):
        return None
    for secret in secrets:
        if secret:
            message = message.replace(secret, '[已隐藏]')
    return message


def available_actions(status, *, approval_exists, ready, busy, siliconflow_ready=False,
                      siliconflow_result_exists=False) -> dict[str, bool]:
    if busy:
        return {action: action == 'stop' for action in (*_ACTIONS, 'review', 'siliconflow-review', 'stop')}
    return {
        'prepare': True,
        'samples': ready and status in ('prepared', 'samples_ready', 'samples_incomplete'),
        'review': status in ('samples_ready', 'samples_incomplete', 'full_ready',
                            'full_incomplete', 'final_reviewed', 'exported'),
        'siliconflow-review': siliconflow_result_exists,
        'approve': status == 'samples_ready',
        'full': ready and approval_exists and status in ('samples_ready', 'full_incomplete', 'full_ready'),
        'accept-final': status == 'full_ready',
        'export': status in ('final_reviewed', 'exported'),
        'export-draft': status in ('full_ready', 'final_reviewed', 'exported'),
        'stop': False,
        'siliconflow-pilot': siliconflow_ready and status in ('prepared','samples_ready','samples_incomplete'),
    }


def request_stop(campaign: Path) -> Path:
    campaign = Path(campaign)
    if not campaign.is_dir():
        raise ValueError('项目目录尚未创建，请等待当前准备步骤结束。')
    marker = campaign / 'STOP.flag'
    marker.write_text('Stop requested by the user.\n', encoding='utf-8')
    return marker


class CloudSubtitleApp:
    def __init__(self, root: tk.Tk, *, source='', campaign='', baseline=''):
        self.root = root
        self.process = None
        self.events = queue.Queue()
        self.running_campaign = None
        self.source = tk.StringVar(value=source)
        self.campaign = tk.StringVar(value=campaign)
        self.baseline = tk.StringVar(value=baseline)
        self.status = tk.StringVar(value='尚未准备项目')
        self.config_status = tk.StringVar()
        self.siliconflow_status = tk.StringVar()
        self.buttons = {}
        self.entries = []
        root.title('云端日语字幕重做')
        root.geometry('980x800')
        root.minsize(760, 600)
        root.protocol('WM_DELETE_WINDOW', self._close)
        style = ttk.Style(root)
        style.configure('Title.TLabel', font=('Microsoft YaHei UI', 18, 'bold'))
        style.configure('Note.TLabel', foreground='#525f70')
        outer = ttk.Frame(root)
        outer.pack(fill='both', expand=True)
        canvas = tk.Canvas(outer, highlightthickness=0)
        scroll = ttk.Scrollbar(outer, orient='vertical', command=canvas.yview)
        scroll.pack(side='right', fill='y')
        canvas.pack(side='left', fill='both', expand=True)
        canvas.configure(yscrollcommand=scroll.set)
        body = ttk.Frame(canvas, padding=22)
        window = canvas.create_window((0, 0), window=body, anchor='nw')
        body.bind('<Configure>', lambda _event: canvas.configure(scrollregion=canvas.bbox('all')))
        canvas.bind('<Configure>', lambda event: canvas.itemconfigure(window, width=event.width))
        canvas.bind('<MouseWheel>', lambda event: canvas.yview_scroll(-int(event.delta / 120), 'units'))
        ttk.Label(body, text='云端日语字幕重做', style='Title.TLabel').pack(anchor='w')
        ttk.Label(body, text='先试识别效果，再决定完整字幕路线。识别按钮会上传音频；保存配置不会发出请求。',
                  style='Note.TLabel', wraplength=820).pack(anchor='w', pady=(6, 16))

        files = ttk.LabelFrame(body, text='1  选择文件与项目', padding=12)
        files.pack(fill='x')
        files.columnconfigure(1, weight=1)
        for row, (label, variable, kind) in enumerate((('源视频', self.source, 'video'),
                ('原字幕（可选对照）', self.baseline, 'subtitle'), ('项目目录', self.campaign, 'directory'))):
            ttk.Label(files, text=label).grid(row=row, column=0, sticky='w', padx=(0, 10), pady=5)
            entry = ttk.Entry(files, textvariable=variable)
            entry.grid(row=row, column=1, sticky='ew', pady=5)
            self.entries.append(entry)
            button = ttk.Button(files, text='选择…', command=lambda v=variable, k=kind: self._browse(v, k))
            button.grid(row=row, column=2, padx=(10, 0), pady=5)
            self.entries.append(button)
        self.campaign.trace_add('write', lambda *_: self._refresh())

        account = ttk.LabelFrame(body, text='2  账户与预算', padding=12)
        account.pack(fill='x', pady=12)
        ttk.Label(account, text='总预算 ¥20  ·  费用停止线 ¥18（本地估算；包含已预留请求）').pack(anchor='w')
        ttk.Label(account, textvariable=self.config_status, style='Note.TLabel', wraplength=820).pack(anchor='w', pady=6)
        tools = ttk.Frame(account)
        tools.pack(anchor='w')
        self.configure_button = ttk.Button(tools, text='百炼完整字幕：配置账户与价格', command=self._configure)
        self.configure_button.pack(side='left')
        self.siliconflow_configure_button = ttk.Button(tools, text='硅基流动试识别：配置 Key', command=self._configure_siliconflow)
        self.siliconflow_configure_button.pack(side='left',padx=8)
        ttk.Button(tools, text='打开配置指南', command=lambda: self._open_path(_GUIDE)).pack(side='left', padx=8)
        ttk.Label(account,textvariable=self.siliconflow_status,style='Note.TLabel',wraplength=820).pack(anchor='w',pady=(8,0))

        actions = ttk.LabelFrame(body, text='3  制作与人工验收', padding=12)
        actions.pack(fill='x')
        action_specs = [
            ('prepare', '准备样片（不收费）', lambda: self._launch('prepare')),
            ('siliconflow-pilot', '硅基流动试识别', lambda: self._launch('siliconflow-pilot')),
            ('siliconflow-review', '打开硅基流动试听', self._siliconflow_review),
            ('samples', '百炼样片（计费）', lambda: self._launch('samples')),
            ('review', '打开字幕样片对照', self._review),
            ('approve', '验收样片', self._approve),
            ('full', '处理整片（计费）', lambda: self._launch('full')),
            ('accept-final', '最终抽检确认', self._final_review),
            ('export', '压制 MP4', lambda: self._launch('export')),
            ('stop', '停止后续请求', self._stop),
        ]
        for index, (action, label, callback) in enumerate(action_specs):
            button = ttk.Button(actions, text=label, command=callback)
            button.grid(row=index // 4, column=index % 4, sticky='ew', padx=4, pady=5)
            self.buttons[action] = button
        for column in range(4):
            actions.columnconfigure(column, weight=1, uniform='action')
        ttk.Label(actions, text='验收由你操作：样片至少抽检 20 句，时间合格率达到 90%，并确认内容通过。',
                  style='Note.TLabel', wraplength=820).grid(row=(len(action_specs)+3)//4, column=0, columnspan=4, sticky='w', pady=(8, 0))
        ttk.Label(body, textvariable=self.status, wraplength=840).pack(anchor='w', pady=(16, 6))
        logframe = ttk.Frame(body)
        logframe.pack(fill='both', expand=True)
        self.log = tk.Text(logframe, height=10, wrap='word', state='disabled',
                           font=('Microsoft YaHei UI', 10), padx=10, pady=8)
        logscroll = ttk.Scrollbar(logframe, command=self.log.yview)
        self.log.configure(yscrollcommand=logscroll.set)
        self.log.pack(side='left', fill='both', expand=True)
        logscroll.pack(side='right', fill='y')
        self._refresh()
        root.after(150, self._poll)

    def _browse(self, variable, kind):
        if kind == 'directory':
            selected = filedialog.askdirectory(parent=self.root, mustexist=False, title='选择项目目录')
        else:
            types = [('字幕文件', '*.srt')] if kind == 'subtitle' else [('视频文件', '*.mp4 *.mkv *.mov *.avi *.webm'), ('全部文件', '*.*')]
            selected = filedialog.askopenfilename(parent=self.root, filetypes=types)
        if selected:
            variable.set(selected)
            if kind == 'video' and not self.campaign.get().strip():
                source = Path(selected)
                self.campaign.set(str(source.parent / (source.stem + '-云端字幕')))

    def _manifest(self):
        if not self.campaign.get().strip():
            return {}
        path = Path(self.campaign.get()) / 'campaign.json'
        if not path.exists():
            return {}
        try:
            value = json.loads(path.read_text(encoding='utf-8'))
            return value if isinstance(value, dict) else {'status': 'invalid'}
        except (OSError, ValueError):
            return {'status': 'invalid'}

    def _refresh(self):
        if not hasattr(self, 'configure_button'):
            return
        report = cloud_settings.readiness()
        self.config_status.set('百炼完整字幕：'+report['message'])
        try:
            sf_ready=siliconflow_ready(siliconflow_environment())
            sf_message='Key与当前每秒价格已配置，可先准备样片。' if sf_ready else '单独配置 Key 并核实当前每秒价格，无需百炼账户。'
        except cloud_settings.ConfigurationRequired as error:
            sf_ready=False
            sf_message=str(error)
        self.siliconflow_status.set('硅基流动试识别：'+sf_message+' 仅比较文字；没有时间戳时不能生成正式字幕或审批整片。')
        manifest = self._manifest()
        status = manifest.get('status', '')
        campaign = Path(self.campaign.get()) if self.campaign.get().strip() else None
        busy = self.process is not None
        enabled = available_actions(status, approval_exists=bool(campaign and (campaign / 'approval.json').is_file()),
                                    ready=report['ready'], busy=busy,siliconflow_ready=sf_ready,
                                    siliconflow_result_exists=bool(campaign and (campaign/'硅基流动识别试听.html').is_file()))
        for action, button in self.buttons.items():
            button.configure(state='normal' if enabled[action] else 'disabled')
        self.configure_button.configure(state='disabled' if busy else 'normal')
        self.siliconflow_configure_button.configure(state='disabled' if busy else 'normal')
        for widget in self.entries:
            widget.configure(state='disabled' if busy else 'normal')
        if not busy:
            labels = {'prepared': '已准备，可以开始样片', 'samples_ready': '样片齐全，等待人工验收',
                      'samples_incomplete': '样片未完成，可检查进度后继续', 'full_ready': '整片字幕完成，等待最终抽检',
                      'full_incomplete': '整片未完成，可检查进度后继续', 'final_reviewed': '最终抽检已确认，可以压制 MP4',
                      'exported': 'MP4 已导出', 'invalid': '项目清单无法读取，请检查 campaign.json'}
            self.status.set(labels.get(status, '尚未准备项目' if not status else str(status)))

    def _append(self, text):
        self.log.configure(state='normal')
        self.log.insert('end', text.rstrip() + '\n')
        self.log.see('end')
        self.log.configure(state='disabled')

    def _launch(self, action, **review):
        if self.process is not None:
            return
        try:
            campaign = self.campaign.get().strip()
            source, baseline = self.source.get().strip(), self.baseline.get().strip()
            if action in ('prepare', 'samples'):
                paths=[('源视频', source)]
                if baseline:
                    paths.append(('原字幕', baseline))
                for label, path in paths:
                    if not path or not Path(path).is_file():
                        raise ValueError(f'请选择存在的{label}文件。')
            command = build_command(action, campaign=campaign, source=source, baseline=baseline, **review)
            values = cloud_settings.environment()
            values.update(siliconflow_environment())
            child_environment = dict(os.environ)
            child_environment.update({name: value for name, value in values.items() if value})
            secrets = tuple(values.get(name, '') for name in ('DASHSCOPE_API_KEY', 'DEEPSEEK_API_KEY','SILICONFLOW_API_KEY'))
            self.process = start_process(command, environ=child_environment)
            self.running_campaign = Path(campaign)
        except (ValueError, OSError):
            messagebox.showerror('无法启动', '文件、项目路径或启动参数无效。请检查选择和配置后重试。', parent=self.root)
            return
        self.status.set('正在运行；进度会显示在下方。')
        action_label = {'prepare': '准备样片', 'samples': '制作样片', 'approve': '记录样片验收',
                        'full': '处理整片', 'accept-final': '记录最终抽检', 'export': '压制 MP4',
                        'siliconflow-pilot':'硅基流动试识别，仅生成试听文字对照'}[action]
        self._append(f'已启动：{action_label}。' + ('本步骤会提交计费请求。' if action in ('samples', 'full', 'siliconflow-pilot') else ''))
        self._refresh()
        process = self.process
        def read_output():
            try:
                for line in process.stdout:
                    message = progress_message(line, secrets)
                    if message:
                        self.events.put(('message', message))
            finally:
                process.stdout.close()
                self.events.put(('done', process.wait()))
        threading.Thread(target=read_output, daemon=True).start()

    def _poll(self):
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == 'message':
                    self._append(value)
                    self.status.set(value)
                else:
                    self.process = None
                    self.running_campaign = None
                    self._append('当前步骤已结束。' if value == 0 else f'当前步骤未完成（退出码 {value}），请查看上方进度和项目记录。')
                    self._refresh()
        except queue.Empty:
            pass
        self.root.after(150, self._poll)

    def _stop(self):
        if self.process is None:
            return
        try:
            request_stop(self.running_campaign)
        except (ValueError, OSError) as error:
            messagebox.showerror('暂时无法停止', str(error), parent=self.root)
            return
        self.status.set('已请求停止。正在发送或处理的请求可能尚未结束；请等待记录保存。')
        self._append(self.status.get())
        self.buttons['stop'].configure(state='disabled')

    def _open_path(self, path):
        path = Path(path)
        if not path.is_file():
            messagebox.showinfo('文件尚未生成', '请先完成对应步骤。', parent=self.root)
            return
        if os.name == 'nt':
            os.startfile(str(path.resolve()))
        else:
            webbrowser.open(path.resolve().as_uri())

    def _review(self):
        self._open_path(Path(self.campaign.get())/'review.html')

    def _siliconflow_review(self):
        self._open_path(Path(self.campaign.get())/'硅基流动识别试听.html')

    def _approve(self):
        if self._manifest().get('status') != 'samples_ready':
            messagebox.showerror('样片未完成', '样片全部完成后才能人工验收。', parent=self.root)
            return
        dialog = tk.Toplevel(self.root)
        dialog.title('样片人工验收')
        dialog.transient(self.root)
        dialog.grab_set()
        frame = ttk.Frame(dialog, padding=20)
        frame.pack(fill='both', expand=True)
        ttk.Label(frame, text='请先观看样片对照，填写你实际检查的结果。', wraplength=460).grid(row=0, column=0, columnspan=2, sticky='w', pady=(0, 12))
        reviewed, timing = tk.StringVar(), tk.StringVar()
        for row, (label, variable) in enumerate((('实际抽检句数（至少 20）', reviewed), ('时间合格句数（至少 90%）', timing)), 1):
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky='w', pady=6)
            ttk.Entry(frame, textvariable=variable, width=12).grid(row=row, column=1, padx=(16, 0))
        content = tk.BooleanVar(value=False)
        ttk.Checkbutton(frame, text='我已检查内容，确认无明显漏译、误译或编造，内容通过', variable=content).grid(row=3, column=0, columnspan=2, sticky='w', pady=12)
        def submit():
            try:
                count, passed = validate_review(reviewed.get(), timing.get(), content.get())
            except ValueError as error:
                messagebox.showerror('不能验收', str(error), parent=dialog)
                return
            dialog.destroy()
            self._launch('approve', reviewed=count, timing_passed=passed, content_passed=True)
        ttk.Button(frame, text='保存我的验收结果', command=submit).grid(row=4, column=0, columnspan=2, sticky='e')

    def _final_review(self):
        dialog = tk.Toplevel(self.root)
        dialog.title('最终抽检确认')
        dialog.transient(self.root)
        dialog.grab_set()
        frame = ttk.Frame(dialog, padding=20)
        frame.pack(fill='both', expand=True)
        ttk.Label(frame, text='请实际观看整片中的不同位置，检查时间、内容、漏句和听不清标记。\n程序不会代替你判断整片质量。', wraplength=470).pack(anchor='w')
        confirmed = tk.BooleanVar(value=False)
        ttk.Checkbutton(frame, text='我已完成最终抽检，确认内容与时间通过', variable=confirmed).pack(anchor='w', pady=16)
        def submit():
            if not confirmed.get():
                messagebox.showerror('尚未确认', '完成最终抽检后再勾选确认。', parent=dialog)
                return
            dialog.destroy()
            self._launch('accept-final', content_passed=True)
        ttk.Button(frame, text='记录我的最终确认', command=submit).pack(anchor='e')

    def _configure_siliconflow(self):
        try:
            values=siliconflow_environment()
        except cloud_settings.ConfigurationRequired as error:
            messagebox.showerror('无法读取本机配置',str(error),parent=self.root)
            return
        dialog=tk.Toplevel(self.root)
        dialog.title('硅基流动试识别：独立配置')
        dialog.transient(self.root);dialog.grab_set()
        frame=ttk.Frame(dialog,padding=18);frame.pack(fill='both',expand=True)
        ttk.Label(frame,text='固定候选：Qwen/Qwen3-ASR-1.7B。先比较同一段原音上的识别文字。\n这不是百炼 Qwen-Audio-3.1；没有时间戳时仍需对齐，不能审批整片。',wraplength=650).pack(anchor='w')
        ttk.Label(frame,text='硅基流动 API Key（无需配置百炼）').pack(anchor='w',pady=(14,4))
        key=tk.StringVar(value=values.get('SILICONFLOW_API_KEY',''))
        ttk.Entry(frame,textvariable=key,show='*',width=65).pack(fill='x')
        ttk.Label(frame,text='识别单价（元/秒，请按当前账户报价核实）').pack(anchor='w',pady=(12,4))
        price=tk.StringVar(value=values.get('SILICONFLOW_ASR_CNY_PER_SECOND') or '0.000220')
        ttk.Entry(frame,textvariable=price,width=30).pack(fill='x')
        ttk.Label(frame,text='报价来源').pack(anchor='w',pady=(12,4))
        reference=tk.StringVar(value=values.get('SILICONFLOW_PRICING_REFERENCE',''))
        ttk.Entry(frame,textvariable=reference,width=65).pack(fill='x')
        confirmed=tk.BooleanVar(value=False)
        ttk.Checkbutton(frame,text='我已核实当前账号 Qwen/Qwen3-ASR-1.7B 每秒人民币价格',variable=confirmed).pack(anchor='w',pady=12)
        ttk.Label(frame,text='API Key 保存到本机加密存储，重启后自动读取；单独保存 Key 不确认价格。\n核价7天内有效，5分钟样片按0.000220元/秒估算为0.066元，费用计入20元总预算，18元停止追加请求。',wraplength=650).pack(anchor='w')
        def save(with_price=False):
            if with_price and not confirmed.get():
                messagebox.showerror('尚未核价','请核实当前账号报价后勾选确认。',parent=dialog);return
            try:save_siliconflow_settings(key.get(),confirmed=with_price,
                price_per_second=price.get(),pricing_reference=reference.get())
            except ValueError as error:
                messagebox.showerror('硅基流动配置未保存',str(error),parent=dialog);return
            dialog.destroy();self._refresh();self._append('硅基流动 API Key 已保存到本机加密存储，重启后自动读取；尚未发起模型请求。')
        ttk.Button(frame,text='仅保存 API Key',command=save).pack(anchor='e',pady=(14,0))
        ttk.Button(frame,text='保存已核实价格与 Key',command=lambda:save(True)).pack(anchor='e',pady=(8,0))

    def _configure(self):
        try:
            values = cloud_settings.environment()
        except cloud_settings.ConfigurationRequired as error:
            messagebox.showerror('无法读取本机配置',str(error),parent=self.root)
            return
        dialog = tk.Toplevel(self.root)
        dialog.title('百炼北京账户与已核实价格')
        dialog.transient(self.root)
        dialog.grab_set()
        frame = ttk.Frame(dialog, padding=18)
        frame.pack(fill='both', expand=True)
        frame.columnconfigure(1, weight=1)
        variables = {}
        labels = [
            ('QWEN_ASR_ENDPOINT', '百炼北京 API 地址'),
            ('DASHSCOPE_API_KEY', '百炼 API Key（北京）'),
            ('DEEPSEEK_API_KEY', 'DeepSeek API Key'),
            ('QWEN_ASR_INPUT_CNY_PER_MILLION', '千问识别输入（元/百万 token）'),
            ('QWEN_ASR_OUTPUT_CNY_PER_MILLION', '千问识别输出（元/百万 token）'),
            ('DEEPSEEK_INPUT_CNY_PER_MILLION', 'DeepSeek 输入（元/百万 token）'),
            ('DEEPSEEK_OUTPUT_CNY_PER_MILLION', 'DeepSeek 输出（元/百万 token）'),
            ('CLOUD_PRICING_VERIFIED_ON', '核价日期（YYYY-MM-DD）'),
            ('CLOUD_PRICING_REFERENCE', '实际核实的报价来源'),
        ]
        ttk.Label(frame, text='语音识别：百炼北京 qwen-audio-3.1-asr-flash；中文翻译：DeepSeek。\n千问参考价为输入 0.8 / 输出 2.7，DeepSeek 为输入 2 / 输出 8（元/百万 token）。\n请核实当前价格后保存；保存只检查本机配置，不发送音频或测试请求。', wraplength=700).grid(row=0, column=0, columnspan=2, sticky='w', pady=(0, 12))
        for row, (name, label) in enumerate(labels, 1):
            default = {'QWEN_ASR_ENDPOINT': 'https://dashscope.aliyuncs.com/api/v1',
                       'QWEN_ASR_INPUT_CNY_PER_MILLION': '0.8',
                       'QWEN_ASR_OUTPUT_CNY_PER_MILLION': '2.7',
                       'DEEPSEEK_INPUT_CNY_PER_MILLION': '2',
                       'DEEPSEEK_OUTPUT_CNY_PER_MILLION': '8'}.get(name, '')
            variable = tk.StringVar(value=values.get(name) or default)
            variables[name] = variable
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky='w', pady=6, padx=(0, 14))
            ttk.Entry(frame, textvariable=variable, width=52,
                      show='*' if name.endswith('_KEY') else '').grid(row=row, column=1, sticky='ew', pady=6)
        confirmed = tk.BooleanVar(value=False)
        row=len(labels)+1
        ttk.Checkbutton(frame, text='我已核实百炼北京地域及千问、DeepSeek 的当前人民币输入/输出价格',
                        variable=confirmed).grid(row=row, column=0, columnspan=2, sticky='w', pady=14)
        ttk.Label(frame, text='API Key 保存到本机加密存储，重启后自动读取；密钥不会写入项目文件或运行日志。\n保存成功仅代表配置完整，不代表账户余额、额度或服务连接已经验证。',
                  style='Note.TLabel', wraplength=700).grid(row=row+1, column=0, columnspan=2, sticky='w')
        def save():
            try:
                save_settings({name: variable.get() for name, variable in variables.items()}, confirmed=confirmed.get())
            except ValueError as error:
                messagebox.showerror('配置未保存', str(error), parent=dialog)
                return
            dialog.destroy()
            self._refresh()
            self._append('API Key 已保存到本机加密存储，重启后自动读取；尚未验证真实服务连接。')
        ttk.Button(frame, text='获取百炼 API Key（官方指南）',
                   command=lambda: webbrowser.open(_KEY_GUIDE)).grid(row=row+2, column=0, sticky='w', pady=(16, 0))
        ttk.Button(frame, text='保存已核实配置', command=save).grid(row=row+2, column=1, sticky='e', pady=(16, 0))

    def _close(self):
        if self.process is not None:
            messagebox.showinfo('请求仍在进行', '请点击“停止后续请求”并等待当前步骤结束，再关闭窗口。', parent=self.root)
            return
        self.root.destroy()


def main(argv=None):
    parser = argparse.ArgumentParser(description='云端日语字幕重做')
    parser.add_argument('--source', default='')
    parser.add_argument('--campaign', default='')
    parser.add_argument('--baseline', default='')
    args = parser.parse_args(argv)
    root = tk.Tk()
    CloudSubtitleApp(root, source=args.source, campaign=args.campaign, baseline=args.baseline)
    root.mainloop()


if __name__ == '__main__':
    main()
