"""Host resource monitoring and capacity validation for self-healing scaling actions.

Provides accurate RAM and CPU checks before container scale-out to prevent
host starvation or out-of-memory crashes on developer machines or small nodes.
"""

from __future__ import annotations

from typing import Any

from aiops_common.log import get_logger

log = get_logger(__name__)


def get_host_resources() -> dict[str, Any]:
    """Retrieve current host RAM and CPU utilisation metrics.
    
    Returns:
        dict with available_ram_mb, total_ram_mb, ram_percent, cpu_percent, cpu_count
    """
    try:
        import psutil

        vm = psutil.virtual_memory()
        cpu_pct = psutil.cpu_percent(interval=None)
        cpu_count = psutil.cpu_count() or 1

        return {
            "available_ram_mb": round(vm.available / (1024 * 1024), 2),
            "total_ram_mb": round(vm.total / (1024 * 1024), 2),
            "used_ram_mb": round(vm.used / (1024 * 1024), 2),
            "ram_percent": vm.percent,
            "cpu_percent": cpu_pct,
            "cpu_count": cpu_count,
        }
    except Exception as exc:
        log.warning("resources.check_failed", error=str(exc))
        return {
            "error": str(exc),
            "available_ram_mb": 0.0,
            "total_ram_mb": 0.0,
            "cpu_percent": 100.0,
        }


def check_resource_capacity(
    replica_memory_limit_mb: float = 256.0,
    min_available_ram_mb: float = 256.0,
    max_host_cpu_percent: float = 85.0,
) -> tuple[bool, str, dict[str, Any]]:
    """Validate whether the host has sufficient RAM and CPU to scale out a container.

    Args:
        replica_memory_limit_mb: Memory allocation required by the new container replica
        min_available_ram_mb: Headroom buffer that must remain free on the host
        max_host_cpu_percent: Ceiling on host CPU utilisation above which scaling is unsafe

    Returns:
        (sufficient, reason, metrics):
            sufficient: True if both RAM and CPU checks pass
            reason: Human-readable explanation of capacity or limitation
            metrics: Snapshot of evaluated host metrics
    """
    try:
        import psutil
    except ImportError:
        log.error("resources.psutil_missing")
        return False, "Host capacity could not be reliably determined: psutil not installed", {}

    try:
        metrics = get_host_resources()
        if "error" in metrics:
            return False, f"Host capacity could not be reliably determined: {metrics['error']}", metrics

        available_ram = metrics["available_ram_mb"]
        cpu_percent = metrics["cpu_percent"]
        required_ram = replica_memory_limit_mb + min_available_ram_mb

        if available_ram < required_ram:
            reason = (
                f"Insufficient RAM: {available_ram:.1f} MB available, but {required_ram:.1f} MB required "
                f"({replica_memory_limit_mb:.0f} MB container limit + {min_available_ram_mb:.0f} MB buffer)"
            )
            log.info("resources.insufficient_ram", available_mb=available_ram, required_mb=required_ram)
            return False, reason, metrics

        if cpu_percent > max_host_cpu_percent:
            reason = (
                f"Insufficient CPU: Host CPU utilisation is {cpu_percent:.1f}%, exceeding safety threshold "
                f"of {max_host_cpu_percent:.1f}%"
            )
            log.info("resources.insufficient_cpu", cpu_percent=cpu_percent, threshold=max_host_cpu_percent)
            return False, reason, metrics

        reason = (
            f"Capacity verified: {available_ram:.1f} MB RAM available (needs {required_ram:.1f} MB), "
            f"Host CPU at {cpu_percent:.1f}% (limit {max_host_cpu_percent:.1f}%)"
        )
        return True, reason, metrics

    except Exception as exc:
        log.error("resources.validation_error", error=str(exc), exc_info=True)
        return False, f"Host capacity could not be reliably determined: {exc}", {}

