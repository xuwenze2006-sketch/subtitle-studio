# Preserved output publication implementation plan

> **For agentic workers:** Execute this bounded helper task in the existing isolated worktree; the parent owns runner integration and outer journal v2.

**Goal:** Preserve the actual file displaced during combined-subtitle publication, including late writes through an older handle, and refuse to report a conflict as completion.

**Architecture:** A prepared replacement and unique capture location are serialized before publication. Windows uses `ReplaceFileW` with that capture; only an exclusive read/delete handle can discard a captured generation whose bytes match the recorded machine baseline. Other captured generations remain original file objects. The helper handles one destination, without changing generic atomic I/O or runner scheduling.

**Tech stack:** Python standard library, Windows kernel32 through ctypes. CI currently runs Windows 2025 / Python 3.14.

**Spec:** Parent-approved bounded design in this chat; offline RED evidence is `output/验证记录/2026-10-20/continuous-optimization-batch5/state-audit.txt`.

## Constraints and API

- Edit only this plan, `subtitle_pipeline/output_publication.py`, and `tests/test_output_publication.py`; no runner edits or commits in this subtask.
- `prepare_publication(project, relative, text, expected_old)` returns JSON data with `version`, `relative`, `digest`, `expected_old`, and `attempt`. An exclusive-created `用户修改备份/发布恢复-<uuid>` directory owns `replacement.srt` and `previous.srt`.
- `publish_record(project, record, stop=None)` and `recover_record(project, record, stop=None)` return `status` (`clean`, `preserved`, `busy`), `published`, `relative`, `digest`, `capture`, and `reason`. Only `clean` plus `published=True` proves a completed output.
- `validate_record(project, record)` checks a whole batch before mutations. `discard_prepared(project, record)` cleans an unstarted owned preparation, refusing any record whose displaced capture already exists.
- The parent must save all records before any publication. Preserved/busy returns are visible conflicts; the outer journal remains and already-completed part evidence survives.
- Original destination absence uses no-clobber publication, so concurrent creation survives. Unknown or changed captures never receive automatic deletion.
- Missing destination plus a capture restores the old directory entry without replacement and returns a visible incomplete result. No blind retry follows native partial failure 1177.
- Windows captures use exclusive read/delete plus same-handle deletion to avoid dropping later writes by old handles or mappings. Busy captures are retained. Normal unchanged machine generations are removed without another data copy.
- POSIX fallback captures by rename and publishes by a no-clobber hard link. It retains the captured object because advisory locks cannot exclude arbitrary old writable descriptors; its `portable_retained` result is intentionally weaker and visible. No portable parity claim.
- Leaf reparse points and escaping parent paths fail closed. Distinct hard links are replaced as directory entries, never truncated. The caller must additionally reject destination pathname aliases of its protected input paths. Hostile parent-directory replacement, external deletion of backups, hardware/power-loss durability, and writers bypassing ordinary OS sharing rules are outside the guarantee.

## Implementation and verification

- [x] Write focused behavioral tests and observe RED before adding the helper.
- [x] Implement one-file preparation, native capture/disposal, no-clobber missing-file publication, and fact-based recovery.
- [x] Verify genuine old writable-handle/mapping late writes survive, ordinary machine generations do not accumulate, concurrent creation is preserved, partial-error/cancel/crash states retain recovery evidence, and malformed paths cannot escape.
- [x] Run the focused module and whitespace check: 23 focused tests passed locally on Windows (0.626 s). Parent integrates runner v2 and runs appropriate integration checks. No providers, credentials, real media, or full suite in this subtask.

## Cost and rejected alternatives

1. Moving a hash check nearer to replacement reduces the original window but still permits a save between the check and overwrite. It does not meet the data-preservation requirement.
2. **Selected:** native replacement with an original-object capture and exclusive disposal. Normal Windows generations incur metadata operations and a hash, without another content copy or retained backup. Genuine changes/busy objects keep one visible conflict; the parent halts further publication for that pending journal. Native sharing failures 5/32/33 may retry the same prepared file for at most 0.62 s while no capture exists; partial error 1177 is never retried.
3. Capturing by rename and publishing with no-clobber operations avoids replacement but exposes a missing destination between steps. This is used only for the explicit portable fallback, with retained old objects and no equivalent exclusive-cleanup claim. A generic transaction/automatic merge subsystem is unnecessary for this fix.

The guarantee is recoverable displaced bytes, not that the root pathname remains unchanged: replacement may already have published the machine generation when a captured human edit is detected. It remains in the capture path, subsequent late writes to that old object remain there, and the parent must publish an incomplete/needs-review state with its actual recovery path. No automatic restore overwrites a newer independently created root file.

## Runner integration

外层 `pending_outputs.version=2` 保存所有输出摘要及单文件准备记录，先持久化再发布；恢复前验证整批路径、摘要及唯一性。旧 version1 仍按旧规则恢复。`preserved/busy` 不得算完成，提示当前字幕与恢复目录供人工核对；正常 Windows 机器旧版独占核验后清理，不累计每次更新的备份。

调度器只在 `combine` 边界将所有 `HumanEditConflict` 升级为专门的发布冲突，立即停止新调度；片段翻译原有冲突语义保持。退出 executor 后仍核验本次拥有的在途结果。原出版意图 `checkpoint` 保留；另存严格绑定当前片段状态的 `owned_results_checkpoint`，区分旧出版快照与随后核验完成的工作。未验证文件不能据此取得成功证据。

人工核对并把整个命名恢复目录移出项目后，恢复可根据当前文件事实继续；该操作不会把未发布文件标为已发布。首次发布缺少机器生成基线时，还须把人工根字幕另存移出后选择重新生成，不能替用户猜测来源。人工版本始终保留作对照。

集成证据：`publication-runner-red.log`、`publication-integration-first.log` 显示原覆盖及冲突后继续调度/检查点漂移；`publication-owned-results-green.log` 四项通过，含独立编辑线程、真实目录替换冲突、双 worker 在途结果保存和明确人工核对后仅补剩余分段。

## Native references

- [ReplaceFileW](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-replacefilew): optional backup, same-volume requirement, and documented partial failure states; write-through flag is unsupported.
- [CreateFileW](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createfilew): sharing compatibility applies for the lifetime of handles and writable mappings.
- [SetFileInformationByHandle](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-setfileinformationbyhandle): delete through the handle used for stable inspection.

The throwaway native probe (`state-atomic-probe.py` / `state-atomic-probe-run.txt` under the audit evidence directory) confirmed capture file identity, old-handle and native-mapping late writes, exclusive-open refusal while those writers live, hard-link input preservation, and no-clobber missing-file publication. This is local Windows evidence, not power-failure certification.
