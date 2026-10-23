# 原文 MP4 导出 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** 原文任务可从现有界面导出当前保存字幕的未审核 MP4，验证后发布 GitHub。

**Architecture:** 共享现有安全渲染与发布，独立本地原文输入/回执，沿用 export-draft 操作与认证成片入口。根代理集成 Studio；后端渲染和前端按固定能力契约可独立实施。

**Tech Stack:** Python 标准库、既有 FFmpeg、React/Vite/Vitest；不新增依赖。

**Spec:** `docs/superpowers/specs/2026-10-23-source-only-mp4-design.md`

## Global Constraints

- 不调用提供商，不修改真实项目/账户/费用；保留旧双语 v1 字节/渲染/验收行为。
- 原文导出未审核，原视频和全部音轨保留，禁止未知文件覆盖；记录与流水线状态分离。
- 项目锁、全程输入保护、实际来源重哈希、片段证据、修订/底稿复核和路径包含性不放宽。
- 重复同一绑定复用；修改后保留历史且产生独立成片。缺失人工记录不静默降级。

## Review Focus

- 自动检测原音与旧有效外部原文修改，不能强制新识别或丢失用户字幕（任务1）。
- 空 patch 记录丢失及最终登记失败，不能用虚拟默认版本认领旧成片（任务1）。
- 准备检查后状态/字幕/源文件变化，以及目录链接、未知视频碰撞，不能发布过期或越界结果（任务1）。
- 动作计算期间 job 改变，刷新仍须禁用运行中或已失效的资格（任务2）。
- 原文回执跨布局/页面与恢复草稿，不能重新开放旧字幕导出（任务3）。

## Task 1: 共享渲染和本地原文输入（独立实施）

**Files:** `subtitle_pipeline/media_export.py`; 新增 `subtitle_pipeline/source_video_export.py`, `tests/test_source_video_export.py`，必要的后端专属导出回归。

**Interfaces:** 上述 spec 三个公共函数及模块 CLI；旧云端导出函数签名不变。当前视频查询由 Studio 提供 `video_matches(path,digest)`。

- [x] 写并运行失败回归：真实形状本地完成状态→原文快照→渲染/媒体检查→独立回执，机器字节和旧状态不变；复现新功能缺口。
- [x] 抽取共享渲染，建立完整原文资格/证据、锁、快照、输入键、版本路径、记录存在性及提交重检，保留旧云端行为。
- [x] 覆盖无记录/人工修订/auto/外部修改、译文污染/未完成/损坏证据、缓存/新版本/碰撞、停止/发布登记失败/恢复及路径边界。
- [x] 运行新模块和既有导出/多音轨/证据/取消定向回归，保存报告与哈希后冻结，不运行整套或 Git 变更。

## Task 2: Studio 原文动作和成片（根代理）

**Files:** `subtitle_pipeline/studio.py`; 新增 `tests/test_studio_source_video_export.py`。

**Interfaces:** 消费任务1三函数，合格本地 `export-draft` 生成独立模块命令，预览仅 main 返回 `source_export_supported=True`，成片沿用 `draft_video` 与既有认证路由。

- [x] 先写失败回归：原文资格不依赖账户/识别引擎可用；正确命令和编码参数；旧云端命令不变。
- [x] 集成动作重算、预览能力、当前视频/目录/下载及状态重启；保持控制请求不受媒体哈希锁阻塞。
- [x] 覆盖主项目/片段、未完成/损坏/双语及 job 竞争；新修订撤下旧片入口，保留文件。
- [x] 定向 Studio/前序原文校对回归通过并冻结。

## Task 3: 原文导出交互（独立实施）

**Files:** `frontend/src/Preview.jsx` 和必要的 `Workspace.jsx` 提示；新增 `PreviewSourceVideoExport.test.jsx`，仅按行为更新原文旧测试。

**Interfaces:** 严格使用 `source_export_supported===true`；继续 `run('export-draft')`、`draft_video` 和现有保存回执。不开放双语最终验收。

- [x] 写并运行失败测试：两布局可导出、无新能力旧响应仍隐藏；动作转发/播放/目录与未审核说明。
- [x] 实现能力控制、未保存/保存中/恢复或版本冲突禁止、实际运行进度及保存后刷新能力/成片入口；不自动播放或重复提交。
- [x] 运行相关原文/草稿导出/回执/恢复/工作台回归，保存证据与哈希后冻结；不构建或跑整套。

## Task 4: 验收与公开交付（根代理）

- [x] 独立审查最终整合，必要修复先 RED→GREEN；冻结源码后完整后端/HTTP/前端及生产构建。
- [x] 真实多音轨合成素材、本机 FFmpeg 与浏览器完成原文修改→导出→画面/音轨/字幕/下载核对→重启缓存复用，保留原视频/机器文件摘要与自建服务退出证明。
- [x] 更新使用指南、第31轮记录/报告和成本边界；下次五轮反思35，不把测试/审计加轮。
- [x] 精确路径扫描、提交、普通公开推送，同提交 CI 成功后干净主目录快进；保留完整验证回执，持续目标状态按最新用户指示处理。

已发布 `2b455278167212da18b8ed960387bbd5a56f6de0`；验证分支检查 `39902803007`、main检查 `39903396869` 均成功，主目录已同步。此勾选补齐原计划的收尾记录，不额外计产品轮次。

验证分支门禁：本机完整运行的旧时限失败及原样通过记录全部保留。功能/媒体实测、构建/HTTP与独立审查已完成；同提交独立Windows CI完整成功后才更新main，再确认main检查和同步。精确结果保存于忽略的发布回执。
