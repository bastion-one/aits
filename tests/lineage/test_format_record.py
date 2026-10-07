"""The ledger format record: one row in ``ledger_format``, checked at startup."""

from collections.abc import Iterator
from uuid import uuid4

import pytest
from sqlalchemy import event, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app import ledger
from app.models import LedgerFormat, LineageTag
from tests.lineage.test_transactions import _postgres_url


def _rows(session: Session) -> list[LedgerFormat]:
    session.expire_all()
    return list(session.exec(select(LedgerFormat)).all())


def _fresh_sqlite() -> Engine:
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(eng)
    return eng


@pytest.fixture
def pg_fresh() -> Iterator[Engine]:
    url = _postgres_url()
    bootstrap = create_engine(url, pool_pre_ping=True)
    try:
        with bootstrap.connect() as conn:
            conn.execute(text("SELECT 1"))
    except OperationalError as exc:
        bootstrap.dispose()
        pytest.skip(f"PostgreSQL is not available: {exc}")
    schema = f"aits_fmt_{uuid4().hex}"
    with bootstrap.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    eng = create_engine(url, pool_pre_ping=True)

    @event.listens_for(eng, "connect")
    def _search_path(dbapi_conn, _connection_record) -> None:  # type: ignore[no-untyped-def]
        cursor = dbapi_conn.cursor()
        cursor.execute(f'SET search_path TO "{schema}"')
        cursor.close()

    SQLModel.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()
        with bootstrap.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        bootstrap.dispose()


def test_fresh_init_writes_one_row_equal_to_format(session: Session) -> None:
    [row] = _rows(session)
    assert ledger.FORMAT == {"format": 1, "hash": "sha2-256", "canonical": "rfc8785"}
    assert (row.id, row.format, row.hash, row.canonical) == (1, 1, "sha2-256", "rfc8785")


def test_second_init_is_a_no_op(session: Session) -> None:
    [before] = _rows(session)
    before = before.model_dump()
    ledger.init_ledger(session)
    assert [r.model_dump() for r in _rows(session)] == [before]


def test_observations_without_a_format_record_are_refused() -> None:
    eng = _fresh_sqlite()
    with Session(eng) as s:
        ledger.attest(s, "test")
        ledger.record(
            s,
            LineageTag(
                actor_id="a",
                step_id="s",
                transformation="root",
                occurred_at=ledger.datetime.now(ledger.timezone.utc),
            ),
        )
        with pytest.raises(
            RuntimeError,
            match=r"database has 1 commit-log entries and no ledger format record "
            r"\(pre-format-1 database\); a fresh database is required",
        ):
            ledger.init_ledger(s)
        assert not _rows(s)


def test_a_different_format_is_refused(session: Session) -> None:
    [row] = _rows(session)
    row.format = 2
    session.add(row)
    session.commit()
    with pytest.raises(
        RuntimeError, match=r"ledger format \{'format': 2, .*\} is not \{'format': 1, "
    ):
        ledger.init_ledger(session)


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
def test_check_constraint_rejects_a_second_row(backend: str, request) -> None:
    eng = _fresh_sqlite() if backend == "sqlite" else request.getfixturevalue("pg_fresh")
    with Session(eng) as s:
        ledger.init_ledger(s)
        s.add(LedgerFormat(id=2, **ledger.FORMAT))
        with pytest.raises(IntegrityError, match="ck_ledger_format_one_row|CHECK constraint"):
            s.commit()


@pytest.mark.parametrize("matching", [True, False])
def test_concurrent_initializer_rechecks_the_winning_format(session, monkeypatch, matching):
    # Simulate another starter committing between the first read and insert.
    existing = session.get(LedgerFormat, 1)
    if not matching:
        existing.format = 2
        session.add(existing)
        session.commit()
    session.expunge(existing)
    original_get = session.get
    reads = 0

    def racing_get(table, key, **kwargs):
        nonlocal reads
        if table is LedgerFormat:
            reads += 1
            if reads == 1:
                return None
        return original_get(table, key, **kwargs)

    monkeypatch.setattr(session, "get", racing_get)
    if matching:
        ledger.init_ledger(session)
    else:
        with pytest.raises(RuntimeError, match="ledger format .* is not"):
            ledger.init_ledger(session)
    assert reads == 2
    assert len(_rows(session)) == 1
