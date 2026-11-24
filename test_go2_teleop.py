from dataclasses import replace
import math
import struct
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import go2_lidar as lidar
import go2_teleop as app
from go2_voice import Robot, SafetyError, SafetyGuard


NOW = 10.0
POSE = lidar.Pose(NOW, (0, 0), (0, 0, 0), 0, 0)
CALIBRATION = lidar.Calibration((0, 0, 0), lidar.rotation((0, 0, 0)), -0.32)
FLOOR = [((i + 0.5) * lidar.GRID_M, (j + 0.5) * lidar.GRID_M, -0.32)
         for i in range(-16, 16) for j in range(-16, 16)]


def sample(points, nanos=0, bigendian=False):
    data = b"".join(struct.pack(">4xf4xff12x" if bigendian else "<4xf4xff12x", *p) for p in points)
    fields = [SimpleNamespace(name=name, offset=offset, count=1, datatype=7)
              for name, offset in zip("xyz", (4, 12, 16))]
    header = SimpleNamespace(frame_id=lidar.FRAME_ID, stamp=SimpleNamespace(sec=1, nanosec=nanos))
    return SimpleNamespace(header=header, fields=fields, height=1, width=len(points),
                           point_step=32, row_step=len(data), is_bigendian=bigendian, data=data)


def model_with(*points):
    local_map = lidar.LocalMap()
    local_map.update(sample(FLOOR + list(points)), NOW, POSE, CALIBRATION)
    return local_map.snapshot(POSE, NOW)


class PointCloudTests(unittest.TestCase):
    def test_declared_offsets_endian_and_row_padding(self):
        for bigendian in (False, True):
            message = sample([(1.0, 2.0, 3.0)], bigendian=bigendian)
            message.row_step += 4
            message.height = 2
            message.data = (message.data + b"\0" * 4) * 2
            self.assertEqual(list(lidar.cloud_xyz(message)), [(1, 2, 3)] * 2)
        message.fields[2].offset = message.fields[0].offset
        with self.assertRaises(ValueError):
            list(lidar.cloud_xyz(message))

    def test_preserves_old_obstacles_and_rejects_stale_or_replayed_cloud(self):
        local_map = lidar.LocalMap()
        first = sample(FLOOR + [(0.56, 0.001, -0.1), (0.10, 0.0, 0.0)])
        local_map.update(first, NOW, POSE, CALIBRATION)
        self.assertEqual(local_map.snapshot(POSE, NOW).fusion.self_points, 1)
        second = sample(FLOOR, nanos=100_000_000)
        local_map.update(second, NOW + 0.10, POSE, CALIBRATION)
        world = local_map.snapshot(POSE, NOW + 0.10)
        self.assertEqual(len(world.fusion.occupied), 0)
        self.assertIn(lidar.cell(0.56, 0.001), world.occupied)
        with self.assertRaisesRegex(SafetyError, "更新が途切れ"):
            local_map.snapshot(POSE, NOW + 0.36)
        local_map.update(second, NOW + 0.11, POSE, CALIBRATION)
        with self.assertRaisesRegex(SafetyError, "発行時刻"):
            local_map.snapshot(POSE, NOW + 0.11)

    def test_world_map_uses_transformed_pose(self):
        pose = replace(POSE, xy=(0.12, 0.03), rpy=(0, 0, math.pi / 2))
        calibration = lidar.Calibration((0.05, 0.0, 0.0), CALIBRATION.matrix, -0.32)
        observation = lidar.pointcloud(sample(FLOOR + [(0.5, 0.01, -0.1)]), NOW, pose, calibration)
        self.assertEqual(observation.occupied, {lidar.cell(0.11, 0.58)})

    def test_odometry_jump_invalidates_the_local_map(self):
        local_map = lidar.LocalMap()
        local_map.update(sample(FLOOR), NOW, POSE, CALIBRATION)
        reset_pose = replace(POSE, received=NOW + 0.05, xy=(0.16, 0))
        with self.assertRaisesRegex(SafetyError, "急変"):
            local_map.snapshot(reset_pose, NOW + 0.05)


