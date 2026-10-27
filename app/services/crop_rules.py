"""Evidence-scoped crop catalogue and deterministic suitability rules."""

from pydantic import Field

from app.schemas import (
    CropEvidenceReference,
    CropEvidenceStatus,
    CropSuitabilityStatus,
    StrictModel,
    WaterAccess,
)


class CropCatalogueEntry(StrictModel):
    crop_id: str
    crop_name: str
    cultivation_system: str
    evidence_status: CropEvidenceStatus
    supported_water_access: WaterAccess
    evidence: list[CropEvidenceReference]
    limitations: list[str] = Field(default_factory=list)


_AFRICARICE_URL = "https://www.africarice.org/innovations/varieties/nerica"
_FAO_WATER_URL = "https://www.fao.org/4/s2022e/s2022e02.htm"

_CATALOGUE = (
    CropCatalogueEntry(
        crop_id="rice_nerica_upland",
        crop_name="Rice (upland NERICA)",
        cultivation_system="Upland NERICA",
        evidence_status=CropEvidenceStatus.INCOMPLETE,
        supported_water_access=WaterAccess.RAINFED,
        evidence=[
            CropEvidenceReference(
                evidence_id="africarice-nerica",
                source_name="AfricaRice — NERICA varieties",
                source_url=_AFRICARICE_URL,
                claim=(
                    "AfricaRice describes upland NERICA varieties as adapted "
                    "to rainfed ecosystems in Africa."
                ),
            ),
            CropEvidenceReference(
                evidence_id="fao-crop-water-needs",
                source_name="FAO — Crop Water Needs",
                source_url=_FAO_WATER_URL,
                claim=(
                    "FAO states that rice generally has greater crop water "
                    "needs than beans and wheat; it provides no local estimate."
                ),
            ),
        ],
        limitations=[
            "NERICA evidence is varietal and ecosystem-specific, not a guarantee for all rice.",
            "No Nigeria-specific location, season, soil, or cultivar assessment is available.",
        ],
    ),
    CropCatalogueEntry(
        crop_id="rice_nerica_irrigated",
        crop_name="Rice (irrigated NERICA)",
        cultivation_system="Irrigated NERICA",
        evidence_status=CropEvidenceStatus.INCOMPLETE,
        supported_water_access=WaterAccess.IRRIGATED,
        evidence=[
            CropEvidenceReference(
                evidence_id="africarice-nerica",
                source_name="AfricaRice — NERICA varieties",
                source_url=_AFRICARICE_URL,
                claim=(
                    "AfricaRice distinguishes NERICA varieties adapted to "
                    "irrigated environments, including four irrigated varieties."
                ),
            ),
            CropEvidenceReference(
                evidence_id="fao-crop-water-needs",
                source_name="FAO — Crop Water Needs",
                source_url=_FAO_WATER_URL,
                claim=(
                    "FAO states that rice generally has greater crop water "
                    "needs than beans and wheat; it provides no local estimate."
                ),
            ),
        ],
        limitations=[
            "The evidence describes NERICA varieties, not all rice cultivars.",
            "No Nigeria-specific location, season, soil, or cultivar assessment is available.",
        ],
    ),
)

_RICE_NAMES = {"rice", "nerica", "rice nerica"}


def get_crop_catalogue() -> tuple[CropCatalogueEntry, ...]:
    """Return independent validated records so callers cannot mutate the catalogue."""
    return tuple(entry.model_copy(deep=True) for entry in _CATALOGUE)


def find_crop_candidates(
    preferred_crop: str | None,
    compare_available_crops: bool,
) -> tuple[CropCatalogueEntry, ...]:
    """Select evidence-backed records without treating unsupported crops as matches."""
    if compare_available_crops:
        return get_crop_catalogue()
    if preferred_crop is not None and preferred_crop.casefold().strip() in _RICE_NAMES:
        return get_crop_catalogue()
    return ()


def assess_water_access(
    entry: CropCatalogueEntry,
    water_access: WaterAccess | None,
) -> CropSuitabilityStatus:
    """Assess only the documented cultivation-system and water-access relationship."""
    if water_access in (None, WaterAccess.UNKNOWN):
        return CropSuitabilityStatus.INSUFFICIENT_INFORMATION
    if water_access is entry.supported_water_access or water_access is WaterAccess.BOTH:
        return CropSuitabilityStatus.CONDITIONALLY_SUITABLE
    if (
        entry.supported_water_access is WaterAccess.IRRIGATED
        and water_access is WaterAccess.RAINFED
    ):
        return CropSuitabilityStatus.NOT_RECOMMENDED
    return CropSuitabilityStatus.INSUFFICIENT_INFORMATION
