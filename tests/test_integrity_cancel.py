"""Cancellable evidence reads use only temporary, offline source/subtitle files."""
from contextlib import ExitStack
from dataclasses import asdict
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from subtitle_pipeline import integrity as i, export_io
from subtitle_pipeline.runner import Cancelled
from subtitle_pipeline.subtitles import Chunk, Cue, render_srt


class IntegrityCancellationTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory(prefix='integrity-cancel-')
        self.addCleanup(temporary.cleanup)
        self.project=Path(temporary.name)
        self.source=self.project/'input.wav'
        self.source.write_bytes(b'synthetic-source-block\0'*(150000))
        source_hash=i.sha256(self.source)
        self.chunks=[Chunk(0,0,1000,0,1000),Chunk(1,1000,2000,1000,2000)]
        self.state={'status':'complete','duration_ms':2000,
            'identity':{'engine':'qwen_asr','asr_model':'qwen-audio-3.1-asr-flash',
                'api':'dashscope-v1','translation_model':'deepseek-flash','language':'ja','target':'zh-CN',
                'source':{'path':str(self.source),'sha256':source_hash}},
            'chunks':[asdict(chunk) for chunk in self.chunks],'parts':{}}
        for chunk in self.chunks:
            folder=self.part_folder(chunk.index);folder.mkdir(parents=True)
            (folder/'source.local.srt').write_text(render_srt([Cue(100,800,'こんにちは')]),encoding='utf-8')
            (folder/'target.local.srt').write_text(render_srt([Cue(100,800,'你好')]),encoding='utf-8')
            (folder/'asr-response.json').write_bytes(b'{"offline":true}')
            self.state['parts'][str(chunk.index)]={'asr':'done','translation':'done',
                'asr_evidence':{'provider':'qwen_asr','model':'qwen-audio-3.1-asr-flash',
                    'api':'dashscope-v1','language':'ja','source_sha256':source_hash,
                    'audio_range_ms':[chunk.audio_start_ms,chunk.audio_end_ms]},
                'source_hash':i.sha256(folder/'source.local.srt'),
                'translation_source_hash':i.sha256(folder/'source.local.srt'),
                'target_hash':i.sha256(folder/'target.local.srt'),
                'raw_response_hash':i.sha256(folder/'asr-response.json')}
        self.state_path=self.project/'state.json'
        self.state_path.write_text(json.dumps(self.state,ensure_ascii=False),encoding='utf-8')
        self.stop=threading.Event()
        guards=ExitStack();self.addCleanup(guards.close)
        guards.enter_context(patch('socket.create_connection',side_effect=AssertionError('offline only')))
        guards.enter_context(patch('socket.socket.connect',side_effect=AssertionError('offline only')))

    def part_folder(self,index):return self.project/'片段'/f'{index+1:04d}'

    def part(self,**kwargs):
        return i.verify_part(self.part_folder(0),self.chunks[0],self.state['parts']['0'],
            translated=True,cloud=True,provider='qwen_asr',model='qwen-audio-3.1-asr-flash',
            api='dashscope-v1',language='ja',source_sha256=self.state['identity']['source']['sha256'],**kwargs)

    def sample(self,**kwargs):return i.sample_state_evidence(self.project,source_path=self.source,**kwargs)

    def test_preexisting_stop_performs_no_state_or_part_reads(self):
        self.stop.set()
        with patch.object(Path,'open',side_effect=AssertionError('opened after stop')), \
                patch.object(Path,'read_text',side_effect=AssertionError('read after stop')):
            for operation in (self.part,self.sample):
                with self.subTest(operation=operation.__name__),self.assertRaises(Cancelled):
                    operation(stop=self.stop)

    def test_source_hash_stops_after_one_bounded_read(self):
        original=Path.open;reads=[];streams=[]
        class Reader:
            def __init__(proxy,stream):proxy.stream=stream
            def __enter__(proxy):return proxy
            def __exit__(proxy,*args):proxy.stream.close()
            def readable(proxy):return proxy.stream.readable()
            def readinto(proxy,buffer):
                count=proxy.stream.readinto(buffer);reads.append((len(buffer),count));self.stop.set();return count
            def read(proxy,count):
                result=proxy.stream.read(count);reads.append((count,len(result)));self.stop.set();return result
        def opened(path,*args,**kwargs):
            stream=original(path,*args,**kwargs)
            if path==self.source:
                streams.append(stream)
                return Reader(stream)
            return stream
        with patch.object(Path,'open',opened),patch.object(i,'verify_part') as verify:
            with self.assertRaises(Cancelled):self.sample(stop=self.stop)
        self.assertEqual(reads,[(1024*1024,1024*1024)])
        self.assertTrue(streams and all(stream.closed for stream in streams))
        verify.assert_not_called()

    def test_stop_after_state_read_precedes_json_parsing(self):
        original=Path.read_text
        def read(path,*args,**kwargs):
            value=original(path,*args,**kwargs)
            if path==self.state_path:self.stop.set()
            return value
        with patch.object(Path,'read_text',read),patch.object(i.json,'loads',side_effect=AssertionError('parse after stop')):
            with self.assertRaises(Cancelled):self.sample(stop=self.stop)

    def test_stop_during_model_check_precedes_source_hash(self):
        loads=json.loads
        class Identity(dict):
            def get(inner,name,*args):
                value=super().get(name,*args)
                if name=='api':self.stop.set()
                return value
        def parsed(*args,**kwargs):
            state=loads(*args,**kwargs);state['identity']=Identity(state['identity']);return state
        with patch.object(i.json,'loads',side_effect=parsed), \
                patch.object(export_io,'cancellable_sha256',side_effect=AssertionError('source hash after stop')):
            with self.assertRaises(Cancelled):self.sample(stop=self.stop)

    def test_stop_after_each_part_never_verifies_another_or_returns_evidence(self):
        original=i.verify_part
        for complete in (True,False):
            with self.subTest(complete=complete):
                self.stop.clear();calls=[]
                def verify(*args,**kwargs):
                    valid=original(*args,**kwargs);calls.append(args[1].index);self.stop.set()
                    return valid if complete else False
                with patch.object(i,'verify_part',side_effect=verify):
                    with self.assertRaises(Cancelled):self.sample(stop=self.stop)
                self.assertEqual(calls,[0])

    def test_stop_after_subtitle_parse_does_not_return_false_or_continue_hashing(self):
        parser=i.parse_srt
        def parsed(*args):
            cues=parser(*args);self.stop.set();return cues
        with patch.object(i,'parse_srt',side_effect=parsed), \
                patch.object(export_io,'cancellable_sha256',wraps=export_io.cancellable_sha256) as hashed:
            with self.assertRaises(Cancelled):self.part(stop=self.stop)
        self.assertEqual([Path(c.args[0]).name for c in hashed.call_args_list],['source.local.srt'])

    def test_stop_after_last_state_hash_does_not_return_evidence(self):
        original=export_io.cancellable_sha256
        def hashed(path,stop,**kwargs):
            result=original(path,stop,**kwargs)
            if path==self.state_path:self.stop.set()
            return result
        with patch.object(export_io,'cancellable_sha256',side_effect=hashed):
            with self.assertRaises(Cancelled):self.sample(stop=self.stop)

    def test_io_with_stop_preserves_exact_cause_and_without_stop_keeps_legacy_semantics(self):
        original=Path.open
        for operation,path in ((self.part,self.part_folder(0)/'source.local.srt'),(self.sample,self.source)):
            for cancelled in (False,True):
                with self.subTest(operation=operation.__name__,cancelled=cancelled):
                    self.stop.clear();failure=PermissionError('synthetic denied')
                    def opened(candidate,*args,**kwargs):
                        if candidate==path:
                            if cancelled:self.stop.set()
                            raise failure
                        return original(candidate,*args,**kwargs)
                    with patch.object(Path,'open',opened):
                        if cancelled:
                            with self.assertRaises(Cancelled) as caught:operation(stop=self.stop)
                            self.assertIs(caught.exception.__cause__,failure)
                        elif operation==self.part:
                            self.assertFalse(operation(stop=self.stop))
                        else:
                            with self.assertRaises(PermissionError) as caught:operation(stop=self.stop)
                            self.assertIs(caught.exception,failure)

    def test_state_read_io_with_stop_preserves_exact_cause(self):
        failure=OSError('state denied')
        def read(*args,**kwargs):self.stop.set();raise failure
        with patch.object(Path,'read_text',side_effect=read):
            with self.assertRaises(Cancelled) as caught:self.sample(stop=self.stop)
        self.assertIs(caught.exception.__cause__,failure)

    def test_default_calls_remain_compatible_and_all_stop_hashes_are_cancellable(self):
        with patch.object(export_io,'cancellable_sha256',side_effect=AssertionError('default path changed')):
            self.assertTrue(self.part())
            expected=self.sample()
            self.assertEqual(self.sample(stop=None),expected)
        with patch.object(i,'sha256',side_effect=AssertionError('unbounded hash with stop')), \
                patch.object(export_io,'cancellable_sha256',wraps=export_io.cancellable_sha256) as hashed:
            self.assertEqual(self.sample(stop=self.stop),expected)
        paths=[Path(call.args[0]) for call in hashed.call_args_list]
        self.assertEqual(paths.count(self.source),1)
        self.assertEqual(paths.count(self.state_path),1)
        self.assertEqual(len(paths),8) # source + two complete source/raw/target parts + state.

    def test_changed_content_and_missing_part_still_fail_closed(self):
        for path in (self.source,self.part_folder(0)/'source.local.srt',
                     self.part_folder(1)/'target.local.srt',self.part_folder(1)/'asr-response.json'):
            with self.subTest(path=path.name):
                content=path.read_bytes();path.write_bytes(content+b'changed')
                try:
                    with self.assertRaises(ValueError):self.sample(stop=self.stop)
                finally:path.write_bytes(content)
        (self.part_folder(1)/'asr-response.json').unlink()
        with self.assertRaises(ValueError):self.sample(stop=self.stop)

    def test_non_io_verification_failures_keep_false_or_original_error(self):
        with patch.object(i,'parse_srt',side_effect=ValueError('malformed subtitles')):
            self.assertFalse(self.part(stop=self.stop))
        error=TypeError('unrelated programming failure')
        with patch.object(i,'parse_srt',side_effect=error):
            with self.assertRaises(TypeError) as caught:self.part(stop=self.stop)
        self.assertIs(caught.exception,error)

    def test_parse_error_with_stop_is_not_swallowed_as_invalid_evidence(self):
        for operation in (self.part,self.sample):
            with self.subTest(operation=operation.__name__):
                self.stop.clear();error=ValueError('malformed subtitles during stop')
                def parsed(*args):self.stop.set();raise error
                with patch.object(i,'parse_srt',side_effect=parsed):
                    with self.assertRaises(Cancelled) as caught:operation(stop=self.stop)
                self.assertIs(caught.exception.__cause__,error)


if __name__=='__main__':unittest.main()
