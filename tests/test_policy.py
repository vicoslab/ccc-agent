"""Tests for ccc_agent.policy: change classification and commit decision."""

import unittest

from ccc_agent.paths import AliasMap
from ccc_agent.policy import (
    AUTO_COMMIT,
    NO_CHANGES,
    PENDING_REVIEW,
    ABORT,
    Change,
    PolicyConfig,
    evaluate,
    filter_ignored,
    net_final_changes,
    path_matches,
)

AMAP = AliasMap.for_home("domen", home_subdir="")

WORKSPACE = "/storage/user/Projects/proj-a"


def cfg(**kw):
    base = dict(mode="workspace-auto", allowed_scopes=[WORKSPACE])
    base.update(kw)
    return PolicyConfig.from_dict(base)


class TestPathMatches(unittest.TestCase):
    def test_component_pattern_matches_any_segment(self):
        self.assertTrue(path_matches(".ssh", "/home/domen/.ssh/id_rsa"))
        self.assertTrue(path_matches(".env", "/storage/user/Projects/a/.env"))
        self.assertFalse(path_matches(".env", "/storage/user/Projects/a/env"))

    def test_component_glob(self):
        self.assertTrue(path_matches("id_rsa*", "/home/d/.ssh/id_rsa.pub"))
        self.assertTrue(path_matches("*.pem", "/storage/user/certs/server.pem"))
        self.assertFalse(path_matches("*.pem", "/storage/user/certs/server.pem.txt"))

    def test_slashed_pattern_matches_anywhere(self):
        self.assertTrue(path_matches(".git/hooks", "/storage/user/p/.git/hooks"))
        # ... and everything below the matched directory
        self.assertTrue(path_matches(".git/hooks", "/storage/user/p/.git/hooks/pre-commit"))
        self.assertFalse(path_matches(".git/hooks", "/storage/user/p/.git/hooksX"))

    def test_absolute_pattern(self):
        self.assertTrue(path_matches("/storage/group/*", "/storage/group/shared.txt"))
        self.assertFalse(path_matches("/storage/group/*", "/storage/user/x"))


