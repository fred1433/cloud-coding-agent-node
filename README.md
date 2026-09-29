# codenode

A coding step for a workflow engine. It takes a task and input files, lets a model write and run code
in a throwaway container, and hands back declared output files, a diff, an ordered event stream and a
cost ledger. The agent loop and the API key stay on the controller; only tool execution happens in the
sandbox.

The example task (`examples/line3/`) turns a messy synthetic CNC sensor export into a normalized table,
an `alerts.json` and a plot. Its acceptance checks run after the sandbox is gone, against values known
from the fixture generator; the agent never sees them and cannot edit them.

## Run the tests on a clean clone

Needs Docker and Python 3.12. No API key.

```
make test                  # unit tests, acceptance checks, node contract, attack suite
make bench                 # attack suite only, writes bench/results-runc.json
make test RUNTIME=runsc    # same suite under gVisor, if runsc is installed as a Docker runtime
```

To run the example task against the real API: `ANTHROPIC_API_KEY=... python examples/line3/run_demo.py`.
`CODENODE_MODEL` overrides the model (default `claude-sonnet-5`).

## What is in here

| Path | What it is |
|---|---|
| `codenode/node.py` | The node: tenant policy check, agent loop, reservation-based budget, terminal states, output manifest |
| `codenode/service.py` | Start / follow / cancel for an engine: idempotent run IDs, reconnect by `seq`, tenant checks |
| `codenode/sandbox.py` | One hardened container per run, export of declared outputs, janitor |
| `sandbox/` | The image and the in-container helpers (file operations, command runner, egress proxy) |
| `codenode/tools.py` | `bash_20250124` and `text_editor_20250728` (Anthropic-defined) plus `grep`, `glob`, `fetch_url` |
| `codenode/webfetch.py` | Host-side fetch for the optional fetch profile |
| `codenode/attacks.py` | Scripted tool requests against the real runtime, each with its observed cause |
| `deploy/k8s/` | Minimal pod-per-run manifests and the kind check run in CI |
| `runs/` | The recorded run (JSONL, unedited), its exported outputs and acceptance result |
| `bench/` | Attack-suite results: local Docker, CI runc, CI gVisor, CI kind |

## Node contract

- **Start**: the engine passes an authenticated `TenantContext` (tenant id and policy from its own records,
  never from the task or the agent), a run ID, the node config (validated against
  `codenode/node.schema.json`), the task and files. A config above the tenant's ceilings is refused as
  `policy_blocked`; callers cannot pass container options.
- **Progress**: ordered events with `seq`; `events(after_seq)` resumes without duplicates. Controller
  status and sandbox output are separate (`untrusted_output`).
- **Finish**: one of `succeeded`, `failed`, `cancelled`, `timed_out`, `policy_blocked`; an output
  manifest (path, size, sha256) of the declared outputs actually exported.
- **Retry and cancel**: starting an existing run ID returns the existing run; cancel destroys the
  sandbox and publishes nothing.
- **Tenants**: run IDs are per tenant, so another tenant's run gets the same answers as a missing one,
  and nobody can squat an ID another tenant will use.

## Execution profiles

- **Default (tested)**: `--network none`, read-only root, all capabilities dropped, `no-new-privileges`,
  non-root UID derived from the tenant (hashed, so collisions are possible; allocate from a registry in
  production), private IPC, pids, memory and CPU limits, size-capped tmpfs for `/workspace`,
  `/tmp` (noexec) and `$HOME` (noexec). Dependencies are in the image before tenant data arrives. The
  in-container helpers are root-owned files on the read-only image, run as `/usr/local/bin/python3 -I -S`
  so nothing the agent writes (a `usercustomize.py`, a `.pth`, a fake `python3`) can change a tool result.
- **Egress proxy (tested)**: the sandbox sits on an internal network whose only route is a per-run
  CONNECT proxy applying the tenant's host allowlist. It sees host names, not content.
- **Fetch (tested with stubs and a local receiver)**: `fetch_url` runs on the controller with a
  connection policy (public addresses only, IPv4 and IPv6, redirects re-checked, connection pinned to
  the checked address) and opens only URLs that appeared in the task or in fetched pages.
- **gVisor**: `runtime: runsc` runs the same container under gVisor's application kernel, which narrows
  the host kernel interface the sandbox can reach. It is not a VM. CI runs the suite under it
  (`bench/ci-runsc.json`): 18 of 20 cases hold; under the fork burst and the 3 GB allocation the whole
  sandbox exits instead of refusing the one process, the run ends `failed` and nothing is left behind.
  An earlier gVisor run also caught the file tool following a symlinked directory; the tool now checks
  each path component with lstat before opening and fstat after.

## Cost figures

API cost estimate from recorded usage and the dated price table (`codenode/pricing.json`). Sandbox and
infrastructure costs are excluded. Before each request the node reserves an upper bound (request size
in bytes as tokens, at the cache-write rate, plus the full output allowance) and stops if it does not fit.

## What this does not prove

- Anything about the host kernel under runc: the container shares it. gVisor narrows that surface; a
  microVM (Firecracker with its jailer) changes it again, with its own operations work. Neither is a
  proof of no escape.
- Resistance of a model to prompt injection: the attack suite scripts the agent on purpose.
- The egress proxy does not terminate TLS, so an allowed host is a possible channel and domain fronting
  is not addressed. Choosing which listed link to fetch can leak a few bits per fetch.
- tmpfs content can be swapped by the host; it is removed with the container, not securely erased.
- Load, multi-node Kubernetes, or a production deployment. `deploy/k8s` is checked in a kind cluster
  only; gVisor is not installed in that cluster.
- An adapter for another model provider: the neutral tool schemas are an extension point, untested.

MIT license.
