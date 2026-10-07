"""OpenAPI contract for the root-LT lineage API."""

import tomllib
from pathlib import Path

from app.main import app


def _spec() -> dict:
    return app.openapi()


def test_lineage_paths_and_operation_ids() -> None:
    """Keep generated SDK operations aligned with the root-based API."""
    paths = _spec()["paths"]
    assert "/lineage/" in paths
    assert "/lineage/{root_cid}/nodes/" in paths
    assert "/lineage/nodes/{lt_cid}/" in paths
    assert "/lineage/{root_cid}/" in paths
    assert "/lineage/{session_uuid}/append/" not in paths
    assert "/lineage/{session_uuid}/record/" not in paths
    assert "/agents/{agent_uuid}/record/" not in paths

    assert paths["/lineage/"]["post"]["operationId"] == "lineage_create_root"
    assert (
        paths["/lineage/{root_cid}/nodes/"]["post"]["operationId"] == "lineage_create_lineage_node"
    )
    assert paths["/lineage/nodes/{lt_cid}/"]["get"]["operationId"] == "lineage_get_lineage_node"
    assert paths["/lineage/{root_cid}/"]["get"]["operationId"] == "lineage_get_lineage_graph"
    assert paths["/lineage/"]["get"]["operationId"] == "lineage_list_roots"


def test_node_create_declares_location_and_errors() -> None:
    post = _spec()["paths"]["/lineage/{root_cid}/nodes/"]["post"]
    created = post["responses"]["201"]
    assert "Location" in created["headers"]
    assert "400" in post["responses"]
    assert "404" in post["responses"]
    assert "409" in post["responses"]


def test_request_models_require_prev_and_document_mutual_exclusion() -> None:
    """Publish explicit parents and DUT input constraints for API and SDK consumers."""
    schemas = _spec()["components"]["schemas"]
    node = schemas["LineageNodeCreate"]
    assert "prev" in node["required"]
    assert node["properties"]["prev"]["minItems"] == 1
    assert "dut_cid" in node["properties"]
    assert "dut" in node["properties"]
    assert "mutually exclusive" in node["properties"]["dut_cid"]["description"].lower()
    assert "mutually exclusive" in node["properties"]["dut"]["description"].lower()
    assert node.get("additionalProperties") is False

    root = schemas["LineageRootCreate"]
    assert set(root["required"]) == {"actor_id", "step_id", "transformation"}
    assert root.get("additionalProperties") is False
    assert schemas["InlineDUTCreate"].get("additionalProperties") is False


def test_dut_response_nullability_is_explicit() -> None:
    """Describe structural nodes and distinguish a node's DUT CID from the full DUT body."""
    schemas = _spec()["components"]["schemas"]
    node_dut = schemas["LineageRead"]["properties"]["dut"]
    assert "null" in node_dut.get("anyOf", [])[0].get("type", "") or any(
        option.get("type") == "null" for option in node_dut.get("anyOf", [node_dut])
    )
    envelope_dut = schemas["LineageNodeRead"]["properties"]["dut"]
    assert envelope_dut["description"]
    assert "full DUT" in envelope_dut["description"]
    assert "CID" in schemas["LineageRead"]["properties"]["dut"]["description"]


def test_removed_session_schemas_are_absent() -> None:
    """Keep obsolete session models and pointers out of generated clients."""
    schemas = _spec()["components"]["schemas"]
    for name in (
        "SessionCreate",
        "AppendCreate",
        "RecordCreate",
        "RecordRead",
        "AgentRecordCreate",
        "NewSessionCreate",
        "SessionRead",
        "SessionSummary",
    ):
        assert name not in schemas
    assert "LineageRootCreate" in schemas
    assert "LineageNodeCreate" in schemas
    assert "LineageGraphRead" in schemas
    assert "LineageGraphSummary" in schemas
    assert "session_uuid" not in schemas["LineageRead"]["properties"]
    assert "root" in schemas["LineageRead"]["properties"]
    assert "current_session_uuid" not in schemas["AgentRead"]["properties"]


def test_openapi_info_version_matches_package() -> None:
    pyproject = tomllib.loads(
        Path(__file__).resolve().parents[2].joinpath("pyproject.toml").read_text(encoding="utf-8")
    )
    assert _spec()["info"]["version"] == pyproject["project"]["version"]
