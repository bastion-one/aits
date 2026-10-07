"""Full-audit read consistency across independent SQLite connections."""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.exc import OperationalError
from sqlmodel import Session, SQLModel, create_engine, select

from app import db, ledger
from app.main import app
from app.models import Agent, AgentConfig, CommitLogEntry
from app.routers import audit as audit_router
from tests.helpers import make_dut_chain


@pytest.fixture
def audit_engine(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'audit.db'}",
        connect_args={"check_same_thread": False, "timeout": 0.05},
    )
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA journal_mode=WAL")
    SQLModel.metadata.create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def audit_client(audit_engine):
    app.dependency_overrides[db.get_engine] = lambda: audit_engine
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(db.get_engine)


def test_http_audit_excludes_an_append_before_chain_verification(audit_engine, audit_client):
    """The scan, coverage, chain, and count must describe the same state."""
    with Session(audit_engine) as writer:
        ledger.attest(writer, "test")
        ledger.record(writer, AgentConfig(system_prompt="before the audit"))
    chain_read = select(CommitLogEntry).order_by(CommitLogEntry.seq)
    appended = False

    def append_before_chain(_conn, _cursor, _statement, _parameters, context, _executemany):
        nonlocal appended
        if appended or context.compiled is None:
            return
        if context.compiled.statement.get_final_froms() != chain_read.get_final_froms():
            return
        if not context.compiled.statement.compare(chain_read):
            return
        appended = True
        with Session(audit_engine) as writer:
            ledger.attest(writer, "test")
            ledger.record(writer, AgentConfig(system_prompt="during the audit"))

    event.listen(audit_engine, "before_cursor_execute", append_before_chain)
    try:
        report = audit_client.get("/audit/").json()
    finally:
        event.remove(audit_engine, "before_cursor_execute", append_before_chain)
    assert appended, "the write must occur before the audit's chain read"
    assert report == {
        "valid": True,
        "nodes_checked": 1,
        "nodes_verified": 1,
        "commit_entries": 1,
        "failures": [],
    }
    next_report = audit_client.get("/audit/").json()
    assert next_report["valid"] is True
    assert next_report["nodes_checked"] == next_report["commit_entries"] == 2


def test_http_audit_cannot_miss_an_atomic_corruption_switch(audit_engine, audit_client):
    """A moving scan can pass even though every committed state is corrupted."""
    original_time = datetime.now(timezone.utc)
    with Session(audit_engine) as writer:
        ledger.attest(writer, "test")
        agent, _ = ledger.record(
            writer, Agent(uuid=uuid4(), name="worker", created_at=original_time)
        )
        agent_cid = agent.cid
        config, _ = ledger.record(writer, AgentConfig(system_prompt="original"))
        config_cid = config.cid
        config.system_prompt = "corrupted"
        writer.commit()
    agent_scan = select(Agent)
    switched = False

    def switch_after_scan(_conn, _cursor, _statement, _parameters, context, _executemany):
        nonlocal switched
        if switched or context.compiled is None:
            return
        if context.compiled.statement.get_final_froms() != agent_scan.get_final_froms():
            return
        if not context.compiled.statement.compare(agent_scan):
            return
        switched = True
        with Session(audit_engine) as writer:
            writer.get(Agent, agent_cid).created_at = original_time + timedelta(seconds=1)
            writer.get(AgentConfig, config_cid).system_prompt = "original"
            writer.commit()

    event.listen(audit_engine, "after_cursor_execute", switch_after_scan)
    try:
        report = audit_client.get("/audit/").json()
    finally:
        event.remove(audit_engine, "after_cursor_execute", switch_after_scan)
    assert switched, "the corruption must switch between the Agent and AgentConfig scans"
    assert report["valid"] is False
    assert any(
        failure["record_key"] == config_cid.hex() and failure["property_violated"] == "cid_mismatch"
        for failure in report["failures"]
    )
    next_report = audit_client.get("/audit/").json()
    assert next_report["valid"] is False
    assert any(
        failure["record_key"] == agent_cid.hex() and failure["property_violated"] == "cid_mismatch"
        for failure in next_report["failures"]
    )


def test_audit_snapshot_excludes_committed_appends_and_deletions(audit_engine, audit_client):
    """Recursive verification and log reads retain the state before a parent deletion."""
    _, config, _ = make_dut_chain(audit_client)
    deleted_cid = bytes.fromhex(config["cid"])

    with contextmanager(db.get_audit_session)(audit_engine) as audit:
        audit.exec(select(CommitLogEntry.seq)).all()  # Establish the snapshot.
        with Session(audit_engine) as writer:
            ledger.attest(writer, "test")
            ledger.stage(writer, AgentConfig(system_prompt="after the snapshot"))
            writer.delete(writer.get(AgentConfig, deleted_cid))
            writer.commit()
        assert audit_router.full_audit(audit).model_dump() == {
            "valid": True,
            "nodes_checked": 3,
            "nodes_verified": 3,
            "commit_entries": 3,
            "failures": [],
        }

    with contextmanager(db.get_audit_session)(audit_engine) as audit:
        report = audit_router.full_audit(audit)
        assert report.valid is False
        assert report.nodes_checked == 3
        assert report.commit_entries == 4
        assert any(
            f.property_violated == "missing_node" and f.record_key == deleted_cid.hex()
            for f in report.failures
        )
        assert any(f.property_violated == "dangling_link" for f in report.failures)


@pytest.mark.parametrize("fail", [False, True])
def test_audit_releases_default_journal_read_lock(audit_engine, fail):
    """Both completion and exceptions release a snapshot's SQLite read lock."""
    with audit_engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA journal_mode=DELETE").scalar_one() == "delete"
    with Session(audit_engine) as writer:
        ledger.attest(writer, "test")
        ledger.record(writer, AgentConfig(system_prompt="before the audit"))

    def run_audit():
        with contextmanager(db.get_audit_session)(audit_engine) as audit:
            audit.exec(select(CommitLogEntry.seq)).all()
            with Session(audit_engine) as writer:
                ledger.attest(writer, "test")
                with pytest.raises(OperationalError, match="database is locked"):
                    ledger.record(writer, AgentConfig(system_prompt="blocked"))
                writer.rollback()
            if fail:
                raise RuntimeError("injected audit failure")
            assert audit_router.full_audit(audit).valid is True

    if fail:
        with pytest.raises(RuntimeError, match="injected audit failure"):
            run_audit()
    else:
        run_audit()
    assert audit_engine.pool.checkedout() == 0
    with Session(audit_engine) as writer:
        ledger.attest(writer, "test")
        ledger.record(writer, AgentConfig(system_prompt="after release"))
        assert len(writer.exec(select(CommitLogEntry.seq)).all()) == 2


def test_audit_does_not_change_ordinary_sqlite_read_behavior(audit_engine):
    """Ordinary reads still see later commits after an audit returns its connection."""
    with Session(audit_engine) as writer:
        ledger.attest(writer, "test")
        ledger.record(writer, AgentConfig(system_prompt="before"))
    with contextmanager(db.get_audit_session)(audit_engine) as audit:
        assert audit_router.full_audit(audit).valid is True

    with contextmanager(db.get_session)(audit_engine) as ordinary:
        assert len(ordinary.exec(select(CommitLogEntry.seq)).all()) == 1
        with Session(audit_engine) as writer:
            ledger.attest(writer, "test")
            ledger.record(writer, AgentConfig(system_prompt="after"))
        assert len(ordinary.exec(select(CommitLogEntry.seq)).all()) == 2
