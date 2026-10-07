"""Keep the default loopback origin stable so browser drafts survive restart."""
import errno
import json
from pathlib import Path

from .runner import atomic_json


def _preferred_port(path):
    try:
        path=Path(path)
        if path.stat().st_size>128:return 0
        value=json.loads(path.read_text(encoding='utf-8'))
        port=value.get('port') if isinstance(value,dict) else None
        return port if type(port) is int and 0<port<65536 else 0
    except (OSError,ValueError):return 0


def create_server(factory,handler,path,*,port=0):
    preferred=port or _preferred_port(path)
    warning=''
    try:
        server=factory(('127.0.0.1',preferred),handler)
    except OSError as error:
        if port or not preferred or (error.errno not in (errno.EADDRINUSE,errno.EACCES)
                and getattr(error,'winerror',None) not in (10048,10013)):
            raise
        server=factory(('127.0.0.1',0),handler)
        warning='上次浏览器端口被占用，已使用新端口；旧地址的本机校对草稿暂时无法读取，项目中已正式保存的字幕仍可载入。'
    if not port:
        try:atomic_json(Path(path),{'port':server.server_port})
        except OSError:
            warning+=' 本机浏览器端口暂未保存，下次重启可能无法读取此地址的校对草稿。'
    return server,warning.strip()
