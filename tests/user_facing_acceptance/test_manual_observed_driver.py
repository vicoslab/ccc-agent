import json
import os
import tempfile
import unittest
from unittest import mock

from tests.user_facing_acceptance.manual_observed_driver import (
    build_operator_script,
    main,
)


class TestManualObservedDriver(unittest.TestCase):
    def test_instructions_require_real_client_and_copy_paste_prompts(self):
        scenario = {
            "agent": "codex",
            "workspace": "/storage/user/project",
            "initial_prompt": "FIRST PROMPT",
            "decision_prompt": "SECOND PROMPT",
            "operator_instructions": [
                "Open Codex Desktop.",
                "Connect to target server through the configured SSH profile.",
            ],
            "evidence": {
                "basis": "direct-observation",
                "client_product": "Codex Desktop",
                "client_version": "1.2.3",
            },
        }

        text = build_operator_script(scenario)

        self.assertIn("Open Codex Desktop", text)
        self.assertIn("Do not run codex app-server yourself", text)
        self.assertIn("/storage/user/project", text)
        self.assertIn("FIRST PROMPT", text)
        self.assertIn("SECOND PROMPT", text)
        self.assertIn("capture a screenshot", text.lower())

    def test_guided_run_writes_observed_complete_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            scenario_path = os.path.join(tmp, "scenario.json")
            result_path = os.path.join(tmp, "result.json")
            continue_path = os.path.join(tmp, "continue")
            evidence = {
                "basis": "direct-observation",
                "client_product": "Codex Desktop",
                "client_version": "1.2.3",
                "observed_at": "2026-07-17T00:00:00Z",
                "artifact": "/tmp/screenshot.png",
            }
            with open(scenario_path, "w", encoding="utf-8") as handle:
                json.dump({
                    "agent": "codex", "workspace": "/storage/user/project",
                    "initial_prompt": "FIRST", "decision_prompt": "SECOND",
                    "driver_result_file": result_path,
                    "driver_continue_file": continue_path,
                    "operator_instructions": ["Open Codex Desktop."],
                    "evidence": evidence,
                }, handle)
            open(continue_path, "w", encoding="utf-8").close()
            answers = iter([
                "", "first response", "CCC_END", "",
                "decision response", "CCC_END",
                *("y" for _ in range(8)),
                "sessionStart,stop", "ccc,ccc-status", "/tmp/screenshot.png",
            ])
            with mock.patch("builtins.input", side_effect=lambda _prompt="": next(answers)):
                self.assertEqual(main([scenario_path, "--timeout-seconds", "1"]), 0)

            with open(result_path, encoding="utf-8") as handle:
                result = json.load(handle)
            self.assertEqual(result["phase"], "complete")
            self.assertEqual(result["evidence"], evidence)
            self.assertTrue(result["used_official_client"])
            self.assertEqual(result["plugin_inventory"]["skills"],
                             ["ccc", "ccc-status"])
            self.assertIn("decision response", result["transcript"])


if __name__ == "__main__":
    unittest.main()
