"""HTTP translation of deliberately corrupted stored prev ancestry."""

import pytest

from app import ledger
from app.models import LineageTag
from app.routers.lineage import _raise_graph_error
from tests.helpers import create_node, create_root


@pytest.mark.parametrize("corruption", ["missing", "cycle", "conflict"])
@pytest.mark.parametrize(
    "endpoint,field",
    [
        ("node", "lt_cid"),
        ("list", "cid"),
        ("graph", "root_cid"),
        ("prev", "prev"),
        ("derived", "derived_from"),
    ],
)
def test_corrupted_ancestry_reports_exact_http_error(client, session, corruption, endpoint, field):
    root = create_root(client)
    other = create_root(client, actor="B")
    child = create_node(
        client, root["cid"], [root["cid"]], transformation="private stored payload"
    )["node"]
    row = session.get(LineageTag, bytes.fromhex(child["cid"]))
    if corruption == "missing":
        bad_cid = "ab" * 32
        row.prev_cids = [bad_cid]
        status = 404
        detail = f"Lineage node not found in {field}: {bad_cid}"
    elif corruption == "cycle":
        row.prev_cids = [child["cid"]]
        status = 400
        detail = f"{field} {child['cid']} has cyclic prev ancestry"
    else:
        row.prev_cids = [root["cid"], other["cid"]]
        status = 400
        roots = ", ".join(sorted([root["cid"], other["cid"]]))
        detail = f"{field} {child['cid']} resolves to multiple roots: {roots}"
    session.add(row)
    session.commit()
    if endpoint == "node":
        response = client.get(f"/lineage/nodes/{child['cid']}/")
    elif endpoint == "list":
        response = client.get("/lineage/")
    elif endpoint == "graph":
        response = client.get(f"/lineage/{root['cid']}/")
    else:
        response = client.post(
            f"/lineage/{root['cid']}/nodes/",
            json={
                "actor_id": "A",
                "step_id": "s",
                "transformation": "private submitted payload",
                "prev": [child["cid"]] if endpoint == "prev" else [root["cid"]],
                "derived_from": [child["cid"]] if endpoint == "derived" else [],
            },
        )
    assert response.status_code == status
    assert response.json() == {"detail": detail}
    assert "private" not in response.text


def test_unknown_graph_error_is_reraised():
    error = ledger.LineageGraphError(b"\x01" * 32)
    with pytest.raises(ledger.LineageGraphError) as caught:
        _raise_graph_error(error, field="cid")
    assert caught.value is error


@pytest.mark.parametrize(
    "cid,status,detail",
    [
        ("invalid", 400, "root_cid is not a 64-character hex CID: invalid"),
        ("AB" * 32, 404, "Lineage node not found in root_cid: " + "ab" * 32),
    ],
)
def test_root_path_error_identifies_field_and_normalizes_cid(client, cid, status, detail):
    response = client.get(f"/lineage/{cid}/")
    assert response.status_code == status
    assert response.json() == {"detail": detail}
