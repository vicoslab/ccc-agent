"""Hermes in-process workspace integration uses per-session replacements."""

import importlib.util
import os
import unittest


_PLUGIN = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "ccc_agent", "assets",
    "plugins", "hermes-ccc-containment", "__init__.py")
_spec = importlib.util.spec_from_file_location("ccc_hermes_plugin_test", _PLUGIN)
plugin = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(plugin)


class FakeWorkspaceControl(object):
    def __init__(self):
        self.calls = []

    def replace_workspace_session(self, logical_session_id, generation, paths,
                                  state="active"):
        self.calls.append((logical_session_id, generation, list(paths), state))
        return {"ok": True}


class TestHermesWorkspacePlugin(unittest.TestCase):
    def setUp(self):
        plugin._WORKSPACE_CONTROL = FakeWorkspaceControl()
        plugin._WORKSPACE_GENERATIONS.clear()
        plugin._WORKSPACE_PATHS.clear()

    def test_independent_logical_sessions_never_send_a_global_union(self):
        self.assertTrue(plugin._replace_workspace_session(
            "conversation-a", "/storage/user/Projects/a"))
        self.assertTrue(plugin._replace_workspace_session(
            "conversation-b", "/storage/user/Projects/b"))
        self.assertTrue(plugin._replace_workspace_session(
            "conversation-a", state="ended"))

        self.assertEqual(plugin._WORKSPACE_CONTROL.calls, [
            ("conversation-a", 1, ["/storage/user/Projects/a"], "active"),
            ("conversation-b", 1, ["/storage/user/Projects/b"], "active"),
            ("conversation-a", 2, [], "ended"),
        ])


if __name__ == "__main__":
    unittest.main()
