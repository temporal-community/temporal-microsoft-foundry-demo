# Operating the demo

For whoever sets the demo up or maintains it.

Private state (tokens, the access code, env files) lives outside this repository, in one
directory referred to below as `$TFD_STATE`. Nothing in it is ever committed.

## From nothing

```
TFD_STATE=<private dir outside the repository> SUBSCRIPTION=<name or id> infra/up.sh
```

creates everything in one resource group of your Azure subscription. On November 16, 2026 it
took 15 minutes from an empty resource group to a demo that passed `scripts/booth_check.py`.
It runs five scripts in order:

1. `infra/azure/setup.sh`: the Foundry resource, the project and the model deployment.
2. `infra/vm/create_vm.sh`: the VM.
3. `infra/server/build-server.sh`: the Temporal server, built from source with the Foundry
   compute provider.
4. `infra/vm/install_server.sh`: the server, the proxy and the Temporal UI on the VM.
5. `scripts/release.sh`: the worker in Foundry and the console.

At the end it prints the address and where the access code is. Each script can be run again
by itself. You need `az` (logged in, with the right to assign roles), `go`, `git`, `uv`,
`python3` and `openssl`.

| File in `$TFD_STATE` | What it holds |
|---|---|
| `env.sh` | subscription, resource group, Foundry resource and project names, project endpoint |
| `vm.env` | VM name, public host name, managed identity |
| `agent.env`, `agent-2.env`, ... | the current Foundry agent name and Temporal build ID of each booth station |
| `client.env` | Temporal address, bearer token and TLS setting for the laptop scripts |
| `worker-booth.env` | environment of the hosted agent |
| `certs/token`, `certs/access-code`, `certs/session-secret` | the gRPC bearer token, the booth access code, the console's cookie key |
| `presets.txt` | the questions offered on the page, separated by `\|` |

## What is running

Everything is in one resource group, `rg-temporal-foundry-demo` in East US 2 unless you chose
otherwise. `$TFD_STATE/env.sh` and `vm.env` hold the names.

| Resource | Purpose | Cost |
|---|---|---|
| Foundry resource `tfd-foundry-<suffix>`, project `temporal-foundry-demo` | model deployment `gpt-5-mini`, hosted agents `tfd-worker-<build>` | tokens used; sandbox compute only while a sandbox is up (about $0.12 per hour each) |
| VM `vm-tfd-temporal` (`Standard_D2als_v7`) | Temporal server, proxy, console, Temporal UI | about $0.08 per hour |
| Storage account `tfdstore<suffix>` | carries files to the VM | negligible |

On the VM: `temporal.service` (the server, 127.0.0.1:7233, SQLite file in
`/var/lib/temporal`), `caddy.service` (port 443, public certificate), `tfd-console.service`
(127.0.0.1:8080), and the `temporal-ui` container (127.0.0.1:8081). Port 443 is the only open
port. There is no SSH. To run a script on the VM, use `az vm run-command invoke` the way
`infra/vm/deploy_console.sh` does.

Pause the VM: `az vm deallocate -g rg-temporal-foundry-demo -n vm-tfd-temporal` (start it
again with `az vm start`). Remove everything: `az group delete -n rg-temporal-foundry-demo`.

Kill switch, if sandboxes are being created and should not be: on the VM set
`workercontroller.enabled` to `false` in `/etc/temporal/dynamicconfig/development-sql.yaml`.
The server rereads the file every 10 s.

## Local, with no Azure

A stub model and a stand-in for Foundry, driven by the real worker controller.

```
infra/server/build-server.sh /tmp/foundry                 # builds the server for this machine
infra/server/run-server.sh /tmp/foundry/build/temporal-server-$(go env GOOS)-$(go env GOARCH)
temporal operator namespace create -n default
cd worker && uv sync --group dev && cd ..
PY=$PWD/worker/.venv/bin/python
$PY scripts/setup_deployment.py --deployment tfd --build-id local --provider subprocess \
  --provider-config "{\"command\": \"$PY\", \"args\": \"$PWD/worker/local/session.py,--build-id,local,--idle-timeout,60,--idle-exit,10\", \"timeout\": \"70s\"}" \
  --set-current
$PY scripts/tfd.py start-research --id research-1 --question "..." --follow-up-after 60
$PY scripts/tfd.py show --id research-1          # the plan, and which worker process ran the planner
# wait half a minute: the worker has nothing to do and exits
$PY scripts/tfd.py decide --id research-1 --approve
$PY scripts/tfd.py show --id research-1          # run it again after the 60 s timer
```

`show` lists every step with the worker that ran it. When you wait before approving, the
planner, the research and the follow-up each show a different process ID: the worker exits
when it has nothing to do, and the controller starts another when the Signal or the timer
needs one. Approve at once and the same worker carries on.

`worker/local/session.py` does what Foundry does on a wake: starts `worker/main.py`, sends it
one invocation and leaves it until it is terminated. The `subprocess` provider is dev-only
and needs `timeout` on the path; `infra/server/bin` has a stand-in for macOS and
`run-server.sh` puts it there.

## Stations

A station is one Worker Deployment, one task queue, one Foundry agent and one console. Two
booth laptops on different stations never share a sandbox, so each sees its own zero and its
own wake-ups. Station 1 is deployment `tfd` at the site root. Station N is `tfd-N` under `/N/`.
The stations share the Temporal Service and namespace, the access code, the login and the
Temporal UI.

```
TFD_STATE=<private state dir> TFD_STATIONS=3 scripts/release.sh
```

deploys the current worker code to every station as a new Foundry agent, updates the consoles
and the proxy routes, and makes the new builds current. Run it when no run is working. It
takes about two minutes per station. The next two sections say what it does for one station.

