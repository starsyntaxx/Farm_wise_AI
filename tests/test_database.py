import sqlite3
from datetime import date, datetime, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.database import (
    DatabaseError,
    DuplicateProfileError,
    ProfileNotFoundError,
    delete_profile,
    get_profile,
    initialize_database,
    save_profile,
    update_profile,
)
from app.schemas import (
    AreaUnit,
    FarmLocationInput,
    FarmPlot,
    FarmerProfile,
    WaterAccess,
)

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)


def make_profile(
    *,
    farmer_id=None,
    preferred_name: str = "Amina",
    plot_id=None,
) -> FarmerProfile:
    return FarmerProfile(
        farmer_id=farmer_id or uuid4(),
        preferred_name=preferred_name,
        preferred_language="ha",
        location=FarmLocationInput(
            state="Kano",
            lga="Dala",
            community="Gwale",
            latitude=12.0,
            longitude=8.5,
        ),
        plots=[
            FarmPlot(
                plot_id=plot_id or uuid4(),
                name="North field",
                area_value=1.5,
                area_unit=AreaUnit.HECTARES,
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


def test_initialization_creates_table_and_is_idempotent(tmp_path) -> None:
    database_path = tmp_path / "nested" / "farmwise.sqlite3"

    resolved_path = initialize_database(database_path)
    profile = make_profile()
    save_profile(profile, database_path)
    initialize_database(database_path)

    assert resolved_path == database_path.resolve()
    with sqlite3.connect(database_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = ?",
                ("table",),
            )
        }
    assert "farmer_profiles" in tables
    assert get_profile(profile.farmer_id, database_path) == profile


def test_profile_round_trip_preserves_nested_data_ids_and_timestamps(tmp_path) -> None:
    database_path = tmp_path / "profiles.db"
    profile = make_profile()

    saved = save_profile(profile, database_path)
    retrieved = get_profile(profile.farmer_id, database_path)

    assert saved == profile
    assert retrieved == profile
    assert retrieved is not None
    assert retrieved.farmer_id == profile.farmer_id
    assert retrieved.created_at == profile.created_at
    assert retrieved.updated_at == profile.updated_at
    assert retrieved.location == profile.location
    assert retrieved.plots[0].plot_id == profile.plots[0].plot_id
    assert retrieved.plots[0].created_at == profile.plots[0].created_at
    assert retrieved.plots[0].updated_at == profile.plots[0].updated_at
    assert retrieved.plots[0].planting_date == date(2026, 6, 1)


def test_duplicate_ids_are_rejected_without_replacing_saved_profile(tmp_path) -> None:
    database_path = tmp_path / "profiles.db"
    profile = make_profile()
    conflicting_profile = make_profile(
        farmer_id=profile.farmer_id,
        preferred_name="Binta",
    )
    save_profile(profile, database_path)

    with pytest.raises(DuplicateProfileError):
        save_profile(conflicting_profile, database_path)

    assert get_profile(profile.farmer_id, database_path) == profile


def test_update_changes_only_the_explicitly_selected_profile(tmp_path) -> None:
    database_path = tmp_path / "profiles.db"
    first = make_profile()
    second = make_profile()
    save_profile(first, database_path)
    save_profile(second, database_path)
    updated = first.model_copy(update={"preferred_name": "Fatima"})

    result = update_profile(updated, database_path)

    assert result.preferred_name == "Fatima"
    assert get_profile(first.farmer_id, database_path) == updated
    assert get_profile(second.farmer_id, database_path) == second


def test_update_of_missing_profile_raises_clear_exception(tmp_path) -> None:
    with pytest.raises(ProfileNotFoundError):
        update_profile(make_profile(), tmp_path / "profiles.db")


def test_get_nonexistent_profile_returns_none(tmp_path) -> None:
    assert get_profile(uuid4(), tmp_path / "profiles.db") is None


def test_delete_is_explicit_and_reports_missing_rows(tmp_path) -> None:
    database_path = tmp_path / "profiles.db"
    profile = make_profile()
    save_profile(profile, database_path)

    assert delete_profile(profile.farmer_id, database_path) is True
    assert get_profile(profile.farmer_id, database_path) is None
    assert delete_profile(profile.farmer_id, database_path) is False


@pytest.mark.parametrize(
    "invalid_profile",
    [
        {"farmer_id": "not-a-uuid"},
        {
            "farmer_id": str(uuid4()),
            "created_at": "2026-10-27T12:00:00",
            "updated_at": "2026-10-27T12:00:00Z",
        },
    ],
)
def test_invalid_profile_data_is_rejected_before_save(
    tmp_path,
    invalid_profile: dict,
) -> None:
    with pytest.raises(ValidationError):
        save_profile(invalid_profile, tmp_path / "profiles.db")


def test_invalid_farmer_id_is_rejected(tmp_path) -> None:
    with pytest.raises(ValueError, match="valid UUID"):
        get_profile("invalid", tmp_path / "profiles.db")


def test_database_access_failures_raise_safe_database_error(tmp_path) -> None:
    database_path = tmp_path / "not-a-database-file"
    database_path.mkdir()

    with pytest.raises(DatabaseError, match="Could not initialize"):
        initialize_database(database_path)


def test_parameterized_profile_values_are_stored_as_data(tmp_path) -> None:
    database_path = tmp_path / "profiles.db"
    profile = make_profile(preferred_name="Farmer'); DROP TABLE farmer_profiles; --")

    save_profile(profile, database_path)

    assert get_profile(profile.farmer_id, database_path) == profile
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT count(*) FROM farmer_profiles"
        ).fetchone()[0] == 1
