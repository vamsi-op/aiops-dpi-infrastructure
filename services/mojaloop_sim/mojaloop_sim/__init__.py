"""Simulated Mojaloop microservices.

Stands in for a real Mojaloop deployment while the AIOps platform is built. Each service
runs as its own container so later modules can restart and kill real containers.

    from mojaloop_sim.simulator import Simulator
    from mojaloop_sim.profiles import PROFILES
"""

from __future__ import annotations

__version__ = "0.1.0"
