#!/usr/bin/env python3
"""Localization済みのGo2でNavigationを開始し、地図座標の目標へ移動する。

例: .venv/bin/python make_code/navigation/go2_goto.py -1.9725 0.2319 -1.3598
x/yはm、yawはrad。初期位置・向きの指定とLocalizationの開始は外部アプリで行う。
実行中は、他のクライアントからNavigationの指令を送らないこと。
uslamのログには要求IDがなく、他のクライアントの応答とは区別できない。
コマンド形式の参考: https://github.com/legion1581/unitree_ui/blob/main/docs/slam.md
"""

import argparse
from collections import deque
import json
import math
import sys
import time

if __package__:
    from .ros_connection import add_connection_arguments, init_ros, positive_seconds
    from .check_pose import POSE_TOPIC, planar_pose
else:
    from ros_connection import add_connection_arguments, init_ros, positive_seconds
    from check_pose import POSE_TOPIC, planar_pose

import rclpy
from rclpy.node import Node
from rclpy.qos import QoSProfile, ReliabilityPolicy, qos_profile_sensor_data
from nav_msgs.msg import Odometry
from std_msgs.msg import String

COMMAND_TOPIC = "/uslam/client_command"
LOG_TOPIC = "/uslam/server_log"
TERMINAL_STATES = {
    "REACHED", "FAILURE", "NO_PATH", "TIMEOUT", "TIMEOUT_POINTCLOUD",
    "TIMEOUT_ODOMETRY", "GOAL_OCCUPIED", "ABNORMAL", "GOAL_CHANGED",
    "GOAL_CANCELLED", "GOAL_POINT_UNREACHABLE",
}


def target_pose(x, y, yaw):
    values = (x, y, yaw)
    if any(isinstance(value, bool) for value in values):
        raise ValueError("x、y、yawに真偽値は指定できません。")
    try:
        x, y, yaw = map(float, values)
    except (TypeError, ValueError) as exc:
        raise ValueError("x、y、yawは数値で指定してください。") from exc
    if not all(math.isfinite(value) for value in (x, y, yaw)):
        raise ValueError("x、y、yawは有限の数値で指定してください。")
    # uslamの目標角度はラジアン、[-pi, pi]。
    return x, y, math.atan2(math.sin(yaw), math.cos(yaw))


def add_navigation_arguments(parser):
    add_connection_arguments(parser)
    parser.add_argument("--timeout", type=positive_seconds, default=120.0, help="到着待ち秒数")
    parser.add_argument(
        "--navigation-running", action="store_true",
        help="外部でNavigationも開始済みの場合にnavigation/startを省略",
    )


