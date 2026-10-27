"""FastAPI routes connecting profile persistence and crop planning."""

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator
from uuid import UUID

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse

from app.agents.crop_planning import process_crop_planning
from app.database import (
    DatabaseError,
    DuplicateProfileError,
    get_profile,
    initialize_database,
    save_profile,
)
from app.schemas import (
    AgentResponse,
    CropPlanningAPIRequest,
    CropPlanningRequest,
    FarmerProfile,
)


def create_app(database_path: str | Path | None = None) -> FastAPI:
    """Create an app, optionally using an isolated SQLite database path."""
    configured_path = database_path
    if configured_path is None:
        configured_path = os.environ.get("FARMWISE_DATABASE_PATH")

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        initialize_database(configured_path)
        yield

    application = FastAPI(
        title="FarmWise AI API",
        version="0.1.0",
        lifespan=lifespan,
    )
    application.state.database_path = configured_path

    @application.exception_handler(DatabaseError)
    async def handle_database_error(
        _: Request, __: DatabaseError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": "The profile database could not complete the request."},
        )

    @application.exception_handler(Exception)
    async def handle_unexpected_error(_: Request, __: Exception) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": "An unexpected server error occurred."},
        )

    @application.get("/health")
    def health() -> dict[str, str]:
        return {"status": "available", "service": "FarmWise AI API"}

    @application.post(
        "/profiles",
        response_model=FarmerProfile,
        status_code=status.HTTP_201_CREATED,
    )
    def create_profile(
        profile: FarmerProfile, request: Request
    ) -> FarmerProfile:
        try:
            return save_profile(profile, request.app.state.database_path)
        except DuplicateProfileError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="A profile already exists for this farmer_id.",
            ) from exc

    @application.get(
        "/profiles/{farmer_id}",
        response_model=FarmerProfile,
    )
    def read_profile(farmer_id: UUID, request: Request) -> FarmerProfile:
        profile = get_profile(farmer_id, request.app.state.database_path)
        if profile is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Farmer profile not found.",
            )
        return profile

    @application.post(
        "/planning/assess",
        response_model=AgentResponse,
    )
    def assess_plan(
        assessment_request: CropPlanningAPIRequest,
        request: Request,
    ) -> AgentResponse:
        profile = None
        if assessment_request.farmer_id is not None:
            profile = get_profile(
                assessment_request.farmer_id,
                request.app.state.database_path,
            )
            if profile is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Farmer profile not found.",
                )

        planning_fields = assessment_request.model_dump(
            exclude={"farmer_id", "profile"}
        )
        try:
            planning_request = CropPlanningRequest(
                profile=profile,
                **planning_fields,
            )
            return process_crop_planning(planning_request)
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail=str(exc),
            ) from exc

    return application


app = create_app()
