"""SQLite 持久化与对外 API。"""

from __future__ import annotations

import sqlite3

from .core import (
    OBSERVATION_INTERVAL,
    ActionType,
    Event,
    EventKind,
    ExperimentStatus,
    ObservationResult,
    VariantStats,
    decide_winner,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS experiments (
    experiment_id TEXT PRIMARY KEY,
    control       TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'collecting',
    winner        TEXT,
    concluded_point INTEGER
);
CREATE TABLE IF NOT EXISTS variants (
    experiment_id TEXT NOT NULL REFERENCES experiments(experiment_id),
    name          TEXT NOT NULL,
    position      INTEGER NOT NULL,
    PRIMARY KEY (experiment_id, name)
);
CREATE TABLE IF NOT EXISTS events (
    experiment_id  TEXT NOT NULL REFERENCES experiments(experiment_id),
    seq            INTEGER NOT NULL,
    kind           TEXT NOT NULL,
    variant        TEXT,
    impression_seq INTEGER,
    action         TEXT,
    PRIMARY KEY (experiment_id, seq)
);
CREATE TABLE IF NOT EXISTS settlements (
    experiment_id TEXT NOT NULL REFERENCES experiments(experiment_id),
    point         INTEGER NOT NULL,
    status        TEXT NOT NULL,
    winner        TEXT,
    PRIMARY KEY (experiment_id, point)
);
CREATE TABLE IF NOT EXISTS settlement_stats (
    experiment_id TEXT NOT NULL,
    point         INTEGER NOT NULL,
    variant       TEXT NOT NULL,
    impressions   INTEGER NOT NULL,
    consents      INTEGER NOT NULL,
    PRIMARY KEY (experiment_id, point, variant)
);
"""


class BannerExperimentStore:
    """同意横幅 A/B 实验判胜结算库入口。""" 

    def __init__(self, db_path: str = ":memory:") -> None:
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def close(self) -> None:
        self._conn.close()

    def _init_schema(self) -> None:
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def _variant_order(self, experiment_id: str) -> list[str]:
        rows = self._conn.execute(
            "SELECT name FROM variants WHERE experiment_id = ? ORDER BY position",
            (experiment_id,),
        ).fetchall()
        return [r["name"] for r in rows]

    def _require_experiment(self, experiment_id: str) -> sqlite3.Row:
        row = self._conn.execute(
            "SELECT * FROM experiments WHERE experiment_id = ?", (experiment_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown experiment: {experiment_id!r}")
        return row

    def create_experiment(
        self, experiment_id: str, variants: list[str], control: str
    ) -> None:
        if not 2 <= len(variants) <= 4:
            raise ValueError("experiment must have 2 to 4 variants")
        if len(set(variants)) != len(variants):
            raise ValueError("variant names must be unique")
        if control not in variants:
            raise ValueError("control must be one of the variants")
        with self._conn:
            self._conn.execute(
                "INSERT INTO experiments (experiment_id, control) VALUES (?, ?)",
                (experiment_id, control),
            )
            self._conn.executemany(
                "INSERT INTO variants (experiment_id, name, position)"
                " VALUES (?, ?, ?)",
                [(experiment_id, name, i) for i, name in enumerate(variants)],
            )

    def _contiguous_prefix(self, experiment_id: str) -> int:
        """已收到的从 1 开始连续序号的最大值。"""
        row = self._conn.execute(
            "SELECT MIN(seq) AS first_seq, MAX(seq) AS max_seq FROM events"
            " WHERE experiment_id = ?",
            (experiment_id,),
        ).fetchone()
        if row["first_seq"] is None or row["first_seq"] > 1:
            return 0
        gap = self._conn.execute(
            "SELECT MIN(e.seq) AS gap FROM events e"
            " WHERE e.experiment_id = ? AND e.seq > 1"
            "   AND NOT EXISTS ("
            "       SELECT 1 FROM events p"
            "       WHERE p.experiment_id = e.experiment_id"
            "         AND p.seq = e.seq - 1)",
            (experiment_id,),
        ).fetchone()["gap"]
        # gap 是缺口之后第一个已收到的序号，连续段结束于 gap - 2
        return row["max_seq"] if gap is None else gap - 2

    def _stats_at(self, experiment_id: str, point: int) -> list[VariantStats]:
        impressions = {
            r["variant"]: r["n"]
            for r in self._conn.execute(
                "SELECT variant, COUNT(*) AS n FROM events"
                " WHERE experiment_id = ? AND kind = ? AND seq <= ?"
                " GROUP BY variant",
                (experiment_id, EventKind.IMPRESSION.value, point),
            )
        }
        consents = {
            r["variant"]: r["n"]
            for r in self._conn.execute(
                "SELECT i.variant AS variant, COUNT(*) AS n"
                " FROM events a JOIN events i"
                "   ON i.experiment_id = a.experiment_id"
                "  AND i.seq = a.impression_seq"
                " WHERE a.experiment_id = ? AND a.kind = ? AND a.action = ?"
                "   AND a.seq <= ?"
                " GROUP BY i.variant",
                (
                    experiment_id,
                    EventKind.ACTION.value,
                    ActionType.CONSENT.value,
                    point,
                ),
            )
        }
        return [
            VariantStats(
                variant=name,
                impressions=impressions.get(name, 0),
                consents=consents.get(name, 0),
            )
            for name in self._variant_order(experiment_id)
        ]

    def receive_events(
        self, experiment_id: str, events: list[Event]
    ) -> list[ObservationResult]:
        experiment = self._require_experiment(experiment_id)
        with self._conn:
            self._conn.executemany(
                "INSERT OR IGNORE INTO events"
                " (experiment_id, seq, kind, variant, impression_seq, action)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (
                        experiment_id,
                        e.seq,
                        e.kind.value,
                        e.variant,
                        e.impression_seq,
                        e.action.value if e.action is not None else None,
                    )
                    for e in events
                ],
            )
            if experiment["status"] == ExperimentStatus.CONCLUDED.value:
                return []
            prefix = self._contiguous_prefix(experiment_id)
            row = self._conn.execute(
                "SELECT MAX(point) AS last FROM settlements"
                " WHERE experiment_id = ?",
                (experiment_id,),
            ).fetchone()
            last_settled = row["last"] or 0
            results = []
            point = last_settled + OBSERVATION_INTERVAL
            while point <= prefix:
                result = self._settle_point(experiment, point)
                results.append(result)
                if result.status is ExperimentStatus.CONCLUDED:
                    break
                point += OBSERVATION_INTERVAL
            return results

    def _settle_point(
        self, experiment: sqlite3.Row, point: int
    ) -> ObservationResult:
        experiment_id = experiment["experiment_id"]
        control = experiment["control"]
        order = self._variant_order(experiment_id)
        stats = self._stats_at(experiment_id, point)
        winner = decide_winner(stats, control, order)
        status = (
            ExperimentStatus.CONCLUDED if winner is not None
            else ExperimentStatus.RUNNING
        )
        self._conn.execute(
            "INSERT INTO settlements (experiment_id, point, status, winner)"
            " VALUES (?, ?, ?, ?)",
            (experiment_id, point, status.value, winner),
        )
        self._conn.executemany(
            "INSERT INTO settlement_stats"
            " (experiment_id, point, variant, impressions, consents)"
            " VALUES (?, ?, ?, ?, ?)",
            [
                (experiment_id, point, s.variant, s.impressions, s.consents)
                for s in stats
            ],
        )
        if winner is not None:
            self._conn.execute(
                "UPDATE experiments SET status = ?, winner = ?,"
                " concluded_point = ? WHERE experiment_id = ?",
                (ExperimentStatus.CONCLUDED.value, winner, point, experiment_id),
            )
        else:
            self._conn.execute(
                "UPDATE experiments SET status = ? WHERE experiment_id = ?",
                (ExperimentStatus.RUNNING.value, experiment_id),
            )
        return ObservationResult(
            experiment_id=experiment_id,
            point=point,
            status=status,
            stats=tuple(stats),
            winner=winner,
        )

    def _load_settlement(
        self, experiment_id: str, point: int
    ) -> ObservationResult:
        row = self._conn.execute(
            "SELECT * FROM settlements WHERE experiment_id = ? AND point = ?",
            (experiment_id, point),
        ).fetchone()
        if row is None:
            raise KeyError(
                f"observation point {point} not settled for"
                f" experiment {experiment_id!r}"
            )
        stats = tuple(
            VariantStats(
                variant=r["variant"],
                impressions=r["impressions"],
                consents=r["consents"],
            )
            for r in self._conn.execute(
                "SELECT v.name AS variant, s.impressions, s.consents"
                " FROM settlement_stats s JOIN variants v"
                "   ON v.experiment_id = s.experiment_id"
                "  AND v.name = s.variant"
                " WHERE s.experiment_id = ? AND s.point = ?"
                " ORDER BY v.position",
                (experiment_id, point),
            )
        )
        return ObservationResult(
            experiment_id=experiment_id,
            point=row["point"],
            status=ExperimentStatus(row["status"]),
            stats=stats,
            winner=row["winner"],
        )

    def get_result(
        self, experiment_id: str, observation_point: int | None = None
    ) -> ObservationResult:
        experiment = self._require_experiment(experiment_id)
        if observation_point is not None:
            return self._load_settlement(experiment_id, observation_point)
        if experiment["status"] == ExperimentStatus.CONCLUDED.value:
            return self._load_settlement(
                experiment_id, experiment["concluded_point"]
            )
        row = self._conn.execute(
            "SELECT MAX(point) AS last FROM settlements"
            " WHERE experiment_id = ?",
            (experiment_id,),
        ).fetchone()
        if row["last"] is not None:
            return self._load_settlement(experiment_id, row["last"])
        return ObservationResult(
            experiment_id=experiment_id,
            point=0,
            status=ExperimentStatus.COLLECTING,
        )
