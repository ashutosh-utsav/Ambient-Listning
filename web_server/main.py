import asyncio
import logging
import token
from fastapi import FastAPI, HTTPException, WebSocket, UploadFile, File, Form, Depends, Request, WebSocketException
from fastapi.middleware.cors import CORSMiddleware
import os
import uuid
from fastapi import status
from . import storage_handler
from . import upload_handler
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from . import websocket_manager  
from azure.storage.queue.aio import QueueClient
from azure.storage.blob.aio import BlobServiceClient
from azure.data.tables.aio import TableServiceClient
from core.logging_config import setup_logging
from core.config import get_settings
from contextlib import asynccontextmanager
from azure.core.exceptions import ResourceExistsError
from datetime import datetime
from core.auth import JWTMiddleware
from core.auth import get_current_user_from_websocket
from .websocket_manager import send_error_to_websocket
from openai import OpenAI, AsyncOpenAI

settings = get_settings()
client = AsyncOpenAI(api_key=settings.openai_api_key)

setup_logging()
settings = get_settings()

@asynccontextmanager
async def lifespan(app: FastAPI):
    queue_client = QueueClient.from_connection_string(
        conn_str=settings.azure_storage_connection_string,
        queue_name=settings.azure_queue_name
    )
    blob_service_client = BlobServiceClient.from_connection_string(
        conn_str=settings.azure_storage_connection_string
    )
    table_service_client = TableServiceClient.from_connection_string(
        conn_str=settings.azure_storage_connection_string
    )
    try:
        await queue_client.create_queue()
        logging.info(f"Queue '{settings.azure_queue_name}' created.")
    except ResourceExistsError:
        logging.info(f"Queue '{settings.azure_queue_name}' already exists.")
    except Exception as e:
        logging.error(f"Error connecting to Azure Queue Storage: {e}", exc_info=True)
        raise

    app.state.queue_client = queue_client
    app.state.blob_service_client = blob_service_client
    app.state.table_service_client = table_service_client
    logging.info("Successfully connected to Azure Storage services.")

    yield
    await app.state.queue_client.close()
    await app.state.blob_service_client.close()
    await app.state.table_service_client.close()
    logging.info("Azure Storage connections closed.")


app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  
    allow_credentials=True,
    allow_methods=["*"],  
    allow_headers=["*"],  
)

# app.add_middleware(JWTMiddleware)

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
        "services": {}
    }
    try:
        await app.state.queue_client.get_queue_properties()
        health_status["services"]["azure_queue"] = "healthy"
    except Exception as e:
        health_status["services"]["azure_queue"] = f"unhealthy: {str(e)}"
        health_status["status"] = "degraded"
    return health_status



@app.post("/results/{session_id}")
async def get_results(session_id: str, request: Request):
    result = await storage_handler.get_results_from_storage(
        session_id=session_id,
        table_service_client=request.app.state.table_service_client,
        blob_service_client=request.app.state.blob_service_client
    )

    if result.get("status") == "completed":
        return JSONResponse(status_code=200, content=result)

    if result.get("status") == "processing":
        return JSONResponse(status_code=202, content=result)

    return JSONResponse(status_code=200, content=result)




