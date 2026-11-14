"""Kubernetes executor (module 10) against fake Kubernetes APIs.

No cluster is needed: the executor takes its API objects by injection, and these fakes
record every call. What is pinned here:

* restart  - a named pod is deleted; otherwise a rolling restart annotation is patched
             and the executor waits for the rollout to converge
* rollback - the previous ReplicaSet revision's template is restored (rollout undo),
             without its pod-template-hash label; no previous revision means failure
* scale    - one step at a time, max / global-cap / dry-run rails, HPA floor raised when
             an HPA owns the Deployment, revert when pods are unschedulable
* wiring   - HEALING_EXECUTOR=auto picks Kubernetes inside a pod
* chaos    - bad_deploy rolls out a genuinely new, defective revision
"""

from __future__ import annotations

from types import SimpleNamespace as NS
from typing import Any

import pytest

from aiops_common.config import HealingSettings
from aiops_common.constants import ActionStatus, ExecutionMode
from healing_engine.k8s_executor import (
    CHANGE_CAUSE_ANNOTATION,
    RESTARTED_AT_ANNOTATION,
    REVISION_ANNOTATION,
    KubernetesExecutor,
)


class ApiError(Exception):
    def __init__(self, status: int) -> None:
        super().__init__(f"HTTP {status}")
        self.status = status


def _deployment(
    name: str = "central-ledger",
    *,
    replicas: int = 1,
    revision: int = 1,
    version: str = "v1.0.4",
    converged: bool = True,
    uid: str = "dep-uid",
) -> Any:
    ready = replicas if converged else 0
    return NS(
        metadata=NS(
            name=name,
            uid=uid,
            generation=1,
            annotations={REVISION_ANNOTATION: str(revision)},
        ),
        spec=NS(
            replicas=replicas,
            selector=NS(match_labels={"app.kubernetes.io/name": name}),
            template=NS(
                metadata=NS(
                    labels={
                        "app.kubernetes.io/name": name,
                        "app.kubernetes.io/version": version,
                    }
                ),
                spec=NS(containers=[NS(name="app")]),
            ),
        ),
        status=NS(
            observed_generation=1,
            replicas=replicas,
            updated_replicas=ready,
            available_replicas=ready,
        ),
    )


def _replica_set(revision: int, version: str, owner_uid: str = "dep-uid") -> Any:
    from kubernetes.client import V1Container, V1ObjectMeta, V1PodSpec, V1PodTemplateSpec

    template = V1PodTemplateSpec(
        metadata=V1ObjectMeta(
            labels={
                "app.kubernetes.io/name": "central-ledger",
                "app.kubernetes.io/version": version,
                "pod-template-hash": f"hash{revision}",
            }
        ),
        spec=V1PodSpec(containers=[V1Container(name="app", image="aiops/mojaloop-sim:0.1.0")]),
    )
    return NS(
        metadata=NS(
            annotations={REVISION_ANNOTATION: str(revision)},
            owner_references=[NS(uid=owner_uid)],
        ),
        spec=NS(template=template),
    )


class FakeApps:
    def __init__(self, deployment: Any, replica_sets: list[Any] | None = None) -> None:
        self.deployment = deployment
        self.replica_sets = replica_sets or []
        self.patches: list[Any] = []
        self.scale_patches: list[int] = []
        self.missing = False

    def read_namespaced_deployment(self, name: str, namespace: str) -> Any:
        if self.missing:
            raise ApiError(404)
        return self.deployment

    def patch_namespaced_deployment(self, name: str, namespace: str, body: Any) -> None:
        self.patches.append(body)

    def patch_namespaced_deployment_scale(self, name: str, namespace: str, body: Any) -> None:
        replicas = body["spec"]["replicas"]
        self.scale_patches.append(replicas)
        # A healthy cluster: the new replicas schedule and become available.
        self.deployment.spec.replicas = replicas
        self.deployment.status.replicas = replicas
        self.deployment.status.updated_replicas = replicas
        self.deployment.status.available_replicas = replicas

    def list_namespaced_replica_set(self, namespace: str, label_selector: str) -> Any:
        return NS(items=self.replica_sets)


