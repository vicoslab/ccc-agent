"""Tests for ccc_agent.ctl: the human/operator session control surface."""

import io
import json
import os
import tempfile
import unittest
from unittest import mock

from ccc_agent import ctl
from ccc_agent.branchfs import (BranchfsError, FakeBranchFS, StatusReport,
                                StatusWarning)
from ccc_agent.paths import AliasMap
from ccc_agent.policy import Change
from ccc_agent.runner import RootSpec, RunnerConfig, run_session
from ccc_agent.session import ProtectedRoot, SessionStore


class CtlHarness(object):
    def __init__(self, tmp):
        self.tmp = tmp
        self.state_dir = os.path.join(tmp, "state")
        self.base = os.path.join(tmp, "real", "storage_user")
        os.makedirs(os.path.join(self.base, "Projects", "proj-a"),
                    exist_ok=True)
        self.backend = FakeBranchFS()
        self.store = SessionStore(self.state_dir)
        self.alias_map = AliasMap.for_home("domen", home_subdir="")

    def run_agent(self, argv, mode="workspace-auto", ignore_patterns=()):
        return run_session(RunnerConfig(
            store=self.store, backend=self.backend, alias_map=self.alias_map,
            owner="domen", agent_kind="fake", agent_command=list(argv),
            workspace="/home/domen/Projects/proj-a",
            policy={"mode": mode,
                    "allowed_scopes": ["/home/domen/Projects/proj-a"],
                    "ignore_patterns": list(ignore_patterns)},
            roots=[RootSpec(name="storage_user", base=self.base,
                            store=os.path.join(self.tmp, "stores",
                                               "storage_user"),
                            visible="/storage/user", home_subdir="")],
        ))

    def controller(self):
        return ctl.Controller(store=self.store, backend=self.backend,
                              alias_map=self.alias_map)

    def running_session(self, mode="workspace-auto", branch="agent-live",
                        max_repair_attempts=2):
        """A mounted session parked in `running`, like a launcher mid-run."""
        root = ProtectedRoot(
            name="storage_user", base=self.base,
            store=os.path.join(self.tmp, "stores", "storage_user"),
            branch=branch, mount=os.path.join(self.tmp, "mounts", branch),
            visible="/storage/user", home_subdir="")
        session = self.store.create(
            owner="domen", agent_kind="hermes-gateway",
            agent_command=["hermes", "serve"],
            workspace="/home/domen/Projects/proj-a",
            policy={"mode": mode,
                    "allowed_scopes": ["/home/domen/Projects/proj-a"],
                    "max_policy_repair_attempts": max_repair_attempts},
            protected_roots={"storage_user": root}, completion="manual")
        self.backend.start_daemon(root)
        self.backend.create_branch(root)
        self.backend.mount(root)
        session.transition("mounting")
        session.transition("running")
        self.store.save(session)
        return session, root


