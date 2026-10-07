import React from 'react';
import {describe,expect,it,vi} from 'vitest';
import {act,render,screen} from '@testing-library/react';
import App from './App';

const snapshot={source:'film.mp4',campaign:'campaign',baseline:'',project_id:'p',project_config:{},recent:[],
  job:{busy:true,action:'full',status:'running',message:'处理中',logs:[],total:20,recognized:1,translated:0},
  accounts:{},settings:{},actions:{stop:true},campaign_status:'full_running',local_available:false};
describe('lightweight progress polling',()=>{
  it('reads progress between bounded full refreshes and refreshes details on completion',async()=>{
    vi.useFakeTimers();
    let done=false;
    const calls=[];
    vi.stubGlobal('fetch',vi.fn(async url=>{
      calls.push(url);
      let body={};
      if(url==='/api/state') body={...snapshot,job:{...snapshot.job,busy:!done,status:done?'complete':'running',message:done?'处理完成':'处理中'}};
      if(url==='/api/progress') body={project_id:'p',source:snapshot.source,campaign:snapshot.campaign,
        job:{...snapshot.job,busy:!done,status:done?'complete':'running',recognized:2},persistence_warning:''};
      if(url==='/api/environment') body={version:1,checks:{},export_ready:false};
      if(url.startsWith('/api/preview')) body={project_id:'p',selected_id:'main',selections:[],cues:[],downloads:[]};
      return new Response(JSON.stringify(body),{status:200});
    }));
    try {
      await act(async()=>render(<App/>));
      await act(async()=>vi.advanceTimersByTimeAsync(1100));
      expect(calls.filter(url=>url==='/api/progress')).toHaveLength(1);
      expect(calls.filter(url=>url==='/api/state')).toHaveLength(1);
      await act(async()=>vi.advanceTimersByTimeAsync(4400));
      expect(calls.filter(url=>url==='/api/state').length).toBeGreaterThan(1);
      const before=calls.filter(url=>url==='/api/state').length;
      done=true;
      await act(async()=>vi.advanceTimersByTimeAsync(1100));
      expect(calls.filter(url=>url==='/api/state').length).toBeGreaterThan(before);
      expect(screen.queryByText('处理中')).not.toBeInTheDocument();
    } finally {vi.useRealTimers();}
  });
});
