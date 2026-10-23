# 原文字幕校对 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** 让原文任务在应用内纠错、检查并交付原文字幕，减少工具切换和翻译需求。

**Architecture:** 现有人工校对新增明确的 v2 原文模式，保留 v1 字节与调用契约。存储层、Studio 集成和前端按同一模式字段衔接，沿用草稿与回执。

**Tech Stack:** Python 标准库、React/Vite/Vitest；不增加依赖。

**Spec:** `docs/superpowers/specs/2026-10-23-source-only-review-design.md`

## Global Constraints

- v1 JSON 字节、修订、binding 和四文件快照不变；不加载时迁移。
- v2 仅明确关闭翻译且实际无译文/双语轨时建立；已有记录损坏或模式变化保留并报告。
- v2 模式为 `source_only`，`acceptance_supported=False`，不伪造译文、不调用提供商。
- 不修改真实项目、账户、凭据、费用账本；复用现有干净隔离工作区。
- 提交/发布由根代理统一执行，实施子代理只做分配的源码和定向验证。

## Review Focus

- 自动检测原音在新模式下可校对，旧双语校验不放宽（任务1/2）。
- `translate=false` 但缓存译文存在，或翻译任务译文缺失，不误判原文（任务1/2）。
- 紧凑保存回执必须保持原文模式和能力，切页面后仍可下一条（任务2/3）。
- 旧双语草稿包含非空译文，模式变化后能看到和保留，不能隐藏提交（任务3）。
- 全部原文 checked 仍不可成为双语最终验收，物化无译文文件（任务1/2/3）。

## Task 1: 存储及物化（独立实施）

**Files:** `subtitle_pipeline/manual_review.py`; 新增 `tests/test_manual_review_source_only.py`。

**Interfaces:** 沿用三个公共函数；v2 返回 `mode='source_only',acceptance_supported=False`，展示 cue 的译文字段为空/false；v1 返回不变。

- [x] 写并运行失败回归：`test_source_only_load_does_not_write`、`test_source_only_edit_and_materialize_preserve_machine_bytes`、`test_v1_bytes_revision_and_snapshot_unchanged`；先捕获当前 v1 基线。
- [x] 实现 v2 资格、schema、validation、稀疏 patch、CAS、快照和严格 accepted 拒绝；保持 v1 原样。
- [x] 覆盖 auto、缓存译文、缺失/损坏记录、模式切换、并发、底稿变化、快照精确文件集及隐藏译文请求。
- [x] 运行新模块和既有 manual-review/cache 定向回归，记录 RED/GREEN/最终摘要后冻结；由独立代理复核。

## Task 2: Studio 与原文状态（根代理）

**Files:** `subtitle_pipeline/studio.py`, `subtitle_pipeline/runner.py`; 新增 `tests/test_studio_source_only_review.py`，复用原文 runner 定向测试。

**Interfaces:** 消费任务1公共函数；预览和 `review-cue` 两类返回传递 v2 模式/能力；不更改请求结构。

- [x] 先写失败集成：原文预览→保存→紧凑回执→重启→下载→版本副本，内容与同一修订一致，无译文或机器文件变化。
- [x] 连接原文资格、冲突保护、准确下载轨和版本清单；保留损坏双语与既有记录错误通路。
- [x] 原文完成/外部原文修改使用准确提示；双语提示回归，错误配置不扩大成原文资格。
- [x] 定向 Studio/runner/人工校对回归后冻结；与任务1最终状态集成并审查。

## Task 3: 原文交互（独立实施）

**Files:** `frontend/src/Preview.jsx`, `ManualReview.jsx`，必要的现有回执 helper/页面说明；新增 `PreviewSourceOnlyReview.test.jsx`，扩展相应现有测试。

**Interfaces:** `manual_review.mode==='source_only'` 激活；旧响应缺字段按现有双语行为；`acceptance_supported===false` 禁用验收语义。

- [x] 先写失败回归：原文可编辑并直接 checked，保存请求空译文/false确认；紧凑回执与下一条保持模式，普通双语行为不变。
- [x] 隐藏译文/翻译确认/验收及不可达 MP4 引导，原文语言标记和提示准确。
- [x] 旧非空译文草稿在模式变化时以恢复稿保留、可复制、阻止写入；无自动丢弃/重发/播放。
- [x] 运行相关前端回归后冻结，独立审查模式和草稿边界。

## Task 4: 综合验收、反思与发布（根代理）

- [x] 实施完成后冻结全部源码，运行完整后端、前端、HTTP 与生产构建；检查新内置资源一致。
- [x] 实际浏览器以独立合成原文项目完成编辑、保存、切页、下载、版本和重启恢复；保留机器字节证据。
- [x] 独立审查最终整合；修正真实问题后重跑受影响验证，避免无新证据的重复全量执行。
- [x] 更新使用指南、持续优化记录和第30轮反思：收益、复杂度/验证成本、未解决项；不把审计/测试数计为额外产品轮次。
- [ ] 精确允许路径提交，发布扫描及同提交 CI 成功后同步主工作区；按用户最新明确选择，在本轮收尾后暂停持续目标，等待恢复。

发布步骤在本计划提交时保留待核对；同一提交 CI、主目录同步与暂停的收尾回执保存在忽略的验证目录，不通过额外文档提交改变已验证的提交身份。
