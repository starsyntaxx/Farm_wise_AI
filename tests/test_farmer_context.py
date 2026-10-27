from copy import deepcopy
from datetime import date, datetime, timezone
from uuid import uuid4

from app.agents.farmer_context import process_farmer_context
from app.schemas import (
    AgentStatus,
    AreaUnit,
    FarmLocationInput,
    FarmPlot,
    FarmPlotInput,
    FarmerContextRequest,
    FarmerProfile,
    FarmerProfileInput,
    FarmWiseTask,
    FieldSource,
    InformationImportance,
    InformationSourceType,
    WaterAccess,
)

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)


def make_profile(
    *,
    state: str | None = "Kano",
    lga: str | None = "Dala",
    plots: list[FarmPlot] | None = None,
) -> FarmerProfile:
    return FarmerProfile(
        farmer_id=uuid4(),
        preferred_name="Amina",
        preferred_language="ha",
        location=FarmLocationInput(state=state, lga=lga, community="Gwale"),
        plots=plots or [],
        created_at=NOW,
        updated_at=NOW,
    )


def make_plot(
    *,
    current_crop: str | None = "maize",
    water_access: WaterAccess = WaterAccess.IRRIGATED,
    area_value: float | None = 2.0,
    area_unit: AreaUnit | None = AreaUnit.HECTARES,
    planting_date: date | None = date(2026, 6, 1),
) -> FarmPlot:
    return FarmPlot(
        plot_id=uuid4(),
        name="North field",
        area_value=area_value,
        area_unit=area_unit,
        water_access=water_access,
        current_crop=current_crop,
        planting_date=planting_date,
        created_at=NOW,
        updated_at=NOW,
    )


def make_source(
    *,
    source_type: InformationSourceType = InformationSourceType.FARMER_FORM,
    confirmed: bool = True,
) -> FieldSource:
    return FieldSource(
        source_type=source_type,
        record_id="message-123",
        confirmed_by_farmer=confirmed,
    )


def make_request(
    task: FarmWiseTask,
    *,
    profile: FarmerProfile | None = None,
    new_information: FarmerProfileInput | None = None,
    source: FieldSource | None = None,
) -> FarmerContextRequest:
    return FarmerContextRequest(
        task=task,
        profile=profile,
        new_information=new_information,
        source=source or make_source(),
    )


def test_sufficient_profile_update_returns_success_and_proposal() -> None:
    profile = make_profile()
    request = make_request(
        FarmWiseTask.PROFILE_UPDATE,
        profile=profile,
        new_information=FarmerProfileInput(preferred_language="yo"),
    )

    response = process_farmer_context(request)

    assert response.status is AgentStatus.SUCCESS
    assert response.agent == "farmer_context"
    assert response.missing_information == []
    assert response.requires_human_input is False
    assert response.evidence_ids == ["message-123"]
    assert response.result is not None
    assert response.result["context"]["profile"]["preferred_language"] == "yo"
    assert response.result["proposed_changes"][0]["field_path"] == "preferred_language"
    assert response.result["proposed_changes"][0]["requires_confirmation"] is False


def test_incomplete_crop_planning_identifies_missing_required_and_useful_fields() -> None:
    response = process_farmer_context(
        make_request(
            FarmWiseTask.CROP_PLANNING,
            profile=make_profile(state=None, lga=None, plots=[make_plot(
                water_access=WaterAccess.UNKNOWN,
                area_value=None,
                area_unit=None,
                planting_date=None,
            )]),
        )
    )

    missing = {item.field: item for item in response.missing_information}
    assert response.status is AgentStatus.NEEDS_INPUT
    assert missing["location.state"].importance is InformationImportance.REQUIRED
    assert missing["crop_interest"].importance is InformationImportance.REQUIRED
    assert missing["planting_period"].importance is InformationImportance.REQUIRED
    assert missing["plots.water_access"].importance is InformationImportance.USEFUL
    assert missing["plots.area_value"].importance is InformationImportance.USEFUL


