"""SQLite persistence for farmer profiles."""

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any, Mapping
from uuid import UUID

from pydantic import ValidationError

from app.schemas import FarmerProfile

_BACKEND_DIR = Path(__file__).resolve().parents[1]
DEFAULT_DATABASE_PATH = _BACKEND_DIR / "data" / "farmwise.sqlite3"


class DatabaseError(RuntimeError):
    """A database operation failed without exposing low-level database details."""


class DuplicateProfileError(ValueError):
    """A new profile cannot be saved because its farmer ID already exists."""


class ProfileNotFoundError(LookupError):
    """An explicitly requested profile update found no matching farmer."""


def _resolve_database_path(database_path: str | Path | None) -> Path:
    if database_path is None:
        return DEFAULT_DATABASE_PATH
    path = Path(database_path)
    if not path.is_absolute():
        path = _BACKEND_DIR / path
    return path.resolve()


def initialize_database(database_path: str | Path | None = None) -> Path:
    """Create the profile table if needed; return the resolved database path.

    Relative custom paths are resolved from the backend directory, not the
    process's current working directory. Repeated calls preserve all rows.
    """
    path = _resolve_database_path(database_path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path)) as connection:
            with connection:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS farmer_profiles (
                        farmer_id TEXT PRIMARY KEY,
                        profile_json TEXT NOT NULL
                    )
                    """
                )
    except (OSError, sqlite3.Error) as exc:
        raise DatabaseError("Could not initialize the farmer profile database.") from exc
    return path


def _validate_profile(profile: FarmerProfile | Mapping[str, Any]) -> FarmerProfile:
    try:
        profile_data = (
            profile.model_dump(mode="python")
            if isinstance(profile, FarmerProfile)
            else profile
        )
        return FarmerProfile.model_validate(profile_data)
    except ValidationError:
        raise
    except (TypeError, ValueError) as exc:
        raise ValueError("Profile data must match the FarmerProfile schema.") from exc


def _parse_farmer_id(farmer_id: UUID | str) -> UUID:
    try:
        return farmer_id if isinstance(farmer_id, UUID) else UUID(farmer_id)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("farmer_id must be a valid UUID.") from exc


def _connect(database_path: str | Path | None) -> sqlite3.Connection:
    path = initialize_database(database_path)
    try:
        return sqlite3.connect(path)
    except sqlite3.Error as exc:
        raise DatabaseError("Could not access the farmer profile database.") from exc


def save_profile(
    profile: FarmerProfile | Mapping[str, Any],
    database_path: str | Path | None = None,
) -> FarmerProfile:
    """Insert a new profile; duplicate farmer IDs raise DuplicateProfileError."""
    validated = _validate_profile(profile)
    try:
        with closing(_connect(database_path)) as connection:
            with connection:
                connection.execute(
                    """
                    INSERT INTO farmer_profiles (farmer_id, profile_json)
                    VALUES (?, ?)
                    """,
                    (str(validated.farmer_id), validated.model_dump_json()),
                )
    except sqlite3.IntegrityError as exc:
        if "UNIQUE constraint failed" in str(exc):
            raise DuplicateProfileError(
                f"A profile already exists for farmer_id {validated.farmer_id}."
            ) from exc
        raise DatabaseError("Could not save the farmer profile.") from exc
    except sqlite3.Error as exc:
        raise DatabaseError("Could not save the farmer profile.") from exc
    return validated


def get_profile(
    farmer_id: UUID | str,
    database_path: str | Path | None = None,
) -> FarmerProfile | None:
    """Return a validated profile, or None when the farmer ID is not found."""
    profile_id = _parse_farmer_id(farmer_id)
    try:
        with closing(_connect(database_path)) as connection:
            row = connection.execute(
                "SELECT profile_json FROM farmer_profiles WHERE farmer_id = ?",
                (str(profile_id),),
            ).fetchone()
    except sqlite3.Error as exc:
        raise DatabaseError("Could not retrieve the farmer profile.") from exc

    if row is None:
        return None
    try:
        return FarmerProfile.model_validate_json(row[0])
    except ValidationError as exc:
        raise DatabaseError("Stored farmer profile data is invalid.") from exc


def update_profile(
    profile: FarmerProfile | Mapping[str, Any],
    database_path: str | Path | None = None,
) -> FarmerProfile:
    """Replace a profile only when explicitly called; missing IDs raise an error."""
    validated = _validate_profile(profile)
    try:
        with closing(_connect(database_path)) as connection:
            with connection:
                cursor = connection.execute(
                    """
                    UPDATE farmer_profiles
                    SET profile_json = ?
                    WHERE farmer_id = ?
                    """,
                    (validated.model_dump_json(), str(validated.farmer_id)),
                )
                if cursor.rowcount == 0:
                    raise ProfileNotFoundError(
                        f"No profile exists for farmer_id {validated.farmer_id}."
                    )
    except sqlite3.Error as exc:
        raise DatabaseError("Could not update the farmer profile.") from exc
    return validated


def delete_profile(
    farmer_id: UUID | str,
    database_path: str | Path | None = None,
) -> bool:
    """Delete an explicitly requested profile; return False if it does not exist."""
    profile_id = _parse_farmer_id(farmer_id)
    try:
        with closing(_connect(database_path)) as connection:
            with connection:
                cursor = connection.execute(
                    "DELETE FROM farmer_profiles WHERE farmer_id = ?",
                    (str(profile_id),),
                )
    except sqlite3.Error as exc:
        raise DatabaseError("Could not delete the farmer profile.") from exc
    return cursor.rowcount > 0
