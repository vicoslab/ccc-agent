"""CCC agent-containment Hermes plugin.

Loaded only inside a bwrap-contained CCC session (``ccc-agent run`` points
``HERMES_BUNDLED_PLUGINS`` at the read-only plugin store and sets
``HERMES_ACCEPT_HOOKS=1``). A direct ``hermes`` run does not see this plugin.

Hermes' native equivalent of Claude's ``additionalContext`` is the
``pre_llm_call`` plugin hook: returning ``{"context": text}`` injects text into
the current turn's user message.  This plugin uses that hook to make the bundled
``ccc-commit`` skill mandatory session context instead of relying on the model to
choose a skill by description.  At final-response/idle boundaries it also
signals the trusted CCC supervisor, then appends or queues a kept-file review
prompt when non-workspace/out-of-policy files remain in the branch.

The plugin never freezes, commits, or aborts directly.  It shells out only to
``ccc-agent turn-*`` commands, which reach the trusted supervisor over
``CCC_AGENT_CONTROL_SOCK``.  All paths degrade safe: if the control socket,
``ccc-agent`` command, or Hermes injection surface is unavailable, process-exit
review in ``ccc-agent run`` remains authoritative.
"""

from __future__ import annotations

import hashlib
import logging
import os
import subprocess
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

_FINALIZED_TURNS = set()
_LAST_REVIEW_DIGEST = None


CCC_REVIEW_HEADER = "CCC contained-session review is pending."


FIRST_TURN_PREFIX = (
    "CCC contained-session skill ccc-commit is active because "
    "CCC_AGENT_SESSION is set. Its rules are part of the current Hermes "
    "session context."
)

TURN_REMINDER = (
    "CCC contained-session reminder: workspace/in-policy files are written "
    "through by the supervisor, while non-workspace or out-of-policy files stay "
    "separate until the user decides. When work is finished and Hermes would "
    "otherwise idle, check kept files with ccc-agent turn-kept-status or follow "
    "the CCC review prompt; if kept files exist, ask the user whether to commit, "
    "discard, or keep them and then run the exact ccc-agent turn-resolve command "
    "matching the user's decision."
)

REVIEW_INSTRUCTION = (
    "The supervisor kept non-workspace or out-of-policy files separate and they "
    "are not committed. Ask the user whether to commit, discard, or keep them, "
    "then run the exact ccc-agent turn-resolve command that matches the user's "
    "decision."
)


def _contained() -> bool:
    return bool(os.environ.get("CCC_AGENT_SESSION"))


def _has_control_socket() -> bool:
    return bool(os.environ.get("CCC_AGENT_CONTROL_SOCK"))


def _ctl() -> str:
    return os.environ.get("CCC_AGENT_CLI", "ccc-agent")


def _plugin_root() -> Path:
    return Path(__file__).resolve().parent


def _strip_frontmatter(text: str) -> str:
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            return parts[2].strip()
    return text.strip()


def _skill_body(name: str = "ccc-commit") -> str:
    path = _plugin_root() / "skills" / name / "SKILL.md"
    try:
        return _strip_frontmatter(path.read_text(encoding="utf-8"))
    except OSError as exc:
        logger.debug("ccc skill read failed: %s", exc)
        return ""


