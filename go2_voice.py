#!/usr/bin/env python3
"""日本語の音声 → Whisper → Ollama → Go2。実行に必要な自作コードはこの1ファイルです。"""

# このプログラムは、次の順番で動きます。
# ① マイクで録音  ② Whisperで文字起こし  ③ Ollamaで指示を分類
# ④ Python側で速度と時間を決定  ⑤ 公式SDKでGo2に指令を送信
# AIには速度・距離・任意のプログラムを決めさせません。

import argparse
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import threading
import time
import unicodedata

# すでに保存されているWhisperモデルだけを利用します。
os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
ROOT = Path(__file__).resolve().parent
# セットアップで同梱した公式SDKを優先します。以前のデモには依存しません。
SDK = ROOT / "vendor" / "unitree_sdk2_python"
if SDK.is_dir():
    sys.path.insert(0, str(SDK))

# ── 安全設定：変更する前に意味を理解してください ──────────────────
MAX_RANGE_M = 0.30                # 開始位置からの目標範囲：30cm
STOP_MARGIN_M = 0.05              # 制動のために5cmを残す
SAFE_RANGE_M = MAX_RANGE_M - STOP_MARGIN_M
LINEAR_SPEED = 0.15               # 前進・後退の速度：秒速15cm
LINEAR_SECONDS = 1.0              # 通常の1回の直進：1秒、指令上15cm
ANGULAR_SPEED = 0.20              # 左右の旋回速度：毎秒0.2ラジアン
TURN_SECONDS = 0.5               # 旋回は0.5秒
STATE_MAX_AGE = 0.5              # 0.5秒間状態を受信しなければ停止
SEND_INTERVAL = 0.05             # 速度指令を約20回/秒送る
INTENTS = ("forward", "backward", "left", "right", "stop", "stand", "sit")
LABELS = dict(zip(INTENTS, ("前進", "後退", "左旋回", "右旋回", "停止", "立ち上がる", "伏せる")))


class SafetyError(RuntimeError):
    """安全条件を満たせない場合に処理を中断するための例外です。"""


class SafetyGuard:
    """位置と移動指令の両方を確認します。開始位置は自動リセットしません。"""

    def __init__(self, position, expected_error_code=0):
        self.origin = tuple(position[:2])
        self.expected_error_code = expected_error_code
        self.remaining = SAFE_RANGE_M
        self.tripped = False

    def check(self, state, now):
        # 一度安全停止した場合、その起動中は再び動かしません。
        if self.tripped:
            raise SafetyError("安全停止済みです。原因を確認してからプログラムを再起動してください。")
        try:
            if now - state["received"] > STATE_MAX_AGE:
                raise SafetyError("Go2の状態受信が途切れたため停止します。")
            values = (*state["position"], *state["velocity"], state["yaw_speed"])
            if not all(math.isfinite(v) for v in values):
                raise SafetyError("Go2の状態に無効な数値があるため停止します。")
            if state["error_code"] != self.expected_error_code:
                message = (f"Go2の状態コードが許可した開始時の値と異なります。"
                           f"コード：{state['error_code']}、開始時の許可値：{self.expected_error_code}。")
                if state["error_code"] == 100 and self.expected_error_code == 0:
                    message += "本体の警告を確認し、コード100を許可する場合は --allow-state-code 100 を指定してください。"
                raise SafetyError(message)
            distance = math.hypot(state["position"][0] - self.origin[0],
                                  state["position"][1] - self.origin[1])
            if distance >= SAFE_RANGE_M:
                raise SafetyError("開始位置から25cmに達したため停止します。")
            return distance
        except SafetyError:
            self.tripped = True
            raise

    def reserve_translation(self):
        # 前後どちらに進んでも距離を消費します。
        # 同じ指示を繰り返して30cmを超えないよう、起動中の合計を25cmに制限します。
        if self.remaining < 0.005:
            raise SafetyError("この起動中の移動指令の合計が上限に達しました。追加の直進は実行しません。")
        distance = min(LINEAR_SPEED * LINEAR_SECONDS, self.remaining)
        self.remaining = max(0.0, self.remaining - distance)
        return distance / LINEAR_SPEED


