import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import go2_voice as app


class SafetyTests(unittest.TestCase):
    def state(self, code=0):
        return dict(received=10.0, position=[0.0, 0.0, 0.0],
                    velocity=[0.0, 0.0, 0.0], yaw_speed=0.0, error_code=code)

    def test_default_rejects_100(self):
        with self.assertRaises(app.SafetyError):
            app.SafetyGuard([0, 0]).check(self.state(100), 10.0)

    def test_explicit_100_accepts_only_fixed_code_and_latches(self):
        for changed in (0, 101, 1013, 2010):
            with self.subTest(changed=changed):
                guard = app.SafetyGuard([0, 0], expected_error_code=100)
                self.assertEqual(guard.check(self.state(100), 10.0), 0)
                with self.assertRaises(app.SafetyError):
                    guard.check(self.state(changed), 10.0)
                with self.assertRaises(app.SafetyError):
                    guard.check(self.state(100), 10.0)

    def test_100_keeps_freshness_range_and_numeric_checks(self):
        for problem in ("stale", "range", "nan"):
            with self.subTest(problem=problem):
                state = self.state(100)
                if problem == "stale":
                    state["received"] = 9.0
                elif problem == "range":
                    state["position"][0] = app.SAFE_RANGE_M
                else:
                    state["velocity"][0] = float("nan")
                with self.assertRaises(app.SafetyError):
                    app.SafetyGuard([0, 0], expected_error_code=100).check(state, 10.0)

    def test_translation_budget_is_unchanged(self):
        guard = app.SafetyGuard([0, 0], expected_error_code=100)
        distance = sum(guard.reserve_translation() * app.LINEAR_SPEED for _ in range(2))
        self.assertAlmostEqual(distance, app.SAFE_RANGE_M)
        with self.assertRaises(app.SafetyError):
            guard.reserve_translation()


class ConfigTests(unittest.TestCase):
    def test_env_file_and_environment_precedence(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(app, "ROOT", Path(directory)), patch.dict(os.environ, {}, clear=True):
                self.assertIsNone(app.configured_network_interface())
                (Path(directory) / ".env").write_text('# comment\nGO2_NET_IF="eth-test" # comment\n')
                self.assertEqual(app.configured_network_interface(), "eth-test")
                os.environ["GO2_NET_IF"] = "eth-env"
                self.assertEqual(app.configured_network_interface(), "eth-env")
                os.environ["GO2_NET_IF"] = ""
                self.assertIsNone(app.configured_network_interface())

    def test_cli_overrides_file_and_check_sends_no_stop(self):
        with patch.object(app, "configured_network_interface") as config, \
                patch.object(app, "network_interface", return_value="eth-cli") as interface, \
                patch.object(app, "Robot") as robot:
            self.assertEqual(app.main(["--check", "--net-if", "eth-cli", "--allow-state-code", "100"]), 0)
            config.assert_not_called()
            interface.assert_called_once_with("eth-cli")
            robot.assert_called_once_with("eth-cli", monitor_enabled=False, allowed_state_code=100)
            robot.return_value.stop.assert_not_called()
            robot.return_value.close.assert_called_once()

    def test_default_check_uses_config(self):
        with patch.object(app, "configured_network_interface", return_value="eth-file"), \
                patch.object(app, "network_interface", return_value="eth-file") as interface, \
                patch.object(app, "Robot") as robot:
            self.assertEqual(app.main(["--check"]), 0)
            interface.assert_called_once_with("eth-file")
            robot.assert_called_once_with("eth-file", monitor_enabled=False, allowed_state_code=None)

    def test_other_code_cannot_be_allowed(self):
        with patch.object(app, "Robot") as robot:
            with self.assertRaises(SystemExit) as error:
                app.main(["--check", "--allow-state-code", "1013"])
            self.assertEqual(error.exception.code, 2)
            robot.assert_not_called()


if __name__ == "__main__":
    unittest.main()
