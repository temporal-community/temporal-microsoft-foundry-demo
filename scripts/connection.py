"""How the laptop-side scripts reach Temporal.

Local harness: nothing set, 127.0.0.1:7233 in plain text. The demo VM: TEMPORAL_ADDRESS,
TEMPORAL_API_KEY (the bearer token the proxy checks) and TEMPORAL_TLS=1. TEMPORAL_TLS_CA (a
path) pins a private CA instead of the system's trust store.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from temporalio.client import Client
from temporalio.service import TLSConfig


async def connect(**kwargs: Any) -> Client:
    ca_path = os.environ.get("TEMPORAL_TLS_CA")
    tls: TLSConfig | bool = os.environ.get("TEMPORAL_TLS", "").lower() in {"1", "true", "yes"}
    if ca_path:
        tls = TLSConfig(server_root_ca_cert=Path(ca_path).read_bytes())
    return await Client.connect(
        os.environ.get("TEMPORAL_ADDRESS", "127.0.0.1:7233"),
        namespace=os.environ.get("TEMPORAL_NAMESPACE", "default"),
        api_key=os.environ.get("TEMPORAL_API_KEY") or None,
        tls=tls,
        **kwargs,
    )
