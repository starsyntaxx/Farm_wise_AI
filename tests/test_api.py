from datetime import date, datetime, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.database import DatabaseError
from app.main import create_app
from app.schemas import (
    AgentStatus,
    CropPlanningResult,
    FarmLocationInput,
    FarmPlot,
    FarmerProfile,
    WaterAccess,
)


NOW = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)


def make_profile(*, farmer_id=None, preferred_name: str = "Amina") -> FarmerProfile:
    return FarmerProfile(
        farmer_id=farmer_id or uuid4(),
        preferred_name=preferred_name,
        preferred_language="ha",
        location=FarmLocationInput(
            state="Kano",
            lga="Dala",
            community="Gwale",
        ),
        plots=[
            FarmPlot(
                plot_id=uuid4(),
                name="North field",
                area_value=1.5,
                area_unit="hectares",
                water_access=WaterAccess.RAINFED,
                current_crop="maize",
                planting_date=date(2026, 6, 1),
                created_at=NOW,
                updated_at=NOW,
            )
        ],
        created_at=NOW,
        updated_at=NOW,
    )


@pytest.fixture
def client(tmp_path):
    app = create_app(tmp_path / "test-farmwise.sqlite3")
    with TestClient(app) as test_client:
        yield test_client


def test_health_endpoint(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "available",
        "service": "FarmWise AI API",
    }


def test_profile_creation_and_retrieval(client: TestClient) -> None:
    profile = make_profile()

    created = client.post("/profiles", json=profile.model_dump(mode="json"))
    retrieved = client.get(f"/profiles/{profile.farmer_id}")

    assert created.status_code == 201
    assert FarmerProfile.model_validate(created.json()) == profile
    assert retrieved.status_code == 200
    assert FarmerProfile.model_validate(retrieved.json()) == profile


def test_missing_profile_returns_not_found(client: TestClient) -> None:
    response = client.get(f"/profiles/{uuid4()}")

    assert response.status_code == 404
    assert response.json()["detail"] == "Farmer profile not found."


def test_duplicate_profile_returns_conflict_without_replacing_saved_data(
    client: TestClient,
) -> None:
    original = make_profile()
    replacement = make_profile(
        farmer_id=original.farmer_id,
        preferred_name="Binta",
    )
    client.post("/profiles", json=original.model_dump(mode="json"))

    duplicate = client.post("/profiles", json=replacement.model_dump(mode="json"))
    retrieved = client.get(f"/profiles/{original.farmer_id}")

    assert duplicate.status_code == 409
    assert FarmerProfile.model_validate(retrieved.json()) == original


def test_invalid_profile_returns_validation_error(client: TestClient) -> None:
    response = client.post(
        "/profiles",
        json={
            "farmer_id": "not-a-uuid",
            "created_at": "2026-10-27T12:00:00",
            "updated_at": "2026-10-27T12:00:00Z",
        },
    )

    assert response.status_code == 422


def test_planning_uses_saved_profile_and_returns_evidence_and_limitations(
    client: TestClient,
) -> None:
    profile = make_profile()
    client.post("/profiles", json=profile.model_dump(mode="json"))

    response = client.post(
        "/planning/assess",
        json={
            "farmer_id": str(profile.farmer_id),
            "planting_period": "June 2026",
            "preferred_crop": "rice",
        },
    )

    assert response.status_code == 200
    result = response.json()
    assert result["status"] == AgentStatus.PARTIAL.value
    assert result["evidence_ids"]
    assert result["limitations"]
    planning_result = CropPlanningResult.model_validate(result["result"])
    assert planning_result.assessments
    assert planning_result.assessments[0].evidence


def test_planning_returns_missing_information_without_inventing_inputs(
    client: TestClient,
) -> None:
    response = client.post(
        "/planning/assess",
        json={"compare_available_crops": True},
    )

    assert response.status_code == 200
    result = response.json()
    assert result["status"] == AgentStatus.NEEDS_INPUT.value
    assert {
        item["field"] for item in result["missing_information"]
    } >= {"location", "planting_period", "water_access"}


def test_planning_with_nonexistent_farmer_id_returns_not_found(
    client: TestClient,
) -> None:
    response = client.post(
        "/planning/assess",
        json={
            "farmer_id": str(uuid4()),
            "planting_period": "June 2026",
            "preferred_crop": "rice",
        },
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Farmer profile not found."


def test_planning_request_cannot_supply_an_unsaved_profile(
    client: TestClient,
) -> None:
    response = client.post(
        "/planning/assess",
        json={
            "profile": make_profile().model_dump(mode="json"),
            "planting_period": "June 2026",
            "preferred_crop": "rice",
        },
    )

    assert response.status_code == 422


def test_bad_plot_reference_returns_validation_error(client: TestClient) -> None:
    profile = make_profile()
    client.post("/profiles", json=profile.model_dump(mode="json"))

    response = client.post(
        "/planning/assess",
        json={
            "farmer_id": str(profile.farmer_id),
            "plot_id": str(uuid4()),
            "planting_period": "June 2026",
            "preferred_crop": "rice",
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "plot_id does not identify a plot in the supplied profile"


def test_database_path_can_be_configured_with_environment_variable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    database_path = tmp_path / "configured.sqlite3"
    monkeypatch.setenv("FARMWISE_DATABASE_PATH", str(database_path))

    with TestClient(create_app()) as configured_client:
        assert configured_client.get("/health").status_code == 200

    assert database_path.exists()


def test_database_failure_returns_safe_internal_server_error(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_retrieval(*args, **kwargs):
        raise DatabaseError("sensitive database implementation detail")

    monkeypatch.setattr("app.main.get_profile", fail_retrieval)

    response = client.get(f"/profiles/{uuid4()}")

    assert response.status_code == 500
    assert response.json() == {
        "detail": "The profile database could not complete the request."
    }
    assert "sensitive" not in response.text
