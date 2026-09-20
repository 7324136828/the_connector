"""Saved configuration library management and config.json downloads."""

import json

from fastapi import APIRouter, HTTPException, Response

from ..schemas.library import ConfigurationCreate, ConfigurationRecord, ConfigurationUpdate
from ..services.configuration_manager import (
    ConfigurationNotFoundError,
    DuplicateModelIdError,
    configuration_manager,
)

router = APIRouter(prefix="/api/configs", tags=["configurations"])


@router.get("", response_model=list[ConfigurationRecord])
def list_configurations(active_only: bool = False):
    return configuration_manager.list_configs(active_only=active_only)


@router.post("", response_model=ConfigurationRecord, status_code=201)
def create_configuration(request: ConfigurationCreate):
    try:
        return configuration_manager.create_config(**request.model_dump())
    except DuplicateModelIdError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/{config_id}", response_model=ConfigurationRecord)
def get_configuration(config_id: str):
    record = configuration_manager.get_config(config_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Configuration not found.")
    return record


@router.patch("/{config_id}", response_model=ConfigurationRecord)
def update_configuration(config_id: str, request: ConfigurationUpdate):
    try:
        return configuration_manager.update_config(config_id, **request.model_dump(exclude_unset=True))
    except ConfigurationNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.delete("/{config_id}", status_code=204)
def delete_configuration(config_id: str):
    if not configuration_manager.delete_config(config_id):
        raise HTTPException(status_code=404, detail="Configuration not found.")
    return Response(status_code=204)


@router.get("/{config_id}/download")
def download_configuration(config_id: str):
    record = get_configuration(config_id)
    return Response(
        content=json.dumps(record["config"], indent=2, ensure_ascii=False) + "\n",
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="config.json"'},
    )
