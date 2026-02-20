import logging
import sys

def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - [%(name)s] - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )

    # Suppress noisy logs from the Azure SDK.
    logging.getLogger("azure").setLevel(logging.WARNING)

    # --- CHANGE: This is the new setup for our dedicated memory logger. ---
    try:
        # 1. Create a new, named logger.
        memory_logger = logging.getLogger("memory_logger")
        memory_logger.setLevel(logging.INFO)
        memory_logger.propagate = False # Prevent messages from going to the main logger.

        # 2. Create a handler that writes to a 'memory.log' file.
        file_handler = logging.FileHandler("memory.log", mode='a')

        # 3. Create a simple, clean format for easy analysis.
        formatter = logging.Formatter("%(asctime)s,%(message)s", datefmt="%Y-%m-%d %H:%M:%S")
        file_handler.setFormatter(formatter)

        # 4. Add the handler to our new logger.
        memory_logger.addHandler(file_handler)
        
        # Add a header if the file is new/empty
        import os
        if os.path.getsize("memory.log") == 0:
            memory_logger.info("Context,SessionID,Memory_MB")

    except Exception as e:
        logging.getLogger(__name__).error(f"Failed to set up memory logger: {e}")
    # --- END OF CHANGE ---