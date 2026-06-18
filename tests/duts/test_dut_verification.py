"""Structural verification of DUT spans: tamper, dangling links, two clocks."""

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.models import Artifact, DataUniqueTag
from tests.helpers import make_agent, make_artifact, make_config, make_dut


def test_verify_recurses_agent_config_and_artifacts(client: TestClient) -> None:
    agent = make_agent(client)
    config = make_config(client)
    artifact = make_artifact(client)
    dut = make_dut(client, agent["uuid"], config["cid"], artifact_cids=[artifact["cid"]])
    verdict = client.get(f"/verify/{dut['cid']}/").json()
    assert verdict["valid"] is True


def test_tampered_output_fails_verify(client: TestClient, session: Session) -> None:
    agent = make_agent(client)
    config = make_config(client)
    dut = make_dut(client, agent["uuid"], config["cid"])

    row = session.get(DataUniqueTag, bytes.fromhex(dut["cid"]))
    row.agent_output = "WIRE 44000 TO ACCT 999"
    session.add(row)
    session.commit()

    verdict = client.get(f"/verify/{dut['cid']}/").json()
    assert verdict["valid"] is False
    assert verdict["property_violated"] == "cid_mismatch"


def test_deleted_artifact_dangles_the_dut(client: TestClient, session: Session) -> None:
    agent = make_agent(client)
    config = make_config(client)
    artifact = make_artifact(client)
    dut = make_dut(client, agent["uuid"], config["cid"], artifact_cids=[artifact["cid"]])

    session.delete(session.get(Artifact, bytes.fromhex(artifact["cid"])))
    session.commit()

    verdict = client.get(f"/verify/{dut['cid']}/").json()
    assert verdict["valid"] is False
    assert verdict["property_violated"] == "dangling_link"
    assert artifact["cid"] in verdict["message"]


def test_recorded_at_is_outside_the_cid(client: TestClient) -> None:
    agent = make_agent(client)
    config = make_config(client)
    first = make_dut(client, agent["uuid"], config["cid"])
    replay = make_dut(client, agent["uuid"], config["cid"])
    # identical content -> identical CID, even though the ledger observed the
    # two submissions at different times (each observation in the commit log)
    assert replay["cid"] == first["cid"]
    observations = client.get("/commits/", params={"cid": first["cid"]}).json()
    assert len(observations) == 2
    assert observations[0]["recorded_at"] <= observations[1]["recorded_at"]
