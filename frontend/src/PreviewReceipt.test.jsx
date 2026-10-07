import React from 'react';
import {beforeEach,describe,expect,it,vi} from 'vitest';
import {act,fireEvent,render,screen} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import Preview from './Preview';

const cue={id:1,start_ms:1000,end_ms:2000,source_text:'Hello',target_text:'你好',review_status:'unchecked',note:'',warnings:[]};
const base={project_id:'p',selected_id:'main',selections:[{id:'main',name:'整片'}],source_language:'en',target_language:'zh',
  media_available:false,cues:[cue,{...cue,id:2,start_ms:3000,end_ms:4000,source_text:'Next'}],downloads:[],
  manual_review:{supported:true,revision:'r1',summary:{total:2,checked:0,required_checks:2,can_accept:false}}};
const saved={...cue,target_text:'已修改',review_status:'checked'};
const receipt={kind:'review-cue',project_id:'p',selected_id:'main',base_revision:'r1',cue:saved,
  manual_review:{...base.manual_review,revision:'r2',summary:{...base.manual_review.summary,checked:1}}};
const deferred=()=>{let resolve;const promise=new Promise(done=>{resolve=done;});return {promise,resolve};};
const props=request=>({api:{request},snapshot:{project_id:'p',source:'film.mp4',campaign:'campaign',job:{busy:false}},
  connection:'connected',onError:vi.fn(),onReviewChanged:vi.fn(),open:vi.fn(),sessionMemory:{current:null}});
beforeEach(()=>{
  vi.spyOn(HTMLMediaElement.prototype,'pause').mockImplementation(()=>{});
  vi.spyOn(HTMLMediaElement.prototype,'play').mockResolvedValue(undefined);
});
describe('compact review writes in the editor',()=>{
  it('requests one cue and advances only after the successful receipt',async()=>{
    const pending=deferred();
    const request=vi.fn(path=>path==='/api/review-cue' ? pending.promise : Promise.resolve(base));
    const user=userEvent.setup();
    render(<Preview {...props(request)}/>);
    await user.click(await screen.findByRole('button',{name:'编辑第 1 句'}));
    fireEvent.change(screen.getByLabelText('译文（中文）'),{target:{value:'已修改'}});
    await user.click(screen.getByRole('button',{name:'核对并下一条'}));
    expect(request.mock.calls.find(([path])=>path==='/api/review-cue')[1].response_mode).toBe('cue');
    await act(async()=>pending.resolve(receipt));
    expect(screen.getByLabelText('原文（英语）')).toHaveValue('Next');
    expect(screen.getByRole('status')).toHaveTextContent('本句已保存，已定位下一条');
  });
  it('applies a receipt after remounting while the new layout read is still pending',async()=>{
    const saving=deferred(),reading=deferred();let reads=0;
    const request=vi.fn(path=>path==='/api/review-cue' ? saving.promise : ++reads===1 ? Promise.resolve(base) : reading.promise);
    const shared=props(request),user=userEvent.setup();
    const first=render(<Preview {...shared}/>);
    await user.click(await screen.findByRole('button',{name:'编辑第 1 句'}));
    fireEvent.change(screen.getByLabelText('译文（中文）'),{target:{value:'已修改'}});
    await user.click(screen.getByRole('button',{name:'保存修改'}));
    first.unmount();
    render(<Preview {...shared} variant='workbench'/>);
    await act(async()=>saving.resolve(receipt));
    await act(async()=>reading.resolve(base));
    expect(screen.getByLabelText('译文（中文）')).toHaveValue('已修改');
    expect(screen.queryByText('有未保存修改')).not.toBeInTheDocument();
    expect(request.mock.calls.filter(([path])=>path==='/api/review-cue')).toHaveLength(1);
  });
});
