"""Factories shared across the unit suites: build the standard node chain
(agent -> config -> artifact -> DUT -> root LT) through the API."""

from typing import Any

from fastapi.testclient import TestClient

from app import dag

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


def create_root(
    client: TestClient, actor: str = "A", transformation: str = "root", **overrides: Any
) -> dict:
    payload = {
        "actor_id": actor,
        "step_id": "s",
        "transformation": transformation,
        **overrides,
    }
    r = client.post("/lineage/", json=payload)
    assert r.status_code == 201, r.text
    return r.json()


def create_node(
    client: TestClient,
    root_cid: str,
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
    r = client.post(f"/lineage/{root_cid}/nodes/", json=payload)
    assert r.status_code == 201, r.text
    return r.json()


def inline_dut(agent_uuid: str, **overrides: Any) -> dict[str, Any]:
    payload = {
        "agent_uuid": agent_uuid,
        "span_id": "span-1",
        "input_context": "find PO",
        "agent_output": "PO-88",
        **overrides,
    }
    return payload


def make_dut_chain(client: TestClient) -> tuple[dict, dict, dict]:
    """One agent + config + DUT, the common preamble."""
    agent = make_agent(client)
    config = make_config(client)
    dut = make_dut(client, agent["uuid"], config["cid"])
    return agent, config, dut


def _raise_type_error(_cid: bytes):
    raise TypeError("no assembler")


def verify_reports_malformed_nodes() -> bool:
    """Whether ``dag.verify`` turns a resolver ``TypeError`` into a
    ``malformed_node`` finding (it does once audit/malformed-node-finding lands)."""
    try:
        return dag.verify(b"\x00" * 32, _raise_type_error).property_violated == "malformed_node"
    except TypeError:
        return False
