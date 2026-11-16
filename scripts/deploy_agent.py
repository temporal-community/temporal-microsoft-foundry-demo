"""Deploy the worker to Foundry as a new hosted agent, one agent per Temporal build ID.

The build ID is a hash of the code and its pinned dependencies, and it is part of the agent
name. An agent is never redeployed in place: the agent endpoint serves one agent version, and
workflows pinned to a build must keep finding that build's code.

    deploy_agent.py --idle-timeout 120 --env TFD_DIAGNOSTICS=1 --env TFD_WORKER_DISABLED=1
    deploy_agent.py --idle-timeout 300 --env-file ../azure/worker.env --suffix a

Reads PROJECT_ENDPOINT from the environment. Prints the agent name and build ID.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import os
import sys
import time
import zipfile
from datetime import timedelta
from pathlib import Path

from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import (
    CodeConfiguration,
    HostedAgentDefinition,
    ProtocolVersionRecord,
    SessionConfiguration,
)
from azure.core.exceptions import ResourceNotFoundError
from azure.identity import DefaultAzureCredential

APP_DIR = Path(__file__).resolve().parent.parent / "worker"
DEPLOYMENT_NAME = "tfd"


def code_files() -> list[Path]:
    files = [APP_DIR / "main.py", APP_DIR / "requirements.txt"]
    files += sorted((APP_DIR / "tfd_worker").glob("*.py"))
    return files


def build_id(extra: str = "") -> str:
    digest = hashlib.sha256()
    for path in code_files():
        digest.update(path.relative_to(APP_DIR).as_posix().encode())
        digest.update(path.read_bytes())
    digest.update(extra.encode())
    return digest.hexdigest()[:10]


def code_zip() -> Path:
    target = APP_DIR / ".build" / "code.zip"
    target.parent.mkdir(exist_ok=True)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in code_files():
            # Fixed timestamps keep the zip identical for identical code. No Unix mode bits:
            # entries written the way azd writes them (Go's zip.Writer.Create). With a Unix
            # mode but no file-type bits, Foundry's remote build did not see requirements.txt.
            info = zipfile.ZipInfo(path.relative_to(APP_DIR).as_posix(), (2026, 1, 1, 0, 0, 0))
            info.create_system = 0
            info.external_attr = 0
            archive.writestr(info, path.read_bytes(), zipfile.ZIP_DEFLATED)
    target.write_bytes(buffer.getvalue())
    return target


def parse_env(pairs: list[str], env_file: str | None) -> dict[str, str]:
    values: dict[str, str] = {}
    if env_file:
        for line in Path(env_file).read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip()
    for pair in pairs:
        key, _, value = pair.partition("=")
        values[key] = value
    return values


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--idle-timeout", type=int, default=300)
    parser.add_argument("--cpu", default="1")
    parser.add_argument("--memory", default="2Gi")
    parser.add_argument("--env", action="append", default=[])
    parser.add_argument("--env-file")
    parser.add_argument("--suffix", default="", help="distinguishes two deployments of the same code")
    parser.add_argument(
        "--deployment",
        default=DEPLOYMENT_NAME,
        help="Worker Deployment name, also used as the task queue (one per booth station: tfd, tfd-2, ...)",
    )
    parser.add_argument("--wait-seconds", type=int, default=900)
    args = parser.parse_args()

    endpoint = os.environ["PROJECT_ENDPOINT"]
    extra_env = parse_env(args.env, args.env_file)
    # The configuration is part of the build: a different idle timeout or environment is a
    # different worker as far as pinned workflows are concerned.
    # The default deployment adds nothing to the hash, so its build IDs stay what they were.
    station = "" if args.deployment == DEPLOYMENT_NAME else f"|{args.deployment}"
    build = build_id(f"{args.idle_timeout}|{sorted(extra_env.items())}|{args.suffix}{station}")
    agent_name = f"tfd-worker-{build}"
    environment = {
        "PYTHONNOUSERSITE": "1",
        "PYTHONUNBUFFERED": "1",
        "TEMPORAL_DEPLOYMENT_NAME": args.deployment,
        "TEMPORAL_BUILD_ID": build,
        "TEMPORAL_TASK_QUEUE": args.deployment,
        "TFD_SESSION_IDLE_TIMEOUT_SECONDS": str(args.idle_timeout),
        **extra_env,
    }
    definition = HostedAgentDefinition(
        cpu=args.cpu,
        memory=args.memory,
        environment_variables=environment,
        protocol_versions=[ProtocolVersionRecord(protocol="invocations", version="2.0.0")],
        code_configuration=CodeConfiguration(
            runtime="python_3_13",
            entry_point=["python", "main.py"],
            dependency_resolution="remote_build",
        ),
        session_configuration=SessionConfiguration(
            idle_timeout_seconds=timedelta(seconds=args.idle_timeout)
        ),
    )
    project = AIProjectClient(endpoint=endpoint, credential=DefaultAzureCredential())
    # The name is the build: an agent that already exists is this exact code and configuration.
    # Deploying it again would add a version in place, which a pinned workflow must never see.
    try:
        existing = project.agents.get_version(agent_name, "1")
    except ResourceNotFoundError:
        existing = None
    if existing is not None and existing.get("status") == "active":
        print(f"{agent_name} is already deployed; nothing to do", file=sys.stderr)
        print(f"AGENT_NAME={agent_name}")
        print(f"AGENT_VERSION={existing.version}")
        print(f"BUILD_ID={build}")
        print("DEPLOY_SECONDS=0")
        return
    started = time.time()
    with code_zip().open("rb") as code:
        version = project.agents.create_version_from_code(
            agent_name,
            definition=definition,
            code=code,
            description=f"Temporal worker for deployment {args.deployment}, build {build}",
            metadata={"temporal_deployment": args.deployment, "temporal_build_id": build},
        )
    print(f"created {agent_name} version {version.version} status={version.get('status')}", file=sys.stderr)
    status = version.get("status")
    while status not in ("active", "failed") and time.time() - started < args.wait_seconds:
        time.sleep(10)
        version = project.agents.get_version(agent_name, version.version)
        if version.get("status") != status:
            status = version.get("status")
            print(f"  {round(time.time() - started)}s status={status}", file=sys.stderr)
    if status != "active":
        print(dict(version), file=sys.stderr)
        raise SystemExit(f"agent version did not become active: {status}")
    print(f"AGENT_NAME={agent_name}")
    print(f"AGENT_VERSION={version.version}")
    print(f"BUILD_ID={build}")
    print(f"DEPLOY_SECONDS={round(time.time() - started)}")


if __name__ == "__main__":
    main()
