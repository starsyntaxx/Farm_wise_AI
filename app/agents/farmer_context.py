"""Deterministic preparation of farmer context for downstream components."""

from datetime import date, datetime
from enum import Enum
from typing import Any
from uuid import UUID

from app.schemas import (
    AgentResponse,
    AgentStatus,
    FarmWiseTask,
    FarmerContextData,
    FarmerContextRequest,
    FarmerContextResult,
    FarmerProfile,
    FieldSource,
    InformationImportance,
    InformationSourceType,
    MissingInformation,
    ProposedFieldChange,
    WaterAccess,
)

_FARMER_CONFIRMED_SOURCES = {
    InformationSourceType.FARMER_FORM,
    InformationSourceType.FARMER_MESSAGE,
    InformationSourceType.FARM_JOURNAL,
}

_LOCATION_FIELDS = ("state", "lga", "community", "latitude", "longitude")
_PLOT_FIELDS = (
    "name",
    "area_value",
    "area_unit",
    "water_access",
    "current_crop",
    "planting_date",
)


def _is_farmer_confirmed(source: FieldSource) -> bool:
    return (
        source.confirmed_by_farmer
        and source.source_type in _FARMER_CONFIRMED_SOURCES
    )


def _has_value(value: Any) -> bool:
    if isinstance(value, Enum):
        value = value.value
    return value is not None and value != WaterAccess.UNKNOWN.value


def _json_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    return value


def _flatten_profile(profile: FarmerProfile | None) -> dict[str, Any]:
    if profile is None:
        return {}

    data = profile.model_dump(mode="json")
    values: dict[str, Any] = {}
    for field in ("preferred_name", "preferred_language"):
        if _has_value(data[field]):
            values[field] = data[field]

    location = data.get("location")
    if location is not None:
        for field in _LOCATION_FIELDS:
            value = location.get(field)
            if _has_value(value):
                values[f"location.{field}"] = value

    for index, plot in enumerate(data["plots"]):
        for field in _PLOT_FIELDS:
            value = plot.get(field)
            if _has_value(value):
                values[f"plots[{index}].{field}"] = value

    return values


def _flatten_new_information(
    request: FarmerContextRequest,
) -> tuple[dict[str, Any], list[tuple[str, Any]]]:
    information = request.new_information
    if information is None:
        return {}, []

    values: dict[str, Any] = {}
    new_plot_entries: list[tuple[str, dict[str, Any]]] = []
    for field in ("preferred_name", "preferred_language"):
        value = getattr(information, field)
        if field in information.model_fields_set and _has_value(value):
            values[field] = _json_value(value)

    if "location" in information.model_fields_set and information.location is not None:
        location = information.location
        for field in _LOCATION_FIELDS:
            value = getattr(location, field)
            if field in location.model_fields_set and _has_value(value):
                values[f"location.{field}"] = _json_value(value)

    if "plots" in information.model_fields_set:
        for index, plot in enumerate(information.plots):
            supplied_values: dict[str, Any] = {}
            for field in _PLOT_FIELDS:
                value = getattr(plot, field)
                if field in plot.model_fields_set and _has_value(value):
                    supplied_values[field] = value
                    if (
                        request.profile is not None
                        and index < len(request.profile.plots)
                    ):
                        values[f"plots[{index}].{field}"] = _json_value(value)
            if supplied_values:
                new_plot_entries.append((f"plots[{index}]", supplied_values))

    return values, new_plot_entries


def _assign_path(profile_data: dict[str, Any], field_path: str, value: Any) -> bool:
    """Apply one confirmed value to an in-memory copy, never to the request profile."""
    if field_path.startswith("location."):
        field = field_path.removeprefix("location.")
        if profile_data.get("location") is None:
            profile_data["location"] = {
                name: None for name in _LOCATION_FIELDS
            }
        profile_data["location"][field] = value
        return True

    if field_path.startswith("plots["):
        index_end = field_path.index("]")
        index = int(field_path[len("plots[") : index_end])
        plot_field = field_path[index_end + 2 :]
        if index >= len(profile_data["plots"]):
            return False
        profile_data["plots"][index][plot_field] = value
        return True

    profile_data[field_path] = value
    return True


def _make_proposal(
    field_path: str,
    previous_value: Any,
    proposed_value: Any,
    request: FarmerContextRequest,
    confirmed: bool,
) -> ProposedFieldChange:
    conflicts = previous_value is not None and previous_value != proposed_value
    if conflicts and not confirmed:
        reason = "The new value conflicts with the saved value and needs clarification."
    elif not confirmed:
        reason = "The source does not establish that the farmer confirmed this value."
    elif conflicts:
        reason = "The farmer explicitly supplied a replacement value."
    else:
        reason = "The farmer explicitly supplied this new value."

    return ProposedFieldChange(
        field_path=field_path,
        previous_value=previous_value,
        proposed_value=proposed_value,
        source=request.source,
        requires_confirmation=not confirmed,
        reason=reason,
    )


def _missing(
    field: str,
    importance: InformationImportance,
    reason: str,
    question: str | None,
) -> MissingInformation:
    return MissingInformation(
        field=field,
        importance=importance,
        reason=reason,
        question=question,
    )


