"""The spawned server enforces service keys over real HTTP."""

from uuid import uuid4

import pytest

aits_client = pytest.importorskip("aits_client")

pytestmark = pytest.mark.integration


def test_client_without_a_service_key_is_rejected(base_url: str) -> None:
    with aits_client.ApiClient(aits_client.Configuration(host=base_url)) as anonymous:
        with pytest.raises(aits_client.ApiException) as exc:
            aits_client.AgentsApi(anonymous).list_all()
    assert exc.value.status == 401


def test_client_with_the_service_key_is_accepted(api_client) -> None:
    assert isinstance(aits_client.AgentsApi(api_client).list_all(), list)


def test_key_name_is_recorded_on_the_commit_log(api_client) -> None:
    body = aits_client.ConfigCreate(system_prompt=f"attributed {uuid4()}")
    config = aits_client.ConfigsApi(api_client).create(body)
    [entry] = aits_client.AuditApi(api_client).commits(cid=config.cid)
    assert entry.principal == "integration"
