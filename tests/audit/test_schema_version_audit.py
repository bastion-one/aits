"""A stored row whose schema version has no assembler is reported by the audit."""

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from app.models import AgentConfig
from tests.helpers import make_config, verify_reports_malformed_nodes


@pytest.mark.skipif(
    not verify_reports_malformed_nodes(),
    reason="dag.verify does not yet report malformed_node (audit/malformed-node-finding)",
)
def test_audit_reports_a_row_with_an_unknown_version(client: TestClient, session: Session) -> None:
    config = make_config(client)
    row = session.get(AgentConfig, bytes.fromhex(config["cid"]))
    row.v = 2
    session.add(row)
    session.commit()

    audit = client.get("/audit/").json()
    assert audit["valid"] is False
    failure = next(f for f in audit["failures"] if f["property_violated"] == "malformed_node")
    assert failure["record_key"] == config["cid"]
