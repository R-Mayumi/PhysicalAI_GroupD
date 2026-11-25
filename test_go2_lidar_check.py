import copy
import unittest

import go2_lidar as lidar
import go2_lidar_check as app
from go2_voice import SafetyError
from test_go2_teleop import CALIBRATION, FLOOR, NOW, sample


class DiagnosticTests(unittest.TestCase):
    def state(self):
        return {"received": NOW, "position": (0, 0, 0.32), "velocity": (0, 0, 0),
                "rpy": (0, 0, 0), "yaw_speed": 0, "error_code": 2010}

    def test_nonzero_code_can_be_observed_without_changing_state(self):
        state = self.state()
        original = copy.deepcopy(state)
        model = app.analyze_packet((sample(FLOOR), NOW, state), lidar.LocalMap(), CALIBRATION, NOW)
        self.assertTrue(model.floor)
        self.assertEqual(state, original)
        self.assertEqual(state["error_code"], 2010)

    def test_stale_moving_tilted_or_invalid_state_is_rejected(self):
        for changed in [
            {"received": NOW - 0.2}, {"received": NOW + 0.01},
            {"velocity": (0.03, 0, 0)}, {"velocity": (0, 0, float("nan"))},
            {"yaw_speed": 0.06}, {"rpy": (0.3, 0, 0)},
            {"position": (float("nan"), 0, 0)},
        ]:
            with self.subTest(changed=changed):
                with self.assertRaises(SafetyError):
                    app.diagnostic_pose(dict(self.state(), **changed), NOW)

    def test_stale_cloud_is_rejected_and_obstacle_preserved(self):
        packet = (sample(FLOOR), NOW - 0.3, self.state())
        with self.assertRaisesRegex(SafetyError, "点群が古"):
            app.analyze_packet(packet, lidar.LocalMap(), CALIBRATION, NOW)
        packet = (sample(FLOOR + [(0.56, 0.001, -0.1)]), NOW, self.state())
        model = app.analyze_packet(packet, lidar.LocalMap(), CALIBRATION, NOW)
        self.assertIn("障害物", lidar.safety_monitor(model, (lidar.LINEAR_SPEED, 0, 0), (0, 0), NOW))

    def test_height_profile_exposes_floor_band_without_modifying_calibration(self):
        pose = app.diagnostic_pose(self.state(), NOW)
        profile = app.height_profile(sample(FLOOR), pose, CALIBRATION)
        self.assertEqual(profile["floor_band_points"], len(FLOOR))
        self.assertEqual(CALIBRATION.ground_z, -0.32)


if __name__ == "__main__":
    unittest.main()
