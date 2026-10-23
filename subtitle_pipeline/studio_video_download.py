"""Verify and pin one MP4 generation without buffering its body in memory."""
import hashlib
import os


class VideoDownloadError(ValueError):
    def __init__(self, message, status):
        super().__init__(message)
        self.status = status


def stamp(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def verified_open(path, digest, checks):
    """Return the same verified handle that the HTTP response will stream."""
    lock, cache = checks
    key = str(path)
    with lock:
        stream = None
        try:
            stream = path.open('rb')
            before = stamp(os.fstat(stream.fileno())); path_before = stamp(path.stat())
            # Windows fd/path ctime may differ; compare each with itself later.
            if before[:4] != path_before[:4]:
                raise OSError('File generation changed')
            signature = digest, before, path_before
            if cache.get(key) != signature:
                cache.pop(key, None)
                actual = hashlib.file_digest(stream, 'sha256').hexdigest()
                if (actual != digest or stamp(os.fstat(stream.fileno())) != before
                        or stamp(path.stat()) != path_before):
                    raise OSError('File generation changed')
                if len(cache) >= 8: cache.pop(next(iter(cache)))
                cache[key] = signature
            stream.seek(0)
            return stream
        except OSError:
            if stream is not None: stream.close()
            cache.pop(key, None)
            return None
