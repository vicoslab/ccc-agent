"""Tests for centralized, hardened workspace admission."""

import os
import tempfile
import unittest
from types import SimpleNamespace

from ccc_agent.paths import AliasMap
from ccc_agent.workspace import (WorkspaceAdmissionError,
                                 WorkspaceAdmissionPolicy)


class TestWorkspaceAdmissionPolicy(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.protected = os.path.join(self._tmp.name, "protected")
        self.projects = os.path.join(self.protected, "Projects")
        self.workspace = os.path.join(self.projects, "alpha")
        self.other = os.path.join(self.protected, "Other", "beta")
        os.makedirs(self.workspace)
        os.makedirs(self.other)
        self.root = SimpleNamespace(
            name="storage", visible=self.protected, base=self.protected,
            mount=self.protected)
        self.policy = WorkspaceAdmissionPolicy(
            {"storage": self.root}, AliasMap({}),
            workspace_admission_roots=[self.projects])

    def tearDown(self):
        self._tmp.cleanup()

    def test_rejects_malformed_remote_and_outside_paths(self):
        for path in ("", "relative", "file:///tmp/x", "ssh://host/x",
                     "/tmp/nul\x00suffix", os.path.join(self._tmp.name, "outside")):
            with self.subTest(path=path):
                with self.assertRaises(WorkspaceAdmissionError):
                    self.policy.admit(path)

    def test_canonical_key_deduplicates_ccc_home_alias(self):
        policy = WorkspaceAdmissionPolicy(
            {"storage": SimpleNamespace(name="storage", visible="/storage/user",
                                         base="/storage/user", mount="/storage/user")},
            AliasMap.for_home("alice"))
        self.assertEqual(policy.canonical_key("/home/alice/Projects/a"),
                         policy.canonical_key("/storage/user/Projects/a"))

    def test_returns_serializable_identity_record(self):
        record = self.policy.admit(self.workspace)
        self.assertEqual(record["visible_path"], self.workspace)
        self.assertEqual(record["canonical_path"], self.workspace)
        self.assertEqual(record["protected_root"], "storage")
        self.assertEqual(record["relative_path"], "Projects/alpha")
        self.assertIn("st_dev", record["identity"])
        self.assertTrue(record["components"])

    def test_rejects_protected_root_unless_broad_root_is_explicit(self):
        default = WorkspaceAdmissionPolicy(
            {"storage": self.root}, AliasMap({}),
            workspace_admission_roots=[self.protected])
        with self.assertRaises(WorkspaceAdmissionError):
            default.admit(self.protected)

        broad = WorkspaceAdmissionPolicy(
            {"storage": self.root}, AliasMap({}),
            workspace_admission_roots=[self.protected],
            allow_protected_root_workspace=True)
        self.assertEqual(broad.admit(self.protected)["canonical_path"],
                         self.protected)

    def test_rejects_workspace_outside_operator_admission_roots(self):
        with self.assertRaises(WorkspaceAdmissionError):
            self.policy.admit(self.other)

    def test_authoritative_admission_requires_existing_directory(self):
        missing = os.path.join(self.projects, "missing")
        regular = os.path.join(self.projects, "file")
        with open(regular, "w") as fh:
            fh.write("not a directory")
        with self.assertRaises(WorkspaceAdmissionError):
            self.policy.admit(missing)
        with self.assertRaises(WorkspaceAdmissionError):
            self.policy.admit(regular)
        proposed = self.policy.admit(missing, require_existing=False)
        self.assertIsNone(proposed["identity"])

    def test_rejects_symlinked_workspace_components(self):
        target = os.path.join(self.projects, "target")
        os.makedirs(os.path.join(target, "child"))
        link = os.path.join(self.projects, "link")
        os.symlink(target, link)
        with self.assertRaises(WorkspaceAdmissionError):
            self.policy.admit(os.path.join(link, "child"))

    def test_revalidate_detects_root_replacement(self):
        record = self.policy.admit(self.workspace)
        moved = self.workspace + ".old"
        os.rename(self.workspace, moved)
        os.makedirs(self.workspace)
        with self.assertRaises(WorkspaceAdmissionError):
            self.policy.revalidate(record)

    def test_contains_change_uses_canonical_boundary_checks(self):
        record = self.policy.admit(self.workspace)
        self.assertTrue(self.policy.contains_change(
            record, os.path.join(self.workspace, "src", "a.py")))
        self.assertFalse(self.policy.contains_change(
            record, self.workspace + "-other/a.py"))


if __name__ == "__main__":
    unittest.main()