def _task_missing_information(
    task: FarmWiseTask,
    profile: FarmerProfile | None,
    values: dict[str, Any],
    proposal_fields_needing_confirmation: set[str],
    has_new_information: bool,
) -> tuple[list[MissingInformation], list[str]]:
    missing: list[MissingInformation] = []
    limitations: list[str] = []
    for field_path in sorted(proposal_fields_needing_confirmation):
        task_relevant = (
            task is FarmWiseTask.PROFILE_UPDATE
            or (
                task is FarmWiseTask.CROP_PLANNING
                and (field_path.startswith("location.") or field_path.startswith("plots"))
            )
            or (
                task is FarmWiseTask.CROP_MONITORING
                and field_path.startswith("plots")
            )
        )
        missing.append(
            _missing(
                field_path,
                (
                    InformationImportance.REQUIRED
                    if task_relevant
                    else InformationImportance.USEFUL
                ),
                "A proposed profile value needs farmer confirmation before use.",
                f"Please confirm the proposed value for {field_path.replace('.', ' ')}.",
            )
        )

    if task is FarmWiseTask.CROP_PLANNING:
        if "location.state" not in values:
            missing.append(
                _missing(
                    "location.state",
                    InformationImportance.REQUIRED,
                    "A state-level location is needed to prepare a location-aware plan.",
                    "Which state is your farm in?",
                )
            )
        elif not any(
            path in values for path in ("location.lga", "location.community")
        ):
            missing.append(
                _missing(
                    "location.lga",
                    InformationImportance.USEFUL,
                    "A local area can make planning context more specific.",
                    "Which LGA or community is your farm in?",
                )
            )

        missing.append(
            _missing(
                "crop_interest",
                InformationImportance.REQUIRED,
                "The request schema cannot represent a crop of interest or permission to compare crops.",
                "Which crop are you considering, or may the system compare supported crops?",
            )
        )
        limitations.append(
            "FarmerContextRequest has no crop-interest or crop-comparison-permission field."
        )

        if not any(path.endswith(".planting_date") for path in values):
            missing.append(
                _missing(
                    "planting_period",
                    InformationImportance.REQUIRED,
                    "The request has no planting-period field; a plot planting date is the only related available field.",
                    "When do you plan to plant?",
                )
            )
            limitations.append(
                "Planting period is not directly represented; only a plot planting_date is available."
            )

        plot_indices = {
            path.split("]", maxsplit=1)[0] + "]"
            for path in values
            if path.startswith("plots[")
        }
        plot_count = len(profile.plots) if profile is not None else 0
        if plot_indices:
            plot_count = max(
                plot_count,
                max(int(path[6:-1]) for path in plot_indices) + 1,
            )
        water_missing = not plot_count or any(
            f"plots[{index}].water_access" not in values
            for index in range(plot_count)
        )
        if water_missing:
            missing.append(
                _missing(
                    "plots.water_access",
                    InformationImportance.USEFUL,
                    "Water access can materially affect crop comparisons.",
                    "Is the relevant plot rainfed, irrigated, or both?",
                )
            )
        area_missing = not plot_count or any(
            f"plots[{index}].area_value" not in values
            for index in range(plot_count)
        )
        if area_missing:
            missing.append(
                _missing(
                    "plots.area_value",
                    InformationImportance.USEFUL,
                    "Plot area may be needed for area-specific planning.",
                    "What is the area of the relevant plot, and in which unit?",
                )
            )

    elif task is FarmWiseTask.CROP_MONITORING:
        plots = profile.plots if profile is not None else []
        if not plots:
            missing.append(
                _missing(
                    "plot_reference",
                    InformationImportance.REQUIRED,
                    "No saved plot is available to associate with a monitoring request.",
                    "Which saved plot or plan is this about?",
                )
            )
        elif len(plots) > 1:
            missing.append(
                _missing(
                    "plot_reference",
                    InformationImportance.REQUIRED,
                    "Several saved plots exist and the request does not identify one.",
                    "Which plot is this observation about?",
                )
            )

        if plots and not any(plot.current_crop for plot in plots):
            missing.append(
                _missing(
                    "current_crop",
                    InformationImportance.REQUIRED,
                    "No current crop is recorded for the available plot.",
                    "What crop is currently growing on the plot?",
                )
            )

        missing.append(
            _missing(
                "observation",
                InformationImportance.REQUIRED,
                "The request schema cannot carry the farmer's new observation or question.",
                "What have you observed, or what question would you like help with?",
            )
        )
        limitations.append(
            "FarmerContextRequest has no saved-plan reference, growth-stage, or observation field."
        )

    elif task is FarmWiseTask.PROFILE_UPDATE and not has_new_information:
        missing.append(
            _missing(
                "new_information",
                InformationImportance.REQUIRED,
                "No non-empty profile information was supplied to prepare an update.",
                "What profile detail would you like to add or change?",
            )
        )

    return missing, limitations


