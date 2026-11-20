"""Общее хранилище полевых замеров (SQLite): права, журнал событий, версия."""

import contextlib
import sqlite3
import threading

import pytest

from coverage.field_store import FieldStore, device_tag

A = "11111111-1111-4111-8111-111111111111"
B = "22222222-2222-4222-8222-222222222222"


def row(i=0, source="manual", batch=None, **kw):
    base = {
        "lat": 42.85 + i * 1e-4,
        "lon": 74.58,
        "rsrp_dbm": -100.0 - i,
        "timestamp": f"2026-11-20T22:{i % 60:02d}:00+06:00",
        "source": source,
        "batch_id": batch,
    }
    return base | kw


@pytest.fixture
def store(tmp_path):
    s = FieldStore(tmp_path / "field.db")
    yield s
    s.close()


def test_empty_store(store):
    c = store.count()
    assert c["total"] == 0 and c["authors"] == 0 and c["last_hour"] == 0
    assert c["by_source"] == {"map_pin": 0, "gps": 0, "manual": 0, "import": 0}
    assert store.version() == 0 and store.list() == [] and store.events() == []
    df = store.as_dataframe()
    assert df.empty and {"device_id", "author_label", "seq"} <= set(df.columns)


def test_add_list_newest_first_and_seq(store):
    r = store.add_many([row(0), row(1), row(2)], A, "Айбек")
    assert len(r.ids) == len(set(r.ids)) == 3 and r.seq == 1
    listed = store.list()
    assert [x["id"] for x in listed] == r.ids[::-1]
    assert {x["seq"] for x in listed} == {1} and {x["author_label"] for x in listed} == {"Айбек"}
    assert store.list(limit=1, offset=1)[0]["id"] == r.ids[1]
    assert store.get(r.ids[0])["rsrp_dbm"] == -100.0 and store.get("nope") is None


def test_filters_since_seq_device_source(store):
    store.add_many([row(0, "gps", accuracy_m=12.5)], A)
    store.add_many([row(1, "map_pin"), row(2, "gps")], B)
    assert len(store.list(device_id=A)) == 1 and len(store.list(device_id=B)) == 2
    assert [x["seq"] for x in store.list(since_seq=1)] == [2, 2]
    assert [x["source"] for x in store.list(source="map_pin")] == ["map_pin"]
    assert store.total() == 3 and store.total(device_id=A) == 1 and store.total(source="gps") == 2
    c = store.count()
    assert c["total"] == 3 and c["by_source"]["gps"] == 2 and c["authors"] == 2 and c["last_hour"] == 3
    assert store.list(source="gps")[-1]["accuracy_m"] == 12.5


def test_delete_own_foreign_missing(store):
    (rid,) = store.add_many([row(0)], A).ids
    with pytest.raises(PermissionError, match="только свои"):
        store.delete(rid, B)
    assert store.get(rid) is not None
    with pytest.raises(KeyError):
        store.delete("nope", A)
    ch = store.delete(rid, A)
    assert ch.count == 1 and store.get(rid) is None


def test_delete_batch_rules(store):
    store.add_many([row(0, "import", "B1"), row(1, "import", "B1")], A)
    store.add_many([row(2, "import", "B2")], B)
    with pytest.raises(PermissionError):
        store.delete_batch("B1", B)
    with pytest.raises(KeyError):
        store.delete_batch("nope", A)
    assert store.delete_batch("B1", A).count == 2
    assert store.total() == 1


def test_version_grows_on_every_change_only(store):
    assert store.version() == 0
    ids = store.add_many([row(0), row(1, "import", "B")], A).ids
    assert store.version() == 1  # один пакет = одно изменение
    store.delete(ids[0], A)
    assert store.version() == 2
    store.delete_batch("B", A)
    assert store.version() == 3
    store.add_many([row(5)], A)
    assert store.clear().seq == 5 and store.version() == 5
    # неудачные и пустые операции версию не меняют
    for bad in (lambda: store.delete("nope", A), lambda: store.delete_batch("nope", A), lambda: store.add_many([], A)):
        with contextlib.suppress(KeyError):
            bad()
    assert store.version() == 5


