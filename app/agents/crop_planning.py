"""Assemble evidence-scoped crop assessments without external services."""

import re

from app.schemas import (
    AgentResponse,
    AgentStatus,
    CropAssessment,
    CropEvidenceStatus,
    CropPlanningRequest,
    CropPlanningResult,
    CropSuitabilityStatus,
    FarmLocationInput,
    FarmPlot,
    InformationImportance,
    MissingInformation,
    WaterAccess,
)
from app.services.crop_rules import (
    CropCatalogueEntry,
    assess_water_access,
    find_crop_candidates,
)


_AGENT_NAME = "crop_planning"
_BASE_LIMITATIONS = [
    "No current or location-specific climate information has been retrieved.",
    "The catalogue contains only evidence-scoped NERICA rice cultivation systems; it is not a general Nigerian crop guide.",
    "Planting dates, soil suitability, yield, and probability of success are not estimated.",
    "Plot area and reported soil type are accepted but not evaluated because no validated rules for them are available.",
]


def _effective_location(request: CropPlanningRequest) -> FarmLocationInput | None:
    saved = request.profile.location if request.profile is not None else None
    if saved is None and request.location is None:
        return None
    values = saved.model_dump() if saved is not None else {}
    if request.location is not None:
        values.update(request.location.model_dump(exclude_unset=True))
    return FarmLocationInput.model_validate(values)


def _selected_plot(request: CropPlanningRequest) -> FarmPlot | None:
    plots = request.profile.plots if request.profile is not None else []
    if request.plot_id is not None:
        if request.profile is None:
            raise ValueError("plot_id requires a profile containing that plot")
        for plot in plots:
            if plot.plot_id == request.plot_id:
                return plot
        raise ValueError("plot_id does not identify a plot in the supplied profile")
    if len(plots) == 1:
        return plots[0]
    return None


def _has_specific_location(location: FarmLocationInput | None) -> bool:
    if location is None:
        return False
    if location.latitude is not None and location.longitude is not None:
        return True
    return bool(location.state and (location.lga or location.community))


def _missing_information(
    request: CropPlanningRequest,
    location: FarmLocationInput | None,
    water_access: WaterAccess | None,
) -> list[MissingInformation]:
    missing: list[MissingInformation] = []
    if not _has_specific_location(location):
        missing.append(
            MissingInformation(
                field="location",
                importance=InformationImportance.REQUIRED,
                reason="A state with an LGA/community or a coordinate pair is needed for location-aware planning.",
                question="Which state and LGA/community is the farm in?",
            )
        )
    if request.planting_period is None:
        missing.append(
            MissingInformation(
                field="planting_period",
                importance=InformationImportance.REQUIRED,
                reason="The intended planting period is not provided, and this catalogue has no verified local planting calendar.",
                question="When are you hoping to plant?",
            )
        )
    if request.preferred_crop is None and not request.compare_available_crops:
        missing.append(
            MissingInformation(
                field="crop_choice",
                importance=InformationImportance.REQUIRED,
                reason="Choose a crop or permit comparison of the currently supported catalogue entries.",
                question="Which crop are you considering, or may I compare the crops currently in the catalogue?",
            )
        )
    if (
        request.profile is not None
        and len(request.profile.plots) > 1
        and request.plot_id is None
        and request.water_access is None
    ):
        missing.append(
            MissingInformation(
                field="plot_id",
                importance=InformationImportance.REQUIRED,
                reason="The profile contains multiple plots, so plot-specific conditions cannot be selected safely.",
                question="Which plot should this assessment use?",
            )
        )
    if water_access in (None, WaterAccess.UNKNOWN):
        missing.append(
            MissingInformation(
                field="water_access",
                importance=InformationImportance.USEFUL,
                reason="Water access distinguishes the NERICA cultivation systems in this catalogue.",
                question="Is this plot rainfed, irrigated, or both?",
            )
        )
    return missing


