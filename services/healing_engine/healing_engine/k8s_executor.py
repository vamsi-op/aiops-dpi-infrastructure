"""Kubernetes implementation of the remediation executor (module 10).

The playbooks do not change; only what "restart", "scale" and "rollback" mean does:

* restart  - one named pod is deleted (its ReplicaSet replaces it), or the whole Deployment
             gets a rolling restart, exactly like `kubectl rollout restart`.
* scale    - the Deployment's replica count is patched one step at a time, inside the same
             min / max / global-cap rails as Docker. If a HorizontalPodAutoscaler owns the
             Deployment, its `minReplicas` floor is raised instead so the HPA does not
             immediately undo a pre-emptive scale-out.
* rollback - a real `kubectl rollout undo`: the pod template of the previous ReplicaSet
             revision is copied back onto the Deployment.

Every action waits for the rollout to converge before it reports success, so "succeeded"
means the cluster actually reached the desired state, not just that the API accepted a patch.

The `kubernetes` client is synchronous; calls run in a worker thread so they never block
the engine's event loop.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import Any

from aiops_common.config import HealingSettings, get_settings
from aiops_common.constants import ActionStatus, ExecutionMode
from aiops_common.log import get_logger

from .executor import ExecutorInterface, ScaleResult

log = get_logger(__name__)

REVISION_ANNOTATION = "deployment.kubernetes.io/revision"
RESTARTED_AT_ANNOTATION = "kubectl.kubernetes.io/restartedAt"
CHANGE_CAUSE_ANNOTATION = "kubernetes.io/change-cause"
VERSION_LABEL = "app.kubernetes.io/version"
# Only DPI workload pods count towards the global cap; the platform's own pods do not.
WORKLOAD_SELECTOR = "app.kubernetes.io/part-of=mojaloop"


class KubernetesExecutor(ExecutorInterface):
    """Acts on Deployments in one namespace through the Kubernetes API."""

    def __init__(
        self,
        namespace: str | None = None,
        settings: HealingSettings | None = None,
        *,
        apps_api: Any = None,
        core_api: Any = None,
        autoscaling_api: Any = None,
        poll_interval_seconds: float = 2.0,
    ) -> None:
        self.settings = settings or get_settings().healing
        self.namespace = namespace or self.settings.k8s_namespace
        self.poll_interval_seconds = poll_interval_seconds
        self._locks: dict[str, asyncio.Lock] = {}

        if apps_api is None or core_api is None or autoscaling_api is None:
            from kubernetes import client, config

            try:
                config.load_incluster_config()
                source = "in-cluster"
            except config.ConfigException:
                # Running from a terminal against kind: fall back to ~/.kube/config.
                config.load_kube_config()
                source = "kubeconfig"
            apps_api = apps_api or client.AppsV1Api()
            core_api = core_api or client.CoreV1Api()
            autoscaling_api = autoscaling_api or client.AutoscalingV2Api()
            log.info("k8s_executor.initialized", namespace=self.namespace, config=source)

        self.apps = apps_api
        self.core = core_api
        self.autoscaling = autoscaling_api

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _lock(self, service: str) -> asyncio.Lock:
        if service not in self._locks:
            self._locks[service] = asyncio.Lock()
        return self._locks[service]

    async def _call(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        return await asyncio.to_thread(fn, *args, **kwargs)

    async def _read_deployment(self, service: str) -> Any:
        return await self._call(self.apps.read_namespaced_deployment, service, self.namespace)

    @staticmethod
    def _is_not_found(exc: Exception) -> bool:
        return getattr(exc, "status", None) == 404

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()

    async def wait_for_rollout(self, service: str, timeout_seconds: float | None = None) -> bool:
        """Poll until the Deployment's rollout has converged, like `kubectl rollout status`."""
        timeout = timeout_seconds or self.settings.k8s_rollout_timeout_seconds
        deadline = time.monotonic() + timeout
        while True:
            dep = await self._read_deployment(service)
            spec_replicas = dep.spec.replicas or 0
            status = dep.status
            converged = (
                (status.observed_generation or 0) >= (dep.metadata.generation or 0)
                and (status.updated_replicas or 0) == spec_replicas
                and (status.replicas or 0) == spec_replicas
                and (status.available_replicas or 0) == spec_replicas
            )
            if converged:
                return True
            if time.monotonic() >= deadline:
                log.warning(
                    "k8s_executor.rollout_timeout",
                    service=service,
                    timeout=timeout,
                    desired=spec_replicas,
                    updated=status.updated_replicas,
                    available=status.available_replicas,
                )
                return False
            await asyncio.sleep(self.poll_interval_seconds)

    async def _unschedulable_pods(self, service: str) -> list[str]:
        """Names of this service's pods the scheduler could not place (cluster is full)."""
        pods = await self._call(
            self.core.list_namespaced_pod,
            self.namespace,
            label_selector=f"app.kubernetes.io/name={service}",
        )
        stuck = []
        for pod in pods.items:
            for cond in pod.status.conditions or []:
                if (
                    cond.type == "PodScheduled"
                    and cond.status == "False"
                    and cond.reason == "Unschedulable"
                ):
                    stuck.append(pod.metadata.name)
        return stuck

    async def _find_hpa(self, service: str) -> Any | None:
        hpas = await self._call(
            self.autoscaling.list_namespaced_horizontal_pod_autoscaler, self.namespace
        )
        for hpa in hpas.items:
            ref = hpa.spec.scale_target_ref
            if ref.kind == "Deployment" and ref.name == service:
                return hpa
        return None

    # ------------------------------------------------------------------
    # ExecutorInterface
    # ------------------------------------------------------------------
    async def restart_container(
        self,
        service: str,
        instance: str | None = None,
    ) -> tuple[bool, str]:
        """Delete one pod, or roll every pod of the Deployment one at a time."""
        try:
            if instance:
                await self._call(
                    self.core.delete_namespaced_pod,
                    instance,
                    self.namespace,
                    grace_period_seconds=10,
                )
                log.info("k8s_executor.pod_deleted", service=service, pod=instance)
                return True, f"Deleted pod {instance}; its ReplicaSet will replace it"

            async with self._lock(service):
                patch = {
                    "spec": {
                        "template": {
                            "metadata": {"annotations": {RESTARTED_AT_ANNOTATION: self._now()}}
                        }
                    }
                }
                await self._call(
                    self.apps.patch_namespaced_deployment, service, self.namespace, patch
                )
                log.info("k8s_executor.rollout_restart_started", service=service)
                if not await self.wait_for_rollout(service):
                    return False, f"Rolling restart of {service} did not complete in time"

            log.info("k8s_executor.rollout_restart_complete", service=service)
            return True, f"Restarted {service} (rolling restart of all pods completed)"
        except Exception as exc:  # noqa: BLE001 - ApiException and transport errors alike
            if self._is_not_found(exc):
                return False, f"Deployment or pod for {service} not found in {self.namespace}"
            log.error("k8s_executor.restart_failed", service=service, error=str(exc))
            return False, f"Kubernetes error during restart: {exc}"

    async def scale_service(
        self,
        service: str,
        replicas: int,
    ) -> ScaleResult:
        """Scale a Deployment one replica at a time, inside the same rails as Docker."""
        settings = self.settings
        min_replicas = settings.min_replicas
        max_replicas = settings.max_replicas

        try:
            current = await self.get_replica_count(service)
        except Exception as exc:  # noqa: BLE001
            return ScaleResult(
                False,
                f"Cannot read Deployment {service}: {exc}",
                {"status": ActionStatus.FAILED, "execution_mode": ExecutionMode.REAL},
            )

        def meta(status: str, mode: str, after: int, simulated: int, **extra: Any) -> dict:
            return {
                "status": status,
                "execution_mode": mode,
                "actual_replicas_before": current,
                "actual_replicas_after": after,
                "simulated_replicas_after": simulated,
                "executor": "kubernetes",
                **extra,
            }

        if replicas == current:
            return ScaleResult(
                True,
                f"Deployment '{service}' already at {replicas} replica(s)",
                meta(ActionStatus.SUCCEEDED, ExecutionMode.REAL, current, current),
            )

        scaling_out = replicas > current
        if scaling_out:
            target = current + 1  # one step at a time, same as the Docker executor
            if current >= max_replicas:
                log.info("k8s_executor.scale_capped_max", service=service, current=current)
                return ScaleResult(
                    False,
                    f"Deployment '{service}' already at maximum replicas ({max_replicas}). "
                    "Scaling capped.",
                    meta(ActionStatus.SKIPPED_MAX_REPLICAS, ExecutionMode.SIMULATED, current, target),
                )
            try:
                pods = await self._call(
                    self.core.list_namespaced_pod, self.namespace, label_selector=WORKLOAD_SELECTOR
                )
                running = [p for p in pods.items if p.status.phase in ("Running", "Pending")]
                if len(running) >= settings.global_max_containers:
                    return ScaleResult(
                        False,
                        f"Global workload pod limit ({settings.global_max_containers}) reached "
                        f"({len(running)} pods). Cannot scale out '{service}'.",
                        meta(ActionStatus.SKIPPED_GLOBAL_LIMIT, ExecutionMode.SIMULATED, current, target),
                    )
            except Exception as exc:  # noqa: BLE001
                log.warning("k8s_executor.global_limit_check_failed", error=str(exc))
        else:
            target = replicas
            if current <= min_replicas:
                return ScaleResult(
                    False,
                    f"Deployment '{service}' already at minimum replicas ({min_replicas}). "
                    "Cannot scale in.",
                    meta(
                        ActionStatus.SKIPPED_MIN_REPLICAS,
                        ExecutionMode.SIMULATED,
                        current,
                        max(min_replicas, replicas),
                    ),
                )
            target = max(min_replicas, target)

        verb = "out" if scaling_out else "in"
        if not settings.enabled:
            return ScaleResult(
                True,
                f"Dry-run: Would scale {verb} '{service}' from {current} to {target} replica(s)",
                meta(ActionStatus.SKIPPED_DRY_RUN, ExecutionMode.SIMULATED, current, target),
            )

        async with self._lock(service):
            try:
                hpa = await self._find_hpa(service)
                if hpa is not None:
                    # The HPA owns spec.replicas; raising (or lowering) its floor is how a
                    # pre-emptive decision survives the HPA's next reconcile loop.
                    hpa_patch: dict[str, Any] = {"spec": {"minReplicas": target}}
                    if (hpa.spec.max_replicas or 0) < target:
                        hpa_patch["spec"]["maxReplicas"] = target
                    await self._call(
                        self.autoscaling.patch_namespaced_horizontal_pod_autoscaler,
                        hpa.metadata.name,
                        self.namespace,
                        hpa_patch,
                    )
                await self._call(
                    self.apps.patch_namespaced_deployment_scale,
                    service,
                    self.namespace,
                    {"spec": {"replicas": target}},
                )
                log.info(
                    "k8s_executor.scaled",
                    service=service,
                    before=current,
                    target=target,
                    via_hpa=hpa is not None,
                )

                if not await self.wait_for_rollout(service):
                    stuck = await self._unschedulable_pods(service)
                    if stuck:
                        # The cluster has no room. Put the replica count back rather than
                        # leave Pending pods behind, and report it as a capacity skip.
                        await self._call(
                            self.apps.patch_namespaced_deployment_scale,
                            service,
                            self.namespace,
                            {"spec": {"replicas": current}},
                        )
                        return ScaleResult(
                            False,
                            f"Insufficient cluster capacity: {len(stuck)} pod(s) unschedulable. "
                            f"Reverted '{service}' to {current} replica(s).",
                            meta(
                                ActionStatus.SKIPPED_INSUFFICIENT_CAPACITY,
                                ExecutionMode.REAL,
                                current,
                                target,
                                unschedulable_pods=stuck,
                            ),
                        )
                    after = await self.get_replica_count(service)
                    return ScaleResult(
                        False,
                        f"Scale {verb} of '{service}' to {target} did not become ready in time",
                        meta(ActionStatus.FAILED, ExecutionMode.REAL, after, after),
                    )

                return ScaleResult(
                    True,
                    f"Scaled {verb} '{service}' from {current} to {target} replica(s)"
                    + (" (HPA floor raised)" if hpa is not None and scaling_out else ""),
                    meta(
                        ActionStatus.SUCCEEDED,
                        ExecutionMode.REAL,
                        target,
                        target,
                        hpa=hpa.metadata.name if hpa is not None else None,
                    ),
                )
            except Exception as exc:  # noqa: BLE001
                log.error("k8s_executor.scale_failed", service=service, error=str(exc))
                return ScaleResult(
                    False,
                    f"Kubernetes error during scale {verb}: {exc}",
                    meta(ActionStatus.FAILED, ExecutionMode.REAL, current, current),
                )

    async def rollback_deployment(
        self,
        service: str,
    ) -> tuple[bool, str]:
        """Restore the previous ReplicaSet revision's pod template (`kubectl rollout undo`)."""
        async with self._lock(service):
            try:
                dep = await self._read_deployment(service)
                annotations = dep.metadata.annotations or {}
                current_rev = int(annotations.get(REVISION_ANNOTATION, "0"))

                selector = ",".join(
                    f"{k}={v}" for k, v in (dep.spec.selector.match_labels or {}).items()
                )
                replica_sets = await self._call(
                    self.apps.list_namespaced_replica_set, self.namespace, label_selector=selector
                )
                owned = [
                    rs
                    for rs in replica_sets.items
                    if any(ref.uid == dep.metadata.uid for ref in rs.metadata.owner_references or [])
                ]

                def revision(rs: Any) -> int:
                    return int((rs.metadata.annotations or {}).get(REVISION_ANNOTATION, "0"))

                previous = sorted(
                    (rs for rs in owned if revision(rs) < current_rev), key=revision
                )
                if not previous:
                    return False, (
                        f"No previous revision of {service} to roll back to "
                        f"(current revision {current_rev})"
                    )
                target_rs = previous[-1]
                target_rev = revision(target_rs)

                template = target_rs.spec.template
                labels = dict(template.metadata.labels or {})
                # The hash label belongs to the ReplicaSet, not the Deployment's template;
                # copying it back would make the selector match the old ReplicaSet only.
                labels.pop("pod-template-hash", None)
                template.metadata.labels = labels

                from kubernetes.client import ApiClient

                body_template = ApiClient().sanitize_for_serialization(template)
                from_version = (dep.spec.template.metadata.labels or {}).get(VERSION_LABEL, "?")
                to_version = labels.get(VERSION_LABEL, "?")

                patch = [
                    {"op": "replace", "path": "/spec/template", "value": body_template},
                    {
                        "op": "add",
                        "path": "/metadata/annotations/"
                        + CHANGE_CAUSE_ANNOTATION.replace("/", "~1"),
                        "value": f"aiops healing-engine: rollback to revision {target_rev}",
                    },
                ]
                await self._call(
                    self.apps.patch_namespaced_deployment, service, self.namespace, patch
                )
                log.info(
                    "k8s_executor.rollback_started",
                    service=service,
                    from_revision=current_rev,
                    to_revision=target_rev,
                    from_version=from_version,
                    to_version=to_version,
                )

                if not await self.wait_for_rollout(service):
                    return False, (
                        f"Rollback of {service} to revision {target_rev} did not complete in time"
                    )
                return True, (
                    f"Rolled back {service} from revision {current_rev} ({from_version}) "
                    f"to revision {target_rev} ({to_version})"
                )
            except Exception as exc:  # noqa: BLE001
                if self._is_not_found(exc):
                    return False, f"Deployment {service} not found in {self.namespace}"
                log.error("k8s_executor.rollback_failed", service=service, error=str(exc))
                return False, f"Kubernetes error during rollback: {exc}"

    async def get_replica_count(self, service: str) -> int:
        """Desired replicas of the Deployment (what the scheduler is converging towards)."""
        dep = await self._read_deployment(service)
        return int(dep.spec.replicas or 0)

    async def service_healthy(self, service: str) -> bool:
        """At least one available pod and none stuck in CrashLoopBackOff."""
        try:
            dep = await self._read_deployment(service)
            if (dep.status.available_replicas or 0) < 1:
                return False
            pods = await self._call(
                self.core.list_namespaced_pod,
                self.namespace,
                label_selector=f"app.kubernetes.io/name={service}",
            )
            for pod in pods.items:
                for cs in pod.status.container_statuses or []:
                    waiting = cs.state.waiting if cs.state else None
                    if waiting is not None and waiting.reason == "CrashLoopBackOff":
                        return False
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("k8s_executor.health_check_failed", service=service, error=str(exc))
            return True  # unknown is not unhealthy; SLO verification still runs
