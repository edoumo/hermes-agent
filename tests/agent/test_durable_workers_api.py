import base64
import json
import sqlite3

import pytest

from agent.durable_workers_api import (
    DurableWorkersApiError,
    DurableWorkersProjection,
    InvalidCursorError,
    NotFoundError,
)


def _seed(path):
    db = sqlite3.connect(path)
    db.executescript(
        """
        CREATE TABLE durable_workers(
          worker_id TEXT PRIMARY KEY,parent_session_id TEXT,label TEXT,status TEXT,
          role TEXT,model TEXT,toolsets_json TEXT,created_at REAL,updated_at REAL,
          revision INTEGER,last_activation_id TEXT);
        CREATE TABLE durable_worker_messages(
          message_id TEXT PRIMARY KEY,worker_id TEXT,direction TEXT,content TEXT,
          state TEXT,created_at REAL,updated_at REAL);
        CREATE TABLE durable_worker_activations(
          activation_id TEXT PRIMARY KEY,worker_id TEXT,message_id TEXT,subagent_id TEXT,
          state TEXT,started_at REAL,completed_at REAL,summary TEXT,error TEXT,
          owner_pid INTEGER,owner_started_at INTEGER);
        CREATE TABLE durable_worker_tasks(
          task_id TEXT PRIMARY KEY,parent_session_id TEXT,worker_id TEXT,subject TEXT,
          description TEXT,status TEXT,revision INTEGER,created_at REAL,updated_at REAL);
        CREATE TABLE durable_worker_task_dependencies(task_id TEXT,blocked_by_task_id TEXT);
        """
    )
    for i in range(4):
        db.execute(
            "INSERT INTO durable_workers VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                f"dw-{i}",
                "parent-a",
                f"worker {i}",
                "DORMANT",
                "leaf",
                None,
                '["file"]',
                10 + i,
                20 + i,
                1,
                None,
            ),
        )
    db.execute(
        "INSERT INTO durable_workers VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            "dw-foreign",
            "parent-b",
            "foreign",
            "DORMANT",
            "leaf",
            None,
            "[]",
            99,
            99,
            1,
            None,
        ),
    )
    for i in range(3):
        db.execute(
            "INSERT INTO durable_worker_messages VALUES(?,?,?,?,?,?,?)",
            (
                f"m-{i}",
                "dw-0",
                "parent",
                f"msg {i}",
                "CONSUMED",
                30 + i,
                30 + i,
            ),
        )
    db.execute(
        "INSERT INTO durable_worker_messages VALUES(?,?,?,?,?,?,?)",
        (
            "m-foreign",
            "dw-foreign",
            "parent",
            "secret",
            "CONSUMED",
            100,
            100,
        ),
    )
    db.execute(
        "INSERT INTO durable_worker_activations VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            "a-1",
            "dw-0",
            "m-0",
            "sa-1",
            "SUCCEEDED",
            40,
            41,
            "ok",
            None,
            1234,
            5678,
        ),
    )
    for i in range(3):
        db.execute(
            "INSERT INTO durable_worker_tasks VALUES(?,?,?,?,?,?,?,?,?)",
            (
                f"t-{i}",
                "parent-a",
                "dw-0",
                f"task {i}",
                "",
                "pending",
                1,
                50 + i,
                50 + i,
            ),
        )
    db.execute("INSERT INTO durable_worker_task_dependencies VALUES('t-2','t-1')")
    db.commit()
    db.close()


def test_worker_pagination_is_bounded_and_parent_scoped(tmp_path):
    path = tmp_path / "durable.db"
    _seed(path)
    api = DurableWorkersProjection(path)

    first = api.list_workers("parent-a", limit=2)
    assert [item["worker_id"] for item in first.items] == ["dw-3", "dw-2"]
    assert first.has_more is True
    assert first.next_cursor
    assert all(item["parent_session_id"] == "parent-a" for item in first.items)

    second = api.list_workers("parent-a", limit=2, cursor=first.next_cursor)
    assert [item["worker_id"] for item in second.items] == ["dw-1", "dw-0"]
    assert second.has_more is False


