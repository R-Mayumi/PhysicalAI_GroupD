#!/usr/bin/env bash
# Source in a subshell. No persistent ROS/network changes; no robot domain traffic.
task_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
source /home/scg-hp2/miniforge3/etc/profile.d/conda.sh
conda activate ros_ai
export ROS_DOMAIN_ID=42
export ROS_LOCALHOST_ONLY=1
export ROS_LOG_DIR="$task_root/docs/environment_audit/ros_logs"
export RMW_IMPLEMENTATION=rmw_fastrtps_cpp
unset CYCLONEDDS_URI
