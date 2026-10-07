"""Video export, exact-input checkpoints, verification and safe publication.

Cloud recognition and human approval remain in cloud_workflow. Their narrow
verification callbacks are supplied explicitly in ExportDependencies.
"""
from __future__ import annotations
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Callable
import json
import math
import os
import shutil
import sys
import tempfile
import time

from . import runner as r
from . import environment
from .integrity import fingerprint, sha256
from .export_progress import ExportProgress
from .export_io import (cancellable_sha256, cancellable_copy, copy_and_hash,
                        rename_with_retry, unlink_with_retry)
from .copy_space import require_copy_space
from .local_process import capture_process
from .audio_integrity import (audio_track_descriptors, verify_audio_tracks,
                              audio_evidence_matches, parse_audio_stream_hashes)
from .languages import manifest_languages, target_filename, video_output_path


@dataclass(frozen=True)
class ExportDependencies:
    state_is_complete: Callable
    artifacts_for: Callable
    validate_public_subtitles: Callable
    write_manifest: Callable
    emit: Callable


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def _emit(message, **data):
    print(json.dumps({'message': message, **data}, ensure_ascii=False), flush=True)


def media_info(path, stop=None):
    process = capture_process(['ffprobe', '-v', 'error', '-show_streams', '-show_format',
                               '-of', 'json', str(path)], stop=stop, timeout=60)
    return json.loads(process.stdout)


def audio_digest(path,stop=None,*,index=0):
    process=capture_process(['ffmpeg','-nostdin','-v','error','-i',str(path),'-map',f'0:a:{index}',
        '-c:a','copy','-f','hash','-hash','sha256','-'],stop=stop,timeout=300)
    return process.stdout.strip()


def audio_digests(path,stop=None,*,expected_count):
    if type(expected_count) is not int or expected_count<=0:
        raise ValueError('音轨摘要数量无效')
    if stop is not None and stop.is_set():raise r.Cancelled('已停止音轨完整性校验')
    process=capture_process(['ffmpeg','-nostdin','-v','error','-i',str(path),'-map','0:a',
        '-c:a','copy','-f','streamhash','-hash','sha256','-'],stop=stop,timeout=300)
    if stop is not None and stop.is_set():raise r.Cancelled('已停止音轨完整性校验')
    values=parse_audio_stream_hashes(process.stdout,expected_count)
    if stop is not None and stop.is_set():raise r.Cancelled('已停止音轨完整性校验')
    return values


def _publish_video(partial,final,stop,*,expected_hash=None,emit=None):
    """Verify a copy on the destination volume, then publish without clobbering."""
    emit = _emit if emit is None else emit
    temporary=None
    try:
        if stop.is_set():
            raise r.Cancelled('已停止导出，编码结果已保留')
        require_copy_space(partial,final)
        if stop.is_set():
            raise r.Cancelled('已停止导出，编码结果已保留')
        with tempfile.NamedTemporaryFile(dir=final.parent,prefix='.'+final.name+'.',
                suffix='.tmp',delete=False) as destination:
            temporary=Path(destination.name)
            with partial.open('rb') as source:
                if expected_hash is None:
                    expected=copy_and_hash(source,destination,stop)
                else:
                    cancellable_copy(source,destination,stop)
                    expected=expected_hash
            destination.flush()
            os.fsync(destination.fileno())
        if cancellable_sha256(temporary,stop)!=expected:
            raise ValueError('导出文件复制校验失败，编码结果已保留')
        if stop.is_set():
            raise r.Cancelled('已停止导出，编码结果已保留')
        if os.name=='nt':
            # Windows rename fails when the destination exists. Both paths
            # now belong to the same directory, so no cross-volume move occurs.
            rename_with_retry(temporary,final,stop)
        else:
            # POSIX rename overwrites existing files; link provides no-clobber.
            os.link(temporary,final)
            temporary.unlink()
        temporary=None
        return expected
    finally:
        if temporary is not None:
            primary_error=sys.exc_info()[1]
            try:
                unlink_with_retry(temporary,stop)
            except Exception as cleanup_error:
                message=f'导出临时文件暂时无法清理，待清理路径：{temporary}'
                error=primary_error if primary_error is not None else cleanup_error
                error.add_note(message)
                try:
                    emit(message,cleanup_pending=str(temporary))
                except Exception:
                    # A logging failure must not replace the export failure either.
                    pass
                if primary_error is None:
                    raise