## A new worker build

A worker build is a new Foundry agent and a new Worker Deployment Version. Never redeploy an
agent in place: workflows pinned to a build must keep finding that build's code.

```
. $TFD_STATE/env.sh; export PROJECT_ENDPOINT
cd worker && uv export --no-dev --no-hashes --no-emit-project -o requirements.txt && cd ..
worker/.venv/bin/python scripts/deploy_agent.py --idle-timeout 120 --env-file $TFD_STATE/worker-booth.env
# prints AGENT_NAME and BUILD_ID; save them as $TFD_STATE/agent.env
. $TFD_STATE/client.env
worker/.venv/bin/python scripts/setup_deployment.py --deployment tfd --build-id $BUILD_ID \
  --provider azure-foundry \
  --provider-config "{\"project_endpoint\": \"$PROJECT_ENDPOINT\", \"agent_name\": \"$AGENT_NAME\"}" \
  --scaler-config '{"scale_up_cooloff_ms": 15000, "metrics_poll_interval_ms": 30000}' --set-current
```

Between the two commands, redeploy the console (next section) so that it watches the new
agent before Temporal sends work to it. Then check the whole flow:

```
TFD_BASE=https://<host> TFD_CODE=$(cat $TFD_STATE/certs/access-code) \
  worker/.venv/bin/python scripts/booth_check.py --follow-up 60
```

It starts a run from zero sandboxes, waits for the plan and for zero, approves, and prints
every wake and how long it took. `--break crash-worker` kills the worker during the research.

The build ID is a hash of the worker code, its pinned dependencies and its environment, so
the same inputs give the same agent name. Old agents cost nothing while idle; delete them
with `project.agents.delete(name, force=True)` once no open workflow is pinned to them.

Leave the worker's poller counts at the SDK defaults. With two Activity pollers, the four
parallel researchers did not all find a waiting poller, and the controller answered with
extra sandboxes: three for one run.

## The console

```
TFD_STATE=<private state dir> infra/vm/deploy_console.sh
```

The script packs `console/`, carries it to the VM through the storage account and applies it
with `az vm run-command`. For each station it writes an environment file from the state
directory (`agent.env` or `agent-N.env` names the agent that console watches and stops) and a
systemd unit: `tfd-console` on port 8080 for station 1, `tfd-console-N` on port 809N for
station N. It installs `infra/vm/Caddyfile` with one route per extra station. The proxy is
restarted only when its config changed, because a restart cuts every worker's connection. Do
not deploy while a run is working.

Settings are listed in `console/README.md`. Two that matter at the booth:

- `TFD_SHOW_STOP_BUTTON=1` adds "Stop the sandbox" beside "Crash the worker". It is off. A hard
  stop takes about 50 s to recover from, against about 10 s for a crashed worker.
- The access code is `certs/access-code`. To change it, replace the file and redeploy. Every
  browser has to sign in again.

Logs: `journalctl -u tfd-console` on the VM, through `az vm run-command`.

## Scaler settings

The controller starts a sandbox when a task arrives with no worker, and it also checks the
backlog every `metrics_poll_interval_ms`. `scale_up_cooloff_ms` is the least time between two
starts. Keep it longer than a wake-up takes: with 5 s, a backlog check that landed while a
sandbox was still starting, after a call to Foundry of 5 s or more, started a second one. With
15 s there is one sandbox per wake-up, and a wake-up that really failed is still retried at
the next backlog check.

`scripts/update_scaler.py` shows the settings of a running version and changes them in place,
with no new version and no new agent:

```
. $TFD_STATE/client.env
worker/.venv/bin/python scripts/update_scaler.py --deployment tfd --build-id $BUILD_ID
worker/.venv/bin/python scripts/update_scaler.py --deployment tfd --build-id $BUILD_ID \
  --scaler-config '{"scale_up_cooloff_ms": 15000, "metrics_poll_interval_ms": 30000}'
```

## Replacing the server binary

```
infra/server/build-server.sh /tmp/tfd-build linux amd64
TFD_STATE=<private state dir> infra/vm/update_server.sh /tmp/tfd-build/build/temporal-server-linux-amd64
TFD_STATE=<private state dir> infra/vm/update_server.sh --rollback      # put the previous one back
```

`update_server.sh` swaps only the binary and restarts the server, which is down for about ten
seconds. Run it when no run is working, then run `scripts/booth_check.py`.

## Dead sandboxes and the proxy

Foundry stops a sandbox without closing its connections. Until the proxy notices, Temporal
keeps handing tasks to that worker's open polls, and each one is lost until its timeout. Two
settings keep that window to about 15 s: `keepalive_interval 5s` in the Caddyfile and
`net.ipv4.tcp_keepalive_probes = 2` on the VM (`/etc/sysctl.d/90-tfd.conf`). Do not add
`flush_interval -1` to the gRPC route: it kept dead workers' polls open for more than a minute.

## Looking at a run without waking anything

`scripts/tfd.py show --id <workflow id>` reads describe and history. `scripts/foundry.py
sessions --agent <agent>` lists Foundry's own session records (the `status` field lags by a
few minutes; `stopped` is filled in later and is accurate). `scripts/foundry.py logs` reads a
sandbox's log stream. Do not use `temporal workflow query`: a Query wakes a sandbox.

The `temporal` CLI reaches the server through the proxy with TLS and the token as its API key:

```
. $TFD_STATE/vm.env
temporal workflow list --address $VM_FQDN:443 --tls --api-key "$(cat $TFD_STATE/certs/token)"
```
