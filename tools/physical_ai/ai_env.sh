#!/usr/bin/env bash
# Source from a fresh terminal, independently of ROS/SDK shells.
task_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
source "$task_root/.venv-ai/bin/activate"
unset PYTHONPATH PYTHONHOME
export JUPYTER_CONFIG_DIR="$task_root/physical_ai/runtime/jupyter_config"
export JUPYTER_DATA_DIR="$task_root/physical_ai/runtime/jupyter_data"
export JUPYTER_RUNTIME_DIR="$task_root/physical_ai/runtime/jupyter_runtime"
export IPYTHONDIR="$task_root/physical_ai/runtime/ipython"
mkdir -p "$JUPYTER_CONFIG_DIR" "$JUPYTER_DATA_DIR" "$JUPYTER_RUNTIME_DIR" "$IPYTHONDIR"
