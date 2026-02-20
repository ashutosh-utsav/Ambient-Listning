import logging
import os
import psutil

logger = logging.getLogger(__name__)

def log_memory_usage(context: str, session_id: str | None = None):
    """Logs the current memory usage of the worker process."""
    try:
        process = psutil.Process(os.getpid())
        rss_mb = process.memory_info().rss / (1024 * 1024)
        log_msg = f"[MEM_LOG] Context: {context} | Memory: {rss_mb:.2f} MB"
        if session_id:
            log_msg += f" | Session: {session_id}"
        logger.info(log_msg)
    except Exception as e:
        logger.error(f"Failed to log memory usage: {e}")