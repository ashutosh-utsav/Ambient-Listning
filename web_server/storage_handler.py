import asyncio
import logging
import json
from urllib.parse import unquote
from fastapi import HTTPException
from core.config import get_settings
from azure.data.tables.aio import TableServiceClient
from azure.storage.blob.aio import BlobServiceClient
from azure.core.exceptions import ResourceNotFoundError
import io
import random
from typing import Optional, Dict
from typing import List
from core.services import get_summary_from_text
from openai import AsyncOpenAI

settings = get_settings()
client = AsyncOpenAI(api_key=settings.openai_api_key)

logger = logging.getLogger(__name__)


async def estimate_recovery_time(blob_service_client, audio_path: str, session_id: str) -> dict:
    """
    Estimate recovery time for a specific session's audio folder.
    Each .wav file ≈ 25s to transcribe + 30s total summarization time.
    Handles cases where session_id may be URL-encoded (e.g. %24 instead of $).
    """
    try:
        if not audio_path:
            return {
                "status": "fatal_error",
                "message": "No audio path found in table for this session."
            }

        session_id = unquote(session_id or "")
        if not session_id:
            logger.error("[ESTIMATE] Missing session_id, cannot locate session folder.")
            return {"status": "fatal_error", "message": "Session ID missing."}

        container = blob_service_client.get_container_client(
            container=settings.azure_blob_container_name
        )

        if audio_path.lower().endswith(".wav"):
            audio_path = audio_path.rsplit("/", 1)[0] + "/"
        if not audio_path.endswith("/"):
            audio_path += "/"

        logger.info(f"[{session_id}] Estimating recovery time for prefix: {audio_path}")
        logger.warning(f"[DEBUG] Container used: {settings.azure_blob_container_name}")
        logger.warning(f"[DEBUG] Prefix being used: {audio_path}")
        logger.warning(f"[DEBUG] Session ID after decoding: {session_id}")


        found_blobs = []
        async for blob in container.list_blobs(name_starts_with=audio_path):
            if not blob.name.lower().endswith(".wav"):
                continue
            found_blobs.append(blob.name)

        logger.info(f"[DEBUG] Total blobs found under {audio_path}: {len(found_blobs)}")

        count = 0
        session_prefix = f"{audio_path}{session_id}/"
        logger.info(f"[DEBUG] Looking specifically under: {session_prefix}")

        for blob_name in found_blobs:
            if not blob_name.startswith(session_prefix):
                continue
            if not blob_name.lower().endswith(".wav"):
                continue

            relative_path = blob_name[len(session_prefix):]
            if "/" in relative_path:
                continue

            logger.debug(f"[MATCH] Counting {blob_name}")
            count += 1

 
        if count == 0:
            logger.error(f"[ESTIMATE] No audio files found for session {session_id}")
            return {
                "status": "fatal_error",
                "message": "No audio files found for this session."
            }

        total_seconds = count * 25 + 30
        estimated_minutes = round(total_seconds / 60, 2)

        logger.info(
            f"[ESTIMATE] Found {count} audio chunks for session {session_id}. "
            f"Estimated {estimated_minutes} minutes total."
        )

        return {
            "status": "error",
            "errorCode": "RECOVERY_POSSIBLE",
            "message": f"Found {count} audio chunks for recovery.",
            "audio_chunks": count,
            "estimated_minutes": estimated_minutes
        }

    except Exception as e:
        logger.error(f"[ESTIMATE] Error estimating recovery time: {e}", exc_info=True)
        return {
            "status": "error",
            "errorCode": "ESTIMATION_FAILED",
            "message": f"Failed to estimate recovery time: {str(e)}"
        }



