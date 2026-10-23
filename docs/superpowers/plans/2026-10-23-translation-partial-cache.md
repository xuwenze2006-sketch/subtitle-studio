# 翻译部分缓存 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 同批补译失败或取消后保留已验证有效的译文，恢复只请求缺失项。
**Architecture:** 在现有translate_texts内按完整映射分类并复用save，保持原有缓存与runner完成协议。
**Tech Stack:** Python标准库和unittest，无新依赖。
**Spec:** `docs/superpowers/specs/2026-10-23-translation-partial-cache-design.md`

## Global Constraints

- 不请求真实服务、付费提供商或处理真实项目；原文件、费用及历史失败证据保留。
- 不放宽ID、正文、来源、完成证明、取消、节流和期限；无复制启发式扩张。
- 同一最终提交完整验证后普通公开推送，确认同提交CI并同步干净主目录。

## Review Focus

- 先失败的补译不能阻止有效peer落盘；先成功的补译不能因后来失败丢失。
- 取消中的响应不能追加缓存；已落盘子集应保留。
- ID歧义时不能通过部分结果污染缓存。
- 正常批次一次写入，磁盘错误不被当作网络失败重试。
- runner失败时不能声称翻译完成，恢复不重新识别且保留重复字幕顺序。

## Task 1: 补译缓存

**Files:** `subtitle_pipeline/translate.py`；新增 `tests/test_translation_partial_cache.py`。
**Interfaces:** 保持 `translate_texts(texts,source,target,cache_path,stop_event=None)` 与已有缓存schema不变。

- [x] 新增回归，运行并记录RED：超时/无正文、取消、ID歧义、正常写入次数、磁盘错误及runner恢复。
- [x] 先保存完整映射中的有效子集，再逐条保存补译；运行GREEN及既有翻译/恢复相关检查。

## Task 2: 验收和交付

- [x] 独立离线故障/恢复探针对比请求与缓存，记录实际成本与边界。
- [ ] 更新指南、维护记录及本批报告，独立审查最终差异并冻结源码。
- [ ] 完整后端/HTTP、前端及生产构建；验证分支和main同提交CI成功后同步，保留发布回执。
