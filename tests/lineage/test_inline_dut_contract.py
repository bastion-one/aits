"""Exact inline DUT payloads survive the atomic LT/DUT commit."""

from datetime import datetime

import pytest
from sqlmodel import Session, select

from app.models import DataUniqueTag, LineageTag
from tests.helpers import create_node, create_root, inline_dut, make_dut_chain

LT_TIME = "2026-06-10T09:01:00+00:00"
DUT_TIME = "2026-06-09T18:30:01.123456-05:00"
KEYS = ["INV-4471", "PO-88", "customer:123"]


@pytest.mark.parametrize("explicit", [True, False])
def test_inline_fields_and_distinct_event_times(client, engine, explicit):
    agent, config, _ = make_dut_chain(client)
    root = create_root(client)
    dut = inline_dut(agent["uuid"], config_cid=config["cid"], business_object_keys=KEYS, sequence=7)
    if explicit:
        dut["occurred_at"] = DUT_TIME
    created = create_node(client, root["cid"], [root["cid"]], occurred_at=LT_TIME, dut=dut)
    expected_dut = "2026-06-09T23:30:01.123456Z" if explicit else "2026-06-10T09:01:00Z"
    assert created["dut"]["business_object_keys"] == KEYS
    assert created["dut"]["sequence"] == 7
    assert created["dut"]["occurred_at"] == expected_dut
    assert created["node"]["occurred_at"] == "2026-06-10T09:01:00Z"
    assert client.get(f"/duts/{created['dut']['cid']}/").json() == created["dut"]
    with Session(engine) as fresh:
        saved_dut = fresh.get(DataUniqueTag, bytes.fromhex(created["dut"]["cid"]))
        saved_lt = fresh.get(LineageTag, bytes.fromhex(created["node"]["cid"]))
        assert saved_dut.business_object_keys == KEYS
        assert saved_dut.sequence == 7
        assert saved_dut.occurred_at == datetime.fromisoformat(expected_dut)
        assert saved_lt.occurred_at == datetime.fromisoformat(LT_TIME)


def test_naive_explicit_inline_event_time_is_rejected_without_writes(client, engine):
    agent, config, _ = make_dut_chain(client)
    root = create_root(client)
    with Session(engine) as fresh:
        before = len(fresh.exec(select(DataUniqueTag)).all())
    response = client.post(
        f"/lineage/{root['cid']}/nodes/",
        json={
            "actor_id": "A",
            "step_id": "s",
            "transformation": "t",
            "prev": [root["cid"]],
            "occurred_at": LT_TIME,
            "dut": inline_dut(
                agent["uuid"], config_cid=config["cid"], occurred_at="2026-06-09T18:30:01"
            ),
        },
    )
    assert response.status_code == 400
    assert response.json() == {"detail": "dut.occurred_at must be an aware datetime"}
    with Session(engine) as fresh:
        assert len(fresh.exec(select(DataUniqueTag)).all()) == before


@pytest.mark.parametrize("field", ["config_cid", "artifact_cids"])
def test_inline_malformed_link_identifies_exact_field(client, field):
    agent, config, _ = make_dut_chain(client)
    root = create_root(client)
    dut = inline_dut(agent["uuid"], config_cid=config["cid"])
    dut[field] = ["invalid"] if field == "artifact_cids" else "invalid"
    response = client.post(
        f"/lineage/{root['cid']}/nodes/",
        json={
            "actor_id": "A",
            "step_id": "s",
            "transformation": "t",
            "prev": [root["cid"]],
            "dut": dut,
        },
    )
    assert response.status_code == 400
    assert response.json() == {"detail": f"dut.{field} is not a 64-character hex CID: invalid"}


def test_inline_no_active_config_gives_actionable_detail(client):
    from tests.helpers import make_agent

    agent = make_agent(client)
    root = create_root(client)
    response = client.post(
        f"/lineage/{root['cid']}/nodes/",
        json={
            "actor_id": "A",
            "step_id": "s",
            "transformation": "t",
            "prev": [root["cid"]],
            "dut": inline_dut(agent["uuid"]),
        },
    )
    assert response.status_code == 409
    assert response.json() == {
        "detail": (
            f"Agent {agent['uuid']} has no active config; pass config_cid or activate one first"
        )
    }
