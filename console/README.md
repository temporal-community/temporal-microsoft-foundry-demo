# Booth console

One web page and a small backend for running the demo live. A visitor enters an access code
and asks a research question. The left column shows the plan for approval and then the text
each agent writes. The right column counts the Foundry sandboxes that are up and lists each
time Temporal woke one, with the seconds it took.

The interfaces it implements are in [`docs/contract.md`](../docs/contract.md).

## How it reads a run

The console never sends a Query or an Update. Either one needs a worker, so it would wake a
Foundry sandbox just to look. A run is read from two calls that need no worker:

- **Describe** gives the memo the workflow publishes (phase, plan, findings, brief), the
  Activities that are running now and a Workflow Task that is waiting for a worker.
- **Event History** gives what ran where. The first task a sandbox runs for a run is a wake.
  Its latency is the time from the event that made the task (run started, Signal, timer
  fired) to the task starting.

The sandbox count does not come from Foundry, whose session status lags by minutes. Each
sandbox reports its own lifecycle to `POST /api/internal/sandbox-events`. A sandbox that has
been silent for 45 s is shown as gone. When a sandbox reports `idle_exit`, the console stops
that Foundry session, which is what brings the count back to zero within seconds.

`app/history.py` holds the logic as pure functions over normalized events, so it is tested
without Temporal.

## Run it locally

