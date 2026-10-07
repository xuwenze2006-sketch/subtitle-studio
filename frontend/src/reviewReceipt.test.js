import { describe, expect, it } from 'vitest';
import { applyReviewReceipt } from './reviewReceipt';

const first = {id:1,start_ms:0,end_ms:1000,source_text:'a',target_text:'甲',review_status:'unchecked'};
const second = {id:2,start_ms:2000,end_ms:3000,source_text:'b',target_text:'乙',review_status:'unchecked'};
const preview = {project_id:'p',selected_id:'main',cues:[first,second],selections:[{id:'main'}],
  manual_review:{supported:true,revision:'r1',summary:{checked:0}},exported_video:{name:'old'}};
const submission = {sample:'main',cueId:1,revision:'r1'};
const receipt = {kind:'review-cue',project_id:'p',selected_id:'main',base_revision:'r1',
  cue:{...first,target_text:'新译文',review_status:'checked'},
  manual_review:{supported:true,revision:'r2',summary:{checked:1}},exported_video:null,draft_video:null};

describe('one-cue save acknowledgement', () => {
  it('updates one cue and summary while preserving other cue identities', () => {
    const result=applyReviewReceipt(preview,receipt,submission);
    expect(result.cues[0]).toEqual(receipt.cue);
    expect(result.cues[1]).toBe(second);
    expect(result.manual_review.revision).toBe('r2');
    expect(result.selections).toBe(preview.selections);
    expect(result.exported_video).toBeNull();
    expect(preview.cues[0]).toBe(first);
  });
  it.each([
    {...receipt,project_id:'other'}, {...receipt,selected_id:'sample-1'},
    {...receipt,base_revision:'old'}, {...receipt,cue:{...receipt.cue,id:2}},
  ])('rejects an acknowledgement for a different write', invalid => {
    expect(()=>applyReviewReceipt(preview,invalid,submission)).toThrow(/刷新/);
  });
  it('requires refresh if another window has already advanced the preview version', () => {
    expect(()=>applyReviewReceipt({...preview,manual_review:{...preview.manual_review,revision:'r3'}},receipt,submission)).toThrow(/刷新/);
  });
  it('accepts a read that already contains the acknowledged version', () => {
    const fresh={...preview,cues:[receipt.cue,second],manual_review:receipt.manual_review};
    expect(applyReviewReceipt(fresh,receipt,submission)).toBe(fresh);
  });
  it('retains the existing complete-preview receipt contract', () => {
    const full={...preview,manual_review:receipt.manual_review};
    expect(applyReviewReceipt(preview,full,submission)).toBe(full);
  });
});
