# 素材消费生命周期保护计划

> **For agentic workers:** Use superpowers:executing-plans and test-driven-development for the steps below.

**Goal:** Windows 任务实际读取的素材与本次身份哈希始终属于同一份不可写入的文件内容，避免新内容错误复用旧身份。

**Architecture:** 在项目独占锁内、读取身份前取得现有 `stable_read_guard(source)`，一直持有到 `_run_locked` 返回或抛错，覆盖探测、切点、全部本地提取和云端时间线生成。云端样片 `prepare` 在现有参数检查之后也保护整个准备过程，包括新建、恢复校验、时间线和稍后的源视频预览。它允许普通读者及 FFmpeg，不复制大文件，也不逐片重复完整哈希。导入字幕继续使用已核验的内存快照。

**Tech Stack:** Python、现有 Windows CreateFileW 共享读句柄、unittest、离线合成 FFmpeg 对照。

**Spec:** `docs/superpowers/specs/2026-09-28-subtitle-pipeline.md` 的输入身份与跨输入禁止混用要求。

## Constraints and tradeoffs

- identity、prepare、分段证据、预算及已完成结果的格式保持不变。
- 普通 Windows 写入、替换和删除在任务期间被拒绝；需要编辑素材时先停止任务。完整任务持有一个句柄，避免调度器与阶段释放耦合。
- 取消和异常均释放源句柄与项目锁；不延长 seed 文件保护，不增加完整内容读取。
- POSIX 仍沿用现有行为，不宣称相同的写入隔离。父目录恶意重绑定及绕过 Windows 共享语义不在此保证中。
- 旧版本已经错误生成的结果无法由新版本反向证明正确，此修复保护新执行，不能凭空补出历史证据。

## Steps

- [x] 用准备、探测、识别与收尾的真实 Windows 写入/替换尝试编写失败测试，同时覆盖正常继续、取消、异常释放和已有写者拒绝。
- [x] 运行测试并保存失败证据；仅在 `run_pipeline` 外层添加保护，调整保护助手的范围说明。
- [x] 独立复现并补齐云端样片 `prepare` 同类入口，用薄 wrapper 保持原有参数与补丁边界，覆盖新建、恢复和后续预览。
- [x] 运行相关回归及真实 FFmpeg 合成音频读取对照，确认无额外源文件内容扫描。
- [x] 完成独立审查、完整离线后端及 HTTP 检查，记录收益与限制；实际发布结果以本批发布回执为准。
