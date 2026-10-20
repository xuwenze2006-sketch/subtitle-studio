# 导出保护、外部响应与进度轮询 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 修复已离线复现的文件覆盖、无界响应和轮询阻塞，保留缓存、账本及停止语义。

**Architecture:** 导出在已有暂存和校验边界使用独立临时文件与稳定读取证明；Azure 在账本 send 回调内约束成功及错误正文；轮询复用 StudioController 的 detached read view，把路径解析移出共享锁。

**Tech Stack:** Python 标准库、Windows NTFS、unittest、临时预算账本及线程事件。

**Spec:** 用户要求继续优先优化实际价值最高的地方；持续优化记录累计 7 轮，本批三组独立问题从第 8 轮起计，导出同一文件边界的两处修补合并一轮。完成第 10 轮进行第二次五轮反思。

## Global Constraints

- 基线 bc56fc2，复用现有隔离工作树；不覆盖主工作区或用户数据。
- 只用合成媒体、临时文件、替代传输及离线测试；不请求真实云服务。
- 不完整付费响应保留 unknown 和预算预留，不自动退款或重发。
- 保持字幕格式、旧导出恢复、人工修订与原项目标识的规范路径语义。
- 停止仍是合作式停止，不因轮询修复提前声称工作进程结束。

## Review Focus

- 暂存 SRT、ASS 硬链接或符号链接不能覆盖源文件/字幕或别的已生成文件。
- 哈希期间文件被原位改写或替换，不可复用旧成片证明并报告成功。Windows 同尺寸写入并恢复 mtime 时，st_ctime 是创建时间；须用原生 ChangeTime 补齐已复现边界，查询失败不默默降级。
- Azure 成功与 HTTP 错误均受限；长度头缺失、错误、低报亦不能绕过限额。
- 正文超限应有界读取、关闭响应，账本保守保留请求，不存巨量正文。
- 慢路径解析时 Stop 与 worker progress 可继续；轮询返回当前 job，项目切换不返回旧标识配新状态。

## Tasks

- [x] **导出边界（第 8 轮）** — 修改 subtitle_pipeline/media_export.py 和对应 tests/test_*export*.py；先将审计脚本中的真实硬链接与哈希期间改写转成 RED 测试，再修复临时创建/原子发布、SRT/ASS 转换和成片稳定性证明。复用现有原子 I/O，保留原恢复参数兼容。独立审查又证实 Windows 恢复 mtime 可绕过普通 token，追加原生 FILE_BASIC_INFO.ChangeTime 及真实回归，不增加第二遍整片哈希。定向测试覆盖正常恢复、停止及别名文件字节保留。
- [x] **Azure 正文（第 9 轮）** — 修改 subtitle_pipeline/azure_asr.py 和对应 Azure 测试；使用有界 read，限制成功与 HTTPError 路径。不完整响应以现有账本 unknown 处理。测试上限、低报/缺失长度、边界正常结果及真实临时账本重入不重发。
- [x] **进度读取（第 10 轮）** — 修改 subtitle_pipeline/studio.py 和 tests/test_studio_progress.py；已用 Event 门控真实 Path.resolve 证明 Stop 等待超时。复用 _read_view，最后在锁内刷新 live job/warning，项目变更重试一次。测试慢读、停止、进度、隔离副本和项目切换。
- [x] **本地验证收尾** — 独立审查改动与回归风险，冻结代码后执行完整离线后端及 HTTP 测试，更新报告及五轮反思。前端未变时不重复本机前端验证。

发布收尾：公开扫描、推送、完整 Windows CI 和主工作区同步的结果保存至忽略的 batch3/release-receipt.json；不为写入同一提交的 CI 结果反复生成新提交。

读写及 API 的总截止问题另有离线证据；本批不扩张成全部传输层重构，保留给下一轮独立处理。
