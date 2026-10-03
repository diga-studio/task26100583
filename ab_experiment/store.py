"""SQLite 持久化层：实验、事件、结算结果。"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from .errors import ExperimentNotFoundError

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS experiments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    terminated INTEGER NOT NULL DEFAULT 0,
    winning_variant TEXT,
    winning_point INTEGER
);
CREATE TABLE IF NOT EXISTS variants (
    experiment_id INTEGER NOT NULL REFERENCES experiments(id),
    position INTEGER NOT NULL,
    name TEXT NOT NULL,
    is_control INTEGER NOT NULL,
    PRIMARY KEY (experiment_id, position),
    UNIQUE (experiment_id, name)
);
CREATE TABLE IF NOT EXISTS events (
    experiment_id INTEGER NOT NULL REFERENCES experiments(id),
    seq INTEGER NOT NULL,
    kind TEXT NOT NULL,
    variant TEXT,
    action_type TEXT,
    impression_seq INTEGER,
    PRIMARY KEY (experiment_id, seq)
);
CREATE TABLE IF NOT EXISTS settlements (
    experiment_id INTEGER NOT NULL REFERENCES experiments(id),
    point INTEGER NOT NULL,
    stats_json TEXT NOT NULL,
    qualified_json TEXT NOT NULL,
    winning_variant TEXT,
    PRIMARY KEY (experiment_id, point)
);
"""


@dataclass(frozen=True)
class VariantRecord:
    position: int
    name: str
    is_control: bool


@dataclass(frozen=True)
class ExperimentRecord:
    experiment_id: int
    variants: tuple[VariantRecord, ...]
    terminated: bool
    winning_variant: str | None
    winning_point: int | None


@dataclass(frozen=True)
class EventRecord:
    seq: int
    kind: str  # "impression" | "action"
    variant: str | None
    action_type: str | None  # "consent" | "reject" | "close"
    impression_seq: int | None


@dataclass(frozen=True)
class SettlementRecord:
    point: int
    stats_json: str
    qualified_variants: tuple[str, ...]
    winning_variant: str | None


def connect(path: str) -> sqlite3.Connection:
    """打开（必要时创建）数据库并初始化 schema。"""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


def create_experiment(
    conn: sqlite3.Connection, variants: list[str], control: str
) -> int:
    """创建实验，返回 experiment_id。"""
    cur = conn.execute("INSERT INTO experiments DEFAULT VALUES")
    experiment_id = cur.lastrowid
    conn.executemany(
        "INSERT INTO variants (experiment_id, position, name, is_control) "
        "VALUES (?, ?, ?, ?)",
        [
            (experiment_id, position, name, 1 if name == control else 0)
            for position, name in enumerate(variants)
        ],
    )
    conn.commit()
    return experiment_id


def get_experiment(
    conn: sqlite3.Connection, experiment_id: int
) -> ExperimentRecord:
    """读取实验；不存在时抛 ExperimentNotFoundError。"""
    row = conn.execute(
        "SELECT * FROM experiments WHERE id = ?", (experiment_id,)
    ).fetchone()
    if row is None:
        raise ExperimentNotFoundError(f"experiment {experiment_id} not found")
    variants = tuple(
        VariantRecord(
            position=v["position"],
            name=v["name"],
            is_control=bool(v["is_control"]),
        )
        for v in conn.execute(
            "SELECT * FROM variants WHERE experiment_id = ? ORDER BY position",
            (experiment_id,),
        )
    )
    return ExperimentRecord(
        experiment_id=row["id"],
        variants=variants,
        terminated=bool(row["terminated"]),
        winning_variant=row["winning_variant"],
        winning_point=row["winning_point"],
    )


def insert_events(
    conn: sqlite3.Connection, experiment_id: int, events: list[EventRecord]
) -> int:
    """幂等写入事件（同序号重复投递内容相同，直接忽略），返回新写入条数。"""
    before = conn.total_changes
    conn.executemany(
        "INSERT OR IGNORE INTO events "
        "(experiment_id, seq, kind, variant, action_type, impression_seq) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [
            (
                experiment_id,
                e.seq,
                e.kind,
                e.variant,
                e.action_type,
                e.impression_seq,
            )
            for e in events
        ],
    )
    conn.commit()
    return conn.total_changes - before


def max_contiguous_seq(conn: sqlite3.Connection, experiment_id: int) -> int:
    """返回已收到的从 1 开始连续序号的最大值。"""
    row = conn.execute(
        """
        WITH RECURSIVE c(seq) AS (
            SELECT 1
            WHERE EXISTS (
                SELECT 1 FROM events WHERE experiment_id = ? AND seq = 1
            )
            UNION ALL
            SELECT seq + 1 FROM c
            WHERE EXISTS (
                SELECT 1 FROM events
                WHERE experiment_id = ? AND seq = c.seq + 1
            )
        )
        SELECT COALESCE(MAX(seq), 0) AS m FROM c
        """,
        (experiment_id, experiment_id),
    ).fetchone()
    return row["m"]


def events_up_to(
    conn: sqlite3.Connection, experiment_id: int, point: int
) -> list[EventRecord]:
    """读取序号 <= point 的全部事件，按序号升序。"""
    rows = conn.execute(
        "SELECT * FROM events WHERE experiment_id = ? AND seq <= ? "
        "ORDER BY seq",
        (experiment_id, point),
    ).fetchall()
    return [
        EventRecord(
            seq=r["seq"],
            kind=r["kind"],
            variant=r["variant"],
            action_type=r["action_type"],
            impression_seq=r["impression_seq"],
        )
        for r in rows
    ]


def settled_points(conn: sqlite3.Connection, experiment_id: int) -> list[int]:
    """已结算观察点列表（升序）。"""
    rows = conn.execute(
        "SELECT point FROM settlements WHERE experiment_id = ? ORDER BY point",
        (experiment_id,),
    ).fetchall()
    return [r["point"] for r in rows]


def save_settlement(
    conn: sqlite3.Connection, experiment_id: int, record: SettlementRecord
) -> None:
    """保存一个观察点的结算结果。"""
    conn.execute(
        "INSERT INTO settlements "
        "(experiment_id, point, stats_json, qualified_json, winning_variant) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            experiment_id,
            record.point,
            record.stats_json,
            json.dumps(list(record.qualified_variants)),
            record.winning_variant,
        ),
    )
    conn.commit()


def get_settlement(
    conn: sqlite3.Connection, experiment_id: int, point: int
) -> SettlementRecord | None:
    """读取某观察点的结算结果。"""
    row = conn.execute(
        "SELECT * FROM settlements WHERE experiment_id = ? AND point = ?",
        (experiment_id, point),
    ).fetchone()
    if row is None:
        return None
    return SettlementRecord(
        point=row["point"],
        stats_json=row["stats_json"],
        qualified_variants=tuple(json.loads(row["qualified_json"])),
        winning_variant=row["winning_variant"],
    )


def mark_terminated(
    conn: sqlite3.Connection,
    experiment_id: int,
    winning_variant: str,
    winning_point: int,
) -> None:
    """标记实验终止并冻结胜者。"""
    conn.execute(
        "UPDATE experiments SET terminated = 1, winning_variant = ?, "
        "winning_point = ? WHERE id = ?",
        (winning_variant, winning_point, experiment_id),
    )
    conn.commit()
