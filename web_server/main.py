"""
MIGRATION NOTE: Azure clients replaced with aioboto3 (S3 + DynamoDB) and ARQ (Redis queue).
Old Azure calls kept as comments for reference.
"""

import asyncio
import logging
import json
import os
import uuid
import aioboto3
from botocore.exceptions import ClientError
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, WebSocket, UploadFile, File, Form, Request, WebSocketException
from fastapi.middleware.cors import CORSMiddleware
from fastapi import status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from openai import AsyncOpenAI

from arq import create_pool
from arq.connections import RedisSettings

# -- OLD Azure imports (kept for reference) --
# from azure.storage.queue.aio import QueueClient
# from azure.storage.blob.aio import BlobServiceClient
# from azure.data.tables.aio import TableServiceClient
# from azure.core.exceptions import ResourceExistsError
# from core.auth import JWTMiddleware, get_current_user_from_websocket

from core.logging_config import setup_logging
from core.config import get_settings
from core.auth import APIKeyMiddleware, verify_api_key
from core.aws_helpers import dynamo_upsert
from . import storage_handler
from . import upload_handler
from . import websocket_manager
from .websocket_manager import send_error_to_websocket
from .kb_router import router as kb_router

settings = get_settings()
client = AsyncOpenAI(api_key=settings.openai_api_key)

setup_logging()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # -- AWS: aioboto3 session for S3 and DynamoDB --
    boto_session = aioboto3.Session(
        aws_access_key_id=settings.aws_access_key_id,
        aws_secret_access_key=settings.aws_secret_access_key,
        region_name=settings.aws_region,
    )

    # -- OLD Azure client init --
    # queue_client = QueueClient.from_connection_string(conn_str=settings.azure_storage_connection_string, queue_name=settings.azure_queue_name)
    # blob_service_client = BlobServiceClient.from_connection_string(conn_str=settings.azure_storage_connection_string)
    # table_service_client = TableServiceClient.from_connection_string(conn_str=settings.azure_storage_connection_string)

    async with boto_session.client("s3", endpoint_url=settings.s3_endpoint_url) as s3:
        async with boto_session.resource("dynamodb", endpoint_url=settings.dynamodb_endpoint_url) as dynamodb:
            table = await dynamodb.Table(settings.dynamodb_table_name)

            # ARQ Redis pool
            arq_pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))

            kb_table = await dynamodb.Table(settings.kb_dynamodb_table_name)

            app.state.s3 = s3
            app.state.dynamodb_table = table
            app.state.dynamodb_kb_table = kb_table
            app.state.arq_pool = arq_pool

            logging.info("AWS services connected (S3, DynamoDB, Redis/ARQ).")

            yield

            await arq_pool.aclose()
            logging.info("Redis pool closed.")


app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# -- AWS: optional API key middleware (replaces JWT middleware) --
app.add_middleware(APIKeyMiddleware)
# -- OLD: app.add_middleware(JWTMiddleware) --

app.include_router(kb_router)

app.mount("/static", StaticFiles(directory="static"), name="static")

TEMP_UPLOAD_DIR = "uploads/temp"
os.makedirs(TEMP_UPLOAD_DIR, exist_ok=True)


