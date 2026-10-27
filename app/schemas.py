"""Shared, provider-independent data contracts for the FarmWise backend."""

import math
import re
from datetime import date, datetime
from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)


class StrictModel(BaseModel):
    """Reject undeclared fields so misspelled input cannot pass unnoticed."""

    model_config = ConfigDict(extra="forbid")


class AgentStatus(str, Enum):
    SUCCESS = "success"
    PARTIAL = "partial"
    NEEDS_INPUT = "needs_input"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"


class FarmWiseTask(str, Enum):
    CROP_PLANNING = "crop_planning"
    CROP_MONITORING = "crop_monitoring"
    PROFILE_UPDATE = "profile_update"


class InformationSourceType(str, Enum):
    FARMER_FORM = "farmer_form"
    FARMER_MESSAGE = "farmer_message"
    SAVED_PROFILE = "saved_profile"
    FARM_JOURNAL = "farm_journal"
    EXTERNAL_SOURCE = "external_source"
    SYSTEM = "system"


class WaterAccess(str, Enum):
    RAINFED = "rainfed"
    IRRIGATED = "irrigated"
    BOTH = "both"
    UNKNOWN = "unknown"


class AreaUnit(str, Enum):
    HECTARES = "hectares"
    ACRES = "acres"


class InformationImportance(str, Enum):
    REQUIRED = "required"
    USEFUL = "useful"
    OPTIONAL = "optional"


class CropSuitabilityStatus(str, Enum):
    SUITABLE = "suitable"
    CONDITIONALLY_SUITABLE = "conditionally_suitable"
    INSUFFICIENT_INFORMATION = "insufficient_information"
    NOT_RECOMMENDED = "not_recommended"


class CropEvidenceStatus(str, Enum):
    VERIFIED = "verified"
    INCOMPLETE = "incomplete"
    ILLUSTRATIVE = "illustrative"


def _trim_optional_text(value: Any) -> Any:
    if isinstance(value, str):
        value = value.strip()
        if not value:
            raise ValueError("must not be empty")
    return value


def _is_timezone_aware(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() is not None


class FarmLocationInput(StrictModel):
    state: str | None = None
    lga: str | None = None
    community: str | None = None
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)

    @field_validator("state", "lga", "community", mode="before")
    @classmethod
    def trim_text(cls, value: Any) -> Any:
        return _trim_optional_text(value)

    @model_validator(mode="after")
    def coordinates_are_a_pair(self) -> "FarmLocationInput":
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError("latitude and longitude must be supplied together")
        return self


class FarmPlotInput(StrictModel):
    name: str | None = None
    area_value: float | None = Field(default=None, gt=0)
    area_unit: AreaUnit | None = None
    water_access: WaterAccess = WaterAccess.UNKNOWN
    current_crop: str | None = None
    planting_date: date | None = None

    @field_validator("name", "current_crop", mode="before")
    @classmethod
    def trim_text(cls, value: Any) -> Any:
        return _trim_optional_text(value)

    @model_validator(mode="after")
    def area_value_and_unit_are_a_pair(self) -> "FarmPlotInput":
        if (self.area_value is None) != (self.area_unit is None):
            raise ValueError("area_value and area_unit must be supplied together")
        return self


class FarmPlot(FarmPlotInput):
    plot_id: UUID
    created_at: datetime
    updated_at: datetime

    @field_validator("created_at", "updated_at")
    @classmethod
    def timestamps_must_include_timezone(cls, value: datetime) -> datetime:
        if not _is_timezone_aware(value):
            raise ValueError("timestamp must include a timezone")
        return value


class FarmerProfileInput(StrictModel):
    preferred_name: str | None = None
    location: FarmLocationInput | None = None
    preferred_language: str | None = None
    plots: list[FarmPlotInput] = Field(default_factory=list)

    @field_validator("preferred_name", "preferred_language", mode="before")
    @classmethod
    def trim_text(cls, value: Any) -> Any:
        return _trim_optional_text(value)


class FarmerProfile(FarmerProfileInput):
    farmer_id: UUID
    plots: list[FarmPlot] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime

    @field_validator("created_at", "updated_at")
    @classmethod
    def timestamps_must_include_timezone(cls, value: datetime) -> datetime:
        if not _is_timezone_aware(value):
            raise ValueError("timestamp must include a timezone")
        return value


class FieldSource(StrictModel):
    source_type: InformationSourceType
    record_id: str | None = None
    observed_at: datetime | None = None
    confirmed_by_farmer: bool

    @field_validator("record_id", mode="before")
    @classmethod
    def trim_record_id(cls, value: Any) -> Any:
        return _trim_optional_text(value)

    @field_validator("observed_at")
    @classmethod
    def observed_timestamp_must_include_timezone(
        cls, value: datetime | None
    ) -> datetime | None:
        if value is not None and not _is_timezone_aware(value):
            raise ValueError("timestamp must include a timezone")
        return value


_PROFILE_FIELD_PATHS = {
    "preferred_name",
    "location",
    "location.state",
    "location.lga",
    "location.community",
    "location.latitude",
    "location.longitude",
    "preferred_language",
    "plots",
}
_PLOT_FIELD_NAMES = {
    "name",
    "area_value",
    "area_unit",
    "water_access",
    "current_crop",
    "planting_date",
}
_PLOT_FIELD_PATH = re.compile(
    r"plots(?:\[\d+\])(?:\.(?:"
    + "|".join(sorted(_PLOT_FIELD_NAMES))
    + r"))?\Z"
)