class FakeCore:
    def __init__(self, pods: list[Any] | None = None) -> None:
        self.pods = pods or []
        self.deleted: list[str] = []

    def delete_namespaced_pod(self, name: str, namespace: str, **_: Any) -> None:
        self.deleted.append(name)

    def list_namespaced_pod(self, namespace: str, label_selector: str = "") -> Any:
        return NS(items=self.pods)


class FakeAutoscaling:
    def __init__(self, hpas: list[Any] | None = None) -> None:
        self.hpas = hpas or []
        self.patches: list[Any] = []

    def list_namespaced_horizontal_pod_autoscaler(self, namespace: str) -> Any:
        return NS(items=self.hpas)

    def patch_namespaced_horizontal_pod_autoscaler(
        self, name: str, namespace: str, body: Any
    ) -> None:
        self.patches.append((name, body))


def _pod(phase: str = "Running", unschedulable: bool = False) -> Any:
    conditions = (
        [NS(type="PodScheduled", status="False", reason="Unschedulable")]
        if unschedulable
        else []
    )
    return NS(
        metadata=NS(name="central-ledger-abc"),
        status=NS(phase=phase, conditions=conditions, container_statuses=[]),
    )


def _executor(
    apps: FakeApps,
    core: FakeCore | None = None,
    autoscaling: FakeAutoscaling | None = None,
    **settings: Any,
) -> KubernetesExecutor:
    base = {"HEALING_ENABLED": True, "K8S_ROLLOUT_TIMEOUT_SECONDS": 1}
    base.update(settings)
    return KubernetesExecutor(
        namespace="aiops",
        settings=HealingSettings(**base),
        apps_api=apps,
        core_api=core or FakeCore(),
        autoscaling_api=autoscaling or FakeAutoscaling(),
        poll_interval_seconds=0.01,
    )


# ---------------------------------------------------------------------------
# restart
# ---------------------------------------------------------------------------
async def test_restart_named_pod_deletes_it():
    core = FakeCore()
    ok, msg = await _executor(FakeApps(_deployment()), core).restart_container(
        "central-ledger", instance="central-ledger-abc"
    )
    assert ok is True
    assert core.deleted == ["central-ledger-abc"]
    assert "ReplicaSet will replace it" in msg


async def test_restart_whole_service_is_a_rolling_restart():
    apps = FakeApps(_deployment())
    ok, msg = await _executor(apps).restart_container("central-ledger")
    assert ok is True, msg
    annotations = apps.patches[0]["spec"]["template"]["metadata"]["annotations"]
    assert RESTARTED_AT_ANNOTATION in annotations
    assert "rolling restart" in msg


async def test_restart_reports_failure_when_rollout_never_converges():
    apps = FakeApps(_deployment(converged=False))
    ok, msg = await _executor(apps).restart_container("central-ledger")
    assert ok is False
    assert "did not complete" in msg


async def test_restart_missing_deployment_is_a_clean_failure():
    apps = FakeApps(_deployment())
    apps.missing = True
    # Patching works on the fake, but the rollout wait reads the Deployment and gets 404.
    ok, msg = await _executor(apps).restart_container("central-ledger")
    assert ok is False
    assert "not found" in msg


# ---------------------------------------------------------------------------
# rollback (rollout undo)
# ---------------------------------------------------------------------------
async def test_rollback_restores_previous_revision_template():
    apps = FakeApps(
        _deployment(revision=3, version="v1.0.5-canary"),
        replica_sets=[
            _replica_set(1, "v1.0.3"),
            _replica_set(2, "v1.0.4"),
            _replica_set(3, "v1.0.5-canary"),
            _replica_set(9, "foreign", owner_uid="someone-else"),
        ],
    )
    ok, msg = await _executor(apps).rollback_deployment("central-ledger")

    assert ok is True, msg
    assert "revision 3 (v1.0.5-canary) to revision 2 (v1.0.4)" in msg

    patch = apps.patches[0]
    assert isinstance(patch, list), "rollback must be a JSON patch, like kubectl rollout undo"
    template = next(op for op in patch if op["path"] == "/spec/template")["value"]
    labels = template["metadata"]["labels"]
    assert labels["app.kubernetes.io/version"] == "v1.0.4"
    assert "pod-template-hash" not in labels
    cause = next(op for op in patch if op["path"] != "/spec/template")
    assert cause["path"].endswith(CHANGE_CAUSE_ANNOTATION.replace("/", "~1"))


