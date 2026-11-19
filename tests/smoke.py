from pathlib import Path
from tempfile import TemporaryDirectory
import json, os, shutil, subprocess, sys, textwrap, urllib.request, urllib.error
import wave

src = Path(__file__).resolve().parents[1] / "srt_studio.py"
with TemporaryDirectory() as t:
    root = Path(t)
    program = root / src.name
    shutil.copy2(src, program)
    testhome=root/'home'
    (testhome/'Downloads').mkdir(parents=True)
    ext = root/'fake'; ext.mkdir(); (ext/'whisper').mkdir()
    settings=root/'settings_whisper_gpu.json'
    settings.write_text(json.dumps({"model":"turbo","preset":"Fast","language":"English","keep_model_loaded":False}))
    (ext/'torch.py').write_text(textwrap.dedent("""
        import types
        __version__='2.9.1+cu128'
        version=types.SimpleNamespace(cuda='12.8')
        class GPU:
            def is_available(self): return True
            def device_count(self): return 1
            def get_device_properties(self, idx):
                return types.SimpleNamespace(name='NVIDIA GeForce RTX 5070 Ti', total_memory=16*1024**3, major=12, minor=0)
            def mem_get_info(self, idx): return 13*1024**3,16*1024**3
            def device(self, name):
                from contextlib import nullcontext
                return nullcontext()
            def empty_cache(self): pass
        cuda=GPU()
        backends=types.SimpleNamespace(cudnn=types.SimpleNamespace(),cuda=types.SimpleNamespace(matmul=types.SimpleNamespace()))
        def set_num_threads(n): pass
        def set_float32_matmul_precision(s): pass
        """))
    (ext/'tqdm.py').write_text(textwrap.dedent("""
        class tqdm:
            def __init__(self, total=1, **kw): self.n=0; self.total=total
            def update(self,n=1): self.n+=n
            def close(self): pass
        """))
    (ext/'whisper'/'transcribe.py').write_text(textwrap.dedent("""
        from tqdm import tqdm
        import time
        def transcribe(**kw):
            assert kw['task'] in ('transcribe','translate')
            if kw.get('audio','').endswith('error.wav'): raise RuntimeError('Simulated decode error')
            bar=tqdm.tqdm(total=24)
            try:
                for i in range(24):
                    bar.update()
                    time.sleep(.028)
            finally: bar.close()
            return {'segments':[{'start':.15,'end':1.26,'text':'Test caption.'}], 'language':'en'}
        """))
    (ext/'whisper'/'__init__.py').write_text(textwrap.dedent("""
        from .transcribe import transcribe
        import time
        def available_models(): return ['tiny','base','medium','large','turbo']
        class Model:
            def transcribe(self, **kw): return transcribe(**kw)
        def load_model(name, device):
            assert device in ('cpu','cuda:0')
            time.sleep(.37)
            return Model()
        """))
    for name in ('Original TAKE 01.wav','Original TAKE 02.wav','Browser clip & notes.wav','error.wav'):
        with wave.open(str(root/name),'wb') as f:
            f.setparams((1,2,8000,0,'NONE','not compressed'))
            f.writeframes(bytes(1000))
    (ext/'tkinter.py').write_text(textwrap.dedent("""
        import os,types
        class Tk:
            def withdraw(self): pass
            def attributes(self,*a): pass
            def destroy(self): pass
        filedialog=types.SimpleNamespace(askopenfilenames=lambda **kw: [os.environ['TEST_ORIGINAL']], askdirectory=lambda **kw: os.environ['TEST_OUTPUT'])
        """))
    env={**os.environ,'PYTHONPATH':str(ext),'TEST_ORIGINAL':str(root/'Original TAKE 01.wav'),
         'TEST_OUTPUT':str(root/'chosen-folder'),'HOME':str(testhome),'USERPROFILE':str(testhome)}
    proc=subprocess.Popen([sys.executable,str(program),'--no-browser'],env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    try:
        assert proc.stdout.readline().strip()=='SRT Studio'
        url=proc.stdout.readline().strip()
        assert url.startswith("http://127.0.0.1:")
        base, token=url.split('/#')
        def api(path, data=None):
            headers={'X-Studio-Token':token,'Content-Type':'application/json'}
            req=urllib.request.Request(base+'/api/'+path,json.dumps(data).encode() if data is not None else None,headers)
            with urllib.request.urlopen(req,timeout=8) as response: return json.load(response)
        try:
            urllib.request.urlopen(base+'/api/state')
        except urllib.error.HTTPError as e: assert e.code==403
        else: raise AssertionError('token was not enforced')

        import time
        from urllib.parse import quote
        def until(fn):
            end = time.monotonic() + 8
            while time.monotonic() < end:
                val=fn()
                if val: return val
                time.sleep(.045)
            raise AssertionError(api('state'))
        def state(): return api('state')
        def upload(name):
            headers={'X-Studio-Token':token,'Content-Type':'application/octet-stream','X-Media-Name':quote(name)}
            req=urllib.request.Request(base+'/api/upload',(root/name).read_bytes(),headers)
            with urllib.request.urlopen(req,timeout=8) as r: return json.load(r)
        assert len(urllib.request.urlopen(base+'/').read()) > 30_000
        until(lambda: state()['engine']['state']=='ready')
        assert state()['config']['preset']=='Fast' and state()['config']['language']=='English'
        assert state()['devices'][0]['total']==16
        assert state()['engine']['precise_progress']
        assert api('picker',{'mode':'files'})['added']==1
        assert api('picker',{'mode':'files'})['added']==0
        assert state()['rows'][0]['source']==str(root)
        assert upload('Browser clip & notes.wav')['added']==1
        rowid=state()['rows'][1]['id']
        api('queue',{'action':'up','ids':[rowid]})
        assert state()['rows'][0]['id']==rowid
        cfg={**state()['config'],'fmt_vtt':True,'fmt_txt':True}
        api('start',{'config':cfg})
        until(lambda: state()['batch']['phase']=='running')
        until(lambda: (state()['batch']['file_progress'] or 0)>0)
        until(lambda: state()['batch']['phase']=='finished')
        assert state()['batch']['done']==2
        assert (root/'Original TAKE 01.srt').read_text()=='1\n00:00:00,150 --> 00:00:01,260\nTest caption.\n\n'
        assert (root/'Original TAKE 01.vtt').read_text().startswith('WEBVTT\n\n00:00:00.150')
        assert (testhome/'Downloads'/'SRT Studio'/'Browser clip & notes.txt').read_text()=='Test caption.\n'
        api('start',{'config':cfg})
        until(lambda: state()['batch']['phase']=='finished')
        assert state()['batch']['skipped']==2
        assert api('picker',{'mode':'output'})['dir']==str(root/'chosen-folder')
        cfg=state()['config']
        api('start',{'config':cfg})
        until(lambda: state()['batch']['phase']=='running')
        api('cancel',{})
        until(lambda: state()['batch']['phase']=='cancelled')
        assert all(row['status']=='Cancelled' for row in state()['rows'])
        api('start',{'config':cfg})
        until(lambda: state()['batch']['phase']=='finished')
        assert state()['batch']['done']==2
        assert (root/'chosen-folder'/'Original TAKE 01.srt').exists()
        assert (root/'chosen-folder'/'Browser clip & notes.vtt').exists()
        try:
            api('start',{'config':dict(cfg,task='translate')})
        except urllib.error.HTTPError as e:
            assert e.code==400 and b'multilingual model' in e.read()
        else: raise AssertionError('turbo translate was not blocked')
        api('queue',{'action':'remove','ids':[rowid]})
        assert len(state()['rows'])==1 and (root/'chosen-folder'/'Browser clip & notes.vtt').exists()
        api('queue',{'action':'clear'})
        assert len(state()['rows'])==0
        cfg=api('reset',{})['config']
        assert cfg['model']=='turbo'
        assert json.loads(settings.read_text())==cfg
        assert len(state()['events'])>4
        api('shutdown',{})
        assert proc.wait(8)==0
        print('PASS — native dialog integration, browser upload API, queue ordering, settings, CUDA telemetry, progress, all exports, skip, cancellation, validation, and shutdown')
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(6)
        err=proc.stderr.read()
        if err: print(err)
