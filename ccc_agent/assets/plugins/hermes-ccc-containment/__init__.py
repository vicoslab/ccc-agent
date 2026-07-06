"""CCC agent-containment Hermes plugin.

Loaded only inside a bwrap-contained CCC session (ccc-agent run points
``HERMES_BUNDLED_PLUGINS`` at the read-only plugin store and sets
``HERMES_ACCEPT_HOOKS=1``). A direct ``hermes`` run does not see this plugin.

The hooks are best-effort turn/session-boundary SIGNALS: they shell out to
``ccc-agent turn-finalize``, which reaches the trusted supervisor over the
per-turn control socket (``CCC_AGENT_CONTROL_SOCK``). The supervisor commits the
turn's in-scope changes and defers anything out-of-scope to session-end review.
This plugin never freezes, commits, or aborts, and every path degrades safely:
if the control socket or ccc-agent is missing the hook is a silent no-op, and
process-exit finalization in ccc-agent run remains the authoritative fallback.

Hermes ``post_llm_call``/``on_session_end`` hooks cannot block the turn or feed
instructions back to the agent, so -- like the Codex ``notify`` path -- Hermes
out-of-scope changes are deferred to session-end review rather than prompted
mid-turn.
"""

from __future__ import annotations

import logging
import os
import subprocess

logger = logging.getLogger(__name__)


def _signal_turn_boundary(**_):
    """Report a turn/session boundary to the CCC supervisor. Never raises."""
    if not os.environ.get("CCC_AGENT_SESSION"):
        return  # not a contained session; inert
    if not os.environ.get("CCC_AGENT_CONTROL_SOCK"):
        return  # no per-turn control channel; session-end review handles it
    ctl = os.environ.get("CCC_AGENT_CLI", "ccc-agent")
    try:
        subprocess.run([ctl, "turn-finalize"],
                       stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL,
                       timeout=30, check=False)
    except Exception as exc:  # plumbing failure must never break the agent
        logger.debug("ccc turn-finalize signal failed: %s", exc)


def register(ctx) -> None:
    ctx.register_hook("post_llm_call", _signal_turn_boundary)
    ctx.register_hook("on_session_end", _signal_turn_boundary)
