"""Общее хранилище полевых замеров: один SQLite-файл на весь сервер, одно соединение под блокировкой.

Полевые замеры лежат отдельно от исходного measurements.csv и никогда не попадают в result.json.
Каждое изменение пишет ровно одно событие в `field_events` в той же транзакции, что и данные.
Версия данных = MAX(seq) из журнала: по ней фронтенд узнаёт, что пора обновиться.

`device_id` — не аутентификация, а защита от случайных действий: он даёт право удалять своё и
подпись в ленте, но не влияет на видимость. Полный device_id наружу не отдаётся: он же и «ключ» владельца.

На бесплатном Render диск временный: при перезапуске или деплое `field.db` очищается.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import uuid
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, NamedTuple

import pandas as pd

log = logging.getLogger(__name__)

DEFAULT_DB_PATH = "data/field.db"
ENV_DB_PATH = "FIELD_DB_PATH"
SOURCES = ("map_pin", "gps", "manual", "import")
MAX_EVENT_IDS = 500  # больше id в событие не кладём (measurement_ids = NULL)
BUSY_TIMEOUT_MS = 5000
TAG_LEN = 8  # сколько символов device_id видно другим (для «моё/чужое» и подписи «Устройство 7f3a»)

SCHEMA = """
CREATE TABLE IF NOT EXISTS field_measurements (
  id           TEXT PRIMARY KEY,
  lat          REAL NOT NULL,
  lon          REAL NOT NULL,
  rsrp_dbm     REAL NOT NULL,
  timestamp    TEXT NOT NULL,
  source       TEXT NOT NULL CHECK (source IN ('map_pin','gps','manual','import')),
  accuracy_m   REAL,
  device_id    TEXT NOT NULL,
  author_label TEXT,
  batch_id     TEXT,
  created_at   TEXT NOT NULL,
  seq          INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_fm_created ON field_measurements(created_at);
CREATE INDEX IF NOT EXISTS ix_fm_device  ON field_measurements(device_id);
CREATE INDEX IF NOT EXISTS ix_fm_batch   ON field_measurements(batch_id);

CREATE TABLE IF NOT EXISTS field_events (
  seq             INTEGER PRIMARY KEY AUTOINCREMENT,
  type            TEXT NOT NULL CHECK (type IN ('add','delete','import','delete_batch','clear')),
  device_id       TEXT,
  author_label    TEXT,
  count           INTEGER NOT NULL,
  batch_id        TEXT,
  measurement_ids TEXT,
  summary         TEXT,
  created_at      TEXT NOT NULL
);
"""

# Порядок важен: используется в INSERT и в as_dataframe.
COLUMNS = (
    "id",
    "lat",
    "lon",
    "rsrp_dbm",
    "timestamp",
    "source",
    "accuracy_m",
    "device_id",
    "author_label",
    "batch_id",
    "created_at",
    "seq",
)
PUBLIC_COLUMNS = tuple(c for c in COLUMNS if c != "device_id")


class Change(NamedTuple):
    """Итог изменения: номер версии (seq события) и сколько замеров затронуто."""

    seq: int
    count: int


class AddResult(NamedTuple):
    ids: list[str]
    seq: int


def resolve_db_path(path: str | Path | None = None) -> str:
    """Путь к БД: аргумент, затем FIELD_DB_PATH, затем data/field.db (относительно корня репозитория)."""
    if path is not None:
        return str(path)
    env = os.environ.get(ENV_DB_PATH)
    if env:
        return env
    return str(Path(__file__).resolve().parent.parent / DEFAULT_DB_PATH)


def device_tag(device_id: str | None) -> str | None:
    """Короткий публичный след device_id (8 символов из 32 hex: подобрать остальное нельзя)."""
    return device_id.replace("-", "")[:TAG_LEN] if device_id else None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _plural(n: int, one: str, few: str, many: str) -> str:
    n10, n100 = n % 10, n % 100
    if n10 == 1 and n100 != 11:
        return one
    if 2 <= n10 <= 4 and not 12 <= n100 <= 14:
        return few
    return many


def _measurements(n: int) -> str:
    return f"{n} {_plural(n, 'замер', 'замера', 'замеров')}"


class FieldStore:
    """Потокобезопасное хранилище на SQLite. Версия растёт при каждом изменении."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = resolve_db_path(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._listeners: list[Callable[[int], None]] = []
        self._conn = sqlite3.connect(self.path, check_same_thread=False, timeout=BUSY_TIMEOUT_MS / 1000)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
            self._migrate_legacy()
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    # --- служебное -----------------------------------------------------------------------------

    def _migrate_legacy(self) -> None:
        """Старая схема (до общей базы: без device_id и журнала) пересоздаётся: там были демо-данные."""
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(field_measurements)")}
        if cols and "device_id" not in cols:
            log.info("Полевые замеры: старая схема без device_id, таблицы пересозданы")
            self._conn.executescript("DROP TABLE field_measurements; DROP TABLE IF EXISTS field_meta;")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def add_listener(self, fn: Callable[[int], None]) -> None:
        """fn(version) вызывается после фиксации каждого изменения (из потока, сделавшего запись)."""
        self._listeners.append(fn)

    def _notify(self, seq: int) -> None:
        for fn in list(self._listeners):
            try:
                fn(seq)
            except Exception:  # слушатель не должен ронять запись
                log.exception("Слушатель изменений упал")

    def _event(
        self,
        type_: str,
        device_id: str | None,
        author_label: str | None,
        count: int,
        batch_id: str | None,
        ids: list[str] | None,
        summary: str,
    ) -> int:
        ids_json = json.dumps(ids) if ids is not None and len(ids) <= MAX_EVENT_IDS else None
        cur = self._conn.execute(
            "INSERT INTO field_events (type, device_id, author_label, count, batch_id, measurement_ids, summary,"
            " created_at) VALUES (?,?,?,?,?,?,?,?)",
            (type_, device_id, author_label, count, batch_id, ids_json, summary, _now()),
        )
        return int(cur.lastrowid)

    # --- запись --------------------------------------------------------------------------------

    def add_many(
        self,
        rows: Iterable[Mapping[str, Any]],
        device_id: str,
        author_label: str | None = None,
        batch_id: str | None = None,
        summary: str | None = None,
    ) -> AddResult:
        """Добавляет уже проверенные замеры и событие одной транзакцией.

        Источник берётся из строк. Если все строки с source='import', событие имеет тип «import».
        """
        items = list(rows)
        if not items:
            return AddResult([], self.version())
        ids = [uuid.uuid4().hex for _ in items]
        now = _now()
        label = (author_label or "").strip() or None
        if batch_id is None:  # пакет можно задать и в самих строках
            batch_id = (
                next(iter({r.get("batch_id") for r in items} - {None}), None)
                if len({r.get("batch_id") for r in items}) == 1
                else None
            )
        is_import = all(r.get("source") == "import" for r in items)
        text = summary or (f"импорт {_measurements(len(items))}" if is_import else f"+{_measurements(len(items))}")
        with self._lock, self._conn:  # транзакция: либо данные и событие, либо ничего
            seq = self._event("import" if is_import else "add", device_id, label, len(items), batch_id, ids, text)
            self._conn.executemany(
                f"INSERT INTO field_measurements ({', '.join(COLUMNS)}) VALUES ({','.join('?' * len(COLUMNS))})",
                [
                    (
                        rid,
                        float(r["lat"]),
                        float(r["lon"]),
                        float(r["rsrp_dbm"]),
                        str(r["timestamp"]),
                        str(r["source"]),
                        None if r.get("accuracy_m") is None else float(r["accuracy_m"]),
                        device_id,
                        label or r.get("author_label") or None,
                        batch_id or r.get("batch_id") or None,
                        now,
                        seq,
                    )
                    for rid, r in zip(ids, items)
                ],
            )
        log.info("device %s… добавил %d", device_id[:4], len(ids))
        self._notify(seq)
        return AddResult(ids, seq)

    def delete(self, measurement_id: str, device_id: str) -> Change:
        """Удаляет свой замер. KeyError: нет такого; PermissionError: замер чужой."""
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT device_id, author_label FROM field_measurements WHERE id = ?", (measurement_id,)
            ).fetchone()
            if row is None:
                raise KeyError(measurement_id)
            if row["device_id"] != device_id:
                raise PermissionError("Можно удалять только свои замеры")
            self._conn.execute("DELETE FROM field_measurements WHERE id = ?", (measurement_id,))
            seq = self._event(
                "delete", device_id, row["author_label"], 1, None, [measurement_id], f"удалил {_measurements(1)}"
            )
        log.info("device %s… удалил 1", device_id[:4])
        self._notify(seq)
        return Change(seq, 1)

    def delete_batch(self, batch_id: str, device_id: str) -> Change:
        """Удаляет свой пакет (импорт). KeyError: пакета нет; PermissionError: в пакете есть чужие замеры."""
        with self._lock, self._conn:
            rows = self._conn.execute(
                "SELECT id, device_id, author_label FROM field_measurements WHERE batch_id = ?", (batch_id,)
            ).fetchall()
            if not rows:
                raise KeyError(batch_id)
            if any(r["device_id"] != device_id for r in rows):
                raise PermissionError("Можно удалять только свои замеры")
            self._conn.execute("DELETE FROM field_measurements WHERE batch_id = ?", (batch_id,))
            seq = self._event(
                "delete_batch",
                device_id,
                rows[0]["author_label"],
                len(rows),
                batch_id,
                [r["id"] for r in rows],
                f"отменил пакет: {_measurements(len(rows))}",
            )
        log.info("device %s… удалил пакет, строк %d", device_id[:4], len(rows))
        self._notify(seq)
        return Change(seq, len(rows))

    def clear(self) -> Change:
        """Полный сброс (только для администратора: проверка токена в API)."""
        with self._lock, self._conn:
            n = int(self._conn.execute("SELECT COUNT(*) FROM field_measurements").fetchone()[0])
            self._conn.execute("DELETE FROM field_measurements")
            seq = self._event("clear", None, None, n, None, None, f"сброс: удалено {_measurements(n)}")
        log.info("Полевые замеры: сброс, удалено %d", n)
        self._notify(seq)
        return Change(seq, n)

    # --- чтение --------------------------------------------------------------------------------

    def version(self) -> int:
        with self._lock:
            return int(self._conn.execute("SELECT COALESCE(MAX(seq), 0) FROM field_events").fetchone()[0])

    def count(self) -> dict:
        """total, by_source (все четыре ключа), authors (число устройств), last_hour (добавлено за час)."""
        hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="seconds")
        with self._lock:
            by = self._conn.execute("SELECT source, COUNT(*) AS n FROM field_measurements GROUP BY source").fetchall()
            authors = self._conn.execute("SELECT COUNT(DISTINCT device_id) FROM field_measurements").fetchone()[0]
            last_hour = self._conn.execute(
                "SELECT COUNT(*) FROM field_measurements WHERE created_at >= ?", (hour_ago,)
            ).fetchone()[0]
        by_source = dict.fromkeys(SOURCES, 0)
        by_source.update({r["source"]: int(r["n"]) for r in by})
        return {
            "total": sum(by_source.values()),
            "by_source": by_source,
            "authors": int(authors),
            "last_hour": int(last_hour),
        }

    def get(self, measurement_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM field_measurements WHERE id = ?", (measurement_id,)).fetchone()
        return dict(row) if row else None

    def list(
        self,
        limit: int = 100,
        offset: int = 0,
        device_id: str | None = None,
        since_seq: int | None = None,
        source: str | None = None,
    ) -> list[dict]:
        """Самые новые сверху. device_id — только замеры этого устройства; since_seq — добавленные позже."""
        where, args = [], []
        if device_id:
            where.append("device_id = ?")
            args.append(device_id)
        if since_seq is not None:
            where.append("seq > ?")
            args.append(int(since_seq))
        if source:
            where.append("source = ?")
            args.append(source)
        sql = "SELECT * FROM field_measurements"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY rowid DESC LIMIT ? OFFSET ?"
        args += [int(limit), int(offset)]
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [dict(r) for r in rows]

    def total(self, device_id: str | None = None, source: str | None = None) -> int:
        where, args = [], []
        if device_id:
            where.append("device_id = ?")
            args.append(device_id)
        if source:
            where.append("source = ?")
            args.append(source)
        sql = "SELECT COUNT(*) FROM field_measurements" + (" WHERE " + " AND ".join(where) if where else "")
        with self._lock:
            return int(self._conn.execute(sql, args).fetchone()[0])

    def events(self, since_seq: int = 0, limit: int = 50) -> list[dict]:
        """Лента изменений, самые новые сверху."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM field_events WHERE seq > ? ORDER BY seq DESC LIMIT ?", (int(since_seq), int(limit))
            ).fetchall()
        out = []
        for r in rows:
            e = dict(r)
            e["measurement_ids"] = json.loads(e["measurement_ids"]) if e["measurement_ids"] else None
            out.append(e)
        return out

    def as_dataframe(self) -> pd.DataFrame:
        """Все замеры в порядке добавления (старые сверху), со всеми столбцами."""
        with self._lock:
            rows = self._conn.execute("SELECT * FROM field_measurements ORDER BY rowid ASC").fetchall()
        return pd.DataFrame([dict(r) for r in rows], columns=list(COLUMNS))
