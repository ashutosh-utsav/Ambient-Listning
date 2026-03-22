"""
Knowledge Base API endpoints.

GET  /kb/clients/{clinic_id}/{patient_pin}/brief      — pre-session client brief
GET  /kb/clients/{clinic_id}/{patient_pin}/sessions    — session timeline
GET  /kb/clients/{clinic_id}/{patient_pin}/sessions/{session_id} — session detail
POST /kb/clients/{clinic_id}/{patient_pin}/sessions    — manually submit a session (testing)
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, HTMLResponse
from pydantic import BaseModel
from typing import List, Optional

from core.kb_service import (
    get_brief, get_session_timeline, get_session_detail,
    index_session_and_update_brief,
)

router = APIRouter(prefix="/kb", tags=["knowledge-base"])


class SessionSubmit(BaseModel):
    session_id: str
    summary: str = ""
    bullet_points: List[str] = []
    action_items: List[str] = []
    participants: List[str] = []


@router.get("/", response_class=HTMLResponse)
async def kb_test_page():
    with open("static/kb.html", "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())


@router.get("/clients/{clinic_id}/{patient_pin}/brief")
async def client_brief(clinic_id: str, patient_pin: str, request: Request):
    brief = await get_brief(
        clinic_id=clinic_id,
        patient_pin=patient_pin,
        kb_table=request.app.state.dynamodb_kb_table,
        s3_client=request.app.state.s3,
    )
    if brief is None:
        return JSONResponse(
            status_code=404,
            content={"detail": "No brief found for this client. No sessions have been processed yet."},
        )
    return brief


@router.get("/clients/{clinic_id}/{patient_pin}/sessions")
async def client_sessions(clinic_id: str, patient_pin: str, request: Request):
    timeline = await get_session_timeline(
        clinic_id=clinic_id,
        patient_pin=patient_pin,
        kb_table=request.app.state.dynamodb_kb_table,
    )
    return {"clinic_id": clinic_id, "patient_pin": patient_pin, "sessions": timeline}


@router.get("/clients/{clinic_id}/{patient_pin}/sessions/{session_id}")
async def client_session_detail(
    clinic_id: str, patient_pin: str, session_id: str, request: Request
):
    detail = await get_session_detail(
        clinic_id=clinic_id,
        patient_pin=patient_pin,
        session_id=session_id,
        kb_table=request.app.state.dynamodb_kb_table,
        s3_client=request.app.state.s3,
    )
    if detail is None:
        return JSONResponse(
            status_code=404,
            content={"detail": "Session not found for this client."},
        )
    return detail


@router.post("/clients/{clinic_id}/{patient_pin}/sessions")
async def submit_session(
    clinic_id: str, patient_pin: str, body: SessionSubmit, request: Request
):
    """Manually submit a session into the KB (for testing)."""
    import asyncio

    summary_dict = {
        "summary": body.summary,
        "bullet_points": body.bullet_points,
        "action_items": body.action_items,
        "participants": body.participants,
    }

    asyncio.create_task(
        index_session_and_update_brief(
            clinic_id=clinic_id,
            patient_pin=patient_pin,
            session_id=body.session_id,
            summary_dict=summary_dict,
            transcript_s3_key="",
            s3_client=request.app.state.s3,
        )
    )

    return {"status": "accepted", "session_id": body.session_id}
