# Installation and build

`ccc-agent` is a Python package with a single `ccc-agent` console script and
bundled shell/plugin assets. The Python package is deliberately dependency-free;
BranchFS, bubblewrap, and FUSE support are runtime components configured outside
pip.

## Managed image integration

Managed CCC images include integration for installing and wiring `ccc-agent` as
part of image/runtime setup. In that environment, users normally do not install
this repository manually; the image setup installs the package, writes the
runtime `config.json`, points it at the BranchFS binary and bwrap, and may enable
agent shims according to deployment policy.

This repository still documents manual installation for development, testing, and
non-image deployments.

## System install

A system install puts package assets under the system Python prefix so the bwrap
sandbox can expose them read-only from OS paths.

```bash
/usr/bin/python3 -m pip install --break-system-packages \
  "git+https://github.com/vicoslab/ccc-agent.git@master"

sudo ccc-agent setup --system \
  --branchfs-bin /usr/local/bin/branchfs \
  --bwrap-bin "$(command -v bwrap)" \
  --storage-root /storage \
  --branch-store /opt/branchfs_branches
```

Optional deployment-specific flags:

```bash
sudo ccc-agent setup --system \
  --container-name "$CONTAINER_NAME" \
  --state-dir /storage/user/.ccc-agent \
  --enable-shims
```

System mode writes `/etc/ccc-agent/config.json` by default. Keep it root-owned
and not writable by the agent user.

## User/development install

From a local checkout:

```bash
python3 -m pip install --user -e .
ccc-agent setup --user
```

User mode writes `~/.config/ccc-agent/config.json` by default and protects the
user's home as the default root. It is useful for local development and CLI tests,
but production-style deployments should prefer system-owned config/assets.

To choose a config path explicitly:

```bash
ccc-agent setup --user --config /path/to/config.json --state-dir /path/to/state
export CCC_AGENT_CONFIG=/path/to/config.json
```

## Building a wheel or source distribution

```bash
python3 -m pip install --user build
python3 -m build
python3 -m pip install --user dist/ccc_agent-*.whl
```

The package declares only `setuptools>=61` for builds and no runtime Python
dependencies.

## Shell completion

Package installation installs completion files into standard `share/...` paths:

```text
share/bash-completion/completions/ccc-agent
share/zsh/site-functions/_ccc-agent
share/fish/vendor_completions.d/ccc-agent.fish
```

With `pip install --user`, these are under the user base, usually
`~/.local/share/...`. Start a new shell after installation. For a one-off fallback:

```bash
ccc-agent completion bash
ccc-agent completion zsh
ccc-agent completion fish
```

Completions read the configured session store and complete session-id prefixes for
operator commands such as `show`, `status`, `diff`, `review`, `commit`, `abort`,
`thaw`, `finish`, and cleanup flags.

## Agent plugin setup

By default `ccc-agent setup` writes config entries for bundled native plugins:

- Codex plugin assets;
- Claude Code plugin assets;
- Hermes bundled plugin assets.

For a contained run, `ccc-agent run` bind-mounts the matching plugin read-only
into the sandbox and activates it for that invocation. Direct, uncontained
Claude/Hermes runs are not modified.

Codex currently needs a small managed block in `~/.codex/config.toml` so Codex
recognizes and trusts the `ccc-agent` plugin when the read-only plugin cache is
mounted inside a contained run. `ccc-agent setup` maintains only that marked
block. The plugin hook exits when `CCC_AGENT_SESSION` is absent, so ordinary Codex
runs do not gain commit authority or use BranchFS containment by accident.

Disable plugin entries with:

```bash
ccc-agent setup --system --no-agent-plugins
# or user mode:
ccc-agent setup --user --no-agent-plugins
```

Without plugins, process-exit finalization still works for every command.

## Transparent shims

Shims let users type the normal agent command names while launching through
`ccc-agent run`. The SSH shell router is stricter for remote app-server/server
commands: detected Codex/Claude remote commands are launched with `--serve` and
produce no router/supervisor banner on stdout/stderr, so the SSH stream remains
owned by the original agent protocol.

Simple system case:

```bash
sudo ccc-agent setup --system --enable-shims
command -v codex
command -v claude
```

Conda-compatible case: keep shims in a dedicated trusted directory and add conda
activation hooks so the shim directory stays before `$CONDA_PREFIX/bin`.

```bash
conda activate my-agent-env
ccc-agent setup --user --enable-shims \
  --link-dir "$HOME/.local/share/ccc-agent/shims" \
  --conda-activate-shims \
  --conda-prefix "$CONDA_PREFIX"

command -v codex   # should show the shim path, not $CONDA_PREFIX/bin/codex
```

Do not install a shim directly over the real binary inside the same conda
`bin/` directory. The shim must be able to skip itself and resolve the real agent
from the rest of `PATH`.

## Updating

```bash
python3 -m pip install --upgrade --user "git+https://github.com/vicoslab/ccc-agent.git@master"
ccc-agent setup --user
```

For system installs, rerun the system pip install and `ccc-agent setup --system`
with the same deployment options. Rerunning setup is idempotent for generated
config and managed plugin blocks.

## Uninstalling

User install:

```bash
python3 -m pip uninstall ccc-agent
rm -f ~/.config/ccc-agent/config.json
```

System install:

```bash
sudo /usr/bin/python3 -m pip uninstall ccc-agent
sudo rm -f /etc/ccc-agent/config.json
```

Do not remove `state_dir` blindly if it contains pending-review or failed
sessions. Inspect with `ccc-agent list` first, then commit/abort/cleanup as
needed.
