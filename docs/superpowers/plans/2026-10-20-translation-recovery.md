# 免费翻译恢复与普通文件保存 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 从累计第 6 轮起修复两个真实恢复故障，保留人工文件、旧凭据和已完成工作。

**Architecture:** 复用现有 valid_translation_content 及 atomic_io 助手。只修改已有状态恢复和原子发布边界，不新建缓存格式或后台服务。

**Tech Stack:** Python、Windows DPAPI、unittest、合成媒体和实际 Windows 文件共享句柄。

**Spec:** 持续优化目标要求每五轮反思；上一批完成五轮。本批发现两个已复现问题，不为凑轮数扩展范围。

## Global Constraints

- 基线 46a8615，现有隔离工作树初始干净。
- 不触发真实翻译、识别、账目变更或用户凭据读取。
- 人工修改不得自动重译覆盖；标点/音乐原文仍兼容。
- 文件系统重试只重用已准备临时文件，不重新加密、翻译或付费请求。

## Review Focus

- 新无效译文不被持久标记成功，旧无效缓存可以恢复。
- 只有已证明未经人工修改的旧无效机器译文才允许重译。
- 恢复请求前和发布前复核双目标；失败、取消或再次继续时仍保留旧生成证明，不遗忘人工编辑保护。
- 正常缓存和已经校验的字幕不产生额外请求。
- 暂时共享冲突可在同次保存内恢复；永久错误仍保留旧文件。
- 清理失败保留主要失败原因，凭据错误信息不泄漏密钥。

## Tasks

- [x] 免费翻译：真实 runner RED 证明多次重试零请求卡住；验证新响应、旧缓存、旧 part 的共同修复，并覆盖人工修改保护及标点原文。
- [x] Windows 保存：真实 CreateFileW 分享锁 RED；凭据与免费翻译缓存复用有界 replace/cleanup，验证成功只准备/请求一次以及失败保留字节。
- [x] 独立审查和最终后端/HTTP 回归；源码未变的前端沿用基线证据，不反复运行无关套件。
- [x] 更新累计轮数和报告，保留初跑失败与最终验证证据。

发布收尾：公开扫描、提交、推送、Windows CI 和主工作区同步的结果记录在忽略的 `output/验证记录/2026-10-20/continuous-optimization-batch2/release-receipt.json`，避免为记录同一提交的 CI 结果而反复生成新提交。