def test_missing_location_is_requested_for_crop_planning() -> None:
    response = process_farmer_context(
        make_request(
            FarmWiseTask.CROP_PLANNING,
            profile=make_profile(state=None, lga=None, plots=[make_plot()]),
        )
    )

    assert any(item.field == "location.state" for item in response.missing_information)
    assert response.status is AgentStatus.NEEDS_INPUT


def test_schema_limitations_for_crop_planning_are_explicit() -> None:
    response = process_farmer_context(
        make_request(
            FarmWiseTask.CROP_PLANNING,
            profile=make_profile(plots=[make_plot(planting_date=None)]),
        )
    )

    assert any("crop-interest" in limitation for limitation in response.limitations)
    assert any(
        "planting period" in limitation.lower()
        for limitation in response.limitations
    )
    assert {"crop_interest", "planting_period"} <= {
        item.field for item in response.missing_information
    }


def test_monitoring_reports_unrepresentable_observation_and_plan_inputs() -> None:
    response = process_farmer_context(
        make_request(
            FarmWiseTask.CROP_MONITORING,
            profile=make_profile(plots=[make_plot()]),
        )
    )

    assert response.status is AgentStatus.NEEDS_INPUT
    assert any(item.field == "observation" for item in response.missing_information)
    assert any("saved-plan" in item for item in response.limitations)


def test_profile_update_without_new_information_needs_input() -> None:
    profile = make_profile()
    response = process_farmer_context(
        make_request(FarmWiseTask.PROFILE_UPDATE, profile=profile)
    )

    assert response.status is AgentStatus.NEEDS_INPUT
    assert response.missing_information[0].field == "new_information"
    assert response.result["proposed_changes"] == []


def test_unconfirmed_conflicting_update_is_proposed_but_not_applied() -> None:
    profile = make_profile()
    response = process_farmer_context(
        make_request(
            FarmWiseTask.PROFILE_UPDATE,
            profile=profile,
            new_information=FarmerProfileInput(preferred_name="Fatima"),
            source=make_source(confirmed=False),
        )
    )

    assert response.status is AgentStatus.NEEDS_INPUT
    assert response.requires_human_input is True
    assert response.result["context"]["profile"]["preferred_name"] == "Amina"
    change = response.result["proposed_changes"][0]
    assert change["previous_value"] == "Amina"
    assert change["proposed_value"] == "Fatima"
    assert change["requires_confirmation"] is True
    assert change["source"]["confirmed_by_farmer"] is False
    assert any(item.field == "preferred_name" for item in response.missing_information)


def test_omitted_fields_preserve_existing_profile_values() -> None:
    profile = make_profile()
    response = process_farmer_context(
        make_request(
            FarmWiseTask.PROFILE_UPDATE,
            profile=profile,
            new_information=FarmerProfileInput(preferred_name="Amina"),
        )
    )

    updated_context = response.result["context"]["profile"]
    assert updated_context["preferred_name"] == "Amina"
    assert updated_context["preferred_language"] == "ha"
    assert updated_context["location"]["state"] == "Kano"
    assert updated_context["location"]["lga"] == "Dala"
    assert updated_context["plots"] == []
    assert response.result["proposed_changes"] == []


def test_nested_location_update_preserves_other_location_fields() -> None:
    profile = make_profile()
    response = process_farmer_context(
        make_request(
            FarmWiseTask.PROFILE_UPDATE,
            profile=profile,
            new_information=FarmerProfileInput(
                location=FarmLocationInput(lga="Tarauni")
            ),
        )
    )

    location = response.result["context"]["profile"]["location"]
    assert location == {
        "state": "Kano",
        "lga": "Tarauni",
        "community": "Gwale",
        "latitude": None,
        "longitude": None,
    }
    assert response.result["proposed_changes"][0]["field_path"] == "location.lga"


