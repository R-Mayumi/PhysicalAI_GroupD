#!/usr/bin/env python3
"""PCのキー入力でGo2を操作し、LiDARを使って指令を通すか判定する。"""

import argparse
import os
from pathlib import Path
import signal
import sys
import threading
import time

from go2_lidar import (
    ANGULAR_SPEED, LINEAR_SPEED, POSE_MAX_AGE, SCAN_MAX_AGE, ZERO,
    Calibration, LocalMap, safety_monitor, state_monitor,
)
from go2_voice import Robot, SEND_INTERVAL, SafetyError, configured_network_interface, network_interface


LIDAR_TOPIC = "rt/utlidar/cloud"
KEY_MAX_AGE = 0.10
# Linux input-event-codes.h の物理キーコード。
ESC, SPACE = 1, 57
COMMANDS = {
    17: ("前進", (LINEAR_SPEED, 0, 0)),       # W
    31: ("後退", (-LINEAR_SPEED, 0, 0)),      # S
    30: ("左並進", (0, LINEAR_SPEED, 0)),     # A
    32: ("右並進", (0, -LINEAR_SPEED, 0)),    # D
    16: ("左旋回", (0, 0, ANGULAR_SPEED)),   # Q
    18: ("右旋回", (0, 0, -ANGULAR_SPEED)),  # E
}