def _validate_video_parameters(info,result):
    video=next(s for s in info['streams'] if s['codec_type']=='video')
    out=next(s for s in result['streams'] if s['codec_type']=='video')
    for field in ('width','height'):
        if video.get(field)!=out.get(field):raise ValueError('导出视频参数不一致：'+field)
    try:
        source_rate=Fraction(video['r_frame_rate']);output_rate=Fraction(out['r_frame_rate'])
        valid_rate=source_rate>0 and source_rate==output_rate
    except (KeyError,ValueError,ZeroDivisionError,TypeError):valid_rate=False
    if not valid_rate:raise ValueError('导出视频参数不一致：r_frame_rate')
    # MKV commonly omits nb_frames. An absent count is not evidence of lost frames.
    count=str(video.get('nb_frames',''))
    if count.isdecimal() and int(count)>0:
        output_count=str(out.get('nb_frames',''))
        if not output_count.isdecimal() or int(count)!=int(output_count):
            raise ValueError('导出视频参数不一致：nb_frames')
    duration=float(info['format']['duration']);output_duration=float(result['format']['duration'])
    if (not math.isfinite(duration) or not math.isfinite(output_duration)
            or duration<=0 or output_duration<=0 or abs(duration-output_duration)>.1):
        raise ValueError('导出视频时长异常')
    return out


def _binding_matches(saved,current):
    """The only implicit encoder in a historical binding was h264_qsv."""
    if saved==current:return True
    return (isinstance(saved,dict) and isinstance(current,dict)
        and current.get('encoder')=='qsv' and 'encoder' not in saved
        and saved=={key:value for key,value in current.items() if key!='encoder'})


def _encoding_identity_matches(saved,current):
    if saved==current:return True
    if current.get('encoder')!='qsv' or not isinstance(saved,dict):return False
    legacy=dict(current)
    legacy.pop('encoder')
    legacy['output_binding']={key:value for key,value in current['output_binding'].items() if key!='encoder'}
    # This retains exact ASS and argument comparison. A missing or altered field
    # in either format is not a completed-encode proof.
    return saved==legacy


def _resume_encoder(folder,manifest,output_key,binding,requested,*,published,output_hash=None,source_info=None):
    """Read a candidate policy only; completion still needs the full proof below."""
    saved_bindings=[]
    if published:
        # A manifest can still describe an older encoder after the new final
        # was published but its manifest write failed. Match the actual bytes.
        if manifest.get(output_key+'_sha256')==output_hash:
            saved_bindings.append((manifest.get(output_key+'_binding'),False))
        try:record=read_json(folder/'publication-checkpoint.json')
        except (OSError,ValueError):record={}
        if isinstance(record,dict):saved_bindings.append((record.get('output_binding'),True))
    else:
        try:record=read_json(folder/'encoding-checkpoint.json')
        except (OSError,ValueError):record={}
        if isinstance(record,dict) and record.get('status') in ('encoded','pending_source_validation'):
            identity=record.get('identity')
            if isinstance(identity,dict):saved_bindings.append((identity.get('output_binding'),False))
    for saved,needs_publication_proof in saved_bindings:
        if not isinstance(saved,dict):continue
        candidate=saved.get('encoder','qsv')
        if candidate not in ('qsv','cpu') or requested not in ('auto',candidate):continue
        current={**binding,'encoder':candidate}
        if not _binding_matches(saved,current):continue
        if published and (needs_publication_proof or 'audio_policy' in binding):
            if _publication_evidence(folder,current,output_hash,source_info) is None:continue
        return candidate
    return None


