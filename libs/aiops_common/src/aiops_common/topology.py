"""The monitored DPI system's service graph, loaded from a file.

Nothing in detection, root cause analysis or healing is specific to Mojaloop: what they need
is the list of services and who calls whom. Root cause analysis walks the graph to find the
deepest failing dependency; the cascade playbook opens breakers on every caller of it. So the
graph is configuration, not code, and pointing the platform at another DPI system (an
identity stack, a commerce network, anything exposing the DPI metrics) is a new file:

    {
      "name": "my-dpi-system",
      "services": {
        "gateway":  {"calls": ["registry", "ledger"]},
        "registry": {"calls": []},
        "ledger":   {"calls": []}
      }
    }

Set ``AIOPS_TOPOLOGY_FILE`` to its path. Unset, the built-in Mojaloop graph is used, which is
what the bundled simulator runs (see ``config/topology.mojaloop.json``).

``calls`` maps a service to the services it *calls*. Failure flows the other way: a degraded
dependency makes its callers wait and fail.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from .constants import MOJALOOP_DEPENDENCIES

TOPOLOGY_FILE_ENV = "AIOPS_TOPOLOGY_FILE"


class TopologyError(ValueError):
    """The topology file is malformed or describes an impossible graph."""


@dataclass(frozen=True)
class Topology:
    name: str
    # service -> services it calls, in file order.
    dependencies: dict[str, tuple[str, ...]] = field(default_factory=dict)

    @property
    def services(self) -> tuple[str, ...]:
        return tuple(self.dependencies)

    def dependencies_of(self, service: str) -> tuple[str, ...]:
        return self.dependencies.get(service, ())

    def callers_of(self, service: str) -> list[str]:
        return sorted(s for s, deps in self.dependencies.items() if service in deps)

    def transitive_dependencies(self, services: str | list[str] | set[str]) -> set[str]:
        """Every service the given service(s) call, directly or indirectly."""
        stack = [services] if isinstance(services, str) else list(services)
        seen: set[str] = set()
        while stack:
            for dependency in self.dependencies_of(stack.pop()):
                if dependency not in seen:
                    seen.add(dependency)
                    stack.append(dependency)
        return seen

    def calls_transitively(self, caller: str, target: str) -> bool:
        return target in self.transitive_dependencies(caller)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "services": {s: {"calls": list(d)} for s, d in self.dependencies.items()},
        }


def parse_topology(data: dict[str, Any]) -> Topology:
    """Validate a topology document: known callees, no self-calls, no cycles."""
    if not isinstance(data, dict) or not isinstance(data.get("services"), dict):
        raise TopologyError("topology must be an object with a 'services' object")
    services: dict[str, Any] = data["services"]
    if not services:
        raise TopologyError("topology has no services")

    dependencies: dict[str, tuple[str, ...]] = {}
    for service, spec in services.items():
        calls = (spec or {}).get("calls", []) if isinstance(spec, dict) else None
        if not isinstance(calls, list) or not all(isinstance(c, str) for c in calls):
            raise TopologyError(f"{service}: 'calls' must be a list of service names")
        unknown = [c for c in calls if c not in services]
        if unknown:
            raise TopologyError(f"{service} calls unknown service(s): {', '.join(unknown)}")
        if service in calls:
            raise TopologyError(f"{service} calls itself")
        dependencies[service] = tuple(dict.fromkeys(calls))

    topology = Topology(name=str(data.get("name") or "dpi"), dependencies=dependencies)
    # A cycle has no "deepest" failing service, so root cause analysis cannot rank it.
    for service in dependencies:
        if service in topology.transitive_dependencies(service):
            raise TopologyError(f"dependency cycle through {service}")
    return topology


def default_topology() -> Topology:
    """The bundled Mojaloop graph (what the simulator runs)."""
    return Topology(
        name="mojaloop",
        dependencies={
            str(s): tuple(str(d) for d in deps) for s, deps in MOJALOOP_DEPENDENCIES.items()
        },
    )


def load_topology(path: str | os.PathLike[str] | None = None) -> Topology:
    path = path or os.getenv(TOPOLOGY_FILE_ENV) or None
    if path is None:
        return default_topology()
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TopologyError(f"cannot read topology file {path}: {exc}") from exc
    return parse_topology(data)


@lru_cache(maxsize=1)
def get_topology() -> Topology:
    """Process-wide topology, loaded once."""
    return load_topology()
