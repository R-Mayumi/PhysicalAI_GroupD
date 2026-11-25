#!/usr/bin/env python3
"""map.jsonに保存した名前付きの目標へ移動する。

例: .venv/bin/python make_code/navigation/goto.py desk
同じディレクトリのmap.jsonを読み込む。別のファイルは--mapで指定できる。
形式: {"desk": {"x": 1.0, "y": 2.0, "yaw": 0.0}}
数値は例。外部アプリで使用中の地図に対応する実際の目標を登録すること。
"""

import argparse
import json
from pathlib import Path
import sys

if __package__:
    from .go2_goto import Go2Navigator, add_navigation_arguments, target_pose
    from .ros_connection import init_ros
else:
    from go2_goto import Go2Navigator, add_navigation_arguments, target_pose
    from ros_connection import init_ros

import rclpy

LOCATIONS_FILE = Path(__file__).with_name("map.json")


def load_target(region, locations_file=LOCATIONS_FILE):
    try:
        with open(locations_file, encoding="utf-8") as stream:
            locations = json.load(stream)
    except FileNotFoundError as exc:
        raise ValueError(
            f"目標一覧がありません: {locations_file}。"
            ' {"目的地名": {"x": 0.0, "y": 0.0, "yaw": 0.0}} の形式で作成し、'
            "実際の地図座標を設定してください。--map でも指定できます。"
        ) from exc
    if not isinstance(locations, dict):
        raise ValueError("目標一覧は目的地名をキーにしたJSONオブジェクトにしてください。")
    if region not in locations:
        raise ValueError(f"Unknown region: {region}. Available: {', '.join(locations)}")
    pose = locations[region]
    if not isinstance(pose, dict) or not all(key in pose for key in ("x", "y", "yaw")):
        raise ValueError(f"{region}: x、y、yawを持つオブジェクトが必要です。")
    return target_pose(pose["x"], pose["y"], pose["yaw"])


def goto(region, robot, *, locations_file=LOCATIONS_FILE, timeout=120, start_navigation=True):
    """SPINE互換: 名前で指定した領域へ移動する。"""
    try:
        x, y, yaw = load_target(region, locations_file)
    except (ValueError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return False
    print(f"Target: {region}, x={x}, y={y}, yaw={yaw}")
    return robot.goto(x, y, yaw, timeout=timeout, start_navigation=start_navigation)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("region", help="目標一覧に登録した目的地名")
    parser.add_argument("--map", type=Path, default=LOCATIONS_FILE, dest="locations_file")
    add_navigation_arguments(parser)
    args = parser.parse_args(argv)
    robot = None
    try:
        # ファイル・目的地の不備はROS接続や動作指令の前に検出する。
        x, y, yaw = load_target(args.region, args.locations_file)
        init_ros(args.net_if, args.domain_id)
        robot = Go2Navigator()
        print(f"Target: {args.region}, x={x}, y={y}, yaw={yaw}")
        success = robot.goto(
            x, y, yaw, timeout=args.timeout, start_navigation=not args.navigation_running
        )
        print(f"Navigation result: {success}")
        return 0 if success else 1
    except KeyboardInterrupt:
        print("中断しました。", file=sys.stderr)
        return 130
    except (ValueError, RuntimeError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    finally:
        if robot is not None:
            robot.destroy_node()
        rclpy.try_shutdown()


if __name__ == "__main__":
    sys.exit(main())
