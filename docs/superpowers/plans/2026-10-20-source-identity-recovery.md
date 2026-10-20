# 来源时间戳变化后的任务恢复 Implementation Plan

> **For agentic workers:** 使用 superpowers:executing-plans 按本计划执行。主控已授权本分工直接实施，不另建代理或提交。

**Goal:** 第 12 轮：完整内容未变时允许原任务继续，仍拒绝真实内容、路径或任务配置变化。

**Architecture:** 将已有 Windows ChangeTime 与稳定 stat token 算法提取到不依赖 runner 的 `file_observation.py`；导出保留兼容包装。runner 每次只完整哈希一次，并以前后稳定观察确认本次读取，再比较除来源修改时间以外的严格任务身份；不改写已有 state.identity，保留输出恢复 journal 的指纹。

**Tech Stack:** Python unittest、临时合成文件、现有 ctypes Win32 ABI；不新增依赖。

**Spec:** 主控本轮任务授权；反例及对照见 `output/验证记录/2026-10-20/continuous-optimization-batch4/legacy-audit.txt`。

## Global Constraints

- 仅修改 runner.py、media_export.py，新增 file_observation.py、tests/test_runner_identity_recovery.py 与本计划；其余文件由主控/其他分工持有。
- 不读真实凭据、账本或用户媒体，不访问外网或真实提供商，不跑全套、不提交、不迁移旧 engine 缓存。
- task 的 source 与 seed 只忽略 mtime_ns，path/size/sha256 和其他字段全部保持严格比较；完整 SHA 不可被元数据缓存替代。
- 读取中变化或原生观察失败时拒绝；已有停止语义保留；不增加第二遍媒体哈希。

## Review Focus

- 既有 state.identity 与 pending_outputs.identity 指纹必须逐值保留。
- 等长写入并恢复 mtime 在读取中仍须被 Windows ChangeTime 检出。
- 原生观察失败、停止与异常路径不得泄露文件描述符或覆写状态。
- prepare.json 恢复必须遵守相同内容身份规则。
- 导出兼容包装必须保留既有变化错误及关闭描述符行为。

## Task 1: 严格内容身份与稳定读取

**Files:** 新增 `tests/test_runner_identity_recovery.py`、`subtitle_pipeline/file_observation.py`；修改 `subtitle_pipeline/runner.py:257-269,572,581`、`subtitle_pipeline/media_export.py:345-384`。

**Interfaces:** `file_observation.file_token(path, *, change_time=None)` 返回原五项 stat token（Windows 追加 ChangeTime）；`windows_file_change_time(descriptor)` 保留原 ABI。runner `_task_identity_matches(saved,current)` 仅在 source/seed 文件字典移除 mtime_ns 后比较整个 task 字典。

- [x] 写正常零请求继续、source/seed 仅时间变化、内容/路径/语言变化拒绝、prepare 恢复、pending_outputs 指纹保持，以及单次完整读取、读取中改写/替换、停止和原生查询失败用例。
- [x] 运行新测试，记录 `identity-red.log`，确认时间变化与读取竞态为真实 RED，正常与内容变化对照已通过。
- [x] 提取现有观察代码，保留 media_export 两个私有包装；file_identity 在一次完整哈希前后观察，取消优先；state/prepare 入口调用严格内容比较。
- [x] 运行新测试并记录 `identity-green.log`；运行 runner、来源检查、prepare、云端集成和全部导出相关专项，记录 `identity-regression.log`。
- [x] 自审范围、指纹兼容、异常/描述符释放与 git diff --check，回报主控，不提交。

## Decisions / Verification

- 主控裁决：禁止 runner 反向依赖 media_export；第二个真实调用方驱动抽取底层观察模块，不复制 native 实现。
- file_identity 原实现只有哈希前 stat。新实现保留 stat/mtime 作为本次读取稳定性观察，不再把跨次 mtime 变化视为内容变化。
- 本分工的验证范围由主控限定为专项，完整集成与全套验证由主控统一执行。
- 原 runner 在成功创建 state 后仍保留 prepare.json。专项最初额外假设其删除，经读回原行为后改为断言原 prepare 字节保留、state 成功及后续零请求继续，不新增清理行为。
