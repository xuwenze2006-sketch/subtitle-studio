import { describe, expect, it } from 'vitest';
import { createCuePlaybackIndex } from './cuePlaybackIndex';

function reference(cues,time) {
  const active=cues.findIndex(cue=>time>=cue.start_ms && time<cue.end_ms);
  return {active,previous:active>=0 ? active-1 : cues.findLastIndex(cue=>cue.start_ms<time),
    next:active>=0 ? active+1<cues.length ? active+1 : -1 : cues.findIndex(cue=>cue.start_ms>time)};
}
describe('indexed playback position', () => {
  it('preserves first overlapping cue, gaps and exact boundaries', () => {
    const cues=Object.freeze([{start_ms:0,end_ms:5000},{start_ms:1000,end_ms:2000},
      {start_ms:1000,end_ms:9000},{start_ms:12000,end_ms:13000}].map(Object.freeze));
    const index=createCuePlaybackIndex(cues);
    for(const time of [-1,0,999,1000,2000,4999,5000,8999,9000,12000,13000]) {
      expect(index.at(time)).toEqual(reference(cues,time));
    }
  });
  it('preserves legacy unsorted and missing-ID behavior', () => {
    const cues=[{start_ms:5000,end_ms:9000},{start_ms:1000,end_ms:6000}];
    for(const time of [0,1000,3000,5000,6000,9000]) expect(createCuePlaybackIndex(cues).at(time)).toEqual(reference(cues,time));
    expect(createCuePlaybackIndex([]).at(0)).toEqual({active:-1,previous:-1,next:-1});
  });
  it('matches playback behavior throughout a twenty-thousand-cue timeline', () => {
    const cues=Array.from({length:20000},(_,i)=>({start_ms:i*2000,end_ms:i*2000+1700}));
    const index=createCuePlaybackIndex(cues);
    for(const time of [0,1699,1700,1234567,19999700,39998000,40000000]) expect(index.at(time)).toEqual(reference(cues,time));
  });
});
