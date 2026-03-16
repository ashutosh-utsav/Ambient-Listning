"""
MIGRATION NOTE: Azure Blob + Table replaced with S3 + DynamoDB.
Old Azure calls kept as comments for reference.

Function signatures changed:
  blob_service_client: BlobServiceClient  →  s3_client
  table_service_client: TableServiceClient →  dynamodb_table
"""

import asyncio
import logging
import json
from urllib.parse import unquote
from botocore.exceptions import ClientError

# from azure.data.tables.aio import TableServiceClient  # -- OLD --
# from azure.storage.blob.aio import BlobServiceClient  # -- OLD --
# from azure.core.exceptions import ResourceNotFoundError  # -- OLD --

import io
import random
from typing import Optional, Dict, List

from core.config import get_settings
from core.aws_helpers import dynamo_upsert
from core.services import get_summary_from_text
from openai import AsyncOpenAI

settings = get_settings()
client = AsyncOpenAI(api_key=settings.openai_api_key)
logger = logging.getLogger(__name__)


async def _s3_object_exists(s3_client, key: str) -> bool:
    """Check if an S3 object exists. Replaces Azure blob_client.exists()."""
    try:
        await s3_client.head_object(Bucket=settings.s3_bucket_name, Key=key)
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] in ("404", "NoSuchKey"):
            return False
        raise


async def list_wav_files_in_session_folder(s3_client, session_folder_prefix: str) -> List[str]:
    """
    Return sorted list of .wav S3 keys directly under session_folder_prefix.
    session_folder_prefix must end with a trailing slash.
    Replaces Azure container.list_blobs(name_starts_with=prefix).
    """
    results = []

    # -- AWS: S3 paginator for list_objects_v2 --
    paginator = s3_client.get_paginator("list_objects_v2")
    async for page in paginator.paginate(Bucket=settings.s3_bucket_name, Prefix=session_folder_prefix):
        for obj in page.get("Contents", []):
            name = obj["Key"]
            if not name.lower().endswith(".wav"):
                continue
            relative = name[len(session_folder_prefix):]
            if "/" in relative:
                continue
            results.append(name)

    # -- OLD Azure --
    # container = blob_service_client.get_container_client(container=settings.azure_blob_container_name)
    # async for blob in container.list_blobs(name_starts_with=session_folder_prefix):
    #     name = blob.name
    #     if not name.lower().endswith(".wav"):
    #         continue
    #     relative = name[len(session_folder_prefix):]
    #     if "/" in relative:
    #         continue
    #     results.append(name)

    return sorted(results)


async def estimate_recovery_time(s3_client, audio_path: str, session_id: str) -> dict:
    """
    Estimate recovery time for a specific session's audio folder.
    Each .wav file ≈ 25s to transcribe + 30s total summarization time.
    Replaces Azure blob listing for estimation.
    """
    try:
        if not audio_path:
            return {"status": "fatal_error", "message": "No audio path found for this session."}

        session_id = unquote(session_id or "")
        if not session_id:
            logger.error("[ESTIMATE] Missing session_id.")
            return {"status": "fatal_error", "message": "Session ID missing."}

        if audio_path.lower().endswith(".wav"):
            audio_path = audio_path.rsplit("/", 1)[0] + "/"
        if not audio_path.endswith("/"):
            audio_path += "/"

        logger.info(f"[{session_id}] Estimating recovery time for prefix: {audio_path}")

        # -- AWS: S3 paginator --
        found_keys = []
        paginator = s3_client.get_paginator("list_objects_v2")
        async for page in paginator.paginate(Bucket=settings.s3_bucket_name, Prefix=audio_path):
            for obj in page.get("Contents", []):
                if obj["Key"].lower().endswith(".wav"):
                    found_keys.append(obj["Key"])

        # -- OLD Azure --
        # container = blob_service_client.get_container_client(container=settings.azure_blob_container_name)
        # async for blob in container.list_blobs(name_starts_with=audio_path):
        #     if blob.name.lower().endswith(".wav"):
        #         found_keys.append(blob.name)

        logger.info(f"[DEBUG] Total .wav keys found under {audio_path}: {len(found_keys)}")

        count = 0
        session_prefix = f"{audio_path}{session_id}/"
        for key in found_keys:
            if not key.startswith(session_prefix):
                continue
            relative_path = key[len(session_prefix):]
            if "/" in relative_path:
                continue
            count += 1

        if count == 0:
            logger.error(f"[ESTIMATE] No audio files found for session {session_id}")
            return {"status": "fatal_error", "message": "No audio files found for this session."}

        total_seconds = count * 25 + 30
        estimated_minutes = round(total_seconds / 60, 2)

        return {
            "status": "error",
            "errorCode": "RECOVERY_POSSIBLE",
            "message": f"Found {count} audio chunks for recovery.",
            "audio_chunks": count,
            "estimated_minutes": estimated_minutes,
        }

    except Exception as e:
        logger.error(f"[ESTIMATE] Error estimating recovery time: {e}", exc_info=True)
        return {
            "status": "error",
            "errorCode": "ESTIMATION_FAILED",
            "message": f"Failed to estimate recovery time: {str(e)}",
        }