def network_interface(explicit):
    """指定された網カードを使います。省略時はGo2用サブネットから探します。"""
    result = subprocess.run(["ip", "-j", "-4", "addr", "show"],
                            capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError("ネットワーク情報を取得できません。PCの通常の端末から実行してください。")
    interfaces = json.loads(result.stdout)
    if explicit:
        matches = [item for item in interfaces if item["ifname"] == explicit]
    else:
        subnet = ipaddress.ip_network("192.168.123.0/24")
        matches = [item for item in interfaces if any(
            ipaddress.ip_address(address["local"]) in subnet
            for address in item.get("addr_info", []) if address.get("family") == "inet")]
    if len(matches) != 1 or not matches[0].get("addr_info"):
        raise RuntimeError("Go2用のIPv4アドレスを持つ網カードを特定できません。ip -br -4 addrで確認し、--net-ifで指定してください。")
    return matches[0]["ifname"]


def configured_network_interface():
    """CLI > 環境変数 > このファイルと同じ場所の.env > 自動検出。"""
    if "GO2_NET_IF" in os.environ:
        return os.environ["GO2_NET_IF"].strip() or None
    config = ROOT / ".env"
    if not config.is_file():
        return None
    value = None
    for line in config.read_text(encoding="utf-8").splitlines():
        key, separator, raw = line.partition("=")
        if separator and key.strip() == "GO2_NET_IF":
            parts = shlex.split(raw, comments=True)
            if len(parts) > 1:
                raise ValueError(".envのGO2_NET_IFには網カード名を1つ指定してください。")
            value = parts[0] if parts else None
    return value or None


class Robot:
    """SDKの接続・状態受信・停止処理をまとめたクラスです。"""

    def __init__(self, net_if, monitor_enabled=True, allowed_state_code=None):
        if allowed_state_code not in (None, 100):
            raise ValueError("個別に許可できる状態コードは100だけです。")
        self.client = None
        self.subscribers = []
        self.lock = threading.Lock()
        self.states = {}
        self.topic = None
        self.guard = None
        self.watchdog_end = threading.Event()
        self.watchdog = None
        self.stop_failed = False
        try:
            from unitree_sdk2py.core.channel import ChannelFactoryInitialize, ChannelSubscriber
            from unitree_sdk2py.idl.unitree_go.msg.dds_ import SportModeState_
            from unitree_sdk2py.go2.sport.sport_client import SportClient
            ChannelFactoryInitialize(0, net_if)
            self.client = SportClient()
            self.client.SetTimeout(1.0)
            self.client.Init()
            for topic in ("rt/sportmodestate", "rt/lf/sportmodestate"):
                sub = ChannelSubscriber(topic, SportModeState_)
                self.subscribers.append(sub)
                sub.Init(lambda message, topic=topic: self.receive(message, topic), 1)
            time.sleep(1.0)
            code, version = self.client.GetServerApiVersion()
            if code != 0:
                raise RuntimeError(f"Go2の運動APIに接続できません。応答コード：{code}")
            # 計測座標系の混在を避けるため、接続後は同じトピックだけを使用します。
            with self.lock:
                fresh = [topic for topic, state in self.states.items()
                         if time.monotonic() - state["received"] <= STATE_MAX_AGE]
            if not fresh:
                raise SafetyError("Go2の新しい運動状態を受信できません。")
            self.topic = "rt/sportmodestate" if "rt/sportmodestate" in fresh else fresh[0]
            state = self.state()
            # 非ゼロ値を正常とは解釈しません。明示指定された開始時の値だけを固定します。
            expected_code = 100 if allowed_state_code == 100 and state["error_code"] == 100 else 0
            self.guard = SafetyGuard(state["position"], expected_error_code=expected_code)
            self.guard.check(state, time.monotonic())
            if expected_code:
                print("注意：意味が未確認の状態コード100を明示指定により許可しています。"
                      "正常動作を保証するものではありません。コードが変わった場合は停止します。")
            print(f"Go2に接続しました。網カード：{net_if}、運動API：{version}")
            print("現在位置を基準にします。移動指令は起動中の合計25cmまでです。")
            # 音声認識・LLMの処理中にも状態を監視します。
            # 停止後の自動再開はせず、安全停止を記憶します。
            if monitor_enabled:
                self.watchdog = threading.Thread(target=self.monitor, daemon=True)
                self.watchdog.start()
        except BaseException:
            self.close()
            raise

    def receive(self, message, topic):
        state = {"received": time.monotonic(), "position": list(message.position),
                 "velocity": list(message.velocity), "mode": message.mode,
                 "error_code": message.error_code, "yaw_speed": message.yaw_speed}
        with self.lock:
            self.states[topic] = state

    def state(self):
        with self.lock:
            state = self.states.get(self.topic)
        if state is None:
            raise SafetyError("Go2の状態がありません。")
        return state

    def request(self, method, *values):
        code = method(*values)
        if code != 0:
            raise RuntimeError(f"Go2の指令が失敗しました。API：{method.__name__}、応答コード：{code}")

    def stop(self):
        if self.client is not None:
            self.request(self.client.StopMove)

    def monitor(self):
        while not self.watchdog_end.wait(0.05):
            try:
                self.guard.check(self.state(), time.monotonic())
            except Exception as exc:
                self.guard.tripped = True
                print(f"\n安全監視：{exc}", flush=True)
                # 通信異常の場合、停止要求自体も届かない可能性があります。
                # 数回試行した後は自動で動作を再開しません。
                for _ in range(3):
                    try:
                        self.stop()
                        return
                    except Exception:
                        self.stop_failed = True
                print("停止要求の成功を確認できません。リモコンで停止してください。", flush=True)
                return

    def execute(self, intent):
        if intent == "stop":
            self.stop()
            print("停止要求が成功しました。")
            return
        self.guard.check(self.state(), time.monotonic())
        if intent in ("stand", "sit"):
            try:
                self.request(self.client.StandUp if intent == "stand" else self.client.StandDown)
                if intent == "stand":
                    time.sleep(1.0)
                    self.guard.check(self.state(), time.monotonic())
                    self.request(self.client.BalanceStand)
            finally:
                self.stop()
            print("姿勢の変更要求が成功しました。実際の姿勢を確認してください。")
            return
        if intent not in ("forward", "backward", "left", "right"):
            raise ValueError("対応していない指示です。")
        duration = self.guard.reserve_translation() if intent in ("forward", "backward") else TURN_SECONDS
        x = LINEAR_SPEED * ({"forward": 1, "backward": -1}.get(intent, 0))
        yaw = ANGULAR_SPEED * ({"left": 1, "right": -1}.get(intent, 0))
        before = self.state()
        print(f"実行：{LABELS[intent]}、直進速度 {x:g} m/s、旋回速度 {yaw:g} rad/s、{duration:.2f}秒")
        try:
            self.request(self.client.BalanceStand)
            deadline = time.monotonic() + duration
            while time.monotonic() < deadline:
                self.guard.check(self.state(), time.monotonic())
                # Moveの成功は「送信成功」であり、実際の移動成功ではありません。
                self.request(self.client.Move, x, 0.0, yaw)
                time.sleep(min(SEND_INTERVAL, max(0.0, deadline - time.monotonic())))
        finally:
            # 正常終了・エラー・Ctrl+Cのいずれの場合も停止を要求します。
            self.stop()
        time.sleep(0.3)
        after = self.state()
        self.guard.check(after, time.monotonic())
        displacement = math.hypot(after["position"][0] - before["position"][0],
                                  after["position"][1] - before["position"][1])
        print(f"指令送信と停止要求が完了しました。状態値による前後の位置差：約{displacement * 100:.1f}cm")
        print(f"残りの直進指令距離：{self.guard.remaining * 100:.1f}cm。実際の動きも目視で確認してください。")

    def close(self):
        self.watchdog_end.set()
        if self.watchdog is not None:
            self.watchdog.join(timeout=4.0)
        for sub in self.subscribers:
            sub.Close()
        self.subscribers.clear()


def classify(text, model):
    """Ollamaの出力を候補の中から選ばせ、Pythonでも検証します。"""
    text = unicodedata.normalize("NFKC", text).strip()
    # 停止を求める言葉はLLMを待たずに停止へ変換します。
    if not text or re.search(r"停止|止ま|ストップ|やめ|ない|ません|禁止|動くな|ジャンプ|宙返り|バク転", text):
        return "stop"
    import ollama
    schema = {"type": "object", "properties": {
        "intent": {"type": "string", "enum": list(INTENTS)}},
        "required": ["intent"], "additionalProperties": False}
    prompt = (
        "あなたはGo2への日本語指示を分類します。出力はintentだけのJSONです。"
        "前進=forward、後退=backward、左に曲がる=left、右に曲がる=right、"
        "停止=stop、立ち上がる=stand、伏せる・座る=sit。"
        "後ろに進むはbackwardです。否定、曖昧な指示、複数の動作、質問、"
        "ジャンプなど未対応の動作は必ずstopにしてください。距離や速度は出力しません。"
    )
    client = ollama.Client(host="http://127.0.0.1:11434", timeout=20.0)
    response = client.chat(model=model, messages=[{"role": "system", "content": prompt},
                           {"role": "user", "content": text}], format=schema,
                           options={"temperature": 0, "num_predict": 32}, keep_alive="2h")
    result = json.loads(response["message"]["content"])
    if set(result) != {"intent"} or result["intent"] not in INTENTS:
        raise ValueError("Ollamaが許可されていない指示を返しました。動作しません。")
    return result["intent"]


def microphone(explicit):
    import sounddevice as sd
    if explicit is not None:
        return int(explicit) if explicit.isdigit() else explicit
    # このPCの内蔵マイクはACP PDMです。ヘッドセット端子との取り違えを避けます。
    devices = sd.query_devices()
    for index, device in enumerate(devices):
        if "acp-pdm" in device["name"].lower() and device["max_input_channels"] > 0:
            return index
    default = int(sd.default.device[0])
    if default < 0:
        raise RuntimeError("入力マイクがありません。--list-devicesで一覧を確認してください。")
    return default


def record(seconds, device):
    import numpy as np
    import sounddevice as sd
    from scipy.signal import resample_poly
    info = sd.query_devices(device, "input")
    rate = int(info["default_samplerate"])
    channels = 1
    try:
        sd.check_input_settings(device=device, samplerate=rate, channels=channels, dtype="float32")
    except sd.PortAudioError:
        channels = min(2, int(info["max_input_channels"]))
        sd.check_input_settings(device=device, samplerate=rate, channels=channels, dtype="float32")
    print(f"録音開始：{seconds:g}秒、マイク：{info['name']}、{rate} Hz", flush=True)
    try:
        audio = sd.rec(int(seconds * rate), samplerate=rate, channels=channels,
                       dtype="float32", device=device)
        sd.wait()
    finally:
        sd.stop()
    # Whisperは16kHzの音声を使うので、マイクの実際の録音周波数から変換します。
    audio = audio.mean(axis=1)
    if rate != 16000:
        common = math.gcd(rate, 16000)
        audio = resample_poly(audio, 16000 // common, rate // common)
    return np.asarray(audio, dtype=np.float32)


class JapaneseParser(argparse.ArgumentParser):
    def format_usage(self):
        return super().format_usage().replace("usage: ", "使い方：", 1)

    def format_help(self):
        return super().format_help().replace("usage: ", "使い方：", 1)

    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(2, "引数の指定に誤りがあります。--helpで使用できるオプションと値を確認してください。\n")


def main(argv=None):
    parser = JapaneseParser(description=__doc__, add_help=False, usage="%(prog)s [オプション]")
    parser._positionals.title = "位置引数"
    parser._optionals.title = "オプション"
    parser.add_argument("-h", "--help", action="help", help="使い方を表示して終了")
    parser.add_argument("--net-if", metavar="網カード",
                        help="Go2接続用。省略時はGO2_NET_IF環境変数、.env、自動検出の順")
    parser.add_argument("--allow-state-code", type=int, choices=(100,), metavar="100",
                        help="本体の警告確認後、開始時の状態コード100を個別に許可。コード変化時は停止")
    parser.add_argument("--device", metavar="マイク", help="入力マイクの番号または名前")
    parser.add_argument("--list-devices", action="store_true", help="入力マイクの一覧を表示して終了")
    parser.add_argument("--seconds", type=float, default=5.0, metavar="秒", help="録音時間。既定は5秒")
    parser.add_argument("--countdown", type=int, default=3, metavar="秒", help="録音前の待機。既定は3秒")
    parser.add_argument("--model", default="qwen2.5:3b", metavar="モデル", help="Ollamaモデル。既定はqwen2.5:3b")
    parser.add_argument("--text", metavar="指示", help="音声の代わりに日本語の指示を1回処理して終了")
    parser.add_argument("--once", action="store_true", help="録音・実行を1回だけ行って終了")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--check", action="store_true", help="APIと状態受信のみ確認。動作指令を送らない")
    modes.add_argument("--dry-run", action="store_true", help="音声認識と指示分類のみ確認。Go2へ接続しない")
    args = parser.parse_args(argv)
    if not 0.5 <= args.seconds <= 10 or not 0 <= args.countdown <= 10:
        parser.error("録音時間は0.5〜10秒、待機は0〜10秒にしてください。")
    if args.list_devices:
        import sounddevice as sd
        for index, device in enumerate(sd.query_devices()):
            if device["max_input_channels"] > 0:
                print(f"番号 {index}：{device['name']}")
        return 0
    robot = None
    whisper = None
    try:
        if not args.dry_run:
            robot = Robot(network_interface(args.net_if if args.net_if is not None
                                            else configured_network_interface()),
                          monitor_enabled=not args.check,
                          allowed_state_code=args.allow_state_code)
        if args.check:
            print("接続確認に成功しました。動作指令は送っていません。")
            return 0
        print("日本語音声 → Whisper → Ollama → Go2")
        print("Enterで録音、または日本語で入力してください。qで終了します。")
        if args.dry_run:
            print("解析確認モードです。Go2への指令は送信しません。")
        while True:
            text = args.text if args.text is not None else (
                "" if args.once else input("\n指示 / Enterで録音 / qで終了：").strip())
            if text.lower() == "q":
                break
            if not text:
                if whisper is None:
                    from faster_whisper import WhisperModel
                    print("保存済みのWhisperモデルを読み込んでいます。", flush=True)
                    whisper = WhisperModel("base", device="cpu", compute_type="int8", local_files_only=True)
                device = microphone(args.device)
                for remaining in range(args.countdown, 0, -1):
                    print(f"録音まで{remaining}秒", flush=True)
                    time.sleep(1)
                audio = record(args.seconds, device)
                segments, _ = whisper.transcribe(audio, language="ja", beam_size=1, temperature=0.0,
                                                vad_filter=True, condition_on_previous_text=False)
                text = "".join(segment.text for segment in segments).strip()
                print("文字起こし：" + (text or "音声を認識できませんでした。"))
            if text:
                intent = classify(text, args.model)
                print("解釈した指示：" + LABELS[intent])
                if robot is not None:
                    robot.execute(intent)
            if args.text is not None or args.once:
                break
        return 0
    finally:
        if robot is not None:
            try:
                # 接続確認モードでは停止を含めて動作指令を送りません。
                if not args.check:
                    robot.stop()
            finally:
                robot.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (KeyboardInterrupt, EOFError):
        print("\n終了しました。")
    except ModuleNotFoundError as exc:
        print(f"必要なライブラリが見つかりません：{exc.name}。このディレクトリの.venv/bin/pythonで実行してください。", file=sys.stderr)
        raise SystemExit(1)
    except Exception as exc:
        print(f"処理を中断しました：{exc}", file=sys.stderr)
        raise SystemExit(1)
