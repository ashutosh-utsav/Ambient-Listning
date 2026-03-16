import asyncio
import logging
import gc
from datetime import datetime, timedelta
from . import processor
from worker.utils import log_memory_usage

logger = logging.getLogger(__name__)


class SessionManager:
    def __init__(
        self,
        s3_client,          # was: blob_service_client: BlobServiceClient
        dynamodb_table,     # was: table_service_client: TableServiceClient
        error_reporter,
    ):
        self.session_processors = {}
        self.s3_client = s3_client
        self.dynamodb_table = dynamodb_table
        self.error_reporter = error_reporter
        self.janitor_task = None
        self.memory_logger_task = None

    def get_or_create_session(self, task):
        session_id = task.session_id
        if session_id not in self.session_processors:
            logger.info(f"[{session_id}] Creating new session processor.")
            self.session_processors[session_id] = processor.SessionProcessor(
                session_id=session_id,
                clinic_id=task.clinic_id,
                patient_pin=task.patient_pin,
                s3_client=self.s3_client,           # was: blob_service_client=
                dynamodb_table=self.dynamodb_table, # was: table_service_client=
                error_reporter=self.error_reporter,
                ref_ids=task.ref_ids or [],
            )
            log_memory_usage("Session Created", session_id)
        return self.session_processors[session_id]

    async def finalize_and_remove_session(self, session_id: str):
        if session_id in self.session_processors:
            logger.info(f"[{session_id}] Finalizing and cleaning up session.")
            proc = self.session_processors[session_id]
            try:
                await proc.finalize_session()
            except Exception:
                logger.error(f"[{session_id}] An error occurred during finalization.", exc_info=True)
            finally:
                if session_id in self.session_processors:
                    del self.session_processors[session_id]
                del proc
                gc.collect()
                logger.info(f"[{session_id}] Session removed from memory and garbage collected.")
                log_memory_usage("Session Cleaned", session_id)

    async def _memory_logger(self):
        while True:
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                logger.info("Memory logger task cancelled.")
                raise
            except Exception:
                logger.exception("Error in memory logger task. Continuing...")

    async def _cleanup_stale_sessions(self):
        while True:
            try:
                await asyncio.sleep(60)
                stale_sessions = []
                now = datetime.now()

                for session_id, proc in list(self.session_processors.items()):
                    idle_minutes = (now - proc.last_updated_time).total_seconds() / 60
                    if idle_minutes > 30:
                        logger.warning(
                            f"[{session_id}] Session is stale (inactive for {idle_minutes:.1f} minutes). Marking for cleanup."
                        )
                        stale_sessions.append(session_id)

                if stale_sessions:
                    logger.info(f"Cleaning up {len(stale_sessions)} stale session(s)...")
                    for session_id in stale_sessions:
                        await self.finalize_and_remove_session(session_id)
                    gc.collect()
                    logger.info(f"Stale session cleanup complete. Active sessions: {len(self.session_processors)}")

            except asyncio.CancelledError:
                logger.info("Janitor task cancelled.")
                raise
            except Exception:
                logger.exception("Error in janitor task. Continuing...")

    def start_janitor(self):
        if self.janitor_task is None:
            self.janitor_task = asyncio.create_task(self._cleanup_stale_sessions())
            logger.info("Session janitor task started.")
        if self.memory_logger_task is None:
            self.memory_logger_task = asyncio.create_task(self._memory_logger())
            logger.info("Memory logger task started.")

    async def stop_janitor(self):
        if self.janitor_task:
            self.janitor_task.cancel()
            try:
                await self.janitor_task
            except asyncio.CancelledError:
                logger.info("Session janitor task stopped.")
            finally:
                self.janitor_task = None
        if self.memory_logger_task:
            self.memory_logger_task.cancel()
            try:
                await self.memory_logger_task
            except asyncio.CancelledError:
                logger.info("Memory logger task stopped.")
            finally:
                self.memory_logger_task = None

    async def cleanup_all_sessions(self):
        logger.info(f"Cleaning up all {len(self.session_processors)} active sessions...")
        session_ids = list(self.session_processors.keys())
        for session_id in session_ids:
            try:
                await self.finalize_and_remove_session(session_id)
            except Exception:
                logger.error(f"[{session_id}] Error during shutdown cleanup.", exc_info=True)
        gc.collect()
        logger.info("All sessions cleaned up.")
