"""Chinese desktop launcher for the resumable subtitle pipeline."""

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import re
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk


_LANGUAGES = {"日语 (ja)": "ja", "英语 (en)": "en", "中文 (zh)": "zh", "自动检测 (auto)": "auto"}


def default_project(source: Path, root: Path | None = None) -> Path:
    """Keep outputs separate for equal basenames from different input folders."""
    source = Path(source).expanduser().resolve()
    short_name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", source.stem).strip(" .")[:48] or "媒体"
    path_hash = hashlib.sha256(str(source).casefold().encode("utf-8")).hexdigest()[:10]
    return (Path.cwd() / "字幕任务" if root is None else Path(root)) / f"{short_name}-{path_hash}"


def project_config_from_state(state: dict) -> dict:
    """Validate and whitelist saved configuration before constructing a job."""
    config = state.get("config") if isinstance(state, dict) else None
    if not isinstance(config, dict):
        raise ValueError("项目状态缺少 config 配置")
    result = {}
    for name in ("source", "project"):
        value = config.get(name)
        if not isinstance(value, (str, Path)) or not str(value).strip():
            raise ValueError(f"项目状态缺少有效的 {name} 路径")
        result[name] = Path(value).expanduser().resolve()
    result["language"] = config.get("language", "ja")
    result["target"] = config.get("target", "zh-CN")
    if result["language"] not in _LANGUAGES.values():
        raise ValueError("本入口支持的原文语言是日语、英语、中文和自动检测")
    if result["target"] not in ("zh-CN", "en", "ja"):
        raise ValueError("翻译目标支持中文、英语和日语")
    for name, default in (("workers", 1), ("threads", 6), ("seed_complete_until_ms", 0)):
        value = config.get(name, default)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{name} 必须是整数")
        result[name] = value
    if result["workers"] not in (1, 2) or not 1 <= result["threads"] <= 32:
        raise ValueError("识别并发数仅支持 1 或 2，线程数须在 1 到 32 之间")
    if result["seed_complete_until_ms"] < 0:
        raise ValueError("已完成识别的截止时间不能小于零")
    for name, default in (("chunk_seconds", 300), ("overlap_seconds", 2)):
        value = config.get(name, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"{name} 必须是有限数值")
        result[name] = float(value)
    if not 0 <= result["overlap_seconds"] < result["chunk_seconds"]:
        raise ValueError("每段时长必须大于上下文时长，且上下文不能为负数")
    result["translate"] = config.get("translate", True)
    if not isinstance(result["translate"], bool):
        raise ValueError("translate 必须是布尔值")
    seed = config.get("seed_srt")
    if seed is not None and (not isinstance(seed, (str, Path)) or not str(seed).strip()):
        raise ValueError("接管字幕路径无效")
    result["seed_srt"] = Path(seed).expanduser().resolve() if seed is not None else None
    return result


