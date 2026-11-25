#!/usr/bin/env python3
"""立位静止したGo2のLiDAR幾何診断。DDS購読のみで、運動許可は出さない。

手順02〜05の補助。状態コードが非ゼロでも観測データを解析するが、
走行用state_monitorの代わりには使わない。取付・床面の校正値も変更しない。
"""

import argparse
from collections import Counter
from datetime import datetime
import json
import math
from pathlib import Path
import sys
import threading
import time
from zoneinfo import ZoneInfo

from go2_lidar import (
    Calibration, FLOOR_TOLERANCE, LocalMap, MAX_TILT, POSE_MAX_AGE,
    Pose, SCAN_MAX_AGE, cloud_xyz, rotation, safety_monitor,
)
from go2_teleop import COMMANDS, LIDAR_TOPIC
from go2_voice import SafetyError, configured_network_interface, network_interface


def diagnostic_pose(state, now):
    """数値・鮮度・静止を検査。状態コードは結果に記録し、運動許可には使わない。"""
    if state is None:
        raise SafetyError("点群受信時の機体状態がありません。")
    position, velocity, rpy = state["position"], state["velocity"], state["rpy"]
    if (len(position) != 3 or len(velocity) != 3 or len(rpy) != 3 or
            not all(math.isfinite(value) for value in
                    (*position, *velocity, *rpy, state["yaw_speed"], state["received"], now))):
        raise SafetyError("診断用の機体状態に無効な数値があります。")
    if not 0 <= now - state["received"] <= POSE_MAX_AGE:
        raise SafetyError("診断用の機体状態の受信が途切れました。")
    speed, yaw_speed = math.hypot(*velocity[:2]), abs(state["yaw_speed"])
    if max(abs(rpy[0]), abs(rpy[1])) > MAX_TILT:
        raise SafetyError("傾きが静止診断の範囲を超えています。")
    # ManualControlの移動開始時と同じ、水平速度・旋回速度の静止条件。
    if speed > 0.025 or yaw_speed > 0.05:
        raise SafetyError("機体が動いているため静止診断を中断します。")
    return Pose(state["received"], tuple(position[:2]), tuple(rpy), speed, yaw_speed)


def analyze_packet(packet, local_map, calibration, now):
    message, received, state = packet
    if not 0 <= now - received < SCAN_MAX_AGE:
        raise SafetyError("診断用のLiDAR点群が古くなっています。")
    pose = diagnostic_pose(state, received)
    diagnostic_pose(state, now)
    local_map.update(message, received, pose, calibration)
    return local_map.snapshot(pose, now)


def height_profile(message, pose, calibration):
    """既存処理と同じ変換後のZ分布。床面推定や設定への自動反映は行わない。"""
    axis = rotation(pose.rpy)[2]
    row = tuple(sum(axis[i] * calibration.matrix[i][j] for i in range(3)) for j in range(3))
    offset = sum(a * b for a, b in zip(axis, calibration.xyz))
    histogram = Counter()
    floor_band_points = 0
    for point in cloud_xyz(message):
        if not all(math.isfinite(value) for value in point):
            continue
        if not 1e-6 < sum(value * value for value in point) <= 9:
            continue
        z = sum(a * b for a, b in zip(row, point)) + offset
        if abs(z - calibration.ground_z) <= FLOOR_TOLERANCE:
            floor_band_points += 1
        if -1 <= z <= 0.5:
            histogram[math.floor(z / 0.02)] += 1
    return {
        "configured_ground_z_m": calibration.ground_z,
        "floor_band_half_width_m": FLOOR_TOLERANCE,
        "floor_band_points": floor_band_points,
        "z_histogram_bin_width_m": 0.02,
        "most_populated_z_bins": [
            {"lower_m": round(index * 0.02, 3), "upper_m": round((index + 1) * 0.02, 3), "count": count}
            for index, count in histogram.most_common(8)
        ],
        "note": "Z分布は参考値。床や障害物の実測との照合が必要。",
    }


