#!/bin/sh
# Manual runtime smoke test for CCC native agent-plugin hook injection.
#
# Run on a CAPABLE CCC container (branchfs + bwrap + a working
# /etc/ccc-agent/config.json + a logged-in codex/claude/hermes). It proves:
#
#   1. plugin assets are packaged and well-formed;
#   2. a CONTAINED run loads the CCC plugin (the hook sees CCC_AGENT_SESSION /
#      CCC_AGENT_CONTROL_SOCK and a turn-finalize event appears);
#   3. a DIRECT run creates no CCC session and CCC hooks stay inert when
#      CCC_AGENT_SESSION is unset;
#   4. if the plugin is missing/broken, ccc-agent run STILL finalizes the
#      session at process exit (graceful degradation), never auto-committing
#      unsafe deltas because of the hook failure.
#
# It is intentionally best-effort and read-mostly: where a real model turn is
# needed it uses a one-shot prompt, so set the agent up for non-interactive use
# (e.g. `codex exec`, `claude -p`) before running.
set -eu

CTL="${CCC_AGENT_CLI:-ccc-agent}"
PLUGINS="$(python3 - <<'PY'
from ccc_agent.setup import plugins_dir
print(plugins_dir())
PY
)"

pass() { printf '  PASS  %s\n' "$1"; }
fail() { printf '  FAIL  %s\n' "$1"; FAILED=1; }
FAILED=0

echo "== 1. plugin asset layout (${PLUGINS}) =="
for p in claude-ccc-containment codex-ccc-containment hermes-ccc-containment; do
    [ -d "${PLUGINS}/${p}" ] && pass "${p} present" || fail "${p} missing"
done
[ -f "${PLUGINS}/.claude-plugin/marketplace.json" ] \
    && pass "claude marketplace" || fail "claude marketplace"
[ -f "${PLUGINS}/claude-ccc-containment/.claude-plugin/plugin.json" ] \
    && pass "claude manifest" || fail "claude manifest"
[ -f "${PLUGINS}/codex-ccc-containment/.codex-plugin/plugin.json" ] \
    && pass "codex manifest" || fail "codex manifest"
[ -f "${PLUGINS}/hermes-ccc-containment/plugin.yaml" ] \
    && pass "hermes manifest" || fail "hermes manifest"
for s in claude-ccc-containment/hooks/ccc-stop-hook.sh \
         codex-ccc-containment/hooks/ccc-stop-hook.sh; do
    bash -n "${PLUGINS}/${s}" 2>/dev/null && pass "syntax ${s}" || fail "syntax ${s}"
done

echo "== 2. contained run loads the plugin (per agent) =="
echo "   Run manually, then check the session events for a turn-finalize:"
echo "     ${CTL} run --agent claude -- claude -p 'create file ccc_probe.txt'"
echo "     ${CTL} run --agent codex  -- codex exec 'create file ccc_probe.txt'"
echo "     ${CTL} run --agent hermes -- hermes -z 'create file ccc_probe.txt'"
echo "     ${CTL} list   # newest session should be auto-committed/pending-review"
echo "     ${CTL} show <session> | grep -E 'control-server|finalize|committed'"

echo "== 3. direct run creates no CCC session =="
echo "   A plain 'claude'/'codex'/'hermes' run must create NO ccc-agent session"
echo "   and CCC hooks must be inert when CCC_AGENT_SESSION is unset."

echo "== 4. graceful degradation (missing/broken plugin) =="
echo "   Point an agent_plugins[*].src at a nonexistent dir (or run --no-agent-"
echo "   plugins) and repeat a contained run: the session must still reach"
echo "   auto-committed/pending-review via process-exit finalize, and"
echo "   out-of-scope deltas must remain pending-review, never auto-committed."

echo
if [ "${FAILED}" -eq 0 ]; then
    echo "asset checks OK; complete steps 2-4 interactively on a capable container."
else
    echo "asset checks FAILED — fix packaging before runtime validation." >&2
    exit 1
fi
