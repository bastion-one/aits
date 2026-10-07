"""Unified node creation: structural, existing DUT, and inline DUT modes."""

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.models import CommitLogEntry, DataUniqueTag, LineageTag
from tests.helpers import (
    CONFIG_BODY,
    create_node,
    create_root,
    inline_dut,
    make_agent,
    make_artifact,
    make_config,
    make_dut,
)


def _register(client: TestClient, name: str = "worker") -> dict:
    r = client.post("/agents/", json={"name": name, "config": CONFIG_BODY})
    assert r.status_code == 201, r.text
    return r.json()


def test_structural_node_has_null_dut(client: TestClient) -> None:
    genesis = create_root(client)
    created = create_node(client, genesis["cid"], [genesis["cid"]])
    assert created["dut"] is None
    assert created["node"]["dut"] is None
    assert created["root"] == genesis["cid"] == created["node"]["root"]


def test_dut_cid_attaches_existing_dut(client: TestClient) -> None:
    """Attach an existing DUT without recording a second observation of that DUT."""
    agent = make_agent(client)
    config = make_config(client)
    dut = make_dut(client, agent["uuid"], config["cid"])
    genesis = create_root(client)
    created = create_node(client, genesis["cid"], [genesis["cid"]], dut_cid=dut["cid"])
    assert created["dut"]["cid"] == dut["cid"]
    assert created["node"]["dut"] == dut["cid"]
    observations = client.get("/commits/", params={"cid": dut["cid"]}).json()
    assert len(observations) == 1


def test_inline_dut_creates_dut_and_lt(client: TestClient) -> None:
    """Return and persist a DUT linked to the node created in the same request."""
    agent = _register(client)
    genesis = create_root(client)
    created = create_node(
        client,
        genesis["cid"],
        [genesis["cid"]],
        actor=agent["name"],
        dut=inline_dut(agent["uuid"], agent_output="PO-88"),
    )
    assert created["dut"] is not None
    assert created["dut"]["agent_output"] == "PO-88"
    assert created["dut"]["config_cid"] == agent["active_config_cid"]
    assert created["node"]["dut"] == created["dut"]["cid"]
    assert created["node"]["root"] == genesis["cid"]
    assert client.get(f"/duts/{created['dut']['cid']}/").json()["cid"] == created["dut"]["cid"]


def test_inline_dut_defaults_to_the_active_config(client: TestClient) -> None:
    agent = _register(client)
    genesis = create_root(client)
    created = create_node(client, genesis["cid"], [genesis["cid"]], dut=inline_dut(agent["uuid"]))
    assert created["dut"]["config_cid"] == agent["active_config_cid"]

    # An explicit config CID overrides the agent's active configuration.
    other = make_config(client, system_prompt="other recipe")
    explicit = create_node(
        client,
        genesis["cid"],
        [created["node"]["cid"]],
        dut=inline_dut(agent["uuid"], span_id="span-x", config_cid=other["cid"]),
    )
    assert explicit["dut"]["config_cid"] == other["cid"]


def test_inline_dut_without_active_config_is_409(client: TestClient) -> None:
    agent = make_agent(client)  # Register without a configuration.
    genesis = create_root(client)
    r = client.post(
        f"/lineage/{genesis['cid']}/nodes/",
        json={
            "actor_id": "a",
            "step_id": "s",
            "transformation": "t",
            "prev": [genesis["cid"]],
            "dut": inline_dut(agent["uuid"]),
        },
    )
    assert r.status_code == 409
    assert "no active config" in r.json()["detail"]


def test_inline_dut_carries_artifacts(client: TestClient) -> None:
    agent = _register(client)
    artifact = make_artifact(client, b"invoice bytes")
    genesis = create_root(client)
    created = create_node(
        client,
        genesis["cid"],
        [genesis["cid"]],
        dut=inline_dut(agent["uuid"], artifact_cids=[artifact["cid"]]),
    )
    assert created["dut"]["artifact_cids"] == [artifact["cid"]]
    assert client.get(f"/verify/{created['node']['cid']}/").json()["valid"] is True


def test_inline_dut_time_defaults_to_lt_time(client: TestClient) -> None:
    agent = _register(client)
    genesis = create_root(client)
    when = "2026-06-10T09:07:00+00:00"
    created = create_node(
        client,
        genesis["cid"],
        [genesis["cid"]],
        occurred_at=when,
        dut=inline_dut(agent["uuid"]),
    )
    assert created["node"]["occurred_at"] == "2026-06-10T09:07:00Z"
    assert created["dut"]["occurred_at"] == "2026-06-10T09:07:00Z"


