# 字幕工坊桌面界面改造

用户要求先保存 API Key，随后要求更换旧 Tkinter 前端架构。视频字幕基准实测暂后置，原视频与旧任务保持原样。

## 实施范围

- React + Vite 本地静态前端，Python 本机服务复用既有 runner/cloud_workflow；桌面入口打开独立浏览器应用窗口，无云端部署。
- 浅色中文工作台：新建字幕、任务记录、结果预览、API 设置；真实进度、日志和人工验收状态分开。
- Windows DPAPI 加密保存三个受支持服务的 Key。保存后重启可读取，API 不返回密钥值；旧环境变量作为兼容来源，暂不删除。
- Key 保存与模型免费/计价确认分开。保存本身不调用模型；付费操作仍受原 20 元预算、18 元追加线和人工样片门槛约束。
- 仅监听 127.0.0.1 随机端口；启动令牌验证访问，拒绝跨源写入，静态资源无外部 CDN。媒体仅提供当前选择的源文件，下载仅允许项目白名单输出。

## 接口约定

所有 /api 请求由前端 fetch 加 X-Subtitle-Token，来自首次 URL hash 的 token；POST /api/session 设置 HttpOnly SameSite=Strict 会话 cookie 供视频读取。请求为 JSON。

- GET /api/state：source/campaign/baseline，job（busy/action/status/message/total/recognized/translated/logs），accounts（siliconflow/bailian/deepseek 各 configured/storage/ready），settings（公开价格等），recent（path/title/status/updated），local_available。
- POST /api/project：source/campaign/baseline；选择当前任务，返回状态。
- POST /api/pick：kind=source/campaign/baseline；返回 path。
- POST /api/credentials：provider=siliconflow/bailian/deepseek，key，free_confirmed（可选）；只保存合法 Key，可单独更新免费确认。
- POST /api/settings：公开百炼/DeepSeek价格与 endpoint 字段、confirmed；不返回或覆盖 Key。
- POST /api/run：action=local/prepare/siliconflow-pilot/samples/approve/full/accept-final/export；local 参数 language/chunk_seconds/workers/translate；审核参数 reviewed/timing_passed/content_passed。
- POST /api/stop：协作停止当前任务并保留结果。
- GET /api/preview：cues（id/start_ms/end_ms/ja/zh），media_available，source_name，issues；/api/media 仅当前源媒体，支持 Range。
- POST /api/open：target=project/review/siliconflow-review；只打开当前项目对应文件。
- GET /api/download?name=原文.srt/中文草稿.srt/双语草稿.srt：下载当前正式任务字幕，不将试听文字当成SRT。

## 验证

先写失效测试再实现凭据存取、API与操作边界；离线运行完整Python测试，React build及前端功能测试。Windows实际DPAPI使用虚构Key验证新进程读取、磁盘不含明文。检查真实浏览器页面、响应式布局及模拟Key保存后重新加载。测试不调用收费模型，不以UI通过冒充字幕准确率通过。
