"""Create a Worker Deployment Version with a serverless compute config and make it current.

Usage:
  setup_deployment.py --deployment test --build-id v1 --provider subprocess \
      --provider-config '{"command": "/path/to/launch.sh", "timeout": "2m"}' \
      [--scaler-config '{"scale_up_cooloff_ms": 30000, "metrics_poll_interval_ms": 30000}']

Connection: TEMPORAL_ADDRESS, TEMPORAL_API_KEY, TEMPORAL_TLS_CA (see connection.py).

The provider config is the JSON object the Worker Controller hands to the compute
provider (for azure-foundry: {"project_endpoint": "...", "agent_name": "..."}).
"""

import argparse
import asyncio
import json
import os
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import connection  # noqa: E402

from temporalio.api.common.v1 import Payload
from temporalio.api.compute.v1 import (
    ComputeConfig,
    ComputeConfigScalingGroup,
    ComputeProvider,
    ComputeScaler,
)
from temporalio.api.deployment.v1 import WorkerDeploymentVersion
from temporalio.api.workflowservice.v1 import (
    CreateWorkerDeploymentRequest,
    CreateWorkerDeploymentVersionRequest,
    DescribeWorkerDeploymentVersionRequest,
    SetWorkerDeploymentCurrentVersionRequest,
)
from temporalio.service import RPCError


def json_payload(value: dict) -> Payload:
    return Payload(metadata={"encoding": b"json/plain"}, data=json.dumps(value).encode())


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--namespace", default=os.environ.get("TEMPORAL_NAMESPACE", "default"))
    parser.add_argument("--deployment", required=True)
    parser.add_argument("--build-id", required=True)
    parser.add_argument("--provider", required=True)
    parser.add_argument("--provider-config", required=True)
    parser.add_argument("--scaler", default="no-sync")
    parser.add_argument("--scaler-config", default="")
    parser.add_argument("--set-current", action="store_true")
    parser.add_argument("--set-current-attempts", type=int, default=30)
    args = parser.parse_args()

    client = await connection.connect()
    svc = client.workflow_service
    version = WorkerDeploymentVersion(deployment_name=args.deployment, build_id=args.build_id)

    try:
        await svc.create_worker_deployment(
            CreateWorkerDeploymentRequest(
                namespace=args.namespace,
                deployment_name=args.deployment,
                request_id=str(uuid.uuid4()),
            )
        )
        print(f"created deployment {args.deployment}")
    except RPCError as err:
        print(f"create deployment: {err.status.name}: {err.message}")

    group = ComputeConfigScalingGroup(
        provider=ComputeProvider(
            type=args.provider, details=json_payload(json.loads(args.provider_config))
        )
    )
    if args.scaler_config:
        group.scaler.CopyFrom(
            ComputeScaler(type=args.scaler, details=json_payload(json.loads(args.scaler_config)))
        )
    try:
        await svc.create_worker_deployment_version(
            CreateWorkerDeploymentVersionRequest(
                namespace=args.namespace,
                deployment_version=version,
                compute_config=ComputeConfig(scaling_groups={"default": group}),
                request_id=str(uuid.uuid4()),
            )
        )
        print(f"created version {args.deployment}.{args.build_id} with provider {args.provider}")
    except RPCError as err:
        print(f"create version: {err.status.name}: {err.message}")

    if args.set_current:
        # The version has to be registered by a polling worker first; the controller
        # invokes one when the version is created, so retry until it shows up.
        for attempt in range(1, args.set_current_attempts + 1):
            try:
                await svc.set_worker_deployment_current_version(
                    SetWorkerDeploymentCurrentVersionRequest(
                        namespace=args.namespace,
                        deployment_name=args.deployment,
                        build_id=args.build_id,
                    )
                )
                print(f"set current version after {attempt} attempt(s)")
                break
            except RPCError as err:
                print(f"set current (attempt {attempt}): {err.status.name}: {err.message}")
                await asyncio.sleep(2)

    # Deployment calls are rate limited right after a create, so retry the describe.
    for _ in range(10):
        try:
            described = await svc.describe_worker_deployment_version(
                DescribeWorkerDeploymentVersionRequest(
                    namespace=args.namespace, deployment_version=version
                )
            )
            print(described)
            break
        except RPCError as err:
            print(f"describe: {err.status.name}: {err.message}")
            await asyncio.sleep(3)


if __name__ == "__main__":
    asyncio.run(main())
