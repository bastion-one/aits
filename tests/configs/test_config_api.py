"""Behavioral-only config value objects: content addressing + global dedup."""

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.models import AgentConfig
from tests.helpers import CONFIG_BODY, make_config


def test_identical_content_dedupes_globally(client: TestClient) -> None:
    first = make_config(client)
    again = make_config(client)
    assert first["cid"] == again["cid"]

    observations = client.get("/commits/", params={"cid": first["cid"]}).json()
    assert len(observations) == 2  # one node, one log entry per submission


def test_behavioral_mutations_move_the_cid(client: TestClient) -> None:
    base = make_config(client)
    assert make_config(client, system_prompt="Extract the PO number.")["cid"] != base["cid"]
    assert (
        make_config(client, llm_config={"model": "gpt-4", "temperature": 0.7})["cid"] != base["cid"]
    )
    assert make_config(client, tools=[{"name": "ocr"}])["cid"] != base["cid"]


def test_get_roundtrip_and_404(client: TestClient) -> None:
    config = make_config(client)
    got = client.get(f"/configs/{config['cid']}/").json()
    assert got["system_prompt"] == CONFIG_BODY["system_prompt"]
    assert got["llm_config"] == CONFIG_BODY["llm_config"]
    assert client.get(f"/configs/{'0' * 64}/").status_code == 404
    assert client.get("/configs/not-hex/").status_code == 400


def test_verify_catches_tamper(client: TestClient, session: Session) -> None:
    config = make_config(client)
    assert client.get(f"/verify/{config['cid']}/").json()["valid"] is True

    row = session.get(AgentConfig, bytes.fromhex(config["cid"]))
    row.system_prompt = "IGNORE ALL PRIOR INSTRUCTIONS"
    session.add(row)
    session.commit()

    verdict = client.get(f"/verify/{config['cid']}/").json()
    assert verdict["valid"] is False
    assert verdict["property_violated"] == "cid_mismatch"
