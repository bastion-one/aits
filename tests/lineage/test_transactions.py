"""Atomic DUT/LT writes: rollback, observation counts, commit-chain integrity."""

from __future__ import annotations

import os
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError, IntegrityError, OperationalError
from sqlmodel import Session, SQLModel, create_engine, select

from app import db, ledger
from app.config import get_settings
from app.db import get_session
from app.hashing import ZERO_PREV, sha256_hash
from app.ledger import verify_commit_log
from app.main import app
from app.models import (
    Agent,
    AgentConfig,
    CommitLogEntry,
    ConfigActivation,
    DataUniqueTag,
    LineageTag,
)
from app.routers.audit import full_audit
from tests.helpers import CONFIG_BODY, create_node, create_root, inline_dut, make_dut_chain


def _register(client: TestClient) -> dict:
    r = client.post("/agents/", json={"name": "worker", "config": CONFIG_BODY})
    assert r.status_code == 201, r.text
    return r.json()


def _counts(engine: Engine) -> tuple[int, int, int]:
    with Session(engine) as fresh:
        return (
            len(fresh.exec(select(DataUniqueTag)).all()),
            len(fresh.exec(select(LineageTag)).all()),
            len(fresh.exec(select(CommitLogEntry)).all()),
        )


def test_lt_stage_failure_rolls_back_inline_dut(
    client: TestClient, engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Roll back the staged DUT and observations when storing its enclosing LT fails."""
    agent = _register(client)
    genesis = create_root(client)
    before = _counts(engine)
    original = ledger.stage

    def boom(session: Session, row):
        if isinstance(row, LineageTag):
            raise RuntimeError("injected during LT storage")
        return original(session, row)

    monkeypatch.setattr(ledger, "stage", boom)
    with pytest.raises(RuntimeError, match="injected during LT storage"):
        client.post(
            f"/lineage/{genesis['cid']}/nodes/",
            json={
                "actor_id": agent["name"],
                "step_id": "s",
                "transformation": "t",
                "prev": [genesis["cid"]],
                "dut": inline_dut(agent["uuid"]),
            },
        )
    assert _counts(engine) == before


def test_failure_after_dut_flush_rolls_back(
    client: TestClient, engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ensure a database flush does not leave a partial DUT write after failure."""
    agent = _register(client)
    genesis = create_root(client)
    before = _counts(engine)
    original = ledger.stage

    def boom(session: Session, row):
        stored, entry = original(session, row)
        if isinstance(row, DataUniqueTag):
            raise RuntimeError("injected after DUT flush")
        return stored, entry

    monkeypatch.setattr(ledger, "stage", boom)
    with pytest.raises(RuntimeError, match="injected after DUT flush"):
        client.post(
            f"/lineage/{genesis['cid']}/nodes/",
            json={
                "actor_id": agent["name"],
                "step_id": "s",
                "transformation": "t",
                "prev": [genesis["cid"]],
                "dut": inline_dut(agent["uuid"]),
            },
        )
    assert _counts(engine) == before


def test_commit_failure_rolls_back(
    client: TestClient, engine: Engine, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Leave no DUT, LT, or observation behind when the final commit raises an error."""
    agent = _register(client)
    genesis = create_root(client)
    before = _counts(engine)
    original = session.commit
    fired = {"done": False}

    def once() -> None:
        if not fired["done"]:
            fired["done"] = True
            raise RuntimeError("injected commit failure")
        original()

    monkeypatch.setattr(session, "commit", once)
    with pytest.raises(RuntimeError, match="injected commit failure"):
        client.post(
            f"/lineage/{genesis['cid']}/nodes/",
            json={
                "actor_id": agent["name"],
                "step_id": "s",
                "transformation": "t",
                "prev": [genesis["cid"]],
                "dut": inline_dut(agent["uuid"]),
            },
        )
    assert _counts(engine) == before


def test_inline_replay_dedupes_nodes_and_appends_observations(
    client: TestClient, engine: Engine, session: Session
) -> None:
    """Reuse DUT and LT identities on replay while recording both submissions in the log."""
    agent = _register(client)
    genesis = create_root(client)
    body = {
        "actor_id": agent["name"],
        "step_id": "s",
        "transformation": "t",
        "prev": [genesis["cid"]],
        "occurred_at": "2026-06-10T09:01:00+00:00",
        "dut": inline_dut(
            agent["uuid"],
            span_id="replay",
            input_context="same",
            agent_output="same",
            occurred_at="2026-06-10T09:01:00+00:00",
        ),
    }
    first = client.post(f"/lineage/{genesis['cid']}/nodes/", json=body).json()
    again = client.post(f"/lineage/{genesis['cid']}/nodes/", json=body).json()
    assert again["node"]["cid"] == first["node"]["cid"]
    assert again["dut"]["cid"] == first["dut"]["cid"]
    with Session(engine) as fresh:
        dut_obs = fresh.exec(
            select(CommitLogEntry).where(CommitLogEntry.cid == bytes.fromhex(first["dut"]["cid"]))
        ).all()
        lt_obs = fresh.exec(
            select(CommitLogEntry).where(CommitLogEntry.cid == bytes.fromhex(first["node"]["cid"]))
        ).all()
        assert len(dut_obs) == 2
        assert len(lt_obs) == 2
        assert verify_commit_log(fresh)


def test_success_commit_chain_and_temporal_order(client: TestClient, engine: Engine) -> None:
    """Check that an inline write preserves log integrity and dependency observation order."""
    agent = _register(client)
    genesis = create_root(client)
    created = create_node(client, genesis["cid"], [genesis["cid"]], dut=inline_dut(agent["uuid"]))
    with Session(engine) as fresh:
        assert verify_commit_log(fresh)
        assert ledger.check_temporal_order(fresh, bytes.fromhex(created["node"]["cid"]))


def _postgres_url() -> str:
    return os.environ.get(
        "AITS_TEST_DATABASE_URL",
        "postgresql+psycopg://bastion:bastion@127.0.0.1:5432/bastion",
    )


@pytest.fixture
def pg_engine() -> Iterator[Engine]:
    url = _postgres_url()
    bootstrap = create_engine(url, pool_pre_ping=True)
    try:
        with bootstrap.connect() as conn:
            conn.execute(text("SELECT 1"))
    except OperationalError as exc:
        bootstrap.dispose()
        pytest.skip(f"PostgreSQL is not available: {exc}")
    schema = f"aits_tx_{uuid4().hex}"
    with bootstrap.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(url, pool_pre_ping=True)

    @event.listens_for(engine, "connect")
    def _search_path(dbapi_conn, _connection_record) -> None:  # type: ignore[no-untyped-def]
        cursor = dbapi_conn.cursor()
        cursor.execute(f'SET search_path TO "{schema}"')
        cursor.close()
        # Commit so a later rolled-back transaction cannot undo the SET and
        # drop the connection back to the public schema.
        dbapi_conn.commit()

    SQLModel.metadata.create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()
        with bootstrap.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        bootstrap.dispose()


def test_postgres_concurrent_node_writes_keep_a_single_commit_chain(pg_engine: Engine) -> None:
    """Serialize eight writers' observations into one unbroken PostgreSQL commit chain."""
    when = datetime(2026, 6, 10, 9, 0, tzinfo=timezone.utc)
    with Session(pg_engine) as session:
        ledger.attest(session, "test")
        root, _ = ledger.record(
            session,
            LineageTag(
                actor_id="root",
                step_id="s",
                transformation="root",
                occurred_at=when,
            ),
        )
        root_cid = root.cid

    def write(actor: str) -> None:
        with Session(pg_engine) as session:
            ledger.attest(session, "test")
            ledger.stage(
                session,
                LineageTag(
                    actor_id=actor,
                    step_id="s",
                    transformation="work",
                    occurred_at=when.replace(minute=1),
                    prev_cids=[root_cid.hex()],
                ),
            )
            session.commit()

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(write, f"w{i}") for i in range(8)]
        for fut in futures:
            fut.result()

    with Session(pg_engine) as session:
        result = verify_commit_log(session)
        assert result, getattr(result, "message", result)
        entries = session.exec(select(CommitLogEntry).order_by(CommitLogEntry.seq)).all()
        assert len(entries) == 9  # root + 8 concurrent nodes
        for prev, current in zip(entries, entries[1:]):
            assert current.prev_hash == prev.entry_hash


def test_postgres_concurrent_identical_submissions_dedupe(
    pg_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Race identical writes at the commit lock: store one child LT and eight observations."""
    when = datetime(2026, 6, 10, 9, 0, tzinfo=timezone.utc)
    with Session(pg_engine) as session:
        ledger.attest(session, "test")
        root, _ = ledger.record(
            session,
            LineageTag(
                actor_id="root",
                step_id="s",
                transformation="root",
                occurred_at=when,
            ),
        )
        root_cid = root.cid

    barrier = threading.Barrier(8)
    original = getattr(ledger, "lock_commit_head")

    def wait_then_lock(session: Session) -> None:
        barrier.wait()
        original(session)

    monkeypatch.setattr(ledger, "lock_commit_head", wait_then_lock)

    def write() -> None:
        with Session(pg_engine) as session:
            ledger.attest(session, "test")
            ledger.stage(
                session,
                LineageTag(
                    actor_id="A",
                    step_id="s",
                    transformation="work",
                    occurred_at=when.replace(minute=1),
                    prev_cids=[root_cid.hex()],
                ),
            )
            session.commit()

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(write) for _ in range(8)]
        for fut in futures:
            fut.result()

    with Session(pg_engine) as session:
        assert len(session.exec(select(LineageTag)).all()) == 2
        entries = session.exec(select(CommitLogEntry).order_by(CommitLogEntry.seq)).all()
        assert len(entries) == 9
        result = verify_commit_log(session)
        assert result, getattr(result, "message", result)
        for prev, current in zip(entries, entries[1:]):
            assert current.prev_hash == prev.entry_hash


def test_postgres_inline_rollback_matches_sqlite(
    pg_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Replay the LT-stage failure against an isolated Postgres schema."""
    monkeypatch.setattr(get_settings(), "auth_disabled", True)

    def _override() -> Iterator[Session]:
        with Session(pg_engine) as session:
            yield session

    app.dependency_overrides[get_session] = _override
    try:
        client = TestClient(app)
        agent = _register(client)
        genesis = create_root(client)
        before = _counts(pg_engine)
        original = ledger.stage

        def boom(session: Session, row):
            if isinstance(row, LineageTag):
                raise RuntimeError("injected during LT storage")
            return original(session, row)

        monkeypatch.setattr(ledger, "stage", boom)
        with pytest.raises(RuntimeError, match="injected during LT storage"):
            client.post(
                f"/lineage/{genesis['cid']}/nodes/",
                json={
                    "actor_id": agent["name"],
                    "step_id": "s",
                    "transformation": "t",
                    "prev": [genesis["cid"]],
                    "dut": inline_dut(agent["uuid"]),
                },
            )
        assert _counts(pg_engine) == before
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def pg_audit_client(pg_engine: Engine) -> Iterator[TestClient]:
    app.dependency_overrides[db.get_engine] = lambda: pg_engine
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(db.get_engine)


def test_postgres_audit_snapshot_excludes_appends_and_deletions(
    pg_engine: Engine, pg_audit_client: TestClient
) -> None:
    """All full-audit reads retain the snapshot despite later committed changes."""
    _, config, _ = make_dut_chain(pg_audit_client)
    deleted_cid = bytes.fromhex(config["cid"])

    with contextmanager(db.get_audit_session)(pg_engine) as audit:
        audit.exec(select(CommitLogEntry.seq)).all()
        with Session(pg_engine) as writer:
            ledger.attest(writer, "test")
            ledger.stage(writer, AgentConfig(system_prompt="after the snapshot"))
            writer.delete(writer.get(AgentConfig, deleted_cid))
            writer.commit()
        assert full_audit(audit).model_dump() == {
            "valid": True,
            "nodes_checked": 3,
            "nodes_verified": 3,
            "commit_entries": 3,
            "failures": [],
        }

    with contextmanager(db.get_audit_session)(pg_engine) as audit:
        report = full_audit(audit)
        assert report.valid is False
        assert report.nodes_checked == 3
        assert report.commit_entries == 4
        assert any(
            f.property_violated == "missing_node" and f.record_key == deleted_cid.hex()
            for f in report.failures
        )
        assert any(f.property_violated == "dangling_link" for f in report.failures)


def test_postgres_audit_is_read_only_and_restores_connection_settings(pg_engine: Engine) -> None:
    """Read-only isolation belongs to the audit, including after a failed query."""
    with contextmanager(db.get_audit_session)(pg_engine) as audit:
        assert audit.exec(text("SHOW transaction_isolation")).scalar_one() == "repeatable read"
        assert audit.exec(text("SHOW transaction_read_only")).scalar_one() == "on"
        with pytest.raises(DBAPIError) as caught:
            audit.exec(text("DELETE FROM agent_config"))
        assert caught.value.orig.sqlstate == "25006"  # read_only_sql_transaction

    with contextmanager(db.get_session)(pg_engine) as ordinary:
        ledger.attest(ordinary, "test")
        assert ordinary.exec(text("SHOW transaction_isolation")).scalar_one() == "read committed"
        assert ordinary.exec(text("SHOW transaction_read_only")).scalar_one() == "off"
        ledger.record(ordinary, AgentConfig(system_prompt="ordinary writer"))


def _stall_first_activation_writes(monkeypatch: pytest.MonkeyPatch, parties: int) -> None:
    """Hold each activation write until all ``parties`` writers have read the chain head.

    Patches ``ledger.stage`` (which ``activate`` and ``record`` look up through
    the module) to wait on a barrier before writing a ``ConfigActivation``.
    Without the commit lock around the head read, every writer reaches the
    barrier holding the same head, the barrier releases them together, and the
    chain forks. With the lock, the first writer blocks the others before their
    head read, so its barrier wait times out (1 s, swallowed on purpose) and the
    writers proceed one at a time. ``parties`` must equal the number of
    concurrent writers or the barrier never trips on unfixed code.
    """
    barrier = threading.Barrier(parties)
    original = ledger.stage

    def stalled(session: Session, row):
        if isinstance(row, ConfigActivation):
            try:
                barrier.wait(timeout=1)
            except threading.BrokenBarrierError:
                pass
        return original(session, row)

    monkeypatch.setattr(ledger, "stage", stalled)


def _agent_and_configs(engine: Engine, count: int) -> tuple[UUID, list[bytes]]:
    agent_uuid = uuid4()
    with Session(engine) as session:
        ledger.attest(session, "test")
        ledger.record(
            session, Agent(uuid=agent_uuid, created_at=datetime.now(timezone.utc), name="racer")
        )
        configs = [
            ledger.record(session, AgentConfig(system_prompt=f"config {i}"))[0].cid
            for i in range(count)
        ]
    return agent_uuid, configs


def test_postgres_concurrent_first_activations_stay_linear(
    pg_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Concurrent activations for one agent append to a single linear chain."""
    agent_uuid, configs = _agent_and_configs(pg_engine, 4)
    _stall_first_activation_writes(monkeypatch, len(configs))

    def activate(config_cid: bytes) -> None:
        with Session(pg_engine) as session:
            ledger.attest(session, "test")
            agent = session.exec(select(Agent).where(Agent.uuid == agent_uuid)).one()
            ledger.activate(session, agent, config_cid)

    with ThreadPoolExecutor(max_workers=len(configs)) as pool:
        for fut in [pool.submit(activate, cid) for cid in configs]:
            fut.result()

    with Session(pg_engine) as session:
        chain = ledger.activation_rows(session, agent_uuid)
        assert {row.config_cid for row in chain} == set(configs)


def test_postgres_concurrent_compare_and_set_admits_one_writer(
    pg_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Concurrent activations that all expect an empty chain: one wins, the rest get 409."""
    agent_uuid, configs = _agent_and_configs(pg_engine, 4)
    _stall_first_activation_writes(monkeypatch, len(configs))

    def _override() -> Iterator[Session]:
        with Session(pg_engine) as session:
            yield session

    app.dependency_overrides[get_session] = _override
    try:
        client = TestClient(app)

        def activate(config_cid: bytes) -> int:
            return client.post(
                f"/agents/{agent_uuid}/activations/",
                json={"config_cid": config_cid.hex(), "expected_head": None},
            ).status_code

        with ThreadPoolExecutor(max_workers=len(configs)) as pool:
            statuses = sorted(pool.map(activate, configs))
    finally:
        app.dependency_overrides.pop(get_session, None)

    assert statuses == [201, 409, 409, 409]
    with Session(pg_engine) as session:
        assert len(ledger.activation_rows(session, agent_uuid)) == 1


def test_postgres_commit_lock_wait_times_out(
    pg_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A writer that cannot get the commit lock in time fails instead of waiting forever."""
    monkeypatch.setattr(get_settings(), "commit_lock_timeout_ms", 200)
    with Session(pg_engine) as holder, Session(pg_engine) as waiter:
        ledger.attest(waiter, "test")
        ledger.lock_commit_head(holder)
        assert holder.execute(text("SHOW lock_timeout")).scalar_one() == "0"
        with pytest.raises(ledger.CommitLockTimeout):
            ledger.record(waiter, AgentConfig(system_prompt="waits"))
        holder.rollback()
        waiter.rollback()

        ledger.record(waiter, AgentConfig(system_prompt="waits"))
        assert waiter.execute(text("SHOW lock_timeout")).scalar_one() == "0"


def test_postgres_commit_lock_timeout_is_a_503(
    pg_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "commit_lock_timeout_ms", 200)

    def _override() -> Iterator[Session]:
        with Session(pg_engine) as session:
            yield session

    app.dependency_overrides[get_session] = _override
    try:
        with Session(pg_engine) as holder:
            ledger.lock_commit_head(holder)
            r = TestClient(app).post("/configs/", json=CONFIG_BODY)
            holder.rollback()
    finally:
        app.dependency_overrides.pop(get_session, None)
    assert r.status_code == 503, r.text
    assert r.headers["retry-after"] == "1"


def _add_forked_entries(session: Session) -> None:
    """Add two entries that chain to the same predecessor (``ZERO_PREV``).

    Every other unique column differs, so only ``prev_hash`` can collide.
    """
    now = datetime.now(timezone.utc)
    for name in (b"a", b"b"):
        session.add(
            CommitLogEntry(
                cid=sha256_hash(name),
                recorded_at=now,
                prev_hash=ZERO_PREV,
                entry_hash=sha256_hash(b"entry-" + name),
                principal="test",
            )
        )


def test_commit_log_rejects_two_entries_with_the_same_predecessor(session: Session) -> None:
    """A fork in the commit log is impossible at the storage layer."""
    _add_forked_entries(session)
    with pytest.raises(IntegrityError) as excinfo:
        session.commit()
    # SQLite reports the column, not the constraint name.
    assert "commit_log.prev_hash" in str(excinfo.value.orig)


def test_postgres_commit_log_rejects_two_entries_with_the_same_predecessor(
    pg_engine: Engine,
) -> None:
    """A fresh PostgreSQL schema carries ``uq_commit_log_prev_hash``."""
    with Session(pg_engine) as session:
        _add_forked_entries(session)
        with pytest.raises(IntegrityError) as excinfo:
            session.commit()
    assert excinfo.value.orig.diag.constraint_name == "uq_commit_log_prev_hash"
    assert "uq_commit_log_prev_hash" in str(excinfo.value.orig)
