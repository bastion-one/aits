"""Request field-length and list-length limits return 422, not unbounded storage."""

import pytest
from fastapi.testclient import TestClient

from app.routers.common import MAX_ITEMS, MAX_LABEL, MAX_TEXT
from tests.helpers import CONFIG_BODY, create_root, make_agent, make_artifact, make_config

TOO_LONG_LABEL = "x" * (MAX_LABEL + 1)
TOO_MANY = ["k"] * (MAX_ITEMS + 1)


def test_long_text_at_the_limit_is_accepted(client: TestClient) -> None:
    r = client.post("/configs/", json={**CONFIG_BODY, "system_prompt": "x" * MAX_TEXT})
    assert r.status_code == 201


@pytest.mark.parametrize(
    "overrides",
    [{"system_prompt": "x" * (MAX_TEXT + 1)}, {"tools": [{}] * (MAX_ITEMS + 1)}],
)
def test_config_bounds(client: TestClient, overrides: dict) -> None:
    assert client.post("/configs/", json={**CONFIG_BODY, **overrides}).status_code == 422


def test_422_does_not_echo_submitted_text(client: TestClient) -> None:
    secret = "s3cret-" + "x" * (MAX_TEXT + 1)
    r = client.post("/configs/", json={**CONFIG_BODY, "system_prompt": secret})
    assert r.status_code == 422
    assert "s3cret-" not in r.text
    assert len(r.content) < 4096


def test_agent_name_bound(client: TestClient) -> None:
    assert client.post("/agents/", json={"name": TOO_LONG_LABEL}).status_code == 422
    agent = make_agent(client)
    r = client.patch(f"/agents/{agent['uuid']}/", json={"name": TOO_LONG_LABEL})
    assert r.status_code == 422


@pytest.mark.parametrize(
    "overrides",
    [
        {"span_id": TOO_LONG_LABEL},
        {"input_context": "x" * (MAX_TEXT + 1)},
        {"business_object_keys": [TOO_LONG_LABEL]},
        {"business_object_keys": TOO_MANY},
        {"artifact_cids": TOO_MANY},
    ],
)
def test_dut_bounds(client: TestClient, overrides: dict) -> None:
    agent, config = make_agent(client), make_config(client)
    body = {
        "agent_uuid": agent["uuid"],
        "config_cid": config["cid"],
        "span_id": "s",
        "sequence": 0,
        "input_context": "i",
        "agent_output": "o",
        **overrides,
    }
    assert client.post("/duts/", json=body).status_code == 422


def test_lineage_bounds(client: TestClient) -> None:
    root = {"actor_id": TOO_LONG_LABEL, "step_id": "s", "transformation": "t"}
    assert client.post("/lineage/", json=root).status_code == 422

    genesis = create_root(client)
    agent = make_agent(client)
    node = {"actor_id": "A", "step_id": "s", "transformation": "t", "prev": [genesis["cid"]]}
    url = f"/lineage/{genesis['cid']}/nodes/"
    assert client.post(url, json={**node, "prev": TOO_MANY}).status_code == 422
    assert client.post(url, json={**node, "derived_from": TOO_MANY}).status_code == 422

    secret = "s3cret-" + "x" * MAX_TEXT
    inline = {"agent_uuid": agent["uuid"], "span_id": "s", "input_context": secret}
    r = client.post(url, json={**node, "dut": {**inline, "agent_output": "o"}})
    assert r.status_code == 422
    assert "s3cret-" not in r.text, "lineage 422s must not echo inline DUT text"


def test_artifact_alias_bounds(client: TestClient) -> None:
    artifact = make_artifact(client)
    r = client.post(
        f"/artifacts/{artifact['cid']}/aliases/",
        json={"source": "s", "alias": TOO_LONG_LABEL},
    )
    assert r.status_code == 422
    r = client.post(
        "/artifacts/upload/",
        files={"file": ("f.txt", b"bytes")},
        data={"alias": TOO_LONG_LABEL},
    )
    assert r.status_code == 422
