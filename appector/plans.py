"""Removal/update plans: binding user consent to the executed transaction (S4).

Every prepare_* flow returns a :class:`Plan` describing the exact package
set the user previewed and confirmed; the matching execute_* flow re-runs
the simulation and refuses to run when reality drifted from the plan.
Nothing may execute a privileged transaction that was not previewed.
"""

import hashlib
import json
from dataclasses import dataclass, field
from collections.abc import Iterable

from .policy import (
    APT_MAX_REMOVALS,
    _base_package_name,
    is_critical_apt_package_name,
)


@dataclass
class Plan:
    """A confirmed transaction between prepare_* and execute_*."""

    action: str
    package_ids: list[str] = field(default_factory=list)
    removed_preview: list[str] = field(default_factory=list)
    text: str = ""
    plan_hash: str = ""

    def __post_init__(self):
        if not self.plan_hash:
            self.plan_hash = self.compute_hash()

    def compute_hash(self) -> str:
        payload = json.dumps(
            {
                "action": self.action,
                "package_ids": sorted(self.package_ids),
                "removed_preview": sorted(self.removed_preview),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def same_identity(self, other: "Plan") -> bool:
        return isinstance(other, Plan) and other.plan_hash == self.plan_hash


def removal_grew(planned: Iterable[str], actual: Iterable[str]) -> list[str]:
    """Packages in the re-simulation that the confirmed plan did not contain."""
    planned_set = set(planned or [])
    return sorted(
        package
        for package in (actual or [])
        if package not in planned_set
    )


def check_apt_removal_plan(
    plan: Plan | None,
    package_ids: list[str],
    actual_removed: list[str],
) -> tuple[bool, str]:
    """Shared policy gate for APT removal executions (S4).

    * a plan is mandatory (consent must exist and be bound to a preview);
    * the executed package set must match the confirmed one;
    * the re-simulation must not have grown beyond the preview;
    * critical packages and the >``APT_MAX_REMOVALS`` cascade still apply.
    """
    if plan is None:
        return False, (
            "A removal plan from a preview is required. Preview the removal "
            "again and confirm the plan before executing it."
        )

    if plan.action not in {"remove", "purge", "autoremove", "batch-remove"}:
        return False, f"Plan action {plan.action!r} does not match this operation."

    extra = removal_grew(plan.removed_preview, actual_removed)
    if extra:
        return False, (
            "The APT transaction changed since it was confirmed — the "
            "re-simulation now also removes:\n\n"
            + "\n".join(f"• {name}" for name in extra)
            + "\n\nNothing was removed; preview the removal again."
        )

    missing = [
        package_id
        for package_id in package_ids
        if _base_package_name(package_id)
        not in {_base_package_name(p) for p in actual_removed}
    ]
    if missing:
        return False, (
            "APT no longer confirms removal for:\n\n"
            + "\n".join(f"• {name}" for name in missing)
        )

    critical = [p for p in actual_removed if is_critical_apt_package_name(p)]
    if critical:
        return False, (
            "Blocked by safety policy.\n\n"
            "APT simulation says the following critical packages would be "
            "removed:\n\n"
            + "\n".join(f"• {name}" for name in critical)
        )

    if len(actual_removed) > APT_MAX_REMOVALS:
        return False, (
            "Blocked by safety policy.\n\n"
            f"APT simulation says {len(actual_removed)} packages would be "
            f"removed; the safety limit is {APT_MAX_REMOVALS}."
        )

    return True, ""


def check_apt_upgrade_plan(actual_removed: list[str]) -> tuple[bool, str]:
    """An update/upgrade must never remove packages (S4)."""
    if actual_removed:
        return False, (
            "Blocked: the upgrade transaction would remove packages "
            "(updates must not remove anything):\n\n"
            + "\n".join(f"• {name}" for name in actual_removed)
        )
    return True, ""
