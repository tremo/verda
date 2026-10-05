import json

from fastapi.testclient import TestClient
from pydantic import ValidationError
import pytest

from verda.api import create_app
from verda.dashboard import DashboardProjection, store_projection


def test_dashboard_roundtrip_loses_no_fields(tmp_path, engine, projection):
    path = tmp_path / "data.json"
    path.write_text(json.dumps(projection))
    result = store_projection(path, engine)
    token = "test-dashboard-token-with-thirty-two-characters"
    with TestClient(create_app(engine, token)) as client:
        response = client.get(f'/api/dashboard/{result["projection_id"]}', headers={"Authorization": "Bearer " + token})
        assert response.json() == projection
    assert store_projection(path, engine)["already_imported"] is True


def test_duplicate_listing_ids_rejected(projection):
    projection["records"].append(dict(projection["records"][0]))
    with pytest.raises(ValidationError, match="duplicate"):
        DashboardProjection.model_validate(projection)


@pytest.mark.parametrize("version", [1, 3, "2", True])
def test_incompatible_dashboard_schema_rejected(projection, version):
    projection["schemaVersion"] = version
    with pytest.raises(ValidationError):
        DashboardProjection.model_validate(projection)
