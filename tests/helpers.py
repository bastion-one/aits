"""Factories shared across the unit suites: build the standard node chain
(agent -> config -> artifact -> DUT -> session) through the API."""

from typing import Any

from fastapi.testclient import TestClient

CONFIG_BODY: dict[str, Any] = {
    "system_prompt": "Extract the invoice total.",
    "llm_config": {"model": "gpt-4", "temperature": 0.0},
    "tools": [],
    "metadata": {},
}

OCCURRED_AT = "2026-06-10T09:01:00+00:00"


def make_agent(client: TestClient, name: str = "alpha") -> dict:
    r = client.post("/agents/", json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()


def make_config(client: TestClient, **overrides: Any) -> dict:
    r = client.post("/configs/", json={**CONFIG_BODY, **overrides})
    assert r.status_code == 201, r.text
    return r.json()


def make_artifact(client: TestClient, body: bytes = b"%PDF-1.7 invoice") -> dict:
    r = client.post(
        "/artifacts/", content=body, headers={"Content-Type": "application/octet-stream"}
    )
    assert r.status_code == 201, r.text
    return r.json()


def make_dut(client: TestClient, agent_uuid: str, config_cid: str, **overrides: Any) -> dict:
    payload = {
        "agent_uuid": agent_uuid,
        "config_cid": config_cid,
        "span_id": "span-1",
        "business_object_keys": ["INV-4471"],
        "sequence": 0,
        "input_context": "Extract total from invoice_4471.pdf",
        "agent_output": "total = 4400.00 USD",
        "occurred_at": OCCURRED_AT,
        "artifact_cids": [],
        **overrides,
    }
    r = client.post("/duts/", json=payload)
    assert r.status_code == 201, r.text
    return r.json()


def start_session(client: TestClient, actor: str = "A", **overrides: Any) -> dict:
    r = client.post("/lineage/", json={"actor_id": actor, "step_id": "s", **overrides})
    assert r.status_code == 201, r.text
    return r.json()


def append_node(
    client: TestClient,
    session_uuid: str,
    prev: list[str],
    transformation: str = "work",
    actor: str = "A",
    **overrides: Any,
) -> dict:
    payload = {
        "actor_id": actor,
        "step_id": "s",
        "transformation": transformation,
        "prev": prev,
        **overrides,
    }
    r = client.post(f"/lineage/{session_uuid}/append/", json=payload)
    assert r.status_code == 201, r.text
    return r.json()


def make_dut_chain(client: TestClient) -> tuple[dict, dict, dict]:
    """One agent + config + DUT, the common preamble."""
    agent = make_agent(client)
    config = make_config(client)
    dut = make_dut(client, agent["uuid"], config["cid"])
    return agent, config, dut
