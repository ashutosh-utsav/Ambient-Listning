"""
Worker entry point.

MIGRATION NOTE: The custom Worker class with Azure Queue polling is replaced by ARQ.
ARQ handles queue polling, retries, and failed job tracking automatically.

IMPORTANT: Run exactly ONE worker process. SessionManager holds live audio state
in-memory (transcription buffers, packet reassembly). Multiple worker processes
would split session state across processes and silently lose audio.

To run:
    python -m worker.main
    # or directly via arq CLI:
    arq worker.main.WorkerSettings
"""

import asyncio
import json
import logging
import gc
import os
import aiohttp
import aioboto3
from botocore.exceptions import ClientError

# -- OLD Azure imports (kept for reference) --
# from azure.core.exceptions import ResourceExistsError
# from azure.storage.queue.aio import QueueClient
# from azure.storage.blob.aio import BlobServiceClient
# from azure.data.tables.aio import TableServiceClient

from arq import run_worker
from arq.connections import RedisSettings

from core.config import get_settings
from core.logging_config import setup_logging
from core.models import TranscriptionTask
from . import processor
from .session_manager import SessionManager
from .utils import log_memory_usage

setup_logging()
logger = logging.getLogger(__name__)
settings = get_settings()


async def _report_error(url: str, session_id: str | None, code: str, message: str):
    """Send error event to web_server for WebSocket forwarding."""
    try:
        payload = {
            "session_id": session_id or "",
            "error_code": code,
            "error_message": message,
        }
        async with aiohttp.ClientSession() as http:
            async with http.post(url, json=payload) as resp:
                if resp.status >= 300:
                    text = await resp.text()
                    logger.error(f"[{session_id}] Error callback failed ({resp.status}): {text}")
                else:
                    logger.info(f"[{session_id}] Error reported upstream: {code} - {message}")
    except Exception:
        logger.exception(f"[{session_id}] FAILED to report error upstream.")


async def process_task(ctx: dict, task_data: dict):
    """
    ARQ job function — processes a single TranscriptionTask.

    Replaces Worker.handle_message. ctx is shared across all jobs in this
    worker process (holds SessionManager, report_error, AWS clients).
    """
    session_manager: SessionManager = ctx["session_manager"]
    report_error = ctx["report_error"]

    try:
        task = TranscriptionTask(**task_data)
    except Exception as e:
        logger.exception("Failed to parse task_data to TranscriptionTask.")
        await report_error(None, "TASK_PARSE_ERROR", str(e))
        raise  # ARQ will retry / move to failed jobs

    session_id = task.session_id
    try:
        proc = session_manager.get_or_create_session(task)

        if task.ref_ids:
            proc.ref_ids = task.ref_ids

        if task.action == "process":
            await proc.reassemble_and_process_chunk(task)
        elif task.action == "finalize":
            await session_manager.finalize_and_remove_session(session_id)
        elif task.action == "error":
            await report_error(
                session_id,
                task.error_code or "WORKER_ERROR",
                task.error_message or "Unknown worker error",
            )
        else:
            logger.warning(f"[{session_id}] Unknown task action: '{task.action}'")

    except Exception as e:
        logger.exception(f"[{session_id}] Exception while processing task.")
        await report_error(session_id, "WORKER_MESSAGE_EXCEPTION", str(e))
        raise  # ARQ retries up to max_tries, then moves to failed jobs list


async def startup(ctx: dict):
    """
    ARQ startup hook — runs once when the worker process starts.
    Replaces Worker.setup().
    """
    logger.info("Worker startup...")

    error_callback_url = getattr(
        settings,
        "webserver_error_callback",
        os.getenv("WEBSERVER_ERROR_CALLBACK", "http://localhost:8000/ws/error"),
    )

    async def report_error(session_id, code, message):
        await _report_error(error_callback_url, session_id, code, message)

    ctx["report_error"] = report_error

    # -- AWS: Create aioboto3 clients --
    boto_session = aioboto3.Session(
        aws_access_key_id=settings.aws_access_key_id,
        aws_secret_access_key=settings.aws_secret_access_key,
        region_name=settings.aws_region,
    )

    s3 = await boto_session.client("s3", endpoint_url=settings.s3_endpoint_url).__aenter__()
    dynamodb = await boto_session.resource("dynamodb", endpoint_url=settings.dynamodb_endpoint_url).__aenter__()
    table = await dynamodb.Table(settings.dynamodb_table_name)

    # -- OLD Azure client init --
    # queue_client = QueueClient.from_connection_string(conn_str=settings.azure_storage_connection_string, ...)
    # blob_service_client = BlobServiceClient.from_connection_string(...)
    # table_service_client = TableServiceClient.from_connection_string(...)

    ctx["s3"] = s3
    ctx["dynamodb"] = dynamodb
    ctx["dynamodb_table"] = table

    session_manager = SessionManager(
        s3_client=s3,
        dynamodb_table=table,
        error_reporter=report_error,
    )
    session_manager.start_janitor()
    ctx["session_manager"] = session_manager

    log_memory_usage("Post-setup")
    logger.info("Worker startup complete.")


async def shutdown(ctx: dict):
    """
    ARQ shutdown hook — runs once when the worker process stops.
    Replaces Worker.shutdown().
    """
    logger.info("Worker shutdown initiated.")

    session_manager: SessionManager = ctx.get("session_manager")
    if session_manager:
        await session_manager.stop_janitor()
        await session_manager.cleanup_all_sessions()

    if ctx.get("s3"):
        await ctx["s3"].__aexit__(None, None, None)
    if ctx.get("dynamodb"):
        await ctx["dynamodb"].__aexit__(None, None, None)

    gc.collect()
    logger.info("Worker shutdown complete.")


class WorkerSettings:
    """
    ARQ WorkerSettings — replaces the custom Worker class.

    ARQ handles queue polling, retries, and failed job tracking automatically.
    Failed jobs (after max_tries) are stored in Redis — equivalent to the old DLQ.
    Inspect them with: arq worker.main.WorkerSettings --watch
    """
    functions = [process_task]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    max_jobs = 8            # replaces WORKER_CONCURRENCY=8
    job_timeout = 300       # 5 min max per job
    keep_result = 86400     # keep job results in Redis for 24h


if __name__ == "__main__":
    run_worker(WorkerSettings)