class Keyboard:
    def __init__(self, path):
        # EVIOCGKEYで現在の物理キー状態を取得する。端末のキーリピートには依存しない。
        import fcntl
        self.ioctl = fcntl.ioctl
        self.fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)

    def keys(self):
        size = 8  # このプログラムで使用する最大キーコードは57。
        request = (2 << 30) | (size << 16) | (ord("E") << 8) | 0x18  # EVIOCGKEY(size)
        bits = self.ioctl(self.fd, request, bytes(size))
        return {key for key in (ESC, SPACE, *COMMANDS) if bits[key // 8] & (1 << (key % 8))}

    def close(self):
        os.close(self.fd)


class LidarInput:
    def __init__(self, robot):
        from unitree_sdk2py.core.channel import ChannelSubscriber
        from unitree_sdk2py.idl.sensor_msgs.msg.dds_ import PointCloud2_

        self.robot = robot
        self.lock = threading.Lock()
        self.pending = None
        self.sub = ChannelSubscriber(LIDAR_TOPIC, PointCloud2_)
        self.sub.Init(self.receive)

    def receive(self, message):
        # SDKのコールバックでは最新サンプルの受け渡しだけを行う。
        state = self.robot.state()
        received = time.monotonic()
        with self.lock:
            self.pending = message, received, state

    def take(self):
        with self.lock:
            packet, self.pending = self.pending, None
            return packet

    def close(self):
        self.sub.Close()


def world_model(robot, packet, local_map, calibration):
    if packet is not None:
        message, received, scan_state = packet
        if time.monotonic() - received < SCAN_MAX_AGE:
            scan_pose = state_monitor(scan_state, robot.guard, received)
            local_map.update(message, received, scan_pose, calibration)
    now = time.monotonic()
    pose = state_monitor(robot.state(), robot.guard, now)
    return local_map.snapshot(pose, now)


class ManualControl:
    def __init__(self, robot, keyboard):
        self.robot = robot
        self.keyboard = keyboard
        self.armed = False
        self.active = ZERO
        self.last_status = None
        self.may_be_moving = False
        self.last_sent = None

    def stop(self):
        if self.may_be_moving:
            for attempt in range(3):
                try:
                    self.robot.stop(zero_velocity=True)
                    break
                except Exception as exc:
                    if attempt == 2:
                        raise SafetyError("停止要求を確認できません。リモコンで停止してください。") from exc
            self.may_be_moving = False
        self.active = ZERO
        self.last_sent = None

    def status(self, text):
        if text != self.last_status:
            print(text, flush=True)
            self.last_status = text

    def tick(self, keys, key_time, model, reason):
        directions = keys & COMMANDS.keys()
        if not keys:
            self.stop()
            self.armed = model is not None
            self.status(reason or "待機。Spaceと方向キーを押すと移動します。")
            return
        if model is None or not self.armed or SPACE not in keys or len(directions) != 1:
            waiting_for_direction = model is not None and self.armed and keys == {SPACE} and self.active == ZERO
            self.stop()
            self.armed = waiting_for_direction
            self.status(reason or ("方向キーを待機中。" if self.armed else
                                   "停止。Spaceと方向キーをすべて離すと再入力できます。"))
            return

        label, velocity = COMMANDS[next(iter(directions))]
        now = time.monotonic()
        distance = LINEAR_SPEED * SEND_INTERVAL if velocity[0] or velocity[1] else 0.0
        reason = safety_monitor(model, velocity, self.robot.guard.origin, now)
        if self.last_sent is not None and time.monotonic() - self.last_sent >= KEY_MAX_AGE:
            reason = "制御周期が遅れました。"
        if self.active not in (ZERO, velocity):
            reason = "方向変更の前にキーを離してください。"
        if self.active == ZERO and (model.pose.speed > 0.025 or model.pose.yaw_speed > 0.05):
            reason = "機体の速度が下がるまで待ってください。"
        if distance > self.robot.guard.remaining:
            reason = "この起動中の並進指令が合計25cmに達しました。"
        expires_at = min(key_time + KEY_MAX_AGE, model.fusion.received + SCAN_MAX_AGE,
                         model.pose.received + POSE_MAX_AGE)
        # 判定中のキー解放または入力変更も送信直前に確認する。
        if reason or self.keyboard.keys() != keys or time.monotonic() >= expires_at:
            self.armed = False
            self.stop()
            self.status(reason or "停止。キー入力が変化したか、指令の期限が切れました。")
            return
        self.robot.guard.remaining -= distance
        self.may_be_moving = True
        self.robot.final_gate(self.robot.client.Move, *velocity, permitted=True, expires_at=expires_at)
        self.last_sent = now
        self.active = velocity
        self.status(f"移動中 ({label})")


def check(robot, lidar, local_map, calibration):
    state = robot.state()
    print(f"運動状態コード {state['error_code']}、モード {state['mode']}")
    deadline = time.monotonic() + 2.0
    model = None
    seen_cloud = False
    reason = local_map.error
    while time.monotonic() < deadline:
        packet = lidar.take()
        if packet is not None and not seen_cloud:
            message = packet[0]
            print(f"点群受信 {message.header.frame_id!r}、{message.width * message.height} 点")
            seen_cloud = True
        try:
            if not robot.guard.tripped:
                model = world_model(robot, packet, local_map, calibration)
        except SafetyError as exc:
            model, reason = None, str(exc)
        if robot.guard.tripped and seen_cloud:
            break
        time.sleep(SEND_INTERVAL)
    if model is None:
        print(reason, file=sys.stderr)
        return 1
    now = time.monotonic()
    print(f"点群更新成功。自機点 {model.fusion.self_points}、床面格子 {len(model.floor)}、"
          f"占有格子 {len(model.occupied)}")
    for label, velocity in COMMANDS.values():
        reason = safety_monitor(model, velocity, robot.guard.origin, now)
        print(f"  {label}  {reason or '点群による判定は許可'}")
    return 0


def drive(robot, lidar, local_map, calibration, keyboard):
    control = ManualControl(robot, keyboard)
    print("Space + W/S 前後、A/D 左右並進、Q/E 左右旋回。Escで終了。", flush=True)
    try:
        while True:
            start = time.monotonic()
            keys = keyboard.keys()
            if ESC in keys:
                return 0
            try:
                model, reason = world_model(robot, lidar.take(), local_map, calibration), None
            except SafetyError as exc:
                model, reason = None, str(exc)
            control.tick(keys, start, model, reason)
            if robot.guard.tripped:
                return 1
            time.sleep(max(0, start + SEND_INTERVAL - time.monotonic()))
    finally:
        control.stop()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="状態と点群を受信して各方向を診断する")
    mode.add_argument("--drive", action="store_true", help="実機のキー操作を有効にする")
    parser.add_argument("--calibration", type=Path, required=True, help="実機で確認した取付設定のJSON")
    parser.add_argument("--net-if", help="Go2に接続するネットワークインターフェース")
    parser.add_argument("--keyboard", type=Path, help="Linuxのキーボードの /dev/input/event デバイス")
    args = parser.parse_args(argv)
    if args.drive and (not sys.platform.startswith("linux") or args.keyboard is None):
        parser.error("--drive には Linux と --keyboard が必要です。")

    calibration = Calibration.load(args.calibration)
    local_map = LocalMap()
    keyboard = robot = lidar = None
    try:
        if args.drive:
            keyboard = Keyboard(args.keyboard)
            keyboard.keys()
        net_if = network_interface(args.net_if or configured_network_interface())
        robot = Robot(net_if, monitor_enabled=False, read_only=args.check, require_ready=args.drive)
        lidar = LidarInput(robot)
        if args.check:
            return check(robot, lidar, local_map, calibration)
        return drive(robot, lidar, local_map, calibration, keyboard)
    finally:
        try:
            if lidar is not None:
                lidar.close()
        finally:
            try:
                if robot is not None:
                    robot.close()
            finally:
                if keyboard is not None:
                    keyboard.close()


def terminate(*_):
    raise KeyboardInterrupt


if __name__ == "__main__":
    for name in ("SIGTERM", "SIGHUP", "SIGTSTP"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), terminate)
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("終了しました。")
    except (ImportError, OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
        print(f"処理を中断しました。{exc}", file=sys.stderr)
        raise SystemExit(1)
