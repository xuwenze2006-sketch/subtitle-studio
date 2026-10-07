import React from 'react';
import { beforeEach,describe,expect,it,vi } from 'vitest';
import { act,fireEvent,render,screen,waitFor,within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import Preview from './Preview';

const cues=Array.from({length:3},(_,i)=>({id:100+i,start_ms:1000+i*3000,end_ms:2500+i*3000,
  source_text:`Sentence ${i+1}`,target_text:`译文 ${i+1}`,review_status:i===0?'checked':'unchecked',note:'',warnings:[]}));
const data={project_id:'locator-project',selected_id:'main',selections:[{id:'main',name:'整片'}],
  source_language:'en',target_language:'zh-CN',offset_ms:0,media_available:true,media_url:'/local-test-media',
  cues,manual_review:{supported:true,revision:'r1',summary:{total:3,checked:1,can_accept:false}}};
function setup(overrides={}, extra={}){
  const result={...data,...overrides};
  const props={api:{request:vi.fn(async()=>result)},snapshot:{project_id:'locator-project',source:'test.mp4',job:{busy:false}},
    connection:'connected',onError:vi.fn(),open:vi.fn(),...extra};
  return {...render(<Preview {...props}/>),props,user:userEvent.setup()};
}
beforeEach(()=>{
  vi.spyOn(HTMLMediaElement.prototype,'pause').mockImplementation(()=>{});
  vi.spyOn(HTMLMediaElement.prototype,'play').mockResolvedValue(undefined);
});
describe('字幕快速定位接线',()=>{
  it('locates a cue hidden by search and status filtering without renumbering or playing',async()=>{
    const {user}=setup();
    await screen.findByRole('button',{name:'编辑第 1 句'});
    await user.selectOptions(screen.getByLabelText('核对状态筛选'),'checked');
    await user.type(screen.getByLabelText('搜索字幕'),'Sentence 1');
    await user.type(screen.getByLabelText('字幕序号'),'3');
    await user.click(screen.getByRole('button',{name:'定位字幕'}));
    expect(screen.getByLabelText('原文（英语）')).toHaveValue('Sentence 3');
    expect(screen.getByLabelText('搜索字幕')).toHaveValue('');
    expect(screen.getByLabelText('核对状态筛选')).toHaveValue('all');
    expect(screen.getByLabelText('源素材预览').currentTime).toBe(7);
    expect(screen.getByRole('button',{name:'跟随播放'})).toHaveAttribute('aria-pressed','false');
    expect(HTMLMediaElement.prototype.play).not.toHaveBeenCalled();
  });
  it('maps original-video time to the selected sample timeline',async()=>{
    const {user}=setup({selected_id:'sample-1',offset_ms:1680000,selections:[{id:'sample-1',name:'样片 1'}]});
    await screen.findByRole('button',{name:'编辑第 1 句'});
    await user.selectOptions(screen.getByLabelText('定位方式'),'time');
    await user.type(screen.getByLabelText('原片时间'),'00:28:04.500');
    await user.click(screen.getByRole('button',{name:'定位字幕'}));
    expect(screen.getByLabelText('原文（英语）')).toHaveValue('Sentence 2');
    expect(screen.getByLabelText('源素材预览').currentTime).toBe(4);
    expect(HTMLMediaElement.prototype.play).not.toHaveBeenCalled();
  });
  it('keeps unsaved text and the current playhead when a different cue is requested',async()=>{
    const {user,props}=setup();
    await user.click(await screen.findByRole('button',{name:'编辑第 1 句'}));
    fireEvent.change(screen.getByLabelText('译文（中文）'),{target:{value:'尚未保存的输入'}});
    const playhead=screen.getByLabelText('源素材预览').currentTime;
    await user.type(screen.getByLabelText('字幕序号'),'3');
    await user.click(screen.getByRole('button',{name:'定位字幕'}));
    expect(screen.getByRole('alert')).toHaveTextContent('请先保存或放弃');
    expect(screen.getByLabelText('译文（中文）')).toHaveValue('尚未保存的输入');
    expect(screen.getByLabelText('源素材预览').currentTime).toBe(playhead);
    expect(props.api.request.mock.calls.every(([path])=>path.startsWith('/api/preview'))).toBe(true);
  });
  it('disables locating while a save acknowledgement is pending',async()=>{
    let finish;
    const pending=new Promise(resolve=>{finish=resolve;});
    const {user,props}=setup({}, {api:{request:vi.fn(path=>path==='/api/review-cue'?pending:Promise.resolve(data))}});
    await user.click(await screen.findByRole('button',{name:'编辑第 1 句'}));
    await user.click(screen.getByRole('button',{name:'保存修改'}));
    expect(screen.getByRole('button',{name:'定位字幕'})).toBeDisabled();
    expect(props.api.request.mock.calls.filter(([path])=>path==='/api/review-cue')).toHaveLength(1);
    await act(async()=>finish({...data,manual_review:{...data.manual_review,revision:'r2'}}));
    expect(screen.getByRole('button',{name:'定位字幕'})).toBeEnabled();
  });
  it('reveals the requested row on older playback-only servers even when media is unavailable',async()=>{
    const {user}=setup({manual_review:undefined,media_available:false});
    // The locator exists while the initial preview is still loading. Typing
    // into that disabled input can be lost on a busy test machine.
    await waitFor(()=>expect(screen.getByLabelText('字幕序号')).toBeEnabled());
    await user.type(screen.getByLabelText('字幕序号'),'2');
    await user.click(screen.getByRole('button',{name:'定位字幕'}));
    expect(within(screen.getByLabelText('字幕时间轴')).getByText('Sentence 2')).toBeInTheDocument();
    expect(screen.queryByLabelText('人工核对')).not.toBeInTheDocument();
    expect(screen.getByRole('form',{name:'字幕定位'}).querySelector('[role="status"]')).not.toBeNull();
  });
  it('disables locating empty subtitle results',async()=>{
    setup({cues:[],manual_review:undefined,media_available:false});
    expect(await screen.findByRole('button',{name:'定位字幕'})).toBeDisabled();
    expect(screen.getByLabelText('字幕序号')).toBeDisabled();
  });
  it.each([false,true])('replays the selected cue on an older server without ids (adjacent next cue: %s)',async adjacent=>{
    const oldCues=cues.map(({id,...item})=>item);
    if(adjacent) oldCues[2]={...oldCues[2],start_ms:5500};
    const {user}=setup({manual_review:undefined,cues:oldCues});
    await user.click(await screen.findByRole('button',{name:'播放第 2 句'}));
    const media=screen.getByLabelText('源素材预览');
    media.currentTime=5.6;
    fireEvent.timeUpdate(media);
    fireEvent.pause(media);
    expect(media.currentTime).toBe(5.5);
    fireEvent.keyDown(screen.getByRole('region',{name:'字幕预览工作区'}),{key:'R'});
    expect(media.currentTime).toBe(4);
  });
});
