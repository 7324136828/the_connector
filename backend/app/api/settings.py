"""Read and save preferences; storage services adopt changes on restart."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, StrictStr

from ..config import settings
from ..user_settings import (
    UserSettingsError, default_data_directory, load_user_settings,
    resolve_data_directory, save_data_directory,
)

router = APIRouter(prefix="/api/settings", tags=["settings"])


class StorageSettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    data_dir: StrictStr


def settings_payload(values: dict) -> dict:
    configured = values.get("data_dir", "")
    configuration_error = ""
    try:
        requested = resolve_data_directory(configured)
    except UserSettingsError as error:
        # An override can keep the app running when a saved path becomes invalid.
        # Return that preference so the user can repair it through Settings.
        requested = None
        configuration_error = str(error)
    payload = {
        "data_dir": str(settings.data_dir),
        "configured_data_dir": configured if isinstance(configured, str) else "",
        "default_data_dir": str(default_data_directory()),
        "settings_path": str(settings.settings_path),
        "restart_required": requested != settings.data_dir.resolve(),
    }
    if configuration_error:
        payload["configuration_error"] = configuration_error
    return payload


@router.get("")
def get_settings():
    try:
        return settings_payload(load_user_settings(settings.settings_path))
    except (UserSettingsError, OSError) as error:
        raise HTTPException(status_code=500, detail=str(error)) from error


@router.patch("")
def update_settings(update: StorageSettingsUpdate):
    try:
        return settings_payload(save_data_directory(update.data_dir, settings.settings_path))
    except UserSettingsError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except OSError as error:
        raise HTTPException(status_code=500, detail=f"Could not save settings: {error}") from error
