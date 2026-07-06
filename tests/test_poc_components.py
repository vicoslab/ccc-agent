"""Component tests for the PoC soft sandbox and packaged helper scripts.

Tests all components without requiring FUSE or root:
  - soft sandbox isolate mode (write/modify/delete tracking)
  - soft sandbox tracking mode (pre-run snapshot diff)
  - hook scripts (syntax checks + dry-run behavior)
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
BIN = os.path.join(REPO, "bin")
SCRIPTS = os.path.join(REPO, "scripts")
ASSETS = os.path.join(REPO, "ccc_agent", "assets")
HOOKS = os.path.join(ASSETS, "hooks")

# branchfs binary: env var, worktree debug build, or PATH
_BRANCHFS_CANDIDATES = [
    os.environ.get("CCC_AGENT_BRANCHFS_BIN", ""),
    os.path.normpath(os.path.join(REPO, "..", "worktrees",
                                  "branchfs-agent-containment",
                                  "target", "debug", "branchfs")),
    shutil.which("branchfs") or "",
]
BRANCHFS_BIN = next(
    (c for c in _BRANCHFS_CANDIDATES if c and os.access(c, os.X_OK)),
    None
)

# LD_LIBRARY_PATH for libfuse3
_CONDA_LIB = "/home/domen/conda/envs/branchfs-dev/lib"
BRANCHFS_ENV = dict(os.environ)
if os.path.isdir(_CONDA_LIB):
    BRANCHFS_ENV["LD_LIBRARY_PATH"] = (
        _CONDA_LIB + ":" + BRANCHFS_ENV.get("LD_LIBRARY_PATH", ""))
if BRANCHFS_BIN:
    BRANCHFS_ENV["BRANCHFS_BIN"] = BRANCHFS_BIN
    BRANCHFS_ENV["CCC_AGENT_BRANCHFS_BIN"] = BRANCHFS_BIN


def run(cmd, env=None, input=None, check=False):
    return subprocess.run(
        cmd, capture_output=True, text=True,
        env=env or BRANCHFS_ENV, input=input, check=check)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
class SandboxTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ccc-poc-test-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def workspace(self):
        ws = os.path.join(self.tmp, "workspace")
        os.makedirs(ws)
        with open(os.path.join(ws, "readme.txt"), "w") as f:
            f.write("original content\n")
        with open(os.path.join(ws, "data.txt"), "w") as f:
            f.write("some data\n")
        with open(os.path.join(ws, "delete_me.txt"), "w") as f:
            f.write("to be removed\n")
        return ws

    def run_sandbox(self, workspace, cmd, extra_args=(), input=None):
        state_dir = os.path.join(self.tmp, "state")
        argv = [
            os.path.join(BIN, "ccc-agent"), "softsandbox",
            "--workspace", workspace,
            "--state-dir", state_dir,
            "--no-confirm",
        ]
        argv.extend(extra_args)
        argv.extend(["--", "bash", "-c", cmd])
        return run(argv, env=BRANCHFS_ENV, input=input), state_dir


# ---------------------------------------------------------------------------
# § 1  Soft sandbox — isolate mode
# ---------------------------------------------------------------------------
@unittest.skipUnless(BRANCHFS_BIN, "no branchfs binary found")
class TestSoftsandboxIsolate(SandboxTestBase):

    def test_new_file_committed_to_workspace(self):
        ws = self.workspace()
        result, _ = self.run_sandbox(
            ws,
            'echo "created by agent" > agent_new.txt',
            extra_args=["--isolate"],
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(os.path.isfile(os.path.join(ws, "agent_new.txt")))
        with open(os.path.join(ws, "agent_new.txt")) as f:
            self.assertIn("created by agent", f.read())

    def test_modified_file_committed(self):
        ws = self.workspace()
        result, _ = self.run_sandbox(
            ws,
            'echo "modified" > readme.txt',
            extra_args=["--isolate"],
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        with open(os.path.join(ws, "readme.txt")) as f:
            content = f.read()
        self.assertIn("modified", content)
        self.assertNotIn("original", content)

    def test_unchanged_file_preserved(self):
        ws = self.workspace()
        result, _ = self.run_sandbox(
            ws,
            'echo "only touching new.txt" > new.txt',
            extra_args=["--isolate"],
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        # data.txt untouched: must still have original content
        with open(os.path.join(ws, "data.txt")) as f:
            self.assertEqual(f.read(), "some data\n")

    def test_deleted_file_removed_from_workspace(self):
        ws = self.workspace()
        result, _ = self.run_sandbox(
            ws,
            "rm delete_me.txt",
            extra_args=["--isolate"],
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(os.path.exists(os.path.join(ws, "delete_me.txt")))

    def test_hide_path_excluded_from_agent_view(self):
        ws = self.workspace()
        os.makedirs(os.path.join(ws, ".secrets"), exist_ok=True)
        with open(os.path.join(ws, ".secrets", "api_key.txt"), "w") as f:
            f.write("supersecret\n")
        # Agent tries to cat the hidden file — should not see it
        result, state_dir = self.run_sandbox(
            ws,
            "cat .secrets/api_key.txt > /tmp/stolen_key.txt 2>/dev/null || true",
            extra_args=["--isolate", "--hide", ".secrets"],
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        # The stolen_key file should not contain the secret
        stolen = "/tmp/stolen_key.txt"
        if os.path.exists(stolen):
            with open(stolen) as f:
                self.assertNotIn("supersecret", f.read())

    def test_diff_output_shows_changes(self):
        ws = self.workspace()
        result, _ = self.run_sandbox(
            ws,
            'echo "new" > agent_new.txt && echo "modified" > readme.txt',
            extra_args=["--isolate"],
        )
        combined = result.stdout + result.stderr
        # Should see "True diff" or branch status output
        self.assertRegex(combined, r"(modified|new|delta|A |M )")

    def test_abort_leaves_workspace_unchanged(self):
        ws = self.workspace()
        # Use --no-confirm but with a failing command (rc != 0)
        # to test abort flow — or directly use interactive input
        state_dir = os.path.join(self.tmp, "state")
        argv = [
            os.path.join(BIN, "ccc-agent"), "softsandbox",
            "--workspace", ws,
            "--state-dir", state_dir,
            "--isolate",
            "--", "bash", "-c", 'echo "new file" > should_not_appear.txt',
        ]
        result = run(argv, env=BRANCHFS_ENV, input="a\n")  # "abort"
        self.assertFalse(os.path.exists(os.path.join(ws, "should_not_appear.txt")))

    def test_dry_run_does_not_modify_workspace(self):
        ws = self.workspace()
        result, _ = self.run_sandbox(
            ws,
            'echo "dry" > dry.txt',
            extra_args=["--dry-run"],
        )
        # Dry run shows plan but does not actually run the command
        self.assertFalse(os.path.exists(os.path.join(ws, "dry.txt")))
        self.assertIn("DRY RUN", result.stdout + result.stderr)

    def test_nested_session_env_runs_uncontained(self):
        ws = self.workspace()
        env = dict(BRANCHFS_ENV)
        env["CCC_AGENT_SESSION"] = "outer-session-123"
        state_dir = os.path.join(self.tmp, "state")
        argv = [
            os.path.join(BIN, "ccc-agent"), "softsandbox",
            "--workspace", ws,
            "--state-dir", state_dir,
            "--", "bash", "-c", 'echo "nested runs directly"',
        ]
        result = run(argv, env=env)
        combined = result.stdout + result.stderr
        self.assertIn("nested", combined)


# ---------------------------------------------------------------------------
# § 2  Hook scripts
# ---------------------------------------------------------------------------
class TestHookScripts(unittest.TestCase):

    def _check_syntax(self, path):
        result = subprocess.run(["bash", "-n", path], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0,
                         f"bash -n {path} failed:\n{result.stderr}")

    def test_claude_stop_hook_syntax(self):
        self._check_syntax(os.path.join(HOOKS, "claude-stop-hook.sh"))

    def test_codex_stop_hook_syntax(self):
        self._check_syntax(os.path.join(HOOKS, "codex-stop-hook.sh"))

    def test_hermes_finish_turn_syntax(self):
        self._check_syntax(os.path.join(HOOKS, "hermes-turn-record.sh"))

    def test_claude_hook_exits_0_without_session(self):
        result = subprocess.run(
            ["bash", os.path.join(HOOKS, "claude-stop-hook.sh")],
            capture_output=True, text=True,
            env={k: v for k, v in os.environ.items()
                 if k not in ("CCC_AGENT_SESSION",)})
        self.assertEqual(result.returncode, 0)

    def test_codex_hook_exits_0_without_session(self):
        result = subprocess.run(
            ["bash", os.path.join(HOOKS, "codex-stop-hook.sh")],
            capture_output=True, text=True,
            env={k: v for k, v in os.environ.items()
                 if k not in ("CCC_AGENT_SESSION",)})
        self.assertEqual(result.returncode, 0)


# ---------------------------------------------------------------------------
# § 3  Shim script
# ---------------------------------------------------------------------------
class TestShimScript(unittest.TestCase):

    SHIM = os.path.join(ASSETS, "shims", "ccc-agent-shim.sh")

    def test_syntax_check(self):
        result = subprocess.run(["bash", "-n", self.SHIM],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_shim_fails_without_launcher(self):
        """With an invalid CCC_AGENT_CLI, shim exits non-zero (1=no launcher, 127=no real bin)."""
        env = {k: v for k, v in os.environ.items()
               if k not in ("CCC_AGENT_SESSION", "CCC_AGENT_SHIM_BYPASS")}
        env["CCC_AGENT_CLI"] = "/nonexistent/ccc-agent"
        # Put the shim as "codex" in a first dir, then a real binary in a second dir
        with tempfile.TemporaryDirectory() as d:
            shim_dir = os.path.join(d, "shims")
            real_dir = os.path.join(d, "real")
            os.makedirs(shim_dir)
            os.makedirs(real_dir)
            # Shim at shim_dir/codex
            fake_codex = os.path.join(shim_dir, "codex")
            shutil.copy(self.SHIM, fake_codex)
            os.chmod(fake_codex, 0o755)
            # Real binary at real_dir/codex (will be found after the shim)
            real_bin = os.path.join(real_dir, "codex")
            with open(real_bin, "w") as f:
                f.write("#!/bin/sh\necho real codex\n")
            os.chmod(real_bin, 0o755)
            env["PATH"] = shim_dir + ":" + real_dir + ":/usr/local/bin:/usr/bin:/bin"
            result = subprocess.run([fake_codex], capture_output=True, text=True,
                                    env=env)
        # Shim should fail with exit 1 (launcher missing) and warn on stderr
        self.assertEqual(result.returncode, 1)
        self.assertIn("launcher not found", result.stderr)

    def test_shim_bypass_runs_directly(self):
        """CCC_AGENT_SHIM_BYPASS=1 bypasses the launcher."""
        env = dict(os.environ)
        env["CCC_AGENT_SHIM_BYPASS"] = "1"
        with tempfile.TemporaryDirectory() as d:
            # Create a fake "codex" shim + codex.real
            fake_shim = os.path.join(d, "codex")
            shutil.copy(self.SHIM, fake_shim)
            os.chmod(fake_shim, 0o755)
            # Put the "real" codex later in PATH
            real_dir = os.path.join(d, "real")
            os.makedirs(real_dir)
            real_bin = os.path.join(real_dir, "codex")
            with open(real_bin, "w") as f:
                f.write("#!/bin/sh\necho bypass worked\n")
            os.chmod(real_bin, 0o755)
            env["PATH"] = d + ":" + real_dir + ":/usr/bin:/bin"
            result = subprocess.run([fake_shim], capture_output=True, text=True,
                                    env=env)
        combined = result.stdout + result.stderr
        self.assertIn("bypass", combined)


# ---------------------------------------------------------------------------
# § 4  poc_branchfs_test.sh
# ---------------------------------------------------------------------------
@unittest.skipUnless(BRANCHFS_BIN, "no branchfs binary found")
class TestPocBranchfsSuite(unittest.TestCase):
    """Run the TAP-based branchfs PoC test script and verify all tests pass."""

    def test_poc_script_passes(self):
        poc = os.path.join(REPO, "poc_branchfs_test.sh")
        result = subprocess.run(
            ["bash", poc],
            capture_output=True, text=True, env=BRANCHFS_ENV,
            timeout=120,
        )
        combined = result.stdout + result.stderr
        # Verify TAP summary line
        self.assertIn("# All tests passed.", combined,
                      f"PoC test script failed:\n{combined}")
        self.assertEqual(result.returncode, 0, combined)


if __name__ == "__main__":
    unittest.main(verbosity=2)