async def get_results_from_storage(session_id: str, dynamodb_table, s3_client):
    """
    Replaces: get_results_from_storage(session_id, table_service_client, blob_service_client)
    """
    try:
        # -- AWS: DynamoDB get_item --
        response = await dynamodb_table.get_item(Key={"session_id": session_id})
        entity = response.get("Item")

        # -- OLD Azure --
        # table_client = table_service_client.get_table_client(table_name=settings.azure_table_name)
        # entity = await table_client.get_entity(partition_key=session_id, row_key=session_id)
        # except ResourceNotFoundError: return {"status": "not_found"}

        if not entity:
            return {"status": "not_found"}

        status_value = entity.get("Status", "Processing")

        if status_value == "Processing":
            return {"status": "processing"}

        if status_value == "Error":
            audio_path = entity.get("AudioBlobPath", "")
            if not audio_path:
                return {"status": "fatal_error", "message": "No audio path found. Cannot estimate recovery."}

            prefix = audio_path.rsplit("/", 1)[0] + "/"
            estimate_result = await estimate_recovery_time(s3_client, prefix, session_id=session_id)
            return estimate_result

        if status_value == "Completed":
            transcript_path = entity.get("TranscriptBlobPath")
            if not transcript_path:
                return {
                    "status": "error",
                    "errorCode": "BLOB_PATH_MISSING",
                    "errorMessage": "TranscriptBlobPath is missing in table",
                }

            # -- AWS: S3 exists check + download --
            if not await _s3_object_exists(s3_client, transcript_path):
                return {
                    "status": "error",
                    "errorCode": "TRANSCRIPT_NOT_FOUND",
                    "errorMessage": "Transcript not found in S3",
                }

            response = await s3_client.get_object(Bucket=settings.s3_bucket_name, Key=transcript_path)
            data = await response["Body"].read()
            transcript_json = json.loads(data)

            # -- OLD Azure --
            # blob_client = blob_service_client.get_blob_client(container=settings.azure_blob_container_name, blob=transcript_path)
            # if not await blob_client.exists(): ...
            # stream = await blob_client.download_blob()
            # data = await stream.readall()

            return {
                "status": "completed",
                "summary": transcript_json.get("summary"),
            }

        return {
            "status": f"{status_value}",
            "errorCode": f"{status_value}",
            "errorMessage": f"Unknown status value '{status_value}'",
        }

    except Exception as e:
        logger.error(f"Error in get_results_from_storage for {session_id}", exc_info=True)
        return {
            "status": "error",
            "errorCode": "INTERNAL_EXCEPTION",
            "errorMessage": str(e),
        }


