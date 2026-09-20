"""Isolated system temp directory management and ZIP packaging per productionization standards."""

from __future__ import annotations

import json
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Dict, Optional

from ..schemas.chat import SessionDetail


class TempManager:
    """Manages isolated job folders under system temp for scratch data and exports."""

    def __init__(self):
        self.base_temp_dir = Path(tempfile.gettempdir()) / "the_connector_jobs"
        self.base_temp_dir.mkdir(parents=True, exist_ok=True)

    def get_job_dir(self, session_or_job_id: str) -> Path:
        """Return or create isolated folder structure under system temp."""
        job_dir = self.base_temp_dir / session_or_job_id
        (job_dir / "inputs").mkdir(parents=True, exist_ok=True)
        (job_dir / "work").mkdir(parents=True, exist_ok=True)
        (job_dir / "outputs").mkdir(parents=True, exist_ok=True)
        (job_dir / "archive").mkdir(parents=True, exist_ok=True)
        return job_dir

    def package_session_export_zip(self, session_detail: SessionDetail) -> Path:
        """Package a session's conversation history into a ZIP archive."""
        session = session_detail.session
        messages = session_detail.messages

        job_dir = self.get_job_dir(session.session_id)
        outputs_dir = job_dir / "outputs"
        archive_dir = job_dir / "archive"

        # 1. Write metadata.json
        metadata_file = outputs_dir / "metadata.json"
        with open(metadata_file, "w", encoding="utf-8") as f:
            json.dump(session.model_dump(), f, indent=2)

        # 2. Write transcript.json
        transcript_json_file = outputs_dir / "transcript.json"
        with open(transcript_json_file, "w", encoding="utf-8") as f:
            json.dump([m.model_dump() for m in messages], f, indent=2)

        # 3. Write human-readable transcript.md
        transcript_md_file = outputs_dir / "transcript.md"
        with open(transcript_md_file, "w", encoding="utf-8") as f:
            f.write(f"# Chat Transcript: {session.title}\n\n")
            f.write(f"- **Session ID**: `{session.session_id}`\n")
            f.write(f"- **Provider / Model**: `{session.provider}` / `{session.model}`\n")
            f.write(f"- **Past Memory Enabled**: `{session.past_memory}`\n")
            f.write(f"- **Total Messages**: {len(messages)}\n")
            f.write(f"- **Created At**: {session.created_at}\n\n---\n\n")

            for m in messages:
                role_label = m.role.upper()
                f.write(f"### {role_label} ({m.created_at or ''})\n\n")
                if m.provider and m.model:
                    f.write(f"*Routed via {m.provider}/{m.model} | Latency: {m.latency_ms or 0:.1f}ms*\n\n")
                f.write(f"{m.content}\n\n---\n\n")

        # 4. Package all files into ZIP
        zip_path = archive_dir / f"{session.session_id}_export.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for file in outputs_dir.glob("*"):
                if file.is_file():
                    zf.write(file, arcname=file.name)

        return zip_path

    def purge_temp(self, session_or_job_id: str) -> None:
        """Purge isolated temp folder when job or session is closed/discarded."""
        job_dir = self.base_temp_dir / session_or_job_id
        if job_dir.exists() and job_dir.is_dir():
            shutil.rmtree(job_dir, ignore_errors=True)


temp_manager = TempManager()
