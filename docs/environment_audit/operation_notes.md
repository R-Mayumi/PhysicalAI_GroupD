# 起動・停止・再確認手順

2026-11-11 17:17 JST、利用者指示により作業中断。Qwen3 4Bは導入・評価済み、8Bは約50.1%で取得停止し未導入・未推論。以下は作成済みの操作手順であり、中断後に実行する指示ではない。

作業ディレクトリは `~/pyhai_go2_workspace`。SDK、ROS、AIはそれぞれ新しい端末で実行する。今回作成したスクリプトは実機の運動指令を送らない。既存 `go2_voice.py` の通常起動は運動を伴うため、この環境確認手順には含めない。

## 1. 起動時のリソース確認

```bash
cd ~/pyhai_go2_workspace
date -Is
uptime
free -h
swapon --show
df -h .
ip -br -4 address
ollama list
ollama ps
```

起動直後の値を調べる場合は、利用者が通常起動した直後、LLM・Jupyter・ROSを起動する前に実行して時刻とともに保存する。今回の作業では再起動していない。8Bの追加取得・評価スクリプトは空きSSD 15 GiB、MemAvailable 7 GiBを開始条件とする。これは今回の検証用の余裕を含む条件で、全条件に通用する最低仕様ではない。

## 2. Go2の読み取り専用再確認

```bash
cd ~/pyhai_go2_workspace
ip -br -4 address
# 現在の接続名を上の出力と照合する。監査時の値をそのまま他のPCへ流用しない。
GO2_AUDIT_IF=enx9c69d351a4cf
ip -4 address show dev "$GO2_AUDIT_IF"
python3 tools/physical_ai/run_logged.py --timeout 30 \
  .venv/bin/python -B tools/physical_ai/check_go2_sensors.py \
  --net-if "$GO2_AUDIT_IF" \
  --output "docs/environment_audit/go2_recheck_$(date +%Y%m%d_%H%M%S).json"
```

購読8秒とカメラ取得後に自動終了する。既存の有線IPv4設定・DDS Domain 0を使用し、ネットワーク設定を書き換えない。`PASS` は受信・復号の判定であって、ロボット全体の正常状態ではない。`error_code=1001` が解決するまで運動試験に進まない。未接続時のデータ経路は `NOT_TESTED` とし、非ゼロ終了もログで確認する。

## 3. ROS 2開発環境

新しい端末で既存Humbleを有効化し、テスト用overlayを読み込む。

```bash
cd ~/pyhai_go2_workspace
source tools/physical_ai/ros_env.sh
source physical_ai/ros_ws/install/setup.bash
ros2 launch environment_smoke check.launch.py
```

約5秒でプロセス間の合成String通信を確認し自動終了する。実機とは別のDomain 42、localhost限定、既存Fast DDSを使用する。`ros2 topic list` と `ros2 node list` は確認プログラム内部で `--no-daemon` を付けて実行する。

再ビルドが必要な場合：

```bash
cd ~/pyhai_go2_workspace
source tools/physical_ai/ros_env.sh
cd physical_ai/ros_ws
colcon build --base-paths src/environment_smoke \
  --packages-select environment_smoke --executor sequential \
  --event-handlers console_direct+ desktop_notification- status- terminal_title-
```

初回sandbox内ビルドはタイムアウトしたが、通常ホスト環境で上記のビルド・launch・通信を確認した。AI用 `.venv-ai` の中でROS依存をpip導入しない。実機DDSにROSを直接接続する設定は、この手順には含まない。

## 4. MuJoCoのGo2モデルと状態

```bash
cd ~/pyhai_go2_workspace
source tools/physical_ai/ai_env.sh
MUJOCO_GL=egl python -B tools/physical_ai/check_mujoco.py --render \
  --output docs/environment_audit/mujoco_recheck.json
```

640×480のモデル画像と、関節・質量・慣性・摩擦・接触・qpos/qvel・センサー値のJSONを生成する。関節への制御入力はゼロにし、2秒間の重力・接触計算を行う。自立歩行・姿勢維持制御の確認ではない。

デスクトップで初期姿勢を表示する場合：

```bash
MUJOCO_GL=glfw python -B tools/physical_ai/check_mujoco.py --viewer-seconds 30 \
  --output docs/environment_audit/mujoco_gui_recheck.json
```

30秒で表示を閉じて状態計算を保存する。元の `unitree_mujoco.py` や `test_unitree_sdk2.py` は起動しない。今回のスクリプトはDDSを初期化しない。将来SDK連携シミュレータを使う場合は、公式推奨の実機と異なるDomain 1・`lo` を別設定として検討する。

## 5. JupyterLab

```bash
cd ~/pyhai_go2_workspace
source tools/physical_ai/ai_env.sh
python -m jupyterlab --no-browser --ip=127.0.0.1 --port=8888
```

端末に表示される認証URLをこのPCのブラウザで開く。設定・runtimeは `physical_ai/runtime/` に保存する。停止は端末でCtrl+Cを2回。動作を短時間で再検証して自動停止する場合：

```bash
python -B tools/physical_ai/check_jupyter.py
```

この確認はNumPy・OpenCV・MuJoCoをカーネルでimportし、認証付きAPIのHTTP 200を確認する。ROSとSDKはこのカーネルに追加していない。

## 6. ローカルLLM

既存Ollamaサービス（127.0.0.1:11434）を利用する。サービスの再起動・設定変更は不要。

```bash
ollama list
ollama ps
curl -sS http://127.0.0.1:11434/api/generate -H 'Content-Type: application/json' -d '{
  "model":"qwen3:4b-q4_K_M", "prompt":"IMUとLiDARの違いを日本語で説明してください。",
  "stream":false, "think":false, "keep_alive":"2m",
  "options":{"num_ctx":2048,"num_predict":128,"num_thread":6,"num_gpu":0,"temperature":0}
}'
```

モデル取得が完了したものだけを指定する。比較評価時はモデル名を `qwen3:8b-q4_K_M` に変える。既存3Bを使う場合は `qwen2.5:3b` とする。contextを大きくするとメモリ条件も変わるので、今回の測定結果を無条件に一般化しない。

明示的なモデル停止（モデルファイルは削除しない）：

```bash
ollama stop qwen3:4b-q4_K_M
ollama stop qwen3:8b-q4_K_M
```

ROSと同時に測り直す場合は他のモデルがロードされていないことを `ollama ps` で確認する。

```bash
cd ~/pyhai_go2_workspace
python3 tools/physical_ai/run_logged.py --timeout 240 \
  python3 tools/physical_ai/ollama_check.py bench --model qwen3:8b-q4_K_M
```

確認用ROSプロセスを起動し、ロード・推論・リソース測定後、自分がロードしたモデルとROSプロセスを終了する。別のモデルがロード済みなら既存利用を維持するため測定を中止する。

## 7. 保全と依存関係の再確認

```bash
cd ~/pyhai_go2_workspace
python3 tools/physical_ai/preserve.py --verify
.venv-ai/bin/python -m pip check
```

監査開始後、`make_code/README.md` に変更が検出されている。この作業では編集しておらず、既存変更を巻き戻していないため、保全スクリプトの全体判定はこの差分がある限り `FAIL` となる。JSONに差分ファイルの一覧を記録する。元の基準ハッシュを更新してPASSに見せかけない。
