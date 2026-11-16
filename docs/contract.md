# Interfaces between the worker, the console and the scripts

The Temporal worker runs inside Foundry (`worker/`). The console is a small web backend and
one page (`console/`). The scripts run on a laptop (`scripts/`). These three share nothing but
JSON, so the console needs only the plain `temporalio` client.

## The workflow

Type `DeepResearchWorkflow`, task queue `tfd`, Worker Deployment `tfd`. Workflow ID
`research-<8 hex chars>`.

Input (one JSON object):

```json
{"question": "string", "max_subquestions": 4, "follow_up_after_seconds": 0, "auto_approve": false}
```

`follow_up_after_seconds` 0 means no follow-up stage. `auto_approve` skips the approval wait.

Signal `decide` (one JSON object). `subquestions`, when present, replaces the planner's list:

```json
{"approved": true, "note": "string", "subquestions": ["string"]}
```

Result (one JSON object): `{"question", "plan", "research", "report", "follow_up", "approved"}`
with the same shapes as the memo keys below.

## Progress: the memo

The workflow publishes progress by upserting its memo. Reading the memo (describe) needs no
worker. **Nothing may send a Query**: a Query wakes a Foundry sandbox just to look.

| Key | Value |
|---|---|
| `phase` | `planning`, `awaiting_approval`, `researching`, `writing`, `waiting_for_follow_up`, `writing_follow_up`, `completed`, `rejected` |
| `question` | string |
| `plan` | `{"summary": "string", "subquestions": ["string"]}` |
| `decision` | `{"approved": bool, "note": "string"}` |
| `research` | list of `{"index": int, "subquestion": "string", "status": "pending" or "running" or "done" or "failed", "findings": "markdown string", "error": "string"}` |
| `report` | markdown string |
| `follow_up` | markdown string |
| `follow_up_due` | ISO 8601 time the follow-up timer fires |

## What ran where: history

Everything else comes from Event History (`GetWorkflowExecutionHistory`), which also needs no
worker:

- Each agent model call is an Activity of type `microsoft_agent_framework.invoke_model`. Its
  scheduled event's user metadata summary names the agent (`planner`, `researcher 2`,
  `writer`, `follow-up`). `ActivityTaskStarted.identity` and `WorkflowTaskStarted.identity`
  are the worker identity.
- Worker identity in Foundry: `foundry/<agent name>/v<agent version>/<first 12 chars of the
  session ID>/pid<n>`. Locally: `local/<host>/pid<n>`.
- A wake is a task whose worker identity names a session not seen earlier in the run. Its
  latency is the time from the event that made the task (workflow started, Signal, timer
  fired) to the task starting.

## Sandbox events (worker to console)

The sandbox reports its own lifecycle, because Foundry's session status lags by minutes.
Best effort: a failed report never affects the worker.

`POST {TFD_CONSOLE_URL}/api/internal/sandbox-events` with `Authorization: Bearer <token>`
(the same token the worker presents to Temporal):

```json
{"session": "full session id", "agent": "tfd-worker-...", "agent_version": "1",
 "build_id": "...", "event": "boot", "at": 1794800000.0, "detail": {}}
```

| `event` | Sent when |
|---|---|
| `boot` | the container process starts |
| `polling` | the worker is connected and polling |
| `alive` | every 15 s while the worker polls; `detail.inflight` is the number of running Activities |
| `keepalive` | the worker sent its own session a request to stay alive |
| `idle_exit` | the worker went quiet and stopped polling; the sandbox can be released |
| `stopping` | the process received SIGTERM |
| `worker_failed` | the worker process died and will not be restarted |

On `idle_exit` the console stops that Foundry session (`POST .../endpoint/sessions/{id}:stop`).
It can, because it runs as the identity that created the session (the controller's); the
sandbox cannot stop itself.

## Requests to a sandbox (invocations endpoint)

| Body | Sent by | Effect |
|---|---|---|
| `{"deploymentName", "buildId"}` | the controller, without a session ID | Foundry creates a sandbox; the worker starts when the container starts; 202 (409 if the build is not this agent's) |
| the same, with `agent_session_id` | the worker itself, every 45 s while an Activity runs | resets the session's idle timer |
| `{"action": "crash_worker"}` with `agent_session_id` | the console, on the presenter's request | kills the worker process; needs `TFD_ALLOW_FAULTS=1` |

## Console API (browser to console)

All under `/api`, JSON, cookie session after `POST /api/login {"code": "..."}`.

| Call | Purpose |
|---|---|
| `GET /api/config` | names to display (Foundry agent, model deployment, region), preset questions, limits |
| `POST /api/runs {"question", "follow_up_after_seconds"}` | start a run, returns `{"id"}` |
| `GET /api/runs` | recent runs, newest first |
| `GET /api/runs/{id}` | status, memo fields, timeline (from history), wakes with latency, sandboxes used |
| `POST /api/runs/{id}/decision {"approved", "note", "subquestions"}` | send the `decide` Signal |
| `GET /api/sandboxes` | sandboxes the console knows about and their state, and how many are polling now |
| `POST /api/sandboxes/{session}/stop` | stop a sandbox now (presenter action; a hard stop) |
| `POST /api/sandboxes/{session}/crash-worker` | kill the worker process inside a sandbox mid-task (presenter action); the sandbox restarts the worker and Temporal retries the interrupted call |
| `GET /api/session` | 200 when the login cookie is valid, 401 otherwise; the proxy uses it to put the Temporal UI behind the same login |

## Page element IDs (for the recorder)

`data-testid` values the page keeps stable: `access-code`, `login-button`, `question-input`,
`preset-question`, `start-button`, `phase`, `plan`, `approve-button`, `reject-button`,
`research-item`, `report`, `follow-up`, `sandbox-count`, `sandbox-card`, `timeline`,
`wake-badge`, `stop-sandbox-button`, `crash-worker-button`, `temporal-ui-link`.