def _encode_or_resume(folder,source,binding,args,stop,progress,*,emit=None,resume_only=False):
    """Return (digest, reused) for an encode bound to exact inputs and arguments."""
    emit = _emit if emit is None else emit
    partial=folder/'result.partial.mp4'
    receipt_path=folder/'encoding-checkpoint.json'
    identity={'version':1,'output_binding':binding,'encoder':binding['encoder'],
              'ass_sha256':sha256(folder/'captions.ass'),
              'encoder_args':[str(arg) for arg in args]}
    def file_token(path):
        stat=path.stat()
        return stat.st_dev,stat.st_ino,stat.st_size,stat.st_mtime_ns,stat.st_ctime_ns
    original_source_token=file_token(source)
    def validate_inputs(*,hash_source=False):
        if (file_token(source)!=original_source_token
                or (hash_source and cancellable_sha256(source,stop)!=binding['source_sha256'])
                or file_token(source)!=original_source_token):
            raise ValueError('编码过程中源视频发生变化，未记录为可复用成片')
        if (sha256(folder/'captions.ass')!=identity['ass_sha256']
                or sha256(folder/'captions.srt')!=binding['captions_sha256']):
            raise ValueError('编码过程中字幕发生变化，未记录为可复用成片')
    def finish_validation(digest,partial_token):
        validate_inputs(hash_source=True)
        if file_token(partial)!=partial_token:
            raise ValueError('校验过程中编码文件发生变化，未记录为可复用成片')
        r.atomic_json(receipt_path,{'status':'encoded','identity':identity,'sha256':digest,
                                  'size_bytes':partial_token[2]})
        # Pending receipts have not completed the first media validation pass.
        return digest,False
    try:receipt=read_json(receipt_path)
    except (OSError,ValueError):receipt={}
    if not isinstance(receipt,dict):receipt={}
    if (receipt.get('status')=='encoded' and _encoding_identity_matches(receipt.get('identity'),identity)
            and partial.is_file()):
        partial_token=file_token(partial)
        digest=cancellable_sha256(partial,stop)
        if file_token(partial)!=partial_token:
            raise ValueError('校验过程中编码文件发生变化，未复用编码结果')
        if digest==receipt.get('sha256'):
            validate_inputs(hash_source=True)
            if file_token(partial)!=partial_token:
                raise ValueError('校验过程中编码文件发生变化，未复用编码结果')
            emit('已找到与当前素材和字幕一致的编码结果，继续校验，无需重新压制')
            return digest,True
    saved_token=receipt.get('source_token')
    saved_digest=receipt.get('sha256')
    if (receipt.get('status')=='pending_source_validation' and _encoding_identity_matches(receipt.get('identity'),identity)
            and isinstance(saved_token,list) and len(saved_token)==5
            and all(type(value) is int for value in saved_token)
            and isinstance(saved_digest,str) and len(saved_digest)==64
            and all(char in '0123456789abcdef' for char in saved_digest)
            and type(receipt.get('size_bytes')) is int and receipt['size_bytes']>0
            and partial.is_file()):
        if tuple(saved_token)!=original_source_token:
            raise ValueError('等待校验期间源视频发生变化，未复用编码结果')
        progress.stage('validating')
        partial_token=file_token(partial)
        digest=cancellable_sha256(partial,stop)
        if file_token(partial)!=partial_token:
            raise ValueError('校验过程中编码文件发生变化，未复用编码结果')
        validate_inputs()
        if digest==saved_digest and partial_token[2]==receipt['size_bytes']:
            emit('已核对待验证成片的完整哈希，继续源视频与媒体检查，无需重新压制')
            return finish_validation(digest,partial_token)
    if stop.is_set():raise r.Cancelled('已停止导出，编码结果已保留')
    if resume_only:return None
    if partial.exists():
        # Preserve legacy, interrupted or mismatched outputs instead of -y clobbering.
        archive=folder/'保留的编码';archive.mkdir(exist_ok=True)
        partial.rename(archive/f'result-{time.time_ns()}.mp4')
    r.atomic_json(receipt_path,{'status':'encoding','identity':identity})
    (folder/'encode-progress.txt').unlink(missing_ok=True)
    emit('开始本地压制带字幕 MP4，不产生识别费用')
    with progress.watch(folder/'encode-progress.txt'):
        r.run_process(args,folder/'encode.log',stop,cwd=folder)
    progress.stage('validating')
    validate_inputs()
    partial_token=file_token(partial)
    digest=cancellable_sha256(partial,stop)
    if file_token(partial)!=partial_token:
        raise ValueError('校验过程中编码文件发生变化，未记录为可复用成片')
    validate_inputs()
    # No resumable receipt exists until a successful encoder's entire output
    # has been hashed. A later cancellation must repeat full source validation.
    r.atomic_json(receipt_path,{'status':'pending_source_validation','identity':identity,
        'source_token':list(original_source_token),'sha256':digest,'size_bytes':partial_token[2]})
    return finish_validation(digest,partial_token)


