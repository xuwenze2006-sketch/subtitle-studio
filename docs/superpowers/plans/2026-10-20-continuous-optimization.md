# 持续优化第一批 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 修复五个已经离线复现、影响费用安全、来源证明、长字幕响应或保存反馈的问题，每五轮反思实际收益。

**Architecture:** 保持现有预算账本、字幕校验、前端写入协议；在现有边界作小范围修正。独立问题各有失败复现、聚焦回归，最后统一运行离线套件和构建。

**Tech Stack:** Python、Windows、React、TypeScript、unittest、Vitest。

**Spec:** 本次用户要求继续深度优化；现有接口契约和下述失败复现约束实现，不新增工作流。

## Global Constraints

- 基线 5186144；使用现有隔离工作树，保留用户媒体、凭据、历史及账本。
- 仅合成数据、替代传输和回环 HTTP；不发送付费 API 请求。
- 一轮是一个可独立评估和验证的问题，审查、构建、文档不额外计轮数。
- 预算不因无法编码的密钥被占用；不能因不可信用量解除保守预留。

## Review Focus

- 修正密钥后同一请求可以正常执行，之前没有预算 attempt 或网络连接。
- 不强加提供商未公开的密钥格式；覆盖实际请求头编码及控制字符边界。
- 用量缺失或矛盾时采用已有预留；有效或缺省 total_tokens 保持旧行为。
- 性能索引保留接缝去重、歧义冲突和原修改日志语义。
- 状态证明绑定实际解析的字节；CRLF、非 ASCII 和取消仍兼容。
- 写入成功与后续读取失败分别反馈；真实写入失败保留输入，重试刷新不重复 POST。

## Tasks

- [x] 1. 共享纯密钥校验，在四个云适配器、设置/加密存储入口中于预算准入前拒绝无法传输的值。先测旧代码产生 unknown，再测零发送、零登记、修正后可用。
- [x] 2. 校验 DeepSeek usage.total_tokens；不可信用量返回既有保守预留路径。先证矛盾用量记零，再验证有效/缺失/畸形值。
- [x] 3. 优化 integrity.reconcile_merged_source 的重复扫描；用真实临时文件证明输出一致、比较次数随字幕量合理增长，并记录 10k/20k 耗时。
- [x] 4. sample_state_evidence 只为经过校验的状态字节返回证明，末尾发现文件变化则拒绝。先复现旧状态通过却签新状态，再验证字节/换行/并发/取消。
- [x] 5. App.perform 分离已提交操作与状态刷新；真实组件验证成功后清密钥、刷新错误可单独重试、POST 失败保留草稿。
- [x] 审查全部 diff，解决阻塞意见；运行 backend、frontend、HTTP smoke、build、diff check。
- [x] 记录完成轮数、证据与五轮反思。

发布范围：仅经验证的源码、测试、文档及生产构建；暂存后执行公开发布扫描。
