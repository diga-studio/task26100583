"""上层服务：创建实验、接收事件、查询结算结果。"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from fractions import Fraction
from typing import Iterable, Literal

from . import store
from .errors import InvalidEventError, InvalidExperimentConfigError
from .stats import SIGNIFICANCE_CHI2_THRESHOLD, pearson_chi2_2x2

OBSERVATION_INTERVAL = 1000
MIN_IMPRESSIONS_PER_ARM = 1000

ActionType = Literal["consent", "reject", "close"]
_ACTION_TYPES = ("consent", "reject", "close")


@dataclass(frozen=True)
class ImpressionEvent:
    seq: int
    variant: str


@dataclass(frozen=True)
class ActionEvent:
    seq: int
    impression_seq: int
    action_type: ActionType


Event = ImpressionEvent | ActionEvent


@dataclass(frozen=True)
class VariantStats:
    variant: str
    is_control: bool
    impressions: int
    consents: int

    @property
    def consent_rate(self) -> float:
        return self.consents / self.impressions if self.impressions else 0.0


def _variant_stats_to_json(stats: tuple[VariantStats, ...]) -> str:
    return json.dumps(
        [
            {
                "variant": s.variant,
                "is_control": s.is_control,
                "impressions": s.impressions,
                "consents": s.consents,
            }
            for s in stats
        ]
    )


def _variant_stats_from_json(payload: str) -> tuple[VariantStats, ...]:
    return tuple(
        VariantStats(
            variant=item["variant"],
            is_control=item["is_control"],
            impressions=item["impressions"],
            consents=item["consents"],
        )
        for item in json.loads(payload)
    )


@dataclass(frozen=True)
class ObservationResult:
    """单个观察点的结算结果。"""

    point: int
    stats: tuple[VariantStats, ...]
    qualified_variants: tuple[str, ...]
    winning_variant: str | None


@dataclass(frozen=True)
class ExperimentStatus:
    """实验当前状态查询结果。"""

    experiment_id: int
    settled_points: tuple[int, ...]
    terminated: bool
    winning_variant: str | None
    winning_point: int | None


def create_experiment(
    conn: sqlite3.Connection, variants: list[str], control: str
) -> int:
    """创建实验：2-4 个变体，其中一个为对照。返回 experiment_id。"""
    if not 2 <= len(variants) <= 4:
        raise InvalidExperimentConfigError("variants 数量必须为 2 至 4")
    if len(set(variants)) != len(variants):
        raise InvalidExperimentConfigError("variants 存在重名")
    if control not in variants:
        raise InvalidExperimentConfigError("control 必须在 variants 中")
    return store.create_experiment(conn, list(variants), control)


def _validate_event(
    event: Event, variant_names: set[str]
) -> store.EventRecord:
    if event.seq < 1:
        raise InvalidEventError("seq 必须从 1 开始")
    if isinstance(event, ImpressionEvent):
        if event.variant not in variant_names:
            raise InvalidEventError(f"未知变体: {event.variant}")
        return store.EventRecord(
            seq=event.seq,
            kind="impression",
            variant=event.variant,
            action_type=None,
            impression_seq=None,
        )
    if event.action_type not in _ACTION_TYPES:
        raise InvalidEventError(f"未知动作类型: {event.action_type}")
    if event.impression_seq >= event.seq:
        raise InvalidEventError("动作序号必须晚于对应展示序号")
    return store.EventRecord(
        seq=event.seq,
        kind="action",
        variant=None,
        action_type=event.action_type,
        impression_seq=event.impression_seq,
    )


def _settle_point(
    conn: sqlite3.Connection,
    experiment: store.ExperimentRecord,
    point: int,
) -> ObservationResult:
    events = store.events_up_to(conn, experiment.experiment_id, point)
    impressions: dict[str, int] = {v.name: 0 for v in experiment.variants}
    consents: dict[str, int] = {v.name: 0 for v in experiment.variants}
    impression_variant: dict[int, str] = {}
    for event in events:
        if event.kind == "impression":
            impressions[event.variant] += 1
            impression_variant[event.seq] = event.variant
        elif event.action_type == "consent":
            variant = impression_variant.get(event.impression_seq)
            if variant is not None:
                consents[variant] += 1

    stats = tuple(
        VariantStats(
            variant=v.name,
            is_control=v.is_control,
            impressions=impressions[v.name],
            consents=consents[v.name],
        )
        for v in experiment.variants
    )
    control = next(s for s in stats if s.is_control)

    qualified: list[VariantStats] = []
    for arm in stats:
        if arm.is_control:
            continue
        if (
            arm.impressions <= MIN_IMPRESSIONS_PER_ARM
            or control.impressions <= MIN_IMPRESSIONS_PER_ARM
        ):
            continue
        if arm.consents * control.impressions <= control.consents * arm.impressions:
            continue
        chi2 = pearson_chi2_2x2(
            arm.consents,
            arm.impressions - arm.consents,
            control.consents,
            control.impressions - control.consents,
        )
        if chi2 is None or chi2 < SIGNIFICANCE_CHI2_THRESHOLD:
            continue
        qualified.append(arm)

    winning_variant: str | None = None
    if qualified:
        # 相对对照的绝对提升最大者；提升比较等价于同意率比较（同一对照），
        # 用 Fraction 精确比较（未舍入）；相同则取创建顺序靠前者
        # （max 保留首个最大值，qualified 按创建顺序排列）。
        best = max(
            qualified,
            key=lambda arm: Fraction(arm.consents, arm.impressions),
        )
        winning_variant = best.variant

    store.save_settlement(
        conn,
        experiment.experiment_id,
        store.SettlementRecord(
            point=point,
            stats_json=_variant_stats_to_json(stats),
            qualified_variants=tuple(arm.variant for arm in qualified),
            winning_variant=winning_variant,
        ),
    )
    return ObservationResult(
        point=point,
        stats=stats,
        qualified_variants=tuple(arm.variant for arm in qualified),
        winning_variant=winning_variant,
    )


def receive_events(
    conn: sqlite3.Connection, experiment_id: int, events: Iterable[Event]
) -> list[ObservationResult]:
    """接收一批事件，返回本次新结算的观察点结果（按点升序）。"""
    experiment = store.get_experiment(conn, experiment_id)
    variant_names = {v.name for v in experiment.variants}
    records = [_validate_event(e, variant_names) for e in events]
    store.insert_events(conn, experiment_id, records)

    if experiment.terminated:
        return []

    contiguous = store.max_contiguous_seq(conn, experiment_id)
    already = set(store.settled_points(conn, experiment_id))
    results: list[ObservationResult] = []
    point = OBSERVATION_INTERVAL
    while point <= contiguous:
        if point not in already:
            result = _settle_point(conn, experiment, point)
            results.append(result)
            if result.winning_variant is not None:
                store.mark_terminated(
                    conn, experiment_id, result.winning_variant, point
                )
                break
        point += OBSERVATION_INTERVAL
    return results


def get_status(
    conn: sqlite3.Connection, experiment_id: int
) -> ExperimentStatus:
    """查询实验当前状态。"""
    experiment = store.get_experiment(conn, experiment_id)
    return ExperimentStatus(
        experiment_id=experiment.experiment_id,
        settled_points=tuple(store.settled_points(conn, experiment_id)),
        terminated=experiment.terminated,
        winning_variant=experiment.winning_variant,
        winning_point=experiment.winning_point,
    )


def get_observation(
    conn: sqlite3.Connection, experiment_id: int, point: int
) -> ObservationResult | None:
    """查询某个已结算观察点的结果；未结算返回 None。"""
    record = store.get_settlement(conn, experiment_id, point)
    if record is None:
        return None
    return ObservationResult(
        point=record.point,
        stats=_variant_stats_from_json(record.stats_json),
        qualified_variants=record.qualified_variants,
        winning_variant=record.winning_variant,
    )