def _encoding_args(source,partial,binding,*,draft):
    multiple_audio='audio_policy' in binding
    args=['ffmpeg','-nostdin','-hide_banner','-v','warning','-y','-threads','4','-i',source,
        '-map','0:v:0','-map','0:a' if multiple_audio else '0:a:0']
    if binding['encoder']=='qsv':
        # Preserve historical QSV arguments exactly for checkpoint compatibility.
        args.extend(['-vf','subtitles=captions.ass,format=nv12','-filter_threads','2',
            '-c:v','h264_qsv','-global_quality','20','-preset','veryfast' if draft else 'fast',
            '-async_depth','8'])
    else:
        args.extend(['-vf','subtitles=captions.ass,format=yuv420p','-filter_threads','2',
            '-c:v','libx264','-crf','20','-preset','veryfast' if draft else 'fast'])
    args.extend(['-c:a','copy'])
    if multiple_audio:
        for index,track in enumerate(binding['audio_tracks']):
            # Relative modifiers preserve other original disposition flags.
            # MP4's first-track default when none is specified is recorded below.
            disposition=('+' if track['default'] else '-')+'default'+('+' if track['forced'] else '-')+'forced'
            args.extend([f'-disposition:a:{index}',disposition])
    args.extend(['-movflags','+faststart','-progress','encode-progress.txt','-nostats',partial])
    return args


def _publication_evidence(folder,binding,output_hash,source_info=None):
    """Recover only bytes that already passed media checks before publication."""
    try:record=read_json(folder/'publication-checkpoint.json')
    except (OSError,ValueError):return None
    if (not isinstance(record,dict) or type(record.get('version')) is not int or record['version']!=1
            or not _binding_matches(record.get('output_binding'),binding) or record.get('sha256')!=output_hash):return None
    verification=record.get('verification')
    if (not isinstance(verification,dict) or not _binding_matches(verification.get('output_binding'),binding)
            or ('encoder' in verification and verification['encoder']!=binding['encoder'])
            or not isinstance(verification.get('source_audio_sha256'),str)
            or not verification['source_audio_sha256'].strip()
            or verification.get('source_audio_sha256')!=verification.get('output_audio_sha256')):return None
    if 'audio_policy' in binding:
        if (binding['audio_policy']!='all_tracks_v1' or source_info is None
                or not audio_evidence_matches(verification.get('audio_tracks'),source_info)
                or binding.get('audio_tracks')!=audio_track_descriptors(source_info)):
            return None
        if verification['source_audio_sha256']!='SHA256='+verification['audio_tracks']['source'][0]['sha256']:
            return None
    fields=verification.get('video_fields')
    if (not isinstance(fields,dict)
            or set(fields)!={'width','height','r_frame_rate','nb_frames'}
            or any(type(fields.get(key)) is not int or fields[key]<=0 for key in ('width','height'))
            or not isinstance(verification.get('review'),str) or not verification['review'].strip()):return None
    try:
        duration=verification['duration']
        if isinstance(duration,bool) or not math.isfinite(float(duration)) or float(duration)<=0:return None
        if Fraction(fields['r_frame_rate'])<=0:return None
    except (KeyError,TypeError,ValueError,OverflowError,ZeroDivisionError):return None
    return {**verification,'output_binding':binding,'encoder':binding['encoder']}


def _check_export_stop(stop,cause=None):
    if stop is not None and stop.is_set():
        raise r.Cancelled('已停止导出，已保存的成片和记录保留') from cause


def _record_export(campaign,manifest,folder,final,output_key,output_hash,binding,draft,verification,*,stop=None,dependencies):
    _check_export_stop(stop)
    try:
        r.atomic_json(folder/'verification.json',verification)
        _check_export_stop(stop)
        manifest.update({output_key:str(final),output_key+'_sha256':output_hash,
                         output_key+'_binding':binding,output_key+'_encoder':binding['encoder']})
        if not draft:manifest['status']='exported'
        _check_export_stop(stop)
        dependencies.write_manifest(campaign,manifest)
    except OSError as error:
        _check_export_stop(stop,error)
        raise
    _check_export_stop(stop)


