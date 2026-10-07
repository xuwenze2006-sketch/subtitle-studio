// A successful write is never retried because its view needs a fresh read.
export function applyReviewReceipt(preview, receipt, submission) {
  if (receipt?.kind !== 'review-cue') return receipt;
  const revision = receipt.manual_review?.revision;
  const invalid = !preview || receipt.project_id !== preview.project_id ||
    receipt.selected_id !== preview.selected_id || receipt.selected_id !== submission.sample ||
    receipt.base_revision !== submission.revision || receipt.cue?.id !== submission.cueId ||
    !receipt.manual_review?.supported || typeof revision !== 'string' || !revision;
  if (invalid) throw new Error('修改已保存，校对回执与当前页面不一致，请刷新核对状态。');
  // A remounted layout may read the committed version before receiving its receipt.
  if (preview.manual_review?.revision === revision) return preview;
  if (preview.manual_review?.revision !== receipt.base_revision ||
      !preview.cues.some(cue => cue.id === submission.cueId)) {
    throw new Error('修改已保存，当前字幕版本已变化，请刷新核对状态。');
  }
  return {...preview, cues:preview.cues.map(cue=>cue.id===submission.cueId ? receipt.cue : cue),
    manual_review:receipt.manual_review, exported_video:null, draft_video:null};
}
