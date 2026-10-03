"""领域类型与纯计算逻辑（判胜规则）。"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field

OBSERVATION_INTERVAL = 1000
CHI2_THRESHOLD_95 = 3.841458820694124
MIN_IMPRESSIONS = 1000


class EventKind(enum.Enum):
    IMPRESSION = "impression"
    ACTION = "action"


class ActionType(enum.Enum):
    CONSENT = "consent"
    REJECT = "reject"
    CLOSE = "close"


class ExperimentStatus(enum.Enum):
    COLLECTING = "collecting"
    RUNNING = "running"
    CONCLUDED = "concluded"


@dataclass(frozen=True)
class Event:
    """单个业务事件。同一实验内 seq 从 1 开始连续递增。"""

    seq: int
    kind: EventKind
    variant: str | None = None
    impression_seq: int | None = None
    action: ActionType | None = None

    @staticmethod
    def impression(seq: int, variant: str) -> "Event":
        return Event(seq=seq, kind=EventKind.IMPRESSION, variant=variant)

    @staticmethod
    def action_event(seq: int, impression_seq: int, action: ActionType) -> "Event":
        return Event(
            seq=seq,
            kind=EventKind.ACTION,
            impression_seq=impression_seq,
            action=action,
        )


@dataclass(frozen=True)
class VariantStats:
    variant: str
    impressions: int
    consents: int

    @property
    def consent_rate(self) -> float:
        if self.impressions == 0:
            return 0.0
        return self.consents / self.impressions


@dataclass(frozen=True)
class ObservationResult:
    """一个观察点的结算结果。"""

    experiment_id: str
    point: int
    status: ExperimentStatus
    stats: tuple[VariantStats, ...] = field(default_factory=tuple)
    winner: str | None = None


def chi2_2x2(
    success_a: int, failure_a: int, success_b: int, failure_b: int
) -> float | None:
    """2x2 Pearson 卡方（无连续性校正）。总数为 0 时返回 None。"""
    total_success = success_a + success_b
    total_failure = failure_a + failure_b
    if total_success == 0 or total_failure == 0:
        return None
    row_a = success_a + failure_a
    row_b = success_b + failure_b
    n = row_a + row_b
    numerator = n * (success_a * failure_b - failure_a * success_b) ** 2
    denominator = row_a * row_b * total_success * total_failure
    return numerator / denominator


def decide_winner(
    stats: list[VariantStats], control: str, variant_order: list[str]
) -> str | None:
    """按规则在单个观察点上判定胜者；无合格变体返回 None。"""
    by_variant = {s.variant: s for s in stats}
    control_stats = by_variant[control]
    if control_stats.impressions <= MIN_IMPRESSIONS:
        return None

    best: str | None = None
    best_lift = 0.0
    for variant in variant_order:
        if variant == control:
            continue
        candidate = by_variant[variant]
        if candidate.impressions <= MIN_IMPRESSIONS:
            continue
        lift = candidate.consent_rate - control_stats.consent_rate
        if lift <= 0:
            continue
        chi2 = chi2_2x2(
            candidate.consents,
            candidate.impressions - candidate.consents,
            control_stats.consents,
            control_stats.impressions - control_stats.consents,
        )
        if chi2 is None or chi2 < CHI2_THRESHOLD_95:
            continue
        if best is None or lift > best_lift:
            best = variant
            best_lift = lift
    return best
