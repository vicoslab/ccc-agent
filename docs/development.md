# Development

## Repository layout

```text
bin/ccc-agent                  console-script wrapper for local checkout
ccc_agent/                     stdlib-only Python supervisor package
ccc_agent/assets/              bundled hooks, shims, completions, plugins
config/                        example runtime config and legacy examples
docs/                          user, install, architecture, policy, dev docs
scripts/                       diagnostic and end-to-end smoke scripts
tests/                         unittest suite
pyproject.toml                 package metadata and package-data rules
setup.py                       compatibility setup entrypoint
```

## Development install

```bash
python3 -m pip install --user -e .
ccc-agent setup --user
```

For command help from a checkout without installing:

```bash
./bin/ccc-agent --help
./bin/ccc-agent run --help
./bin/ccc-agent review --help
```

## Unit tests

Run all stdlib-only tests:

```bash
python3 -m unittest discover
```

Run targeted tests:

```bash
python3 -m unittest tests.test_policy
python3 -m unittest tests.test_runner
python3 -m unittest tests.test_cli
```

The suite is designed to run without real FUSE. Integration tests that can use a
real `branchfs` binary do so when available or when `CCC_AGENT_BRANCHFS_BIN` is
set.

## Script checks and smoke tests

Syntax/check selected scripts:

```bash
bash -n ccc_agent/assets/hooks/claude-stop-hook.sh
bash -n ccc_agent/assets/hooks/codex-stop-hook.sh
bash -n ccc_agent/assets/shims/ccc-agent-shim.sh
```

Smoke scripts under `scripts/` exercise specific runtime behavior:

```bash
scripts/test-e2e-none.sh       # policy pipeline without real sandbox boundary
scripts/test-plugin-hooks.sh   # bundled plugin/hook behavior where supported
scripts/test-e2e-bwrap.sh      # real bwrap + FUSE path; needs capable host
```

The opt-in user-facing acceptance suite launches the real Codex, Claude, and
Hermes executables directly for local CLI and SSH CLI cells. Its remote-client
cells are human-observed runs through the official Desktop/WebUI product; an
inferred app-server command cannot satisfy them:

```bash
scripts/run-user-facing-acceptance.sh --self-test-only
scripts/run-user-facing-acceptance.sh /tmp/acceptance.json core
scripts/run-user-facing-acceptance.sh /tmp/acceptance.json full
```

`core` runs the three actual local `ccc-agent run` flows. `full` also requires
direct SSH and operator-driven observed-client cells for all three agents.
Lower-level documented protocol smokes are reported separately and do not count
as Desktop/WebUI evidence. These tests make real model calls and filesystem
changes below a dedicated test root, so they never run from ordinary `unittest discover`. See
[User-facing acceptance testing](user-facing-acceptance-testing.md) for the
manifest, pass evidence, remote-driver contract, and manual desktop fallback.

Do not claim FUSE runtime validation passed unless a real BranchFS mount and
bwrap run succeeded on a host with working `/dev/fuse`/sidecar support.

## Runtime validation checklist

On a FUSE-capable host:

```bash
# Confirm prerequisites.
command -v branchfs
command -v bwrap
test -e /dev/fuse

# Confirm ccc-agent configuration.
ccc-agent --version
ccc-agent setup --user   # or inspect system config
ccc-agent list

# End-to-end protected run.
mkdir -p /tmp/ccc-agent-smoke/project
cd /tmp/ccc-agent-smoke/project
ccc-agent run --policy workspace-auto -- bash -lc 'echo ok > result.txt'
cat result.txt

# Out-of-scope/pending-review behavior.
ccc-agent run --workspace "$PWD" -- bash -lc 'echo nope > ../outside.txt'
ccc-agent list
```

Deployment-specific validation should also prove:

- real underlays and BranchFS stores are not visible inside the sandbox;
- `~/.codex`, `~/.claude`, and `~/.hermes` behavior matches the chosen
  `protect_agent_state` setting;
- Codex/Claude/Hermes hooks fire only for contained runs;
- hook failure degrades to process-exit review;
- `ccc-agent review`, `commit`, `abort`, `resume`, `thaw`, and `cleanup` behave
  as expected on preserved sessions.

## Packaging checks

```bash
python3 -m pip install --user build
python3 -m build
python3 -m pip install --force-reinstall --user dist/ccc_agent-*.whl
ccc-agent --help
```

Check that package data includes:

- shell hooks under `ccc_agent/assets/hooks/`;
- shims under `ccc_agent/assets/shims/`;
- completions under `ccc_agent/assets/completions/`;
- plugin directories and `SKILL.md` files under `ccc_agent/assets/plugins/`.

## Documentation checks

After changing docs:

1. Run `./bin/ccc-agent --help` and relevant subcommand help to verify CLI text.
2. Check relative links in `README.md` and `docs/*.md`.
3. Keep the top-level README short and task-oriented.
4. Put implementation rationale in `architecture.md` or `design-decisions.md`,
   not in the README.

## Contribution rules

- Keep the trusted supervisor stdlib-only.
- Do not put commit authority in agent plugins, hooks, or shims.
- Do not expose BranchFS stores, daemon sockets, or control files inside the
  agent-visible mount.
- Keep the FUSE sidecar policy-free.
- Add/adjust tests for policy, path aliasing, review artifact lifecycle, sandbox
  argv construction, and resume/recovery behavior when behavior changes.
- Preserve production-safe defaults: no automatic broad shims or restarts unless
  explicitly enabled by deployment setup.
