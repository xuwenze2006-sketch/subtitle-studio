"""Cancellable export I/O using tiny temporary files only."""

import hashlib
import importlib
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from subtitle_pipeline.runner import Cancelled

try:
    export_io=importlib.import_module('subtitle_pipeline.export_io')
except ModuleNotFoundError:
    export_io=None


class ObservedReader:
    def __init__(self,stream,after_read=None):
        self.stream=stream
        self.after_read=after_read
        self.sizes=[]

    def read(self,size):
        self.sizes.append(size)
        data=self.stream.read(size)
        if self.after_read:self.after_read()
        return data

    def __enter__(self):return self

    def __exit__(self,*args):self.stream.close()


class ExportIOTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(export_io,'export I/O helpers are not implemented')
        self.temporary=tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root=Path(self.temporary.name)
        self.source=self.root/'source.bin'
        self.destination=self.root/'destination.bin'
        self.data=b'abcdefghij'
        self.source.write_bytes(self.data)
        self.stop=threading.Event()

    def test_hash_matches_content_using_bounded_reads_and_closes_its_file(self):
        with self.source.open('rb') as stream:
            observed=ObservedReader(stream)
            with patch.object(Path,'open',return_value=observed):
                digest=export_io.cancellable_sha256(self.source,self.stop,chunk_size=3)
            self.assertTrue(stream.closed)
        self.assertEqual(digest,hashlib.sha256(self.data).hexdigest())
        self.assertEqual(observed.sizes,[3,3,3,3,3])

    def test_cancelled_hash_does_not_open_source(self):
        self.stop.set()
        with patch.object(Path,'open',side_effect=AssertionError('cancelled hash opened source')):
            with self.assertRaises(Cancelled):
                export_io.cancellable_sha256(self.source,self.stop)

    def test_hash_cancellation_after_first_read_stops_without_reading_remaining_data(self):
        with self.source.open('rb') as stream:
            observed=ObservedReader(stream,self.stop.set)
            with patch.object(Path,'open',return_value=observed):
                with self.assertRaises(Cancelled):
                    export_io.cancellable_sha256(self.source,self.stop,chunk_size=3)
            self.assertEqual(observed.sizes,[3])
            self.assertTrue(stream.closed)

    def test_copy_hashes_exact_content_without_closing_passed_files(self):
        expected=hashlib.sha256(self.data).hexdigest()
        with self.source.open('rb') as source,self.destination.open('w+b') as destination:
            observed=ObservedReader(source)
            digest=export_io.copy_and_hash(observed,destination,self.stop,expected_hash=expected,chunk_size=3)
            self.assertFalse(source.closed)
            self.assertFalse(destination.closed)
            destination.seek(0)
            self.assertEqual(destination.read(),self.data)
        self.assertEqual(digest,expected)
        self.assertEqual(observed.sizes,[3,3,3,3,3])
        self.assertEqual(self.source.read_bytes(),self.data)

    def test_cancelled_copy_does_not_touch_either_open_file(self):
        self.stop.set()
        self.destination.write_bytes(b'keep destination')
        with self.source.open('rb') as source,self.destination.open('r+b') as destination:
            with self.assertRaises(Cancelled):
                export_io.copy_and_hash(source,destination,self.stop,chunk_size=3)
            self.assertEqual(source.tell(),0)
            self.assertEqual(destination.tell(),0)
            self.assertFalse(source.closed)
            self.assertFalse(destination.closed)
        self.assertEqual(self.destination.read_bytes(),b'keep destination')

    def test_copy_cancellation_during_read_cannot_return_success_or_write_that_chunk(self):
        with self.source.open('rb') as source,self.destination.open('w+b') as destination:
            observed=ObservedReader(source,self.stop.set)
            with self.assertRaises(Cancelled):
                export_io.copy_and_hash(observed,destination,self.stop,chunk_size=3)
            self.assertEqual(observed.sizes,[3])
            self.assertEqual(destination.tell(),0)
            self.assertFalse(source.closed)
            self.assertFalse(destination.closed)

    def test_copy_cancellation_during_write_does_not_read_another_chunk(self):
        stop=self.stop
        with self.source.open('rb') as source,self.destination.open('w+b') as destination:
            observed=ObservedReader(source)
            class CancelOnWrite:
                def write(self,data):
                    count=destination.write(data)
                    stop.set()
                    return count
            with self.assertRaises(Cancelled):
                export_io.copy_and_hash(observed,CancelOnWrite(),stop,chunk_size=3)
            self.assertEqual(observed.sizes,[3])
            destination.seek(0)
            self.assertEqual(destination.read(),b'abc')
            self.assertFalse(source.closed)
            self.assertFalse(destination.closed)

    def test_expected_hash_mismatch_is_not_success(self):
        with self.source.open('rb') as source,self.destination.open('w+b') as destination:
            with self.assertRaises(ValueError):
                export_io.copy_and_hash(source,destination,self.stop,expected_hash='0'*64,chunk_size=3)
            self.assertFalse(source.closed)
            self.assertFalse(destination.closed)

    def test_destination_disk_error_propagates_without_reading_remaining_source(self):
        with self.source.open('rb') as source,self.destination.open('w+b') as destination:
            observed=ObservedReader(source)
            class BrokenWriter:
                def write(self,data):raise OSError('synthetic disk full')
            with self.assertRaisesRegex(OSError,'synthetic disk full'):
                export_io.copy_and_hash(observed,BrokenWriter(),self.stop,chunk_size=3)
            self.assertEqual(observed.sizes,[3])
            self.assertEqual(destination.tell(),0)
            self.assertFalse(source.closed)
            self.assertFalse(destination.closed)

    def test_copyfileobj_fault_injection_remains_supported(self):
        def broken_copy(source,destination,*args):raise OSError('injected copy failure')
        with self.source.open('rb') as source,self.destination.open('w+b') as destination:
            with patch.object(export_io.shutil,'copyfileobj',side_effect=broken_copy):
                with self.assertRaisesRegex(OSError,'injected copy failure'):
                    export_io.copy_and_hash(source,destination,self.stop)
            self.assertEqual(source.tell(),0)
            self.assertEqual(destination.tell(),0)
            self.assertFalse(source.closed)
            self.assertFalse(destination.closed)

    def test_short_destination_write_cannot_report_success(self):
        with self.source.open('rb') as source,self.destination.open('w+b') as destination:
            observed=ObservedReader(source)
            class ShortWriter:
                def write(self,data):return destination.write(data[:1])
            with self.assertRaises(OSError):
                export_io.copy_and_hash(observed,ShortWriter(),self.stop,chunk_size=3)
            self.assertEqual(observed.sizes,[3])
            destination.seek(0)
            self.assertEqual(destination.read(),b'a')
            self.assertFalse(source.closed)
            self.assertFalse(destination.closed)

    def test_empty_file_hash_and_copy_are_valid(self):
        self.source.write_bytes(b'')
        expected=hashlib.sha256(b'').hexdigest()
        self.assertEqual(export_io.cancellable_sha256(self.source,self.stop),expected)
        with self.source.open('rb') as source,self.destination.open('w+b') as destination:
            self.assertEqual(export_io.copy_and_hash(source,destination,self.stop,expected_hash=expected),expected)
        self.assertEqual(self.destination.read_bytes(),b'')

    def test_nonpositive_or_noninteger_chunks_are_rejected_before_io(self):
        for size in (0,-1,True,1.5):
            with self.subTest(size=size),self.source.open('rb') as source,self.destination.open('w+b') as destination:
                with self.assertRaises(ValueError):
                    export_io.cancellable_sha256(self.source,self.stop,chunk_size=size)
                with self.assertRaises(ValueError):
                    export_io.copy_and_hash(source,destination,self.stop,chunk_size=size)
                self.assertEqual(source.tell(),0)
                self.assertEqual(destination.tell(),0)

    def test_known_hash_copy_transfers_exact_bytes_without_constructing_a_hasher(self):
        with self.source.open('rb') as source,self.destination.open('w+b') as destination, \
             patch.object(export_io.hashlib,'sha256',side_effect=AssertionError('known digest must not be recomputed')):
            observed=ObservedReader(source)
            self.assertTrue(hasattr(export_io,'cancellable_copy'),'cancellable copy helper is missing')
            self.assertIsNone(export_io.cancellable_copy(observed,destination,self.stop,chunk_size=3))
            self.assertEqual(observed.sizes,[3,3,3,3,3])
            destination.seek(0)
            self.assertEqual(destination.read(),self.data)
            self.assertFalse(source.closed)
            self.assertFalse(destination.closed)

    def test_known_hash_copy_checks_cancellation_before_and_during_copy(self):
        self.assertTrue(hasattr(export_io,'cancellable_copy'),'cancellable copy helper is missing')
        for when in ('before','reading','writing'):
            with self.subTest(when=when),self.source.open('rb') as source,self.destination.open('w+b') as destination, \
                 patch.object(export_io.hashlib,'sha256',side_effect=AssertionError('known digest must not be recomputed')):
                self.stop.clear()
                if when=='before':self.stop.set()
                observed=ObservedReader(source,self.stop.set if when=='reading' else None)
                stop=self.stop
                class CancelOnWrite:
                    def write(self,data):
                        count=destination.write(data)
                        stop.set()
                        return count
                writer=CancelOnWrite() if when=='writing' else destination
                with self.assertRaises(Cancelled):
                    export_io.cancellable_copy(observed,writer,stop,chunk_size=3)
                self.assertEqual(observed.sizes,[] if when=='before' else [3])
                destination.seek(0)
                self.assertEqual(destination.read(),b'abc' if when=='writing' else b'')
                self.assertFalse(source.closed)
                self.assertFalse(destination.closed)

    def test_known_hash_copy_rejects_short_writes_without_constructing_a_hasher(self):
        self.assertTrue(hasattr(export_io,'cancellable_copy'),'cancellable copy helper is missing')
        with self.source.open('rb') as source,self.destination.open('w+b') as destination, \
             patch.object(export_io.hashlib,'sha256',side_effect=AssertionError('known digest must not be recomputed')):
            observed=ObservedReader(source)
            class ShortWriter:
                def write(self,data):return destination.write(data[:1])
            with self.assertRaises(OSError):
                export_io.cancellable_copy(observed,ShortWriter(),self.stop,chunk_size=3)
            self.assertEqual(observed.sizes,[3])
            destination.seek(0)
            self.assertEqual(destination.read(),b'a')
            self.assertFalse(source.closed)
            self.assertFalse(destination.closed)

    @unittest.skipUnless(os.name=='nt','Windows sharing conflicts and no-overwrite rename')
    def test_rename_retries_only_transient_windows_sharing_errors(self):
        self.assertTrue(hasattr(export_io,'rename_with_retry'))
        original=os.rename
        for code in (5,32,33):
            source=self.root/f'source-{code}.bin';source.write_bytes(self.data)
            destination=self.root/f'destination-{code}.bin'
            calls=[]
            def rename(src,dst):
                calls.append((src,dst))
                if len(calls)<3:
                    error=PermissionError('file briefly locked');error.winerror=code
                    raise error
                return original(src,dst)
            with patch.object(export_io.os,'rename',side_effect=rename),patch.object(self.stop,'wait',return_value=False):
                export_io.rename_with_retry(source,destination,self.stop)
            self.assertEqual(destination.read_bytes(),self.data)
            self.assertFalse(source.exists())
            self.assertEqual(len(calls),3)
            self.assertTrue(all(pair==(source,destination) for pair in calls))

    @unittest.skipUnless(os.name=='nt','Windows sharing conflicts and no-overwrite rename')
    def test_permanent_or_unrelated_rename_errors_do_not_retry_forever(self):
        self.assertTrue(hasattr(export_io,'rename_with_retry'))
        for code,expected_attempts in ((32,6),(19,1)):
            error=PermissionError('persistent error');error.winerror=code
            with patch.object(export_io.os,'rename',side_effect=error) as rename, \
                    patch.object(self.stop,'wait',return_value=False) as wait:
                with self.assertRaises(PermissionError) as raised:
                    export_io.rename_with_retry(self.source,self.destination,self.stop)
            self.assertIs(raised.exception,error)
            self.assertEqual(rename.call_count,expected_attempts)
            self.assertLessEqual(sum(call.args[0] for call in wait.call_args_list),1)
            self.assertTrue(self.source.exists())
            self.assertFalse(self.destination.exists())

    @unittest.skipUnless(os.name=='nt','Windows sharing conflicts and no-overwrite rename')
    def test_rename_backoff_is_cancellable_without_removing_source(self):
        self.assertTrue(hasattr(export_io,'rename_with_retry'))
        error=PermissionError('briefly locked');error.winerror=32
        def stop_wait(delay):self.stop.set();return True
        with patch.object(export_io.os,'rename',side_effect=error) as rename, \
                patch.object(self.stop,'wait',side_effect=stop_wait):
            with self.assertRaises(Cancelled):
                export_io.rename_with_retry(self.source,self.destination,self.stop)
        self.assertEqual(rename.call_count,1)
        self.assertTrue(self.source.exists())
        self.assertFalse(self.destination.exists())

    @unittest.skipUnless(os.name=='nt','Windows sharing conflicts and no-overwrite rename')
    def test_rename_keeps_windows_no_overwrite_semantics(self):
        self.assertTrue(hasattr(export_io,'rename_with_retry'))
        self.destination.write_bytes(b'other output')
        with self.assertRaises(FileExistsError):
            export_io.rename_with_retry(self.source,self.destination,self.stop)
        self.assertEqual(self.destination.read_bytes(),b'other output')
        self.assertEqual(self.source.read_bytes(),self.data)

    @unittest.skipUnless(os.name=='nt','Windows cleanup sharing conflicts')
    def test_cleanup_retries_only_the_owned_path_and_removes_it(self):
        self.assertTrue(hasattr(export_io,'unlink_with_retry'))
        original=Path.unlink
        other=self.root/'other.tmp';other.write_bytes(b'keep')
        attempts=[]
        def unlink(path,*args,**kwargs):
            attempts.append(path)
            if len(attempts)<3:
                error=PermissionError('cleanup briefly locked');error.winerror=33
                raise error
            return original(path,*args,**kwargs)
        with patch.object(Path,'unlink',unlink),patch.object(self.stop,'wait',return_value=False):
            export_io.unlink_with_retry(self.source,self.stop)
        self.assertEqual(attempts,[self.source]*3)
        self.assertFalse(self.source.exists())
        self.assertEqual(other.read_bytes(),b'keep')

    def test_cleanup_attempts_removal_even_when_export_was_cancelled(self):
        self.assertTrue(hasattr(export_io,'unlink_with_retry'))
        self.stop.set()
        export_io.unlink_with_retry(self.source,self.stop)
        self.assertFalse(self.source.exists())

    @unittest.skipUnless(os.name=='nt','Windows cleanup sharing conflicts')
    def test_cleanup_backoff_is_cancellable_but_nonsharing_errors_never_retry(self):
        for code,cancel in ((32,True),(19,False)):
            with self.subTest(code=code):
                error=PermissionError('cleanup error');error.winerror=code
                def wait(delay):self.stop.set();return True
                self.stop.clear()
                with patch.object(Path,'unlink',side_effect=error) as unlink, \
                        patch.object(self.stop,'wait',side_effect=wait) as backoff:
                    with self.assertRaises(Cancelled if cancel else PermissionError):
                        export_io.unlink_with_retry(self.source,self.stop)
                self.assertEqual(unlink.call_count,1)
                self.assertEqual(backoff.call_count,1 if cancel else 0)
                self.assertEqual(self.source.read_bytes(),self.data)


if __name__=='__main__':unittest.main()