def _is_profile_field_path(value: str) -> bool:
    return value in _PROFILE_FIELD_PATHS or _PLOT_FIELD_PATH.fullmatch(value) is not None


def _is_json_value(value: Any) -> bool:
    if value is None or isinstance(value, (str, bool, int)):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, list):
        return all(_is_json_value(item) for item in value)
    if isinstance(value, dict):
        return all(
            isinstance(key, str) and _is_json_value(item)
            for key, item in value.items()
        )
    return False


class ProposedFieldChange(StrictModel):
    field_path: str
    previous_value: Any
    proposed_value: Any
    source: FieldSource
    requires_confirmation: bool
    reason: str

    @field_validator("field_path", mode="before")
    @classmethod
    def validate_profile_field_path(cls, value: Any) -> Any:
        if not isinstance(value, str) or not _is_profile_field_path(value):
            raise ValueError("field_path must identify a recognised profile field")
        return value

    @field_validator("previous_value", "proposed_value")
    @classmethod
    def validate_json_compatibility(cls, value: Any) -> Any:
        if not _is_json_value(value):
            raise ValueError("value must be JSON-compatible")
        return value

    @field_validator("reason", mode="before")
    @classmethod
    def trim_reason(cls, value: Any) -> Any:
        return _trim_optional_text(value)


class MissingInformation(StrictModel):
    field: str
    importance: InformationImportance
    reason: str
    question: str | None = None

    @field_validator("field", "reason", mode="before")
    @classmethod
    def trim_required_text(cls, value: Any) -> Any:
        return _trim_optional_text(value)

    @field_validator("question", mode="before")
    @classmethod
    def trim_question(cls, value: Any) -> Any:
        return _trim_optional_text(value)


class AgentResponse(StrictModel):
    agent: str
    status: AgentStatus
    result: dict[str, Any] | None
    evidence_ids: list[str] = Field(default_factory=list)
    missing_information: list[MissingInformation] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    requires_human_input: bool

    @field_validator("agent")
    @classmethod
    def agent_identifier_must_not_be_empty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("agent must not be empty")
        return value

    @field_validator("result")
    @classmethod
    def result_must_be_json_compatible(
        cls, value: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        if value is not None and not _is_json_value(value):
            raise ValueError("result must contain only JSON-compatible values")
        return value

    @field_validator("evidence_ids", "limitations")
    @classmethod
    def trim_list_text(cls, values: list[str]) -> list[str]:
        trimmed = [value.strip() for value in values]
        if any(not value for value in trimmed):
            raise ValueError("list values must not be empty")
        return trimmed

    @model_validator(mode="after")
    def status_has_required_explanation(self) -> "AgentResponse":
        if self.status is AgentStatus.NEEDS_INPUT and not self.missing_information:
            raise ValueError("needs_input requires missing_information")
        if self.status is AgentStatus.UNAVAILABLE and not self.limitations:
            raise ValueError("unavailable requires an explanatory limitation")
        return self


class CropEvidenceReference(StrictModel):
    evidence_id: str
    source_name: str
    source_url: AnyHttpUrl
    claim: str

    @field_validator("evidence_id", "source_name", "claim", mode="before")
    @classmethod
    def trim_evidence_text(cls, value: Any) -> Any:
        return _trim_optional_text(value)


class CropPlanningRequest(StrictModel):
    profile: FarmerProfile | None = None
    location: FarmLocationInput | None = None
    plot_id: UUID | None = None
    planting_period: str | None = None
    water_access: WaterAccess | None = None
    area_value: float | None = Field(default=None, gt=0)
    area_unit: AreaUnit | None = None
    soil_type: str | None = None
    preferred_crop: str | None = None
    compare_available_crops: bool = False

    @field_validator("planting_period", "soil_type", "preferred_crop", mode="before")
    @classmethod
    def trim_optional_planning_text(cls, value: Any) -> Any:
        return _trim_optional_text(value)

    @model_validator(mode="after")
    def planning_area_value_and_unit_are_a_pair(self) -> "CropPlanningRequest":
        if (self.area_value is None) != (self.area_unit is None):
            raise ValueError("area_value and area_unit must be supplied together")
        return self


class CropPlanningAPIRequest(CropPlanningRequest):
    """HTTP planning input; the API loads the profile from persistence."""

    farmer_id: UUID | None = None
    profile: None = None


class CropAssessment(StrictModel):
    crop_id: str
    crop_name: str
    cultivation_system: str
    evidence_status: CropEvidenceStatus
    suitability: CropSuitabilityStatus
    supporting_factors: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    missing_information: list[MissingInformation] = Field(default_factory=list)
    evidence: list[CropEvidenceReference] = Field(default_factory=list)
    explanation: str


class CropPlanningResult(StrictModel):
    assessments: list[CropAssessment]
    ordering_reason: str


class FarmerContextRequest(StrictModel):
    task: FarmWiseTask
    profile: FarmerProfile | None
    new_information: FarmerProfileInput | None
    source: FieldSource


class FarmerContextData(StrictModel):
    profile: FarmerProfile | None
    task: FarmWiseTask
    available_fields: list[str]


class FarmerContextResult(StrictModel):
    task: FarmWiseTask
    context: FarmerContextData
    field_sources: dict[str, FieldSource]
    proposed_changes: list[ProposedFieldChange]
    confirmation_required: bool