@app.get("/", response_class=HTMLResponse)
async def get_root():
    with open("static/index.html", "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())


@app.get("/health")
async def health_check():
    return {"status": "healthy", "timestamp": datetime.now().isoformat()}


@app.get("/health/detailed")
async def detailed_health_check():
    health_status = {
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "services": {},
    }

    # -- AWS: Redis ping --
    try:
        await app.state.arq_pool.ping()
        health_status["services"]["redis"] = "healthy"
    except Exception as e:
        health_status["services"]["redis"] = f"unhealthy: {str(e)}"
        health_status["status"] = "degraded"

    # -- AWS: S3 bucket check --
    try:
        await app.state.s3.head_bucket(Bucket=settings.s3_bucket_name)
        health_status["services"]["s3"] = "healthy"
    except Exception as e:
        health_status["services"]["s3"] = f"unhealthy: {str(e)}"
        health_status["status"] = "degraded"

    # -- OLD Azure Queue health check --
    # await app.state.queue_client.get_queue_properties()
    # health_status["services"]["azure_queue"] = "healthy"

    return health_status


@app.post("/results/{session_id}")
async def get_results(session_id: str, request: Request):
    result = await storage_handler.get_results_from_storage(
        session_id=session_id,
        dynamodb_table=request.app.state.dynamodb_table,
        s3_client=request.app.state.s3,
        # -- OLD: table_service_client=..., blob_service_client=... --
    )

    if result.get("status") == "completed":
        return JSONResponse(status_code=200, content=result)
    if result.get("status") == "processing":
        return JSONResponse(status_code=202, content=result)
    return JSONResponse(status_code=200, content=result)


@app.post("/recover/{session_id}")
async def recover_session(session_id: str, request: Request):
    """Trigger a background recovery task for a failed session."""
    try:
        dynamodb_table = request.app.state.dynamodb_table
        s3_client = request.app.state.s3

        # -- AWS: DynamoDB get_item --
        response = await dynamodb_table.get_item(Key={"session_id": session_id})
        entity = response.get("Item")

        # -- OLD Azure --
        # table_client = table_service_client.get_table_client(table_name=settings.azure_table_name)
        # entity = await table_client.get_entity(partition_key=session_id, row_key=session_id)

        if not entity:
            return JSONResponse(
                status_code=404,
                content={"status": "not_found", "message": "Session not found"},
            )

        current_status = entity.get("Status", "Unknown")

        if current_status == "Completed":
            return JSONResponse(status_code=200, content={"status": "completed", "message": "Session already completed."})
        if current_status == "Recovering":
            return JSONResponse(status_code=200, content={"status": "recovering", "message": "Recovery already in progress."})

        asyncio.create_task(
            storage_handler.recover_session_from_audio(
                session_id=session_id,
                dynamodb_table=dynamodb_table,
                s3_client=s3_client,
            )
        )

        return JSONResponse(
            status_code=202,
            content={
                "status": "recovering",
                "message": "Recovery started in background. Check /results after a few minutes.",
            },
        )

    except Exception as e:
        logging.exception(f"Error starting recovery for {session_id}")
        return JSONResponse(
            status_code=500,
            content={"status": "error", "message": f"Failed to start recovery: {str(e)}"},
        )


@app.post("/upload")
async def handle_file_upload(
    file: UploadFile = File(...),
    clinic_id: str = Form(...),
    patient_pin: str = Form(...),
):
    if not file.content_type.startswith("audio/"):
        logging.error("Invalid file type uploaded.")
        return JSONResponse(status_code=400, content={"message": "Invalid file type. Please upload an audio file."})

    temp_filename = f"{uuid.uuid4()}-{file.filename}"
    temp_filepath = os.path.join(TEMP_UPLOAD_DIR, temp_filename)

    with open(temp_filepath, "wb") as f:
        content = await file.read()
        f.write(content)

    result_data = await upload_handler.transcribe_and_summarize_file(temp_filepath, clinic_id, patient_pin)
    return JSONResponse(content=result_data)


@app.get("/testing")
async def testing_endpoint():
    return {"message": "This is a test endpoint"}


async def is_openai_alive() -> bool:
    try:
        await client.models.list()
        return True
    except Exception as e:
        logging.error(f"OpenAI API health check failed: {e}")
        return True


@app.websocket("/ws/{session_id}")
async def websocket_endpoint(websocket: WebSocket, session_id: str):
    await websocket.accept()

    try:
        auth_msg = await websocket.receive_json()
    except Exception as e:
        await websocket.send_json({"type": "auth-error", "message": "Failed to receive authentication message"})
        await websocket.close(code=1008, reason="Failed to receive authentication message")
        logging.error(f"WebSocket auth error: {e}")
        return

    if auth_msg.get("type") != "AUTH":
        await websocket.send_json({"type": "auth-error", "message": "Invalid authentication message"})
        await websocket.close(code=1008, reason="Invalid authentication message")
        return

    # -- AWS: API key auth (replaces JWT token validation) --
    if settings.enable_api_key_auth:
        provided_key = auth_msg.get("api_key") or auth_msg.get("token")
        if not verify_api_key(provided_key):
            await websocket.send_json({"type": "auth-error", "message": "Invalid API key"})
            await websocket.close(code=1008, reason="Invalid API key")
            logging.warning(f"WebSocket rejected: invalid API key for session {session_id}")
            return
    # else: auth disabled — accept all connections

    # -- OLD JWT token validation (kept for reference) --
    # token = auth_msg.get("token")
    # if not token:
    #     await websocket.send_json({"type": "auth-error", "message": "No token provided"})
    #     await websocket.close(code=1008, reason="Missing token")
    #     return
    # try:
    #     user = await get_current_user_from_websocket(websocket, token)
    #     websocket.state.user = user
    # except WebSocketException as e:
    #     reason = e.reason
    #     if reason == "Token has expired":
    #         await websocket.send_json({"type": "TOKEN_EXPIRIED", "message": "Token has expired"})
    #     else:
    #         await websocket.send_json({"type": "auth-error", "message": "Invalid token"})
    #     await websocket.close(code=1008, reason="Invalid token")
    #     return

    await websocket.send_json({"type": "auth-success"})

    alive = await is_openai_alive()
    if not alive:
        await websocket.send_json({"type": "service-error", "message": "OpenAI service is currently unavailable"})
        await websocket.close(code=1011, reason="OpenAI service unavailable")
        return

    ref_ids = auth_msg.get("refID", []) or auth_msg.get("ref_ids", [])
    if not isinstance(ref_ids, list):
        ref_ids = []

    clinic_id = websocket.query_params.get("clinicId")
    patient_pin = websocket.query_params.get("patientPin")

    if not (session_id and clinic_id and patient_pin):
        await websocket.send_json({"type": "auth-error", "message": "Missing session_id, clinicId, or patientPin"})
        await websocket.close(code=1008, reason="Missing required params")
        return

    try:
        # -- AWS: DynamoDB upsert (replaces Azure Table upsert_entity) --
        await dynamo_upsert(websocket.app.state.dynamodb_table, session_id, {
            "Status": "Processing",
            "TranscriptBlobPath": "",
            "AudioBlobPath": "",
            "ErrorCode": "",
            "ErrorMessage": "",
            "ClinicId": clinic_id,
            "PatientPin": patient_pin,
            "RefIds": ",".join(ref_ids),
        })
        # -- OLD Azure Table --
        # table_client = websocket.app.state.table_service_client.get_table_client(settings.azure_table_name)
        # await table_client.upsert_entity({"PartitionKey": session_id, "RowKey": session_id, ...}, mode="merge")

        logging.info(f"[{session_id}] Initial DynamoDB entry created with Status=Processing")
    except Exception as e:
        logging.error(f"[{session_id}] Failed to create initial DynamoDB entry: {e}", exc_info=True)

    await websocket_manager.handle_connection(
        websocket=websocket,
        session_id=session_id,
        arq_pool=websocket.app.state.arq_pool,  # was: queue_client=websocket.app.state.queue_client
        clinic_id=clinic_id,
        patient_pin=patient_pin,
        ref_ids=ref_ids,
    )


@app.post("/ws/error")
async def websocket_error_endpoint(payload: dict):
    """Worker posts here on failure. Forwards to connected WebSocket."""
    session_id = payload.get("session_id")
    error_code = payload.get("error_code") or "UNKNOWN"
    error_message = payload.get("error_message") or "An unknown error occurred."

    await send_error_to_websocket(session_id, error_code, error_message)
    return {"status": "sent"}
