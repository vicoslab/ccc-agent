"""CCC agent-containment Hermes plugin.

Packaged for explicit/operator Hermes plugin configuration. Setup-generated
``ccc-agent`` configs no longer point ``HERMES_BUNDLED_PLUGINS`` at this plugin
by default; process-exit review remains authoritative when the plugin is absent.

Hermes' native equivalent of Claude's ``additionalContext`` is the
``pre_llm_call`` plugin hook: returning ``{"context": text}`` injects text into
the current turn's user message.  This plugin uses that hook to make the bundled
``ccc-containment`` skill mandatory session context instead of relying on the model to
choose a skill by description.  At final-response/idle boundaries it also
signals the trusted CCC supervisor, sends process-pinned workspace replacements
while shell hook calls remain proposal/cleanup hints, then appends or queues a
kept-file review prompt when non-workspace/out-of-policy files remain in the
branch.

The plugin never freezes, commits, or aborts directly. It uses a process-pinned
`WorkspaceControlClient` only for complete workspace-root replacement and shells
out to `ccc-agent turn-*` for non-authoritative lifecycle/proposal signals. All
paths degrade safe: if the control socket, `ccc-agent` command, or Hermes
injection surface is unavailable, process-exit review in `ccc-agent run` remains
authoritative.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import subprocess
import threading
from pathlib import Path
from typing import Optional, Tuple

from ccc_agent.control import (ControlError, WorkspaceControlClient)

logger = logging.getLogger(__name__)

_FINALIZED_TURNS = set()
_LAST_REVIEW_DIGEST = None
_WORKSPACE_PATHS = {}
_HOOK_WORKSPACE_PATHS = {}
_WORKSPACE_CONTROL = None
_WORKSPACE_LOCK = threading.RLock()
_WORKSPACE_TAG = re.compile(r"^\[Workspace::v1: (/[^\]\n]+)\]")
_AUTHORITATIVE_WORKSPACE_TAG_PLATFORMS = frozenset(("api_server", "webui"))


CCC_REVIEW_HEADER = "CCC contained-session review is pending."


FIRST_TURN_PREFIX = (
    "CCC contained-session skill ccc-containment is active because "
    "CCC_AGENT_SESSION is set. Its rules are part of the current Hermes "
    "session context."
)

TURN_REMINDER = (
    "CCC contained-session reminder: workspace/in-policy files are written "
    "through by the supervisor, while other files stay separate. Hermes does "
    "not have an admitted CCC MCP mutation connection; do not run turn-resolve "
    "or turn-approve. When work is finished and Hermes would otherwise idle, "
    "leave kept-path resolution to external session review."
)

REVIEW_INSTRUCTION = (
    "The supervisor kept non-workspace or out-of-policy files separate and they "
    "are not committed. Do not attempt turn-resolve or turn-approve from Hermes; "
    "report that external CCC session review is required."
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


def _skill_body(name: str = "ccc-containment") -> str:
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


def _workspace_from_kwargs(**kwargs) -> str:
    for key in ("workspace", "workspace_dir", "cwd", "current_working_directory"):
        value = kwargs.get(key)
        if value:
            return str(value)
    # Hermes documents ``platform`` as a pre_llm_call field. The WebUI/API
    # adapter authoritatively prepends Workspace::v1; on messaging platforms
    # this text is ordinary user-controlled content and cannot grant scope.
    user_message = kwargs.get("user_message")
    platform = str(kwargs.get("platform") or "")
    if (platform in _AUTHORITATIVE_WORKSPACE_TAG_PLATFORMS and
            isinstance(user_message, str)):
        match = _WORKSPACE_TAG.match(user_message)
        if match:
            return match.group(1)
    return ""


def _hook_session_from_kwargs(**kwargs) -> str:
    for key in ("session_id", "conversation_id", "thread_id"):
        value = kwargs.get(key)
        if value:
            return str(value)
    return os.environ.get("CCC_AGENT_HOOK_SESSION") or os.environ.get("CCC_AGENT_SESSION", "")


def _confirm_workspaces(workspaces) -> bool:
    """Replace roots through the process-pinned in-process Hermes channel."""
    global _WORKSPACE_CONTROL
    roots = sorted(set(str(path) for path in workspaces if path))
    with _WORKSPACE_LOCK:
        if _WORKSPACE_CONTROL is None:
            sock = os.environ.get("CCC_AGENT_CONTROL_SOCK")
            token = os.environ.get("CCC_AGENT_CONTROL_TOKEN")
            if not sock or not token:
                return False
            try:
                client = WorkspaceControlClient(sock, token)
                client.admit("hermes")
                _WORKSPACE_CONTROL = client
            except (ControlError, OSError) as exc:
                logger.debug("ccc Hermes workspace admission failed: %s", exc)
                return False
        try:
            _WORKSPACE_CONTROL.confirm_workspace_roots(roots)
            return True
        except (ControlError, OSError) as exc:
            logger.debug("ccc Hermes workspace confirmation failed: %s", exc)
            return False


def _signal_workspace_start(**kwargs) -> None:
    """Confirm the per-session root union; hooks remain proposal hints."""
    if not _contained() or not _has_control_socket():
        return
    hook_session = _hook_session_from_kwargs(**kwargs)
    workspace = _workspace_from_kwargs(**kwargs)
    if not hook_session or not workspace:
        return
    with _WORKSPACE_LOCK:
        old_confirmed = _WORKSPACE_PATHS.get(hook_session)
        old_hook = _HOOK_WORKSPACE_PATHS.get(hook_session)
        if old_confirmed == workspace and old_hook == workspace:
            return
        try:
            if (old_hook and old_hook != workspace and
                    os.environ.get("CCC_AGENT_HOOK_TOKEN")):
                try:
                    _run_ctl("turn-remove-workspace", "--agent-session",
                             hook_session, old_hook)
                except Exception:
                    pass
            if os.environ.get("CCC_AGENT_HOOK_TOKEN"):
                proc = _run_ctl("turn-add-workspace", "--agent-session",
                                hook_session, workspace)
                if proc.returncode == 0:
                    _HOOK_WORKSPACE_PATHS[hook_session] = workspace
                else:
                    logger.debug("ccc turn-add-workspace exited %s: %s",
                                 proc.returncode, proc.stderr.strip())

            candidate = dict(_WORKSPACE_PATHS)
            candidate[hook_session] = workspace
            if _confirm_workspaces(candidate.values()):
                _WORKSPACE_PATHS[hook_session] = workspace
        except Exception as exc:
            logger.debug("ccc turn-add-workspace signal failed: %s", exc)


def _signal_workspace_end(**kwargs) -> None:
    """Remove only the ending session from confirmed and hook-owned state."""
    if not _contained() or not _has_control_socket():
        return
    hook_session = _hook_session_from_kwargs(**kwargs)
    if not hook_session:
        return
    with _WORKSPACE_LOCK:
        confirmed_workspace = _WORKSPACE_PATHS.get(hook_session)
        hook_workspace = _HOOK_WORKSPACE_PATHS.get(hook_session)
        if confirmed_workspace is not None:
            candidate = dict(_WORKSPACE_PATHS)
            candidate.pop(hook_session, None)
            _confirm_workspaces(candidate.values())
            # Never let a later successful replacement resurrect an ended
            # session after a transient clear failure.
            _WORKSPACE_PATHS.pop(hook_session, None)
        if hook_workspace is None:
            return
        try:
            if os.environ.get("CCC_AGENT_HOOK_TOKEN"):
                proc = _run_ctl("turn-remove-workspace", "--agent-session",
                                hook_session, hook_workspace)
                if proc.returncode not in (0,):
                    logger.debug("ccc turn-remove-workspace exited %s: %s",
                                 proc.returncode, proc.stderr.strip())
        except Exception as exc:
            logger.debug("ccc turn-remove-workspace signal failed: %s", exc)
        finally:
            _HOOK_WORKSPACE_PATHS.pop(hook_session, None)


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
    _signal_workspace_start(**_)
    if is_first_turn:
        body = _skill_body("ccc-containment")
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
        _signal_workspace_end(**kwargs)
        _queue_review_message(ctx, "on_session_end")

    ctx.register_hook("pre_llm_call", pre_llm_call)
    ctx.register_hook("transform_llm_output", transform_llm_output)
    ctx.register_hook("post_llm_call", post_llm_call)
    ctx.register_hook("on_session_end", on_session_end)
