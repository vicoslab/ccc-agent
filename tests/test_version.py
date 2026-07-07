"""Tests for ccc-agent version/traceability helpers."""

import unittest
from unittest import mock

from ccc_agent import version as version_mod


class TestVersionHelpers(unittest.TestCase):
    def test_git_commit_prefers_build_metadata_from_installed_wheel(self):
        build_commit = "a" * 40
        source_commit = "b" * 40
        with mock.patch("ccc_agent.version._build_git_commit",
                        return_value=build_commit):
            with mock.patch("ccc_agent.version._source_git_commit",
                            return_value=source_commit):
                self.assertEqual(version_mod.git_commit(), build_commit)

    def test_git_commit_falls_back_to_source_checkout(self):
        source_commit = "b" * 40
        with mock.patch("ccc_agent.version._build_git_commit",
                        return_value=None):
            with mock.patch("ccc_agent.version._source_git_commit",
                            return_value=source_commit):
                self.assertEqual(version_mod.git_commit(), source_commit)

    def test_version_string_contains_full_git_commit_when_known(self):
        commit = "c" * 40
        with mock.patch("ccc_agent.version.git_commit", return_value=commit):
            self.assertEqual(
                version_mod.version_string(),
                "v0.4 (git %s)" % commit)

    def test_version_string_omits_git_commit_when_unknown(self):
        with mock.patch("ccc_agent.version.git_commit", return_value=None):
            self.assertEqual(version_mod.version_string(), "v0.4")


if __name__ == "__main__":
    unittest.main()
