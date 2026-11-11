#!/usr/bin/env python3
"""Go2の状態受信と取得APIだけを調査する。動作・設定・音声再生は行わない。"""

import argparse
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "vendor/unitree_sdk2_python"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--net-if", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    from unitree_sdk2py.core.channel import ChannelFactoryInitialize, ChannelSubscriber
    from unitree_sdk2py.idl.unitree_go.msg.dds_ import LowState_, SportModeState_, LidarState_, WirelessController_, UwbState_
    from unitree_sdk2py.idl.sensor_msgs.msg.dds_ import PointCloud2_
    from unitree_sdk2py.go2.sport.sport_client import SportClient
    from unitree_sdk2py.go2.video.video_client import VideoClient
    from unitree_sdk2py.go2.vui.vui_client import VuiClient
    from unitree_sdk2py.go2.robot_state.robot_state_client import RobotStateClient
    from unitree_sdk2py.go2.obstacles_avoid.obstacles_avoid_client import ObstaclesAvoidClient
    from unitree_sdk2py.rpc.client import Client
    from cyclonedds.domain import DomainParticipant
    from cyclonedds.builtin import BuiltinDataReader, BuiltinTopicDcpsPublication, BuiltinTopicDcpsSubscription

    class AudioHubReadClient(Client):
        """コミュニティ実装の既知の取得APIだけをDDS経由で照合する。"""
        def __init__(self):
            super().__init__("audiohub", False)

        def Init(self):
            self._RegistApi(1001, 0)  # GET_AUDIO_LIST
            self._RegistApi(1010, 0)  # GET_PLAY_MODE

        def GetAudioList(self):
            return self._Call(1001, "{}")

        def GetPlayMode(self):
            return self._Call(1010, "{}")

    result = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "net_if": args.net_if,
        "scope": "DDS subscriptions and read-only RPCs; no motion, setters, audio playback, or robot filesystem access",
        "packages": {m: importlib.util.find_spec(m) is not None for m in
                     ("cyclonedds", "cv2", "aiortc", "unitree_webrtc_connect", "go2_webrtc_driver", "rclpy")},
        "topics": {}, "rpc": {},
    }
    lock = threading.Lock()
    subscribers = []
    ChannelFactoryInitialize(0, args.net_if)
    discovery_participant = DomainParticipant(0)
    discovery_readers = {
        "publications": BuiltinDataReader(discovery_participant, BuiltinTopicDcpsPublication),
        "subscriptions": BuiltinDataReader(discovery_participant, BuiltinTopicDcpsSubscription),
    }

    def receive(message, topic):
        now = time.monotonic()
        with lock:
            item = result["topics"].setdefault(topic, {"count": 0, "first": now})
            if item["count"] == 0:
                item["first"] = now
            item["count"] += 1
            item["last"] = now
            if isinstance(message, SportModeState_):
                item["sample"] = {k: getattr(message, k) for k in
                                  ("mode", "error_code", "position", "velocity", "body_height")}
            elif isinstance(message, LowState_):
                item["sample"] = {
                    "version_raw": list(message.version),
                    "battery_soc": message.bms_state.soc,
                    "power_v": message.power_v,
                    "imu_rpy": list(message.imu_state.rpy),
                    "motor_state_slots": len(message.motor_state),
                    "foot_force": list(message.foot_force),
                    "foot_force_est": list(message.foot_force_est),
                    "wireless_remote_bytes": len(message.wireless_remote),
                }
            elif isinstance(message, PointCloud2_):
                item["sample"] = {
                    "width": message.width, "height": message.height,
                    "frame_id": message.header.frame_id,
                    "point_step": message.point_step, "data_bytes": len(message.data),
                    "fields": [f.name for f in message.fields],
                }
            elif isinstance(message, LidarState_):
                item["sample"] = {k: getattr(message, k) for k in
                                  ("firmware_version", "software_version", "sdk_version", "error_state", "cloud_frequency", "cloud_size")}
            elif isinstance(message, WirelessController_):
                item["sample"] = {k: getattr(message, k) for k in ("lx", "ly", "rx", "ry", "keys")}
            elif isinstance(message, UwbState_):
                item["sample"] = {k: getattr(message, k) for k in
                                  ("distance_est", "yaw_est", "error_state", "enabled_from_app")}

    try:
        for topic, cls in (("rt/sportmodestate", SportModeState_),
                           ("rt/lf/sportmodestate", SportModeState_),
                           ("rt/lowstate", LowState_), ("rt/lf/lowstate", LowState_),
                           ("rt/utlidar/cloud", PointCloud2_), ("rt/utlidar/voxel_map", PointCloud2_),
                           ("rt/utlidar/lidar_state", LidarState_),
                           ("rt/wirelesscontroller", WirelessController_), ("rt/uwbstate", UwbState_)):
            result["topics"].setdefault(topic, {"count": 0, "first": None})
            sub = ChannelSubscriber(topic, cls)
            sub.Init(lambda m, topic=topic: receive(m, topic), 10)
            subscribers.append(sub)
        time.sleep(3)

        for name, cls, methods in (
            ("sport", SportClient, ("GetServerApiVersion",)),
            ("vui", VuiClient, ("GetServerApiVersion", "GetSwitch", "GetVolume", "GetBrightness")),
            ("robot_state", RobotStateClient, ("GetServerApiVersion", "ServiceList")),
            ("obstacles_avoid", ObstaclesAvoidClient, ("GetServerApiVersion", "SwitchGet")),
            ("videohub", VideoClient, ("GetServerApiVersion", "GetImageSample")),
            ("audiohub", AudioHubReadClient, ("GetAudioList", "GetPlayMode")),
        ):
            client = cls()
            client.SetTimeout(3.0)
            client.Init()
            for method in methods:
                key = name + "." + method
                start = time.monotonic()
                try:
                    code, data = getattr(client, method)()
                    item = {"code": code}
                    if method == "ServiceList" and code == 0:
                        item["value"] = [vars(s) for s in data]
                    elif method == "GetImageSample":
                        item["bytes"] = len(data) if data is not None else 0
                        if code == 0 and data:
                            import cv2
                            import numpy as np
                            frame = cv2.imdecode(np.frombuffer(bytes(data), dtype=np.uint8), cv2.IMREAD_COLOR)
                            item["decoded_shape"] = list(frame.shape) if frame is not None else None
                            item["image_saved"] = False
                    else:
                        item["value"] = data
                    item["elapsed_s"] = round(time.monotonic() - start, 3)
                    result["rpc"][key] = item
                except Exception as exc:
                    result["rpc"][key] = {"exception": type(exc).__name__, "detail": str(exc)}
                print(key, json.dumps(result["rpc"][key], ensure_ascii=False), flush=True)
    finally:
        result["dds_discovery"] = {}
        for kind, reader in discovery_readers.items():
            endpoints = {}
            for endpoint in reader.take(4096):
                if hasattr(endpoint, "topic_name"):
                    endpoints[endpoint.topic_name] = endpoint.type_name
            result["dds_discovery"][kind] = dict(sorted(endpoints.items()))
        for sub in subscribers:
            sub.Close()
        with lock:
            for item in result["topics"].values():
                first = item.pop("first")
                last = item.pop("last", None)
                span = last - first if first is not None and last is not None else 0
                item["observed_span_s"] = round(span, 3)
                item["approx_hz"] = round((item["count"] - 1) / span, 1) if span else None
        result["finished_at"] = datetime.now(timezone.utc).isoformat()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print("Saved:", args.output, flush=True)


if __name__ == "__main__":
    main()
