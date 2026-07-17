# Codex remote-client transport provenance — 2026-07-17

## Purpose

This note separates public app-server facts, directly observed remote-client
behavior, and unverified assumptions. Acceptance code may cite this note for a
topology smoke, but it may not use it to claim that Codex Desktop itself passed.
A Desktop pass requires a current operator-driven run and UI evidence.

## Public source

OpenAI Codex public repository commit:

- commit: `315195492c80fdade38e917c18f9584efd599304`
- source: <https://github.com/openai/codex/blob/315195492c80fdade38e917c18f9584efd599304/codex-rs/app-server/README.md#protocol>

That source documents:

- `codex app-server --stdio` / `--listen stdio://` as the default JSONL
  transport;
- `codex app-server --listen unix://PATH` as WebSocket over a Unix socket;
- `codex app-server proxy --sock PATH` as a raw stdin/stdout proxy carrying the
  WebSocket upgrade and frames.

It says app-server powers rich interfaces such as the VS Code extension. It does
**not**, at this revision, specify the exact Codex Desktop-to-SSH launch command.
Therefore stdio acceptance is classified as `documented-protocol-smoke`, not
Desktop acceptance.

## Direct server observation

While the user operated the Codex remote client against `domen-cuda10` on
2026-07-17, process/session inspection on the target showed two relevant paths:

```text
ccc-agent serve codex ... -- <real-codex> \
  -c features.code_mode_host=true app-server --listen unix://

ccc-agent serve codex ... -- <real-codex> app-server proxy
```

The long-lived app-server and proxy were in distinct outer CCC sessions. The
app-server spawned the plugin MCP child. This observation explained why testing
only a foreground CLI or a single stdio child missed environment recovery,
server-wrapper admission, and multiple read-only MCP connection behavior.

The observation establishes the Unix-socket/proxy **topology** for that observed
run. It does not establish every Desktop version's bootstrap flags, shell
wrapping, socket path, reconnect policy, or UI behavior. A future automated
proxy-topology smoke must use a private test socket and cite both this observation
and the public transport specification. It must still be reported separately
from a human-observed Desktop cell.

## Claude direct observation

A live Claude Desktop/remote connection was present on `domen-cuda10` during the
same investigation. Target-side process inspection directly showed this chain
(the socket token and per-run paths are intentionally omitted):

```text
ccc-agent serve claude -- /bin/bash -c \
  '<home>/.claude/remote/srv/<revision>/server --stop ... && \
   <home>/.claude/remote/srv/<revision>/server --serve --socket ... --token-file ...'

<home>/.claude/remote/srv/<revision>/server --serve ...
  -> <home>/.claude/remote/ccd-cli/2.1.209 \
       --output-format stream-json --input-format stream-json ...
```

This is stronger than an inferred `claude` CLI command: it identifies the
observed Desktop remote server and versioned `ccd-cli` child topology. It led to
narrow recognition of `~/.claude/remote/ccd-cli/<version>` as a read-only remote
wrapper child and a bounded retry for the observed client-registration race.
The path, ancestry, launch-boundary, and PID/start-time checks remain required;
an arbitrary executable called `ccd-cli` is rejected.

This process observation still does **not** by itself certify the Desktop UI.
The operator-driven cell must verify the actual prompt, visible CCC question and
status, plugin inventory, filesystem effects, review result, and cleanup.

## Hermes

No equivalent Hermes WebUI remote-launch process observation was captured in
this session, and its public implementation did not provide an exact
Desktop-to-CCC SSH invocation contract. Do not invent one. Use
`scripts/manual-observed-client-driver.py` with the product UI and retain product
version, timestamp, screenshot/recording, visible responses, and server-side CCC
artifacts.
