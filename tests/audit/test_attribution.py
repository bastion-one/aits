"""The identity AITS authenticated is recorded on every commit-log entry."""

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine, select

from app import ledger
from app.config import get_settings
from app.models import AgentConfig, CommitLogEntry
from tests.helpers import CONFIG_BODY

KEY = {"Authorization": "Bearer s3cret"}


@pytest.fixture
def keyed(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(get_settings(), "auth_disabled", False)
    monkeypatch.setattr(get_settings(), "auth_service_keys", "gateway:s3cret")
    return client


def _entries(session: Session) -> list[CommitLogEntry]:
    session.expire_all()
    return list(session.exec(select(CommitLogEntry).order_by(CommitLogEntry.seq)).all())


def test_entry_records_the_authenticated_key_name(keyed: TestClient, session: Session) -> None:
    assert keyed.post("/configs/", json=CONFIG_BODY, headers=KEY).status_code == 201
    [entry] = _entries(session)
    assert entry.principal == "gateway"
    [listed] = keyed.get("/commits/", headers=KEY).json()
    assert listed["principal"] == "gateway"


def test_open_mode_records_anonymous(client: TestClient, session: Session) -> None:
    assert client.post("/configs/", json=CONFIG_BODY).status_code == 201
    [entry] = _entries(session)
    assert entry.principal == "anonymous"


def test_rewriting_principal_breaks_the_chain(keyed: TestClient, session: Session) -> None:
    keyed.post("/configs/", json=CONFIG_BODY, headers=KEY)
    [entry] = _entries(session)
    entry.principal = "someone-else"
    session.commit()
    result = ledger.verify_commit_log(session)
    assert result.is_err and result.property_violated == "entry_hash_mismatch"
    entry.principal = "gateway"
    session.commit()
    assert ledger.verify_commit_log(session).is_ok


def test_ledger_refuses_an_unattested_append() -> None:
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        with pytest.raises(RuntimeError, match="attest"):
            ledger.record(session, AgentConfig(system_prompt="x"))
        ledger.attest(session, "script")
        stored, entry = ledger.record(session, AgentConfig(system_prompt="x"))
        assert entry.principal == "script"
        assert stored.cid == entry.cid
