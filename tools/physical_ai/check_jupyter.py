#!/usr/bin/env python3
"""Run an isolated kernel and authenticated loopback JupyterLab startup check."""
import json
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from jupyter_client import KernelManager

ROOT = Path(__file__).resolve().parents[2]
result = {}
manager = KernelManager(kernel_name='python3')
manager.start_kernel(cwd=ROOT)
client = manager.client()
client.start_channels()
try:
    client.wait_for_ready(timeout=30)
    request_id = client.execute("import sys,numpy,cv2,mujoco; print(sys.prefix,numpy.__version__,cv2.__version__,mujoco.__version__)")
    outputs = []
    while True:
        msg = client.get_iopub_msg(timeout=30)
        if msg['parent_header'].get('msg_id') != request_id:
            continue
        if msg['msg_type'] == 'stream':
            outputs.append(msg['content']['text'])
        if msg['msg_type'] == 'error':
            raise RuntimeError(msg['content'])
        if msg['msg_type'] == 'status' and msg['content']['execution_state']=='idle':
            break
    result['kernel'] = {'status':'PASS','output':outputs}
finally:
    client.stop_channels()
    manager.shutdown_kernel(now=True)
    client = None
with socket.socket() as sock:
    sock.bind(('127.0.0.1',0))
    port = sock.getsockname()[1]
token = secrets.token_hex(24)
with tempfile.TemporaryFile(mode='w+t') as log:
    proc = subprocess.Popen([sys.executable,'-m','jupyterlab','--no-browser','--ip=127.0.0.1',
                              f'--port={port}','--ServerApp.port_retries=0',
                              f'--ServerApp.root_dir={ROOT}',f'--IdentityProvider.token={token}'],
                             stdout=log,stderr=log)
    try:
        for _ in range(100):
            if proc.poll() is not None:
                raise RuntimeError('JupyterLab exited early')
            try:
                request = urllib.request.Request(f'http://127.0.0.1:{port}/api',
                                                 headers={'Authorization':f'token {token}'})
                with urllib.request.urlopen(request, timeout=1) as response:
                    result['server'] = {'status':'PASS','http_status':response.status,
                                        'api':json.load(response),'bind':'127.0.0.1'}
                break
            except OSError:
                time.sleep(0.2)
        else:
            raise RuntimeError('JupyterLab startup timed out')
    except Exception:
        log.seek(0)
        print(log.read().replace(token,'[REDACTED]'))
        raise
    finally:
        proc.terminate()
        proc.wait(timeout=20)
result['status'] = 'PASS'
(ROOT/'docs/environment_audit/jupyter_check.json').write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps(result))