@app.post("/recover/{session_id}")
async def recover_session(session_id: str, request: Request):
    """
    Trigger a background recovery task for a failed session.
    """
    try:
        table_service_client = request.app.state.table_service_client
        blob_service_client = request.app.state.blob_service_client

        table_client = table_service_client.get_table_client(
            table_name=settings.azure_table_name
        )
        try:
            entity = await table_client.get_entity(
                partition_key=session_id,
                row_key=session_id
            )
        except Exception:
            return JSONResponse(
                status_code=404,
                content={"status": "not_found", "message": "Session not found in table"}
            )

        current_status = entity.get("Status", "Unknown")

        if current_status == "Completed":
            return JSONResponse(
                status_code=200,
                content={"status": "completed", "message": "Session already recovered."}
            )

        if current_status == "Recovering":
            return JSONResponse(
                status_code=200,
                content={"status": "recovering", "message": "Recovery already in progress."}
            )

        asyncio.create_task(
            storage_handler.recover_session_from_audio(
                session_id=session_id,
                table_service_client=table_service_client,
                blob_service_client=blob_service_client
            )
        )

        return JSONResponse(
            status_code=202,
            content={
                "status": "recovering",
                "message": "Recovery started in background. Check /results after a few minutes."
            }
        )

    except Exception as e:
        logger.exception(f"Error starting recovery for {session_id}")
        return JSONResponse(
            status_code=500,
            content={"status": "error", "message": f"Failed to start recovery: {str(e)}"}
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
        logging.error(f"WebSocket authentication error not helo not helo: {e}")
        return
    
    if auth_msg.get("type") != "AUTH" not in auth_msg:
        await websocket.send_json({"type": "auth-error", "message": "Invalid authentication message"})
        await websocket.close(code=1008, reason="Invalid authentication message")
        # logging.error(f"WebSocket authentication error hello hello: {e}")
        return

    token = auth_msg.get("token")
    if not token:
        await websocket.send_json({"type": "auth-error", "message": "No token provided in authentication message"})
        await websocket.close(code=1008, reason="Missing token")
        logging.error(f"WebSocket authentication error: Missing token")
        return
    
    try:
        user = await get_current_user_from_websocket(websocket, token)
        websocket.state.user = user
    
    except WebSocketException as e:

        reason = e.reason

        if reason == "Token has expired":
            await websocket.send_json({"type": "TOKEN_EXPIRIED", "message":" Token has expired"})
        else:
            await websocket.send_json({"type": "auth-error", "message": "Invalid token"})
        
        await websocket.close(code=1008, reason="Invalid token")
        logging.error(f"WebSocket authentication error: {e}")
        return


    except Exception as e:
        await websocket.send_json({"type": "auth-error", "message":" Invalid token"})
        await websocket.close(code=1008, reason="Invalid token")
        logging.error(f"WebSocket authentication error: {e}")
        return
    
    await websocket.send_json({"type": "auth-success"})

    alive = await is_openai_alive()
    if not alive:
        await websocket.send_json({"type": "service-error", "message": "OpenAI service is currently unavailable"})
        await websocket.close(code=1011, reason="OpenAI service unavailable")
        logging.error(f"WebSocket connection error: OpenAI service unavailable")
        return
    
    ref_ids = auth_msg.get("refID", []) or auth_msg.get("ref_ids", [])
    if not isinstance(ref_ids, list):
        ref_ids = []

    # "RefIds": ",".join(ref_ids),   # type: ignore

    clinic_id = websocket.query_params.get("clinicId")
    patient_pin = websocket.query_params.get("patientPin")

    if not (session_id and clinic_id and patient_pin):
        await websocket.send_json({"type": "auth-error", "message": "Missing session_id, clinicId, or patientPin"})
        await websocket.close(code=1008, reason="Missing session_id, clinicId, or patientPin")
        logging.error(f"WebSocket connection error: Missing session_id, clinicId, or patientPin")
        return

    try:
        table_client = websocket.app.state.table_service_client.get_table_client(
            settings.azure_table_name
        )

        await table_client.upsert_entity(
            {
                "PartitionKey": session_id,
                "RowKey": session_id,
                "Status": "Processing",
                "TranscriptBlobPath": "",
                "AudioBlobPath": "",
                "ErrorCode": "",
                "ErrorMessage": "",
                "ClinicId": clinic_id,
                "PatientPin": patient_pin,
                "RefIds": ",".join(ref_ids),
            },
            mode="merge",
        )

        logging.info(f"[{session_id}] Initial table entry created with Status=Processing")

    except Exception as e:

        logging.error(f"[{session_id}] Failed to create initial table entry: {e}", exc_info=True)
        pass

    await websocket_manager.handle_connection(
        websocket=websocket,
        session_id=session_id,
        queue_client=websocket.app.state.queue_client,
        clinic_id=clinic_id,
        patient_pin=patient_pin,
        ref_ids=ref_ids
    )


@app.post("/ws/error")
async def websocket_error_endpoint(payload: dict):
    """
    Worker posts here on any failure. We forward to the connected WebSocket (if present).
    Payload:
    {
      "session_id": "abc",
      "error_code": "TRANSCRIPTION_FAILED",
      "error_message": "Whisper failed after 3 retries"
    }
    """
    session_id = payload.get("session_id")
    error_code = payload.get("error_code") or "UNKNOWN"
    error_message = payload.get("error_message") or "An unknown error occurred."

    await send_error_to_websocket(session_id, error_code, error_message)
    return {"status": "sent"}