def test_events_journal(store):
    r = store.add_many([row(0)], A, "Айбек", summary="+1 замер в r3_c11 (−112 дБм) · GPS")
    store.add_many([row(1, "import", "B"), row(2, "import", "B")], B)
    store.delete(r.ids[0], A)
    store.clear()
    ev = store.events()
    assert [e["type"] for e in ev] == ["clear", "delete", "import", "add"]  # новые сверху
    assert ev[3]["summary"].startswith("+1 замер в r3_c11") and ev[3]["author_label"] == "Айбек"
    assert ev[2]["count"] == 2 and ev[2]["batch_id"] == "B" and len(ev[2]["measurement_ids"]) == 2
    assert ev[2]["summary"] == "импорт 2 замера" and ev[3]["measurement_ids"] == r.ids
    assert ev[1]["measurement_ids"] == r.ids and ev[1]["device_id"] == A
    assert ev[0]["device_id"] is None and ev[0]["count"] == 2
    assert [e["seq"] for e in store.events(since_seq=2)] == [4, 3]
    assert len(store.events(limit=1)) == 1


def test_many_ids_not_stored_in_event(store):
    store.add_many([row(i, timestamp=f"2026-11-20T10:{i // 60:02d}:{i % 60:02d}+06:00") for i in range(501)], A)
    assert store.events()[0]["measurement_ids"] is None and store.events()[0]["count"] == 501


def test_dataframe_in_insertion_order(store):
    store.add_many([row(0), row(1), row(2)], A)
    df = store.as_dataframe()
    assert df["rsrp_dbm"].tolist() == [-100.0, -101.0, -102.0] and df["source"].eq("manual").all()
    assert df["device_id"].eq(A).all()


def test_invalid_source_rolls_back_data_and_event(store):
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        store.add_many([row(0, source="bogus")], A)
    assert store.count()["total"] == 0 and store.version() == 0  # событие откатилось вместе с данными


def test_persistence_between_instances(tmp_path):
    path = tmp_path / "sub" / "f.db"  # каталог создаётся автоматически
    a = FieldStore(path)
    a.add_many([row(0)], A)
    a.close()
    b = FieldStore(path)
    assert b.total() == 1 and b.version() == 1 and b.events()[0]["type"] == "add"
    b.close()


def test_legacy_schema_is_recreated(tmp_path):
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.executescript(
        "CREATE TABLE field_measurements (id TEXT PRIMARY KEY, lat REAL, lon REAL, rsrp_dbm REAL, timestamp TEXT,"
        " source TEXT, accuracy_m REAL, device_label TEXT, batch_id TEXT, created_at TEXT);"
        "CREATE TABLE field_meta (key TEXT PRIMARY KEY, value TEXT); INSERT INTO field_meta VALUES ('version','7');"
    )
    con.commit()
    con.close()
    s = FieldStore(path)
    assert s.version() == 0 and s.total() == 0
    s.add_many([row(0)], A)
    assert s.version() == 1
    s.close()


def test_env_path(monkeypatch, tmp_path):
    monkeypatch.setenv("FIELD_DB_PATH", str(tmp_path / "env.db"))
    s = FieldStore()
    assert s.path.endswith("env.db")
    s.close()


def test_listener_called_after_commit(store):
    seen = []
    store.add_listener(seen.append)
    store.add_many([row(0)], A)
    store.clear()
    assert seen == [1, 2]


def test_concurrent_adds_are_safe(store):
    errors = []

    def work(k):
        try:
            for j in range(25):
                store.add_many(
                    [row(k, timestamp=f"2026-11-20T10:{k:02d}:{j:02d}+06:00")], f"{k:08d}-0000-4000-8000-000000000000"
                )
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(k,)) for k in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors
    assert store.total() == 200 and store.version() == 200
    assert sorted(e["seq"] for e in store.events(limit=500)) == list(range(1, 201))


def test_device_tag_is_short_and_hides_the_rest():
    assert device_tag(A) == "11111111" and device_tag(None) is None