async def test_rollback_without_previous_revision_fails():
    apps = FakeApps(_deployment(revision=1), replica_sets=[_replica_set(1, "v1.0.4")])
    ok, msg = await _executor(apps).rollback_deployment("central-ledger")
    assert ok is False
    assert "No previous revision" in msg
    assert apps.patches == []


# ---------------------------------------------------------------------------
# scale
# ---------------------------------------------------------------------------
async def test_scale_out_moves_one_replica_at_a_time():
    apps = FakeApps(_deployment(replicas=1))
    res = await _executor(apps, FakeCore([_pod()])).scale_service("central-ledger", 3)
    assert res.success is True, res.message
    assert apps.scale_patches == [2]
    assert res.metadata["status"] == ActionStatus.SUCCEEDED
    assert res.metadata["execution_mode"] == ExecutionMode.REAL
    assert res.metadata["actual_replicas_before"] == 1
    assert res.metadata["actual_replicas_after"] == 2


async def test_scale_out_capped_at_max_replicas():
    apps = FakeApps(_deployment(replicas=3))
    res = await _executor(apps, HEALING_MAX_REPLICAS=3).scale_service("central-ledger", 4)
    assert res.success is False
    assert res.metadata["status"] == ActionStatus.SKIPPED_MAX_REPLICAS
    assert apps.scale_patches == []


async def test_scale_out_blocked_by_global_pod_cap():
    apps = FakeApps(_deployment(replicas=1))
    core = FakeCore([_pod() for _ in range(5)])
    res = await _executor(apps, core, HEALING_GLOBAL_MAX_CONTAINERS=5).scale_service(
        "central-ledger", 2
    )
    assert res.success is False
    assert res.metadata["status"] == ActionStatus.SKIPPED_GLOBAL_LIMIT
    assert apps.scale_patches == []


async def test_scale_dry_run_touches_nothing():
    apps = FakeApps(_deployment(replicas=1))
    res = await _executor(apps, HEALING_ENABLED=False).scale_service("central-ledger", 2)
    assert res.metadata["status"] == ActionStatus.SKIPPED_DRY_RUN
    assert res.metadata["execution_mode"] == ExecutionMode.SIMULATED
    assert apps.scale_patches == []


async def test_scale_raises_hpa_floor_when_hpa_owns_deployment():
    apps = FakeApps(_deployment(replicas=1))
    hpa = NS(
        metadata=NS(name="central-ledger"),
        spec=NS(scale_target_ref=NS(kind="Deployment", name="central-ledger"), max_replicas=1),
    )
    autoscaling = FakeAutoscaling([hpa])
    res = await _executor(apps, FakeCore([_pod()]), autoscaling).scale_service(
        "central-ledger", 2
    )
    assert res.success is True, res.message
    name, body = autoscaling.patches[0]
    assert name == "central-ledger"
    assert body["spec"]["minReplicas"] == 2
    assert body["spec"]["maxReplicas"] == 2  # raised so min <= max stays valid
    assert "HPA floor raised" in res.message


async def test_scale_reverts_when_cluster_has_no_room():
    apps = FakeApps(_deployment(replicas=1))
    # Patching scale bumps spec, but nothing ever becomes available.
    apps.deployment.status.available_replicas = 1

    def scale(name: str, namespace: str, body: Any) -> None:
        apps.scale_patches.append(body["spec"]["replicas"])
        apps.deployment.spec.replicas = body["spec"]["replicas"]

    apps.patch_namespaced_deployment_scale = scale  # type: ignore[method-assign]
    core = FakeCore([_pod(), _pod(phase="Pending", unschedulable=True)])
    res = await _executor(apps, core).scale_service("central-ledger", 2)

    assert res.success is False
    assert res.metadata["status"] == ActionStatus.SKIPPED_INSUFFICIENT_CAPACITY
    assert apps.scale_patches == [2, 1], "must put the replica count back"


