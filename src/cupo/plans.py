"""Plan definitions and result types.

v0.1 takes plans as a plain dict. The YAML loader described in the README lands
in v0.2 and will produce exactly these objects, so nothing here has to change
when it does.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from .windows import VALID_WINDOWS, InvalidWindow

OnLimit = Literal["block", "degrade", "bill"]

UNLIMITED = "unlimited"


class PlanError(ValueError):
    pass


@dataclass(frozen=True)
class Entitlement:
    """What one plan grants for one feature."""

    feature: str
    limit: int | None  # None means unlimited
    window: str
    on_limit: OnLimit = "block"
    overage_price: float | None = None  # price per unit, only used when on_limit="bill"

    @property
    def unlimited(self) -> bool:
        return self.limit is None


@dataclass(frozen=True)
class CheckResult:
    """The answer to 'is this customer allowed to do this, right now?'"""

    allowed: bool
    feature: str
    used: int
    limit: int | None
    resets_at: datetime | None
    degraded: bool = False
    overage: bool = False
    reason: str | None = None
    upgrade_url: str | None = None

    @property
    def remaining(self) -> int | None:
        if self.limit is None:
            return None
        return max(0, self.limit - self.used)

    def __bool__(self) -> bool:
        return self.allowed


def _parse_entitlement(feature: str, raw: Any) -> Entitlement:
    # pdf_export: false  -> feature denied outright
    if raw is False:
        return Entitlement(feature=feature, limit=0, window="month")

    # pdf_export: true   -> feature granted with no metering
    if raw is True or raw == UNLIMITED:
        return Entitlement(feature=feature, limit=None, window="month")

    if not isinstance(raw, dict):
        raise PlanError(
            f"feature {feature!r}: expected a dict, True, False or 'unlimited', "
            f"got {type(raw).__name__}"
        )

    if "limit" not in raw:
        raise PlanError(f"feature {feature!r}: missing 'limit'")

    limit = raw["limit"]
    if limit == UNLIMITED:
        limit = None
    elif not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
        raise PlanError(
            f"feature {feature!r}: 'limit' must be a non-negative int or 'unlimited'"
        )

    window = raw.get("window", "month")
    if window not in VALID_WINDOWS:
        raise InvalidWindow(
            f"feature {feature!r}: unknown window {window!r}; "
            f"expected one of {', '.join(VALID_WINDOWS)}"
        )

    on_limit = raw.get("on_limit", "block")
    if on_limit not in ("block", "degrade", "bill"):
        raise PlanError(
            f"feature {feature!r}: on_limit must be 'block', 'degrade' or 'bill'"
        )

    overage_price = raw.get("overage_price")
    if on_limit == "bill" and overage_price is None:
        raise PlanError(
            f"feature {feature!r}: on_limit='bill' requires 'overage_price'"
        )

    return Entitlement(
        feature=feature,
        limit=limit,
        window=window,
        on_limit=on_limit,
        overage_price=overage_price,
    )


class Plans:
    """A parsed, validated set of plans.

    Parsing happens once at startup and raises loudly. A typo in a plan name
    should crash the process on boot, not silently grant unlimited access at
    three in the morning.
    """

    def __init__(self, raw: dict[str, dict[str, Any]]):
        if not isinstance(raw, dict) or not raw:
            raise PlanError("plans must be a non-empty dict")

        self._plans: dict[str, dict[str, Entitlement]] = {}
        for plan_name, features in raw.items():
            if not isinstance(features, dict):
                raise PlanError(f"plan {plan_name!r}: expected a dict of features")
            self._plans[plan_name] = {
                feature: _parse_entitlement(feature, spec)
                for feature, spec in features.items()
            }

    def entitlement(self, plan: str, feature: str) -> Entitlement:
        try:
            features = self._plans[plan]
        except KeyError:
            raise PlanError(
                f"unknown plan {plan!r}; known plans: {', '.join(sorted(self._plans))}"
            ) from None
        try:
            return features[feature]
        except KeyError:
            raise PlanError(
                f"plan {plan!r} does not define feature {feature!r}"
            ) from None

    def features(self, plan: str) -> dict[str, Entitlement]:
        try:
            return dict(self._plans[plan])
        except KeyError:
            raise PlanError(f"unknown plan {plan!r}") from None

    @property
    def names(self) -> list[str]:
        return sorted(self._plans)