def run_check(net_if, seconds, calibration):
    # 通信は購読だけ。SportClient、Robot、Publisherは生成しない。
    from unitree_sdk2py.core.channel import ChannelFactoryInitialize, ChannelSubscriber
    from unitree_sdk2py.idl.sensor_msgs.msg.dds_ import PointCloud2_
    from unitree_sdk2py.idl.unitree_go.msg.dds_ import SportModeState_

    lock = threading.Lock()
    states, state_counts, error_codes = {}, Counter(), set()
    pending = None
    cloud_count = 0
    chosen_topic = None
    subscribers = []
    local_map = LocalMap()
    last_packet = last_model = last_seen_packet = None
    errors = Counter()
    report = {
        "started_at": datetime.now(ZoneInfo("Asia/Tokyo")).isoformat(),
        "diagnostic_only": True,
        "motion_authorized": False,
        "scope": "DDS subscriptions only; no RPC, motion, stop, or calibration changes",
        "interface": net_if,
        "geometry_status": "not_computed",
        "calibration_independently_verified": False,
    }

    def receive_state(message, topic):
        state = {
            "received": time.monotonic(), "position": tuple(message.position),
            "velocity": tuple(message.velocity), "rpy": tuple(message.imu_state.rpy),
            "yaw_speed": message.yaw_speed, "error_code": message.error_code,
            "body_height_m": message.body_height, "mode": message.mode,
        }
        with lock:
            states[topic] = state
            state_counts[topic] += 1
            error_codes.add(message.error_code)

    def receive_cloud(message):
        nonlocal pending, cloud_count, chosen_topic
        received = time.monotonic()
        with lock:
            cloud_count += 1
            if chosen_topic is None:
                for topic in ("rt/sportmodestate", "rt/lf/sportmodestate"):
                    if topic in states and 0 <= received - states[topic]["received"] <= POSE_MAX_AGE:
                        chosen_topic = topic
                        break
            pending = message, received, states.get(chosen_topic)

    try:
        ChannelFactoryInitialize(0, net_if)
        for topic in ("rt/sportmodestate", "rt/lf/sportmodestate"):
            subscriber = ChannelSubscriber(topic, SportModeState_)
            subscribers.append(subscriber)
            subscriber.Init(lambda message, name=topic: receive_state(message, name), 1)
        subscriber = ChannelSubscriber(LIDAR_TOPIC, PointCloud2_)
        subscribers.append(subscriber)
        subscriber.Init(receive_cloud)
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            with lock:
                packet, pending = pending, None
            if packet is not None:
                last_seen_packet = packet
                try:
                    model = analyze_packet(packet, local_map, calibration, time.monotonic())
                    last_packet, last_model = packet, model
                except (SafetyError, ValueError, OverflowError) as exc:
                    errors[str(exc)] += 1
                    last_packet = last_model = None
            time.sleep(0.01)
        now = time.monotonic()
        with lock:
            current_state = states.get(chosen_topic)
        if current_state is not None:
            report.update({
                "pose_topic": chosen_topic,
                "last_state_code": current_state["error_code"],
                "body_height_m": current_state["body_height_m"],
                "reported_velocity_m_s": current_state["velocity"],
                "reported_yaw_speed_rad_s": current_state["yaw_speed"],
                "reported_rpy_rad": current_state["rpy"],
                "static_check_scope": "planar velocity and yaw speed; vertical velocity recorded separately",
                "static_planar_velocity_limit_m_s": 0.025,
                "static_yaw_speed_limit_rad_s": 0.05,
            })
        pose = diagnostic_pose(current_state, now)
        if last_seen_packet is not None:
            message, received, scan_state = last_seen_packet
            report.update({
                "frame_id": message.header.frame_id,
                "points_in_last_cloud": message.width * message.height,
                "height_profile": height_profile(message, diagnostic_pose(scan_state, received), calibration),
            })
        model = local_map.snapshot(pose, now)
        if last_model is None or last_packet is None:
            raise SafetyError(local_map.error or "最新の点群を解析できません。")
        report.update({
            "geometry_status": "computed",
            "self_points": model.fusion.self_points,
            "floor_cells": len(model.floor), "occupied_cells": len(model.occupied),
            "directions": {
                label: {"geometry_clear": reason is None,
                        "reason": reason or "幾何上の障害物・未観測は検出なし。運動許可ではありません。"}
                for label, velocity in COMMANDS.values()
                for reason in [safety_monitor(model, velocity, pose.xy, now)]
            },
        })
    except (SafetyError, ValueError, RuntimeError, OverflowError) as exc:
        report["error"] = str(exc)
    finally:
        for subscriber in subscribers:
            subscriber.Close()
        with lock:
            report.update({"clouds_received": cloud_count,
                           "state_samples": dict(state_counts), "observed_state_codes": sorted(error_codes)})
        report["processing_errors"] = dict(errors)
        report["finished_at"] = datetime.now(ZoneInfo("Asia/Tokyo")).isoformat()
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--net-if")
    parser.add_argument("--calibration", type=Path, required=True)
    parser.add_argument("--seconds", type=float, default=3.0)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if not math.isfinite(args.seconds) or not 1 <= args.seconds <= 30:
        parser.error("--secondsは1〜30で指定してください。")
    try:
        calibration = Calibration.load(args.calibration)
        net_if = network_interface(args.net_if if args.net_if is not None else configured_network_interface())
        report = run_check(net_if, args.seconds, calibration)
        output = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)
        if args.output:
            args.output.write_text(output + "\n", encoding="utf-8")
        print(output)
        return 0 if report["geometry_status"] == "computed" else 1
    except (ValueError, OSError, RuntimeError) as exc:
        print(f"診断を中断しました: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("診断を終了しました。動作指令は送っていません。")
        sys.exit(130)