async def transcribe_wav_bytes(
    wav_bytes: bytes,
    filename: str,
    session_id: str,
    retries: int = 3,
) -> Optional[Dict]:
    """Transcribes a single WAV chunk via OpenAI. No storage dependency — unchanged."""
    attempt = 0
    file_obj = None
    try:
        while attempt < retries:
            try:
                file_obj = io.BytesIO(wav_bytes)
                file_obj.name = filename

                logger.info(f"[{session_id}] Transcribing {filename} (Attempt {attempt + 1}/{retries})")

                transcription = await client.audio.transcriptions.create(
                    model="gpt-4o-transcribe",
                    file=file_obj,
                    response_format="json",
                )

                if hasattr(transcription, "model_dump"):
                    transcription = transcription.model_dump()

                text = (
                    transcription.get("text")
                    if isinstance(transcription, dict)
                    else getattr(transcription, "text", "")
                )
                language = (
                    transcription.get("language")
                    if isinstance(transcription, dict)
                    else getattr(transcription, "language", "unknown")
                )
                segments_raw = (
                    transcription.get("segments")
                    if isinstance(transcription, dict)
                    else getattr(transcription, "segments", None)
                )
                segments = []
                if segments_raw and isinstance(segments_raw, list):
                    for seg in segments_raw:
                        if isinstance(seg, dict):
                            segments.append({
                                "start": seg.get("start", 0.0),
                                "end": seg.get("end", 0.0),
                                "text": seg.get("text", ""),
                            })
                else:
                    logger.warning(f"[{session_id}] No segments returned for {filename}")

                if not text:
                    logger.warning(f"[{session_id}] Empty transcription for {filename}")
                    return None

                return {"text": text, "language": language or "unknown", "segments": segments}

            except Exception as e:
                attempt += 1
                logger.error(f"[{session_id}] Transcription error for {filename} (attempt {attempt}): {e}")
                if attempt < retries:
                    await asyncio.sleep(2 ** attempt + random.random())
                else:
                    return None
    finally:
        if file_obj:
            file_obj.close()


