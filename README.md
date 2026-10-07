# 字幕工坊

Windows 本机字幕工作台：分段识别、双语翻译、断点恢复、人工校对和视频导出。React 界面由 Python 后端提供，仅监听 `127.0.0.1`。

支持日语、英语和中文的六种双语组合，也可只生成原文字幕。本地 Whisper 识别不上传音频；云端识别、翻译需要联网，会把选定音频或字幕发送给对应服务。机器生成的结果始终需要人工核对。

## 安装与启动

当前支持 Windows 10/11 x64，验证环境为 Python 3.14、Node.js 24.19。后端只用 Python 标准库，不需要安装 pip 依赖。

1. 安装 **Python 3.14 x64（包含 Tcl/Tk）**，确认 `python --version` 与 `python -c "import tkinter"` 可用。
2. 安装 FFmpeg，将包含 `ffmpeg.exe` 和 `ffprobe.exe` 的目录加入 `PATH`。确认 `ffmpeg -version`、`ffprobe -version` 可用。字幕压制需要 FFmpeg 的 `subtitles` / libass 支持。
3. 下载或克隆完整源码，进入仓库根目录，运行：

```powershell
python -m subtitle_pipeline.studio
```

仓库包含 `frontend/dist`，正常启动不需要 Node.js。启动会打开桌面浏览器窗口；也可用 `--no-browser` 仅启动后端。关闭页面后，正在进行的任务会继续运行；空闲服务会自动退出。已有服务运行时再次启动会连接原服务。默认启动优先复用上次浏览器端口，让同一浏览器的校对草稿可在服务重启后恢复；端口被占用时会使用新端口并提示。

可指定初始素材和任务目录：

```powershell
python -m subtitle_pipeline.studio --source "D:\Videos\example.mp4" --campaign "D:\Subtitles\example"
```

更新后端代码后，先完成或停止当前任务，再退出旧服务并重新启动。仅刷新页面不会加载后端修改。

### 识别与导出的额外要求

- **本地识别：** 将 `whisper-cli.exe`、配套 DLL 和 small 模型安装到 `%LOCALAPPDATA%/SubtitlePipeline/runtimes/whisper.cpp/`；模型文件位置为 `Models/small.bin`。兼容原 Subtitle Edit 安装目录。不附带或自动下载引擎、模型。仅做云端识别时不需要 Whisper。
- **云端服务：** 在“API 设置”中配置所用服务的 Key，并核实模型、地域及当前价格。参见[云端配置](docs/云端字幕配置与使用.md)。保存 Key 不发送收费请求。
- **MP4 导出：** 页面自动检查 FFmpeg、字幕滤镜及编码器的实际可用性。“视频导出编码方式”可选自动、Intel QSV 或 CPU；自动模式先短编码探测 QSV，不可用时选择 CPU `libx264`。CPU 通常较慢。编码失败后不会自动再跑另一遍；字幕生成、校对和 SRT 下载不需要 QSV。

## 日常使用

1. 选择视频，创建任务并选择识别模式及字幕语言。
2. 准备样片，在实际听看、核对字幕后验收，再继续整片；也可以明确选择先生成未审核整片草稿。
3. 在校对台逐句定位、修改文字和时间、标记疑点。未保存输入会在当前浏览器保留本机草稿，刷新后可恢复；正式保存仍需点击按钮。其他窗口的旧版本不能覆盖新版本。
4. 下载 SRT 或保存独立字幕版本。MP4 分为“未审核草稿”和“审核版”，审核版需要明确人工验收。
5. 中断后选择原任务继续。保留原目录、账本、原始响应和锁文件；未知提交状态不会自动重发付费请求。

完整行为与限制见[使用指南](docs/使用指南.md)。旧 Tkinter 入口 `python -m subtitle_pipeline.cloud_gui` 和 `python -m subtitle_pipeline.gui` 继续保留。

## 数据与安全

| 位置 | 内容 |
| --- | --- |
| `subtitle_pipeline/`、`frontend/src/` | 后端与前端源码 |
| `frontend/dist/` | 随源码发布的前端构建 |
| `tests/` | 离线回归与本机验证工具 |
| `字幕任务/` | 字幕、人工记录、原始响应、断点、费用账本 |
| `输入文件/`、`输出文件/` | 可选的个人素材与交付目录 |
| `output/`、`验证样例/`、`输入缓存/` | 本机验证、历史资料与缓存 |
| `%LOCALAPPDATA%/SubtitlePipeline/` | 加密凭据、窗口状态与本机运行记录 |

素材、任务、账本及本机记录均不纳入 Git。API Key 使用当前 Windows 用户的 DPAPI 加密，接口不返回原值；不要分享 `credentials.json`、`runtime.json` 或带 `#token=` 的启动链接。服务面向单用户本机场景，不支持监听局域网或直接部署到公网。

费用账本记录本程序的消费估算和在途预留，不是服务商账户的硬性上限，也无法统计其他程序的费用。修改素材、语言或模型请创建新任务；不要手工清理运行中的请求锁。

## 开发与验证

开发前端需要 **Node.js 24.15–24.x**，锁定依赖由 `npm ci` 安装：

```powershell
python tests/run_offline.py
python tests/studio_http_smoke.py
cd frontend
npm ci
npm test -- --reporter=dot
npm run build
```

完整界面联调使用构建后的页面和 Python 服务。`npm run dev` 仅启动 Vite，没有配置本机认证接口代理。

GitHub Actions 在 Windows 上运行离线后端测试、本机 HTTP / DPAPI 检查、前端测试与构建，并检查 `dist` 是否与源码一致。云端真实识别、人工字幕质量和 QSV 媒体压制不包含在自动回归内。

维护方式见 [CONTRIBUTING](CONTRIBUTING.md)，安全边界见 [SECURITY](SECURITY.md)，验证记录见[重点深度优化](docs/验证报告/2026-10-07/重点深度优化.md)、[五项优化验证](docs/验证报告/2026-10-07/全部优化.md)与[真实媒体验收](docs/验证报告/2026-10-07/真实媒体验收.md)。个人迁移记录及一次性修复脚本保留在本机，不是发行工具。
