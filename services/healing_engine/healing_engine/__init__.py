"""Self-healing playbook engine (module 6).

Executes remediation actions in response to anomalies and incidents, with four safety rails:
- Master switch (HEALING_ENABLED): dry-run mode when disabled
- Per-target cooldown: prevents hammering the same service
- Hourly budget: limits total actions per hour
- Minimum confidence: requires RCA confidence threshold

Playbooks map anomaly kinds to Docker operations (restart, scale, rollback).
Before/after metric snapshots prove effectiveness.
"""

__version__ = "0.1.0"
