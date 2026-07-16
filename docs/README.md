# ccc-agent documentation

This directory is the detailed documentation for `ccc-agent`: the user workflow,
installation, configuration, runtime architecture, design rationale, and
development/validation procedures.

## 1. User-facing documentation

- [User guide](user-guide.md) — how to run protected sessions, review results,
  resume work, and clean old sessions.
- [Agent integration](agent-integration.md) — how Codex, Claude Code, Hermes, and
  generic commands are handled; plugin and turn-boundary behavior.
- [Path policy and secret handling](policy.md) — auto-commit rules, manual review,
  deny patterns, hide paths, ignored runtime noise, and conflict handling.

## 2. Installation, build, and configuration

- [Installation and build](installation.md) — managed image integration, system
  and user installs, source/wheel builds, shell completion, and shims.
- [Dependencies](dependencies.md) — runtime, build, test, and optional platform
  dependencies.
- [Configuration](configuration.md) — `config.json` search order, protected roots,
  sandbox options, agent state, credentials, plugins, and policy keys.

## 3. Architecture and design rationale

- [Architecture](architecture.md) — trust split, lifecycle, sandbox layout,
  BranchFS/FUSE plumbing, review artifacts, and limits.
- [Trusted MCP commit protocol](trusted-mcp-commit-protocol.md) — exact-client
  registration, process/connection pinning, transport hardening, human
  elicitation, supervisor commit signaling, spoofing defenses, and fail-closed
  behavior.
- [Trusted workspace-scope protocol](trusted-workspace-scope-protocol.md) — why
  workspace scope is commit authority, hook proposal limits, Claude MCP Roots,
  Codex app-server observation, Hermes in-process signaling, replacement and
  cleanup rules, and spoofing defenses.
- [Per-logical-session delta routing](session-delta-routing.md) — optional nested
  BranchFS provenance, Codex bwrap interception, durable recovery, reconciliation,
  safe apply, failure behavior, and limitations.
- [Design decisions](design-decisions.md) — why BranchFS, why rootless bwrap, why
  process-exit finalization remains authoritative, why hooks are best-effort, and
  other selected tradeoffs.

## 4. Development and validation

- [Development](development.md) — repository layout, test commands, static checks,
  runtime/FUSE validation, packaging checks, and contribution rules.

## Documentation model

The top-level `README.md` is intentionally short and user-oriented. Detailed
material belongs here:

- usage and workflows under user-facing docs;
- operational setup under installation/configuration/dependencies;
- implementation internals under architecture/design decisions;
- validation and contributor workflow under development.

When changing behavior, update the page closest to the user-visible effect and
then update cross-links from this index if needed.
