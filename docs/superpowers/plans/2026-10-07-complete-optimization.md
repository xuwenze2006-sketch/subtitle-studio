# 字幕工坊全部优化 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 落实草稿恢复、校对响应、兼容性、真实媒体验收及导出职责拆分。

**Architecture:** 在既有 React/Python 工作台中增量实现。独立草稿存储、环境检测和媒体导出模块；沿用现有人工版本、项目锁、费用与完整性证明。

**Tech Stack:** Windows、Python 3.14 标准库、React、Node 24、FFmpeg。

**Spec:** `docs/superpowers/specs/2026-10-07-complete-optimization-design.md`

## Global Constraints

- 不写入个人素材、任务、账本或真实凭据；不调用付费模型接口。
- 新代码在独立 worktree；代理仅修改指定文件，主控集成并复核。
- 保留项目/修订冲突、取消和检查点恢复、源/字幕/音轨内容校验。
- 不增加产品依赖；公开提交使用 GitHub noreply 身份。

## Review Focus

- 浏览器存储不可用、坏数据、旧草稿或项目版本改变：编辑继续可用，恢复不自动覆盖。
- 保存期间换页面/项目或继续输入：迟到回执不丢输入、不错误推进。
- 重叠字幕和旧无 ID 结果：播放、定位行为一致。
- QSV 声明可用但驱动不可用、用户明确指定 CPU：实际探测与选择结果一致。
- 编码切换、取消、磁盘发布失败：检查点不误复用、不覆盖已存在成片。

### Task 1: 本地校对草稿

**Files:** 新建 `frontend/src/reviewDraftStorage.js` 及测试；集成 `App.jsx`、`Preview.jsx`。

**Interfaces:** 提供读/写/清除草稿的方法，以项目+片段为键；值包含字幕 ID、修订、fields、original、保存时间。不保存 token/key。

- [x] 先测试持久恢复、版本冲突、存储失败、容量/过期清理。
- [x] 确认失败，再实现独立模块。
- [x] 集成页面恢复提示、提交成功清理及保存后新增输入保留。
- [x] 运行草稿及跨布局/并发提交回归。

### Task 2: 校对响应与播放索引

**Files:** `subtitle_pipeline/studio.py`、`manual_review.py`、`frontend/src/Preview.jsx`、定位 helper、相关测试及基准。

**Interfaces:** 单句提交支持小回执，包含 base_revision/revision/cue/summary/project_id/selected_id；保留全量返回兼容路径。客户端按版本应用并必要时读取全量视图。

- [x] 先测试仅返回一条、严格修订匹配、迟到回执与保持其他 cue 对象。
- [x] 实现回执，复用既有提交 store；播放查找用已排序索引。
- [x] 缩减进度轮询的重复详情读取，设置严格失效规则。
- [x] 测量 5,000/20,000 句保存响应体及服务耗时，核对内容等价。

### Task 3: 环境自检、CPU 导出与职责拆分

**Files:** 新建 `subtitle_pipeline/environment.py`、`media_export.py`，修改 `cloud_workflow.py`；主控集成 Studio/前端环境与编码选项。

**Interfaces:** `probe_environment(stop=None)` 返回结构化检查；`export_video(campaign,stop,*,draft=False,encoder='auto')` 支持 auto/qsv/cpu；记录实际 encoder，保留旧 QSV 绑定兼容。

- [x] 先测试无工具、真实探测失败、auto/cpu/qsv 选择、编码身份变化失效。
- [x] 抽出完整媒体导出职责，现有入口代理到模块。
- [x] 集成环境检查页面和导出参数传递到 worker/CLI。
- [x] 运行导出、音轨、取消、检查点及进程回归。

### Task 4: 可复用真实媒体验收

**Files:** `tests/media_acceptance.py`、合成输入辅助及验证说明。

**Interfaces:** 显式本机运行，输出结构化结果和媒体，使用临时/忽略目录；不改变个人数据。

- [x] 生成不同编码、多音轨和足够覆盖跳转的合成样本。
- [x] 验证真实 CPU/QSV 压制、取消与恢复、字幕画面及音轨保持。
- [x] 通过浏览器检查打开、播放、跳转、逐句试听和草稿恢复。
- [x] 记录人工听看及长片尚不能由合成短片证明的范围。

### Task 5: 集成与发布

- [x] 独立审查每个交付及全分支，修复具体问题。
- [x] 完整后端、HTTP/DPAPI、前端测试与构建，确认生成资产同步。
- [x] 更新使用说明与验证报告，核对公开文件和原个人数据未修改。
- 发布步骤：提交、推送公开仓库，确认 GitHub CI 成功及远端 SHA 一致；外部状态以最终发布回执为准。