def _draft_video_inputs(campaign,manifest,language,target,*,stop=None,dependencies):
    """Bind a draft to complete generation and current edits, never to approval."""
    from .manual_review import load_review,materialize_review
    folder=campaign/'整片'
    if not dependencies.state_is_complete(folder,language=language,target=target,stop=stop,
            expected_source={key:manifest['source'][key] for key in ('path','sha256')}):
        raise ValueError('整片字幕尚未完整生成，不能导出草稿视频')
    artifacts=dependencies.artifacts_for(campaign,manifest,full=True)
    validated=dependencies.validate_public_subtitles(folder,artifacts,language=language,target=target)
    current=load_review(folder,language=language,target=target)
    if current['binding'].get('identity',{}).get('source',{}).get('sha256')!=manifest['source']['sha256']:
        raise ValueError('人工校对来源与原视频不一致')
    revision=None
    captions=folder/target_filename(target)
    if current['exists']:
        snapshot=materialize_review(folder,language=language,target=target,require_accepted=False)
        if snapshot['revision']!=current['revision']:
            raise ValueError('人工校对版本在准备草稿时发生变化，请重新导出')
        revision=snapshot['revision']
        captions=snapshot['folder']/target_filename(target)
        artifacts=snapshot['files']
        validated={'captions_sha256':sha256(captions)}
    else:
        final_review=read_json(campaign/'final-review.json') if (campaign/'final-review.json').exists() else {}
        previous_draft=manifest.get('draft_output_binding') or {}
        if final_review.get('manual_revision') or previous_draft.get('manual_revision'):
            raise ValueError('原人工校对记录缺失，不能回退机器字幕导出草稿')
    binding={'source_sha256':manifest['source']['sha256'],
             'captions_sha256':validated['captions_sha256'],
             'review_artifacts_sha256':fingerprint(artifacts),'render_version':1,
             'review_status':'unreviewed_draft','language':language,'target':target,
             'manual_revision':revision,
             'machine_source_sha256':current['binding']['source_sha256'],
             'machine_target_sha256':current['binding']['target_sha256']}
    _check_export_stop(stop)
    return captions,binding


def export_video(campaign,stop,*,draft=False,encoder='auto',dependencies):
    if type(draft) is not bool:raise ValueError('草稿导出选项须为布尔值')
    if not isinstance(encoder,str) or encoder not in ('auto','qsv','cpu'):
        raise ValueError('编码方式须为 auto、qsv 或 cpu')
    if stop.is_set():raise r.Cancelled('已停止导出，已保存结果保留')
    if draft:
        # Keep generation and manual writers out while pinning and encoding
        # the current snapshot. CLI already owns the outer campaign lock.
        with r.ProjectLock(campaign/'整片'):
            return _export_video(campaign,stop,draft=True,encoder=encoder,dependencies=dependencies)
    return _export_video(campaign,stop,draft=False,encoder=encoder,dependencies=dependencies)


