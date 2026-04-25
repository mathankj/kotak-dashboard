"""Tests for backend/storage atomic-write + locking primitives.

Uses tmp_path so we don't touch the real paper_trades.json / orders_log.json.
"""
import json
import threading

from backend.storage._safe_io import (
    atomic_write_json, file_lock, read_json,
)


def test_atomic_write_then_read(tmp_path):
    p = tmp_path / "x.json"
    atomic_write_json(str(p), {"a": 1, "b": [2, 3]})
    assert json.loads(p.read_text()) == {"a": 1, "b": [2, 3]}


def test_atomic_write_overwrites(tmp_path):
    p = tmp_path / "x.json"
    atomic_write_json(str(p), [1])
    atomic_write_json(str(p), [2, 3])
    assert json.loads(p.read_text()) == [2, 3]


def test_atomic_write_no_tmp_left_behind(tmp_path):
    p = tmp_path / "x.json"
    atomic_write_json(str(p), {"k": "v"})
    # The .tmp file is renamed by os.replace; nothing should remain.
    assert not (tmp_path / "x.json.tmp").exists()


def test_read_json_default_when_missing(tmp_path):
    p = tmp_path / "missing.json"
    assert read_json(str(p), []) == []
    assert read_json(str(p), {"x": 1}) == {"x": 1}


def test_read_json_default_on_corrupt(tmp_path):
    p = tmp_path / "corrupt.json"
    p.write_text("{ not valid json")
    assert read_json(str(p), []) == []


def test_file_lock_same_path_returns_same_lock(tmp_path):
    p = str(tmp_path / "a.json")
    assert file_lock(p) is file_lock(p)


def test_file_lock_different_paths_different_locks(tmp_path):
    a = str(tmp_path / "a.json")
    b = str(tmp_path / "b.json")
    assert file_lock(a) is not file_lock(b)


def test_concurrent_appends_no_lost_writes(tmp_path):
    """20 threads each appending — final file should have all 20 entries.

    Without the lock, concurrent read-modify-write on append_order would lose
    some entries (last writer wins). With the per-path lock we get all of them.
    """
    from backend.storage import orders as orders_mod
    # Repoint the storage path at the tmp dir for isolation.
    p = tmp_path / "orders.json"
    orig = orders_mod.ORDERS_FILE
    orders_mod.ORDERS_FILE = str(p)
    try:
        threads = [
            threading.Thread(target=orders_mod.append_order,
                             args=({"i": i},))
            for i in range(20)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        result = orders_mod.read_orders()
        assert len(result) == 20
        assert sorted(e["i"] for e in result) == list(range(20))
    finally:
        orders_mod.ORDERS_FILE = orig
