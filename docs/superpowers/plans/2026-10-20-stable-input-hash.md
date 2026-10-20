# Windows 输入哈希的写入保护

**Goal:** 第 14 轮。原生实测发现同一时间刻度内改写文件，NTFS `ChangeTime` 可能不推进，导致单次内容读取前后的 token 相等但哈希已经过时。保护 Windows `file_identity` 的一次哈希区间，同时保留单次内容读取、取消和原生错误关闭语义。

**Evidence:** 忽略目录 `output/验证记录/2026-10-20/continuous-optimization-batch5/native-observation-audit.txt` 保存八轮原测试、真实 token 与 WinAPI `FileBasicInfo` 对照。无延迟的不同内容改写 3/8 次 token 相等；精确哈希重放 5/8 次返回旧摘要。恢复 mtime 前 `ChangeTime` 已经不变，第二个取消分支还存在重复写相同内容的夹具问题。

**Design:** `file_observation.stable_read_guard(path)` 在 Windows 以 `CreateFileW(GENERIC_READ, FILE_SHARE_READ, OPEN_EXISTING)` 持有独立句柄，禁止其他写入或删除访问。`runner.file_identity` 在该上下文内执行文件类型检查、两次 token 与唯一一次内容哈希；root 同时将相同保护用于 `media_export._stable_final_hash` 的最终视频哈希区间。既有写入/删除句柄使保护无法建立时立即拒绝，并说明关闭编辑器或结束写入后重试；不重试。其他平台上下文为空，保留原有 token 检测。所有分支释放自有句柄，关闭失败不能覆盖原始异常。

**Boundary:** 保护仅覆盖上述两处哈希。退出后允许写入和替换；不承诺整个媒体处理期间不可变。导入字幕的后续消费仍由第 13 轮内存字节快照与 SHA 核对保护。`file_token`、导出其他阶段及任务 identity 格式保持现状。

- [x] RED：真实存在的可写句柄在旧代码中不阻止内容哈希；删除句柄被 CRT 拒绝但缺少明确提示。四个失败 subcase 保存到 `batch5/windows-hash-guard-red.log`。
- [x] 实现 typed WinAPI、`INVALID_HANDLE_VALUE` 检查、共享冲突提示及异常/取消句柄释放。
- [x] Windows 的旧重写/替换竞态测试改为证明原生写入被阻止且旧内容保留；两条停止路径分别重置夹具并检查退出后恢复可写。Linux 保留实际变化拒绝测试。
- [x] 真实已有/新增普通读者成功；原生查询失败、读取失败、取消、无效句柄及关闭失败对照。
- [x] root 集成后四个 focused 模块 `tests.test_windows_hash_guard tests.test_runner_identity_recovery tests.test_seed_content_binding tests.test_media_export_files` 共 40 tests 通过，1 个已有 symlink 特权 skip；日志 `batch5/windows-hash-guard-focused.log`。root 后续负责整体评审与发布。
- [x] 额外固化 retained writable mmap、全部 user handle 关闭后的原生可写 view，以及 hardlink 真实写入对照；新增 3 tests、每项两条停止路径全部通过，0.044s，日志 `batch5/windows-hash-guard-native-extra-tests.log`。原生 view 拒绝发生在内容读取前；释放 view 后重新获得写入和替换能力。`git diff --check` 通过。
