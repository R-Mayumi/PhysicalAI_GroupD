#!/usr/bin/env python3
"""手順03の補助診断。DDS購読だけで胴体高さを記録し、校正値は変更しない。"""

import argparse
from collections import deque
from datetime import datetime
import json
import math
from pathlib import Path
import statistics
import sys
import threading
import time
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "vendor/unitree_sdk2_python"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--net-if", required=True)
    parser.add_argument("--seconds", type=float, default=3.0)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not math.isfinite(args.seconds) or not 1 <= args.seconds <= 30:
        parser.error("--secondsは1〜30で指定してください。")

    from unitree_sdk2py.core.channel import ChannelFactoryInitialize, ChannelSubscriber
    from unitree_sdk2py.idl.unitree_go.msg.dds_ import SportModeState_

    lock = threading.Lock()
    observations = {}
    subscribers = []
    report = {
        "started_at": datetime.now(ZoneInfo("Asia/Tokyo")).isoformat(),
        "interface": args.net_if,
        "scope": "DDS subscriptions only; no RPC, setters, motion, or stop commands",
        "calibration_changed": False,
        "height_is_robot_reported_not_independently_measured": True,
        "topics": {},
    }

    def receive(message, topic):
        sample = {
            "body_height_m": float(message.body_height),
            "position_m": [float(value) for value in message.position],
            "rpy_rad": [float(value) for value in message.imu_state.rpy],
            "mode": int(message.mode),
            "error_code": int(message.error_code),
        }
        with lock:
            entry = observations[topic]
            entry["count"] += 1
            entry["last"] = sample
            entry["error_codes"].add(sample["error_code"])
            if math.isfinite(sample["body_height_m"]):
                entry["heights"].append(sample["body_height_m"])

    try:
        ChannelFactoryInitialize(0, args.net_if)
        for topic in ("rt/sportmodestate", "rt/lf/sportmodestate"):
            observations[topic] = {"count": 0, "heights": deque(maxlen=2000), "error_codes": set()}
            subscriber = ChannelSubscriber(topic, SportModeState_)
            subscribers.append(subscriber)
            subscriber.Init(lambda message, name=topic: receive(message, name), 1)
        time.sleep(args.seconds)
    except Exception as exc:
        report["exception"] = repr(exc)
    finally:
        for subscriber in subscribers:
            subscriber.Close()
        with lock:
            for topic, entry in observations.items():
                heights = list(entry["heights"])
                report["topics"][topic] = {
                    "count": entry["count"],
                    "error_codes": sorted(entry["error_codes"]),
                    "body_height_m": {
                        "min": min(heights), "median": statistics.median(heights), "max": max(heights)
                    } if heights else None,
                    "last": entry.get("last"),
                }
        report["finished_at"] = datetime.now(ZoneInfo("Asia/Tokyo")).isoformat()
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if any(entry["count"] for entry in report["topics"].values()) else 1


if __name__ == "__main__":
    sys.exit(main())