def _has_supplied_information(
    request: FarmerContextRequest,
    values: dict[str, Any],
    new_plot_entries: list[tuple[str, dict[str, Any]]],
) -> bool:
    if values or new_plot_entries:
        return True
    information = request.new_information
    if information is None:
        return False
    if any(
        field in information.model_fields_set and getattr(information, field) is not None
        for field in ("preferred_name", "preferred_language")
    ):
        return True
    if (
        "location" in information.model_fields_set
        and information.location is not None
        and any(
            field in information.location.model_fields_set
            and getattr(information.location, field) is not None
            for field in _LOCATION_FIELDS
        )
    ):
        return True
    return False


def _source_map(
    profile: FarmerProfile | None,
    request: FarmerContextRequest,
    known_values: dict[str, Any],
    accepted_paths: set[str],
) -> dict[str, FieldSource]:
    sources: dict[str, FieldSource] = {}
    if profile is not None:
        saved_source = FieldSource(
            source_type=InformationSourceType.SAVED_PROFILE,
            record_id=str(profile.farmer_id),
            confirmed_by_farmer=False,
        )
        sources.update({path: saved_source for path in _flatten_profile(profile)})

    for path in accepted_paths:
        if path in known_values:
            sources[path] = request.source
    return sources


def process_farmer_context(request: FarmerContextRequest) -> AgentResponse:
    """Prepare a task-specific context without persisting or mutating profile data."""
    original_values = _flatten_profile(request.profile)
    incoming_values, new_plot_entries = _flatten_new_information(request)
    confirmed = _is_farmer_confirmed(request.source)
    if (
        request.profile is not None
        and request.new_information is not None
        and "plots" in request.new_information.model_fields_set
        and request.new_information.plots
    ):
        plot_mapping_limitation = (
            "The request schema has no plot identifier for new information; plot "
            "updates are matched to saved plots by list position."
        )
    else:
        plot_mapping_limitation = None
    profile_data = (
        request.profile.model_dump(mode="json") if request.profile is not None else None
    )

    proposed_changes: list[ProposedFieldChange] = []
    paths_needing_confirmation: set[str] = set()
    accepted_paths: set[str] = set()
    effective_values = dict(original_values)
    has_new_information = _has_supplied_information(
        request, incoming_values, new_plot_entries
    )

    for field_path, value in incoming_values.items():
        previous_value = original_values.get(field_path)
        if previous_value == value:
            effective_values[field_path] = value
            accepted_paths.add(field_path)
            continue

        proposal = _make_proposal(
            field_path,
            previous_value,
            value,
            request,
            confirmed,
        )
        proposed_changes.append(proposal)
        if confirmed:
            effective_values[field_path] = value
            accepted_paths.add(field_path)
            if profile_data is not None:
                _assign_path(profile_data, field_path, value)
        else:
            paths_needing_confirmation.add(field_path)

    existing_plot_count = len(request.profile.plots) if request.profile else 0
    for field_path, plot_values in new_plot_entries:
        plot_index = int(field_path[len("plots[") : field_path.index("]")])
        if plot_index < existing_plot_count:
            continue
        proposed_changes.append(
            _make_proposal(
                field_path,
                None,
                _json_value(plot_values),
                request,
                confirmed,
            )
        )
        if confirmed:
            accepted_paths.add(field_path)
            for plot_field, value in plot_values.items():
                value = _json_value(value)
                effective_values[f"{field_path}.{plot_field}"] = value
                accepted_paths.add(f"{field_path}.{plot_field}")
        else:
            paths_needing_confirmation.add(field_path)

    contextual_profile = (
        FarmerProfile.model_validate(profile_data)
        if profile_data is not None
        else None
    )
    missing_information, limitations = _task_missing_information(
        request.task,
        contextual_profile,
        effective_values,
        paths_needing_confirmation,
        has_new_information,
    )
    if plot_mapping_limitation is not None:
        limitations.append(plot_mapping_limitation)

    required_missing = any(
        item.importance is InformationImportance.REQUIRED
        for item in missing_information
    )
    useful_missing = any(
        item.importance is InformationImportance.USEFUL
        for item in missing_information
    )
    status = (
        AgentStatus.NEEDS_INPUT
        if required_missing
        else AgentStatus.PARTIAL
        if useful_missing
        else AgentStatus.SUCCESS
    )
    context = FarmerContextData(
        profile=contextual_profile,
        task=request.task,
        available_fields=sorted(effective_values),
    )
    result = FarmerContextResult(
        task=request.task,
        context=context,
        field_sources=_source_map(
            request.profile,
            request,
            effective_values,
            accepted_paths,
        ),
        proposed_changes=proposed_changes,
        confirmation_required=any(
            change.requires_confirmation for change in proposed_changes
        ),
    )
    if proposed_changes:
        limitations.append(
            "Proposed profile changes and the contextual view are not persisted."
        )

    return AgentResponse(
        agent="farmer_context",
        status=status,
        result=result.model_dump(mode="json"),
        evidence_ids=[request.source.record_id] if request.source.record_id else [],
        missing_information=missing_information,
        limitations=limitations,
        requires_human_input=status is AgentStatus.NEEDS_INPUT,
    )
