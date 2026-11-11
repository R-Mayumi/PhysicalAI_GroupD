#!/usr/bin/env python3
"""Fetch verified official registry layers and upload through Ollama's blob API.

Fallback for a stalled server-side pull. Does not write the service's model store
directly or change/restart the service. An ordinary pull installs the manifest.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import http.client
import json
import os
from pathlib import Path
import shutil
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = Path(__file__).resolve().parents[2]
DEST = ROOT/'docs/environment_audit'


def fetch_layer(url, path, size, tag, digest):
    state_path = path.with_suffix('.parts.json')
    if state_path.exists():
        state = json.loads(state_path.read_text())
        prefix, completed = state['prefix'], set(state['completed'])
    else:
        # Reuse only a sequential prefix from the first attempt, if present.
        prefix, completed = min(path.stat().st_size,size) if path.exists() else 0, set()
    chunks = [(start,min(start+32*1024*1024,size)-1)
              for start in range(prefix,size,32*1024*1024)]
    fd = os.open(path,os.O_CREAT|os.O_RDWR,0o600)
    os.ftruncate(fd,size)
    lock = threading.Lock()
    started = time.monotonic()
    def save_state():
        state_path.write_text(json.dumps({'prefix':prefix,'completed':sorted(completed)})+'\n')
        transferred = prefix+sum(end-start+1 for start,end in chunks if start in completed)
        (DEST/'model_download_progress.json').write_text(json.dumps({
            'tag':tag,'digest':digest,'bytes':transferred,'total':size,
            'elapsed_s':time.monotonic()-started})+'\n')
    save_state()
    def transfer(chunk):
        start,end = chunk
        if start in completed:
            return
        for attempt in range(3):
            try:
                request = urllib.request.Request(url,headers={'Range':f'bytes={start}-{end}'})
                with urllib.request.urlopen(request,timeout=60) as source:
                    if source.status!=206 or not source.headers.get('Content-Range','').startswith(f'bytes {start}-{end}/'):
                        raise RuntimeError('Server did not honor exact byte range')
                    position = start
                    while position<=end:
                        block = source.read(min(1024*1024,end-position+1))
                        if not block:
                            raise RuntimeError('Truncated range')
                        os.pwrite(fd,block,position)
                        position += len(block)
                with lock:
                    completed.add(start)
                    save_state()
                return
            except Exception:
                if attempt==2:
                    raise
                time.sleep(1)
    try:
        with ThreadPoolExecutor(max_workers=16) as pool:
            for future in as_completed([pool.submit(transfer,chunk) for chunk in chunks]):
                future.result()
    finally:
        os.close(fd)
    hasher = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):
            hasher.update(block)
    if 'sha256:'+hasher.hexdigest()!=digest:
        raise RuntimeError('Registry layer hash mismatch')
    return state_path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tag', required=True, choices=['4b-q4_K_M','8b-q4_K_M'])
    args = parser.parse_args()
    fields = dict((line.split(':')[0],int(line.split()[1])*1024)
                  for line in Path('/proc/meminfo').read_text().splitlines())
    before = {'timestamp':datetime.now(timezone.utc).isoformat(),
              'disk_free_bytes':shutil.disk_usage(ROOT).free,
              'mem_available_bytes':fields['MemAvailable'],
              'swap_used_bytes':fields['SwapTotal']-fields['SwapFree']}
    if before['disk_free_bytes'] < 15*1024**3 or before['mem_available_bytes'] < 7*1024**3:
        raise SystemExit('Requires 15 GiB disk free')
    base = 'https://registry.ollama.ai/v2/library/qwen3/'
    request = urllib.request.Request(base+'manifests/'+args.tag,
        headers={'Accept':'application/vnd.docker.distribution.manifest.v2+json'})
    with urllib.request.urlopen(request, timeout=30) as response:
        manifest = json.load(response)
    result = {'started_at':datetime.now(timezone.utc).isoformat(),'tag':args.tag,'before':before,
              'manifest':manifest,'layers':[]}
    cache = ROOT/'physical_ai/model_downloads'
    cache.mkdir(parents=True, exist_ok=True)
    for layer in [manifest['config'],*manifest['layers']]:
        digest, size = layer['digest'], layer['size']
        record = {'digest':digest, 'size_bytes':size}
        connection = http.client.HTTPConnection('127.0.0.1',11434,timeout=60)
        connection.request('HEAD','/api/blobs/'+digest)
        response = connection.getresponse()
        response.read()
        exists = response.status==200
        connection.close()
        if exists:
            record['status']='EXISTING'
        else:
            path = cache/digest.replace(':','-')
            start = time.monotonic()
            state_path = fetch_layer(base+'blobs/'+digest,path,size,args.tag,digest)
            connection = http.client.HTTPConnection('127.0.0.1',11434,timeout=300)
            with path.open('rb') as stream:
                connection.request('POST','/api/blobs/'+digest,body=stream,
                    headers={'Content-Length':str(size),'Content-Type':'application/octet-stream'})
                response = connection.getresponse()
                response.read()
                if response.status not in (200,201):
                    raise RuntimeError(f'Blob upload failed: HTTP {response.status}')
            connection.close()
            path.unlink()  # Only this script's verified temporary download.
            state_path.unlink(missing_ok=True)
            record.update(status='VERIFIED_AND_UPLOADED',elapsed_s=time.monotonic()-start)
        result['layers'].append(record)
        print(json.dumps(record),flush=True)
    result['finished_at']=datetime.now(timezone.utc).isoformat()
    (DEST/('registry_cache_'+args.tag+'.json')).write_text(json.dumps(result,indent=2)+'\n')


if __name__=='__main__':
    main()