class TestController(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.h = CtlHarness(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def pending_session(self):
        # out-of-scope write => pending-review with frozen branch
        return self.h.run_agent(["sh", "-c", "echo x > ../../outside.txt"])

    def cache_review_session(self):
        return self.h.run_agent([
            "sh", "-c",
            "echo keep > result.txt; mkdir -p ../../.cache/pip; "
            "echo wheel > ../../.cache/pip/wheel.txt",
        ], mode="manual")

    def test_list_renders_sessions(self):
        session = self.pending_session()
        out = io.StringIO()
        self.h.controller().list(out=out)
        text = out.getvalue()
        self.assertIn(session.session_id, text)
        self.assertIn("pending-review", text)

    def test_show_dumps_json(self):
        session = self.pending_session()
        out = io.StringIO()
        self.h.controller().show(session.session_id, out=out)
        data = json.loads(out.getvalue())
        self.assertEqual(data["session_id"], session.session_id)

    def test_diff_prints_stored_changes(self):
        session = self.pending_session()
        out = io.StringIO()
        self.h.controller().diff(session.session_id, out=out)
        self.assertIn("/storage/user/outside.txt", out.getvalue())

    def test_diff_prints_collapsed_delete_summary(self):
        session = self.pending_session()
        review = self.h.store.review_dir(session.session_id)
        status_path = os.path.join(review, "status.storage_user.json")
        os.makedirs(os.path.join(self.h.base, "tree"), exist_ok=True)
        change = Change("D", "/storage/user/tree", "tombstone", 0,
                        "storage_user", summary="3 nested deletions hidden")
        with open(status_path, "w") as fh:
            json.dump([change.to_dict()], fh)

        out = io.StringIO()
        self.h.controller().diff(session.session_id, out=out)

        self.assertIn("D /storage/user/tree", out.getvalue())
        self.assertIn("3 nested deletions hidden", out.getvalue())

    def test_diff_filters_stored_tombstone_when_underlying_path_is_missing(self):
        session = self.pending_session()
        review = self.h.store.review_dir(session.session_id)
        status_path = os.path.join(review, "status.storage_user.json")
        stale = Change("D", "/storage/user/.claude.json.tmp.66.deadbeef",
                       "tombstone", 0, "storage_user")
        with open(status_path, "w") as fh:
            json.dump([stale.to_dict()], fh)

        out = io.StringIO()
        self.h.controller().diff(session.session_id, out=out)

        self.assertNotIn(".claude.json.tmp.66.deadbeef", out.getvalue())

    def test_diff_summarizes_ignored_policy_changes_without_full_listing(self):
        session = self.cache_review_session()
        out = io.StringIO()
        self.h.controller().diff(session.session_id, out=out)

        text = out.getvalue()
        self.assertIn("Changes to be committed", text)
        self.assertIn("/storage/user/Projects/proj-a/result.txt", text)
        self.assertIn("Ignored by policy (not committed)", text)
        self.assertIn("1 change(s)", text)
        self.assertIn(".cache", text)
        self.assertIn("--show-ignored", text)
        self.assertIn("--include-ignored", text)
        self.assertNotIn("/storage/user/.cache/pip/wheel.txt", text)

    def test_diff_show_ignored_lists_ignored_policy_changes(self):
        session = self.cache_review_session()
        out = io.StringIO()
        self.h.controller().diff(session.session_id, show_ignored=True, out=out)

        text = out.getvalue()
        self.assertIn("/storage/user/Projects/proj-a/result.txt", text)
        self.assertIn("/storage/user/.cache/pip/wheel.txt", text)
        self.assertIn("ignored by .cache", text)

    def test_review_default_summarizes_ignored_policy_changes(self):
        session = self.cache_review_session()
        out = io.StringIO()
        self.h.controller().review(session.session_id, out=out)

        text = out.getvalue()
        self.assertIn("Ignored by policy (not committed)", text)
        self.assertIn("--show-ignored", text)
        self.assertNotIn("/storage/user/.cache/pip/wheel.txt", text)

    def test_thaw_clears_generated_review_cache(self):
        session = self.pending_session()
        review = self.h.store.review_dir(session.session_id)
        status_path = os.path.join(review, "status.storage_user.json")
        decision_path = os.path.join(review, "policy-decision.json")
        summary_path = os.path.join(review, "summary.md")
        note_path = os.path.join(review, "operator-notes.txt")
        self.assertTrue(os.path.exists(status_path))
        self.assertTrue(os.path.exists(decision_path))
        self.assertTrue(os.path.exists(summary_path))
        with open(note_path, "w") as fh:
            fh.write("keep this human note\n")

        updated = self.h.controller().thaw(session.session_id)

        self.assertEqual(updated.state, "running")
        self.assertFalse(os.path.exists(status_path))
        self.assertFalse(os.path.exists(decision_path))
        self.assertFalse(os.path.exists(summary_path))
        self.assertTrue(os.path.exists(note_path))

    def test_review_accept_keeps_ignored_policy_changes_discarded_by_default(self):
        session = self.cache_review_session()
        updated = self.h.controller().review(session.session_id, accept=True)

        self.assertEqual(updated.state, "committed")
        self.assertTrue(os.path.isfile(os.path.join(
            self.h.base, "Projects", "proj-a", "result.txt")))
        self.assertFalse(os.path.exists(os.path.join(
            self.h.base, ".cache", "pip", "wheel.txt")))

    def test_review_accept_include_ignored_commits_ignored_policy_changes(self):
        session = self.cache_review_session()
        updated = self.h.controller().review(
            session.session_id, accept=True, include_ignored=True)

        self.assertEqual(updated.state, "committed")
        self.assertTrue(os.path.isfile(os.path.join(
            self.h.base, "Projects", "proj-a", "result.txt")))
        self.assertTrue(os.path.isfile(os.path.join(
            self.h.base, ".cache", "pip", "wheel.txt")))

    def test_diff_with_empty_review_status_does_not_fallback_to_live_branch(self):
        session = self.h.run_agent(["true"])
        self.assertEqual(session.state, "auto-committed")

        class StatusWouldBeWrong(FakeBranchFS):
            def status(self, root):
                raise AssertionError("diff should use the empty stored review")

        controller = ctl.Controller(store=self.h.store,
                                    backend=StatusWouldBeWrong(),
                                    alias_map=self.h.alias_map)
        out = io.StringIO()
        controller.diff(session.session_id, out=out)
        self.assertEqual(out.getvalue(), "")

    def test_diff_running_session_ignores_stale_review_cache(self):
        session, root = self.h.running_session(branch="agent-running-cache")
        review = self.h.store.review_dir(session.session_id)
        os.makedirs(review, exist_ok=True)
        with open(os.path.join(review, "status.storage_user.json"), "w") as fh:
            json.dump([
                Change("A", "/storage/user/stale-from-cache.txt", "file", 1,
                       "storage_user").to_dict()
            ], fh)

        live = os.path.join(root.mount, "Projects", "proj-a", "live.txt")
        os.makedirs(os.path.dirname(live), exist_ok=True)
        with open(live, "w") as fh:
            fh.write("live\n")

        out = io.StringIO()
        self.h.controller().diff(session.session_id, out=out)

        text = out.getvalue()
        self.assertIn("/storage/user/Projects/proj-a/live.txt", text)
        self.assertNotIn("stale-from-cache", text)

    def test_finish_refreshes_existing_review_cache(self):
        session, root = self.h.running_session(mode="manual",
                                               branch="agent-refresh-cache")
        review = self.h.store.review_dir(session.session_id)
        os.makedirs(review, exist_ok=True)
        stale_status = os.path.join(review, "status.storage_user.json")
        with open(stale_status, "w") as fh:
            json.dump([
                Change("A", "/storage/user/stale-before-finish.txt", "file", 1,
                       "storage_user").to_dict()
            ], fh)
        stale_ignored = os.path.join(review, "ignored.obsolete_root.json")
        with open(stale_ignored, "w") as fh:
            json.dump([
                Change("A", "/storage/user/stale-ignored.txt", "file", 1,
                       "obsolete_root").to_dict()
            ], fh)

        live = os.path.join(root.mount, "Projects", "proj-a", "fresh.txt")
        os.makedirs(os.path.dirname(live), exist_ok=True)
        with open(live, "w") as fh:
            fh.write("fresh\n")

        updated = self.h.controller().finish(session.session_id)

        self.assertEqual(updated.state, "pending-review")
        with open(stale_status) as fh:
            cached = json.load(fh)
        cached_paths = [entry["path"] for entry in cached]
        self.assertIn("/storage/user/Projects/proj-a/fresh.txt", cached_paths)
        self.assertNotIn("/storage/user/stale-before-finish.txt", cached_paths)
        self.assertFalse(os.path.exists(stale_ignored))

    def test_diff_live_status_failure_is_actionable_control_error(self):
        session, _root = self.h.running_session(branch="agent-stale-running")

        class BrokenStatus(FakeBranchFS):
            def status_report(self, root):
                raise BranchfsError(
                    "/usr/local/bin/branchfs start-daemon failed (1): "
                    "Error: Daemon failed to start")

        controller = ctl.Controller(store=self.h.store,
                                    backend=BrokenStatus(),
                                    alias_map=self.h.alias_map)

        with self.assertRaises(ctl.ControlError) as cm:
            controller.diff(session.session_id, out=io.StringIO())

        message = str(cm.exception)
        self.assertIn("could not read live BranchFS status", message)
        self.assertIn("ccc-agent resume %s" % session.session_id, message)
        self.assertIn("Daemon failed to start", message)

    def test_diff_live_status_prints_branchfs_warnings(self):
        session, root = self.h.running_session(branch="agent-live-warning")
        os.makedirs(os.path.join(root.mount, "Projects", "proj-a"), exist_ok=True)
        with open(os.path.join(root.mount, "Projects", "proj-a", "result.txt"), "w") as fh:
            fh.write("ok\n")

        class WarningStatus(FakeBranchFS):
            def status_report(self, root):
                base = FakeBranchFS.status_report(self, root)
                return StatusReport(
                    changes=base.changes,
                    warnings=[StatusWarning(
                        path="/storage/user/Projects/proj-a/unreadable",
                        message="branchfs status failed; using direct store fallback",
                        root=root.name,
                    )],
                )

        controller = ctl.Controller(store=self.h.store,
                                    backend=WarningStatus(),
                                    alias_map=self.h.alias_map)
        # Reuse the already-created branch/mount from the harness with the new
        # backend by copying FakeBranchFS' simulated state.
        controller.backend._state = self.h.backend._state
        controller.backend._deletes = self.h.backend._deletes
        controller.backend._mounted = self.h.backend._mounted

        out = io.StringIO()
        controller.diff(session.session_id, out=out)

        text = out.getvalue()
        self.assertIn("/storage/user/Projects/proj-a/result.txt", text)
        self.assertIn("WARNING", text)
        self.assertLess(text.index("WARNING"),
                        text.index("/storage/user/Projects/proj-a/result.txt"))
        self.assertIn("/storage/user/Projects/proj-a/unreadable", text)
        self.assertIn("direct store fallback", text)

    def test_diff_path_prints_unified_base_delta_diff(self):
        base_path = os.path.join(self.h.base, "Projects", "proj-a",
                                 "notes.txt")
        with open(base_path, "w") as fh:
            fh.write("old\n")
        session = self.h.run_agent([
            "sh", "-c", "printf 'old\\nnew\\n' > notes.txt",
        ], mode="manual")
        self.assertEqual(session.state, "pending-review")

        out = io.StringIO()
        self.h.controller().diff(session.session_id, "notes.txt", out=out)

        text = out.getvalue()
        self.assertIn("--- a/Projects/proj-a/notes.txt", text)
        self.assertIn("+++ b/Projects/proj-a/notes.txt", text)
        self.assertIn("+new", text)
        self.assertNotIn("/storage/user/outside.txt", text)

    def test_diff_path_uses_one_match_when_aliases_are_same_store_file(self):
        # On CCC, /home/domen can be a symlink/alias to a subdirectory under
        # /storage/user.  BranchFS/status can surface both spellings for the
        # same changed file; a file-specific diff should not force the operator
        # through an ambiguity that has only one underlying store/base file.
        self.h.alias_map = AliasMap.for_home("domen", home_subdir="domen-cuda10")
        root = ProtectedRoot(
            name="storage_user", base=self.h.base,
            store=os.path.join(self._tmp.name, "stores", "storage_user"),
            branch="agent-alias", mount=os.path.join(self._tmp.name, "mount"),
            visible="/storage/user", home_subdir="domen-cuda10")
        session = self.h.store.create(
            owner="domen", agent_kind="fake", agent_command=["true"],
            workspace="/home/domen",
            policy={"mode": "manual", "allowed_scopes": ["/home/domen"]},
            protected_roots={"storage_user": root})
        session.state = "pending-review"
        self.h.store.save(session)

        rel = os.path.join("domen-cuda10", ".gitconfig")
        base_path = os.path.join(root.base, rel)
        delta_path = os.path.join(root.store, "branches", root.branch,
                                  "files", rel)
        os.makedirs(os.path.dirname(base_path), exist_ok=True)
        os.makedirs(os.path.dirname(delta_path), exist_ok=True)
        with open(base_path, "w") as fh:
            fh.write("[user]\n")
        with open(delta_path, "w") as fh:
            fh.write("[user]\n\tname = Domen\n")

        class DuplicateAliasStatus(FakeBranchFS):
            def status_report(self, status_root):
                return StatusReport(changes=[
                    Change("M", "/home/domen/.gitconfig", "file", 23,
                           status_root.name),
                    Change("M", "/storage/user/domen-cuda10/.gitconfig",
                           "file", 23, status_root.name),
                ], warnings=[])

        controller = ctl.Controller(store=self.h.store,
                                    backend=DuplicateAliasStatus(),
                                    alias_map=self.h.alias_map)
        out = io.StringIO()
        controller.diff(session.session_id,
                        "/storage/user/domen-cuda10/.gitconfig", out=out)

        text = out.getvalue()
        self.assertIn("--- a/domen-cuda10/.gitconfig", text)
        self.assertIn("+++ b/domen-cuda10/.gitconfig", text)
        self.assertIn("+\tname = Domen", text)

    def test_diff_path_lists_choices_for_distinct_ambiguous_matches(self):
        root_a = ProtectedRoot(
            name="storage_a", base=os.path.join(self._tmp.name, "real-a"),
            store=os.path.join(self._tmp.name, "stores", "a"),
            branch="agent-a", mount=os.path.join(self._tmp.name, "mount-a"),
            visible="/storage/user")
        root_b = ProtectedRoot(
            name="storage_b", base=os.path.join(self._tmp.name, "real-b"),
            store=os.path.join(self._tmp.name, "stores", "b"),
            branch="agent-b", mount=os.path.join(self._tmp.name, "mount-b"),
            visible="/storage/user")
        session = self.h.store.create(
            owner="domen", agent_kind="fake", agent_command=["true"],
            workspace="/storage/user",
            policy={"mode": "manual", "allowed_scopes": ["/storage/user"]},
            protected_roots={"storage_a": root_a, "storage_b": root_b})
        session.state = "pending-review"
        self.h.store.save(session)

        for root, text in ((root_a, "a\n"), (root_b, "b\n")):
            base_path = os.path.join(root.base, ".gitconfig")
            delta_path = os.path.join(root.store, "branches", root.branch,
                                      "files", ".gitconfig")
            os.makedirs(os.path.dirname(base_path), exist_ok=True)
            os.makedirs(os.path.dirname(delta_path), exist_ok=True)
            with open(base_path, "w") as fh:
                fh.write("old\n")
            with open(delta_path, "w") as fh:
                fh.write(text)

        class TwoRootStatus(FakeBranchFS):
            def status_report(self, status_root):
                return StatusReport(changes=[
                    Change("M", "/storage/user/.gitconfig", "file", 2,
                           status_root.name),
                ], warnings=[])

        controller = ctl.Controller(store=self.h.store, backend=TwoRootStatus(),
                                    alias_map=self.h.alias_map)
        with self.assertRaises(ctl.ControlError) as cm:
            controller.diff(session.session_id, "/storage/user/.gitconfig",
                            out=io.StringIO())

        message = str(cm.exception)
        self.assertIn("path /storage/user/.gitconfig is ambiguous (2 matches)",
                      message)
        self.assertIn("choose one of", message)
        self.assertIn("storage_a:/storage/user/.gitconfig", message)
        self.assertIn("storage_b:/storage/user/.gitconfig", message)
        self.assertIn("root storage_a", message)
        self.assertIn("root storage_b", message)

        out = io.StringIO()
        controller.diff(session.session_id, "storage_a:/storage/user/.gitconfig",
                        out=out)
        self.assertIn("--- a/.gitconfig", out.getvalue())
        self.assertIn("+a", out.getvalue())

    def test_diff_path_prefers_file_delta_over_tombstone_for_same_relpath(self):
        root = ProtectedRoot(
            name="storage", base=os.path.join(self._tmp.name, "real-storage"),
            store=os.path.join(self._tmp.name, "stores", "storage"),
            branch="agent-delete-rewrite",
            mount=os.path.join(self._tmp.name, "mount-storage"),
            visible="/storage")
        session = self.h.store.create(
            owner="domen", agent_kind="fake", agent_command=["true"],
            workspace="/storage/user/domen-cuda10",
            policy={"mode": "manual",
                    "allowed_scopes": ["/storage/user/domen-cuda10"]},
            protected_roots={"storage": root})
        session.state = "pending-review"
        self.h.store.save(session)

        rel = os.path.join("user", "domen-cuda10", ".gitconfig")
        base_path = os.path.join(root.base, rel)
        delta_path = os.path.join(root.store, "branches", root.branch,
                                  "files", rel)
        os.makedirs(os.path.dirname(base_path), exist_ok=True)
        os.makedirs(os.path.dirname(delta_path), exist_ok=True)
        with open(base_path, "w") as fh:
            fh.write("[user]\n")
        with open(delta_path, "w") as fh:
            fh.write("[user]\n\tname = Domen\n")

        class DeleteThenRewriteStatus(FakeBranchFS):
            def status_report(self, status_root):
                path = "/storage/user/domen-cuda10/.gitconfig"
                return StatusReport(changes=[
                    Change("D", path, "tombstone", 0, status_root.name),
                    Change("M", path, "file", 23, status_root.name),
                ], warnings=[])

        controller = ctl.Controller(store=self.h.store,
                                    backend=DeleteThenRewriteStatus(),
                                    alias_map=self.h.alias_map)
        listing = io.StringIO()
        controller.diff(session.session_id, out=listing)
        listing_text = listing.getvalue()
        self.assertIn(
            "M /storage/user/domen-cuda10/.gitconfig (file, 23 bytes)",
            listing_text)
        self.assertNotIn(
            "D /storage/user/domen-cuda10/.gitconfig (tombstone",
            listing_text)

        out = io.StringIO()
        controller.diff(session.session_id,
                        "/storage/user/domen-cuda10/.gitconfig", out=out)

        text = out.getvalue()
        self.assertIn("--- a/user/domen-cuda10/.gitconfig", text)
        self.assertIn("+++ b/user/domen-cuda10/.gitconfig", text)
        self.assertIn("+\tname = Domen", text)

    def test_diff_stored_review_nets_delete_rewrite_artifacts(self):
        root = ProtectedRoot(
            name="storage", base=os.path.join(self._tmp.name, "real-storage"),
            store=os.path.join(self._tmp.name, "stores", "storage"),
            branch="agent-stored-delete-rewrite",
            mount=os.path.join(self._tmp.name, "mount-storage"),
            visible="/storage")
        session = self.h.store.create(
            owner="domen", agent_kind="fake", agent_command=["true"],
            workspace="/storage/user/domen-cuda10",
            policy={"mode": "manual",
                    "allowed_scopes": ["/storage/user/domen-cuda10"]},
            protected_roots={"storage": root})
        session.state = "pending-review"
        self.h.store.save(session)

        review = self.h.store.review_dir(session.session_id)
        os.makedirs(review, exist_ok=True)
        path = "/storage/user/domen-cuda10/.gitconfig"
        with open(os.path.join(review, "status.storage.json"), "w") as fh:
            json.dump([
                Change("D", path, "tombstone", 0, "storage").to_dict(),
                Change("M", path, "file", 23, "storage").to_dict(),
            ], fh)

        out = io.StringIO()
        self.h.controller().diff(session.session_id, out=out)

        text = out.getvalue()
        self.assertIn("M /storage/user/domen-cuda10/.gitconfig (file, 23 bytes)",
                      text)
        self.assertNotIn("D /storage/user/domen-cuda10/.gitconfig (tombstone",
                         text)

    def test_normalized_change_view_does_not_scan_deletes_pairwise(self):
        changes = [
            Change("D", "/storage/user/bulk/file-%04d.txt" % idx,
                   "tombstone", 0, "storage")
            for idx in range(1200)
        ]
        calls = []
        original = ctl._is_descendant_path

        def counted(child, parent):
            calls.append((child, parent))
            return original(child, parent)

        with mock.patch("ccc_agent.ctl._is_descendant_path", side_effect=counted):
            normalized = ctl._normalized_change_view(changes)

        self.assertEqual(len(normalized), len(changes))
        self.assertLess(len(calls), len(changes) * 10)

    def test_commit_pending_session(self):
        session = self.pending_session()
        self.h.controller().commit(session.session_id)
        reloaded = self.h.store.load(session.session_id)
        self.assertEqual(reloaded.state, "committed")
        self.assertTrue(os.path.isfile(os.path.join(self.h.base,
                                                    "outside.txt")))

    def test_commit_pending_session_does_not_apply_ignored_infra(self):
        session = self.h.run_agent([
            "sh", "-c",
            "mkdir -p ../../.codex/plugins/ccc-agent; "
            "echo hook > ../../.codex/plugins/ccc-agent/hooks.json; "
            "echo x > ../../outside.txt",
        ], ignore_patterns=["/storage/user/.codex"])
        self.assertEqual(session.state, "pending-review")

        self.h.controller().commit(session.session_id)

        self.assertTrue(os.path.isfile(os.path.join(self.h.base,
                                                    "outside.txt")))
        self.assertFalse(os.path.exists(os.path.join(
            self.h.base, ".codex", "plugins", "ccc-agent", "hooks.json")))

    def test_abort_pending_session(self):
        session = self.pending_session()
        self.h.controller().abort(session.session_id)
        reloaded = self.h.store.load(session.session_id)
        self.assertEqual(reloaded.state, "aborted")
        self.assertFalse(os.path.exists(os.path.join(self.h.base,
                                                     "outside.txt")))

    def test_abort_unmounts_running_session_before_discarding_branch(self):
        # Real BranchFS can fail abort-branch with ENOTEMPTY/.nfs leftovers if
        # the branch is still mounted.  Operator abort must quiesce the FUSE
        # view before discarding the delta.
        class MountedAbortFails(FakeBranchFS):
            def __init__(self):
                super(MountedAbortFails, self).__init__()
                self.calls = []

            def unmount(self, root):
                self.calls.append("unmount")
                super(MountedAbortFails, self).unmount(root)

            def abort(self, root):
                self.calls.append("abort")
                if root.mount in self._mounted:
                    raise RuntimeError("Directory not empty (os error 39)")
                super(MountedAbortFails, self).abort(root)

        self.h.backend = MountedAbortFails()
        session, root = self.h.running_session(branch="agent-running-abort")
        with open(os.path.join(root.mount, "junk.txt"), "w") as fh:
            fh.write("discard me\n")

        self.h.controller().abort(session.session_id)

        reloaded = self.h.store.load(session.session_id)
        self.assertEqual(reloaded.state, "aborted")
        self.assertEqual(self.h.backend.calls[-2:], ["unmount", "abort"])
        self.assertFalse(os.path.exists(os.path.join(self.h.base,
                                                     "junk.txt")))

    def test_commit_terminal_session_rejected(self):
        session = self.h.run_agent(["sh", "-c", "echo ok > fine.txt"])
        self.assertEqual(session.state, "auto-committed")
        with self.assertRaises(ctl.ControlError):
            self.h.controller().commit(session.session_id)

    def test_finish_turn_records_event(self):
        session = self.pending_session()
        self.h.controller().finish_turn(session.session_id)
        reloaded = self.h.store.load(session.session_id)
        self.assertTrue(any(e["event"] == "turn-finished"
                            for e in reloaded.events))

    def test_finish_finalizes_running_session(self):
        # simulate a long-running session that a hook/human finishes
        session, root = self.h.running_session(branch="agent-longrun")
        with open(os.path.join(root.mount, "served.txt"), "w") as fh:
            fh.write("output\n")

        self.h.controller().finish(session.session_id)
        reloaded = self.h.store.load(session.session_id)
        self.assertEqual(reloaded.state, "pending-review")

    def test_unknown_session_raises(self):
        with self.assertRaises(ctl.ControlError):
            self.h.controller().show("agent-missing")


class TestCheckBeforeFinal(unittest.TestCase):
    """Hook-driven bounded self-repair: live cleanliness check, no freeze."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.h = CtlHarness(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def touch(self, root, relpath, content="x\n"):
        path = os.path.join(root.mount, relpath)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(content)

    def check(self, session_id):
        out = io.StringIO()
        result = self.h.controller().check_before_final(session_id, out=out)
        return result, out.getvalue(), self.h.store.load(session_id)

    def test_clean_in_scope_change_allows(self):
        session, root = self.h.running_session()
        self.touch(root, "Projects/proj-a/result.txt")
        result, _text, reloaded = self.check(session.session_id)
        self.assertEqual(result, ctl.CHECK_ALLOW)
        self.assertEqual(reloaded.state, "running")
        self.assertEqual(reloaded.repair_attempts, 0)
        self.assertTrue(any(e["event"] == "check-clean"
                            for e in reloaded.events))

    def test_clean_check_ignores_mode(self):
        # manual/read-only-review gate the *commit* at finalize; the hook
        # cleanliness check still lets the agent finish its turn.
        for mode in ("manual", "read-only-review"):
            with self.subTest(mode=mode):
                session, root = self.h.running_session(
                    mode=mode, branch="agent-%s" % mode)
                self.touch(root, "Projects/proj-a/result.txt")
                result, _text, reloaded = self.check(session.session_id)
                self.assertEqual(result, ctl.CHECK_ALLOW)
                self.assertEqual(reloaded.state, "running")

    def test_out_of_scope_change_requests_repair(self):
        session, root = self.h.running_session()
        self.touch(root, "outside.txt")
        result, text, reloaded = self.check(session.session_id)
        self.assertEqual(result, ctl.CHECK_REPAIR)
        self.assertEqual(reloaded.repair_attempts, 1)
        self.assertEqual(reloaded.state, "running")
        self.assertEqual(self.h.backend.branch_state(root), "open")
        self.assertTrue(any(e["event"] == "repair-requested"
                            for e in reloaded.events))
        self.assertIn("/storage/user/outside.txt", text)
        self.assertIn("revert", text)

    def test_deny_match_requests_repair(self):
        session, root = self.h.running_session()
        self.touch(root, "Projects/proj-a/.env", "SECRET=1\n")
        result, text, reloaded = self.check(session.session_id)
        self.assertEqual(result, ctl.CHECK_REPAIR)
        self.assertEqual(reloaded.repair_attempts, 1)
        self.assertIn(".env", text)

    def test_same_file_conflict_requests_llm_repair_before_final(self):
        session, root = self.h.running_session()
        rel = "Projects/proj-a/conflict.txt"
        base_path = os.path.join(root.base, rel)
        os.makedirs(os.path.dirname(base_path), exist_ok=True)
        with open(base_path, "w") as fh:
            fh.write("alpha\nbeta\n")
        branch_dir = os.path.join(root.store, "branches", root.branch)
        os.makedirs(os.path.join(branch_dir, "touch-content"), exist_ok=True)
        key = ctl._touch_content_key("/" + rel)
        with open(os.path.join(branch_dir, "touch-content", key), "wb") as fh:
            fh.write(b"alpha\nbeta\n")
        with open(os.path.join(branch_dir, "touches.json"), "w") as fh:
            json.dump({
                "/" + rel: {
                    "path": "/" + rel,
                    "base_at_first_touch": ctl._path_identity(base_path),
                    "base_content_key": key,
                }
            }, fh)
        with open(base_path, "w") as fh:
            fh.write("alpha current\nbeta\n")
        self.touch(root, rel, "alpha session\nbeta\n")

        result, text, reloaded = self.check(session.session_id)

        self.assertEqual(result, ctl.CHECK_REPAIR)
        self.assertEqual(reloaded.repair_attempts, 1)
        self.assertIn("potential-conflict", text)
        self.assertIn("/storage/user/Projects/proj-a/conflict.txt", text)
        self.assertIn("latest session would win", text)

    def test_check_before_final_ignores_infra_patterns(self):
        session, root = self.h.running_session()
        session.policy["ignore_patterns"] = ["/storage/user/.codex"]
        self.h.store.save(session)

        self.touch(root, ".codex/plugins/ccc-agent/hooks.json", "{}\n")

        result, text, reloaded = self.check(session.session_id)
        self.assertEqual(result, ctl.CHECK_ALLOW)
        self.assertEqual(reloaded.repair_attempts, 0)
        self.assertIn("clean", text)

    def test_exhausted_budget_defers_to_review(self):
        session, root = self.h.running_session(max_repair_attempts=2)
        self.touch(root, "outside.txt")
        sid = session.session_id
        self.assertEqual(self.check(sid)[0], ctl.CHECK_REPAIR)
        self.assertEqual(self.check(sid)[0], ctl.CHECK_REPAIR)
        result, text, reloaded = self.check(sid)
        self.assertEqual(result, ctl.CHECK_EXHAUSTED)
        self.assertEqual(reloaded.repair_attempts, 2)  # no further increment
        self.assertEqual(reloaded.state, "running")
        self.assertTrue(any(e["event"] == "repair-budget-exhausted"
                            for e in reloaded.events))
        self.assertIn("review", text)

    def test_non_running_sessions_rejected(self):
        pending = self.h.run_agent(["sh", "-c", "echo x > ../../outside.txt"])
        self.assertEqual(pending.state, "pending-review")
        with self.assertRaises(ctl.ControlError):
            self.h.controller().check_before_final(pending.session_id)

        terminal = self.h.run_agent(["sh", "-c", "echo ok > fine.txt"])
        self.assertEqual(terminal.state, "auto-committed")
        with self.assertRaises(ctl.ControlError):
            self.h.controller().check_before_final(terminal.session_id)


if __name__ == "__main__":
    unittest.main()