async def get_results_from_storage(
    session_id: str,
    table_service_client: TableServiceClient,
    blob_service_client: BlobServiceClient
):
    try:
        table_client = table_service_client.get_table_client(
            table_name=settings.azure_table_name
        )

        try:
            entity = await table_client.get_entity(
                partition_key=session_id,
                row_key=session_id
            )
        except ResourceNotFoundError:
            return {"status": "not_found"}

        status_value = entity.get("Status", "Processing")


        if status_value == "Processing":
            return {"status": "processing"}

        if status_value == "Error":
            audio_path = entity.get("AudioBlobPath", "")
            if not audio_path:
                return {
                    "status": "fatal_error",
                    "message": "No audio path found in table. Cannot estimate recovery."
                }


            prefix = audio_path.rsplit("/", 1)[0] + "/"
            logger.info(f"[{session_id}] Estimating recovery time for prefix: {prefix}")


            estimate_result = await estimate_recovery_time(
                blob_service_client,
                prefix,
                session_id=session_id
            )
            return estimate_result
        
        if status_value == "Completed":
            transcript_path = entity.get("TranscriptBlobPath")

            if not transcript_path:
                return {
                    "status": "error",
                    "errorCode": "BLOB_PATH_MISSING",
                    "errorMessage": "TranscriptBlobPath is missing in table"
                }

            blob_client = blob_service_client.get_blob_client(
                container=settings.azure_blob_container_name,
                blob=transcript_path
            )

            if not await blob_client.exists():
                return {
                    "status": "error",
                    "errorCode": "TRANSCRIPT_NOT_FOUND",
                    "errorMessage": "Transcript blob not found in storage"
                }

            stream = await blob_client.download_blob()
            data = await stream.readall()
            transcript_json = json.loads(data)

            return {
                "status": "completed",
                # "text": transcript_json.get("text"),
                "summary": transcript_json.get("summary")
                # "segments": transcript_json.get("segments", []),
                # "language": transcript_json.get("language", "unknown")
            }

        return {
            "status": f"{status_value}",
            "errorCode": f"{status_value}",
            "errorMessage": f"Unknown status value '{status_value}'"
        }

    except Exception as e:
        logger.error(f"Error in get_results_from_storage for {session_id}", exc_info=True)
        return {
            "status": "error",
            "errorCode": "INTERNAL_EXCEPTION",
            "errorMessage": str(e)
        }

async def transcribe_wav_bytes(
    wav_bytes: bytes,
    filename: str,
    session_id: str,
    retries: int = 3
) -> Optional[Dict]:
    """
    Transcribes a single WAV chunk using OpenAI Whisper (gpt-4o-transcribe).
    Includes retries, structured return, and defensive parsing.
    """
    attempt = 0

    try:
        while attempt < retries:
            try:
                file_obj = io.BytesIO(wav_bytes)
                file_obj.name = filename

                logger.info(f"[{session_id}] Transcribing {filename} (Attempt {attempt+1}/{retries})")

                transcription = await client.audio.transcriptions.create(
                    model="gpt-4o-transcribe",
                    file=file_obj,
                    response_format="json",
                )

                if hasattr(transcription, "model_dump"):
                    transcription = transcription.model_dump()

                text = transcription.get("text") if isinstance(transcription, dict) else getattr(transcription, "text", "")
                language = transcription.get("language") if isinstance(transcription, dict) else getattr(transcription, "language", "unknown")

                segments_raw = transcription.get("segments") if isinstance(transcription, dict) else getattr(transcription, "segments", None)
                segments = []

                if segments_raw and isinstance(segments_raw, list):
                    for seg in segments_raw:
                        if isinstance(seg, dict):
                            segments.append({
                                "start": seg.get("start", 0.0),
                                "end": seg.get("end", 0.0),
                                "text": seg.get("text", "")
                            })
                else:
                    logger.warning(f"[{session_id}] No segments returned for {filename}")

                if not text:
                    logger.warning(f"[{session_id}] Empty transcription for {filename}")
                    return None

                logger.info(f"[{session_id}] Whisper returned text length {len(text)} for {filename}")

                return {
                    "text": text,
                    "language": language or "unknown",
                    "segments": segments
                }

            except Exception as e:
                attempt += 1
                logger.error(f"[{session_id}] Transcription error for {filename} (attempt {attempt}): {e}")
                if attempt < retries:
                    await asyncio.sleep(2 ** attempt + random.random())
                else:
                    logger.error(f"[{session_id}] Fallback transcription failed for file {filename}: {e}")
                    return None

    finally:
        file_obj.close()