class SubtitleApp:
    def __init__(self, window: tk.Tk, args: argparse.Namespace):
        self.window = window
        self.window.title("分段字幕制作 · 本地 small")
        self.window.geometry("940x690")
        self.window.minsize(840, 620)
        self.events = queue.Queue()
        self.worker = None
        self.stop_event = None
        self.busy = False
        self.closing = False
        self._updating_project = False
        self._custom_project = args.project is not None
        self._last_log = None
        self._threads = 6
        self._overlap_seconds = 2.0
        self._seed_srt = args.seed_srt
        self._seed_until = args.seed_until_ms
        self._seed_source = args.source.resolve() if args.source else None
        self.source = tk.StringVar(value=str(args.source.resolve()) if args.source else "")
        initial_project = args.project.resolve() if args.project else (default_project(args.source) if args.source else Path.cwd() / "字幕任务")
        self.project = tk.StringVar(value=str(initial_project))
        self.language = tk.StringVar(value="日语 (ja)")
        self.minutes = tk.StringVar(value="5")
        self.workers = tk.StringVar(value="1（推荐）")
        self.translate = tk.BooleanVar(value=True)
        self.status = tk.StringVar(value="选择视频或音频后，点击“开始 / 继续”。")
        self.counts = tk.StringVar(value="识别 0 / 0    翻译 0 / 0")
        self.form_widgets = []
        self._build_widgets()
        self.source.trace_add("write", self._source_changed)
        self.project.trace_add("write", self._project_changed)
        self.window.protocol("WM_DELETE_WINDOW", self._close)
        self.window.after(100, self._poll)
        if args.autostart:
            self.window.after(250, self._start)

    def _build_widgets(self):
        style = ttk.Style(self.window)
        style.configure("TLabel", font=("Microsoft YaHei UI", 10))
        style.configure("TButton", font=("Microsoft YaHei UI", 10), padding=(9, 5))
        style.configure("Heading.TLabel", font=("Microsoft YaHei UI", 17, "bold"))
        body = ttk.Frame(self.window, padding=20)
        body.pack(fill="both", expand=True)
        body.columnconfigure(1, weight=1)
        ttk.Label(body, text="分段识别，完成一段就开始翻译", style="Heading.TLabel").grid(row=0, column=0, columnspan=4, sticky="w")
        ttk.Label(body, text="保留 small 模型；原文、中文草稿和检查报告逐段保存，可停止后继续。", wraplength=860).grid(row=1, column=0, columnspan=4, sticky="w", pady=(7, 17))
        ttk.Label(body, text="视频 / 音频").grid(row=2, column=0, sticky="w", padx=(0, 12))
        source_entry = ttk.Entry(body, textvariable=self.source)
        source_entry.grid(row=2, column=1, columnspan=2, sticky="ew", pady=6)
        source_button = ttk.Button(body, text="选择文件…", command=self._choose_source)
        source_button.grid(row=2, column=3, padx=(8, 0))
        ttk.Label(body, text="输出项目").grid(row=3, column=0, sticky="w")
        project_entry = ttk.Entry(body, textvariable=self.project)
        project_entry.grid(row=3, column=1, columnspan=2, sticky="ew", pady=6)
        project_button = ttk.Button(body, text="选择目录…", command=self._choose_project)
        project_button.grid(row=3, column=3, padx=(8, 0))
        options = ttk.Frame(body)
        options.grid(row=4, column=0, columnspan=4, sticky="w", pady=(12, 5))
        ttk.Label(options, text="原文语言").pack(side="left")
        language_box = ttk.Combobox(options, textvariable=self.language, values=list(_LANGUAGES), state="readonly", width=15)
        language_box.pack(side="left", padx=(8, 22))
        ttk.Label(options, text="每段约").pack(side="left")
        minutes_entry = ttk.Entry(options, textvariable=self.minutes, width=6)
        minutes_entry.pack(side="left", padx=(8, 4))
        ttk.Label(options, text="分钟").pack(side="left", padx=(0, 22))
        ttk.Label(options, text="同时识别").pack(side="left")
        worker_box = ttk.Combobox(options, textvariable=self.workers, values=["1（推荐）", "2（试验）"], state="readonly", width=11)
        worker_box.pack(side="left", padx=8)
        ttk.Label(body, text="双路识别可能争抢 CPU；一条识别队列也会与翻译并行。", foreground="#555555").grid(row=5, column=0, columnspan=4, sticky="w", pady=5)
        translate_check = ttk.Checkbutton(body, text="自动翻译为简体中文（Google 免费翻译，无需密钥）", variable=self.translate)
        translate_check.grid(row=6, column=0, columnspan=4, sticky="w", pady=(10, 4))
        ttk.Label(body, text="启用后字幕文字将发送到 Google；音频识别在本机完成。译文是草稿，仍需结合原音复核。", foreground="#555555", wraplength=860).grid(row=7, column=0, columnspan=4, sticky="w", pady=(0, 14))
        actions = ttk.Frame(body)
        actions.grid(row=8, column=0, columnspan=4, sticky="ew")
        self.start_button = ttk.Button(actions, text="开始 / 继续", command=self._start)
        self.start_button.pack(side="left", padx=(0, 8))
        resume_button = ttk.Button(actions, text="载入已有项目…", command=self._load_project)
        resume_button.pack(side="left", padx=(0, 8))
        self.stop_button = ttk.Button(actions, text="停止并保留进度", command=self._request_stop, state="disabled")
        self.stop_button.pack(side="left", padx=(0, 8))
        ttk.Button(actions, text="打开结果目录", command=self._open_project).pack(side="right")
        self.form_widgets = [(source_entry, "normal"), (source_button, "normal"), (project_entry, "normal"),
                             (project_button, "normal"), (language_box, "readonly"), (minutes_entry, "normal"),
                             (worker_box, "readonly"), (translate_check, "normal"), (resume_button, "normal")]
        ttk.Separator(body).grid(row=9, column=0, columnspan=4, sticky="ew", pady=15)
        ttk.Label(body, textvariable=self.status, wraplength=860).grid(row=10, column=0, columnspan=4, sticky="w")
        ttk.Label(body, textvariable=self.counts).grid(row=11, column=0, columnspan=4, sticky="w", pady=7)
        self.progress = ttk.Progressbar(body, mode="determinate", maximum=1)
        self.progress.grid(row=12, column=0, columnspan=4, sticky="ew", pady=(0, 10))
        body.rowconfigure(13, weight=1)
        self.log = tk.Text(body, height=8, wrap="word", state="disabled", font=("Microsoft YaHei UI", 10), relief="solid", borderwidth=1)
        self.log.grid(row=13, column=0, columnspan=4, sticky="nsew")

    def _source_changed(self, *_):
        if not self._custom_project and self.source.get().strip():
            self._updating_project = True
            try:
                self.project.set(str(default_project(Path(self.source.get().strip()))))
            except (OSError, ValueError):
                pass
            finally:
                self._updating_project = False

    def _project_changed(self, *_):
        if not self._updating_project:
            self._custom_project = True

    def _choose_source(self):
        name = filedialog.askopenfilename(parent=self.window, title="选择要制作字幕的视频或音频", filetypes=[("视频和音频", "*.mp4 *.mkv *.mov *.avi *.webm *.mp3 *.wav *.m4a *.flac *.aac *.ogg"), ("所有文件", "*.*")])
        if name:
            self.source.set(name)

    def _choose_project(self):
        name = filedialog.askdirectory(parent=self.window, title="选择保存字幕和进度的项目目录")
        if name:
            self.project.set(name)

    def _load_project(self):
        filename = filedialog.askopenfilename(parent=self.window, title="选择字幕项目中的 state.json", filetypes=[("项目状态", "state.json"), ("JSON 文件", "*.json")])
        if not filename:
            return
        try:
            values = project_config_from_state(json.loads(Path(filename).read_text(encoding="utf-8-sig")))
            self._custom_project = True
            self.source.set(str(values["source"]))
            self.project.set(str(values["project"]))
            self.language.set(next(label for label, code in _LANGUAGES.items() if code == values["language"]))
            self.minutes.set(format(values["chunk_seconds"] / 60, ".12g"))
            self.workers.set("1（推荐）" if values["workers"] == 1 else "2（试验）")
            self.translate.set(values["translate"])
            self._threads = values["threads"]
            self._overlap_seconds = values["overlap_seconds"]
            self._seed_srt = values["seed_srt"]
            self._seed_until = values["seed_complete_until_ms"]
            self._seed_source = values["source"]
            self.status.set("已载入项目。点击“开始 / 继续”复用已保存的结果。")
            self._append_log(f"已载入：{filename}")
        except (OSError, ValueError, TypeError) as exc:
            messagebox.showerror("无法载入项目", str(exc), parent=self.window)

    def _build_config(self):
        from .runner import PipelineConfig
        if not self.source.get().strip() or not self.project.get().strip():
            raise ValueError("请选择源文件和输出项目目录")
        source = Path(self.source.get().strip()).expanduser().resolve()
        if not source.is_file():
            raise ValueError("源文件不存在，请重新选择视频或音频")
        project = Path(self.project.get().strip()).expanduser().resolve()
        if project.exists() and not project.is_dir():
            raise ValueError("输出项目必须是目录，不能是已有文件")
        try:
            seconds = float(self.minutes.get()) * 60
        except ValueError as exc:
            raise ValueError("每段分钟数须填写数字，例如 5") from exc
        if source != self._seed_source:
            self._seed_srt, self._seed_until = None, 0
        values = project_config_from_state({"config": {
            "source": source, "project": project, "language": _LANGUAGES[self.language.get()],
            "target": "zh-CN", "workers": int(self.workers.get()[0]), "threads": self._threads,
            "chunk_seconds": seconds, "overlap_seconds": self._overlap_seconds,
            "translate": self.translate.get(), "seed_srt": self._seed_srt,
            "seed_complete_until_ms": self._seed_until,
        }})
        return PipelineConfig(**values)

    def _set_busy(self, busy):
        self.busy = busy
        for widget, enabled_state in self.form_widgets:
            widget.configure(state="disabled" if busy else enabled_state)
        self.start_button.configure(state="disabled" if busy else "normal")
        self.stop_button.configure(state="normal" if busy else "disabled")

    def _start(self):
        if self.busy or self.closing or (self.worker and self.worker.is_alive()):
            return
        try:
            config = self._build_config()
        except (OSError, ValueError, KeyError) as exc:
            messagebox.showerror("请检查任务设置", str(exc), parent=self.window)
            return
        self.stop_event = threading.Event()
        self._set_busy(True)
        self.status.set("正在检查项目并准备任务…")
        self._append_log(f"开始 / 继续：{config.source.name}")

        def run():
            try:
                from .runner import run_pipeline
                result = run_pipeline(config, stop_event=self.stop_event,
                                      on_progress=lambda update: self.events.put(("progress", dict(update))))
                self.events.put(("done", result))
            except Exception as exc:
                self.events.put(("error", f"{type(exc).__name__}: {exc}"))

        self.worker = threading.Thread(target=run, name="subtitle-pipeline", daemon=True)
        self.worker.start()

    def _request_stop(self):
        if self.busy and self.stop_event:
            self.stop_event.set()
            self.stop_button.configure(state="disabled")
            self.status.set("正在停止本任务；已完成的片段会保留，请等待退出。")
            self._append_log("已请求停止，等待当前操作安全结束。")

    def _append_log(self, message):
        if not message or message == self._last_log:
            return
        self._last_log = message
        self.log.configure(state="normal")
        self.log.insert("end", str(message) + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _poll(self):
        for _ in range(100):
            try:
                kind, data = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "progress":
                total = max(0, int(data.get("total", 0)))
                recognized = max(0, int(data.get("recognized", 0)))
                translated = max(0, int(data.get("translated", 0)))
                self.counts.set(f"识别 {recognized} / {total}    翻译 {translated} / {total}" if self.translate.get()
                                else f"识别 {recognized} / {total}    自动翻译未启用")
                self.progress.configure(maximum=max(1, total * (2 if self.translate.get() else 1)),
                                        value=recognized + (translated if self.translate.get() else 0))
                message = data.get("message", "")
                if message and not (self.stop_event and self.stop_event.is_set()):
                    self.status.set(message)
                self._append_log(message)
            elif kind == "done":
                self._set_busy(False)
                result_status = data.get("status", "") if isinstance(data, dict) else ""
                if self.stop_event and self.stop_event.is_set() or result_status in ("cancelled", "stopped"):
                    message = "任务已停止，已保存片段可继续使用。"
                elif result_status in ("complete", "completed", "done"):
                    message = "任务完成。请打开结果目录，并结合原音复核字幕草稿。"
                else:
                    message = "本轮处理已结束；请查看结果和报告，未完成内容可继续重试。"
                self.status.set(message)
                self._append_log(message)
            elif kind == "error":
                self._set_busy(False)
                self.status.set("任务未完成；已保存结果仍保留。请查看下面的错误信息。")
                self._append_log(data)
                if not self.closing:
                    messagebox.showerror("任务未完成", data, parent=self.window)
        if self.closing and not (self.worker and self.worker.is_alive()):
            self.window.destroy()
            return
        self.window.after(100, self._poll)

    def _open_project(self):
        path = Path(self.project.get()).expanduser().resolve()
        if not path.is_dir():
            messagebox.showinfo("尚无结果目录", "任务开始后会创建项目目录。", parent=self.window)
            return
        try:
            os.startfile(str(path))
        except OSError as exc:
            messagebox.showerror("无法打开目录", str(exc), parent=self.window)

    def _close(self):
        if self.closing:
            return
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno("停止任务并关闭？", "任务正在运行。是否停止本任务并关闭窗口？\n已完成的片段和翻译缓存会保留，下次可以继续。", parent=self.window):
                return
            self.closing = True
            self._request_stop()
        else:
            self.window.destroy()


def main(argv=None):
    parser = argparse.ArgumentParser(description="分段字幕制作中文桌面入口")
    parser.add_argument("--source", type=Path)
    parser.add_argument("--project", type=Path)
    parser.add_argument("--seed-srt", type=Path)
    parser.add_argument("--seed-until-ms", type=int, default=0)
    parser.add_argument("--autostart", action="store_true")
    args = parser.parse_args(argv)
    window = tk.Tk()
    SubtitleApp(window, args)
    window.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
