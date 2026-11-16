# Deep research on Microsoft Foundry that sleeps, with Temporal

A research agent built with Microsoft Agent Framework runs inside a Temporal Workflow, in a
Microsoft Foundry hosted agent, on a Foundry model. The run waits for a person or a timer
with zero sandboxes. When the approval arrives or the timer fires, Temporal starts a Foundry
sandbox and the agent continues from the step it was on.

In our runs (East US 2, November 16, 2026) a wake-up from zero took 5 to 8.5 seconds,
including the first one after more than nine idle hours.

## Status

Read this before you build on it.

- **Foundry hosted agents** are generally available.
- **Temporal's plugin for Microsoft Agent Framework**
  ([`temporalio-microsoft-agent-framework`](https://pypi.org/project/temporalio-microsoft-agent-framework/))
  is a pre-release. Interfaces may change.
- **The piece that wakes Foundry is a prototype.** It is an `azure-foundry` compute provider
  for Temporal's worker controller, on an unmerged branch of
  [`temporalio/temporal-auto-scaled-workers`](https://github.com/temporalio/temporal-auto-scaled-workers).
  This repository builds a Temporal server from source with that branch linked in. The
  provider is not part of a Temporal release and is not available in Temporal Cloud. Treat it
  as a preview of where this is going.

This is a demo. It is not a reference for running Temporal in production: the server is a
development build with a SQLite file on one small VM.

## How it works

```
 console (browser)             Temporal server (VM, built from source, worker controller)
   start, approve      ──────▶ workflow state, timers, task queue "tfd"
                                      │ a task arrives and no worker is polling
                                      ▼
                              worker controller ──POST /invocations (managed identity)──▶ Foundry Agent Service
                                                                              hosted agent tfd-worker-<build>
                                                                              ┌──────────────────────────────┐
 Temporal UI ◀─────────── worker polls over gRPC (TLS, outbound) ◀────────────│ container starts, worker polls │
                                                                              │ Agent Framework agents run in  │
                                                                              │ the Workflow; each model call  │
                                                                              │ is an Activity ────────────────┼─▶ gpt-5-mini + web search
                                                                              └──────────────────────────────┘
```

1. `DeepResearchWorkflow` starts. A planner agent proposes sub-questions.
2. The Workflow waits for the `decide` Signal. The worker goes quiet, exits after 15 s, and
   the console stops the sandbox. Nothing is running.
3. The Signal arrives. The controller sees a task with no worker and invokes the hosted
   agent, which starts a sandbox. Researcher agents run in parallel, each with Foundry's web
   search tool, and a writer produces a brief.
4. Optionally the Workflow sleeps on a durable timer. The timer wakes Foundry the same way,
   and a follow-up agent revises the brief.

Every model call is a Temporal Activity, so a call that fails or loses its worker is retried
by itself, and finished calls are not repeated. While an Activity is running, the worker
sends its own Foundry session a request every 45 s, which is what keeps the session from
being stopped as idle.

The agents are ordinary Agent Framework code. The only change is the chat client:
`TemporalChatClient` from Temporal's plugin (`worker/tfd_worker/workflows.py`).

## Run it in your own Azure subscription

You need an Azure subscription in which you can assign roles, and `az` (logged in), `go`,
`git`, `uv`, `python3` and `openssl` on your machine.

```
TFD_STATE=~/.tfd-demo SUBSCRIPTION=<name or id> infra/up.sh
```

This creates one resource group with a Foundry resource, a project, a `gpt-5-mini`
deployment, a small VM and a storage account. It took 15 minutes when we ran it on an empty
resource group. It builds the Temporal server,
installs it on the VM behind a proxy with a public certificate, deploys the worker to Foundry
as a hosted agent and starts the console. At the end it prints the address of the console and
where the access code is. `TFD_STATE` is a private directory for tokens and settings; keep it
out of any repository.

Then open the console, ask a question, and watch the sandbox count. To run the same flow
from a terminal and print the timings:

```
TFD_BASE=https://<host> TFD_CODE=$(cat ~/.tfd-demo/certs/access-code) \
  worker/.venv/bin/python scripts/booth_check.py
```

Cost: the VM is about $0.08 an hour while it runs. Foundry bills sandbox compute only while a
sandbox is up, and the model by tokens. Remove everything with
`az group delete -n rg-temporal-foundry-demo`.

`docs/operations.md` has the details: what runs where, how to release a new worker build, and
how to look at a run without waking anything.

## Run it on a laptop with no Azure

A stub model and a local stand-in for a Foundry session, driven by the real worker
controller. See "Local, with no Azure" in `docs/operations.md`.

## Layout

| Path | What it is |
|---|---|
| `worker/` | The hosted agent: `main.py` (invocations endpoint, supervisor) and `tfd_worker/` (Temporal worker, workflows, lifecycle). Deployed to Foundry as code. |
| `console/` | A small web console for starting runs and approving plans. It shows the sandboxes Foundry has running and what Temporal recorded. |
| `scripts/` | `release.sh` (worker and console), `deploy_agent.py` (one Foundry agent per build), `setup_deployment.py` (register the version with its compute provider), `update_scaler.py` (change a running version's scaler settings), `booth_check.py` (run the flow and print the timings), `tfd.py` and `foundry.py` (inspect runs and sessions). |
| `infra/` | `up.sh` (everything from nothing), Azure resources, the server build, the VM, the proxy. |
| `docs/` | `operations.md` and `contract.md` (the workflow's input, Signal, memo and the console's API). |

Some names say "booth": the demo was built for a conference booth.

## Things worth knowing

- One Foundry agent per Temporal build ID, never redeployed in place. Workflows are pinned to
  the build they started on, and the agent endpoint serves one version.
- The worker starts when the container starts, not when the invocation arrives. Foundry does
  not deliver a request whose caller has hung up, and the provider waits 5 s.
- A Query needs a worker, so a Query wakes a sandbox. The console reads progress from the
  Workflow's memo and its Event History, and the proxy refuses the Query that the Temporal UI
  sends when it opens a running Workflow.
- Foundry stops an idle sandbox without closing its connections. The proxy probes idle
  connections so that Temporal stops offering tasks to a worker that is gone.
- A cold start needs patience from the caller. After nine idle hours Foundry takes about 5 s
  to answer the controller's call, sometimes a little more. The provider's first commit
  waited exactly 5 s: in one run two calls were cut off, no session was created, the
  controller tried again 30 s later, and that wake-up took 45 s. The commit pinned in
  `infra/server/build-server.sh` waits 30 s, and cold wake-ups took 6 to 7 s.

## License

MIT. See `LICENSE`.
