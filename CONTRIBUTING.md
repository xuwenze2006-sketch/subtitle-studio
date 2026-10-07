# 开发与验证

## 环境与联调

使用 Windows、Python 3.14（含 Tcl/Tk）和 Node.js 24.19；支持的 Node 范围写在 `frontend/package.json`。后端没有第三方 Python 包依赖。可在仓库根目录创建 `.venv`，避免本机 Python 环境干扰。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe tests/run_offline.py
.\.venv\Scripts\python.exe tests/studio_http_smoke.py
cd frontend
npm ci
npm test -- --reporter=dot
npm run build
```

返回根目录运行 `.\.venv\Scripts\python.exe -m subtitle_pipeline.studio`。后端直接提供 `frontend/dist`，每次修改前端后重新构建；修改后端后重新启动服务。Vite 开发服务器尚无认证代理，不用于完整流程验证。

## 改动约束

- 使用临时目录、模拟凭据和合成字幕复现缺陷；不要把个人素材、账本、原始模型响应或真实 Key 加进夹具。
- 先复现再修复；回归应验证真实故障与用户可见结果，尤其是取消、未知提交、磁盘失败和多个窗口交错。
- 已发送写入和付费请求不能因为浏览器读取超时而自动重试。恢复必须沿用原请求身份、证据和费用账本。
- 人工字幕与审核记录不能被自动重建覆盖；源内容变更应明确报告冲突。
- 前端源码、锁文件及 `frontend/dist` 同步提交。不要只提交构建文件，也不要遗漏其依赖的新 Python 模块。

## 验证范围

`python tests/run_offline.py` 默认拦截外网 socket。少量 HTTP 用例只连接各自临时启动的 `127.0.0.1` 服务。`tests/studio_http_smoke.py` 使用临时凭据目录检查 Windows DPAPI、会话、Range 和跨域拒绝，不调用识别服务。

`npm test` 的默认单 worker 模式降低内存消耗。若整机资源不足导致超时，保留失败日志并先停止自己启动的并行验证，区分调度压力和真正的异步竞态；不要把一次失败隐藏为自动重试成功，也不要通过放宽产品断言掩盖问题。

需要检查真实压制时，可显式运行 `python tests/media_smoke.py`。它生成合成素材，需要 FFmpeg/libass 与 Intel QSV；结果位于忽略的 `验证样例/`。通过离线测试不证明真实服务质量、当前报价、人工审核结果或特定硬件可用。

完整的 CPU/QSV、H.264/HEVC、多音轨、字幕像素和取消恢复检查使用 `python tests/media_acceptance.py --encoders cpu,qsv`；仅 CPU 可传 `--encoders cpu`。该工具生成离线语音合成素材，拒绝覆盖或写出 `验证样例/`，每轮保留独立报告。它不加入日常离线回归；浏览器操作和人耳听看应另外记录。响应基准为 `python tests/studio_review_benchmark.py`，比较 5,000 / 20,000 句的全量/小回执并核对最终视图等价。

`media_export.py` 负责媒体压制、编码身份、音轨校验与发布恢复；`cloud_workflow.py` 通过明确的依赖接口提供任务状态和字幕验收。`environment.py` 负责有时限的离线探测。校对草稿仅序列化白名单字段，跨窗口不得清除另一窗口新写入的草稿；默认浏览器端口记录只保存端口，不保存会话令牌。

提交前运行 `git diff --check`，核对完整暂存清单，并在独立候选目录重新执行测试和构建。CI 的构建一致性检查同时检查已跟踪变更和新增未跟踪资产，避免遗漏带新哈希名的文件。

依赖更新使用 npm 锁文件；评估 `npm audit` 的实际受影响路径，禁止盲目 `npm audit fix --force`。GitHub Actions 固定到完整提交 SHA，更新由 Dependabot 提议、回归后再合入。
