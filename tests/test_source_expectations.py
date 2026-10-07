"""Independent expected identities are compared to actual offline source reads."""
from contextlib import contextmanager, ExitStack
from dataclasses import asdict
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from subtitle_pipeline import integrity as i
from subtitle_pipeline.runner import Cancelled
from subtitle_pipeline.subtitles import Chunk, Cue, render_srt


class SourceExpectationsTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory(prefix='source-expectations-')
        self.addCleanup(temporary.cleanup)
        self.project=Path(temporary.name)
        self.source=self.project/'source.bin'
        self.content=b'actual-source-content\0'*100000
        self.source.write_bytes(self.content)
        self.digest=i.sha256(self.source)
        self.other=self.project/'other.bin';self.other.write_bytes(self.content)
        self.expected={'path':str(self.source),'sha256':self.digest}
        chunk=Chunk(0,0,1000,0,1000)
        folder=self.project/'片段'/'0001';folder.mkdir(parents=True)
        (folder/'source.local.srt').write_text(render_srt([Cue(100,800,'こんにちは')]),encoding='utf-8')
        (folder/'target.local.srt').write_text(render_srt([Cue(100,800,'你好')]),encoding='utf-8')
        (folder/'asr-response.json').write_text('{"offline":true}',encoding='utf-8')
        part={'asr':'done','translation':'done',
            'asr_evidence':{'provider':'qwen_asr','model':'qwen-audio-3.1-asr-flash',
                'api':'dashscope-v1','language':'ja','source_sha256':self.digest,'audio_range_ms':[0,1000]},
            'source_hash':i.sha256(folder/'source.local.srt'),
            'translation_source_hash':i.sha256(folder/'source.local.srt'),
            'target_hash':i.sha256(folder/'target.local.srt'),
            'raw_response_hash':i.sha256(folder/'asr-response.json')}
        self.state={'status':'complete','duration_ms':1000,
            'identity':{'engine':'qwen_asr','asr_model':'qwen-audio-3.1-asr-flash',
                'api':'dashscope-v1','translation_model':'deepseek-flash','language':'ja','target':'zh-CN',
                'source':{'path':str(self.source),'sha256':self.digest}},
            'chunks':[asdict(chunk)],'parts':{'0':part}}
        self.state_path=self.project/'state.json';self.save_state()
        self.stop=threading.Event()
        guards=ExitStack();self.addCleanup(guards.close)
        guards.enter_context(patch('socket.create_connection',side_effect=AssertionError('offline only')))
        guards.enter_context(patch('socket.socket.connect',side_effect=AssertionError('offline only')))

    def save_state(self):self.state_path.write_text(json.dumps(self.state,ensure_ascii=False),encoding='utf-8')

    def sample(self,**kwargs):
        return i.sample_state_evidence(self.project,**kwargs)

    @contextmanager
    def source_reads(self,on_read=None):
        original=Path.open;records=[]
        class Reader:
            def __init__(proxy,stream,record):proxy.stream=stream;proxy.record=record
            def __enter__(proxy):return proxy
            def __exit__(proxy,*args):proxy.stream.close();proxy.record['closed']=proxy.stream.closed
            def readable(proxy):return proxy.stream.readable()
            def record_read(proxy,count,requested):
                proxy.record['bytes']+=count;proxy.record['reads'].append((requested,count))
                if not count:proxy.record['eof']=True
                if on_read:on_read(proxy.record,count)
            def read(proxy,size):
                data=proxy.stream.read(size);proxy.record_read(len(data),size);return data
            def readinto(proxy,buffer):
                count=proxy.stream.readinto(buffer);proxy.record_read(count,len(buffer));return count
        def opened(path,*args,**kwargs):
            stream=original(path,*args,**kwargs)
            if path in (self.source,self.other) and (args and args[0]=='rb' or kwargs.get('mode')=='rb'):
                record={'path':path,'bytes':0,'eof':False,'closed':False,'reads':[]};records.append(record)
                return Reader(stream,record)
            return stream
        with patch.object(Path,'open',opened):yield records

    def assert_full_reads(self,records,paths):
        self.assertEqual([item['path'] for item in records],paths)
        for item in records:
            self.assertEqual(item['bytes'],len(self.content))
            self.assertTrue(item['eof']);self.assertTrue(item['closed'])

    def test_same_resolve_reads_all_bytes_once_and_keeps_existing_evidence_fields(self):
        baseline=self.sample()
        for stop in (None,self.stop):
            with self.subTest(cancellable=stop is not None):
                with self.source_reads() as records:
                    proof=self.sample(expected_source=self.expected,stop=stop)
                self.assertEqual(proof,baseline)
                self.assertEqual(set(proof),{'path','sha256','source_path','source_sha256'})
                self.assert_full_reads(records,[self.source])

    def test_each_same_path_expectation_is_checked_separately(self):
        for target in ('manifest','state'):
            with self.subTest(target=target):
                expected=dict(self.expected)
                if target=='manifest':expected['sha256']='0'*64
                else:self.state['identity']['source']['sha256']='0'*64;self.save_state()
                with self.source_reads() as records,patch.object(i,'verify_part') as verify:
                    with self.assertRaises(ValueError) as caught:
                        self.sample(expected_source=expected,stop=self.stop)
                if target=='manifest':self.assertIsInstance(caught.exception,i.SourceIdentityMismatch)
                else:self.assertNotIsInstance(caught.exception,i.SourceIdentityMismatch)
                self.assert_full_reads(records,[self.source]);verify.assert_not_called()

    def test_different_paths_equal_bytes_are_read_manifest_then_state(self):
        expected={'path':str(self.other),'sha256':self.digest}
        with self.source_reads() as records:
            proof=self.sample(expected_source=expected,stop=self.stop)
        self.assert_full_reads(records,[self.other,self.source])
        self.assertEqual(proof['source_path'],str(self.source))

    def test_distinct_path_syntax_resolving_to_same_file_reads_once(self):
        child=self.project/'existing-directory';child.mkdir()
        expected={'path':str(child/'..'/self.source.name),'sha256':self.digest}
        self.assertNotEqual(expected['path'],str(self.source))
        self.assertEqual(Path(expected['path']).resolve(),self.source)
        with self.source_reads() as records:
            proof=self.sample(expected_source=expected,stop=self.stop)
        self.assert_full_reads(records,[self.source])
        self.assertEqual(proof['source_sha256'],self.digest)

    def test_different_paths_each_corruption_is_rejected(self):
        expected={'path':str(self.other),'sha256':self.digest}
        for changed in (self.other,self.source):
            with self.subTest(changed=changed.name):
                changed.write_bytes(b'x'*len(self.content))
                try:
                    with self.source_reads() as records,patch.object(i,'verify_part') as verify:
                        with self.assertRaises(ValueError) as caught:
                            self.sample(expected_source=expected,stop=self.stop)
                    if changed==self.other:self.assertIsInstance(caught.exception,i.SourceIdentityMismatch)
                    else:self.assertNotIsInstance(caught.exception,i.SourceIdentityMismatch)
                    self.assert_full_reads(records,[self.other] if changed==self.other else [self.other,self.source])
                    verify.assert_not_called()
                finally:changed.write_bytes(self.content)

    def test_same_length_rewrite_with_restored_mtime_is_rejected_by_actual_read(self):
        before=self.source.stat()
        self.source.write_bytes(b'x'*len(self.content))
        os.utime(self.source,ns=(before.st_atime_ns,before.st_mtime_ns))
        after=self.source.stat()
        self.assertEqual((before.st_size,before.st_mtime_ns),(after.st_size,after.st_mtime_ns))
        with self.source_reads() as records:
            with self.assertRaises(i.SourceIdentityMismatch):
                self.sample(expected_source=self.expected,stop=self.stop)
        self.assert_full_reads(records,[self.source])

    def test_default_none_retains_legacy_call_and_source_path_is_still_strict(self):
        original=self.sample()
        self.assertEqual(self.sample(expected_source=None),original)
        with self.assertRaises(ValueError) as caught:
            self.sample(source_path=self.other,expected_source=self.expected,stop=self.stop)
        self.assertNotIsInstance(caught.exception,i.SourceIdentityMismatch)
        self.assertIn('样片实际音频',str(caught.exception))

    def test_missing_additional_source_and_directory_are_specific_identity_errors(self):
        for path in (self.project/'missing.bin',self.project):
            with self.subTest(path=path),patch.object(i,'verify_part') as verify:
                expected={'path':str(path),'sha256':self.digest}
                with self.assertRaises(i.SourceIdentityMismatch) as caught:
                    self.sample(expected_source=expected,stop=self.stop)
                self.assertIn('原始视频已变化或不存在',str(caught.exception))
                verify.assert_not_called()

    def test_additional_read_stop_is_bounded_and_does_not_read_state_source(self):
        expected={'path':str(self.other),'sha256':self.digest}
        def after_read(record,count):self.stop.set()
        with self.source_reads(after_read) as records,patch.object(i,'verify_part') as verify:
            with self.assertRaises(Cancelled):self.sample(expected_source=expected,stop=self.stop)
        self.assertEqual([item['path'] for item in records],[self.other])
        self.assertEqual(records[0]['reads'],[(1024*1024,1024*1024)])
        self.assertTrue(records[0]['closed']);verify.assert_not_called()

    def test_stop_at_additional_eof_never_opens_second_source(self):
        expected={'path':str(self.other),'sha256':self.digest}
        def after_read(record,count):
            if not count:self.stop.set()
        with self.source_reads(after_read) as records:
            with self.assertRaises(Cancelled):self.sample(expected_source=expected,stop=self.stop)
        self.assert_full_reads(records,[self.other])

    def test_additional_read_io_preserves_exact_stop_cause_and_unstopped_error(self):
        expected={'path':str(self.other),'sha256':self.digest};original=Path.open
        for stopping in (False,True):
            with self.subTest(stopping=stopping):
                self.stop.clear();failure=PermissionError('owned fixture denied')
                def opened(path,*args,**kwargs):
                    if path==self.other:
                        if stopping:self.stop.set()
                        raise failure
                    return original(path,*args,**kwargs)
                with patch.object(Path,'open',opened):
                    if stopping:
                        with self.assertRaises(Cancelled) as caught:self.sample(expected_source=expected,stop=self.stop)
                        self.assertIs(caught.exception.__cause__,failure)
                    else:
                        with self.assertRaises(PermissionError) as caught:self.sample(expected_source=expected,stop=self.stop)
                        self.assertIs(caught.exception,failure)

    def test_additional_source_vanishing_at_open_keeps_identity_error_or_stop_cause(self):
        expected={'path':str(self.other),'sha256':self.digest};original=Path.open
        for stopping in (False,True):
            with self.subTest(stopping=stopping):
                self.stop.clear();failure=FileNotFoundError('fixture disappeared after is_file')
                def opened(path,*args,**kwargs):
                    if path==self.other:
                        if stopping:self.stop.set()
                        raise failure
                    return original(path,*args,**kwargs)
                with patch.object(Path,'open',opened):
                    error_type=Cancelled if stopping else i.SourceIdentityMismatch
                    with self.assertRaises(error_type) as caught:
                        self.sample(expected_source=expected,stop=self.stop)
                self.assertIs(caught.exception.__cause__,failure)

    def test_same_resolve_alias_changes_after_read_are_rejected(self):
        original=Path.resolve;alias=self.project/'synthetic-alias'
        for kind in ('manifest','state'):
            with self.subTest(kind=kind):
                changed=False;expected=dict(self.expected)
                self.state['identity']['source']['path']=str(self.source)
                if kind=='manifest':expected['path']=str(alias)
                else:self.state['identity']['source']['path']=str(alias)
                self.save_state()
                def resolved(path,*args,**kwargs):
                    if path==alias:return self.other if changed else self.source
                    return original(path,*args,**kwargs)
                def after_read(record,count):
                    nonlocal changed
                    if not count:changed=True
                with patch.object(Path,'resolve',resolved),self.source_reads(after_read) as records, \
                        patch.object(i,'verify_part') as verify:
                    with self.assertRaises(i.SourceIdentityMismatch):
                        self.sample(expected_source=expected,stop=self.stop)
                self.assert_full_reads(records,[self.source]);verify.assert_not_called()

    def test_different_resolve_alias_changes_after_read_are_rejected(self):
        original=Path.resolve;alias=self.project/'synthetic-alias';changed=False
        expected={'path':str(alias),'sha256':self.digest}
        def resolved(path,*args,**kwargs):
            if path==alias:return self.source if changed else self.other
            return original(path,*args,**kwargs)
        def after_read(record,count):
            nonlocal changed
            if record['path']==self.source and not count:changed=True
        with patch.object(Path,'resolve',resolved),self.source_reads(after_read) as records:
            with self.assertRaises(i.SourceIdentityMismatch):self.sample(expected_source=expected,stop=self.stop)
        self.assert_full_reads(records,[self.other,self.source])

    def test_post_read_resolve_io_with_stop_keeps_original_cause(self):
        original=Path.resolve;alias=self.project/'synthetic-alias';read_done=False
        expected={'path':str(alias),'sha256':self.digest};failure=PermissionError('resolve unavailable')
        def resolved(path,*args,**kwargs):
            if path==alias:
                if read_done:self.stop.set();raise failure
                return self.source
            return original(path,*args,**kwargs)
        def after_read(record,count):
            nonlocal read_done
            if not count:read_done=True
        with patch.object(Path,'resolve',resolved),self.source_reads(after_read) as records:
            with self.assertRaises(Cancelled) as caught:self.sample(expected_source=expected,stop=self.stop)
        self.assertIs(caught.exception.__cause__,failure)
        self.assert_full_reads(records,[self.source])

    def test_proof_flags_and_malformed_expectations_are_not_accepted(self):
        bad=[{**self.expected,name:True} for name in ('proof','actual_sha256','verified','source_verified')]
        bad.extend(({}, {'path':str(self.source)}, {'sha256':self.digest}, True,
                    {'path':'','sha256':self.digest}, {'path':str(self.source),'sha256':None}))
        for expected in bad:
            with self.subTest(expected=expected):
                with self.source_reads() as records:
                    with self.assertRaises(ValueError):self.sample(expected_source=expected,stop=self.stop)
                self.assertEqual(records,[])

    def test_pre_stop_reads_nothing_with_extra_expectation(self):
        self.stop.set()
        with patch.object(Path,'open',side_effect=AssertionError('read after stop')):
            with self.assertRaises(Cancelled):self.sample(expected_source=self.expected,stop=self.stop)

    def test_late_actual_read_can_accept_early_bad_bytes_repaired_before_it(self):
        self.source.write_bytes(b'x'*len(self.content));original=Path.read_text
        def read(path,*args,**kwargs):
            text=original(path,*args,**kwargs)
            if path==self.state_path:self.source.write_bytes(self.content)
            return text
        with patch.object(Path,'read_text',read),self.source_reads() as records:
            proof=self.sample(expected_source=self.expected,stop=self.stop)
        self.assertEqual(proof['source_sha256'],self.digest)
        self.assert_full_reads(records,[self.source])


if __name__=='__main__':unittest.main()
