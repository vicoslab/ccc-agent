#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: deploy-server-acceptance.sh --ssh TARGET --container NAME --manifest PATH [options]

Build the current working tree, install that exact wheel in a remote Docker
container, then run deterministic black-box platform acceptance there. The
repository, wheel, and manifest must be visible at the same absolute paths in
the container (the CCC shared-storage deployment model).

Required:
  --ssh TARGET             SSH destination, for example user@host
  --container NAME         Docker container receiving the wheel
  --manifest PATH          Acceptance manifest visible in the container

Options:
  --port PORT              SSH port (default: 22)
  --identity PATH          SSH identity file
  --container-user UID:GID Acceptance user (default: current uid:gid)
  --home PATH              HOME inside container (default: current HOME)
  --container-python PATH  Python used for deployment (default: /usr/bin/python3)
  --agent-path PATH        PATH prefix containing real Codex/Claude clients
                           (default: /home/domen/conda/envs/codex/bin)
  --claude-seed PATH       Rematerialize packaged Claude seed here after install
                           (default: /opt/claude-seed)
EOF
}

ssh_target= container= manifest= identity=
ssh_port=22
container_user="$(id -u):$(id -g)"
container_home="$HOME"
container_python=/usr/bin/python3
agent_path=/home/domen/conda/envs/codex/bin
claude_seed=/opt/claude-seed

while (($#)); do
  case "$1" in
    --ssh) ssh_target=${2:?}; shift 2 ;;
    --container) container=${2:?}; shift 2 ;;
    --manifest) manifest=${2:?}; shift 2 ;;
    --port) ssh_port=${2:?}; shift 2 ;;
    --identity) identity=${2:?}; shift 2 ;;
    --container-user) container_user=${2:?}; shift 2 ;;
    --home) container_home=${2:?}; shift 2 ;;
    --container-python) container_python=${2:?}; shift 2 ;;
    --agent-path) agent_path=${2:?}; shift 2 ;;
    --claude-seed) claude_seed=${2:?}; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if [[ -z "$ssh_target" || -z "$container" || -z "$manifest" ]]; then
  usage >&2
  exit 2
fi
[[ -f "$manifest" ]] || { echo "manifest does not exist: $manifest" >&2; exit 2; }

repo=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
sha=$(git -C "$repo" rev-parse HEAD)
run_id="$(date -u +%Y%m%dT%H%M%SZ)-${sha:0:12}"
artifact_dir="$repo/.artifacts/server-acceptance/$run_id"
dist_dir="$artifact_dir/dist"
mkdir -p "$dist_dir"
python -m pip wheel --no-deps "$repo" --wheel-dir "$dist_dir"
wheel=$(python - "$dist_dir" <<'PY'
import glob, os, sys
wheels = glob.glob(os.path.join(sys.argv[1], "ccc_agent-*.whl"))
if len(wheels) != 1:
    raise SystemExit("expected exactly one ccc-agent wheel, got %r" % wheels)
print(os.path.abspath(wheels[0]))
PY
)
wheel_sha256=$(sha256sum "$wheel" | cut -d' ' -f1)
patch_sha256=$(git -C "$repo" diff --binary HEAD | sha256sum | cut -d' ' -f1)

ssh_args=(-o BatchMode=yes -p "$ssh_port")
[[ -z "$identity" ]] || ssh_args+=(-i "$identity")

# The supported target is a dedicated container with an existing system-level
# install. Ubuntu marks that interpreter externally managed, so this explicit
# deployment mode uses pip's required PEP 668 override.
install_command=$(printf '%q ' docker exec "$container" "$container_python" \
  -m pip install --break-system-packages --no-deps --force-reinstall "$wheel")
ssh "${ssh_args[@]}" "$ssh_target" "$install_command"
seed_command=$(printf '%q ' docker exec "$container" "$container_python" \
  -m ccc_agent.claude_plugin --seed-dir "$claude_seed")
ssh "${ssh_args[@]}" "$ssh_target" "$seed_command"
verify_command=$(printf '%q ' docker exec "$container" "$container_python" \
  -c 'import ccc_agent; print(ccc_agent.__file__)')
installed_path=$(ssh "${ssh_args[@]}" "$ssh_target" "$verify_command")

acceptance_command=$(printf '%q ' docker exec -u "$container_user" \
  -e "HOME=$container_home" \
  -e "PATH=$agent_path:/usr/local/bin:/usr/bin:/bin" \
  -w "$repo" "$container" "$container_python" \
  -m tests.user_facing_acceptance.platform "$manifest")
set +e
ssh "${ssh_args[@]}" "$ssh_target" "$acceptance_command" \
  | tee "$artifact_dir/remote-acceptance.stdout.json"
acceptance_rc=${PIPESTATUS[0]}
set -e

python - "$artifact_dir/deployment.json" <<PY
import json, sys
with open(sys.argv[1], "w", encoding="utf-8") as handle:
    json.dump({
        "run_id": ${run_id@Q},
        "git_sha": ${sha@Q},
        "working_tree_patch_sha256": ${patch_sha256@Q},
        "wheel": ${wheel@Q},
        "wheel_sha256": ${wheel_sha256@Q},
        "ssh_target": ${ssh_target@Q},
        "container": ${container@Q},
        "container_user": ${container_user@Q},
        "claude_seed": ${claude_seed@Q},
        "manifest": ${manifest@Q},
        "installed_package": ${installed_path@Q},
        "acceptance_exit_code": $acceptance_rc,
    }, handle, indent=2, sort_keys=True)
    handle.write("\n")
PY

if ((acceptance_rc != 0)); then
  echo "server acceptance FAILED; evidence: $artifact_dir" >&2
  exit "$acceptance_rc"
fi
printf 'server acceptance PASSED\nevidence: %s\nwheel sha256: %s\n' \
  "$artifact_dir" "$wheel_sha256"
