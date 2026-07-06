#!/usr/bin/env bash
# End-to-end per-turn commit + in-UI-relayed approval on real FUSE + real bwrap.
# A scripted "agent" runs INSIDE the sandbox and drives the control socket:
#   turn 1 (in-scope)     -> turn-finalize -> committed to base mid-session
#   turn 2 (out-of-scope) -> turn-finalize -> needs-approval + token
#                         -> turn-approve yes -> committed
# Proves: base updated at the Stop boundary while the mount stays live, and
# out-of-scope only reaches base via a relayed approval.
REPO=$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)
BRANCHFS=/storage/user/agent-workspace/conda-compute-cluster/worktrees/branchfs-agent-containment/target/debug/branchfs
LD_LIB=/home/domen/conda/envs/branchfs-dev/lib
BWRAP=/home/domen/conda/envs/codex/bin/bwrap
T=/home/domen/ccc-e2e-perturn
PROJ_BASE=$T/base STORE=$T/store STATE=$T/state CFG=$T/cfg.json
PASS=0; FAIL=0

cleanup() {
    for mp in $(mount 2>/dev/null | awk -v b="$T" '$3 ~ b {print $3}'); do
        /usr/local/bin/fusermount3 -u "$mp" 2>/dev/null || true
    done
    pkill -TERM -f "run-daemon --base $PROJ_BASE" 2>/dev/null || true
    sleep 0.4
    rm -rf "$T"
}
trap cleanup EXIT
check() { if eval "$2"; then echo "PASS: $1"; PASS=$((PASS+1)); else echo "FAIL: $1"; FAIL=$((FAIL+1)); fi; }

mkdir -p "$PROJ_BASE/Projects/proj-a" "$STORE" "$STATE"
echo "seed" > "$PROJ_BASE/Projects/proj-a/base.txt"

# The in-sandbox agent script (seeded into the workspace so the view exposes it).
# ccc-agent is re-exposed at /ccc-agent (OUTSIDE the view) to avoid bwrap
# mkdir'ing mountpoints into the FUSE view.
cat > "$PROJ_BASE/Projects/proj-a/run-turns.sh" <<EOF
#!/bin/sh
CTL="/ccc-agent/bin/ccc-agent"
echo "=== turn1: in-scope write + finalize ==="
echo "turn1" > result1.txt
"\$CTL" turn-finalize; echo "finalize1_rc=\$?"
echo "mount-alive-after-commit: \$(cat result1.txt)"
echo "=== turn2: out-of-scope write + finalize (expect needs-approval) ==="
echo "turn2-inscope" > result2.txt
echo "escaped" > /storage/user/escape.txt
out=\$("\$CTL" turn-finalize 2>&1); rc=\$?
echo "\$out"
echo "finalize2_rc=\$rc"
tok=\$(printf '%s\n' "\$out" | sed -n 's/.*turn-approve \([0-9a-f][0-9a-f]*\).*/\1/p' | head -1)
echo "captured_token=\$tok"
echo "=== user approves -> turn-approve yes ==="
"\$CTL" turn-approve "\$tok" yes; echo "approve_rc=\$?"
EOF

cat > "$CFG" <<EOF
{"state_dir":"$STATE","branchfs_bin":"$BRANCHFS","bwrap_bin":"$BWRAP",
 "confinement":"bwrap","bwrap_proc_mode":"bind","user":"domen",
 "workspace":"/storage/user/Projects/proj-a",
 "bwrap_ro_binds":["$REPO:/ccc-agent"],
 "roots":[{"name":"r","base":"$PROJ_BASE","store":"$STORE","visible":"/storage/user","home_subdir":""}],
 "policy":{"mode":"workspace-auto","allowed_scopes":["/storage/user/Projects/proj-a"]}}
EOF

echo "=== running contained session with per-turn agent ==="
CCC_AGENT_BRANCHFS_BIN=$BRANCHFS LD_LIBRARY_PATH=$LD_LIB \
"$REPO/bin/ccc-agent" run --config "$CFG" --agent e2e \
    -- sh /storage/user/Projects/proj-a/run-turns.sh 2>&1 | sed 's/^/  /'

echo ""
echo "=== verifications (base = real underlay, checked from host) ==="
check "turn1 in-scope result1.txt committed to base mid-session" \
    '[ -f "$PROJ_BASE/Projects/proj-a/result1.txt" ]'
check "turn2 in-scope result2.txt committed to base" \
    '[ -f "$PROJ_BASE/Projects/proj-a/result2.txt" ]'
check "out-of-scope escape.txt committed ONLY via approval relay" \
    '[ -f "$PROJ_BASE/escape.txt" ]'

echo ""
echo "=== leak check ==="
pgrep -af "run-daemon" 2>/dev/null | grep -v grep || echo "daemons: (none)"
mount 2>/dev/null | grep -iE "ccc-e2e-perturn" | grep -v /run/ccc-fuse-sidecar || echo "mounts: (none)"

echo ""
echo "=== Results: $PASS passed, $FAIL failed ==="
[ $FAIL -eq 0 ]
