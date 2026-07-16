import unittest

from ccc_agent.codex_workspace import CodexWorkspaceMonitor


class TestCodexWorkspaceMonitor(unittest.TestCase):
    def test_confirms_successful_thread_start_and_runtime_roots(self):
        monitor = CodexWorkspaceMonitor("/storage/user/launch")
        monitor.observe_client({
            "id": 7, "method": "thread/start",
            "params": {"cwd": "/storage/user/project",
                       "runtimeWorkspaceRoots": ["/storage/group/shared"]},
        })
        roots = monitor.observe_server({
            "id": 7, "result": {"thread": {"id": "thread-1"}},
        })
        self.assertEqual(roots, [
            "/storage/group/shared", "/storage/user/launch",
            "/storage/user/project",
        ])

    def test_failed_request_never_changes_authority(self):
        monitor = CodexWorkspaceMonitor("/storage/user/launch")
        monitor.observe_client({"id": 8, "method": "thread/start",
                                "params": {"cwd": "/storage"}})
        self.assertIsNone(monitor.observe_server({
            "id": 8, "error": {"code": -1, "message": "rejected"}}))
        self.assertEqual(monitor.roots(), ["/storage/user/launch"])

    def test_resume_uses_authoritative_response_when_request_omits_cwd(self):
        monitor = CodexWorkspaceMonitor()
        monitor.observe_client({"id": 9, "method": "thread/resume",
                                "params": {"threadId": "thread-2"}})
        roots = monitor.observe_server({
            "id": 9,
            "result": {"thread": {"id": "thread-2",
                                   "cwd": "/storage/user/resumed",
                                   "runtimeWorkspaceRoots": [
                                       "/storage/group/resumed"]}},
        })
        self.assertEqual(roots, ["/storage/group/resumed",
                                 "/storage/user/resumed"])

    def test_turn_update_replaces_that_threads_roots_and_keeps_other_threads(self):
        monitor = CodexWorkspaceMonitor()
        for request_id, thread_id, cwd in (
                (1, "a", "/storage/user/a"),
                (2, "b", "/storage/user/b")):
            monitor.observe_client({"id": request_id, "method": "thread/start",
                                    "params": {"cwd": cwd}})
            monitor.observe_server({"id": request_id,
                                    "result": {"thread": {"id": thread_id}}})
        monitor.observe_client({"id": 3, "method": "turn/start",
                                "params": {"threadId": "a",
                                           "cwd": "/storage/user/a2"}})
        roots = monitor.observe_server({"id": 3, "result": {"turn": {"id": "t"}}})
        self.assertEqual(roots, ["/storage/user/a2"])
        self.assertEqual(roots.logical_session_id, "a")
        self.assertEqual(roots.generation, 3)

    def test_relative_and_non_path_values_are_ignored(self):
        monitor = CodexWorkspaceMonitor()
        monitor.observe_client({
            "id": 4, "method": "thread/start",
            "params": {"cwd": "relative", "runtimeWorkspaceRoots": [
                None, "relative", "/storage/user/good"]},
        })
        roots = monitor.observe_server({
            "id": 4, "result": {"thread": {"id": "thread"}},
        })
        self.assertEqual(roots, ["/storage/user/good"])

    def test_fork_tracks_the_new_thread_without_overwriting_its_parent(self):
        monitor = CodexWorkspaceMonitor()
        monitor.observe_client({"id": 1, "method": "thread/start",
                                "params": {"cwd": "/storage/user/parent"}})
        monitor.observe_server({"id": 1,
                                "result": {"thread": {"id": "parent"}}})
        monitor.observe_client({
            "id": 2, "method": "thread/fork",
            "params": {"threadId": "parent",
                       "cwd": "/storage/user/fork"},
        })

        roots = monitor.observe_server({
            "id": 2, "result": {"thread": {"id": "forked"}},
        })
        self.assertEqual(roots, ["/storage/user/fork"])
        self.assertEqual(roots.logical_session_id, "forked")

        monitor.observe_client({"id": 3, "method": "thread/archive",
                                "params": {"threadId": "forked"}})
        roots = monitor.observe_server({"id": 3, "result": {}})
        self.assertEqual(roots, [])
        self.assertEqual(roots.state, "ended")
        self.assertEqual(monitor.roots(), ["/storage/user/parent"])

    def test_out_of_order_successes_cannot_restore_stale_thread_scope(self):
        monitor = CodexWorkspaceMonitor()
        monitor.observe_client({"id": 1, "method": "thread/start",
                                "params": {"cwd": "/storage/user/base"}})
        monitor.observe_server({"id": 1,
                                "result": {"thread": {"id": "thread"}}})
        monitor.observe_client({"id": 2, "method": "turn/start",
                                "params": {"threadId": "thread",
                                           "cwd": "/storage/user/older"}})
        monitor.observe_client({"id": 3, "method": "turn/start",
                                "params": {"threadId": "thread",
                                           "cwd": "/storage/user/newer"}})

        roots = monitor.observe_server({"id": 3, "result": {}})
        self.assertEqual(roots, ["/storage/user/newer"])
        self.assertIsNone(monitor.observe_server({"id": 2, "result": {}}))
        self.assertEqual(monitor.roots(), ["/storage/user/newer"])

        monitor.observe_client({"id": 4, "method": "turn/start",
                                "params": {"threadId": "thread",
                                           "cwd": "/storage/user/resurrect"}})
        monitor.observe_client({"id": 5, "method": "thread/archive",
                                "params": {"threadId": "thread"}})
        ended = monitor.observe_server({"id": 5, "result": {}})
        self.assertEqual(ended, [])
        self.assertEqual(ended.state, "ended")
        self.assertIsNone(monitor.observe_server({"id": 4, "result": {}}))
        self.assertEqual(monitor.roots(), [])


if __name__ == "__main__":
    unittest.main()
