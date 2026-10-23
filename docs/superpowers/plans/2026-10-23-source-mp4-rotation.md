# 原文 MP4 竖屏修复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 旋转信息为90°/270°的原文视频能正确排版字幕并通过导出校验。

**Architecture:** 共享渲染统一按自动旋转后的显示尺寸处理 ASS 和尺寸校验；原文资格、绑定、回执和编码命令保持。

**Tech Stack:** Python、既有 FFmpeg、现有测试框架，无新依赖。

**Spec:** `docs/superpowers/specs/2026-10-23-source-mp4-rotation-design.md`

## Global Constraints

- 只用隔离合成素材，不调用提供商或修改真实项目、账户及历史验证证据。
- 不放宽来源、修订、帧率、帧数、时长和逐音轨校验；无旋转字节/参数兼容。
- 用户已授权验证后上传现有公开仓库，普通推送且等待同提交检查。

## Review Focus

- 完整显示矩阵优先于截断 rotation 和陈旧 rotate 标签；矩阵精度、半离零取整、负数/多圈角度对应实际 FFmpeg，损坏矩阵明确报错。
- 真正宽高错误仍拒绝；不能为了通过竖屏案例而任意接受交换。
- 原文预检仍可校验源自身，成片、检查点恢复和字幕画布使用一致尺寸。
- 无旋转成片不改变编码身份；带旋转旧失败编码因 ASS 变化保留归档。
- 字幕方向、音轨、缓存复用、认证下载需实际媒体证据。

## Task 1: 统一显示尺寸

**Files:** 修改 `subtitle_pipeline/media_export.py`，新增 `tests/test_video_export_rotation.py`。

**Interfaces:** 私有显示尺寸函数供既有 `_validate_video_parameters(info, result)` 和 ASS 画布使用；保持现有公共接口不变。

- [x] 增加回归并记录 RED：90°/270°输出、ASS显示画布、缓存复用；负角度/矩阵优先和错误尺寸边界。
- [x] 实现最小尺寸归一逻辑，记录 GREEN，运行原文/共享导出相关测试。
- [x] 独立审查复现89.6°/90.6°截断误判，再以真实矩阵写RED并修复，7项回归GREEN；旧完整1518项通过的日志保留，最终代码重新冻结验证。

## Task 2: 完整验证与发布

- [x] 新隔离项目实际 FFmpeg/浏览器验证原文修订、竖屏导出、音轨、下载和复用；保留原文件摘要。
- [ ] 冻结源码，完整后端/HTTP、前端及构建，独立审查最终差异。
- [ ] 更新指南、维护记录及第十七批报告；准确说明实测边界。
- [ ] 扫描允许路径，普通上传验证分支；同提交 CI 成功后快进 main、核验 main CI 并同步干净主目录。