async def test_scale_in_respects_min_replicas():
    apps = FakeApps(_deployment(replicas=1))
    res = await _executor(apps).scale_service("central-ledger", 0)
    assert res.success is False
    assert res.metadata["status"] == ActionStatus.SKIPPED_MIN_REPLICAS


async def test_service_healthy_detects_crashloop():
    crashing = _pod()
    crashing.status.container_statuses = [NS(state=NS(waiting=NS(reason="CrashLoopBackOff")))]
    executor = _executor(FakeApps(_deployment()), FakeCore([crashing]))
    assert await executor.service_healthy("central-ledger") is False
    executor = _executor(FakeApps(_deployment()), FakeCore([_pod()]))
    assert await executor.service_healthy("central-ledger") is True


# ---------------------------------------------------------------------------
# wiring
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("mode", "in_pod", "expected"),
    [
        ("auto", True, "kubernetes"),
        ("auto", False, "docker"),
        ("kubernetes", False, "kubernetes"),
        ("docker", True, "docker"),
    ],
)
def test_build_executor_picks_substrate(monkeypatch, mode, in_pod, expected):
    import healing_engine.executor as executor_mod
    import healing_engine.k8s_executor as k8s_mod
    from aiops_common.config import reload_settings

    monkeypatch.setenv("HEALING_EXECUTOR", mode)
    if in_pod:
        monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.96.0.1")
    else:
        monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)

    monkeypatch.setattr(k8s_mod, "KubernetesExecutor", lambda **_: "kubernetes")
    monkeypatch.setattr(executor_mod, "DockerExecutor", lambda **_: "docker")
    assert executor_mod.build_executor(reload_settings()) == expected


# ---------------------------------------------------------------------------
# chaos: a real bad deploy
# ---------------------------------------------------------------------------
def test_version_bump():
    from api_gateway.k8s_chaos import _bump

    assert _bump("v1.0.4") == "v1.0.5-canary"
    assert _bump("v1.0.5-canary") == "v1.0.6-canary"
    assert _bump("latest").startswith("latest-canary-")


async def test_bad_deploy_rolls_out_a_defective_revision():
    from api_gateway.k8s_chaos import roll_out_defective_version

    apps = FakeApps(_deployment(version="v1.0.4"))
    result = await roll_out_defective_version("central-ledger", "aiops", apps_api=apps)

    assert result == ("v1.0.4", "v1.0.5-canary")
    template = apps.patches[0]["spec"]["template"]
    assert template["metadata"]["labels"]["app.kubernetes.io/version"] == "v1.0.5-canary"
    env = {e["name"]: e["value"] for e in template["spec"]["containers"][0]["env"]}
    assert env == {"SIM_VERSION": "v1.0.5-canary", "SIM_DEFECT": "bad_deploy"}


async def test_bad_deploy_failure_returns_none():
    from api_gateway.k8s_chaos import roll_out_defective_version

    apps = FakeApps(_deployment())
    apps.missing = True
    assert await roll_out_defective_version("central-ledger", "aiops", apps_api=apps) is None


def test_simulator_activates_shipped_defect(monkeypatch):
    import mojaloop_sim.asgi as sim

    monkeypatch.setattr(sim, "SIM_DEFECT", "bad_deploy")
    monkeypatch.setattr(sim, "SIM_VERSION", "v1.0.5-canary")
    sim.simulator.faults.clear()
    sim._apply_shipped_defect()
    active = sim.simulator.faults.active()
    try:
        assert [f.kind for f in active] == ["bad_deploy"]
        assert active[0].injected_by == "deploy:v1.0.5-canary"
    finally:
        sim.simulator.faults.clear()
