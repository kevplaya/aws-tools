"""SQLite persistence for AWS audit snapshots."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class SnapshotMetadata:
    id: int
    mode: str
    generated_at: str
    saved_at: str
    resource_count: int
    recommendation_count: int
    problem_count: int

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _json_default(value: Any) -> str | float:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    raise TypeError(f"{type(value).__name__} is not JSON serializable")


class SnapshotStore:
    """Store complete reports with small searchable metadata columns."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    mode TEXT NOT NULL,
                    generated_at TEXT NOT NULL,
                    saved_at TEXT NOT NULL,
                    resource_count INTEGER NOT NULL,
                    recommendation_count INTEGER NOT NULL,
                    problem_count INTEGER NOT NULL,
                    report_json TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_snapshots_mode_id ON snapshots(mode, id DESC)"
            )

    def save(self, report: dict[str, Any]) -> SnapshotMetadata:
        generated_at = str(report.get("generated_at") or datetime.now(timezone.utc).isoformat())
        saved_at = datetime.now(timezone.utc).isoformat()
        values = {
            "mode": str(report.get("mode", "live")),
            "generated_at": generated_at,
            "saved_at": saved_at,
            "resource_count": len(report.get("resources", [])),
            "recommendation_count": len(report.get("recommendations", [])),
            "problem_count": len(report.get("problems", [])),
            # ponytail: one complete JSON snapshot avoids duplicate tables; normalize only
            # when cross-snapshot SQL analytics becomes a real requirement.
            "report_json": json.dumps(report, ensure_ascii=False, default=_json_default),
        }
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO snapshots (
                    mode, generated_at, saved_at, resource_count,
                    recommendation_count, problem_count, report_json
                ) VALUES (
                    :mode, :generated_at, :saved_at, :resource_count,
                    :recommendation_count, :problem_count, :report_json
                )
                """,
                values,
            )
            snapshot_id = int(cursor.lastrowid)
        return SnapshotMetadata(
            id=snapshot_id,
            mode=values["mode"],
            generated_at=values["generated_at"],
            saved_at=values["saved_at"],
            resource_count=values["resource_count"],
            recommendation_count=values["recommendation_count"],
            problem_count=values["problem_count"],
        )

    def latest(self, mode: str = "live") -> tuple[dict[str, Any] | None, SnapshotMetadata | None]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT id, mode, generated_at, saved_at, resource_count,
                       recommendation_count, problem_count, report_json
                FROM snapshots
                WHERE mode = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (mode,),
            ).fetchone()
        if row is None:
            return None, None
        metadata = SnapshotMetadata(
            id=row["id"],
            mode=row["mode"],
            generated_at=row["generated_at"],
            saved_at=row["saved_at"],
            resource_count=row["resource_count"],
            recommendation_count=row["recommendation_count"],
            problem_count=row["problem_count"],
        )
        return json.loads(row["report_json"]), metadata

    def count(self, mode: str = "live") -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS total FROM snapshots WHERE mode = ?", (mode,)
            ).fetchone()
        return int(row["total"])