Python 3.12 or newer and [uv](https://docs.astral.sh/uv/).

```sh
cd console
uv sync --group dev
.venv/bin/python -m playwright install chromium     # once, for the page tests and screenshots
.venv/bin/python -m pytest -q
```

**The page alone, against a scripted run.** A scripted server freezes one realistic run at each moment
of its life. The access code is `booth`.

```sh
.venv/bin/python tools/fake_server.py               # http://127.0.0.1:8090
# then open /#/run/research-<stage>, where <stage> is one of: starting planning approval
# waking researching retry refused writing waiting followup done failed rejecting rejected
.venv/bin/python tools/screenshots.py               # every state at 1920x1080 into screenshots/
.venv/bin/python tools/screenshots.py --size 1440x900 --out screenshots/laptop
```

**Real runs on a laptop, with a stand-in for Foundry.** This needs the custom Temporal server build at
`/tmp/foundry/build/temporal-server-dev` and its run directory at `/tmp/foundry/run`
(`infra/server/build-server.sh`, `run-server.sh`). The harness registers the `tfd` Worker
Deployment with a compute provider that starts `worker/local/session.py`, a stand-in for a
Foundry sandbox with a stub model.

```sh
tools/local_harness.sh
TEMPORAL_ADDRESS=127.0.0.1:7233 TFD_ACCESS_CODE=booth TFD_INTERNAL_TOKEN=local-token \
  TFD_SESSION_SECRET=dev-secret TFD_COOKIE_SECURE=0 TFD_AGENT_NAME=tfd-worker-local \
  TFD_MODEL_NAME=gpt-5-mini TFD_REGION="East US 2" TFD_TEMPORAL_UI_URL=http://localhost:8233 \
  TFD_STATE_DIR=/tmp/foundry/console/state \
  .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8080
.venv/bin/python tools/live_run.py                  # three runs through the real page
```

`tools/live_run.py` clicks through a full run with an edited sub-question and a follow-up,
a run started through the API with a 20 s follow-up, and a rejected run. It exits non-zero
if a state does not arrive or the page logs an error. Pointed at a deployed console with
`--base` and `--code`, it is an end-to-end check that spends three runs of model tokens.

## Configuration

Everything comes from the environment. `TFD_ACCESS_CODE` and `TFD_INTERNAL_TOKEN` are
required, and the console does not start without them.

| Variable | Default | Meaning |
|---|---|---|
| `TFD_ACCESS_CODE` | none | The code visitors type. Not a user account: it keeps strangers from spending model tokens. |
| `TFD_INTERNAL_TOKEN` | none | Bearer token the sandboxes present to `/api/internal/sandbox-events`. The same token the worker presents to Temporal. |
| `TFD_SESSION_SECRET` | random per process | Signs the login cookie. Set it, or every restart logs everyone out. |
| `TFD_COOKIE_SECURE` | `1` | Set `0` only to use the page over plain http on a laptop. |
| `TEMPORAL_ADDRESS` | `127.0.0.1:7233` | Temporal frontend, plain gRPC. |
| `TEMPORAL_NAMESPACE` | `default` | |
| `TFD_TASK_QUEUE` | `tfd` | |
| `FOUNDRY_PROJECT_ENDPOINT` | empty | `https://<account>.services.ai.azure.com/api/projects/<project>`. Needed with `TFD_FOUNDRY_STOP=1`. |
| `TFD_FOUNDRY_STOP` | `0` | `1` lets the console stop Foundry sessions and ask a sandbox to crash its worker. With `0` those requests are only recorded. |
| `TFD_SHOW_STOP_BUTTON` | `0` | `1` adds "Stop the sandbox" next to "Crash the worker". A stopped sandbox does not close its connections, so the run takes about a minute and a half to recover instead of half a minute. |
| `TFD_AGENT_NAME`, `TFD_MODEL_NAME`, `TFD_REGION` | empty | Shown in the page footer. |
| `TFD_TEMPORAL_UI_URL` | empty | Base of the Temporal UI, a path or a URL. The run link is `<base>/namespaces/<namespace>/workflows/<id>`. Empty hides the link. |
| `TFD_STATE_DIR` | `state` | Where `sandboxes.jsonl` is kept, so a restart still knows the sandboxes of the last two hours. |
| `TFD_PRESETS` | four built-in questions | A JSON list of strings, or questions separated by a vertical bar. At most 8 are used. |
| `TFD_MAX_QUESTION_LENGTH` | `300` | Characters. Questions shorter than 8 are refused too. |
| `TFD_MAX_FOLLOW_UP_SECONDS` | `600` | Longest follow-up delay the API accepts. The page offers none, 60 s and 120 s. |
| `TFD_STARTS_PER_MINUTE` | `4` | Runs one login may start per minute. All logins together may start six times that. |
| `TFD_MAX_OPEN_RUNS` | `8` | Runs that may be working at once. Runs waiting on approval or a timer are not counted. |

## On the demo VM

`infra/vm/deploy_console.sh` installs it. What the console needs there:

- This directory without `tests/`, `tools/` and `screenshots/`, at `/opt/tfd-console/app`.
- `uv sync --frozen --no-dev` with `UV_PROJECT_ENVIRONMENT=/opt/tfd-console/venv`.
  `.python-version` asks for Python 3.12, which uv downloads if the VM has none. Every locked
  dependency has a prebuilt Linux wheel for x86_64 and arm64, so no compiler is needed.
- `/etc/tfd-console.env` with the variables above, readable by root only.
- `deploy/tfd-console.service`: uvicorn on `127.0.0.1:8080` as the user `tfdconsole`, with
  `/var/lib/tfd-console` as the only writable path.
- One process. The sandbox registry, the limits and the short run cache are in memory, so
  the unit must not get `--workers`.
- A proxy in front that terminates TLS, passes the visitor's `Host` header through unchanged
  and sets `X-Forwarded-For`. The console refuses a POST whose `Origin` does not match
  `Host`, and counts wrong access codes per forwarded address.
- An identity that owns the Foundry sessions. `DefaultAzureCredential` picks up the VM's
  managed identity. It has to be the identity the Temporal worker controller uses to wake
  sandboxes, because Foundry lets only a session's creator stop it or send it a request.
  A user-assigned identity also needs `AZURE_CLIENT_ID` in the environment file.

`GET /api/session` answers 200 with a valid login cookie and 401 without. The proxy uses it
to put the Temporal UI behind the same login.

## What a visitor can and cannot do

- Five wrong access codes from one address lock that address out of logging in for the rest
  of the minute. Right codes are not counted. A login lasts 12 hours.
- Start limits, the cap on working runs and the question length are in the table above.
  A run closes by itself after two hours if nobody approves it.
- Anyone logged in can stop a sandbox or crash a worker. Both are there for the presenter.
- Text written by the models reaches the page as text nodes through a small markdown
  renderer in `app/static/app.js` (headings, lists, bold, italics, code, http and https
  links). The page loads nothing from other hosts and its Content-Security-Policy allows no
  inline script and no `eval`. Browser automation therefore has to pass functions, not
  expression strings, or bypass the policy.

## Files

| Path | What it is |
|---|---|
| `app/main.py` | The API, the login, the limits. `create_app()` takes a gateway and a registry, which is how the tests and the scripted server replace Temporal. |
| `app/gateway.py` | The three things the console asks of Temporal: start, Signal `decide`, read. |
| `app/history.py` | Events to timeline, wakes and sandboxes. Pure functions. |
| `app/sandboxes.py` | What the sandboxes reported, their state, and the stop and crash requests. |
| `app/foundry.py` | The two Foundry requests: stop a session, invoke a session. |
| `app/static/` | The page: `index.html`, `style.css` and `app.js`, served as written. |
| `tools/` | Scripted server, screenshots, the local harness and the click-through. Not deployed. |
| `tests/` | `test_page.py` runs the page in Chromium and skips if the browser is not installed. |