def _export_video(campaign,stop,*,draft,encoder,dependencies):
    emit=dependencies.emit
    manifest=read_json(campaign/'campaign.json')
    progress=ExportProgress(manifest.get('duration_ms',0)/1000,emit)
    progress.stage('preparing')
    # Source content is read by the generation verifier below, where that
    # fresh digest must match both identities. Never pass an earlier digest.
    source=Path(manifest['source']['path'])
    if not source.is_file():
        raise ValueError('原视频内容已改变或缺失；请新建项目，不复用旧缓存')
    language,target=manifest_languages(manifest)
    if draft:
        captions,binding=_draft_video_inputs(campaign,manifest,language,target,stop=stop,dependencies=dependencies)
    else:
        review=read_json(campaign/'final-review.json')
        if (not dependencies.state_is_complete(campaign/'整片',language=language,target=target,stop=stop,
                expected_source={key:manifest['source'][key] for key in ('path','sha256')})
            or review.get('status')!='sampled_approved' or manifest_languages(review)!=(language,target)
            or review.get('source_sha256')!=manifest['source']['sha256']
            or any(sha256(a['path'])!=a['sha256'] for a in review.get('artifacts',[]))
            or not review.get('artifacts')):
            raise ValueError('最终字幕未验收或验收后发生改变，未压制')
        if review.get('manual_revision'):
            from .manual_review import load_review,materialize_review
            dependencies.validate_public_subtitles(campaign/'整片',dependencies.artifacts_for(campaign,manifest,full=True),
                                      language=language,target=target)
            current=load_review(campaign/'整片',language=language,target=target)
            if (not current['exists'] or current['revision']!=review['manual_revision']
                or current['binding']!=review.get('manual_binding')):
                raise ValueError('人工校对版本已变化，请重新验收后导出')
            snapshot=materialize_review(campaign/'整片',language=language,target=target,require_accepted=True)
            if snapshot['files']!=review['artifacts']:raise ValueError('校对快照与验收记录不一致')
            captions=snapshot['folder']/target_filename(target)
            validated={'captions_sha256':sha256(captions)}
        else:
            if (campaign/'整片'/'人工校对'/'校对记录.json').exists():
                raise ValueError('新增人工校对尚未验收，请重新确认后导出')
            validated=dependencies.validate_public_subtitles(campaign/'整片',review['artifacts'],language=language,target=target)
            captions=campaign/'整片'/target_filename(target)
        binding={'source_sha256':manifest['source']['sha256'],
                 'captions_sha256':validated['captions_sha256'],
                 'review_artifacts_sha256':fingerprint(review['artifacts']),
                 'render_version':1}
        if (language,target)!=('ja','zh-CN'):
            binding.update(language=language,target=target)
    _check_export_stop(stop)
    info=media_info(source,stop)
    multiple_audio=len([stream for stream in info['streams'] if stream.get('codec_type')=='audio'])>1
    if multiple_audio:
        # Bind the changed mapping before any existing final is considered.
        # Keep legacy one-track bindings/arguments so completed user encodes
        # do not need to be repeated solely because this policy was added.
        binding.update(audio_policy='all_tracks_v1',audio_tracks=audio_track_descriptors(info))
    edition='未审核草稿' if draft else '修订版'
    status='draft_exported' if draft else 'exported'
    output_key='draft_output' if draft else 'output'
    final=video_output_path(source,target,draft=draft)
    folder=campaign/'导出'/'草稿视频' if draft else campaign/'导出'
    published=final.exists()
    output_hash=cancellable_sha256(final,stop) if published else None
    resume_encoder=_resume_encoder(folder,manifest,output_key,binding,encoder,published=published,
                                  output_hash=output_hash,source_info=info)
    # A completed encode needs verification and publication, not a working
    # encoder. Explicit policies still cannot claim another encoder's output.
    if published:
        selected_encoder=resume_encoder or ('qsv' if encoder=='auto' else encoder)
    else:
        selected_encoder=resume_encoder or environment.select_encoder(encoder,stop)
    _check_export_stop(stop)
    binding['encoder']=selected_encoder
    emit('已选择视频编码方式：'+('QSV' if selected_encoder=='qsv' else 'CPU (libx264)'),encoder=selected_encoder)
    if published:
        verification=_publication_evidence(folder,binding,output_hash,info)
        if (manifest.get(output_key+'_sha256')==output_hash and _binding_matches(manifest.get(output_key+'_binding'),binding)
                and (not multiple_audio or verification is not None)):
            if verification and not (folder/'verification.json').exists():
                _check_export_stop(stop)
                try:
                    r.atomic_json(folder/'verification.json',verification)
                except OSError as error:
                    _check_export_stop(stop,error)
                    raise
            _check_export_stop(stop)
            progress.stage('done')
            emit(edition+'已存在且校验一致，无需重复压制',status=status,output=str(final));return
        if verification:
            progress.stage('publishing')
            _record_export(campaign,manifest,folder,final,output_key,output_hash,binding,draft,verification,stop=stop,dependencies=dependencies)
            _check_export_stop(stop)
            progress.stage('done')
            emit('已恢复成片记录，无需重复压制',status=status,output=str(final));return
        raise ValueError('目标'+edition+'已存在，且无法证明对应当前字幕；未覆盖，请另存旧版后重新导出')
    folder.mkdir(parents=True,exist_ok=True)
    shutil.copyfile(captions,folder/'captions.srt')
    if sha256(folder/'captions.srt')!=binding['captions_sha256']:
        raise ValueError('复制字幕时内容发生变化，未压制')
    r.run_process(['ffmpeg','-nostdin','-v','error','-y','-i',folder/'captions.srt',folder/'captions.ass'],
                  folder/'ass.log',stop)
    video=next(s for s in info['streams'] if s['codec_type']=='video')
    progress.duration=float(info['format']['duration'])
    ass=(folder/'captions.ass').read_text(encoding='utf-8-sig')
    import re
    ass=re.sub(r'(?m)^PlayResX:.*$',f'PlayResX: {video["width"]}',ass)
    ass=re.sub(r'(?m)^PlayResY:.*$',f'PlayResY: {video["height"]}',ass)
    style=f'Style: Default,Microsoft YaHei,{round(video["height"]*25/474)},&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,1.6,0.6,2,20,20,20,1'
    ass=re.sub(r'(?m)^Style: Default,.*$',style,ass)
    r.atomic_text(folder/'captions.ass',ass)
    partial=folder/'result.partial.mp4'
    args=_encoding_args(source,partial,binding,draft=draft)
    encoded=None
    if resume_encoder is not None:
        encoded=_encode_or_resume(folder,source,binding,args,stop,progress,emit=emit,resume_only=True)
    if encoded is None:
        if resume_encoder is not None:
            # The candidate's arguments, ASS, or entire partial did not match.
            # Probe before archiving anything or starting a fresh encode.
            selected_encoder=environment.select_encoder(encoder,stop)
            _check_export_stop(stop)
            binding['encoder']=selected_encoder
            args=_encoding_args(source,partial,binding,draft=draft)
            emit('已选择本机视频编码器：'+('QSV' if selected_encoder=='qsv' else 'CPU (libx264)'),encoder=selected_encoder)
        encoded=_encode_or_resume(folder,source,binding,args,stop,progress,emit=emit)
    encoded_hash,reused_encoding=encoded
    progress.stage('validating')
    if stop.is_set():raise r.Cancelled('已停止导出，编码结果已保留')
    # A copy failure does not invalidate completed media checks. Reuse them only
    # after both whole-file digests and the current encoder identity were checked.
    # Fresh encodes always receive fresh checks, even if their bytes happen to
    # equal an earlier result produced with different parameters.
    verification=_publication_evidence(folder,binding,encoded_hash,info) if reused_encoding else None
    if verification is not None:
        try:
            _validate_video_parameters(info,{'streams':[{'codec_type':'video',**verification['video_fields']}],
                                            'format':{'duration':verification['duration']}})
        except (TypeError,ValueError,KeyError):verification=None
    if verification is None:
        result=media_info(partial,stop);out=_validate_video_parameters(info,result)
        audio_tracks=None
        if multiple_audio:
            audio_tracks=verify_audio_tracks(info,result,source,partial,stop,audio_digest,digests=audio_digests)
            source_audio='SHA256='+audio_tracks['source'][0]['sha256']
            output_audio='SHA256='+audio_tracks['output'][0]['sha256']
        else:
            source_audio=audio_digest(source,stop)
            if stop.is_set():raise r.Cancelled('已停止导出，编码结果已保留')
            output_audio=audio_digest(partial,stop)
            if source_audio!=output_audio:raise ValueError('导出音频与源音频不一致')
        duration=float(info['format']['duration'])
        for position in (0,duration/2,max(0,duration-3)):
            r.run_process(['ffmpeg','-nostdin','-v','error','-ss',str(position),'-i',partial,
                '-t','2','-f','null','-'],folder/f'decode-{int(position)}.log',stop)
        verification={'encoder':selected_encoder,'source_audio_sha256':source_audio,
            'output_audio_sha256':output_audio,'output_binding':binding,
            'video_fields':{k:out.get(k) for k in ('width','height','r_frame_rate','nb_frames')},
            'duration':result['format']['duration'],
            'review':'未人工审核，仅完成自动媒体完整性检查' if draft else '用户抽检通过，未声称逐句人工校对'}
        if audio_tracks is not None:verification['audio_tracks']=audio_tracks
    else:
        emit('已核验源视频与编码文件，复用相同成片的媒体检查记录，继续保存')
    if verification.get('audio_tracks',{}).get('default_policy')=='mp4_first_when_unspecified':
        emit('源视频未指定默认音轨，MP4将第一条设为默认；所有音轨的压缩数据校验一致。')
    r.atomic_json(folder/'publication-checkpoint.json',{'version':1,'output_binding':binding,
                  'sha256':encoded_hash,'verification':verification})
    progress.stage('publishing')
    output_hash=_publish_video(partial,final,stop,expected_hash=encoded_hash,emit=emit)
    _record_export(campaign,manifest,folder,final,output_key,output_hash,binding,draft,verification,stop=stop,dependencies=dependencies)
    _check_export_stop(stop)
    progress.stage('done')
    emit(edition+'MP4已导出并完成媒体完整性检查',status=status,output=str(final))
