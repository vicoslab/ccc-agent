import os
import unittest

from tests.user_facing_acceptance.harness import (
    AGENTS,
    AcceptanceError,
    AcceptanceManifest,
    AcceptanceRunner,
)


_ENABLED = os.environ.get("CCC_AGENT_ACCEPTANCE", "").lower() in (
    "1", "true", "yes", "on")


@unittest.skipUnless(
    _ENABLED,
    "real agents disabled; use scripts/run-user-facing-acceptance.sh",
)
class TestRealUserFacingFlows(unittest.TestCase):
    """Real Codex/Claude/Hermes flows; never run in the default unit suite."""

    @classmethod
    def setUpClass(cls):
        manifest_path = os.environ.get("CCC_AGENT_ACCEPTANCE_MANIFEST")
        if not manifest_path:
            raise AcceptanceError(
                "CCC_AGENT_ACCEPTANCE_MANIFEST is required when acceptance is enabled")
        level = os.environ.get("CCC_AGENT_ACCEPTANCE_LEVEL", "full")
        cls.manifest = AcceptanceManifest.load(manifest_path, level=level)
        cls.runner = AcceptanceRunner(cls.manifest)
        # Fail before any model call if FUSE/bwrap/config/all-three-plugin wiring
        # cannot satisfy the certification contract.
        cls.runner.preflight()

    def _run_transport(self, transport):
        if transport not in self.manifest.required_transports:
            self.skipTest("%s is outside the selected acceptance level" % transport)
        requested_transport = os.environ.get("CCC_AGENT_ACCEPTANCE_TRANSPORT")
        if requested_transport and requested_transport != transport:
            self.skipTest("single-cell run selected %s" % requested_transport)
        requested_agent = os.environ.get("CCC_AGENT_ACCEPTANCE_AGENT")
        agents = (requested_agent,) if requested_agent else AGENTS
        for agent in agents:
            with self.subTest(agent=agent, transport=transport):
                session = self.runner.run(agent, transport)
                expected_state = (
                    "aborted" if self.manifest.transport(agent, transport).get(
                        "final_review_action", "accept") == "abort"
                    else "committed"
                )
                self.assertEqual(session["state"], expected_state)
                self.assertEqual(
                    session["agent_kind"],
                    self.manifest.transport(agent, transport)[
                        "expected_agent_kind"],
                )

    def test_local_cli_matrix(self):
        """Actual ``ccc-agent run`` interactive flows for all three agents."""
        self._run_transport("local-cli")

    def test_remote_direct_ssh_cli_matrix(self):
        """Direct SSH agent CLIs routed to foreground ``ccc-agent serve``."""
        self._run_transport("ssh-cli")

    def test_observed_remote_client_matrix(self):
        """Human-observed official clients; never inferred from protocol smoke."""
        self._run_transport("remote-server")


if __name__ == "__main__":
    unittest.main()
