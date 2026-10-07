"""Explicit loopback-only desktop integration smoke; no provider calls."""
import http.client
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from urllib.parse import quote
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from subtitle_pipeline import studio


def main():
    with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ,{'LOCALAPPDATA':temporary}):
        root=Path(temporary)
        source=root/'synthetic.mp4';source.write_bytes(b'0123456789')
        project=root/'project';project.mkdir()
        subtitle='1\n00:00:01,000 --> 00:00:02,000\nテスト\n'
        (project/'原文.srt').write_text(subtitle,encoding='utf-8',newline='\n')
        static=root/'static';static.mkdir();(static/'index.html').write_text('<html>Local smoke</html>',encoding='utf-8')
        app=studio.StudioController(source=str(source),campaign=str(project),state_path=root/'state.json')
        token='test-session-not-a-provider-key'
        server=studio.ThreadingHTTPServer(('127.0.0.1',0),studio.BaseHTTPRequestHandler)
        host=f'127.0.0.1:{server.server_port}'
        server.RequestHandlerClass=studio.make_handler(app,token,host,static)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        count=0
        def request(method,path,body=None,headers=None,authorized=True):
            nonlocal count
            connection=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=5)
            fields={'X-Subtitle-Token':token} if authorized else {}
            if body is not None:fields['Content-Type']='application/json'
            fields.update(headers or {})
            connection.request(method,path,json.dumps(body) if body is not None else None,fields)
            response=connection.getresponse();content=response.read();result=(response.status,dict(response.getheaders()),content)
            connection.close();count+=1;return result
        try:
            assert request('GET','/')[0]==200
            assert request('GET','/api/state',authorized=False)[0]==403
            assert request('GET','/api/progress',authorized=False)[0]==403
            assert request('GET','/api/environment',authorized=False)[0]==403
            assert request('GET','/api/media',authorized=False)[0]==403
            assert request('POST','/api/credentials',{'provider':'siliconflow','key':'not-saved'},headers={'Origin':'https://external.invalid'})[0]==403
            secret='fake-siliconflow-key-local-smoke-only'
            saved=request('POST','/api/credentials',{'provider':'siliconflow','key':secret})
            assert saved[0]==200 and secret.encode() not in saved[2]
            state=request('GET','/api/state')
            assert state[0]==200 and secret.encode() not in state[2]
            assert json.loads(state[2])['accounts']['siliconflow']['storage']=='encrypted'
            progress=request('GET','/api/progress')
            assert progress[0]==200 and secret.encode() not in progress[2]
            assert 'accounts' not in json.loads(progress[2])
            assert json.loads(progress[2])['project_id']==json.loads(state[2])['project_id']
            report={'version':1,'checks':{'cpu':{'available':True,'message':'Offline fixture'}},
                    'recommended_encoder':'cpu','export_ready':True}
            with patch('subtitle_pipeline.environment.probe_environment',return_value=report) as probe:
                environment=request('GET','/api/environment')
                assert environment[0]==200 and json.loads(environment[2])==report
                probe.assert_called_once_with()
            assert secret.encode() not in (root/'SubtitlePipeline'/'credentials.json').read_bytes()
            session=request('POST','/api/session',{})
            cookie=session[1]['Set-Cookie']
            assert 'HttpOnly' in cookie and 'SameSite=Strict' in cookie
            assert request('GET','/api/state',headers={'Cookie':cookie.split(';')[0]},authorized=False)[0]==200
            media=request('GET','/api/media',headers={'Range':'bytes=2-5'})
            assert media[0]==206 and media[2]==b'2345'
            assert request('GET','/api/media',headers={'Range':'bytes=999-'})[0]==416
            output=request('GET','/api/download?name='+quote('原文.srt'))
            assert output[0]==200 and output[2].decode('utf-8')==subtitle
            (project/'中文草稿.srt').write_bytes(b'')
            empty=request('GET','/api/download?name='+quote('中文草稿.srt'))
            assert empty[0]==200 and empty[1]['Content-Length']=='0' and empty[2]==b''
            empty_range=request('GET','/api/download?name='+quote('中文草稿.srt'),headers={'Range':'bytes=0-'})
            assert empty_range[0]==416 and empty_range[1]['Content-Range']=='bytes */0'
            assert request('GET','/api/download?name=../state.json')[0]==400
            assert request('GET','/%2e%2e/state.json')[0]==404
            assert request('POST','/api/run',{'action':'full'})[0]==400
            with patch.object(studio,'write_public_environment') as save_prices:
                price_body={'price_per_second':0.000220,'pricing_reference':'Synthetic account price','confirmed':True}
                assert request('POST','/api/siliconflow-settings',price_body)[0]==200
                assert float(save_prices.call_args.args[0]['SILICONFLOW_ASR_CNY_PER_SECOND'])==0.000220
                save_prices.reset_mock()
                assert request('POST','/api/siliconflow-settings',{**price_body,'confirmed':False})[0]==400
                assert request('POST','/api/credentials',{'provider':'siliconflow','free_confirmed':True})[0]==400
                save_prices.assert_not_called()
            assert request('GET','/api/state',headers={'Host':'different.invalid'})[0]==403
            assert request('GET','/api/progress',headers={'Host':'different.invalid'})[0]==403
            assert request('GET','/api/environment',headers={'Host':'different.invalid'})[0]==403
            runtime=root/'runtime.json'
            expected={'url':f'http://{host}/#token={token}','pid':os.getpid()}
            studio.atomic_json(runtime,expected)
            assert studio.running_instance(runtime)==expected
            # Account saves hold this lock too: a busy state endpoint must not
            # cause the launcher to overwrite a live instance's runtime file.
            with app.lock:
                assert studio.running_instance(runtime)==expected
            studio.atomic_json(runtime,{**expected,'url':'https://external.invalid/#token='+token})
            assert studio.running_instance(runtime) is None
            print(json.dumps({'passed':True,'loopback_requests':count,'key_roundtrip':'encrypted; not returned',
                              'media_range':'passed','cross_origin':'rejected',
                              'instance_reconnect':'responsive and busy host passed','provider_requests':0},ensure_ascii=False))
        finally:
            server.shutdown();server.server_close();thread.join(3)


if __name__=='__main__':main()
