"""
This module is for data validation,
it will make sure that each data comming from frontend must have this formate.
"""

from pydantic import BaseModel
from typing import Optional, List

class TranscriptionTask(BaseModel):
    session_id: str
    action: str
    audio_chunk: Optional[str] = None
    clinic_id: Optional[str] = None
    patient_pin: Optional[str] = None

    chunk_id: Optional[int] = None

    ref_ids: Optional[List[str]] = None

    packet_index: Optional[int] = None
    total_packets: Optional[int] = None

    error_code: str | None = None
    error_message: str | None = None
