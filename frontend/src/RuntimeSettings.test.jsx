import React from 'react';
import {describe,expect,it,vi} from 'vitest';
import {render,screen} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import RuntimeSettings from './RuntimeSettings';

const report={version:1,checked_at:1791360000,checks:{ffmpeg:{available:true,message:'FFmpeg 可运行'},
  qsv:{available:false,message:'QSV 不可用'},cpu:{available:true,message:'CPU 可运行'},
  subtitles:{available:true,message:'字幕滤镜可用'}},recommended_encoder:'cpu',export_ready:true};
describe('local runtime and encoder settings',()=>{
  it('checks the environment once and displays CPU availability without starting a model task',async()=>{
    const request=vi.fn().mockResolvedValue(report),change=vi.fn();
    const user=userEvent.setup();
    render(<RuntimeSettings api={{request}} connection='connected' encoder='auto' onEncoderChange={change}/>);
    expect(await screen.findByText('环境自检通过 · 自动模式优先 CPU')).toBeInTheDocument();
    await user.click(screen.getByText('查看环境检查'));
    expect(screen.getByText(/QSV 不可用/)).toBeInTheDocument();
    await user.selectOptions(screen.getByLabelText('视频导出编码方式'),'cpu');
    expect(change).toHaveBeenCalledWith('cpu');
    expect(request).toHaveBeenCalledTimes(1);
    expect(request.mock.calls[0][0]).toBe('/api/environment');
    expect(request.mock.calls[0][1]).toBeUndefined();
  });
  it('keeps a manual retry when a local check fails',async()=>{
    const request=vi.fn().mockRejectedValueOnce(new Error('检查超时')).mockResolvedValue(report);
    const user=userEvent.setup();
    render(<RuntimeSettings api={{request}} connection='connected' encoder='auto' onEncoderChange={()=>{}}/>);
    expect(await screen.findByRole('alert')).toHaveTextContent('检查超时');
    await user.click(screen.getByRole('button',{name:'重新检查环境'}));
    expect(await screen.findByText('环境自检通过 · 自动模式优先 CPU')).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });
});
