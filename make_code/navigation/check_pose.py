#!/usr/bin/env python3
"""外部アプリで設定した地図上の自己位置を1回表示する（動作指令なし）。"""

import argparse
import math
import sys
import time

if __package__:
    from .ros_connection import add_connection_arguments, init_ros, positive_seconds
else:
    from ros_connection import add_connection_arguments, init_ros, positive_seconds

import rclpy
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from nav_msgs.msg import Odometry

POSE_TOPIC = "/uslam/localization/odom"


def planar_pose(msg):
    """無効な位置・四元数は受理せず、正規化してyawを求める。"""
    p = msg.pose.pose.position
    q = msg.pose.pose.orientation
    values = (p.x, p.y, p.z, q.x, q.y, q.z, q.w)
    if not all(math.isfinite(value) for value in values):
        raise ValueError("自己位置にNaNまたは無限大が含まれています。")
    norm = math.hypot(q.x, q.y, q.z, q.w)
    if not math.isfinite(norm) or norm < 1e-12:
        raise ValueError("自己位置の姿勢四元数が無効です。")
    x, y, z, w = (value / norm for value in (q.x, q.y, q.z, q.w))
    yaw = math.atan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z))
    return p.x, p.y, yaw


class PoseChecker(Node):
    def __init__(self):
        super().__init__("pose_checker")
        self.received = False
        # BEST_EFFORTで、RELIABLE/BEST_EFFORTどちらの配信元にも接続する。
        self.sub = self.create_subscription(
            Odometry, POSE_TOPIC, self.callback, qos_profile_sensor_data
        )

    def callback(self, msg):
        if self.received:
            return
        try:
            x, y, yaw = planar_pose(msg)
        except ValueError as exc:
            self.get_logger().warning(str(exc))
            return
        print(f"frame_id : {msg.header.frame_id}")
        print(f"x        : {x:.4f} m")
        print(f"y        : {y:.4f} m")
        print(f"yaw      : {yaw:.4f} rad")
        print(f"yaw      : {math.degrees(yaw):.1f} deg")
        self.received = True


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    add_connection_arguments(parser)
    parser.add_argument("--timeout", type=positive_seconds, default=10.0)
    args = parser.parse_args(argv)
    node = None
    try:
        init_ros(args.net_if, args.domain_id)
        node = PoseChecker()
        deadline = time.monotonic() + args.timeout
        while rclpy.ok() and not node.received and time.monotonic() < deadline:
            rclpy.spin_once(node, timeout_sec=min(0.2, max(0.0, deadline - time.monotonic())))
        if not node.received:
            print(
                f"自己位置を受信できません: {POSE_TOPIC}。"
                "アプリ側の自己位置推定、NIC、Domainを確認してください。",
                file=sys.stderr,
            )
            return 1
        return 0
    except KeyboardInterrupt:
        return 130
    except (RuntimeError, ValueError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    finally:
        if node is not None:
            node.destroy_node()
        rclpy.try_shutdown()


if __name__ == "__main__":
    sys.exit(main())