class SafetyTests(unittest.TestCase):
    def test_all_six_clear_and_forward_obstacle_is_directional(self):
        clear = model_with()
        for label, velocity in app.COMMANDS.values():
            with self.subTest(label=label):
                self.assertIsNone(lidar.safety_monitor(clear, velocity, (0, 0), NOW))
        world = model_with((0.56, 0.001, -0.1))
        self.assertIn("障害物", lidar.safety_monitor(world, app.COMMANDS[17][1], (0, 0), NOW))
        self.assertIsNone(lidar.safety_monitor(world, app.COMMANDS[31][1], (0, 0), NOW))

    def test_lateral_rotation_and_unknown_ground(self):
        side = model_with((0.02, -0.38, -0.1))
        self.assertIn("障害物", lidar.safety_monitor(side, app.COMMANDS[32][1], (0, 0), NOW))
        self.assertIsNone(lidar.safety_monitor(side, app.COMMANDS[30][1], (0, 0), NOW))
        for key in (16, 18):
            self.assertIn("障害物", lidar.safety_monitor(side, app.COMMANDS[key][1], (0, 0), NOW))
        front_left_corner = model_with((0.43, 0.46, -0.1))
        self.assertIn("障害物", lidar.safety_monitor(front_left_corner, app.COMMANDS[16][1], (0, 0), NOW))
        self.assertIsNone(lidar.safety_monitor(front_left_corner, app.COMMANDS[18][1], (0, 0), NOW))
        unknown = replace(model_with(), floor=frozenset())
        self.assertIn("未観測", lidar.safety_monitor(unknown, app.COMMANDS[17][1], (0, 0), NOW))

    def test_state_is_checked_before_use(self):
        state = dict(position=(0, 0, 0), velocity=(0, 0, 0), rpy=(0, 0, 0),
                     error_code=1001, received=NOW, yaw_speed=0)
        with self.assertRaisesRegex(SafetyError, "1001"):
            lidar.state_monitor(state, SafetyGuard((0, 0)), NOW)
        state.update(error_code=0, received=NOW - 0.20)
        with self.assertRaisesRegex(SafetyError, "受信が途切れ"):
            lidar.state_monitor(state, SafetyGuard((0, 0)), NOW)

    def test_common_gate_never_sends_denied_or_expired_motion(self):
        robot = Robot.__new__(Robot)
        robot.command_lock = threading.Lock()
        robot.read_only = False
        robot.client = Mock()
        robot.client.Move.return_value = robot.client.StopMove.return_value = 0
        robot.guard = Mock()
        robot.state = Mock()
        with patch("go2_voice.time.monotonic", return_value=NOW):
            with self.assertRaises(SafetyError):
                robot.final_gate(robot.client.Move, 0.08, 0, 0, expires_at=NOW + 1)
            with self.assertRaises(SafetyError):
                robot.final_gate(robot.client.Move, 0.08, 0, 0, permitted=True, expires_at=NOW)
            robot.client.Move.assert_not_called()
            robot.stop(zero_velocity=True)
            robot.client.Move.assert_called_once_with(0, 0, 0)
            robot.client.StopMove.assert_called_once()
            robot.read_only = True
            with self.assertRaisesRegex(SafetyError, "読み取り専用"):
                robot.stop()


class InputTests(unittest.TestCase):
    def test_held_key_stops_when_released_or_lidar_drops_and_requires_rearming(self):
        robot, keyboard = Mock(), Mock()
        robot.guard = SafetyGuard((0, 0))
        control = app.ManualControl(robot, keyboard)
        control.status = Mock()
        keys = {app.SPACE, 17}
        keyboard.keys.return_value = keys
        world = model_with()
        with patch("go2_teleop.time.monotonic", return_value=NOW):
            control.tick(keys, NOW, world, None)
            robot.final_gate.assert_not_called()
            control.tick(set(), NOW, world, None)
            control.tick(keys, NOW, world, None)
            robot.final_gate.assert_called_once()

            control.tick({app.SPACE}, NOW, world, None)
            robot.stop.assert_called_once_with(zero_velocity=True)
            control.tick(keys, NOW, world, None)
            robot.final_gate.assert_called_once()

            control.tick(set(), NOW, world, None)
            control.tick(keys, NOW, world, None)
            control.tick(keys, NOW, None, "LiDAR timeout")
            self.assertEqual(robot.stop.call_count, 2)
            control.tick(keys, NOW, world, None)
            self.assertEqual(robot.final_gate.call_count, 2)

    def test_releases_during_collision_check_cannot_send(self):
        robot, keyboard = Mock(), Mock()
        robot.guard = SafetyGuard((0, 0))
        keyboard.keys.return_value = set()
        control = app.ManualControl(robot, keyboard)
        control.status = Mock()
        world = model_with()
        with patch("go2_teleop.time.monotonic", return_value=NOW):
            control.tick(set(), NOW, world, None)
            control.tick({app.SPACE, 17}, NOW, world, None)
        robot.final_gate.assert_not_called()

    def test_obstacle_during_a_held_key_stops_without_a_second_move(self):
        robot, keyboard = Mock(), Mock()
        robot.guard = SafetyGuard((0, 0))
        control = app.ManualControl(robot, keyboard)
        control.status = Mock()
        keys = keyboard.keys.return_value = {app.SPACE, 17}
        with patch("go2_teleop.time.monotonic", return_value=NOW):
            control.tick(set(), NOW, model_with(), None)
            control.tick(keys, NOW, model_with(), None)
            control.tick(keys, NOW, model_with((0.56, 0.001, -0.1)), None)
        robot.final_gate.assert_called_once()
        robot.stop.assert_called_once_with(zero_velocity=True)

    def test_cli_check_connects_read_only_even_when_state_is_abnormal(self):
        with patch.object(app.Calibration, "load"), patch.object(app, "network_interface", return_value="eth0"), \
                patch.object(app, "Robot") as robot, patch.object(app, "LidarInput") as sensor, \
                patch.object(app, "check", return_value=1):
            self.assertEqual(app.main(["--check", "--net-if", "eth0", "--calibration", "go2.json"]), 1)
        robot.assert_called_once_with("eth0", monitor_enabled=False, read_only=True, require_ready=False)
        robot.return_value.stop.assert_not_called()
        robot.return_value.close.assert_called_once_with()
        sensor.return_value.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
