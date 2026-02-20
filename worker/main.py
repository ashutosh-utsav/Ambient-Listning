import asyncio
import json
import logging
import signal
import traceback
import gc
import os
import aiohttp  # <— NEW
from datetime import datetime, timedelta
from azure.core.exceptions import ResourceExistsError
from azure.storage.queue.aio import QueueClient
from azure.storage.blob.aio import BlobServiceClient
from azure.data.tables.aio import TableServiceClient
from core.config import get_settings
from core.logging_config import setup_logging
from core.models import TranscriptionTask
from . import processor
from .session_manager import SessionManager
from .utils import log_memory_usage

setup_logging()
logger = logging.getLogger(__name__)
settings = get_settings()


class Worker:
    def __init__(self):
        self.session_manager: SessionManager | None = None

        self.queue_client: QueueClient | None = None
        self.dlq_client: QueueClient | None = None
        self.blob_service_client: BlobServiceClient | None = None
        self.table_service_client: TableServiceClient | None = None

        self._stop_event = asyncio.Event()
        self._worker_tasks: set[asyncio.Task] = set()

        self.concurrency = getattr(settings, "WORKER_CONCURRENCY", 8)
        self.semaphore = asyncio.Semaphore(self.concurrency)

        self.messages_per_page = int(getattr(settings, "WORKER_MESSAGES_PER_PAGE", 32))
        self.visibility_timeout = int(getattr(settings, "WORKER_VISIBILITY_TIMEOUT", 60))
        self.empty_backoff_max = int(getattr(settings, "WORKER_BACKOFF_MAX", 30))

        # Fallback default if not provided in config/env
        self.error_callback_url = getattr(
            settings,
            "webserver_error_callback",
            os.getenv("WEBSERVER_ERROR_CALLBACK", "http://localhost:8000/ws/error")
        )

    async def setup(self):
        logger.info("Worker setup starting...")
        self.queue_client = QueueClient.from_connection_string(
            conn_str=settings.azure_storage_connection_string,
            queue_name=settings.azure_queue_name
        )
        self.dlq_client = QueueClient.from_connection_string(
            conn_str=settings.azure_storage_connection_string,
            queue_name=settings.azure_dead_letter_queue_name
        )
        self.blob_service_client = BlobServiceClient.from_connection_string(
            conn_str=settings.azure_storage_connection_string
        )
        self.table_service_client = TableServiceClient.from_connection_string(
            conn_str=settings.azure_storage_connection_string
        )

        if getattr(settings, "ENVIRONMENT", "development") == "development":
            try:
                await self.queue_client.create_queue()
                logger.info(f"Queue '{settings.azure_queue_name}' created.")
            except ResourceExistsError:
                logger.info(f"Queue '{settings.azure_queue_name}' already exists.")
            except Exception:
                logger.exception("Failed creating main queue (dev).")

            try:
                await self.dlq_client.create_queue()
                logger.info(f"DLQ '{settings.azure_dead_letter_queue_name}' created.")
            except ResourceExistsError:
                logger.info(f"DLQ '{settings.azure_dead_letter_queue_name}' already exists.")
            except Exception:
                logger.exception("Failed creating DLQ (dev).")

            try:
                container_client = self.blob_service_client.get_container_client(settings.azure_blob_container_name)
                await container_client.create_container()
                logger.info(f"Blob container '{settings.azure_blob_container_name}' created.")
            except ResourceExistsError:
                logger.info(f"Blob container '{settings.azure_blob_container_name}' already exists.")
            except Exception:
                logger.exception("Failed creating blob container (dev).")

            try:
                table_client = self.table_service_client.get_table_client(table_name=settings.azure_table_name)
                await table_client.create_table()
                logger.info(f"Table '{settings.azure_table_name}' created.")
            except ResourceExistsError:
                logger.info(f"Table '{settings.azure_table_name}' already exists.")
            except Exception:
                logger.exception("Failed creating table (dev).")

        # Pass an error reporter callback down to sessions/processors
        self.session_manager = SessionManager(
            blob_service_client=self.blob_service_client,
            table_service_client=self.table_service_client,
            error_reporter=self.report_error,  # <— NEW
        )
        self.session_manager.start_janitor()
        
        logger.info("Worker setup complete.")
        log_memory_usage("Post-setup")

    async def shutdown(self):
        logger.info("Shutdown initiated.")
        self._stop_event.set()
        
        if self.session_manager:
            await self.session_manager.stop_janitor()
            await self.session_manager.cleanup_all_sessions()

        for task in list(self._worker_tasks):
            task.cancel()

        try:
            await asyncio.wait_for(asyncio.gather(*self._worker_tasks, return_exceptions=True), timeout=30)
        except asyncio.TimeoutError:
            logger.warning("Timeout while waiting for worker tasks to finish; continuing shutdown.")

        self._worker_tasks.clear()

        try:
            if self.blob_service_client:
                await self.blob_service_client.close()
                logger.info("BlobServiceClient closed.")
        except Exception:
            logger.exception("Exception while closing BlobServiceClient.")

        try:
            if self.table_service_client:
                await self.table_service_client.close()
                logger.info("TableServiceClient closed.")
        except Exception:
            logger.exception("Exception while closing TableServiceClient.")
        
        gc.collect()
        logger.info("Shutdown complete.")

    # -------- NEW: Report errors back to the web server so it can notify WS ----------
    async def report_error(self, session_id: str | None, code: str, message: str):
        """
        Sends an error event to the web_server which will forward to the websocket client.
        """
        try:
            payload = {
                "session_id": session_id or "",
                "error_code": code,
                "error_message": message,
            }
            async with aiohttp.ClientSession() as http:
                async with http.post(self.error_callback_url, json=payload) as resp:
                    if resp.status >= 300:
                        text = await resp.text()
                        logger.error(f"[{session_id}] Error callback failed ({resp.status}): {text}")
                    else:
                        logger.info(f"[{session_id}] Error reported upstream: {code} - {message}")
        except Exception:
            logger.exception(f"[{session_id}] FAILED to report error upstream.")

    async def handle_message(self, message):
        async with self.semaphore:
            message_id = getattr(message, "id", "<unknown>")
            logger.debug(f"Handling message {message_id}")

            task_obj = None
            try:
                task_obj = TranscriptionTask.model_validate_json(message.content)
            except Exception as e:
                logger.exception("Failed to parse message content to TranscriptionTask.")
                try:
                    await self._send_to_dlq(message.content)
                    await self._delete_message_safe(message)
                except Exception:
                    logger.exception("Failed moving unparsable message to DLQ.")
                # Also notify client (session unknown)
                await self.report_error(None, "TASK_PARSE_ERROR", str(e))
                return

            session_id = task_obj.session_id
            try:
                proc = self.session_manager.get_or_create_session(task_obj)

                if task_obj.ref_ids:
                    proc.ref_ids = task_obj.ref_ids
                
                if task_obj.action == "process":
                    await proc.reassemble_and_process_chunk(task_obj)
                elif task_obj.action == "finalize":
                    await self.session_manager.finalize_and_remove_session(session_id)
                elif task_obj.action == "error":
                    # Optionally route error-type tasks, if ever used
                    await self.report_error(session_id, task_obj.error_code or "WORKER_ERROR", task_obj.error_message or "Unknown worker error")
                else:
                    logger.warning(f"Unknown task action '{task_obj.action}' for message {message_id}")

                await self._delete_message_safe(message)
                logger.info(f"Task {message_id} processed and deleted successfully.")

            except Exception as e:
                logger.exception(f"Exception while processing message {message_id}.")
                # Notify client about processing failure
                await self.report_error(session_id, "WORKER_MESSAGE_EXCEPTION", str(e))
                dequeue_count = getattr(message, "dequeue_count", None)
                try:
                    if dequeue_count is not None and dequeue_count >= getattr(settings, "WORKER_DLQ_THRESHOLD", 3):
                        logger.warning(f"Moving message {message_id} to DLQ after {dequeue_count} attempts.")
                        await self._send_to_dlq(task_obj.model_dump_json() if task_obj else message.content)
                        await self._delete_message_safe(message)
                    else:
                        logger.info(f"Leaving message {message_id} in queue for retry (dequeue_count={dequeue_count}).")
                except Exception:
                    logger.exception("Failed handling DLQ logic for message.")
                    try:
                        await self._delete_message_safe(message)
                    except Exception:
                        logger.exception("Failed to delete problematic message; manual intervention required.")

    async def _send_to_dlq(self, content: str):
        if not self.dlq_client:
            logger.error("DLQ client not initialized; cannot send to DLQ.")
            return
        await self.dlq_client.send_message(content)
        logger.debug("Message sent to DLQ.")

    async def _delete_message_safe(self, message):
        try:
            try:
                await self.queue_client.delete_message(message)
            except TypeError:
                await self.queue_client.delete_message(message.id, message.pop_receipt)
            except Exception:
                await self.queue_client.delete_message(message.id, message.pop_receipt)
            logger.debug(f"Deleted message {getattr(message, 'id', '')}")
        except Exception:
            logger.exception("Failed to delete message from queue.")

    async def listen_for_tasks(self):
        logger.info("Worker listening for tasks...")
        backoff = 1

        while not self._stop_event.is_set():
            try:
                messages = self.queue_client.receive_messages(
                    messages_per_page=self.messages_per_page,
                    visibility_timeout=self.visibility_timeout
                )

                got_any = False
                async for message in messages:
                    got_any = True
                    task = asyncio.create_task(self.handle_message(message))
                    self._worker_tasks.add(task)
                    task.add_done_callback(lambda t: self._worker_tasks.discard(t))

                    if len(self._worker_tasks) >= self.concurrency * 4:
                        logger.debug("High number of in-flight tasks; yielding to event loop.")
                        await asyncio.sleep(0.1)

                if got_any:
                    backoff = 1
                else:
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, self.empty_backoff_max)

            except asyncio.CancelledError:
                logger.info("listen_for_tasks cancelled. Exiting loop.")
                break
            except Exception as e:
                logger.exception("Unhandled exception in listen_for_tasks loop; sleeping briefly before retry.")
                # Notify generic loop failure (no specific session)
                await self.report_error(None, "WORKER_LOOP_EXCEPTION", str(e))
                await asyncio.sleep(5)

    async def run(self):
        await self.setup()

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, lambda s=sig: asyncio.create_task(self._signal_handler(s)))
            except NotImplementedError:
                pass

        listener_task = asyncio.create_task(self.listen_for_tasks())
        try:
            await self._stop_event.wait()
        finally:
            listener_task.cancel()
            await self.shutdown()

    async def _signal_handler(self, sig):
        logger.info(f"Received signal {sig.name}; setting stop_event.")
        self._stop_event.set()


async def main():
    worker = Worker()
    try:
        await worker.run()
    except Exception:
        logger.exception("Fatal error running worker.")
    finally:
        try:
            await worker.shutdown()
        except Exception:
            logger.exception("Exception during final shutdown.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Worker interrupted by user; exiting.")
