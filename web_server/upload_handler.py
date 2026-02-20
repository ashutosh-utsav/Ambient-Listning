import os
import openai
from core.config import get_settings
from core.services import get_summary_from_text
from core.logging_config import setup_logging
import logging
import uuid
import json
from datetime import datetime
from azure.storage.blob.aio import BlobServiceClient

setup_logging()
logger = logging.getLogger(__name__)

settings = get_settings()
client = openai.AsyncOpenAI(api_key=settings.openai_api_key)

"""
This function will handle the entire process of transcribing and summarizing an uploaded audio file.
This will call the above function to get the summary after doing the transcription using openai whisper API
Also we need to see how will we doing the error handling and edge cases
"""
async def transcribe_and_summarize_file(file_path: str, clinic_id: str, patient_pin: str):
    try:
        with open(file_path, "rb") as audio_file:
            transcript = await client.audio.transcriptions.create(
                model="whisper-1",
                file=audio_file,
                response_format="verbose_json"
            )
        ##logging.info("Transcription successful.")
        
        summary = await get_summary_from_text(transcript.text)
        
        result = transcript.model_dump()
        result["summary"] = summary

        try:
            session_id = str(uuid.uuid4())
            date_str = datetime.now().strftime("%d%m%Y")

            audio_blob_name = f"audio/{clinic_id}/{patient_pin}/{date_str}/{session_id}/{session_id}.wav"
            transcript_blob_name = f"transcripts/{clinic_id}/{patient_pin}/{date_str}/{session_id}.json"

            logger.info(f"Uploading files to Azure Blob Storage for session {session_id}...")

            blob_service_client = BlobServiceClient.from_connection_string(settings.azure_storage_connection_string)
            container_client = blob_service_client.get_container_client(settings.azure_blob_container_name)

            with open(file_path, "rb") as data:
                await container_client.upload_blob(name=audio_blob_name, data=data, overwrite=True)
            logger.info(f"Successfully uploaded audio to: {audio_blob_name}")

            transcript_json_str = json.dumps(result, indent=2, ensure_ascii=False)
            await container_client.upload_blob(name=transcript_blob_name, data=transcript_json_str, overwrite=True)
            logger.info(f"Successfully uploaded transcript to: {transcript_blob_name}")

            await blob_service_client.close()

        except Exception as e:
            logger.error(f"Failed to upload files to Azure Blob Storage: {e}", exc_info=True)

        return result
    except Exception as e:
        logging.error(f"Error in transcribe_and_summarize_file: {e}", exc_info=True)
        raise e

    finally:
        if os.path.exists(file_path):
            os.remove(file_path)