def _run_ctl(*args: str, timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(
        [_ctl(), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
        check=False,
    )


def _signal_turn_boundary(turn_id: Optional[str] = None) -> None:
    """Report a turn/session boundary to the CCC supervisor. Never raises."""
    if turn_id and turn_id in _FINALIZED_TURNS:
        return
    if not _contained() or not _has_control_socket():
        return
    try:
        proc = _run_ctl("turn-finalize", "--default-keep")
        if proc.returncode not in (0, 2):
            logger.debug("ccc turn-finalize exited %s: %s",
                         proc.returncode, proc.stderr.strip())
    except Exception as exc:  # plumbing failure must never break the agent
        logger.debug("ccc turn-finalize signal failed: %s", exc)
    finally:
        if turn_id:
            _FINALIZED_TURNS.add(turn_id)


def _review_kept() -> Tuple[int, str]:
    if not _contained() or not _has_control_socket():
        return 0, ""
    try:
        proc = _run_ctl("turn-review-kept")
    except Exception as exc:
        logger.debug("ccc turn-review-kept failed: %s", exc)
        return 0, ""
    text = "\n".join(part for part in (proc.stdout.strip(), proc.stderr.strip())
                     if part)
    return proc.returncode, text


def _review_context(review_text: str) -> str:
    return "%s\n\n%s\n\n%s" % (
        CCC_REVIEW_HEADER,
        REVIEW_INSTRUCTION,
        review_text.strip(),
    )


def _review_digest(text: str) -> str:
    session = os.environ.get("CCC_AGENT_SESSION", "")
    return hashlib.sha256((session + "\0" + text).encode("utf-8")).hexdigest()


def _mark_or_skip_review(text: str) -> bool:
    """Return True when this exact pending-review prompt has already surfaced."""
    global _LAST_REVIEW_DIGEST
    digest = _review_digest(text)
    if digest == _LAST_REVIEW_DIGEST:
        return True
    _LAST_REVIEW_DIGEST = digest
    return False


def _clear_review_marker() -> None:
    global _LAST_REVIEW_DIGEST
    _LAST_REVIEW_DIGEST = None


def _pending_review_context(mark: bool = False) -> str:
    rc, review = _review_kept()
    if rc == 2 and review:
        if mark and _mark_or_skip_review(review):
            return ""
        return _review_context(review)
    if rc == 0:
        _clear_review_marker()
    return ""


def _pre_llm_context(is_first_turn: bool = False, **_) -> Optional[dict]:
    """Inject mandatory CCC rules into every Hermes turn."""
    if not _contained():
        return None
    parts = []
    if is_first_turn:
        body = _skill_body("ccc-commit")
        if body:
            parts.append("%s\n\n%s" % (FIRST_TURN_PREFIX, body))
    parts.append(TURN_REMINDER)
    review = _pending_review_context(mark=False)
    if review:
        parts.append(review)
    return {"context": "\n\n".join(part for part in parts if part)}


def _append_review_to_response(response_text: str = "", turn_id: Optional[str] = None,
                               **_) -> Optional[str]:
    """Finalize the turn and append a kept-file review prompt before idling."""
    if not _contained():
        return None
    _signal_turn_boundary(turn_id=turn_id)
    review = _pending_review_context(mark=True)
    if not review:
        return None
    response = response_text or ""
    if CCC_REVIEW_HEADER in response:
        return response
    if response.strip():
        return response.rstrip() + "\n\n" + review
    return review


def _queue_review_message(ctx, event: str) -> None:
    """Best-effort fallback for hook points whose return value Hermes ignores."""
    if not _contained():
        return
    review = _pending_review_context(mark=True)
    if not review:
        return
    try:
        ok = ctx.inject_message(review, role="user")
        if not ok:
            logger.debug("ccc %s review injection was not queued", event)
    except Exception as exc:
        logger.debug("ccc %s review injection failed: %s", event, exc)


def register(ctx) -> None:
    def pre_llm_call(**kwargs):
        return _pre_llm_context(**kwargs)

    def transform_llm_output(**kwargs):
        return _append_review_to_response(**kwargs)

    def post_llm_call(**kwargs):
        _signal_turn_boundary(turn_id=kwargs.get("turn_id"))
        _queue_review_message(ctx, "post_llm_call")

    def on_session_end(**kwargs):
        _signal_turn_boundary(turn_id=kwargs.get("turn_id"))
        _queue_review_message(ctx, "on_session_end")

    ctx.register_hook("pre_llm_call", pre_llm_call)
    ctx.register_hook("transform_llm_output", transform_llm_output)
    ctx.register_hook("post_llm_call", post_llm_call)
    ctx.register_hook("on_session_end", on_session_end)