async def recover_session_from_audio(session_id: str, dynamodb_table, s3_client) -> dict:
    """
    Re-transcribe audio blobs to recover a failed session.
    Replaces: recover_session_from_audio(session_id, table_service_client, blob_service_client)
    """
    try:
        logger.info(f"[{session_id}] Recovery requested.")

        # -- AWS: DynamoDB get_item --
        response = await dynamodb_table.get_item(Key={"session_id": session_id})
        entity = response.get("Item")

        # -- OLD Azure --
        # table_client = table_service_client.get_table_client(table_name=settings.azure_table_name)
        # entity = await table_client.get_entity(partition_key=session_id, row_key=session_id)
        # except ResourceNotFoundError: return {"status": "not_found"}

        if not entity:
            logger.error(f"[{session_id}] Table entity not found for recovery.")
            return {"status": "not_found"}

        status_value = entity.get("Status", "Processing")
        if status_value == "Completed":
            return {"status": "already_completed"}
        if status_value == "Recovering":
            return {"status": "already_recovering"}

        clinic_id = entity.get("ClinicId")
        patient_pin = entity.get("PatientPin")
        audio_blob_path = entity.get("AudioBlobPath", "")

        if not audio_blob_path:
            logger.error(f"[{session_id}] No AudioBlobPath in table. Cannot recover.")
            await dynamo_upsert(dynamodb_table, session_id, {
                "Status": "Error",
                "ErrorCode": "AUDIO_PATH_MISSING",
                "ErrorMessage": "AudioBlobPath missing - cannot recover",
            })
            return {"status": "fatal_error", "message": "No audio path in table for this session."}

        await dynamo_upsert(dynamodb_table, session_id, {
            "Status": "Recovering",
            "ErrorCode": "",
            "ErrorMessage": "",
        })

        prefix = audio_blob_path
        if prefix.endswith(".wav"):
            prefix = prefix.rsplit("/", 1)[0] + "/"
        if not prefix.endswith("/"):
            prefix = prefix + "/"

        segments = prefix.strip("/").split("/")
        last_segment = segments[-1] if segments else ""
        session_prefix = prefix if last_segment == session_id else prefix.rstrip("/") + f"/{session_id}/"

        wavs = await list_wav_files_in_session_folder(s3_client, session_prefix)
        if not wavs:
            wavs = await list_wav_files_in_session_folder(s3_client, prefix)

        if not wavs:
            logger.error(f"[{session_id}] No audio files found under prefix {session_prefix} or {prefix}")
            await dynamo_upsert(dynamodb_table, session_id, {
                "Status": "Error",
                "ErrorCode": "AUDIO_NOT_FOUND",
                "ErrorMessage": "No audio blobs found for this session - cannot recover",
            })
            return {"status": "fatal_error", "message": "No audio files found for this session."}

        logger.info(f"[{session_id}] Found {len(wavs)} wavs to transcribe.")

        texts = []
        combined_segments = []
        for idx, key in enumerate(wavs):
            try:
                # -- AWS: S3 get_object --
                s3_resp = await s3_client.get_object(Bucket=settings.s3_bucket_name, Key=key)
                wav_bytes = await s3_resp["Body"].read()

                # -- OLD Azure --
                # blob_client = blob_service_client.get_blob_client(container=settings.azure_blob_container_name, blob=key)
                # stream = await blob_client.download_blob()
                # wav_bytes = await stream.readall()

                filename = key.split("/")[-1] or f"chunk_{idx}.wav"
                result = await transcribe_wav_bytes(wav_bytes, filename=filename, session_id=session_id)

                texts.append(result.get("text", "") if result else "")
                combined_segments.extend(result.get("segments", []) if result else [])

            except Exception as e:
                logger.exception(f"[{session_id}] Error transcribing {key}: {e}")

        full_text = "\n".join([t for t in texts if t]).strip()
        if not full_text:
            await dynamo_upsert(dynamodb_table, session_id, {
                "Status": "Error",
                "ErrorCode": "TRANSCRIPTION_FAILED",
                "ErrorMessage": "Transcription of audio files failed.",
            })
            return {"status": "fatal_error", "message": "Audio found but transcription failed."}

        try:
            summary = await get_summary_from_text(full_text)
        except Exception as e:
            logger.exception(f"[{session_id}] Summary generation failed: {e}")
            await dynamo_upsert(dynamodb_table, session_id, {
                "Status": "Error",
                "ErrorCode": "SUMMARY_FAILED",
                "ErrorMessage": "Failed to generate summary from reconstructed transcript.",
            })
            return {"status": "error", "message": "Summary generation failed."}

        try:
            transcript_blob_path = (
                f"transcripts/{clinic_id}/{patient_pin}/{segments[-2] if len(segments) >= 2 else 'unknown'}/{session_id}.json"
            )
        except Exception:
            transcript_blob_path = f"transcripts/{clinic_id}/{patient_pin}/{session_id}.json"

        transcript_obj = {
            "text": full_text,
            "summary": summary,
            "segments": combined_segments,
            "language": "unknown",
        }

        try:
            # -- AWS: S3 put_object --
            await s3_client.put_object(
                Bucket=settings.s3_bucket_name,
                Key=transcript_blob_path,
                Body=json.dumps(transcript_obj, ensure_ascii=False).encode("utf-8"),
                ContentType="application/json",
            )
            # -- OLD Azure --
            # container_client = blob_service_client.get_container_client(container=settings.azure_blob_container_name)
            # await container_client.upload_blob(name=transcript_blob_path, data=json.dumps(transcript_obj), overwrite=True)

        except Exception as e:
            logger.exception(f"[{session_id}] Failed uploading transcript JSON: {e}")
            await dynamo_upsert(dynamodb_table, session_id, {
                "Status": "Error",
                "ErrorCode": "TRANSCRIPT_UPLOAD_FAILED",
                "ErrorMessage": "Failed to upload transcript JSON.",
            })
            return {"status": "error", "message": "Failed to upload transcript JSON."}

        await dynamo_upsert(dynamodb_table, session_id, {
            "Status": "Completed",
            "TranscriptBlobPath": transcript_blob_path,
            "AudioBlobPath": audio_blob_path,
            "ErrorCode": "",
            "ErrorMessage": "",
        })

        logger.info(f"[{session_id}] Recovery complete.")
        return {"status": "recovered", "transcript_blob": transcript_blob_path, "summary": summary}

    except Exception as e:
        logger.exception(f"[{session_id}] Unexpected fatal error in recovery: {e}")
        try:
            await dynamo_upsert(dynamodb_table, session_id, {
                "Status": "Error",
                "ErrorCode": "RECOVERY_FATAL",
                "ErrorMessage": str(e),
            })
        except Exception:
            logger.exception(f"[{session_id}] Also failed to update table after fatal recovery error.")
        return {"status": "error", "message": "Unexpected fatal error during recovery."}
