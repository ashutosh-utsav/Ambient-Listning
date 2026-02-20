# """
# Debug Helper Utility
# This script is for development/debugging ONLY.
# It downloads a raw PCM append blob from Azure Storage and converts it
# into a playable WAV file by streaming the content, avoiding high memory usage.

# Requires:
# - azure-storage-blob
# - soundfile
# - numpy

# How to run:
# python -m worker.debug_helper <session_id> <clinic_id> <patient_pin> <date_str> <output_file.wav>
# e.g.
# python -m worker.debug_helper "session123" "clinicA" "patientB" "24102025" "session123.wav"
# """

# import asyncio
# import logging
# import soundfile as sf
# import numpy as np
# import sys
# import os

# # Add the project root to the path so we can import 'core'
# sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# try:
#     from azure.storage.blob.aio import BlobClient
#     from core.config import get_settings
#     from core.logging_config import setup_logging
# except ImportError:
#     print("Error: Could not import necessary modules.")
#     print("Please ensure you are in the correct environment and the project root is accessible.")
#     sys.exit(1)

# setup_logging()
# logger = logging.getLogger(__name__)

# # Constants from our app
# SAMPLING_RATE = 16000
# CHANNELS = 1
# SUBTYPE = 'PCM_16' # 16-bit PCM
# READ_CHUNK_SIZE = 1024 * 1024 # 1MB chunks

# async def save_pcm_blob_as_wav(session_id: str, clinic_id: str, patient_pin: str, date_str: str, output_filename: str):
#     """
#     Downloads the raw PCM blob and streams its conversion to a WAV file.
#     """
#     settings = get_settings()
    
#     # Construct the blob name exactly as in processor.py
#     blob_name = f"audio/{clinic_id}/{patient_pin}/{date_str}/{session_id}.pcm"
    
#     logger.info(f"Attempting to download blob: {blob_name}")

#     blob_client = BlobClient.from_connection_string(
#         conn_str=settings.azure_storage_connection_string,
#         container_name=settings.azure_blob_container_name,
#         blob_name=blob_name
#     )

#     try:
#         if not await blob_client.exists():
#             logger.error(f"Blob not found: {blob_name}")
#             return

#         logger.info(f"Blob found. Starting download and conversion to {output_filename}...")

#         # Use soundfile to write the WAV file chunk by chunk
#         with sf.SoundFile(output_filename, 'w', samplerate=SAMPLING_RATE, channels=CHANNELS, subtype=SUBTYPE) as wav_file:
#             # Get the blob download stream
#             stream = await blob_client.download_blob(max_concurrency=4)
            
#             # Read from the stream in chunks
#             async for chunk in stream.chunks(chunk_size=READ_CHUNK_SIZE):
#                 if chunk:
#                     # Convert raw bytes chunk to int16 numpy array
#                     audio_int16 = np.frombuffer(chunk, dtype=np.int16)
#                     # Write the audio data to the WAV file
#                     wav_file.write(audio_int16)
            
#         logger.info(f"Successfully created WAV file: {output_filename}")

#     except Exception as e:
#         logger.error(f"An error occurred: {e}", exc_info=True)
#     finally:
#         await blob_client.close()

# async def main():
#     if len(sys.argv) != 6:
#         print("Usage: python -m worker.debug_helper <session_id> <clinic_id> <patient_pin> <date_str> <output_file.wav>")
#         sys.exit(1)

#     session_id = sys.argv[1]
#     clinic_id = sys.argv[2]
#     patient_pin = sys.argv[3]
#     date_str = sys.argv[4]
#     output_filename = sys.argv[5]

#     await save_pcm_blob_as_wav(session_id, clinic_id, patient_pin, date_str, output_filename)

# if __name__ == "__main__":
#     asyncio.run(main())