"""Browse and reuse every successfully loaded configuration snapshot."""

import json

from fastapi import APIRouter, HTTPException, Response

from ..schemas.configuration_history import ConfigurationHistoryCreate, ConfigurationHistoryRecord
from ..services.configuration_history import configuration_history_manager

router = APIRouter(prefix="/api/config-history", tags=["configuration history"])


@router.get("", response_model=list[ConfigurationHistoryRecord])
def list_configuration_history():
    return configuration_history_manager.list_history()


@router.post("", response_model=ConfigurationHistoryRecord, status_code=201)
def remember_configuration(request: ConfigurationHistoryCreate):
    return configuration_history_manager.remember(**request.model_dump())


@router.get("/{history_id}", response_model=ConfigurationHistoryRecord)
def get_configuration_history(history_id: str):
    record = configuration_history_manager.get_history(history_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Configuration history entry not found.")
    return record


@router.get("/{history_id}/download")
def download_configuration_history(history_id: str):
    record = get_configuration_history(history_id)
    return Response(
        content=json.dumps(record["config"], indent=2, ensure_ascii=False) + "\n",
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="config.json"'},
    )