def test_nested_feeds_fail_closed_on_parent_scope(tmp_path):
    path = tmp_path / "durable.db"
    _seed(path)
    api = DurableWorkersProjection(path)

    assert [m["message_id"] for m in api.list_messages("parent-a", "dw-0").items] == [
        "m-2",
        "m-1",
        "m-0",
    ]
    with pytest.raises(NotFoundError):
        api.list_messages("parent-a", "dw-foreign")
    with pytest.raises(NotFoundError):
        api.get_worker("parent-a", "dw-foreign")


def test_activation_projection_omits_host_process_identity(tmp_path):
    path = tmp_path / "durable.db"
    _seed(path)
    api = DurableWorkersProjection(path)

    item = api.list_activations("parent-a", "dw-0").items[0]
    assert item["activation_id"] == "a-1"
    assert "owner_pid" not in item
    assert "owner_started_at" not in item


def test_task_projection_includes_dependencies_and_readiness(tmp_path):
    path = tmp_path / "durable.db"
    _seed(path)
    api = DurableWorkersProjection(path)

    tasks = {item["task_id"]: item for item in api.list_tasks("parent-a").items}
    assert tasks["t-2"]["blocked_by"] == ["t-1"]
    assert tasks["t-1"]["ready"] is True
    assert tasks["t-2"]["ready"] is False

    db = sqlite3.connect(path)
    db.execute("UPDATE durable_worker_tasks SET status='completed', updated_at=99 WHERE task_id='t-1'")
    db.commit()
    db.close()

    tasks = {item["task_id"]: item for item in api.list_tasks("parent-a").items}
    assert tasks["t-2"]["ready"] is True


def test_change_token_tracks_public_mutations_but_stays_parent_scoped(tmp_path):
    path = tmp_path / "durable.db"
    _seed(path)
    api = DurableWorkersProjection(path)

    before = api.change_token("parent-a")
    db = sqlite3.connect(path)
    db.execute(
        "INSERT INTO durable_worker_activations VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            "a-2",
            "dw-0",
            "m-1",
            None,
            "STARTING",
            200,
            None,
            None,
            None,
            333,
            444,
        ),
    )
    db.commit()
    db.close()
    reserved = api.change_token("parent-a")
    assert reserved != before

    db = sqlite3.connect(path)
    db.execute(
        "UPDATE durable_worker_activations SET subagent_id='sa-2',state='RUNNING' WHERE activation_id='a-2'"
    )
    db.commit()
    db.close()
    bound = api.change_token("parent-a")
    assert bound != reserved

    db = sqlite3.connect(path)
    db.execute("UPDATE durable_workers SET updated_at=999 WHERE worker_id='dw-foreign'")
    db.commit()
    db.close()
    assert api.change_token("parent-a") == bound


def test_cursor_and_limit_validation(tmp_path):
    path = tmp_path / "durable.db"
    _seed(path)
    api = DurableWorkersProjection(path)

    with pytest.raises(DurableWorkersApiError):
        api.list_workers("parent-a", limit=0)
    with pytest.raises(DurableWorkersApiError):
        api.list_workers("parent-a", limit=101)
    with pytest.raises(InvalidCursorError):
        api.list_workers("parent-a", cursor="not-a-cursor")

    cursor = api.list_workers("parent-a", limit=1).next_cursor
    with pytest.raises(InvalidCursorError):
        api.list_tasks("parent-a", cursor=cursor)

    nan_payload = json.dumps(
        {"v": 1, "kind": "workers", "ts": float("nan"), "id": "dw-1"}
    ).encode()
    nan_cursor = base64.urlsafe_b64encode(nan_payload).decode().rstrip("=")
    with pytest.raises(InvalidCursorError):
        api.list_workers("parent-a", cursor=nan_cursor)
