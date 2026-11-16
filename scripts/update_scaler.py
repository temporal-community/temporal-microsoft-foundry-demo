"""Show or change the scaler settings of a Worker Deployment Version that is already running.

    update_scaler.py --deployment tfd --build-id <id>                      # show them
    update_scaler.py --deployment tfd --build-id <id> \
        --scaler-config '{"scale_up_cooloff_ms": 15000, "metrics_poll_interval_ms": 30000}'

Connection: TEMPORAL_ADDRESS, TEMPORAL_API_KEY, TEMPORAL_TLS (see connection.py).

Only the scaler of the "default" scaling group changes. The request still carries the
provider exactly as the server reports it, so the provider is the same afterwards whether or
not the server honours the update mask. The server validates the settings before they are
applied. No new version, no new Foundry agent, and the worker is not restarted.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import connection  # noqa: E402

from google.protobuf.field_mask_pb2 import FieldMask
from temporalio.api.common.v1 import Payload
from temporalio.api.compute.v1 import ComputeConfigScalingGroup, ComputeConfigScalingGroupUpdate, ComputeScaler
from temporalio.api.deployment.v1 import WorkerDeploymentVersion
from temporalio.api.workflowservice.v1 import (
    DescribeWorkerDeploymentVersionRequest,
    UpdateWorkerDeploymentVersionComputeConfigRequest,
    ValidateWorkerDeploymentVersionComputeConfigRequest,
)

GROUP = "default"


def summary(group: ComputeConfigScalingGroup) -> str:
    scaler = group.scaler.details.data.decode() if group.scaler.details.data else "(server defaults)"
    return f"provider {group.provider.type}, scaler {group.scaler.type or '(default)'} {scaler}"


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--namespace", default=os.environ.get("TEMPORAL_NAMESPACE", "default"))
    parser.add_argument("--deployment", required=True)
    parser.add_argument("--build-id", required=True)
    parser.add_argument("--scaler", default="no-sync")
    parser.add_argument("--scaler-config", default="", help="JSON object; omit to only show the current settings")
    args = parser.parse_args()

    client = await connection.connect()
    svc = client.workflow_service
    version = WorkerDeploymentVersion(deployment_name=args.deployment, build_id=args.build_id)

    async def current() -> ComputeConfigScalingGroup:
        described = await svc.describe_worker_deployment_version(
            DescribeWorkerDeploymentVersionRequest(namespace=args.namespace, deployment_version=version)
        )
        groups = described.worker_deployment_version_info.compute_config.scaling_groups
        if GROUP not in groups:
            raise SystemExit(f"{args.deployment}.{args.build_id} has no scaling group named {GROUP!r}")
        return groups[GROUP]

    before = await current()
    print(f"now:   {summary(before)}")
    if not args.scaler_config:
        return 0

    group = ComputeConfigScalingGroup()
    group.CopyFrom(before)
    group.scaler.CopyFrom(
        ComputeScaler(
            type=args.scaler,
            details=Payload(
                metadata={"encoding": b"json/plain"}, data=json.dumps(json.loads(args.scaler_config)).encode()
            ),
        )
    )
    update = {GROUP: ComputeConfigScalingGroupUpdate(scaling_group=group, update_mask=FieldMask(paths=["scaler"]))}
    await svc.validate_worker_deployment_version_compute_config(
        ValidateWorkerDeploymentVersionComputeConfigRequest(
            namespace=args.namespace, deployment_version=version, compute_config_scaling_groups=update
        )
    )
    print("the server accepts the new settings")
    await svc.update_worker_deployment_version_compute_config(
        UpdateWorkerDeploymentVersionComputeConfigRequest(
            namespace=args.namespace,
            deployment_version=version,
            compute_config_scaling_groups=update,
            request_id=str(uuid.uuid4()),
        )
    )
    after = before
    for _ in range(10):  # deployment calls are rate limited right after a change
        await asyncio.sleep(2)
        try:
            after = await current()
            break
        except Exception as error:
            print(f"describe: {str(error)[:120]}")
    print(f"after: {summary(after)}")
    if after.provider != before.provider:
        print("WARNING: the provider changed; put it back with setup_deployment.py's settings")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
