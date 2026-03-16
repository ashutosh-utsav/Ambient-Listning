"""
MIGRATION NOTE: Azure Blob replaced with S3 via aioboto3.
Old Azure calls kept as comments for reference.
"""

import os
import openai
import aioboto3
from core.config import get_settings
from core.services import get_summary_from_text
from core.logging_config import setup_logging
import logging
import uuid
import json
from datetime import datetime

# from azure.storage.blob.aio import BlobServiceClient  # -- OLD --

setup_logging()
logger = logging.getLogger(__name__)

settings = get_settings()
client = openai.AsyncOpenAI(api_key=settings.openai_api_key)


async def transcribe_and_summarize_file(file_path: str, clinic_id: str, patient_pin: str):
    try:
        with open(file_path, "rb") as audio_file:
            transcript = await client.audio.transcriptions.create(
                model="whisper-1",
                file=audio_file,
                response_format="verbose_json",
            )

        summary = await get_summary_from_text(transcript.text)

        result = transcript.model_dump()
        result["summary"] = summary

        try:
            session_id = str(uuid.uuid4())
            date_str = datetime.now().strftime("%d%m%Y")

            audio_s3_key = f"audio/{clinic_id}/{patient_pin}/{date_str}/{session_id}/{session_id}.wav"
            transcript_s3_key = f"transcripts/{clinic_id}/{patient_pin}/{date_str}/{session_id}.json"

            logger.info(f"Uploading files to S3 for session {session_id}...")

            # -- AWS: aioboto3 S3 client --
            boto_session = aioboto3.Session(
                aws_access_key_id=settings.aws_access_key_id,
                aws_secret_access_key=settings.aws_secret_access_key,
                region_name=settings.aws_region,
            )
            async with boto_session.client("s3", endpoint_url=settings.s3_endpoint_url) as s3:
                with open(file_path, "rb") as data:
                    await s3.put_object(
                        Bucket=settings.s3_bucket_name,
                        Key=audio_s3_key,
                        Body=data.read(),
                    )
                logger.info(f"Successfully uploaded audio to: {audio_s3_key}")

                transcript_json_str = json.dumps(result, indent=2, ensure_ascii=False)
                await s3.put_object(
                    Bucket=settings.s3_bucket_name,
                    Key=transcript_s3_key,
                    Body=transcript_json_str.encode("utf-8"),
                    ContentType="application/json",
                )
                logger.info(f"Successfully uploaded transcript to: {transcript_s3_key}")

            # -- OLD Azure --
            # blob_service_client = BlobServiceClient.from_connection_string(settings.azure_storage_connection_string)
            # container_client = blob_service_client.get_container_client(settings.azure_blob_container_name)
            # with open(file_path, "rb") as data:
            #     await container_client.upload_blob(name=audio_blob_name, data=data, overwrite=True)
            # await container_client.upload_blob(name=transcript_blob_name, data=transcript_json_str, overwrite=True)
            # await blob_service_client.close()

        except Exception as e:
            logger.error(f"Failed to upload files to S3: {e}", exc_info=True)

        return result

    except Exception as e:
        logging.error(f"Error in transcribe_and_summarize_file: {e}", exc_info=True)
        raise e

    finally:
        if os.path.exists(file_path):
            os.remove(file_path)
