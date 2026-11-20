"""Хранилище полевых замеров (SQLite)."""

import threading

import pytest

from coverage.field_store import FieldStore


def row(i=0, source="manual", batch=None, **kw):
    base = {
        "lat": 42.85 + i * 1e-4,
        "lon": 74.58,
        "rsrp_dbm": -100.0 - i,
        "timestamp": f"2026-11-20T22:{i:02d}:00+06:00",
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
    assert store.count() == 0 and store.version() == 0 and store.list() == []
    assert store.as_dataframe().empty and list(store.as_dataframe().columns)[:4] == ["id", "lat", "lon", "rsrp_dbm"]


def test_add_list_newest_first(store):
    ids = store.add_many([row(0), row(1), row(2)])
    assert len(ids) == len(set(ids)) == 3
    listed = store.list()
    assert [r["id"] for r in listed] == ids[::-1]
    assert store.list(limit=1, offset=1)[0]["id"] == ids[1]
    assert store.get(ids[0])["rsrp_dbm"] == -100.0
    assert store.get("nope") is None


def test_filter_by_source_and_counts(store):
    store.add_many([row(0, "gps", accuracy_m=12.5), row(1, "map_pin"), row(2, "gps")])
    assert store.count() == 3 and store.count("gps") == 2
    assert [r["source"] for r in store.list(source="map_pin")] == ["map_pin"]
    assert store.count_by_source() == {"map_pin": 1, "gps": 2, "manual": 0, "import": 0}
    assert store.list(source="gps")[-1]["accuracy_m"] == 12.5


def test_delete_one_and_batch_and_clear(store):
    a, _b, _c, d = store.add_many([row(0), row(1, "import", "B1"), row(2, "import", "B1"), row(3, "import", "B2")])
    assert store.delete(a) is True and store.delete(a) is False
    assert store.delete_batch("B1") == 2 and store.delete_batch("B1") == 0
    assert [r["id"] for r in store.list()] == [d]
    assert store.clear() == 1 and store.count() == 0 and store.clear() == 0


def test_version_grows_on_every_change_only(store):
    v = store.version()
    ids = store.add_many([row(0), row(1, "import", "B")])
    assert store.version() == v + 1  # один пакет = одно изменение
    store.delete(ids[0])
    assert store.version() == v + 2
    store.delete_batch("B")
    assert store.version() == v + 3
    store.add_many([row(5)])
    store.clear()
    assert store.version() == v + 5
    # пустые операции версию не меняют
    store.delete("nope")
    store.delete_batch("nope")
    store.clear()
    store.add_many([])
    assert store.version() == v + 5


def test_dataframe_in_insertion_order(store):
    store.add_many([row(0), row(1), row(2)])
    df = store.as_dataframe()
    assert df["rsrp_dbm"].tolist() == [-100.0, -101.0, -102.0] and df["source"].eq("manual").all()


def test_invalid_source_rejected_by_check_constraint(store):
    with pytest.raises(Exception, match="CHECK"):
        store.add_many([row(0, source="bogus")])
    assert store.count() == 0  # транзакция откатилась


def test_persistence_between_instances(tmp_path):
    path = tmp_path / "sub" / "f.db"  # каталог создаётся автоматически
    a = FieldStore(path)
    a.add_many([row(0)])
    a.close()
    b = FieldStore(path)
    assert b.count() == 1 and b.version() == 1
    b.close()


def test_env_path(monkeypatch, tmp_path):
    monkeypatch.setenv("FIELD_DB_PATH", str(tmp_path / "env.db"))
    s = FieldStore()
    assert s.path.endswith("env.db")
    s.close()


def test_concurrent_adds_are_safe(store):
    def work(k):
        for j in range(20):
            store.add_many([row(k, batch=f"t{k}", timestamp=f"2026-11-20T10:{k:02d}:{j:02d}+06:00")])

    threads = [threading.Thread(target=work, args=(k,)) for k in range(5)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert store.count() == 100 and store.version() == 100