class Go2Navigator(Node):
    def __init__(self):
        super().__init__("go2_navigator")
        self.logs = deque(maxlen=512)
        self.log_sequence = 0
        self.last_log_at = 0.0
        self.last_pose_at = 0.0
        self.pose_frame = ""
        self.active = False
        self.navigation_started = False
        self.pub = self.create_publisher(String, COMMAND_TOPIC, 10)
        self.sub = self.create_subscription(
            String, LOG_TOPIC, self.on_log,
            QoSProfile(depth=100, reliability=ReliabilityPolicy.BEST_EFFORT),
        )
        self.pose_sub = self.create_subscription(
            Odometry, POSE_TOPIC, self.on_pose, qos_profile_sensor_data
        )

    def on_pose(self, msg):
        try:
            planar_pose(msg)
        except ValueError:
            return
        self.last_pose_at = time.monotonic()
        self.pose_frame = msg.header.frame_id

    def on_log(self, msg):
        log = msg.data.strip()
        # ファームウェア側がJSON文字列で返す場合も受理する。
        try:
            decoded = json.loads(log)
            if isinstance(decoded, str):
                log = decoded.strip()
        except (ValueError, TypeError):
            pass
        self.log_sequence += 1
        self.last_log_at = time.monotonic()
        self.logs.append((self.log_sequence, log))
        print("LOG:", log, flush=True)

    def wait_for(self, predicate, timeout=10, *, after=0):
        end = time.monotonic() + positive_seconds(timeout)
        while rclpy.ok():
            # ACKより先に到着通知が届いても、同じ送信以降の履歴に残す。
            for sequence, log in self.logs:
                if sequence > after and predicate(log):
                    return log
            remaining = end - time.monotonic()
            if remaining <= 0:
                break
            rclpy.spin_once(self, timeout_sec=min(0.1, remaining))
        return None

    def send(self, command):
        # std_msgs/String.dataにはコマンドそのものを格納する。
        self.pub.publish(String(data=command))
        print("SENT:", command, flush=True)

    def drain_logs(self):
        # spin_onceの戻り値は常にNone。受信時刻を使って短い静止期間を待つ。
        started = time.monotonic()
        deadline = started + 0.5
        while rclpy.ok() and time.monotonic() < deadline:
            rclpy.spin_once(self, timeout_sec=0.01)
            if time.monotonic() - max(started, self.last_log_at) >= 0.05:
                break
        self.logs.clear()

    def wait_ready(self, timeout=10):
        started = time.monotonic()
        deadline = started + positive_seconds(timeout)
        while rclpy.ok() and time.monotonic() < deadline:
            if (self.pub.get_subscription_count() > 0
                    and self.count_publishers(LOG_TOPIC) > 0
                    and self.last_pose_at >= started):
                print(f"Localization受信: frame_id={self.pose_frame}", flush=True)
                return
            rclpy.spin_once(self, timeout_sec=0.1)
        raise RuntimeError(
            "Go2のコマンド受信先・ログ配信元・自己位置を確認できません。"
            "外部アプリでLocalizationを開始し、NICとDomainを確認してください。"
        )

    def command_ack(self, command, timeout=5):
        after = self.log_sequence
        self.send(command)
        action = "/".join(command.split("/")[:2])
        success = {f"{action}/success", f"{command}/success"}
        failure = {f"{action}/failed", f"{command}/failed"}
        response = self.wait_for(lambda log: log in success | failure, timeout, after=after)
        return response in success, response, after

    def stop(self, timeout=2):
        if not self.active:
            return True
        if not rclpy.ok():
            print("ROS 2切断のためnavigation/stopを送信できません。", file=sys.stderr)
            return False
        try:
            accepted, response, _ = self.command_ack("navigation/stop", timeout)
        except RuntimeError as exc:
            accepted, response = False, str(exc)
        finally:
            self.active = False
            self.navigation_started = False
        if not accepted:
            print(f"停止応答を確認できません: {response}", file=sys.stderr)
        return accepted

    def goto(self, x, y, yaw=0.0, timeout=60, *, start_navigation=True):
        """初期自己位置は変更しない。中断・失敗時はNavigationの停止を要求する。"""
        x, y, yaw = target_pose(x, y, yaw)
        timeout = positive_seconds(timeout)
        self.wait_ready()
        self.drain_logs()
        try:
            self.active = True
            if start_navigation and not self.navigation_started:
                accepted, ack, _ = self.command_ack("navigation/start")
                if not accepted:
                    print(f"Navigationを開始できません: {ack}", file=sys.stderr)
                    return False
                self.navigation_started = True
                self.drain_logs()
            command = f"navigation/set_goal_pose/{x}/{y}/{yaw}"
            accepted, ack, after = self.command_ack(command)
            if not accepted:
                print(f"目標受付を確認できません: {ack}", file=sys.stderr)
                return False
            result = self.wait_for(
                lambda log: log.startswith("navigation/state_transition/")
                and log.rsplit("/", 1)[-1] in TERMINAL_STATES,
                timeout=timeout, after=after,
            )
            if result is None:
                print("到着待ちがタイムアウトしました。", file=sys.stderr)
                return False
            state = result.rsplit("/", 1)[-1]
            print("Navigation result:", state)
            if state == "REACHED":
                self.active = False
                return True
            return False
        finally:
            # Ctrl+C、SIGTERM、ACK欠落、例外でも同じ停止処理を通す。
            if self.active:
                self.stop()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("x", type=float, help="地図座標のx [m]")
    parser.add_argument("y", type=float, help="地図座標のy [m]")
    parser.add_argument("yaw", type=float, nargs="?", default=0.0, help="目標向き [rad]")
    add_navigation_arguments(parser)
    args = parser.parse_args(argv)
    robot = None
    try:
        x, y, yaw = target_pose(args.x, args.y, args.yaw)
        init_ros(args.net_if, args.domain_id)
        robot = Go2Navigator()
        success = robot.goto(
            x, y, yaw, timeout=args.timeout, start_navigation=not args.navigation_running
        )
        print("SUCCESS:", success)
        return 0 if success else 1
    except KeyboardInterrupt:
        print("中断しました。", file=sys.stderr)
        return 130
    except (RuntimeError, ValueError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    finally:
        if robot is not None:
            robot.destroy_node()
        rclpy.try_shutdown()


if __name__ == "__main__":
    sys.exit(main())