def test_explicit_parents_override_any_frontier(client: TestClient) -> None:
    agent = _register(client)
    genesis = create_root(client)
    first = create_node(client, genesis["cid"], [genesis["cid"]], dut=inline_dut(agent["uuid"]))[
        "node"
    ]
    fork_a = create_node(
        client,
        genesis["cid"],
        [first["cid"]],
        dut=inline_dut(agent["uuid"], span_id="fork-a"),
    )["node"]
    # Reuse the same parent to fork instead of extending fork_a.
    fork_b = create_node(
        client,
        genesis["cid"],
        [first["cid"]],
        dut=inline_dut(agent["uuid"], span_id="fork-b"),
    )["node"]
    frontier = client.get(f"/lineage/{genesis['cid']}/").json()["frontier"]
    assert sorted(frontier) == sorted([fork_a["cid"], fork_b["cid"]])
    # Join both branches explicitly; the server does not select frontier parents.
    join = create_node(
        client,
        genesis["cid"],
        [fork_a["cid"], fork_b["cid"]],
        dut=inline_dut(agent["uuid"], span_id="join"),
    )["node"]
    assert sorted(join["prev"]) == sorted(frontier)


def test_cross_root_derived_from_uses_exact_lt_cids(client: TestClient) -> None:
    helper_a = _register(client, "sub-A")
    helper_b = _register(client, "sub-B")
    root_a = create_root(client, actor="sub-A")
    root_b = create_root(client, actor="sub-B")
    head_a = create_node(
        client,
        root_a["cid"],
        [root_a["cid"]],
        actor="sub-A",
        dut=inline_dut(helper_a["uuid"], agent_output="PO-88"),
    )["node"]
    head_b = create_node(
        client,
        root_b["cid"],
        [root_b["cid"]],
        actor="sub-B",
        dut=inline_dut(helper_b["uuid"], span_id="b1", agent_output="ACME Corp"),
    )["node"]

    supervisor = _register(client, "super")
    super_root = create_root(client, actor="super")
    merge = create_node(
        client,
        super_root["cid"],
        [super_root["cid"]],
        actor="super",
        transformation="Summarization",
        derived_from=[head_a["cid"], head_b["cid"]],
        dut=inline_dut(
            supervisor["uuid"],
            span_id="s1",
            agent_output="pay ACME Corp 4400 ref PO-88",
        ),
    )
    assert sorted(merge["node"]["derived_from"]) == sorted([head_a["cid"], head_b["cid"]])

    steps = client.get(f"/lineage/nodes/{merge['node']['cid']}/traceback/").json()
    assert {"PO-88", "ACME Corp"} <= {s["agent_output"] for s in steps}
    assert client.get(f"/verify/{merge['node']['cid']}/").json()["valid"] is True


def test_invalid_prev_creates_no_inline_dut(client: TestClient, session: Session) -> None:
    """Leave DUTs, lineage nodes, and observations unchanged when parent validation fails."""
    agent = _register(client)
    genesis = create_root(client)
    before_duts = len(session.exec(select(DataUniqueTag)).all())
    before_lts = len(session.exec(select(LineageTag)).all())
    before_logs = len(session.exec(select(CommitLogEntry)).all())
    r = client.post(
        f"/lineage/{genesis['cid']}/nodes/",
        json={
            "actor_id": agent["name"],
            "step_id": "s",
            "transformation": "t",
            "prev": ["0" * 64],
            "dut": inline_dut(agent["uuid"], input_context="should-not-store"),
        },
    )
    assert r.status_code == 404
    session.expire_all()
    assert len(session.exec(select(DataUniqueTag)).all()) == before_duts
    assert len(session.exec(select(LineageTag)).all()) == before_lts
    assert len(session.exec(select(CommitLogEntry)).all()) == before_logs


def test_inline_success_appends_two_observations(client: TestClient, session: Session) -> None:
    """Record separate observations for the DUT and LT created by one inline request."""
    agent = _register(client)
    genesis = create_root(client)
    before = len(session.exec(select(CommitLogEntry)).all())
    created = create_node(client, genesis["cid"], [genesis["cid"]], dut=inline_dut(agent["uuid"]))
    session.expire_all()
    after = session.exec(select(CommitLogEntry)).all()
    assert len(after) == before + 2
    cids = {e.cid.hex() for e in after[before:]}
    assert created["dut"]["cid"] in cids
    assert created["node"]["cid"] in cids


def test_structural_success_appends_one_observation(client: TestClient, session: Session) -> None:
    genesis = create_root(client)
    before = len(session.exec(select(CommitLogEntry)).all())
    created = create_node(client, genesis["cid"], [genesis["cid"]])
    session.expire_all()
    added = session.exec(select(CommitLogEntry)).all()[before:]
    assert [e.cid.hex() for e in added] == [created["node"]["cid"]]


