#!/usr/bin/env bash
# このPCの端末から実行してください。指定先にアプリと仮想環境を作成します。
# 既存のros_ai環境を共有し、ネット接続や既存ライブラリの変更を避けます。
set -euo pipefail

source_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
target_dir="${PYHAI_GO2_TARGET_DIR:-$HOME/pyhai_go2_workspace}"
base_python="$HOME/miniforge3/envs/ros_ai/bin/python"
sdk_source="$HOME/ros_ai_workspace/unitree_sdk2_python"

if [[ ! -x "$base_python" ]]; then
    echo "動作確認済みのros_ai環境がありません：$base_python" >&2
    exit 1
fi
if [[ ! -d "$source_dir/vendor/unitree_sdk2_python/unitree_sdk2py" && ! -d "$sdk_source/unitree_sdk2py" ]]; then
    echo "公式SDKのコピー元が見つかりません。" >&2
    exit 1
fi

mkdir -p "$target_dir"
if [[ "$source_dir" != "$target_dir" ]]; then
    # 既存のアプリを上書きせずに保存します。
    if [[ -e "$target_dir/go2_voice.py" ]]; then
        backup_dir="$target_dir/backups/$(date +%Y%m%d-%H%M%S)"
        mkdir -p "$backup_dir"
        cp -p "$target_dir/go2_voice.py" "$backup_dir/"
    fi
    for filename in go2_voice.py requirements.txt dependencies.lock.json README.md setup.sh; do
        cp "$source_dir/$filename" "$target_dir/$filename"
    done
fi

# 接続先の個別設定は既存の.envを上書きしません。
if [[ -f "$source_dir/.env.example" && "$source_dir" != "$target_dir" ]]; then
    cp "$source_dir/.env.example" "$target_dir/.env.example"
fi
if [[ ! -e "$target_dir/.env" && -f "$target_dir/.env.example" ]]; then
    cp "$target_dir/.env.example" "$target_dir/.env"
fi

# SDKをこのディレクトリに同梱し、以前のデモのファイル参照をなくします。
mkdir -p "$target_dir/vendor"
if [[ ! -d "$target_dir/vendor/unitree_sdk2_python/unitree_sdk2py" ]]; then
    if [[ -d "$source_dir/vendor/unitree_sdk2_python/unitree_sdk2py" ]]; then
        sdk_source="$source_dir/vendor/unitree_sdk2_python"
    fi
    mkdir -p "$target_dir/vendor/unitree_sdk2_python"
    cp -a "$sdk_source/unitree_sdk2py" "$target_dir/vendor/unitree_sdk2_python/"
    for filename in LICENSE README.md; do
        if [[ -f "$sdk_source/$filename" ]]; then
            cp "$sdk_source/$filename" "$target_dir/vendor/unitree_sdk2_python/"
        fi
    done
fi

# 仮想環境内のPythonから、確認済みのros_aiのライブラリを利用します。
# 完全に独立した環境ではありません。ros_ai環境は削除しないでください。
if [[ ! -x "$target_dir/.venv/bin/python" ]]; then
    "$base_python" -m venv --without-pip --system-site-packages "$target_dir/.venv"
fi

"$target_dir/.venv/bin/python" - "$target_dir" <<'PY'
import importlib
import importlib.metadata
import json
from pathlib import Path
import sys

root = Path(sys.argv[1])
sys.path.insert(0, str(root / "vendor" / "unitree_sdk2_python"))
for name in ("numpy", "scipy", "sounddevice", "faster_whisper", "ollama", "cyclonedds", "unitree_sdk2py.go2.sport.sport_client"):
    importlib.import_module(name)
expected = json.loads((root / "dependencies.lock.json").read_text())
different = []
for name, required in expected.items():
    try:
        current = importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        current = "未インストール"
    if current != required:
        different.append(f"{name}：期待値 {required}、現在 {current}")
if different:
    print("作成時の依存関係と一致しません。既存環境は変更していません。", file=sys.stderr)
    print("\n".join(different), file=sys.stderr)
    raise SystemExit(1)
print("ライブラリの読み込みと依存関係のバージョン確認に成功しました。")
PY

echo "作成完了：$target_dir"
echo "接続確認："
echo "  $target_dir/.venv/bin/python $target_dir/go2_voice.py --check --net-if enx9c69d351a4cf"
echo "音声操作："
echo "  $target_dir/.venv/bin/python $target_dir/go2_voice.py --net-if enx9c69d351a4cf"
