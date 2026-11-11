#!/usr/bin/env python3
"""Inventory/pull/benchmark the existing local Ollama, without robot commands."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import subprocess
import threading
import time
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
DEST = ROOT/'docs/environment_audit'
BASE = 'http://127.0.0.1:11434/api/'


def api(endpoint, body=None, timeout=300):
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(BASE+endpoint, data=data, headers={'Content-Type':'application/json'})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def memory():
    fields = dict((line.split(':')[0], int(line.split()[1])*1024)
                  for line in Path('/proc/meminfo').read_text().splitlines())
    p = subprocess.run(['ps','-eo','pid=,ppid=,comm=,rss='], text=True, capture_output=True)
    processes = [line.split() for line in p.stdout.splitlines() if len(line.split())==4]
    selected = {int(pid) for pid,ppid,comm,rss in processes if comm=='ollama'}
    while True:
        more = {int(pid) for pid,ppid,comm,rss in processes if int(ppid) in selected}
        if more <= selected:
            break
        selected |= more
    return {'time':time.time(), 'used_estimate_bytes':fields['MemTotal']-fields['MemAvailable'],
            'available_bytes':fields['MemAvailable'],
            'swap_used_bytes':fields['SwapTotal']-fields['SwapFree'],
            'ollama_rss_sum_bytes':sum(int(rss)*1024 for pid,ppid,comm,rss in processes if int(pid) in selected)}


def inventory():
    result = {'timestamp':datetime.now(timezone.utc).isoformat(), 'version':api('version'),
              'models':api('tags')['models'], 'ps':api('ps'), 'memory':memory(),
              'disk_free_bytes':shutil.disk_usage(ROOT).free}
    for model in result['models']:
        details = api('show', {'model':model['name']})
        model['show'] = {k:details.get(k) for k in ['details','model_info','parameters','capabilities']}
        name, tag = model['name'].rsplit(':',1)
        manifest = Path('/usr/share/ollama/.ollama/models/manifests/registry.ollama.ai/library')/name/tag
        if manifest.is_file():
            layers = json.loads(manifest.read_text())['layers']
            model['local_layers'] = []
            for layer in layers:
                blob = Path('/usr/share/ollama/.ollama/models/blobs')/layer['digest'].replace(':','-')
                model['local_layers'].append({'media_type':layer['mediaType'],'digest':layer['digest'],
                    'declared_size_bytes':layer['size'],
                    'actual_size_bytes':blob.stat().st_size if blob.exists() else None})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['inventory','pull','bench'])
    parser.add_argument('--model')
    args = parser.parse_args()
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    DEST.mkdir(parents=True, exist_ok=True)
    if args.action == 'inventory':
        result = inventory()
        path = DEST/('ollama_inventory_'+datetime.now().strftime('%H%M%S')+'.json')
    elif args.action == 'pull':
        if not args.model:
            parser.error('--model required')
        before = {'memory':memory(),'disk_free_bytes':shutil.disk_usage(ROOT).free}
        if before['disk_free_bytes'] < 15*1024**3 or before['memory']['available_bytes'] < 7*1024**3:
            raise SystemExit('Resource guard: requires 15 GiB disk and 7 GiB MemAvailable')
        request = urllib.request.Request(BASE+'pull', data=json.dumps({'model':args.model,'stream':True}).encode(),
                                         headers={'Content-Type':'application/json'})
        statuses = []
        with urllib.request.urlopen(request, timeout=600) as response:
            for line in response:
                event = json.loads(line)
                status = event.get('status')
                if event.get('error'):
                    raise RuntimeError(event['error'])
                if status and status not in statuses:
                    statuses.append(status)
                    print(status, flush=True)
        result = {'model':args.model, 'before':before, 'statuses':statuses, 'after':inventory()}
        path = DEST/('pull_'+args.model.replace(':','_')+'.json')
    else:
        if not args.model:
            parser.error('--model required')
        # Do not evict a user's existing loaded models.
        if api('ps')['models']:
            raise SystemExit('A model is already loaded. Stop here to preserve the existing session.')
        if memory()['available_bytes'] < 7*1024**3:
            raise SystemExit('Resource guard: requires 7 GiB MemAvailable')
        stop_path = DEST/('ros_stop_'+str(time.time_ns()))
        ros_path = DEST/('ros_during_'+args.model.replace(':','_')+'_'+stamp+'.json')
        ros_log = tempfile.TemporaryFile(mode='w+t')
        ros_proc = subprocess.Popen(['bash','-c',
            'source tools/physical_ai/ros_env.sh; source physical_ai/ros_ws/install/setup.bash; '
            'exec ros2 run environment_smoke check --seconds 300 --stop-file "$1" --output "$2"',
            'ros-audit',str(stop_path),str(ros_path)], cwd=ROOT, stdout=ros_log,stderr=ros_log)
        time.sleep(3)
        if ros_proc.poll() is not None:
            ros_log.seek(0)
            raise RuntimeError('ROS smoke exited early: '+ros_log.read())
        before = memory()
        samples = []
        stop = threading.Event()
        def sample():
            while not stop.is_set():
                samples.append(memory())
                stop.wait(0.25)
        worker = threading.Thread(target=sample, daemon=True)
        worker.start()
        settings = {'num_ctx':2048,'num_predict':128,'num_thread':6,'num_gpu':0,'temperature':0,'seed':42}
        result = {'model':args.model,'before':before,'options':settings,
                  'execution':'CPU explicitly requested (num_gpu=0); GPU/NPU not benchmarked',
                  'started_at':datetime.now(timezone.utc).isoformat()}
        try:
            start = time.monotonic()
            result['load'] = api('generate', {'model':args.model,'stream':False,'keep_alive':'5m',
                                             'think':False,'options':settings})
            result['load_wall_s'] = time.monotonic()-start
            start = time.monotonic()
            result['inference_started_at_unix'] = time.time()
            result['inference'] = api('generate', {'model':args.model,'stream':False,'keep_alive':'5m',
                'think':False,'options':settings,
                'prompt':'日本語で、ロボットのIMUとLiDARが測る情報の違いを、初学者向けに150字程度で説明してください。'})
            result['inference_finished_at_unix'] = time.time()
            result['inference_wall_s'] = time.monotonic()-start
            response = result['inference']
            result['tokens_per_second'] = response.get('eval_count',0)/(response.get('eval_duration',1)/1e9)
            result['loaded_ps'] = api('ps')
            result['status'] = 'PASS' if response.get('done') and response.get('eval_count',0)>0 else 'FAIL'
        except Exception as exc:
            result['status'] = 'FAIL'
            result['exception'] = repr(exc)
        finally:
            stop.set()
            worker.join(timeout=5)
            result['samples'] = samples
            result['peak_used_estimate_bytes'] = max((s['used_estimate_bytes'] for s in samples),default=0)
            result['peak_swap_used_bytes'] = max((s['swap_used_bytes'] for s in samples),default=0)
            result['peak_ollama_rss_sum_bytes'] = max((s['ollama_rss_sum_bytes'] for s in samples),default=0)
            inference_samples = [s for s in samples
                if result.get('inference_started_at_unix',float('inf')) <= s['time'] <= result.get('inference_finished_at_unix',0)]
            result['inference_peaks'] = {key:max((s[key] for s in inference_samples),default=None)
                for key in ['used_estimate_bytes','swap_used_bytes','ollama_rss_sum_bytes']}
            # Only this benchmark's model was loaded; release its RAM, retaining files.
            result['unload'] = api('generate', {'model':args.model,'keep_alive':0,'stream':False})
            result['after'] = memory()
            stop_path.touch()
            try:
                result['ros_returncode'] = ros_proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                ros_proc.terminate()
                result['ros_returncode'] = ros_proc.wait(timeout=10)
            ros_log.seek(0)
            result['ros_log'] = ros_log.read()
            ros_log.close()
            stop_path.unlink(missing_ok=True)
            result['ros_concurrency'] = json.loads(ros_path.read_text()) if ros_path.exists() else {'status':'FAIL'}
            result['finished_at'] = datetime.now(timezone.utc).isoformat()
        path = DEST/('bench_'+args.model.replace(':','_')+'_'+stamp+'.json')
    path.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'saved':str(path),'status':result.get('status'),'tokens_per_second':result.get('tokens_per_second')}))
    return 1 if result.get('status') == 'FAIL' else 0


if __name__ == '__main__':
    raise SystemExit(main())