def test_inline_config_cid_must_be_strict_hex(client: TestClient, session: Session) -> None:
    """Reject a spaced config CID without storing data or echoing prompt and output."""
    agent = _register(client)
    genesis = create_root(client)
    cid = agent["active_config_cid"]
    spaced = " ".join(cid[i : i + 2] for i in range(0, 64, 2))
    before_duts = len(session.exec(select(DataUniqueTag)).all())
    before_lts = len(session.exec(select(LineageTag)).all())
    before_logs = len(session.exec(select(CommitLogEntry)).all())
    prompt = "INLINE-PROMPT-SHOULD-NOT-LEAK"
    completion = "INLINE-COMPLETION-SHOULD-NOT-LEAK"
    r = client.post(
        f"/lineage/{genesis['cid']}/nodes/",
        json={
            "actor_id": agent["name"],
            "step_id": "s",
            "transformation": "t",
            "prev": [genesis["cid"]],
            "dut": inline_dut(
                agent["uuid"],
                config_cid=spaced,
                input_context=prompt,
                agent_output=completion,
            ),
        },
    )
    assert r.status_code == 400
    assert "dut.config_cid" in r.json()["detail"]
    assert prompt not in r.text
    assert completion not in r.text
    session.expire_all()
    assert len(session.exec(select(DataUniqueTag)).all()) == before_duts
    assert len(session.exec(select(LineageTag)).all()) == before_lts
    assert len(session.exec(select(CommitLogEntry)).all()) == before_logs


def test_inline_artifact_cids_must_be_strict_hex(client: TestClient, session: Session) -> None:
    """Reject a spaced artifact CID without storing data or echoing prompt and output."""
    agent = _register(client)
    artifact = make_artifact(client, b"invoice bytes")
    genesis = create_root(client)
    cid = artifact["cid"]
    spaced = " ".join(cid[i : i + 2] for i in range(0, 64, 2))
    before_duts = len(session.exec(select(DataUniqueTag)).all())
    before_lts = len(session.exec(select(LineageTag)).all())
    before_logs = len(session.exec(select(CommitLogEntry)).all())
    prompt = "INLINE-PROMPT-SHOULD-NOT-LEAK"
    completion = "INLINE-COMPLETION-SHOULD-NOT-LEAK"
    r = client.post(
        f"/lineage/{genesis['cid']}/nodes/",
        json={
            "actor_id": agent["name"],
            "step_id": "s",
            "transformation": "t",
            "prev": [genesis["cid"]],
            "dut": inline_dut(
                agent["uuid"],
                artifact_cids=[spaced],
                input_context=prompt,
                agent_output=completion,
            ),
        },
    )
    assert r.status_code == 400
    assert "dut.artifact_cids" in r.json()["detail"]
    assert prompt not in r.text
    assert completion not in r.text
    session.expire_all()
    assert len(session.exec(select(DataUniqueTag)).all()) == before_duts
    assert len(session.exec(select(LineageTag)).all()) == before_lts
    assert len(session.exec(select(CommitLogEntry)).all()) == before_logs


def test_inline_refs_accept_uppercase_and_mixed_case(client: TestClient) -> None:
    """Resolve mixed-case inline config and artifact CIDs to existing lowercase resources."""
    agent = _register(client)
    artifact = make_artifact(client, b"invoice bytes")
    genesis = create_root(client)
    config_cid = agent["active_config_cid"]
    mixed = "".join(c.upper() if i % 2 == 0 else c for i, c in enumerate(config_cid))
    created = create_node(
        client,
        genesis["cid"],
        [genesis["cid"]],
        actor=agent["name"],
        dut=inline_dut(
            agent["uuid"],
            config_cid=mixed,
            artifact_cids=[artifact["cid"].upper()],
        ),
    )
    assert created["dut"]["config_cid"] == config_cid
    assert created["dut"]["artifact_cids"] == [artifact["cid"]]
    assert created["dut"]["config_cid"] == created["dut"]["config_cid"].lower()


def test_inline_reference_case_does_not_change_identity(client: TestClient) -> None:
    """Case of config and artifact references is not part of DUT or LT identity."""
    agent = _register(client)
    artifact = make_artifact(client, b"invoice bytes")
    genesis = create_root(client)
    when = "2026-06-10T09:07:00+00:00"
    config_cid = agent["active_config_cid"]
    artifact_cid = artifact["cid"]
    assert any(c.isalpha() for c in config_cid + artifact_cid)
    first = create_node(
        client,
        genesis["cid"],
        [genesis["cid"]],
        actor=agent["name"],
        occurred_at=when,
        dut=inline_dut(
            agent["uuid"],
            span_id="span-case",
            config_cid=config_cid,
            artifact_cids=[artifact_cid],
        ),
    )
    second = create_node(
        client,
        genesis["cid"],
        [genesis["cid"]],
        actor=agent["name"],
        occurred_at=when,
        dut=inline_dut(
            agent["uuid"],
            span_id="span-case",
            config_cid=config_cid.upper(),
            artifact_cids=[artifact_cid.upper()],
        ),
    )
    assert second["dut"]["cid"] == first["dut"]["cid"]
    assert second["node"]["cid"] == first["node"]["cid"]
