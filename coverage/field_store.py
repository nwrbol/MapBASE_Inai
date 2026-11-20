"""Хранилище полевых замеров: SQLite из стандартной библиотеки, одно соединение под блокировкой.

Полевые замеры лежат отдельно от исходного measurements.csv и никогда не попадают в result.json.
На Render (free) диск временный: при перезапуске или деплое `field.db` очищается.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import uuid
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

log = logging.getLogger(__name__)

DEFAULT_DB_PATH = "data/field.db"
ENV_DB_PATH = "FIELD_DB_PATH"
SOURCES = ("map_pin", "gps", "manual", "import")

SCHEMA = """
CREATE TABLE IF NOT EXISTS field_measurements (
  id            TEXT PRIMARY KEY,
  lat           REAL NOT NULL,
  lon           REAL NOT NULL,
  rsrp_dbm      REAL NOT NULL,
  timestamp     TEXT NOT NULL,
  source        TEXT NOT NULL CHECK (source IN ('map_pin','gps','manual','import')),
  accuracy_m    REAL,
  device_label  TEXT,
  batch_id      TEXT,
  created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_field_batch ON field_measurements(batch_id);
CREATE TABLE IF NOT EXISTS field_meta (key TEXT PRIMARY KEY, value TEXT);
"""

COLUMNS = (
    "id",
    "lat",
    "lon",
    "rsrp_dbm",
    "timestamp",
    "source",
    "accuracy_m",
    "device_label",
    "batch_id",
    "created_at",
)


def resolve_db_path(path: str | Path | None = None) -> str:
    """Путь к БД: аргумент, затем FIELD_DB_PATH, затем data/field.db (относительно корня репозитория)."""
    import os

    if path is not None:
        return str(path)
    env = os.environ.get(ENV_DB_PATH)
    if env:
        return env
    return str(Path(__file__).resolve().parent.parent / DEFAULT_DB_PATH)


class FieldStore:
    """Потокобезопасное хранилище; счётчик версии растёт при каждом изменении."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = resolve_db_path(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(SCHEMA)
            self._conn.execute("INSERT OR IGNORE INTO field_meta(key, value) VALUES ('version', '0')")
            self._conn.commit()

    # --- служебное -----------------------------------------------------------------------------

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _bump(self) -> None:
        self._conn.execute("UPDATE field_meta SET value = CAST(value AS INTEGER) + 1 WHERE key = 'version'")

    # --- запись --------------------------------------------------------------------------------

    def add_many(self, rows: Iterable[Mapping[str, Any]]) -> list[str]:
        """Добавляет уже проверенные замеры одной транзакцией; возвращает id в том же порядке."""
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        items = list(rows)
        ids = [uuid.uuid4().hex for _ in items]
        if not items:
            return ids
        data = [
            (
                rid,
                float(r["lat"]),
                float(r["lon"]),
                float(r["rsrp_dbm"]),
                str(r["timestamp"]),
                str(r["source"]),
                None if r.get("accuracy_m") is None else float(r["accuracy_m"]),
                r.get("device_label") or None,
                r.get("batch_id") or None,
                now,
            )
            for rid, r in zip(ids, items)
        ]
        with self._lock, self._conn:  # транзакция: либо все строки, либо ни одной
            self._conn.executemany(
                f"INSERT INTO field_measurements ({', '.join(COLUMNS)}) VALUES (?,?,?,?,?,?,?,?,?,?)", data
            )
            self._bump()
        log.info("Полевые замеры: добавлено %d", len(ids))
        return ids

    def delete(self, measurement_id: str) -> bool:
        with self._lock, self._conn:
            cur = self._conn.execute("DELETE FROM field_measurements WHERE id = ?", (measurement_id,))
            if cur.rowcount:
                self._bump()
        return cur.rowcount > 0

    def delete_batch(self, batch_id: str) -> int:
        with self._lock, self._conn:
            cur = self._conn.execute("DELETE FROM field_measurements WHERE batch_id = ?", (batch_id,))
            if cur.rowcount:
                self._bump()
        log.info("Полевые замеры: удалён пакет, строк %d", cur.rowcount)
        return cur.rowcount

    def clear(self) -> int:
        with self._lock, self._conn:
            cur = self._conn.execute("DELETE FROM field_measurements")
            if cur.rowcount:
                self._bump()
        log.info("Полевые замеры: очищено %d", cur.rowcount)
        return cur.rowcount

    # --- чтение --------------------------------------------------------------------------------

    def count(self, source: str | None = None) -> int:
        with self._lock:
            if source:
                row = self._conn.execute(
                    "SELECT COUNT(*) FROM field_measurements WHERE source = ?", (source,)
                ).fetchone()
            else:
                row = self._conn.execute("SELECT COUNT(*) FROM field_measurements").fetchone()
        return int(row[0])

    def count_by_source(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute("SELECT source, COUNT(*) AS n FROM field_measurements GROUP BY source").fetchall()
        out = dict.fromkeys(SOURCES, 0)
        out.update({r["source"]: int(r["n"]) for r in rows})
        return out

    def version(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT value FROM field_meta WHERE key = 'version'").fetchone()
        return int(row[0]) if row else 0

    def get(self, measurement_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM field_measurements WHERE id = ?", (measurement_id,)).fetchone()
        return dict(row) if row else None

    def list(self, limit: int = 50, offset: int = 0, source: str | None = None) -> list[dict]:
        """Самые новые сверху."""
        sql = "SELECT * FROM field_measurements"
        args: list[Any] = []
        if source:
            sql += " WHERE source = ?"
            args.append(source)
        sql += " ORDER BY rowid DESC LIMIT ? OFFSET ?"
        args += [int(limit), int(offset)]
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [dict(r) for r in rows]

    def as_dataframe(self) -> pd.DataFrame:
        """Все замеры в порядке добавления (старые сверху)."""
        with self._lock:
            rows = self._conn.execute("SELECT * FROM field_measurements ORDER BY rowid ASC").fetchall()
        return pd.DataFrame([dict(r) for r in rows], columns=list(COLUMNS))