def _assessment(
    entry: CropCatalogueEntry,
    water_access: WaterAccess | None,
    missing: list[MissingInformation],
) -> CropAssessment:
    suitability = assess_water_access(entry, water_access)
    supporting_factors: list[str] = []
    if suitability is CropSuitabilityStatus.CONDITIONALLY_SUITABLE:
        supporting_factors.append(
            "The reported water access aligns with the NERICA cultivation system described by AfricaRice; this factor alone does not establish local suitability."
        )

    risks = [
        "FAO notes that rice generally needs more water than beans and wheat; no plot-level water quantity or local estimate is available.",
        *entry.limitations,
    ]
    if suitability is CropSuitabilityStatus.NOT_RECOMMENDED:
        explanation = (
            "This catalogue entry describes an irrigated NERICA system, but the plot is reported as rainfed. "
            "Do not treat this system as a fit without verified irrigation access."
        )
    elif suitability is CropSuitabilityStatus.CONDITIONALLY_SUITABLE:
        explanation = (
            "The reported water access matches the cultivation system in the cited NERICA evidence. "
            "This is only a conditional match; local climate, planting period, cultivar, and soil suitability are not established."
        )
    else:
        explanation = (
            "Available information does not establish a match for this NERICA cultivation system. "
            "Unknown conditions were not treated as positive evidence."
        )

    return CropAssessment(
        crop_id=entry.crop_id,
        crop_name=entry.crop_name,
        cultivation_system=entry.cultivation_system,
        evidence_status=entry.evidence_status,
        suitability=suitability,
        supporting_factors=supporting_factors,
        risks=risks,
        missing_information=missing,
        evidence=entry.evidence,
        explanation=explanation,
    )


def _unsupported_crop_assessment(crop_name: str) -> CropAssessment:
    safe_id = re.sub(r"[^a-z0-9]+", "_", crop_name.casefold()).strip("_") or "unknown"
    return CropAssessment(
        crop_id=f"unsupported_{safe_id}",
        crop_name=crop_name,
        cultivation_system="Not represented in the evidence-backed catalogue",
        evidence_status=CropEvidenceStatus.INCOMPLETE,
        suitability=CropSuitabilityStatus.INSUFFICIENT_INFORMATION,
        risks=["No verified catalogue evidence is available for this requested crop."],
        explanation=(
            "This crop is not represented in the current evidence-backed catalogue, "
            "so no suitability conclusion has been made."
        ),
    )


def process_crop_planning(request: CropPlanningRequest) -> AgentResponse:
    """Return a validated planning envelope; this function performs no I/O."""
    location = _effective_location(request)
    selected_plot = _selected_plot(request)
    water_access = request.water_access
    if water_access is None and selected_plot is not None:
        water_access = selected_plot.water_access
    if water_access is WaterAccess.UNKNOWN:
        water_access = None

    missing = _missing_information(request, location, water_access)
    candidates = find_crop_candidates(
        request.preferred_crop, request.compare_available_crops
    )
    assessments = [
        _assessment(entry, water_access, missing) for entry in candidates
    ]

    unsupported_requested_crop = (
        request.preferred_crop is not None
        and request.preferred_crop.casefold().strip()
        not in {"rice", "nerica", "rice nerica"}
    )
    limitations = list(_BASE_LIMITATIONS)
    if unsupported_requested_crop:
        assessments.insert(0, _unsupported_crop_assessment(request.preferred_crop))
        limitations.append(
            f"No verified catalogue entry is available for the requested crop: {request.preferred_crop}."
        )

    status = (
        AgentStatus.NEEDS_INPUT
        if any(item.importance is InformationImportance.REQUIRED for item in missing)
        else AgentStatus.PARTIAL
    )
    result = CropPlanningResult(
        assessments=assessments,
        ordering_reason=(
            "Assessments are grouped by evidence-backed cultivation system; "
            "the available evidence does not support a comparative crop ranking."
        ),
    )
    evidence_ids = list(
        dict.fromkeys(
            evidence.evidence_id
            for assessment in assessments
            for evidence in assessment.evidence
        )
    )
    return AgentResponse(
        agent=_AGENT_NAME,
        status=status,
        result=result.model_dump(mode="json"),
        evidence_ids=evidence_ids,
        missing_information=missing,
        limitations=limitations,
        requires_human_input=bool(missing),
    )