async def list_wav_files_in_session_folder(blob_service_client: BlobServiceClient, session_folder_prefix: str) -> List[str]:
    """
    Return list of blob names (strings) that are .wav files directly under session_folder_prefix.
    session_folder_prefix must end with a trailing slash (e.g. "audio/clinic/patient/date/session_id/").
    """
    container = blob_service_client.get_container_client(container=settings.azure_blob_container_name)
    results = []
    async for blob in container.list_blobs(name_starts_with=session_folder_prefix):
        name = blob.name
        if not name.lower().endswith(".wav"):
            continue

        relative = name[len(session_folder_prefix):]
        if "/" in relative:

            continue
        results.append(name)
    return sorted(results)


async def recover_session_from_audio(
    session_id: str,
    table_service_client: TableServiceClient,
    blob_service_client: BlobServiceClient
) -> dict:
    """
    Attempt to recover a session by transcribing audio blobs and creating transcript+summary.
    This function is safe to run as a background task. It will update the table entries accordingly.
    Returns a dict with final status (useful for testing).
    #Change
    """
    try:
        logger.info(f"[{session_id}] Recovery requested.")

        table_client = table_service_client.get_table_client(table_name=settings.azure_table_name)

        try:
            entity = await table_client.get_entity(partition_key=session_id, row_key=session_id)
        except ResourceNotFoundError:
            logger.error(f"[{session_id}] Table entity not found for recovery.")
            return {"status": "not_found"}

        status_value = entity.get("Status", "Processing")
        if status_value == "Completed":
            logger.info(f"[{session_id}] Already completed. Nothing to recover.")
            return {"status": "already_completed"}

        if status_value == "Recovering":
            logger.info(f"[{session_id}] Recovery already in progress.")
            return {"status": "already_recovering"}

        clinic_id = entity.get("ClinicId")
        patient_pin = entity.get("PatientPin")
        audio_blob_path = entity.get("AudioBlobPath", "")

        if not audio_blob_path:
            logger.error(f"[{session_id}] No AudioBlobPath in table. Cannot recover.")
            await table_client.upsert_entity({
                "PartitionKey": session_id,
                "RowKey": session_id,
                "Status": "Error",
                "ErrorCode": "AUDIO_PATH_MISSING",
                "ErrorMessage": "AudioBlobPath missing - cannot recover"
            }, mode="merge")
            return {"status": "fatal_error", "message": "No audio path in table for this session."}

        try:
            await table_client.upsert_entity({
                "PartitionKey": session_id,
                "RowKey": session_id,
                "Status": "Recovering",
                "ErrorCode": "",
                "ErrorMessage": ""
            }, mode="merge")
        except Exception:
            logger.exception(f"[{session_id}] Failed to mark Recovering in table (continuing anyway).")

        prefix = audio_blob_path
        if prefix.endswith(".wav"):
            prefix = prefix.rsplit("/", 1)[0] + "/"
        if not prefix.endswith("/"):
            prefix = prefix + "/"

        segments = prefix.strip("/").split("/")
        last_segment = segments[-1] if segments else ""
        session_prefix = prefix
        if last_segment != session_id:

            session_prefix = prefix.rstrip("/") + f"/{session_id}/"
        else:
            session_prefix = prefix

        wavs = await list_wav_files_in_session_folder(blob_service_client, session_prefix)
        if not wavs:
            wavs = await list_wav_files_in_session_folder(blob_service_client, prefix)

        if not wavs:

            logger.error(f"[{session_id}] No audio files found under prefix {session_prefix} or {prefix}")
            await table_client.upsert_entity({
                "PartitionKey": session_id,
                "RowKey": session_id,
                "Status": "Error",
                "ErrorCode": "AUDIO_NOT_FOUND",
                "ErrorMessage": "No audio blobs found for this session - cannot recover"
            }, mode="merge")
            return {"status": "fatal_error", "message": "No audio files found for this session."}

        logger.info(f"[{session_id}] Found {len(wavs)} wavs to transcribe.")

        texts = []
        combined_segments = []
        for idx, blob_name in enumerate(wavs):
            try:
                blob_client = blob_service_client.get_blob_client(container=settings.azure_blob_container_name, blob=blob_name)
                stream = await blob_client.download_blob()
                wav_bytes = await stream.readall()

                filename = blob_name.split("/")[-1] or f"chunk_{idx}.wav"
                logger.info(f"[{session_id}] Transcribing {filename} ({idx+1}/{len(wavs)})")
                result = await transcribe_wav_bytes(wav_bytes, filename=filename, session_id=session_id)

                if not result or not result.get("text"):
                    logger.warning(f"[{session_id}] Empty transcription for {filename}")
                    texts.append("")
                else:
                    texts.append(result.get("text", ""))

                segs = result.get("segments", []) if result else []
                combined_segments.extend(segs)

            except Exception as e:
                logger.exception(f"[{session_id}] Error transcribing blob {blob_name}: {e}")
 
        full_text = "\n".join([t for t in texts if t]).strip()
        if not full_text:
            # nothing transcribed
            logger.error(f"[{session_id}] All transcriptions empty or failed.")
            await table_client.upsert_entity({
                "PartitionKey": session_id,
                "RowKey": session_id,
                "Status": "Error",
                "ErrorCode": "TRANSCRIPTION_FAILED",
                "ErrorMessage": "Transcription of audio files failed."
            }, mode="merge")
            return {"status": "fatal_error", "message": "Audio found but transcription failed."}

        try:
            logger.info(f"[{session_id}] Generating summary for reconstructed transcript.")
            summary = await get_summary_from_text(full_text)
        except Exception as e:
            logger.exception(f"[{session_id}] Summary generation failed: {e}")
            await table_client.upsert_entity({
                "PartitionKey": session_id,
                "RowKey": session_id,
                "Status": "Error",
                "ErrorCode": "SUMMARY_FAILED",
                "ErrorMessage": "Failed to generate summary from reconstructed transcript."
            }, mode="merge")
            return {"status": "error", "message": "Summary generation failed."}

        try:
            transcript_blob_path = f"transcripts/{clinic_id}/{patient_pin}/{segments[-2] if len(segments) >= 2 else 'unknown'}/{session_id}.json"
        except Exception:
            transcript_blob_path = f"transcripts/{clinic_id}/{patient_pin}/{session_id}.json"

        transcript_obj = {
            "text": full_text,
            "summary": summary,
            "segments": combined_segments,
            "language": "unknown"
        }

        try:
            container_client = blob_service_client.get_container_client(container=settings.azure_blob_container_name)
            await container_client.upload_blob(name=transcript_blob_path, data=json.dumps(transcript_obj, ensure_ascii=False), overwrite=True)
            logger.info(f"[{session_id}] Uploaded reconstructed transcript to {transcript_blob_path}")
        except Exception as e:
            logger.exception(f"[{session_id}] Failed uploading transcript JSON: {e}")
            await table_client.upsert_entity({
                "PartitionKey": session_id,
                "RowKey": session_id,
                "Status": "Error",
                "ErrorCode": "TRANSCRIPT_UPLOAD_FAILED",
                "ErrorMessage": "Failed to upload transcript JSON."
            }, mode="merge")
            return {"status": "error", "message": "Failed to upload transcript JSON."}

        try:
            await table_client.upsert_entity({
                "PartitionKey": session_id,
                "RowKey": session_id,
                "Status": "Completed",
                "TranscriptBlobPath": transcript_blob_path,
                "AudioBlobPath": audio_blob_path,
                "ErrorCode": "",
                "ErrorMessage": ""
            }, mode="merge")
            logger.info(f"[{session_id}] Recovery complete and table updated to Completed.")
        except Exception as e:
            logger.exception(f"[{session_id}] Failed updating table after recovery: {e}")
            await table_client.upsert_entity({
                "PartitionKey": session_id,
                "RowKey": session_id,
                "Status": "Error",
                "ErrorCode": "TABLE_UPDATE_FAILED",
                "ErrorMessage": "Failed to update table after recovery."
            }, mode="merge")
            return {"status": "error", "message": "Failed updating table."}

        return {"status": "recovered", "transcript_blob": transcript_blob_path, "summary": summary}

    except Exception as e:
        logger.exception(f"[{session_id}] Unexpected fatal error in recovery: {e}")
        try:
            table_client = table_service_client.get_table_client(table_name=settings.azure_table_name)
            await table_client.upsert_entity({
                "PartitionKey": session_id,
                "RowKey": session_id,
                "Status": "Error",
                "ErrorCode": "RECOVERY_FATAL",
                "ErrorMessage": str(e)
            }, mode="merge")
        except Exception:
            logger.exception(f"[{session_id}] Also failed to update table after fatal recovery error.")
        return {"status": "error", "message": "Unexpected fatal error during recovery."}
