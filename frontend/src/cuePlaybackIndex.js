function upperBound(values, value) {
  let low=0, high=values.length;
  while(low<high) {
    const mid=(low+high)>>>1;
    if(values[mid]<=value) low=mid+1; else high=mid;
  }
  return low;
}
function lowerBound(values, value) {
  let low=0, high=values.length;
  while(low<high) {
    const mid=(low+high)>>>1;
    if(values[mid]<value) low=mid+1; else high=mid;
  }
  return low;
}

export function createCuePlaybackIndex(cues) {
  const starts=[], maximumEnds=[];
  let sorted=true, maximum=-Infinity;
  for(const cue of cues) {
    if(!Number.isFinite(cue.start_ms) || !Number.isFinite(cue.end_ms) ||
      (starts.length && cue.start_ms<starts[starts.length-1])) sorted=false;
    starts.push(cue.start_ms);
    maximum=Math.max(maximum,cue.end_ms);
    maximumEnds.push(maximum);
  }
  return {at(time) {
    let active, previous, next;
    if(sorted && Number.isFinite(time)) {
      // Prefix maximum ends retain the original first-match overlap semantics.
      const candidate=upperBound(maximumEnds,time);
      active=candidate<upperBound(starts,time) ? candidate : -1;
      previous=lowerBound(starts,time)-1;
      next=upperBound(starts,time);
      if(next>=cues.length) next=-1;
    } else {
      active=cues.findIndex(cue=>time>=cue.start_ms && time<cue.end_ms);
      previous=cues.findLastIndex(cue=>cue.start_ms<time);
      next=cues.findIndex(cue=>cue.start_ms>time);
    }
    return {active,previous:active>=0 ? active-1 : previous,
      next:active>=0 ? active+1<cues.length ? active+1 : -1 : next};
  }};
}
