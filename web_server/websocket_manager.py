from fastapi import WebSocket, WebSocketDisconnect
from core.models import TranscriptionTask
import base64
import json                             # was missing from original
from core.logging_config import setup_logging
import logging
import math

setup_logging()
logger = logging.getLogger(__name__)

active_connections: dict[str, WebSocket] = {}
session_buffers = {}
session_batch_index: dict[str, int] = {}

AUDIO_BATCH_TARGET_SIZE = 16000 * 2 * 5
SAFE_PACKET_SIZE = 45 * 1024


async def handle_connection(
    websocket: WebSocket,
    session_id: str,
    arq_pool,                           # was: queue_client (Azure QueueClient)
    clinic_id: str,
    patient_pin: str,
    ref_ids: list[str] | None = None,
):
    active_connections[session_id] = websocket
    session_buffers[session_id] = bytearray()
    session_batch_index[session_id] = 0

    try:
        while True:
            msg = await websocket.receive()
            if msg["type"] in ("websocket.disconnect", "websocket.close"):
                raise WebSocketDisconnect

            if "text" in msg:
                try:
                    payload = json.loads(msg["text"])
                except Exception:
                    continue

                if payload.get("type") == "ACTION" and payload.get("action") == "END":
                    logger.info(f"[{session_id}] Received END action from client")
                    raise WebSocketDisconnect
                continue

            if "bytes" in msg:
                data = msg["bytes"]
            else:
                continue

            session_buffers[session_id].extend(data)

            if len(session_buffers[session_id]) >= AUDIO_BATCH_TARGET_SIZE:
                buffer_to_send = session_buffers[session_id]
                session_buffers[session_id] = bytearray()

                num_packets = math.ceil(len(buffer_to_send) / SAFE_PACKET_SIZE)
                current_batch_id = session_batch_index[session_id]

                logger.info(
                    f"[{session_id}] Buffer full. Batch {current_batch_id}. "
                    f"Splitting into {num_packets} packets."
                )

                for i in range(num_packets):
                    start = i * SAFE_PACKET_SIZE
                    end = start + SAFE_PACKET_SIZE
                    packet = buffer_to_send[start:end]
                    encoded_chunk = base64.b64encode(packet).decode("utf-8")

                    task = TranscriptionTask(
                        session_id=session_id,
                        action="process",
                        audio_chunk=encoded_chunk,
                        clinic_id=clinic_id,
                        patient_pin=patient_pin,
                        chunk_id=current_batch_id,
                        packet_index=i,
                        total_packets=num_packets,
                    )

                    # -- AWS: enqueue via ARQ/Redis --
                    await arq_pool.enqueue_job("process_task", task_data=task.model_dump())
                    # -- OLD: await queue_client.send_message(task.model_dump_json()) --

                session_batch_index[session_id] += 1

    except WebSocketDisconnect:
        if session_id in session_buffers and len(session_buffers[session_id]) > 0:
            buffer_to_send = session_buffers[session_id]
            num_packets = math.ceil(len(buffer_to_send) / SAFE_PACKET_SIZE)
            current_batch_id = session_batch_index[session_id]

            logger.info(
                f"[{session_id}] Sending final batch {current_batch_id} "
                f"({len(buffer_to_send)/1024:.2f} KB) in {num_packets} packets."
            )

            for i in range(num_packets):
                start = i * SAFE_PACKET_SIZE
                end = start + SAFE_PACKET_SIZE
                packet = buffer_to_send[start:end]
                encoded_chunk = base64.b64encode(packet).decode("utf-8")

                task = TranscriptionTask(
                    session_id=session_id,
                    action="process",
                    audio_chunk=encoded_chunk,
                    clinic_id=clinic_id,
                    patient_pin=patient_pin,
                    chunk_id=current_batch_id,
                    packet_index=i,
                    total_packets=num_packets,
                )

                # -- AWS: enqueue via ARQ/Redis --
                await arq_pool.enqueue_job("process_task", task_data=task.model_dump())
                # -- OLD: await queue_client.send_message(task.model_dump_json()) --

            session_batch_index[session_id] += 1

        final_task = TranscriptionTask(
            session_id=session_id,
            action="finalize",
            clinic_id=clinic_id,
            patient_pin=patient_pin,
            ref_ids=ref_ids,
        )

        # -- AWS: enqueue finalize via ARQ/Redis --
        await arq_pool.enqueue_job("process_task", task_data=final_task.model_dump())
        # -- OLD: await queue_client.send_message(final_task.model_dump_json()) --

    except Exception as e:
        logger.error(f"[{session_id}] Error in WebSocket connection: {e}", exc_info=True)

    finally:
        session_buffers.pop(session_id, None)
        session_batch_index.pop(session_id, None)
        active_connections.pop(session_id, None)
        logger.info(f"[{session_id}] Cleaned up buffer, batch index, and connection.")


async def send_error_to_websocket(session_id: str, error_code: str, error_message: str):
    ws = active_connections.get(session_id)
    if not ws:
        logger.warning(f"[{session_id}] No active websocket to deliver error: {error_code} - {error_message}")
        return

    try:
        await ws.send_json({
            "type": "error",
            "sessionId": session_id,
            "code": error_code,
            "message": error_message,
        })
        logger.info(f"[{session_id}] Sent error to websocket: {error_code}")
    except Exception:
        logger.exception(f"[{session_id}] Failed sending error to websocket.")
