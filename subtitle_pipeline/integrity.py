"""Content-bound generation records and non-destructive edit reconciliation."""
import hashlib
import json
from pathlib import Path
import unicodedata
from .subtitles import Chunk, Cue, parse_srt, render_srt, merge_chunk_cues, _boundary_duplicate
from .languages import target_filename, output_names


class HumanEditConflict(ValueError):
    """Both editable copies need a human choice before automatic work resumes."""


class SourceIdentityMismatch(ValueError):
    """An additional expected source no longer matches the current file."""


def valid_translation_content(source: str, translated: str) -> bool:
    """Require actual translated content whenever the source contains words."""
    if not isinstance(translated, str) or not translated.strip():
        return False
    def has_lexical_content(text):
        return any(unicodedata.category(char)[0] in {'L', 'N'} for char in text)
    # Preserve punctuation/music-only cues, but never accept an omitted lexical
    # translation merely because the response contains a full stop or ellipsis.
    return not has_lexical_content(source) or has_lexical_content(translated)


def sha256(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream,'sha256').hexdigest()


def text_hash(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def fingerprint(value):
    return text_hash(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')))


def validate_chunk_plan(chunks,duration_ms):
    """Require exactly one contiguous owner for every millisecond of media."""
    if type(duration_ms) is not int or duration_ms<=0 or not chunks:
        raise ValueError('缓存分段计划缺失或媒体时长无效')
    previous=0
    for index,chunk in enumerate(chunks):
        values=(chunk.index,chunk.core_start_ms,chunk.core_end_ms,chunk.audio_start_ms,chunk.audio_end_ms)
        if (any(type(value) is not int for value in values) or chunk.index!=index
            or chunk.core_start_ms!=previous
            or not 0<=chunk.audio_start_ms<=chunk.core_start_ms<chunk.core_end_ms<=chunk.audio_end_ms<=duration_ms):
            raise ValueError('缓存分段计划有缺段、重复或越界，请保留文件并检查')
        previous=chunk.core_end_ms
    if previous!=duration_ms:
        raise ValueError('缓存分段计划未覆盖完整媒体时长')


def evidence_matches(part,chunk,*,cloud,provider=None,model=None,api=None,source_sha256=None,language=None):
    evidence=part.get('asr_evidence')
    provider=provider or ('qwen_asr' if cloud else 'whisper_cpp')
    if (not isinstance(evidence,dict) or evidence.get('provider')!=provider
        or evidence.get('audio_range_ms')!=[chunk.audio_start_ms,chunk.audio_end_ms]):
        return False
    if cloud and (evidence.get('model')!=(model or 'qwen-audio-3.1-asr-flash')
                  or evidence.get('api')!=(api or 'dashscope-v1')
                  or not evidence.get('source_sha256')):
        return False
    # Existing Japanese records predate the explicit language evidence field.
    if language is not None and evidence.get('language','ja' if cloud else language)!=language:
        return False
    return source_sha256 is None or evidence.get('source_sha256')==source_sha256


def reconcile_source(folder,chunk,part,backup,write):
    """Adopt one unambiguous human edit; retain both sides of conflicting edits."""
    local=folder/'source.local.srt'
    public=folder/'原文.srt'
    local_changed=bool(part.get('source_hash')) and sha256(local)!=part['source_hash']
    public_changed=bool(part.get('public_source_hash')) and (
        not public.exists() or sha256(public)!=part['public_source_hash'])
    if public_changed and not public.exists():
        raise ValueError('片段公开原文被删除，请恢复或明确保留本地原文后再继续')
    if not (local_changed or public_changed):
        return False
    local_cues=parse_srt(local.read_text(encoding='utf-8-sig'))
    public_cues=[Cue(c.start_ms-chunk.audio_start_ms,c.end_ms-chunk.audio_start_ms,c.text)
                 for c in parse_srt(public.read_text(encoding='utf-8-sig'))]
    if local_changed and public_changed and local_cues!=public_cues:
        backup(local);backup(public)
        raise ValueError('片段两份原文都有不同的人工修改，请统一后再继续')
    chosen=public_cues if public_changed else local_cues
    for cue in chosen:
        if cue.start_ms<0 or cue.end_ms>chunk.audio_end_ms-chunk.audio_start_ms or cue.end_ms<=cue.start_ms:
            raise ValueError('人工修改的原文时间超出片段范围')
    backup(local);backup(public)
    write(local,render_srt(chosen))
    write(public,render_srt([Cue(c.start_ms+chunk.audio_start_ms,c.end_ms+chunk.audio_start_ms,c.text) for c in chosen]))
    part.update(source_hash=sha256(local),public_source_hash=sha256(public),human_edited=True)
    part.pop('translation',None)
    return True


def reconcile_translation(folder,chunk,part,backup,write,*,target='zh-CN'):
    """Synchronize one verified translation edit without invoking a translator."""
    local=folder/'target.local.srt'
    public=folder/target_filename(target)
    local_changed=bool(part.get('target_hash')) and sha256(local)!=part['target_hash']
    public_changed=bool(part.get('public_target_hash')) and (
        not public.exists() or sha256(public)!=part['public_target_hash'])
    if public_changed and not public.exists():
        raise HumanEditConflict('片段公开译文被删除，请恢复后再继续')
    if not (local_changed or public_changed):
        return False
    try:
        local_cues=parse_srt(local.read_text(encoding='utf-8-sig'))
        public_cues=[Cue(c.start_ms-chunk.audio_start_ms,c.end_ms-chunk.audio_start_ms,c.text)
                     for c in parse_srt(public.read_text(encoding='utf-8-sig'))]
    except ValueError as error:
        raise HumanEditConflict('人工修改的译文格式无效，请保留文件并修正后再继续') from error
    if local_changed and public_changed and local_cues!=public_cues:
        backup(local);backup(public)
        raise HumanEditConflict('片段两份译文都有不同的人工修改，请统一后再继续')
    chosen=public_cues if public_changed else local_cues
    source=parse_srt((folder/'source.local.srt').read_text(encoding='utf-8-sig'))
    if len(chosen)!=len(source) or any((a.start_ms,a.end_ms)!=(b.start_ms,b.end_ms)
                                     for a,b in zip(source,chosen)):
        raise HumanEditConflict('人工修改的译文时间轴与原文不一致')
    backup(local);backup(public)
    write(local,render_srt(chosen))
    write(public,render_srt([Cue(c.start_ms+chunk.audio_start_ms,c.end_ms+chunk.audio_start_ms,c.text)
                            for c in chosen]))
    part.update(target_hash=sha256(local),public_target_hash=sha256(public),human_translation_edited=True)
    return True


def reconcile_merged_source(project,chunks,state,backup,write,persist):
    """Adopt text-only merged edits, journaling all affected copies before I/O."""
    root=project/'原文.srt'
    journal=state.get('merged_source_edit')
    if not root.exists():
        if journal or state.get('merged_source_needs_review'):
            raise HumanEditConflict('合并原文仍需复核，请恢复原文.srt后再继续')
        return False
    root_hash=sha256(root)
    if journal is None:
        previous=state.get('generated_hashes',{}).get('原文.srt')
        if previous==root_hash:
            return False
        if previous is None:
            raise HumanEditConflict('合并原文缺少生成记录，请人工核对后再继续')
        source_parts=[]
        identity=state['identity']
        for chunk in chunks:
            folder=project/'片段'/f'{chunk.index+1:04d}'
            part=state['parts'].get(str(chunk.index),{})
            if (not verify_part(folder,chunk,part,translated=False,cloud=identity['engine']=='qwen_asr',
                    provider=identity['engine'],model=identity.get('asr_model'),api=identity.get('api'),
                    source_sha256=identity['source']['sha256'],language=identity.get('language','ja'))
                or sha256(folder/'原文.srt')!=part.get('public_source_hash')):
                raise HumanEditConflict('合并原文和片段缓存同时改变，请先统一片段后再继续')
            source_parts.append((chunk,parse_srt((folder/'source.local.srt').read_text(encoding='utf-8-sig'))))
        baseline=merge_chunk_cues(source_parts)
        edited=parse_srt(root.read_text(encoding='utf-8-sig'))
        if len(baseline)!=len(edited) or any((a.start_ms,a.end_ms)!=(b.start_ms,b.end_ms)
                                           for a,b in zip(baseline,edited)):
            raise HumanEditConflict('合并原文有增删或时间轴修改，请先人工核对；未重新识别或翻译')
        changes=[(old,new.text) for old,new in zip(baseline,edited) if old.text!=new.text]
        updates=[]
        for chunk,cues in source_parts:
            revised=[]
            for cue in cues:
                absolute=Cue(cue.start_ms+chunk.audio_start_ms,cue.end_ms+chunk.audio_start_ms,cue.text)
                if absolute in baseline:
                    # Another published cue is a separate utterance, even when
                    # it happens to overlap and repeat the same words.
                    replacements={text for old,text in changes if absolute==old}
                else:
                    anchors=[old for old in baseline if _boundary_duplicate(absolute,old)]
                    replacements={text for old,text in changes if old in anchors}
                    if replacements and len(anchors)>1:
                        raise HumanEditConflict('接缝上下文对应多个独立句子，请先核对；未自动替换')
                if len(replacements)>1:
                    raise HumanEditConflict('接缝重复对白无法唯一对应人工修改，请先核对；未自动替换')
                revised.append(Cue(cue.start_ms,cue.end_ms,next(iter(replacements))) if replacements else cue)
            if revised!=cues:
                folder=project/'片段'/f'{chunk.index+1:04d}'
                updates.append({'index':chunk.index,
                    'local_old':sha256(folder/'source.local.srt'),'public_old':sha256(folder/'原文.srt'),
                    'local_text':render_srt(revised),
                    'public_text':render_srt([Cue(c.start_ms+chunk.audio_start_ms,c.end_ms+chunk.audio_start_ms,c.text) for c in revised])})
        journal={'root_sha256':root_hash,'updates':updates}
        state['merged_source_edit']=journal
        state['review_status']='needs_review'
        persist()
    if journal['root_sha256']!=root_hash:
        raise HumanEditConflict('合并原文在更新途中再次改变，请先核对待应用修改')
    # Validate every destination before touching any file. Old or already-applied
    # hashes are accepted so an interrupted local write can resume without ASR.
    for update in journal['updates']:
        folder=project/'片段'/f"{update['index']+1:04d}"
        for name,label in (('source.local.srt','local'),('原文.srt','public')):
            if sha256(folder/name) not in (update[label+'_old'],text_hash(update[label+'_text'])):
                raise HumanEditConflict('片段在合并原文更新途中被另外修改，请保留文件并核对')
    for name in output_names(state['identity'].get('target','zh-CN')):
        backup(project/name)
    for update in journal['updates']:
        folder=project/'片段'/f"{update['index']+1:04d}"
        for name,label in (('source.local.srt','local'),('原文.srt','public')):
            backup(folder/name)
            write(folder/name,update[label+'_text'])
        state['parts'][str(update['index'])].update(source_hash=sha256(folder/'source.local.srt'),
            public_source_hash=sha256(folder/'原文.srt'),human_edited=True)
        state['parts'][str(update['index'])].pop('translation',None)
    state.setdefault('generated_hashes',{})['原文.srt']=root_hash
    if '原文.srt' in state.get('manual_outputs',[]):
        state['manual_outputs'].remove('原文.srt')
    if journal['updates']:
        state['merged_source_edited']=True
        state['review_status']='needs_review'
    state.pop('merged_source_edit',None)
    state.pop('merged_source_needs_review',None)
    persist()
    return bool(journal['updates'])


def _check_evidence_stop(stop,cause=None):
    if stop is not None and stop.is_set():
        # runner imports integrity; keep cancellation support lazy.
        from .runner import Cancelled
        raise Cancelled('已停止来源与字幕校验，已有文件保留') from cause


def _evidence_sha256(path,stop):
    _check_evidence_stop(stop)
    if stop is None:
        return sha256(path)
    from .export_io import cancellable_sha256
    result=cancellable_sha256(path,stop)
    _check_evidence_stop(stop)
    return result


def _evidence_text(path,encoding,stop):
    _check_evidence_stop(stop)
    result=path.read_text(encoding=encoding)
    _check_evidence_stop(stop)
    return result


def _source_hash_with_expectation(original_source,source,expected_source,stop):
    """Read current bytes against two expectations, never a supplied proof."""
    if not isinstance(expected_source,dict) or set(expected_source)!={'path','sha256'}:
        raise ValueError('额外来源期望只允许 path 与 sha256')
    path_value=expected_source['path'];expected_hash=expected_source['sha256']
    if (not isinstance(path_value,(str,Path)) or not str(path_value).strip()
        or not isinstance(expected_hash,str) or len(expected_hash)!=64
        or any(char not in '0123456789abcdefABCDEF' for char in expected_hash)):
        raise ValueError('额外来源期望的路径或 SHA256 无效')
    expected_path=Path(path_value)
    _check_evidence_stop(stop)
    try:
        expected=expected_path.resolve()
        is_file=expected.is_file()
        _check_evidence_stop(stop)
        if not is_file:
            raise SourceIdentityMismatch('原始视频已变化或不存在，请建立新项目')
        actual=_evidence_sha256(expected,stop)
    except (FileNotFoundError,NotADirectoryError) as error:
        _check_evidence_stop(stop,error)
        raise SourceIdentityMismatch('原始视频已变化或不存在，请建立新项目') from error
    _check_evidence_stop(stop)
    if actual!=expected_hash:
        raise SourceIdentityMismatch('原始视频已变化或不存在，请建立新项目')
    # Only a path resolved now can share this actual read. Equal recorded
    # digests (or stat fields) do not prove that two different files match.
    source_hash=actual if source==expected else _evidence_sha256(source,stop)
    _check_evidence_stop(stop)
    changed=(expected_path.resolve()!=expected or original_source.resolve()!=source)
    _check_evidence_stop(stop)
    if changed:
        raise SourceIdentityMismatch('原始视频已变化或不存在，请建立新项目')
    return source_hash


def verify_part(folder,chunk,part, *, translated, cloud,provider=None,model=None,api=None,source_sha256=None,language=None,stop=None):
    _check_evidence_stop(stop)
    done=part.get('asr')=='done'
    _check_evidence_stop(stop)
    if not done:
        return False
    try:
        source=folder/'source.local.srt'
        matches=evidence_matches(part,chunk,cloud=cloud,provider=provider,model=model,api=api,source_sha256=source_sha256,language=language)
        _check_evidence_stop(stop)
        if not matches:
            return False
        matches=_evidence_sha256(source,stop)==part.get('source_hash')
        _check_evidence_stop(stop)
        if not matches:
            return False
        cues=parse_srt(_evidence_text(source,'utf-8-sig',stop))
        _check_evidence_stop(stop)
        duration=chunk.audio_end_ms-chunk.audio_start_ms
        invalid=any(c.start_ms<0 or c.end_ms<=c.start_ms or c.end_ms>duration for c in cues)
        _check_evidence_stop(stop)
        if invalid:
            return False
        if cloud:
            raw=folder/'asr-response.json'
            matches=_evidence_sha256(raw,stop)==part.get('raw_response_hash')
            _check_evidence_stop(stop)
            if not matches:
                return False
        if translated:
            target=folder/'target.local.srt'
            invalid=(part.get('translation')!='done' or part.get('translation_source_hash')!=part['source_hash']
                or _evidence_sha256(target,stop)!=part.get('target_hash'))
            _check_evidence_stop(stop)
            if invalid:
                return False
            targets=parse_srt(_evidence_text(target,'utf-8-sig',stop))
            _check_evidence_stop(stop)
            invalid=(len(cues)!=len(targets) or any(
                (a.start_ms,a.end_ms)!=(b.start_ms,b.end_ms)
                or not valid_translation_content(a.text,b.text) for a,b in zip(cues,targets)))
            _check_evidence_stop(stop)
            if invalid:
                return False
    except (OSError,ValueError,KeyError) as error:
        _check_evidence_stop(stop,error)
        return False
    _check_evidence_stop(stop)
    return True


def sample_state_evidence(project,*,provider='qwen_asr',model='qwen-audio-3.1-asr-flash',
                          api='dashscope-v1',translation_model='deepseek-flash',source_path=None,
                          language='ja',target='zh-CN',stop=None,expected_source=None):
    """Read actual sample artifacts; approval labels alone cannot prove origin."""
    _check_evidence_stop(stop)
    try:
        project=Path(project).resolve()
        state_path=project/'state.json'
        state=json.loads(_evidence_text(state_path,'utf-8',stop))
        _check_evidence_stop(stop)
        identity=state['identity']
        invalid=(state.get('status')!='complete' or 'pending_outputs' in state or identity.get('engine')!=provider
            or identity.get('asr_model')!=model or identity.get('api')!=api
            or identity.get('translation_model')!=translation_model
            or identity.get('language','ja')!=language or identity.get('target','zh-CN')!=target)
        _check_evidence_stop(stop)
        if invalid:
            raise ValueError('样片状态未完成或识别模型/API与本次任务不一致')
        original_source=Path(identity['source']['path'])
        source=original_source.resolve()
        source_hash=(_evidence_sha256(source,stop) if expected_source is None else
                     _source_hash_with_expectation(original_source,source,expected_source,stop))
        invalid=(source_hash!=identity['source'].get('sha256')
            or source_path is not None and source!=Path(source_path).resolve())
        _check_evidence_stop(stop)
        if invalid:
            raise ValueError('样片实际音频与识别来源记录不一致')
        chunks=[Chunk(**value) for value in state['chunks']]
        _check_evidence_stop(stop)
        validate_chunk_plan(chunks,state['duration_ms'])
        _check_evidence_stop(stop)
        for chunk in chunks:
            _check_evidence_stop(stop)
            valid=verify_part(project/'片段'/f'{chunk.index+1:04d}',chunk,
                             state['parts'].get(str(chunk.index),{}),translated=True,cloud=True,
                             provider=provider,model=model,api=api,source_sha256=source_hash,
                             language=language,**({'stop':stop} if stop is not None else {}))
            _check_evidence_stop(stop)
            if not valid:
                raise ValueError('样片识别/翻译文件或来源证明不完整')
        state_hash=_evidence_sha256(state_path,stop)
        _check_evidence_stop(stop)
        return {'path':str(state_path),'sha256':state_hash,
                'source_path':str(source),'source_sha256':source_hash}
    except (OSError,ValueError,KeyError) as error:
        _check_evidence_stop(stop,error)
        raise
