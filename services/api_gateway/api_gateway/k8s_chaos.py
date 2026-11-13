"""Real bad deploys on Kubernetes (module 10).

Under Compose, the `bad_deploy` chaos experiment can only flip a fault on inside a running
process and write a deployment row to make the change visible to RCA. On Kubernetes it does
what a bad release actually does: it patches the Deployment's pod template to a new
version that carries a defect (SIM_DEFECT=bad_deploy). That creates a new ReplicaSet
revision, the pods roll, the defect goes live - and the healing engine's
`rollback_bad_deploy` playbook has a genuine previous revision to `rollout undo` to.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from aiops_common.log import get_logger

log = get_logger(__name__)

VERSION_LABEL = "app.kubernetes.io/version"


def _apps_api() -> Any:
    from kubernetes import client, config

    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()
    return client.AppsV1Api()


def _bump(version: str) -> str:
    """v1.0.4 -> v1.0.5-canary; anything unparseable just gets a timestamped canary tag."""
    base = version.split("-", 1)[0]
    parts = base.lstrip("v").split(".")
    if len(parts) == 3 and all(p.isdigit() for p in parts):
        return f"v{parts[0]}.{parts[1]}.{int(parts[2]) + 1}-canary"
    return f"{base}-canary-{datetime.now(UTC):%H%M%S}"


def _roll_out(service: str, namespace: str, defect: str, apps: Any) -> tuple[str, str]:
    dep = apps.read_namespaced_deployment(service, namespace)
    container = dep.spec.template.spec.containers[0].name
    from_version = (dep.spec.template.metadata.labels or {}).get(VERSION_LABEL, "v1.0.4")
    to_version = _bump(from_version)
    patch = {
        "metadata": {
            "annotations": {"kubernetes.io/change-cause": f"chaos bad_deploy: {to_version}"}
        },
        "spec": {
            "template": {
                "metadata": {"labels": {VERSION_LABEL: to_version}},
                "spec": {
                    "containers": [
                        {
                            "name": container,
                            "env": [
                                {"name": "SIM_VERSION", "value": to_version},
                                {"name": "SIM_DEFECT", "value": defect},
                            ],
                        }
                    ]
                },
            }
        },
    }
    apps.patch_namespaced_deployment(service, namespace, patch)
    return from_version, to_version


async def roll_out_defective_version(
    service: str,
    namespace: str,
    *,
    defect: str = "bad_deploy",
    apps_api: Any = None,
) -> tuple[str, str] | None:
    """Patch the Deployment to a defective new version. Returns (from, to) or None on failure."""
    try:
        apps = apps_api or await asyncio.to_thread(_apps_api)
        from_version, to_version = await asyncio.to_thread(
            _roll_out, service, namespace, defect, apps
        )
    except Exception as exc:  # noqa: BLE001 - chaos must never take the gateway down
        log.warning("chaos.k8s_rollout_failed", service=service, error=str(exc))
        return None
    log.info(
        "chaos.k8s_bad_version_rolled_out",
        service=service,
        from_version=from_version,
        to_version=to_version,
    )
    return from_version, to_version
