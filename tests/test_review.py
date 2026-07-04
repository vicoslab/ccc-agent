"""Post-session review actions (ccc_agent.ctl.Controller.review) on a
pending-review session, driven by FakeBranchFS — no FUSE.

Covers the four report actions for the operator path: accept-all, reject-all,
file-level subset, and line-level emit-patch / apply-patch.
"""

import io
import os
import pty
import shutil
import sys
import tempfile
import threading
import time
import unittest

import ccc_agent.ctl as ctl_module
from ccc_agent import cli as cli_module
from ccc_agent.branchfs import FakeBranchFS
from ccc_agent.ctl import Controller
from ccc_agent.policy import Change
from ccc_agent.paths import AliasMap
from ccc_agent.runner import RootSpec
from ccc_agent.session import SessionStore


class ReviewHarness(object):
    def __init__(self, tmp):
        self.base = os.path.join(tmp, "base")
        os.makedirs(os.path.join(self.base, "Projects", "proj-a"))
        self.backend = FakeBranchFS()
        self.store = SessionStore(os.path.join(tmp, "state"))
        self.alias = AliasMap.for_home("domen", home_subdir="")
        spec = RootSpec(name="r", base=self.base,
                        store=os.path.join(tmp, "store"),
                        visible="/storage/user", home_subdir="")
        self.session = self.store.create(
            owner="domen", agent_kind="t", agent_command=["x"],
            workspace="/storage/user/Projects/proj-a",
            policy={"mode": "workspace-auto",
                    "allowed_scopes": ["/storage/user/Projects/proj-a"]},
            protected_roots={})
        self.session.protected_roots = {
            "r": spec.materialize(self.session.session_id,
                                  self.store.state_dir)}
        self.root = self.session.protected_roots["r"]
        self.backend.start_daemon(self.root)
        self.backend.create_branch(self.root)
        self.backend.mount(self.root)
        self.ctl = Controller(self.store, self.backend, self.alias)

    def write(self, rel, content):
        p = os.path.join(self.root.mount, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as fh:
            fh.write(content)

    def write_bytes(self, rel, content):
        p = os.path.join(self.root.mount, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as fh:
            fh.write(content)

    def write_base(self, rel, content):
        p = os.path.join(self.base, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as fh:
            fh.write(content)
        return p

    def write_base_bytes(self, rel, content):
        p = os.path.join(self.base, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as fh:
            fh.write(content)
        return p

    def record_first_touch(self, rel, base_path, base_content):
        branch_dir = os.path.join(self.root.store, "branches", self.root.branch)
        os.makedirs(os.path.join(branch_dir, "touch-content"), exist_ok=True)
        key = ctl_module._touch_content_key("/" + rel)
        with open(os.path.join(branch_dir, "touch-content", key), "wb") as fh:
            fh.write(base_content.encode("utf-8"))
        with open(os.path.join(branch_dir, "touches.json"), "w") as fh:
            ctl_module.json.dump({
                "/" + rel: {
                    "path": "/" + rel,
                    "base_at_first_touch": ctl_module._path_identity(base_path),
                    "base_content_key": key,
                }
            }, fh)

    def pending(self):
        self.session.state = "pending-review"
        self.store.save(self.session)

    def base_has(self, rel):
        return os.path.isfile(os.path.join(self.base, rel))


class TestReview(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.h = ReviewHarness(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_accept_commits_everything(self):
        self.h.write("escape.txt", "x")
        self.h.pending()
        session = self.h.ctl.review(self.h.session.session_id, accept=True)
        self.assertEqual(session.state, "committed")
        self.assertTrue(self.h.base_has("escape.txt"))

    def test_reject_discards_everything(self):
        self.h.write("escape.txt", "x")
        self.h.pending()
        session = self.h.ctl.review(self.h.session.session_id, reject=True)
        self.assertEqual(session.state, "aborted")
        self.assertFalse(self.h.base_has("escape.txt"))

    def test_file_level_commits_only_chosen(self):
        self.h.write("keep.txt", "k")
        self.h.write("drop.txt", "d")
        self.h.pending()
        out = io.StringIO()
        session = self.h.ctl.review(
            self.h.session.session_id,
            commit_paths=["/storage/user/keep.txt"], out=out)
        self.assertEqual(session.state, "committed")
        self.assertTrue(self.h.base_has("keep.txt"))
        self.assertFalse(self.h.base_has("drop.txt"))

    def test_interactive_tree_selector_selects_entire_folder_subtree(self):
        changes = [
            Change("A", "/storage/user/Projects/proj-a/root.txt", "file", 1, "r"),
            Change("A", "/storage/user/Projects/proj-a/sub/a.txt", "file", 1, "r"),
            Change("A", "/storage/user/Projects/proj-a/sub/b.txt", "file", 1, "r"),
        ]
        keys = iter(["KEY_DOWN", " ", "c"])

        selected = cli_module._select_review_paths_interactive(
            changes, key_reader=lambda: next(keys), stream=io.StringIO(),
            clear_screen=False)

        self.assertEqual(set(selected), {
            "/storage/user/Projects/proj-a/sub/a.txt",
            "/storage/user/Projects/proj-a/sub/b.txt",
        })

    def test_interactive_tree_selector_opens_folder_and_goes_back(self):
        changes = [
            Change("A", "/storage/user/Projects/proj-a/root.txt", "file", 1, "r"),
            Change("A", "/storage/user/Projects/proj-a/sub/a.txt", "file", 1, "r"),
            Change("A", "/storage/user/Projects/proj-a/sub/b.txt", "file", 1, "r"),
        ]
        keys = iter(["KEY_DOWN", "KEY_ENTER", " ", "KEY_BACKSPACE", "c"])

        selected = cli_module._select_review_paths_interactive(
            changes, key_reader=lambda: next(keys), stream=io.StringIO(),
            clear_screen=False)

        self.assertEqual(selected, ["/storage/user/Projects/proj-a/sub/a.txt"])

    def _read_tree_key_from_pty(self, seq):
        master_fd, slave_fd = pty.openpty()
        old_stdin = sys.stdin
        stdin = os.fdopen(slave_fd, "r", encoding="utf-8", buffering=1)

        def writer():
            time.sleep(0.05)
            os.write(master_fd, seq)

        thread = threading.Thread(target=writer)
        thread.start()
        try:
            sys.stdin = stdin
            return cli_module._read_tree_key()
        finally:
            sys.stdin = old_stdin
            stdin.close()
            os.close(master_fd)
            thread.join(1)

    def test_read_tree_key_handles_csi_and_application_cursor_arrows(self):
        self.assertEqual(self._read_tree_key_from_pty(b"\x1b[A"), "KEY_UP")
        self.assertEqual(self._read_tree_key_from_pty(b"\x1b[B"), "KEY_DOWN")
        self.assertEqual(self._read_tree_key_from_pty(b"\x1bOA"), "KEY_UP")
        self.assertEqual(self._read_tree_key_from_pty(b"\x1bOB"), "KEY_DOWN")

    def test_accept_auto_merges_clean_same_file_text_changes(self):
        rel = "Projects/proj-a/merge.txt"
        base_path = self.h.write_base(rel, "one\ntwo\nthree\n")
        self.h.record_first_touch(rel, base_path, "one\ntwo\nthree\n")
        self.h.write_base(rel, "ONE current\ntwo\nthree\n")
        self.h.write(rel, "one\ntwo\nTHREE session\n")
        self.h.pending()

        session = self.h.ctl.review(self.h.session.session_id, accept=True)

        self.assertEqual(session.state, "committed")
        with open(os.path.join(self.h.base, rel)) as fh:
            self.assertEqual(fh.read(), "ONE current\ntwo\nTHREE session\n")
        report_path = os.path.join(self.h.store.review_dir(session.session_id),
                                   "commit-conflicts.json")
        with open(report_path) as fh:
            report = ctl_module.json.load(fh)
        self.assertEqual(len(report["auto_merges"]), 1)
        self.assertEqual(report["conflicts"], [])

    def test_accept_reports_overlapping_same_file_conflict_latest_session_wins(self):
        rel = "Projects/proj-a/conflict.txt"
        base_path = self.h.write_base(rel, "alpha\nbeta\n")
        self.h.record_first_touch(rel, base_path, "alpha\nbeta\n")
        self.h.write_base(rel, "alpha current\nbeta\n")
        self.h.write(rel, "alpha session\nbeta\n")
        self.h.pending()

        session = self.h.ctl.review(self.h.session.session_id, accept=True)

        self.assertEqual(session.state, "committed")
        with open(os.path.join(self.h.base, rel)) as fh:
            self.assertEqual(fh.read(), "alpha session\nbeta\n")
        report_path = os.path.join(self.h.store.review_dir(session.session_id),
                                   "commit-conflicts.json")
        with open(report_path) as fh:
            report = ctl_module.json.load(fh)
        self.assertEqual(report["auto_merges"], [])
        self.assertEqual(len(report["conflicts"]), 1)
        self.assertEqual(report["conflicts"][0]["resolution"], "session_won")

    def test_emit_patch_shows_unified_diff(self):
        # seed a base file, modify it in the view -> patch should show the hunk
        with open(os.path.join(self.h.base, "Projects", "proj-a", "f.txt"),
                  "w") as fh:
            fh.write("old line\n")
        self.h.write("Projects/proj-a/f.txt", "new line\n")
        self.h.pending()
        out = io.StringIO()
        self.h.ctl.review(self.h.session.session_id, emit_patch=True, out=out)
        patch = out.getvalue()
        self.assertIn("-old line", patch)
        self.assertIn("+new line", patch)
        self.assertIn("b/Projects/proj-a/f.txt", patch)

    def test_review_show_file_diffs_includes_only_text_file_hunks(self):
        text_rel = "Projects/proj-a/text.txt"
        binary_rel = "Projects/proj-a/blob.bin"
        self.h.write_base(text_rel, "old line\n")
        self.h.write(text_rel, "new line\n")
        self.h.write_base_bytes(binary_rel, b"\x00old")
        self.h.write_bytes(binary_rel, b"\x00new")
        self.h.pending()

        out = io.StringIO()
        self.h.ctl.review(self.h.session.session_id,
                          show_file_diffs=True, out=out)
        text = out.getvalue()

        self.assertIn("M /storage/user/Projects/proj-a/text.txt", text)
        self.assertIn("M /storage/user/Projects/proj-a/blob.bin", text)
        self.assertIn("Text file diffs:", text)
        self.assertIn("--- a/Projects/proj-a/text.txt", text)
        self.assertIn("-old line", text)
        self.assertIn("+new line", text)
        self.assertIn("skipped binary/non-text: /storage/user/Projects/proj-a/blob.bin", text)
        self.assertNotIn("\x00new", text)

    def test_diff_show_file_diffs_includes_text_hunks(self):
        rel = "Projects/proj-a/diff.txt"
        self.h.write_base(rel, "old\n")
        self.h.write(rel, "new\n")
        self.h.pending()

        out = io.StringIO()
        self.h.ctl.diff(self.h.session.session_id,
                        show_file_diffs=True, out=out)
        text = out.getvalue()

        self.assertIn("M /storage/user/Projects/proj-a/diff.txt", text)
        self.assertIn("Text file diffs:", text)
        self.assertIn("--- a/Projects/proj-a/diff.txt", text)
        self.assertIn("-old", text)
        self.assertIn("+new", text)

    @unittest.skipUnless(shutil.which("patch"), "patch(1) not available")
    def test_apply_patch_applies_hunks_to_base(self):
        with open(os.path.join(self.h.base, "Projects", "proj-a", "f.txt"),
                  "w") as fh:
            fh.write("old line\n")
        self.h.write("Projects/proj-a/f.txt", "new line\n")
        self.h.pending()
        emit = io.StringIO()
        self.h.ctl.review(self.h.session.session_id, emit_patch=True, out=emit)
        patch_file = os.path.join(self._tmp.name, "changes.patch")
        with open(patch_file, "w") as fh:
            fh.write(emit.getvalue())
        session = self.h.ctl.review(self.h.session.session_id,
                                    apply_patch=patch_file, out=io.StringIO())
        self.assertEqual(session.state, "committed")
        with open(os.path.join(self.h.base, "Projects", "proj-a",
                               "f.txt")) as fh:
            self.assertEqual(fh.read(), "new line\n")


if __name__ == "__main__":
    unittest.main()