def test_plot_updates_use_positional_index_and_preserve_other_plots() -> None:
    first_plot = make_plot(current_crop="maize")
    second_plot = make_plot(
        current_crop="sorghum",
        water_access=WaterAccess.RAINFED,
        area_value=1.0,
    )
    profile = make_profile(plots=[first_plot, second_plot])
    response = process_farmer_context(
        make_request(
            FarmWiseTask.PROFILE_UPDATE,
            profile=profile,
            new_information=FarmerProfileInput(
                plots=[
                    FarmPlotInput(),
                    FarmPlotInput(water_access=WaterAccess.BOTH),
                ]
            ),
        )
    )

    plots = response.result["context"]["profile"]["plots"]
    assert plots[0]["water_access"] == WaterAccess.IRRIGATED.value
    assert plots[1]["water_access"] == WaterAccess.BOTH.value
    assert plots[1]["area_value"] == 1.0
    assert plots[1]["plot_id"] == str(second_plot.plot_id)
    assert response.result["proposed_changes"][0]["field_path"] == (
        "plots[1].water_access"
    )
    assert any("list position" in item for item in response.limitations)


def test_unconfirmed_nested_change_preserves_saved_location_in_context() -> None:
    profile = make_profile()
    response = process_farmer_context(
        make_request(
            FarmWiseTask.PROFILE_UPDATE,
            profile=profile,
            new_information=FarmerProfileInput(
                location=FarmLocationInput(state="Kaduna")
            ),
            source=make_source(confirmed=False),
        )
    )

    location = response.result["context"]["profile"]["location"]
    assert location["state"] == "Kano"
    assert location["lga"] == "Dala"
    assert response.result["proposed_changes"][0]["field_path"] == "location.state"


def test_input_profile_and_nested_records_are_not_mutated() -> None:
    profile = make_profile(plots=[make_plot()])
    before = deepcopy(profile.model_dump(mode="json"))
    request = make_request(
        FarmWiseTask.PROFILE_UPDATE,
        profile=profile,
        new_information=FarmerProfileInput(
            location=FarmLocationInput(lga="Tarauni"),
            plots=[FarmPlotInput(current_crop="rice")],
        ),
    )

    process_farmer_context(request)

    assert profile.model_dump(mode="json") == before


def test_no_saved_profile_does_not_invent_identifiers_or_timestamps() -> None:
    response = process_farmer_context(
        make_request(
            FarmWiseTask.PROFILE_UPDATE,
            new_information=FarmerProfileInput(preferred_name="Amina"),
        )
    )

    assert response.result["context"]["profile"] is None
    assert response.result["proposed_changes"][0]["previous_value"] is None
    assert response.result["proposed_changes"][0]["proposed_value"] == "Amina"


def test_new_plot_is_a_proposal_without_invented_persisted_metadata() -> None:
    response = process_farmer_context(
        make_request(
            FarmWiseTask.PROFILE_UPDATE,
            new_information=FarmerProfileInput(
                plots=[
                    FarmPlotInput(
                        area_value=1.5,
                        area_unit=AreaUnit.HECTARES,
                        water_access=WaterAccess.RAINFED,
                        planting_date=date(2027, 5, 1),
                    )
                ]
            ),
        )
    )

    assert response.status is AgentStatus.SUCCESS
    change = response.result["proposed_changes"][0]
    assert change["field_path"] == "plots[0]"
    assert change["proposed_value"] == {
        "area_value": 1.5,
        "area_unit": "hectares",
        "water_access": "rainfed",
        "planting_date": "2027-05-01",
    }
    assert response.result["context"]["profile"] is None


def test_empty_and_null_updates_do_not_delete_existing_values() -> None:
    profile = make_profile()
    response = process_farmer_context(
        make_request(
            FarmWiseTask.PROFILE_UPDATE,
            profile=profile,
            new_information=FarmerProfileInput(
                preferred_name=None,
                location=FarmLocationInput(state=None),
            ),
        )
    )

    assert response.status is AgentStatus.NEEDS_INPUT
    assert response.result["context"]["profile"]["preferred_name"] == "Amina"
    assert response.result["context"]["profile"]["location"]["state"] == "Kano"
    assert response.result["proposed_changes"] == []


def test_agent_uses_only_local_deterministic_data() -> None:
    response = process_farmer_context(
        make_request(
            FarmWiseTask.PROFILE_UPDATE,
            new_information=FarmerProfileInput(preferred_name="Amina"),
        )
    )

    assert response.agent == "farmer_context"
    assert response.result["context"]["profile"] is None
