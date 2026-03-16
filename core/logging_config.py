import logging
import sys


def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - [%(name)s] - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )

    # Suppress noisy logs from AWS SDK.
    # (was: logging.getLogger("azure").setLevel(logging.WARNING))
    logging.getLogger("botocore").setLevel(logging.WARNING)
    logging.getLogger("aiobotocore").setLevel(logging.WARNING)
    logging.getLogger("boto3").setLevel(logging.WARNING)
    logging.getLogger("s3transfer").setLevel(logging.WARNING)

    try:
        memory_logger = logging.getLogger("memory_logger")
        memory_logger.setLevel(logging.INFO)
        memory_logger.propagate = False

        file_handler = logging.FileHandler("memory.log", mode="a")
        formatter = logging.Formatter("%(asctime)s,%(message)s", datefmt="%Y-%m-%d %H:%M:%S")
        file_handler.setFormatter(formatter)
        memory_logger.addHandler(file_handler)

        import os
        if os.path.getsize("memory.log") == 0:
            memory_logger.info("Context,SessionID,Memory_MB")

    except Exception as e:
        logging.getLogger(__name__).error(f"Failed to set up memory logger: {e}")
