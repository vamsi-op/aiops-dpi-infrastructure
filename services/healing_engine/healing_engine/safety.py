"""Safety rails for self-healing actions.

Four independent checks before any action executes:
1. Master switch: HEALING_ENABLED must be True
2. Per-target cooldown: prevents hammering the same service
3. Hourly budget: limits total actions per hour across all services
4. Minimum confidence: requires RCA confidence >= threshold

Each rail has a distinct ActionStatus when it blocks (skipped_dry_run, skipped_cooldown,
skipped_budget, skipped_low_confidence) so the reason is auditable.
"""

from __future__ import annotations

from aiops_common.bus import EventBus
from aiops_common.config import HealingSettings
from aiops_common.constants import (
    HEALING_ATTEMPTS_KEY,
    HEALING_BUDGET_KEY,
    HEALING_COOLDOWN_KEY,
    ActionStatus,
)
from aiops_common.errors import RemediationBlockedError
from aiops_common.log import get_logger

log = get_logger(__name__)


class SafetyRails:
    """Enforces four safety checks before allowing remediation actions.
    
    All state is stored in Redis (cooldowns, budget) so the engine can restart
    without losing track of recent actions.
    """

    def __init__(
        self,
        bus: EventBus,
        settings: HealingSettings,
    ):
        self.bus = bus
        self.settings = settings

    async def check_all(
        self,
        target_service: str,
        confidence: float,
        incident_id: str | None = None,
        playbook: Any = None,
    ) -> tuple[bool, ActionStatus, str]:
        """Run all safety checks in order.
        
        Args:
            target_service: Service being healed
            confidence: RCA confidence for the action (0.0-1.0)
            incident_id: Optional incident being remediated
            playbook: Playbook proposed
            
        Returns:
            (allowed, status, reason):
                - allowed: True if all checks pass
                - status: ActionStatus.PROPOSED if allowed, else skipped_* status
                - reason: Human-readable explanation if blocked
        """
        # Check 1: Master switch (dry-run mode)
        if not self.settings.enabled:
            log.info(
                "safety.blocked_dry_run",
                target=target_service,
                reason="HEALING_ENABLED=false",
            )
            return False, ActionStatus.SKIPPED_DRY_RUN, "Dry-run mode: HEALING_ENABLED=false"

        # Check 2: Minimum confidence threshold
        if confidence < self.settings.min_confidence:
            log.info(
                "safety.blocked_low_confidence",
                target=target_service,
                confidence=confidence,
                threshold=self.settings.min_confidence,
            )
            return (
                False,
                ActionStatus.SKIPPED_LOW_CONFIDENCE,
                f"Confidence {confidence:.2f} below threshold {self.settings.min_confidence}",
            )

        # Check 3: Maximum attempts per incident
        if incident_id:
            attempt_count = await self.get_attempt_count(incident_id)
            max_attempts = getattr(self.settings, "max_attempts_per_incident", 2)
            if attempt_count >= max_attempts:
                log.info(
                    "safety.blocked_max_attempts",
                    target=target_service,
                    incident_id=incident_id,
                    attempts=attempt_count,
                    max_attempts=max_attempts,
                )
                return (
                    False,
                    ActionStatus.SKIPPED_MAX_ATTEMPTS,
                    f"Maximum remediation attempts ({max_attempts}) reached for incident {incident_id}. Manual intervention required.",
                )

        # Check 4: Per-target cooldown
        in_cooldown, cooldown_reason = await self._check_cooldown(target_service)
        if in_cooldown:
            log.info(
                "safety.blocked_cooldown",
                target=target_service,
                cooldown_seconds=self.settings.cooldown_seconds,
            )
            return False, ActionStatus.SKIPPED_COOLDOWN, cooldown_reason

        # Check 5: Hourly budget
        over_budget, budget_reason = await self._check_budget()
        if over_budget:
            log.info(
                "safety.blocked_budget",
                max_per_hour=self.settings.max_actions_per_hour,
            )
            return False, ActionStatus.SKIPPED_BUDGET, budget_reason

        # All checks passed
        log.info(
            "safety.checks_passed",
            target=target_service,
            confidence=confidence,
            incident_id=incident_id,
        )
        return True, ActionStatus.PROPOSED, ""

    async def _check_cooldown(self, target_service: str) -> tuple[bool, str]:
        """Check if target service is in cooldown period.
        
        Returns:
            (in_cooldown, reason): True if cooling down, False if available
        """
        cooldown_key = f"{HEALING_COOLDOWN_KEY}:{target_service}"
        flag = await self.bus.get_flag(cooldown_key)
        
        if flag:
            return True, f"Service {target_service} in cooldown ({self.settings.cooldown_seconds}s)"
        
        return False, ""

    async def _check_budget(self) -> tuple[bool, str]:
        """Check if hourly action budget is exhausted.
        
        Returns:
            (over_budget, reason): True if budget exceeded, False if available
        """
        # Budget is tracked in Redis with 1-hour TTL
        count = await self.bus.incr_with_ttl(
            HEALING_BUDGET_KEY,
            ttl_seconds=3600,
        )
        
        if count > self.settings.max_actions_per_hour:
            return (
                True,
                f"Hourly budget exhausted ({count}/{self.settings.max_actions_per_hour})",
            )
        
        # Decrement since we're just checking, not executing yet
        # Actual execution will increment again
        await self.bus.client.decr(HEALING_BUDGET_KEY)
        
        return False, ""

    async def get_attempt_count(self, incident_id: str) -> int:
        """Get the number of remediation attempts recorded for an incident."""
        attempts_key = f"{HEALING_ATTEMPTS_KEY}:{incident_id}"
        flag = await self.bus.get_flag(attempts_key)
        if flag and str(flag).isdigit():
            return int(flag)
        return 0

    async def record_action_executed(
        self,
        target_service: str,
        success: bool = True,
        incident_id: str | None = None,
        playbook: str | None = None,
    ) -> None:
        """Mark that an action executed.
        
        Sets cooldown for the target service (longer for success, failure cooldown for failure)
        and increments budget and attempt counters.
        """
        cooldown_duration = (
            self.settings.cooldown_seconds if success else getattr(self.settings, "failure_cooldown_seconds", 120)
        )
        cooldown_key = f"{HEALING_COOLDOWN_KEY}:{target_service}"
        await self.bus.set_flag(
            cooldown_key,
            value="1",
            ttl_seconds=cooldown_duration,
        )
        
        # Increment budget counter
        await self.bus.incr_with_ttl(
            HEALING_BUDGET_KEY,
            ttl_seconds=3600,
        )

        # Track attempts per incident
        if incident_id:
            attempts_key = f"{HEALING_ATTEMPTS_KEY}:{incident_id}"
            await self.bus.incr_with_ttl(
                attempts_key,
                ttl_seconds=3600,
            )
        
        log.info(
            "safety.action_recorded",
            target=target_service,
            success=success,
            incident_id=incident_id,
            playbook=playbook,
            cooldown_seconds=cooldown_duration,
        )
