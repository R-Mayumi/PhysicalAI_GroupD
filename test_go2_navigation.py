"""実機・DDS接続を行わず、uslamコマンドと中断処理を検証する。"""

from collections import deque
import contextlib
import io
import json
import math
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from nav_msgs.msg import Odometry
from std_msgs.msg import String

from make_code.navigation import check_pose, go2_goto, goto, ros_connection


class PoseTests(unittest.TestCase):
    def test_normalizes_quaternion_and_rejects_invalid_pose(self):
        msg = Odometry()
        msg.pose.pose.orientation.w = 0.0
        with self.assertRaises(ValueError):
            check_pose.planar_pose(msg)
        msg.pose.pose.orientation.w = 2 * math.cos(0.6)
        msg.pose.pose.orientation.z = 2 * math.sin(0.6)
        self.assertAlmostEqual(check_pose.planar_pose(msg)[2], 1.2)
        msg.pose.pose.position.x = float("nan")
        with self.assertRaises(ValueError):
            check_pose.planar_pose(msg)

    def test_bad_target_file_prevents_connection(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "locations.json"
            with patch.object(goto, "init_ros") as init, contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(goto.main(["desk", "--map", str(path)]), 1)
                path.write_text('{"desk": {"x": true, "y": 0, "yaw": 0}}')
                self.assertEqual(goto.main(["desk", "--map", str(path)]), 1)
                path.write_text('{"desk": {"x": 1, "y": 2}}')
                self.assertEqual(goto.main(["desk", "--map", str(path)]), 1)
                init.assert_not_called()
            path.write_text('{"desk": {"x": 1, "y": 2, "yaw": 7}}')
            x, y, yaw = goto.load_target("desk", path)
            self.assertEqual((x, y), (1, 2))
            self.assertTrue(-math.pi <= yaw <= math.pi)

    def test_environment_file_precedence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env").write_text('GO2_NET_IF="eth-go2" # robot\n')
            with patch.object(ros_connection, "ROOT", root), \
                    patch.dict(ros_connection.os.environ, {}, clear=True):
                self.assertEqual(ros_connection.configured_interface(), "eth-go2")
                ros_connection.os.environ["GO2_NET_IF"] = "eth-env"
                self.assertEqual(ros_connection.configured_interface(), "eth-env")
                ros_connection.os.environ["GO2_NET_IF"] = ""
                self.assertIsNone(ros_connection.configured_interface())


class NavigationTests(unittest.TestCase):
    def setUp(self):
        # Nodeを生成せずに、実際のコマンド・ログ処理だけを動かす。
        self.robot = object.__new__(go2_goto.Go2Navigator)
        self.robot.logs = deque(maxlen=512)
        self.robot.log_sequence = 0
        self.robot.last_log_at = 0.0
        self.robot.active = False
        self.robot.navigation_started = False
        self.robot.pub = Mock()
        self.robot.wait_ready = Mock()
        self.robot.drain_logs = Mock(side_effect=self.robot.logs.clear)
        self.events = deque()
        self.sent = []
        self.goal_events = ["navigation/set_goal_pose/success", "navigation/state_transition/REACHED"]
        self.start_events = ["navigation/start/success"]
        self.goal_ack_echo = False
        self.robot.pub.publish.side_effect = self.publish
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(go2_goto.rclpy, "ok", return_value=True))
        self.stack.enter_context(patch.object(go2_goto.rclpy, "spin_once", side_effect=self.spin))
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.stack.enter_context(contextlib.redirect_stderr(io.StringIO()))

    def publish(self, msg):
        self.sent.append(msg.data)
        if msg.data == "navigation/start":
            self.events.extend(self.start_events)
        elif msg.data.startswith("navigation/set_goal_pose/"):
            if self.goal_ack_echo:
                self.events.extend([msg.data + "/success", "navigation/state_transition/REACHED"])
            else:
                self.events.extend(self.goal_events)
        elif msg.data == "navigation/stop":
            self.events.append("navigation/stop/success")

    def spin(self, node, timeout_sec):
        if self.events:
            event = self.events.popleft()
            if isinstance(event, BaseException):
                raise event
            node.on_log(String(data=event))

    def short_command_ack(self, command, timeout=5):
        return go2_goto.Go2Navigator.command_ack(self.robot, command, timeout=0.01)

    def test_start_then_plain_goal_and_reuse_started_navigation(self):
        self.assertTrue(self.robot.goto(1, 2, 0, timeout=0.01))
        self.assertEqual(self.sent, ["navigation/start", "navigation/set_goal_pose/1.0/2.0/0.0"])
        self.assertTrue(self.robot.goto(2, 3, 0, timeout=0.01))
        self.assertEqual(self.sent.count("navigation/start"), 1)
        self.assertNotIn("navigation/stop", self.sent)
        self.assertFalse(any("localization/" in command for command in self.sent))

    def test_can_skip_start_and_accept_echoed_ack(self):
        self.goal_ack_echo = True
        self.assertTrue(self.robot.goto(1, 2, timeout=0.01, start_navigation=False))
        self.assertEqual(len(self.sent), 1)

    def test_terminal_before_ack_is_preserved(self):
        self.goal_events.reverse()
        self.assertTrue(self.robot.goto(1, 2, timeout=0.01))

    def test_old_success_cannot_satisfy_new_goal(self):
        self.robot.on_log(String(data="navigation/state_transition/REACHED"))
        self.goal_events = ["navigation/set_goal_pose/success"]
        self.assertFalse(self.robot.goto(1, 2, timeout=0.01))
        self.assertEqual(self.sent[-1], "navigation/stop")

    def test_rejected_goal_and_navigation_start_stop(self):
        self.goal_events = ["navigation/set_goal_pose/failed"]
        self.assertFalse(self.robot.goto(1, 2, timeout=0.01))
        self.assertEqual(self.sent[-1], "navigation/stop")
        self.start_events = ["navigation/start/failed"]
        self.sent.clear()
        self.assertFalse(self.robot.goto(1, 2, timeout=0.01))
        self.assertEqual(self.sent, ["navigation/start", "navigation/stop"])

    def test_missing_ack_requests_stop(self):
        self.robot.command_ack = self.short_command_ack
        self.goal_events = []
        self.assertFalse(self.robot.goto(1, 2, timeout=0.01))
        self.assertEqual(self.sent[-1], "navigation/stop")

    def test_failures_and_interrupts_request_stop(self):
        for failure in ["navigation/state_transition/NO_PATH", KeyboardInterrupt(), RuntimeError("DDS")]:
            with self.subTest(failure=failure):
                self.sent.clear()
                self.events.clear()
                self.goal_events = ["navigation/set_goal_pose/success", failure]
                if isinstance(failure, BaseException):
                    with self.assertRaises(type(failure)):
                        self.robot.goto(1, 2, timeout=0.01)
                else:
                    self.assertFalse(self.robot.goto(1, 2, timeout=0.01))
                self.assertEqual(self.sent[-1], "navigation/stop")

    def test_invalid_target_never_connects_or_sends(self):
        for values in [(float("nan"), 0, 0), (0, float("inf"), 0), (0, 0, True)]:
            with self.assertRaises(ValueError):
                self.robot.goto(*values)
        self.robot.wait_ready.assert_not_called()
        self.assertEqual(self.sent, [])

    def test_ready_requires_fresh_pose_and_connected_topics(self):
        self.robot.pub.get_subscription_count.return_value = 1
        self.robot.count_publishers = Mock(return_value=1)
        self.robot.last_pose_at = time.monotonic() - 100
        self.robot.pose_frame = ""
        with self.assertRaises(RuntimeError):
            go2_goto.Go2Navigator.wait_ready(self.robot, timeout=0.01)
        msg = Odometry()
        msg.header.frame_id = "map"
        msg.pose.pose.orientation.w = 1.0
        with patch.object(go2_goto.rclpy, "spin_once",
                          side_effect=lambda *args, **kwargs: self.robot.on_pose(msg)):
            go2_goto.Go2Navigator.wait_ready(self.robot, timeout=0.01)
        self.robot.count_publishers.assert_called_with(go2_goto.LOG_TOPIC)
        self.assertEqual(self.robot.pose_frame, "map")
        self.assertEqual(self.sent, [])

    def test_drain_processes_pending_logs_instead_of_using_spin_return_value(self):
        self.events.extend(["old log 1", "old log 2", "navigation/state_transition/REACHED"])
        go2_goto.Go2Navigator.drain_logs(self.robot)
        self.assertFalse(self.events)
        self.assertFalse(self.robot.logs)
        self.assertEqual(self.robot.log_sequence, 3)

    def test_json_encoded_log_and_sequence_filter(self):
        self.robot.on_log(String(data=json.dumps("navigation/start/success")))
        self.assertEqual(self.robot.wait_for(lambda log: log.endswith("success"), 0.01),
                         "navigation/start/success")
        self.assertIsNone(self.robot.wait_for(lambda log: log.endswith("success"), 0.01,
                                             after=self.robot.log_sequence))


if __name__ == "__main__":
    unittest.main()
