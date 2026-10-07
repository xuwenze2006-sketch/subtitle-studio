import {useCallback,useEffect,useRef,useState} from 'react';
import {Button} from './ui';
import './runtime-settings.css';

const labels={python:'Python',tk:'桌面文件选择',ffmpeg:'FFmpeg',ffprobe:'媒体信息',subtitles:'字幕压制',
  libass:'字幕渲染',whisper:'本地识别模型',qsv:'Intel QSV',cpu:'CPU 编码'};
export default function RuntimeSettings({api,connection,encoder,onEncoderChange,disabled}) {
  const [report,setReport]=useState(null),[checking,setChecking]=useState(false),[error,setError]=useState('');
  const mounted=useRef(false),started=useRef(false),pending=useRef(false);
  const check=useCallback(async()=>{
    if(pending.current) return;
    pending.current=true;setChecking(true);setError('');
    try {
      const result=await api.request('/api/environment');
      if(!result?.checks || result.version!==1) throw new Error('环境检查结果无效，请重新检查。');
      if(mounted.current) setReport(result);
    } catch(failure) {if(mounted.current) setError(failure.message);}
    finally {pending.current=false;if(mounted.current) setChecking(false);}
  },[api]);
  useEffect(()=>{
    mounted.current=true;
    if(connection==='connected' && !started.current) {started.current=true;void check();}
    return ()=>{mounted.current=false;};
  },[connection,check]);
  return <section className='runtime-settings card' aria-label='本机环境与视频导出'>
    <div className='runtime-settings-heading'>
      <label>视频导出编码方式<select aria-label='视频导出编码方式' value={encoder} disabled={disabled}
        onChange={event=>onEncoderChange(event.target.value)}>
        <option value='auto'>自动选择</option><option value='qsv'>Intel QSV</option><option value='cpu'>CPU</option>
      </select></label>
      <span role='status'>{checking ? '正在检查本机环境…' : report?.export_ready
        ? `环境自检通过 · 自动模式优先 ${report.recommended_encoder==='qsv' ? 'Intel QSV' : 'CPU'}`
        : report ? '导出环境需要配置' : '本机环境尚未检查'}</span>
      <Button disabled={connection!=='connected' || checking} busy={checking} onClick={check}>重新检查环境</Button>
    </div>
    <p className='runtime-settings-hint'>{encoder==='cpu' ? '使用 CPU 导出，通常比硬件编码慢。'
      : encoder==='qsv' ? '使用 Intel QSV；不可用时会提示调整编码方式。'
      : '自动选择可运行的 Intel QSV；不可用时使用 CPU。'} 环境检查不调用识别或翻译服务。</p>
    {error && <p className='runtime-settings-error' role='alert'>{error}</p>}
    {report && <details><summary>查看环境检查</summary><ul>{Object.entries(report.checks).map(([key,value])=>
      <li key={key}><strong>{labels[key] || key}</strong><span>{value.available ? '可用' : '需配置'} · {value.message}</span></li>)}</ul></details>}
  </section>;
}
