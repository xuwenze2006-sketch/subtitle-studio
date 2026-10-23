# 原文项目读取优化 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 原文预览不为缺失成片做完整校对，资格布尔查询不生成未使用的交付字幕。

**Architecture:** 保留一个完整输入验证入口；以私有选项区分资格与交付末段。成片查询先检查固定回执，只让候选进入完整验证，不增加缓存。

**Tech Stack:** 现有Python标准库/单测和既有前端工具，无新依赖。

**Spec:** `docs/superpowers/specs/2026-10-23-source-preview-work-design.md`

## Global Constraints

- 不改绑定、状态格式、云端导出、共享编码/发布及UI契约。
- 所有资格门槛、实际导出和候选媒体证明照常核验；没有跨请求缓存。
- 固定回执路径经现有链接/目录包含保护；不接触真实项目、凭据、预算或提供商。
- 素材、人工记录、既有视频及partial exports保留；复用已有隔离工作区。

## Review Focus

- 回执缺失、损坏或非候选时不能触发完整校对读取（任务1）。
- 有效候选不能因提前检查而绕过快照、所有权或媒体摘要（任务1）。
- 资格查询不能在人工记录丢失/所有权冲突检查之前提前成功（任务1）。
- 前一次成功不能遮住下一请求的外部字幕或校对变化（任务1）。
- 实际导出仍产生完整字幕/修订绑定，状态轮询继续不持主控制锁做I/O（任务1、2）。

## Task 1: 输入资格与负路径

**Files:** 修改 `subtitle_pipeline/source_video_export.py`；新增 `tests/test_source_video_read_work.py`。复用已有测试模块的临时项目夹具，不继承其TestCase以免重复发现测试。

**Interfaces:** `source_export_available(project,source)`、`current_source_video(project,source,*,video_matches)`、导出CLI及返回结构不变；仅 `_inputs` 增加私有交付构造选项，默认保持完整行为。

- [x] 写并运行缺失/损坏/非候选回执禁止读取校对、资格禁止SRT渲染及快照路径构造的失败回归。
- [x] 最小实现固定回执负路径前置与资格末段跳过；不跳过完整校对/所有权检查。
- [x] 覆盖缺失人工记录、外部变化、有效候选损坏及完整实际导出；运行新模块和既有原文导出/Studio相关回归。

## Task 2: 工作量与完整交付

**Files:** 持续优化记录、`docs/验证报告/2026-10-23/持续优化第十六批.md`；全部探针/摘要/日志留在忽略的batch16证据目录。操作步骤没有变化，使用指南无需改写。

**Interfaces:** 任务1 API兼容旧Studio/前端，沿用原探针及测试命令；原探针只写新的结果文件，修改前证据保留。

- [ ] 独立整分支审查，并对重要发现执行RED→GREEN；冻结最终源码摘要。
- [x] 重新运行相同离线探针，比较读取/验证/渲染次数、返回能力及缺失成片行为；保留计时范围和主锁I/O检查。
- [ ] 完整后端、HTTP、前端和生产构建通过；源码/构建/发布内容扫描核对。
- [ ] 更新第32轮报告及成本边界，普通验证分支推送、同提交CI通过后快进main并同步干净主目录；目标保持active。
