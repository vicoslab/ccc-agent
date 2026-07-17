# User-facing acceptance testing

This suite certifies the behavior a user actually sees when running Codex,
Claude Code, or Hermes through `ccc-agent`. It complements unit tests; it does
not replace them.

The deterministic platform layer requires a capable deployed target but no model
call or vendor authentication. The separate model/plugin layer is opt-in because
it starts interactive agents, uses authenticated clients, and incurs real model
calls. Both layers require a dedicated writable test root.

Run the harness on the CCC node/container that owns the tested `ccc-agent`
state, BranchFS mounts, and server process. An SSH client may connect back to
that target, but the verifier must read that node's configured `state_dir` and
local session lifecycle. Shared NFS visibility does not make live FUSE or
session state cluster-global.

## Certification layers

The acceptance tooling has two deliberately separate layers:

1. **Platform acceptance** is deterministic and performs no model calls. It runs
   the deployed executable against real BranchFS/FUSE and bwrap, exercises
   foreground `run`, dedicated `serve`, the generic pending-review boundary,
   both accept and abort endings, outer workspace turn decisions plus
   conservative shared/unattributed routing fallback, protocol-clean server
   output, installed plugin/MCP metadata, configured client-hardening library,
   package assets, and mount/socket cleanup. The required `codex_command` drives
   a **documented stdio app-server protocol smoke**, sourced from OpenAI's public
   [`codex-rs/app-server/README.md`](https://github.com/openai/codex/blob/315195492c80fdade38e917c18f9584efd599304/codex-rs/app-server/README.md#protocol):
   it sends `initialize`, performs a global `mcpServerStatus/list`, starts an
   ephemeral thread, calls non-destructive `ccc_status`, and repeats
   thread-scoped inventory. It requires all six CCC tools. This proves CCC works
   with the documented app-server stdio API; it **does not claim that Codex
   Desktop launches this command**. When `claude_command` is set, the harness
   runs the directly installed Claude Code CLI's `claude mcp list` and requires
   `plugin:ccc:ccc` to connect. This is a Claude Code CLI check, not a Claude
   Desktop implementation claim. Both probes reject registration failures
   without making a model call. A wheel reinstall alone does not
   refresh Claude's separately materialized read-only seed; system deployment
   must rematerialize the seed from the installed package before this check can
   pass.
2. **Model/plugin acceptance** is the existing `core`/`full` matrix below. It
   requires authenticated vendor clients and proves native plugin behavior and
   actual user interaction.

Run platform acceptance first after every install or upgrade. For release or
deployment certification, use the deploy-first wrapper so the tested executable
is proven to come from the current working tree:

```bash
cp tests/user_facing_acceptance/platform.example.json \
   /tmp/ccc-agent-platform.json
scripts/deploy-server-acceptance.sh \
  --ssh USER@SERVER --port PORT --identity /path/to/key \
  --container CONTAINER --container-user UID:GID --home /home/USER \
  --manifest /tmp/ccc-agent-platform.json
```

The repository, generated wheel, and manifest must be visible at the same
absolute paths inside the target container. The wrapper builds one wheel,
records its SHA-256 and the working-tree patch SHA-256, installs it with the
explicit PEP 668 override required by the dedicated Ubuntu container,
rematerializes `/opt/claude-seed`, runs platform acceptance as the target user,
and retains deployment plus protocol evidence below
`.artifacts/server-acceptance/`.

For development against an already deployed executable, the non-deploying form
remains available:

```bash
scripts/run-user-facing-acceptance.sh \
   --platform /tmp/ccc-agent-platform.json
```

A platform pass proves the documented Codex stdio protocol smoke, CCC MCP
initialization/tool inventory and a thread-scoped `ccc_status` call, plus the core
BranchFS lifecycle on that exact target. It is **not** a Codex Desktop, Claude
Desktop, or Hermes WebUI pass. Those product/UI claims require the observed
client layer below. Unavailable clients must be reported explicitly rather than
replaced by inferred server commands.

## Model/plugin certification boundary

A full pass covers this matrix:

| Agent | Direct local CLI | Direct SSH CLI | Human-observed official remote client |
|---|---:|---:|---:|
| Codex | actual `ccc-agent run ... -- codex` TTY | actual `ssh ... codex` TTY routed by CCC | Codex Desktop operated through its own UI |
| Claude Code | actual `ccc-agent run ... -- claude` TTY | actual `ssh ... claude` TTY routed by CCC | Claude Desktop/remote client operated through its own UI |
| Hermes | actual `ccc-agent run ... -- hermes` TTY | actual `ssh ... hermes` TTY routed by CCC | Hermes WebUI/gateway operated through its own UI/API |

`core` runs the three direct local CLI cells. `full` requires all nine cells.
The local cells are schema-checked to invoke the named client executable directly
after `ccc-agent run ... --`; the SSH cells must invoke the named client in the
remote shell. The harness drives their real TTYs, pastes the same user prompts a
human would, captures the visible responses, and verifies server-side files,
events, MCP/skill behavior, review UI, and cleanup rather than trusting model
prose.

The third column is intentionally called **observed remote client**, not
"desktop-equivalent." A qualifying run must use an external/manual driver,
declare `user_flow: observed-client`, provide direct-observation or exact
official-source provenance, and include operator instructions. The completed
result must contain direct-observation evidence with product, version,
timestamp, and screenshot/log artifact. A protocol smoke, guessed launch
command, or process that merely resembles a desktop backend cannot satisfy this
column.

A remote-client test is invalid if it merely runs `ccc-agent serve AGENT --
true`, a fake shell server, `codex app-server --stdio`, or another direct command
that bypasses the product UI. Protocol smokes remain useful lower-level checks,
but are reported separately.

## Direct CLI user simulation

The local and SSH CLI cells are automated real-user simulations, not protocol
stubs. Each agent declares an absolute `client_executable`; preflight requires it
to exist and be executable so PATH shims cannot silently recurse through
`ccc-agent`. Their manifest entries must declare `user_flow: direct-cli`. Local
entries must place that exact executable immediately after the `ccc-agent run ...
--` separator; SSH entries must invoke that same named executable in the remote
shell. For example:

```json
{
  "driver": "tmux",
  "user_flow": "direct-cli",
  "command": [
    "{ccc_agent}", "run", "--config", "{ccc_agent_config}",
    "--workspace", "{workspace}", "--agent", "codex", "--",
    "/home/domen/conda/envs/codex/bin/codex",
    "--dangerously-bypass-approvals-and-sandbox"
  ]
}
```

The harness starts the real CLI in a TTY, pastes the generated first prompt as a
user, waits for the real CCC review question, validates the filesystem/session,
pastes the second user decision, verifies CCC operations, sends the product's
normal exit command, and exercises the host review UI. The same scenario and
objective assertions are used for Codex, Claude Code, and Hermes. The dangerous
client flags disable the vendor's *inner* approval/sandbox only so CCC is the
boundary under test; they do not bypass CCC's outer BranchFS/bwrap containment.

## Running an observed Desktop/WebUI cell

No Desktop application is installed on the deployment server, so this gate is
operator-driven. Configure the `remote-server` entry in
`acceptance.example.json` with the client product/version, observation time,
artifact destination, SSH target, and the bundled driver:

```json
{
  "driver": "external",
  "user_flow": "observed-client",
  "command": [
    "python", "scripts/manual-observed-client-driver.py", "{scenario_file}"
  ],
  "evidence": {
    "basis": "direct-observation",
    "client_product": "Codex Desktop",
    "client_version": "<version shown by the app>",
    "observed_at": "<UTC timestamp>",
    "artifact": "<screenshot or screen-recording path>"
  },
  "operator_instructions": [
    "Open Codex Desktop; do not launch a substitute CLI or app-server.",
    "Use the product UI to connect to <SSH target> through the CCC router.",
    "Open {workspace}, capture the UI, and paste prompts from the driver."
  ]
}
```

Then run only the required observed cell (recommended while an operator is
present):

```bash
scripts/run-user-facing-acceptance.sh \
  --cell codex remote-server /path/to/acceptance.json
```

Use `claude` or `hermes` in place of `codex` for those products. Run the entire
nine-cell matrix separately when all products/operators are available:

```bash
scripts/run-user-facing-acceptance.sh \
  /path/to/acceptance.json full
```

For each observed-client cell, the driver prints exact instructions and two
paste-ready prompts. The operator must:

1. open the named official client and record its displayed version;
2. use its normal SSH/remote UI—not a hand-written `serve`/app-server command;
3. open the exact workspace printed by the driver;
4. capture a screenshot showing client, target, and workspace;
5. paste the first prompt, wait for the visible CCC commit/discard/keep question,
   return to the terminal, and paste the visible response there;
6. after server-side verification releases the driver, paste the second prompt,
   wait for CCC status, and paste that response back;
7. close the client and record observed hooks, skills, and artifact paths.

The harness independently checks the resulting CCC session, actual underlay,
plugin events, remembered decisions, review state, and cleanup. Operator answers
and screenshots establish which UI/product was used; they do not replace those
objective server-side assertions.

## What the automated flow proves

Every agent/transport cell gets a unique directory below the manifest's
`test_root`. The harness starts the real interactive agent and performs at least
two model turns.

### First agent turn

The agent is instructed to use real file tools and:

1. prove `CCC_AGENT_SESSION` is non-empty;
2. create a new workspace file and a nested workspace file;
3. modify an inherited workspace file;
4. delete an inherited workspace file;
5. create three files outside the workspace but inside a protected root;
6. create a `.env.*` file inside the workspace to prove deny policy overrides
   workspace auto-commit;
7. stop without manually calling `ccc-agent turn-*` operations.

While the agent stays open, the harness requires:

- native plugin events `turn-workspace-add`, `turn-default-kept`, and
  `turn-kept-review-requested` in the durable session;
- new/modified/deleted workspace changes already reflected in the real
  underlay;
- all out-of-workspace and deny-matched files absent from the real underlay;
- workspace paths marked `committed` and unsafe paths marked `kept` in
  `policy.turn_path_decisions`;
- a visible agent question containing **commit**, **discard**, and **keep**;
- `ccc-agent list` contains the session;
- `ccc-agent show` returns the right JSON record;
- `ccc-agent status` sees every live BranchFS delta, including changes already
  copied through at a turn boundary;
- `ccc-agent diff` shows every pending unsafe path.

This distinction is important: plugin asset presence is not enough. The turn
events, mid-session underlay state, and visible question prove the plugin was
loaded and used.

### Second agent turn

The user tells the agent, through its normal UI/protocol, to use the CCC plugin
operation/skill to:

- commit one remembered outside path;
- discard a second outside path;
- keep a third outside path;
- discard the deny-matched workspace path;
- show compact CCC status.

The harness requires durable `turn-resolved-commit`,
`turn-resolved-discard`, and `turn-resolved-keep` events. It also checks the
real underlay and remembered decisions, not only the model's prose.

### Exit and host-side review

After a clean agent exit, the remaining kept path must leave the session in
`pending-review`. The harness opens `ccc-agent review` under a real TTY and
requires all four choices:

```text
[c] commit all changes
[s] selective accept
[d] discard all changes
[l] keep for later review
```

It first chooses `later` and proves that the session remains pending. The
manifest's `final_review_action` then selects one of two real endings:

- `accept` runs scripted `review --accept`, proves the last kept path reaches
  the underlay, and requires durable state `committed`;
- `abort` runs scripted `ccc-agent abort`, proves the kept path never reaches
  the underlay, and requires durable state `aborted`.

The example manifest uses `abort` for all three local CLI cells and `accept`
for all SSH/server cells. Full certification therefore exercises both outcomes
for Codex, Claude, and Hermes without another model run.

Artifacts include the scenario, TTY transcript, `list/show/status/diff`
outputs, external-driver log/result, final session JSON, and a failure summary
when applicable.

## Safety requirements

The suite refuses to run unless all of these are true:

- `CCC_AGENT_ACCEPTANCE=1` was set by the wrapper;
- the harness is running outside containment (`CCC_AGENT_SESSION` is unset), so
  nested-session reuse cannot collapse the acceptance cells into another run;
- an explicit manifest was provided;
- `test_root` is absolute, is not `/`, `/storage`, `/storage/user`, or `$HOME`,
  and its basename contains `ccc-agent-acceptance`;
- the path is below a configured protected root;
- the backend is `branchfs` and confinement is `bwrap`;
- `branchfs_bin`, `bwrap_bin`, and the configured `ccc-agent` are executable;
- `/dev/fuse` exists;
- `tmux` and util-linux `script` are available for real-TTY automation;
- every required launch command renders without unknown placeholders, contains no
  `REPLACE_WITH_` sentinel, and starts with an available executable;
- `agent_plugins.codex`, `.claude`, and `.hermes` all point to existing trusted
  assets.

The last rule is intentionally stronger than process-exit safety. Current
setup-generated configuration may omit Hermes native plugin injection. Such a
deployment must **fail plugin certification** until an explicit Hermes plugin
configuration is installed; process-exit review alone does not satisfy a test
that claims proper plugin loading and use.

Use a disposable acceptance tree. Do not point this suite at a real project or
at a broad user directory. The harness never reuses an existing scenario path.

## Preparing the manifest

Copy the example and replace every `REPLACE_WITH_*` value:

```bash
cp tests/user_facing_acceptance/acceptance.example.json \
   /tmp/ccc-agent-acceptance.json
$EDITOR /tmp/ccc-agent-acceptance.json
```

Important fields:

| Field | Meaning |
|---|---|
| `ccc_agent` | Exact deployed executable being certified, not the source-tree wrapper unless that is intentional. |
| `ccc_agent_config` | Exact deployment config used by the target container/node. |
| `test_root` | Dedicated agent-visible path below a protected root. |
| `artifacts_dir` | Durable trusted-side output directory. Keep it outside agent write authority when possible. |
| `driver: tmux` | Interactive CLI driven through a real TTY. |
| `driver: external` | Official desktop/server protocol driver; required for `remote-server`. |
| `command` | Trusted argv template. Normal placeholders are one argv value; `{name_shell}` is shell-quoted for an intentional remote shell snippet. Literal braces in vendor shell syntax must be doubled, for example `${{HOME}}`. |
| `expected_agent_kind` | `codex`, `claude`, or `hermes` locally; normally `AGENT-remote` through SSH/server mode. |
| `final_review_action` | `accept` (default) or `abort` after the interactive `later` check. |

The harness provides placeholders for every scenario field, including:

```text
{workspace}              {workspace_shell}
{outside_commit}         {outside_discard}         {outside_keep}
{deny_file}              {initial_prompt}          {decision_prompt}
{scenario_file}          {ccc_agent}               {ccc_agent_config}
```

The example manifest passes each agent's explicit test-only autonomous flag so
the generic tmux driver tests CCC review rather than stalling at the vendor's
separate shell/file approval UI. `ccc-agent` itself does not inject these flags.
Remove or replace them if the deployment also needs to certify the agent's
native approval prompts; in that case, use a version-specific TTY driver that
answers those prompts and retain the same CCC assertions.

Run the harmless harness checks first:

```bash
scripts/run-user-facing-acceptance.sh --self-test-only
```

Run the local matrix:

```bash
scripts/run-user-facing-acceptance.sh \
  /tmp/ccc-agent-acceptance.json core
```

Run full certification:

```bash
scripts/run-user-facing-acceptance.sh \
  /tmp/ccc-agent-acceptance.json full
```

The ordinary unit suite imports and collects the real tests but reports three
skips. It never starts a model client without the explicit wrapper invocation.

## Native plugin preflight

Before spending model calls, the development/deployment agent should record the
following evidence.

### Codex

1. Inspect deployed Codex config and prove `ccc@ccc-agent` is enabled/trusted.
2. Prove the configured read-only plugin cache source exists at the path in
   `agent_plugins.codex.src`.
3. Through the real Codex runtime, list bundled hooks and skills when the
   installed Codex version exposes these protocol methods. Require every CCC
   lifecycle hook and at least the `ccc-commit` and status/operation skills.
4. Do not infer success from cache files alone. Require the automated turn
   events and mid-session commit/keep behavior.

### Claude Code

1. Confirm `enabledPlugins["ccc@ccc-agent"] = true` in the deployed managed or
   user settings without duplicate settings-level hooks.
2. Confirm `agent_plugins.claude.src` is a complete seed containing marketplace,
   installed-plugin, cache, hook, and skill data.
3. Run Claude's own strict validators against both the marketplace and plugin
   directories:

   ```bash
   claude plugin validate --strict <seed>/marketplaces/ccc-agent
   claude plugin validate --strict \
     <seed>/marketplaces/ccc-agent/claude-ccc-containment
   ```

4. On a pristine test home, record startup debug evidence that the plugin,
   skills, and hooks loaded on the **first** invocation.
5. For remote Claude, prove SessionStart restored the narrow CCC handoff into
   the inner `ccd-cli` environment and that Stop/SessionEnd behavior occurred.

### Hermes

1. Confirm the deployment explicitly loads the packaged
   `hermes-ccc-containment` plugin. Current default setup does not do this.
2. Record `hermes plugins list` (or the version's authoritative plugin listing)
   and prove the plugin registered `pre_llm_call`, `transform_llm_output`,
   `post_llm_call`, and `on_session_end`.
3. Prove the `ccc-commit` context was injected on the first contained turn and
   the turn boundary produced real supervisor events.
4. For a gateway/API-server flow, use an API/WebUI-originated session with an
   authoritative workspace value. A messaging-platform prose tag must not be
   treated as workspace authority.

## Official desktop/server driver contract

Desktop automation is vendor- and version-specific. The suite therefore uses a
small external driver per agent instead of pretending that a generic terminal
command exercises the desktop path. The driver may be maintained with the
deployment or client integration and is passed the generated `scenario.json`.

The harness exports:

```text
CCC_AGENT_ACCEPTANCE_SCENARIO
CCC_AGENT_ACCEPTANCE_RESULT
CCC_AGENT_ACCEPTANCE_CONTINUE
CCC_AGENT_ACCEPTANCE_INITIAL_PROMPT
CCC_AGENT_ACCEPTANCE_DECISION_PROMPT
```

The driver must:

1. start/connect through SSH so the packaged login-shell router sees the real
   vendor bootstrap command;
2. use the official client protocol, not shell injection into the contained
   server;
3. prove the protocol stream contains no ccc-agent banner, finish line, review
   dump, or prompt;
4. query or otherwise authoritatively enumerate loaded hooks and skills;
5. start an inner agent session rooted at `scenario.workspace`;
6. submit `initial_prompt`, retain the response, and keep the server/session
   open;
7. atomically write a partial result with `"phase": "first-turn-ready"` to
   `driver_result_file`, then wait without sending another user turn until
   `driver_continue_file` exists; the harness uses this pause to verify live
   status, durable events, and the real underlay itself;
8. after release, submit `decision_prompt` as the user, retain the response, and
   wait for the CCC operations to finish;
9. close the inner client and stop the control server cleanly;
10. atomically replace the result with the complete JSON and
    `"phase": "complete"`, then exit zero.

Required result shape:

```json
{
  "phase": "complete",
  "used_official_client": true,
  "server_started_through_ssh_router": true,
  "protocol_clean": true,
  "plugin_loaded": true,
  "plugin_used": true,
  "workspace_registered": true,
  "asked_user": true,
  "status_used": true,
  "plugin_inventory": {
    "hooks": ["authoritative hook names or IDs"],
    "skills": ["ccc-commit", "status"]
  },
  "first_response": "agent response containing commit/discard/keep question",
  "decision_response": "agent response after CCC operation and status",
  "transcript": "optional additional protocol transcript with secrets removed"
}
```

The partial `first-turn-ready` object must contain at least `phase`,
`first_response`, and any transcript needed to prove the visible question. The
complete object must contain every field above. Write both phases through a
temporary file in the same directory followed by `os.replace` (or an equivalent
atomic rename); the harness deliberately rejects a driver that exits or advances
to `complete` before the live first-turn checks release it.

A driver must not set booleans optimistically. Each must be backed by the
protocol transcript, plugin debug record, or durable CCC session events. Never
place tokens, OAuth credentials, session-control secrets, or raw environment
dumps in the result.

### Codex remote driver

Use the installed client path that launches Codex app-server/remote state over
SSH. The router should classify `codex app-server ...`, recognized Codex remote
executables, and nested payload wrappers as adaptive `serve codex` launches.
When supported by that Codex version, query `hooks/list` and `skills/list`, then
run at least two turns on one thread. Verify a workspace registration event and
distinct turn-resolution events before shutting down the server.

### Claude remote driver

Use the desktop/official remote command that installs/starts the
`~/.claude/remote/srv/.../server` and communicates through the corresponding
`ccd-cli`; do not replace it with a direct `claude -p` call. Capture the exact
SSH command from a real client session when the private command varies by
version. Verify plugin startup, SessionStart workspace registration, Stop
handling, SessionEnd cleanup, and session/control handoff restoration.

### Hermes remote driver

Use `hermes gateway run` with the configured API Server/WebUI adapter (or the
version's official remote client) through the SSH route. Submit the same two
turns through the API/WebUI session, not directly to a shell. Verify the Hermes
plugin inventory, trusted workspace registration, per-turn events, and
`on_session_end` cleanup. The Hermes CLI equivalent for a local one-shot is
`hermes chat -q`, but that is **not** a substitute for gateway/API-server
coverage.

## Manual desktop fallback

If a desktop/server protocol cannot be automated on the installed version, the
full test remains mandatory as a deployment-agent instruction rather than being
silently skipped.

For each affected agent:

1. Start transcript capture and note host, client version, server version,
   deployed `ccc-agent --version`, and config hash. Do not record credentials.
2. Snapshot `ccc-agent list` and the session bundle IDs.
3. Create the generated scenario paths and inherited modify/delete seeds.
4. Initiate a real desktop remote session over SSH and confirm the SSH router
   creates one visible `AGENT-remote` session, not `AGENT-remote-bridge`.
5. Query plugin hooks/skills through the official protocol or capture the
   authoritative plugin startup log.
6. Submit the generated `initial_prompt` unchanged.
7. Before answering the agent's commit/discard/keep question, leave the desktop
   session open and run trusted-side `list`, `show`, `status`, and `diff`.
8. Verify workspace create/modify/delete reached the real underlay and every
   outside/deny path did not.
9. Submit the generated `decision_prompt` unchanged through the desktop UI.
10. Verify one path committed, one discarded, one remained kept, the deny path
    was discarded, and all three `turn-resolved-*` events exist.
11. End only the inner session; verify its workspace ownership is removed
    without removing concurrent inner-session workspaces.
12. Stop the outer server and verify final `pending-review`, then exercise
    interactive `later` and the configured scripted accept/abort ending.
13. Check that no FUSE mount, bwrap/PID-1 process, server socket, handoff file, or
    hidden bridge session leaked.
14. Write the external-driver result JSON from observed evidence. A deployment
    agent may then rerun the automated verifier against that driver.

Screenshots alone are insufficient. Preserve machine-readable session JSON,
status/diff output, protocol/plugin inventory, and underlay checks.

## Additional release scenarios

The main nine-cell matrix covers the requested user workflow. Before production
deployment, also run or document these focused scenarios:

| Area | Required check |
|---|---|
| Process-exit fallback | Disable/misconfigure each plugin deliberately. Safe workspace-only work may finalize; any out-of-scope or deny-matched work must remain pending and must never be committed because a hook failed. |
| One-shot CLI | Run `codex exec`, `claude -p`, and `hermes chat -q`; require correct process-exit state and no interactive deadlock. |
| Direct control | Run each client once without `ccc-agent`; require no new CCC session and no active CCC hook/control behavior when `CCC_AGENT_SESSION` is absent. |
| Agent startup failure | Missing executable, authentication failure, model error, and nonzero agent exit preserve reviewable deltas and useful error events. |
| Workspace aliases | Access the same project through `/home/$USER` and `/storage/user/...`; require one branch/one byte identity and no duplicate commit. |
| Additional scope | A repeated `--scope` path auto-commits; a sibling not granted by workspace/scope remains kept. |
| Manual/read-only/throwaway policy | Manual never auto-commits; read-only-review preserves changes; throwaway aborts without underlay writes. |
| Sensitive paths | `.ssh`, credentials, shell startup files, `.git/hooks`, and configured hide paths are hidden or pending; no secret content appears in artifacts. Use synthetic test secrets only. |
| Agent state | Default Codex/Claude/Hermes runtime state remains direct/shared and out of review; `--protect-agent-state` makes it reviewable without breaking login/runtime startup. |
| Resume/recovery | Resume pending, aborted, and explicitly allowed failed sessions; thaw clears stale generated review data; stale mounts are handled safely. |
| Selective review | Commit a file, directory subtree, and deletion; discard the rest; verify exact underlay and tombstone results. |
| Batch controls | `commit`/`abort` multiple session IDs, invalid/ambiguous prefixes, and mixed success/failure reporting. |
| Concurrency | Two local sessions and two remote inner sessions use independent branch/session IDs; one inner end does not revoke the other's workspace. |
| Server lifecycle | True handoff stays visible as `AGENT-remote`; a foreground/rejected bridge is hidden, aborted, removed, and never commits. |
| Router bypasses | Disabled shims, explicit bypass, nested containment, and unrelated shell mentions pass through; direct agents are foreground and recognized server forms adaptive. |
| Protocol cleanliness | No ccc-agent human text on JSONL/RPC/stdout/stderr protocol channels. |
| Cleanup | Closed records age out only in the intended scope; pending/failed records survive conservative cleanup; no mount/process/socket leaks remain. |
| Packaging/upgrade | Build/install the wheel, inspect plugin/hook/skill/router assets, rerun setup, and repeat a pristine first-start acceptance flow. |

## Pass/fail rules

A cell passes only when all behavior is backed by real artifacts. In particular:

- model prose is not proof of a file operation;
- plugin files on disk are not proof that hooks loaded;
- a `turn-*` event without underlay verification is not proof of commit safety;
- final process-exit review is not proof of per-turn plugin behavior;
- direct SSH CLI is not proof of desktop/server behavior;
- a fake server is not proof of protocol cleanliness or workspace registration;
- `Operation not permitted` from a real FUSE mount is a deployment failure, not
  a passing static test.

Keep failed artifacts and report the exact agent, transport, session ID, first
missing invariant, and relevant command exit status. Do not weaken assertions to
make an incomplete deployment appear certified.