class TestEvaluate(unittest.TestCase):
    def test_no_changes(self):
        d = evaluate([], cfg(), AMAP)
        self.assertEqual(d.decision, NO_CHANGES)

    def test_all_in_scope_auto_commit(self):
        changes = [
            Change(op="A", path=f"{WORKSPACE}/src/new.py"),
            Change(op="M", path=f"{WORKSPACE}/README.md"),
            Change(op="D", path=f"{WORKSPACE}/old.txt"),
        ]
        d = evaluate(changes, cfg(), AMAP)
        self.assertEqual(d.decision, AUTO_COMMIT)
        self.assertEqual(d.out_of_scope, [])
        self.assertEqual(d.deny_matches, [])

    def test_out_of_scope_forces_review(self):
        changes = [
            Change(op="M", path=f"{WORKSPACE}/ok.py"),
            Change(op="M", path="/storage/user/.bashrc"),
        ]
        d = evaluate(changes, cfg(), AMAP)
        self.assertEqual(d.decision, PENDING_REVIEW)
        self.assertEqual(d.out_of_scope, ["/storage/user/.bashrc"])

    def test_ancestor_dirs_of_scope_are_not_out_of_scope(self):
        # Writing a nested file makes BranchFS emit structural deltas for the
        # parent directories (/storage/user/Projects, .../proj-a).  Those are
        # ancestors of the allowed scope and must not block auto-commit.
        changes = [
            Change(op="M", path="/storage/user/Projects", kind="dir"),
            Change(op="M", path=f"{WORKSPACE}", kind="dir"),
            Change(op="A", path=f"{WORKSPACE}/done.txt"),
        ]
        d = evaluate(changes, cfg(), AMAP)
        self.assertEqual(d.decision, AUTO_COMMIT)
        self.assertEqual(d.out_of_scope, [])

    def test_sibling_dir_outside_scope_still_forces_review(self):
        # A directory that is *not* an ancestor of the scope (a sibling under
        # the same parent) must still be flagged out-of-scope.
        changes = [
            Change(op="A", path=f"{WORKSPACE}/ok.py"),
            Change(op="A", path="/storage/user/Projects/proj-other", kind="dir"),
            Change(op="A", path="/storage/user/Projects/proj-other/sneak.txt"),
        ]
        d = evaluate(changes, cfg(), AMAP)
        self.assertEqual(d.decision, PENDING_REVIEW)
        self.assertEqual(
            sorted(d.out_of_scope),
            ["/storage/user/Projects/proj-other",
             "/storage/user/Projects/proj-other/sneak.txt"],
        )

    def test_home_alias_counts_as_in_scope(self):
        # change reported via /home alias, scope declared via /storage/user
        changes = [Change(op="A", path="/home/domen/Projects/proj-a/x.py")]
        d = evaluate(changes, cfg(), AMAP)
        self.assertEqual(d.decision, AUTO_COMMIT)

    def test_scope_declared_via_home_alias(self):
        changes = [Change(op="A", path=f"{WORKSPACE}/y.py")]
        d = evaluate(changes, cfg(allowed_scopes=["/home/domen/Projects/proj-a"]), AMAP)
        self.assertEqual(d.decision, AUTO_COMMIT)

    def test_deny_inside_scope_forces_review(self):
        changes = [Change(op="A", path=f"{WORKSPACE}/.env")]
        d = evaluate(changes, cfg(), AMAP)
        self.assertEqual(d.decision, PENDING_REVIEW)
        self.assertEqual(len(d.deny_matches), 1)
        self.assertEqual(d.deny_matches[0].path, f"{WORKSPACE}/.env")

    def test_git_hooks_denied_by_default(self):
        changes = [Change(op="M", path=f"{WORKSPACE}/.git/hooks/pre-commit")]
        d = evaluate(changes, cfg(), AMAP)
        self.assertEqual(d.decision, PENDING_REVIEW)

    def test_normal_git_objects_allowed(self):
        changes = [Change(op="A", path=f"{WORKSPACE}/.git/objects/ab/cdef")]
        d = evaluate(changes, cfg(), AMAP)
        self.assertEqual(d.decision, AUTO_COMMIT)

    def test_hide_patterns_act_as_deny(self):
        changes = [Change(op="A", path=f"{WORKSPACE}/secrets.yaml")]
        d = evaluate(changes, cfg(hide_patterns=["secrets.yaml"]), AMAP)
        self.assertEqual(d.decision, PENDING_REVIEW)
        self.assertEqual(len(d.deny_matches), 1)

    def test_manual_mode_always_reviews(self):
        changes = [Change(op="A", path=f"{WORKSPACE}/x.py")]
        d = evaluate(changes, cfg(mode="manual"), AMAP)
        self.assertEqual(d.decision, PENDING_REVIEW)

    def test_read_only_review_never_commits(self):
        changes = [Change(op="A", path=f"{WORKSPACE}/x.py")]
        d = evaluate(changes, cfg(mode="read-only-review"), AMAP)
        self.assertEqual(d.decision, PENDING_REVIEW)

    def test_throwaway_aborts(self):
        changes = [Change(op="A", path=f"{WORKSPACE}/x.py")]
        d = evaluate(changes, cfg(mode="throwaway"), AMAP)
        self.assertEqual(d.decision, ABORT)

    def test_unknown_mode_rejected(self):
        with self.assertRaises(ValueError):
            cfg(mode="yolo")

    def test_decision_serializable(self):
        changes = [Change(op="M", path="/storage/user/.ssh/authorized_keys")]
        d = evaluate(changes, cfg(), AMAP)
        data = d.to_dict()
        self.assertEqual(data["decision"], PENDING_REVIEW)
        self.assertEqual(data["total_changes"], 1)
        self.assertTrue(data["deny_matches"])  # .ssh is default-denied
        self.assertTrue(data["out_of_scope"])

    def test_default_ignores_history_noise_but_not_agent_state(self):
        changes = [
            Change(op="A", path="/storage/user/.codex/config.toml"),
            Change(op="M", path="/storage/user/.claude/settings.json"),
            Change(op="M", path="/storage/user/.hermes/config.yaml"),
            Change(op="M", path="/storage/user/.bash_history"),
            Change(op="D", path="/storage/user/domen-cuda10/.bash_history-00002.tmp"),
            Change(op="M", path="/storage/user/.python_history"),
            Change(op="A", path=f"{WORKSPACE}/ok.py"),
        ]
        filtered = filter_ignored(changes, cfg(), AMAP)
        self.assertEqual([c.path for c in filtered], [
            "/storage/user/.codex/config.toml",
            "/storage/user/.claude/settings.json",
            "/storage/user/.hermes/config.yaml",
            f"{WORKSPACE}/ok.py",
        ])

    def test_shell_startup_files_are_reviewed_not_ignored(self):
        changes = [Change(op="M", path="/storage/user/.bashrc")]
        filtered = filter_ignored(changes, cfg(), AMAP)
        self.assertEqual([c.path for c in filtered], ["/storage/user/.bashrc"])
        d = evaluate(filtered, cfg(), AMAP)
        self.assertEqual(d.decision, PENDING_REVIEW)

    def test_net_final_changes_prefers_rewritten_file_over_tombstone(self):
        changes = [
            Change(op="D", path=f"{WORKSPACE}/settings.json",
                   kind="tombstone", root="storage_user"),
            Change(op="M", path=f"{WORKSPACE}/settings.json",
                   kind="file", bytes=42, root="storage_user"),
        ]

        net = net_final_changes(changes, AMAP)

        self.assertEqual(len(net), 1)
        self.assertEqual(net[0].op, "M")
        self.assertEqual(net[0].kind, "file")
        self.assertEqual(net[0].bytes, 42)

    def test_evaluate_counts_delete_rewrite_as_one_final_change(self):
        changes = [
            Change(op="D", path=f"{WORKSPACE}/settings.json",
                   kind="tombstone", root="storage_user"),
            Change(op="M", path=f"{WORKSPACE}/settings.json",
                   kind="file", root="storage_user"),
        ]

        d = evaluate(changes, cfg(), AMAP)

        self.assertEqual(d.decision, AUTO_COMMIT)
        self.assertEqual(d.total_changes, 1)


if __name__ == "__main__":
    unittest.main()
