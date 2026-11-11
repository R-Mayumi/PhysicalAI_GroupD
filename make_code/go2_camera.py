#!/usr/bin/env python3
"""Go2 EDU front camera -> LAN browser MJPEG viewer (read-only)."""
import argparse
import ipaddress
import json
import os
from pathlib import Path
import secrets
import shlex
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
SDK = ROOT / 'vendor' / 'unitree_sdk2_python'
if SDK.is_dir():
    sys.path.insert(0, str(SDK))


def network_interface(explicit):
    result = subprocess.run(['ip', '-j', '-4', 'addr', 'show'], capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError('ネットワーク情報を取得できません。PCの通常の端末から実行してください。')
    interfaces = json.loads(result.stdout)
    if explicit:
        matches = [x for x in interfaces if x['ifname'] == explicit]
    else:
        subnet = ipaddress.ip_network('192.168.123.0/24')
        matches = [x for x in interfaces if any(
            ipaddress.ip_address(a['local']) in subnet
            for a in x.get('addr_info', []) if a.get('family') == 'inet')]
    if len(matches) != 1 or not any(
            a.get('family') == 'inet' for a in matches[0].get('addr_info', [])):
        raise RuntimeError('Go2用のIPv4アドレスを持つNICを特定できません。'
                           'Go2への接続と `ip -br -4 addr` を確認し、--net-if を指定してください。')
    return matches[0]['ifname']


def configured_interface():
    if 'GO2_NET_IF' in os.environ:
        return os.environ['GO2_NET_IF'].strip() or None
    env_file = ROOT / '.env'
    if env_file.exists():
        value = None
        for line in env_file.read_text(encoding='utf-8').splitlines():
            key, sep, raw = line.partition('=')
            if sep and key.strip() == 'GO2_NET_IF':
                parts = shlex.split(raw, comments=True)
                if len(parts) > 1:
                    raise ValueError('.envのGO2_NET_IFにはNIC名を1つ指定してください。')
                value = parts[0] if parts else None
        return value or None
    return None


class Frames:
    def __init__(self):
        self.condition = threading.Condition()
        self.jpeg = None
        self.number = 0
        self.last_received = 0.0
        self.error = None

    def publish(self, jpeg):
        with self.condition:
            self.jpeg = jpeg
            self.number += 1
            self.last_received = time.monotonic()
            self.error = None
            self.condition.notify_all()

    def set_error(self, error):
        with self.condition:
            self.error = str(error)


def create_client(net_if):
    try:
        from unitree_sdk2py.core.channel import ChannelFactoryInitialize
        from unitree_sdk2py.go2.video.video_client import VideoClient
    except ImportError as exc:
        raise RuntimeError('公式SDKまたは依存ライブラリを読み込めません。'
                           'プロジェクト直下の .venv/bin/python で実行してください。') from exc
    ChannelFactoryInitialize(0, net_if)
    client = VideoClient()
    client.SetTimeout(3.0)
    client.Init()
    return client


def get_jpeg(client):
    code, data = client.GetImageSample()
    if code != 0:
        raise RuntimeError(f'GetImageSample 応答コード: {code}。Go2の電源・接続・NICを確認してください。')
    if data is None or len(data) == 0:
        raise RuntimeError('カメラから空の画像が返されました。')
    jpeg = bytes(data)
    # JPEGはそのまま配信し、別形式のときだけOpenCVで変換します。
    if not (jpeg.startswith(b'\xff\xd8') and jpeg.endswith(b'\xff\xd9')):
        import cv2
        import numpy as np
        img = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            raise RuntimeError('画像デコード失敗')
        ok, encoded = cv2.imencode('.jpg', img)
        if not ok:
            raise RuntimeError('JPEG変換失敗')
        jpeg = encoded.tobytes()
    return jpeg


def capture(frames, client, stop):
    while not stop.is_set():
        try:
            frames.publish(get_jpeg(client))
        except Exception as exc:
            frames.set_error(exc)
            print(f'カメラ受信エラー: {exc}', flush=True)
            stop.wait(0.5)


def make_handler(frames, token, stop):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            url = urlsplit(self.path)
            if parse_qs(url.query).get('token', [''])[0] != token:
                self.send_error(403, 'Invalid viewing token')
                return
            if url.path == '/':
                html = ("<!doctype html><html lang='ja'><meta charset='utf-8'>"
                        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
                        "<title>Go2 Camera</title><style>body{margin:0;background:#111;color:#eee;"
                        "font-family:sans-serif;text-align:center}img{width:100%;max-width:1100px;"
                        "height:auto}p{font-size:14px}</style><h2>Go2 前方カメラ</h2>"
                        f"<img src='/stream?token={token}' alt='Go2 camera'>"
                        "<p id='status'>カメラ接続待ち…</p>"
                        "<p>映像のみ（ロボット操作機能なし）</p><script>"
                        "async function updateStatus(){try{"
                        f"const r=await fetch('/status?token={token}',{{cache:'no-store'}});"
                        "const s=await r.json();document.getElementById('status').textContent="
                        "s.receiving?'映像を受信中':(s.error||'カメラ接続待ち…');"
                        "}catch(e){document.getElementById('status').textContent='サーバーとの接続が切れました';}}"
                        "updateStatus();setInterval(updateStatus,1000);</script></html>")
                body = html.encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif url.path == '/stream':
                self.connection.settimeout(5)
                self.send_response(200)
                self.send_header('Content-Type', 'multipart/x-mixed-replace; boundary=frame')
                self.send_header('Cache-Control', 'no-store, no-cache, must-revalidate')
                self.end_headers()
                last = 0
                try:
                    while not stop.is_set():
                        with frames.condition:
                            frames.condition.wait_for(
                                lambda: frames.number != last or stop.is_set(), timeout=2)
                            if stop.is_set():
                                break
                            if frames.number == last:
                                continue
                            last, jpeg = frames.number, frames.jpeg
                        self.wfile.write(b'--frame\r\nContent-Type: image/jpeg\r\n')
                        self.wfile.write(f'Content-Length: {len(jpeg)}\r\n\r\n'.encode())
                        self.wfile.write(jpeg + b'\r\n')
                        self.wfile.flush()
                except OSError:
                    pass
            elif url.path == '/status':
                with frames.condition:
                    fresh = (frames.jpeg is not None and frames.error is None
                             and time.monotonic() - frames.last_received < 3)
                    body = json.dumps({'receiving': fresh, 'error': frames.error}, ensure_ascii=False).encode()
                self.send_response(200 if fresh else 503)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_error(404)

        def log_message(self, fmt, *args):
            # Avoid leaking viewing token via access logs.
            pass

    return Handler


def main():
    parser = argparse.ArgumentParser(description='Go2カメラをLAN内ブラウザへ配信')
    parser.add_argument('--net-if', help='Go2側のネットワークインターフェース')
    parser.add_argument('--host', default='0.0.0.0', help='Webサーバー待受IP (default: 0.0.0.0)')
    parser.add_argument('--port', type=int, default=8080)
    parser.add_argument('--check', action='store_true', help='画像を1枚受信・復号して接続確認後に終了')
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error('--port は1〜65535で指定してください。')
    try:
        net_if = network_interface(args.net_if if args.net_if is not None else configured_interface())
        client = create_client(net_if)
        if args.check:
            import cv2
            import numpy as np
            jpeg = get_jpeg(client)
            image = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
            if image is None:
                raise RuntimeError('画像デコード失敗')
            height, width = image.shape[:2]
            print(f'カメラ受信成功: {width}×{height}, {len(jpeg)} bytes, NIC={net_if}')
            return 0
        frames = Frames()
        stop = threading.Event()
        token = secrets.token_urlsafe(18)
        server = ThreadingHTTPServer((args.host, args.port), make_handler(frames, token, stop))
    except Exception as exc:
        print(f'カメラ起動エラー: {exc}', file=sys.stderr)
        return 1
    worker = threading.Thread(target=capture, args=(frames, client, stop), daemon=True)
    worker.start()
    print(f'Go2映像取得開始: {net_if}', flush=True)
    local_host = '127.0.0.1' if args.host == '0.0.0.0' else args.host
    print(f'このPCのブラウザURL: http://{local_host}:{args.port}/?token={token}', flush=True)
    if args.host == '0.0.0.0':
        print(f'別PCから: http://<このPCのLAN側IP>:{args.port}/?token={token}', flush=True)
        print('LAN側IPは `hostname -I` で確認してください。', flush=True)
    print('終了: Ctrl+C', flush=True)
    try:
        server.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        print('\n終了します。')
    finally:
        stop.set()
        with frames.condition:
            frames.condition.notify_all()
        server.server_close()
        worker.join(timeout=4)
    return 0


if __name__ == '__main__':
    sys.exit(main())
