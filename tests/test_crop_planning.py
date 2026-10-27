from copy import deepcopy
from datetime import datetime, timezone
from uuid import UUID, uuid4

from app.agents.crop_planning import process_crop_planning
from app.schemas import (
    AgentResponse,
    AgentStatus,
    CropPlanningRequest,
    CropPlanningResult,
    CropSuitabilityStatus,
    FarmLocationInput,
    FarmPlot,
    FarmerProfile,
    WaterAccess,
)
from app.services.crop_rules import (
    assess_water_access,
    find_crop_candidates,
    get_crop_catalogue,
)


def _profile(*water_access: WaterAccess) -> FarmerProfile:
    now = datetime.now(timezone.utc)
    return FarmerProfile(
        farmer_id=uuid4(),
        preferred_name="Ada",
        location=FarmLocationInput(state="Kwara", lga="Ilorin West"),
        plots=[
            FarmPlot(
                plot_id=UUID(int=index + 1),
                name=f"Plot {index + 1}",
                area_value=1.5 + index,
                area_unit="hectares",
                water_access=water,
                created_at=now,
                updated_at=now,
            )
            for index, water in enumerate(water_access)
        ],
        created_at=now,
        updated_at=now,
    )


def _complete_request(**overrides: object) -> CropPlanningRequest:
    values: dict[str, object] = {
        "location": FarmLocationInput(state="Kwara", lga="Ilorin West"),
        "planting_period": "June 2026",
        "water_access": WaterAccess.RAINFED,
        "preferred_crop": "rice",
    }
    values.update(overrides)
    return CropPlanningRequest(**values)


def _result(response: AgentResponse) -> CropPlanningResult:
    assert response.result is not None
    return CropPlanningResult.model_validate(response.result)


def test_catalogue_contains_valid_evidence_scoped_entries() -> None:
    catalogue = get_crop_catalogue()

    assert {entry.crop_id for entry in catalogue} == {
        "rice_nerica_upland",
        "rice_nerica_irrigated",
    }
    assert all(entry.evidence_status.value == "incomplete" for entry in catalogue)
    assert all(entry.evidence for entry in catalogue)
    assert all(
        evidence.source_url.scheme == "https"
        for entry in catalogue
        for evidence in entry.evidence
    )
    assert all(entry.limitations for entry in catalogue)


def test_insufficient_request_returns_needs_input_without_recommendation() -> None:
    response = process_crop_planning(CropPlanningRequest())

    assert response.status is AgentStatus.NEEDS_INPUT
    assert {item.field for item in response.missing_information} >= {
        "location",
        "planting_period",
        "crop_choice",
        "water_access",
    }
    assert _result(response).assessments == []
    assert response.requires_human_input is True


def test_missing_location_is_reported_even_when_other_inputs_are_present() -> None:
    response = process_crop_planning(
        _complete_request(location=None)
    )

    assert response.status is AgentStatus.NEEDS_INPUT
    location = next(item for item in response.missing_information if item.field == "location")
    assert location.importance.value == "required"


def test_documented_water_match_and_mismatch_are_distinguished() -> None:
    response = process_crop_planning(_complete_request())
    assessments = {item.crop_id: item for item in _result(response).assessments}

    assert assessments["rice_nerica_upland"].suitability is CropSuitabilityStatus.CONDITIONALLY_SUITABLE
    assert assessments["rice_nerica_upland"].supporting_factors
    assert assessments["rice_nerica_irrigated"].suitability is CropSuitabilityStatus.NOT_RECOMMENDED
    assert assessments["rice_nerica_irrigated"].evidence


def test_unknown_water_access_is_not_treated_as_a_positive_match() -> None:
    response = process_crop_planning(
        _complete_request(water_access=WaterAccess.UNKNOWN)
    )
    assessments = _result(response).assessments

    assert all(
        item.suitability is CropSuitabilityStatus.INSUFFICIENT_INFORMATION
        for item in assessments
    )
    assert any(item.field == "water_access" for item in response.missing_information)


def test_unsupported_crop_is_not_given_an_evidence_based_recommendation() -> None:
    response = process_crop_planning(_complete_request(preferred_crop="cassava"))
    assessment = _result(response).assessments[0]

    assert assessment.crop_name == "cassava"
    assert assessment.suitability is CropSuitabilityStatus.INSUFFICIENT_INFORMATION
    assert assessment.evidence == []
    assert "cassava" in response.limitations[-1]


def test_multiple_plots_require_selection_or_use_the_selected_plot_only() -> None:
    profile = _profile(WaterAccess.RAINFED, WaterAccess.IRRIGATED)
    ambiguous = process_crop_planning(
        CropPlanningRequest(
            profile=profile,
            planting_period="June 2026",
            preferred_crop="rice",
        )
    )
    assert any(item.field == "plot_id" for item in ambiguous.missing_information)
    assert all(
        item.suitability is CropSuitabilityStatus.INSUFFICIENT_INFORMATION
        for item in _result(ambiguous).assessments
    )

    selected = process_crop_planning(
        CropPlanningRequest(
            profile=profile,
            plot_id=profile.plots[1].plot_id,
            planting_period="June 2026",
            preferred_crop="rice",
        )
    )
    selected_assessments = {
        item.crop_id: item.suitability for item in _result(selected).assessments
    }
    assert selected_assessments["rice_nerica_irrigated"] is CropSuitabilityStatus.CONDITIONALLY_SUITABLE
    assert selected_assessments["rice_nerica_upland"] is CropSuitabilityStatus.INSUFFICIENT_INFORMATION


def test_request_profile_is_not_mutated_and_saved_location_is_reused() -> None:
    profile = _profile(WaterAccess.RAINFED)
    original = deepcopy(profile.model_dump(mode="json"))
    response = process_crop_planning(
        CropPlanningRequest(
            profile=profile,
            planting_period="June 2026",
            preferred_crop="rice",
        )
    )

    assert profile.model_dump(mode="json") == original
    assert all(item.field != "location" for item in response.missing_information)


def test_nested_location_override_preserves_saved_location_fields() -> None:
    profile = _profile(WaterAccess.RAINFED)
    response = process_crop_planning(
        CropPlanningRequest(
            profile=profile,
            location=FarmLocationInput(community="Oke-Oyi"),
            planting_period="June 2026",
            preferred_crop="rice",
        )
    )

    assert all(item.field != "location" for item in response.missing_information)


def test_inputs_are_deterministic_and_limits_are_explained() -> None:
    request = _complete_request(
        area_value=2.0,
        area_unit="hectares",
        soil_type="loamy",
    )
    first = process_crop_planning(request)
    second = process_crop_planning(request)

    assert first.model_dump(mode="json") == second.model_dump(mode="json")
    assert "current or location-specific climate" in " ".join(first.limitations)
    assert "not evaluated" in " ".join(first.limitations)
    assert all(
        not any(token in str(assessment.model_dump()).lower() for token in ("yield probability", "success probability"))
        for assessment in _result(first).assessments
    )
    assert first.evidence_ids
    assert _result(first).ordering_reason.startswith("Assessments are grouped")


def test_catalogue_rule_reports_no_match_for_unknown_water() -> None:
    entry = find_crop_candidates("rice", False)[0]

    assert (
        assess_water_access(entry, WaterAccess.UNKNOWN)
        is CropSuitabilityStatus.INSUFFICIENT_INFORMATION
    )
