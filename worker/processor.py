"""
The main file where we are processing the audio from VAD to transcribe through OpenAI.

MIGRATION NOTE: Azure Blob + Table replaced with S3 + DynamoDB.
Old Azure calls kept as comments for reference.
"""

import asyncio
import os
import uuid
import soundfile as sf
import numpy as np
import base64
import json
import gc
import io
import functools
from typing import Awaitable, Callable, Optional
from core.config import get_settings
from core.services import get_summary_from_text
from core.aws_helpers import dynamo_upsert
import logging
import openai
from datetime import datetime
from collections import deque
import random

# -- OLD Azure imports (kept for reference) --
# from azure.storage.blob.aio import BlobServiceClient
# from azure.data.tables.aio import TableServiceClient
# from azure.core.exceptions import ResourceExistsError

from core.models import TranscriptionTask
from core.fallback_utils import build_combined_text

settings = get_settings()
client = openai.AsyncOpenAI(api_key=settings.openai_api_key)

SAMPLING_RATE = settings.SAMPLING_RATE
TRANSCRIPTION_BUFFER_SECONDS = settings.TRANSCRIPTION_BUFFER_SECONDS


def _convert_bytes_to_float_array(audio_bytes: bytes) -> np.ndarray:
    num_samples = len(audio_bytes) // 2
    safe_byte_length = num_samples * 2
    audio_int16 = np.frombuffer(audio_bytes[:safe_byte_length], dtype=np.int16)
    audio_float32 = audio_int16.astype(np.float32) / 32767.0
    return audio_float32


class SessionProcessor:
    def __init__(
        self,
        session_id: str,
        clinic_id: str,
        patient_pin: str,
        s3_client,          # was: blob_service_client: BlobServiceClient
        dynamodb_table,     # was: table_service_client: TableServiceClient
        error_reporter: Callable[[str, str, str], Awaitable[None]],
        ref_ids: list[str] | None = None,
    ):
        self.session_id = session_id
        self.clinic_id = clinic_id
        self.patient_pin = patient_pin
        date_str = datetime.now().strftime("%d%m%Y")

        self.audio_base_path = f"audio/{clinic_id}/{patient_pin}/{date_str}/{self.session_id}"
        self.transcript_blob_name = f"transcripts/{clinic_id}/{patient_pin}/{date_str}/{self.session_id}.json"

        self.s3_client = s3_client
        self.dynamodb_table = dynamodb_table
        self.error_reporter = error_reporter
        self.ref_ids = ref_ids or []

        self.transcription_buffer = deque()
        self.transcription_tasks = []
        self.chunk_index = 0

        self.last_updated_time = datetime.now()
        self.packet_reassembly_buffer = {}
        self.processed_first_chunk = False

    async def reassemble_and_process_chunk(self, task: TranscriptionTask):
        if task.chunk_id not in self.packet_reassembly_buffer:
            self.packet_reassembly_buffer[task.chunk_id] = {}

        self.packet_reassembly_buffer[task.chunk_id][task.packet_index] = task.audio_chunk

        packet_map = self.packet_reassembly_buffer[task.chunk_id]
        required_keys = set(range(task.total_packets))
        current_keys = set(packet_map.keys())

        if required_keys.issubset(current_keys) and len(current_keys) == task.total_packets:
            try:
                full_b64_string = "".join(packet_map[i] for i in range(task.total_packets))
            except KeyError as e:
                self.packet_reassembly_buffer.pop(task.chunk_id, None)
                await self.error_reporter(self.session_id, "REASSEMBLY_ERROR", f"KeyError during reassembly: {e}")
                return

            await self.process_chunk(full_b64_string)
            self.packet_reassembly_buffer.pop(task.chunk_id, None)

        elif len(current_keys) > task.total_packets:
            self.packet_reassembly_buffer.pop(task.chunk_id, None)
            await self.error_reporter(self.session_id, "REASSEMBLY_MISMATCH", "Packet buffer mismatch")

    async def process_chunk(self, audio_chunk_b64: str):
        self.last_updated_time = datetime.now()
        if not audio_chunk_b64:
            return

        loop = asyncio.get_running_loop()
        audio_bytes = await loop.run_in_executor(None, base64.b64decode, audio_chunk_b64)

        if not self.processed_first_chunk:
            if len(audio_bytes) > 44 and audio_bytes.startswith(b"RIFF"):
                audio_bytes = audio_bytes[44:]
            self.processed_first_chunk = True

        audio_float32 = await loop.run_in_executor(None, _convert_bytes_to_float_array, audio_bytes)
        del audio_bytes

        self.transcription_buffer.append(audio_float32)

        buffer_duration = sum(len(c) for c in self.transcription_buffer) / SAMPLING_RATE
        if buffer_duration >= TRANSCRIPTION_BUFFER_SECONDS:
            await self._trigger_transcription_task()
        del audio_float32

    async def _trigger_transcription_task(self):
        if self.transcription_buffer:
            loop = asyncio.get_running_loop()
            audio_list = list(self.transcription_buffer)
            full_buffer_audio = await loop.run_in_executor(None, np.concatenate, audio_list)
            del audio_list

            in_memory_wav = io.BytesIO()
            write_task = functools.partial(sf.write, in_memory_wav, full_buffer_audio, SAMPLING_RATE, format="WAV")
            await loop.run_in_executor(None, write_task)
            del full_buffer_audio

            in_memory_wav.seek(0)
            wav_bytes = in_memory_wav.getvalue()

            try:
                wav_file_name = f"{self.chunk_index + 1:03d}.wav"
                wav_key = f"{self.audio_base_path}/{wav_file_name}"

                # -- AWS: S3 put_object --
                await self.s3_client.put_object(
                    Bucket=settings.s3_bucket_name,
                    Key=wav_key,
                    Body=wav_bytes,
                )
                # -- OLD Azure blob upload --
                # container_client = self.blob_service_client.get_container_client(settings.azure_blob_container_name)
                # await container_client.upload_blob(name=wav_blob_name, data=wav_bytes, overwrite=True)

            except Exception as e:
                await self.error_reporter(self.session_id, "S3_UPLOAD_FAILED", str(e))
            finally:
                del wav_bytes

            in_memory_wav.seek(0)
            in_memory_wav.name = f"chunk_{self.chunk_index}.wav"
            task = asyncio.create_task(
                _transcribe_chunk(in_memory_wav, self.chunk_index, self.session_id, self.error_reporter)
            )
            self.transcription_tasks.append(task)

        self.chunk_index += 1
        self.transcription_buffer.clear()

    async def finalize_session(self):
        session_failed = False

        if len(self.transcription_buffer) > 0:
            await self._trigger_transcription_task()

        partial_transcripts = []
        final_transcript = {"text": "", "segments": [], "language": ""}

        try:
            partial_transcripts = await asyncio.gather(*self.transcription_tasks)
        except Exception as e:
            session_failed = True
            await self.error_reporter(self.session_id, "TRANSCRIPTION_GATHER_FAILED", str(e))

        partial_transcripts.sort(key=lambda x: x[0])
        text_parts = []
        time_offset = 0.0

        for index, transcript_chunk in partial_transcripts:
            if transcript_chunk is None:
                session_failed = True
                continue

            if not final_transcript["language"]:
                if isinstance(transcript_chunk, dict):
                    final_transcript["language"] = transcript_chunk.get("language", "unknown")
                else:
                    final_transcript["language"] = getattr(transcript_chunk, "language", "unknown")

            chunk_text = (
                transcript_chunk["text"]
                if isinstance(transcript_chunk, dict)
                else getattr(transcript_chunk, "text", "")
            )
            text_parts.append(chunk_text)

            segments = (
                transcript_chunk.get("segments", [])
                if isinstance(transcript_chunk, dict)
                else getattr(transcript_chunk, "segments", []) or []
            )
            if segments:
                for segment in segments:
                    sdict = dict(segment)
                    sdict["start"] = sdict.get("start", 0) + time_offset
                    sdict["end"] = sdict.get("end", 0) + time_offset
                    final_transcript["segments"].append(sdict)

                last_segment_end = (
                    segments[-1].get("end", 0)
                    if isinstance(segments[-1], dict)
                    else getattr(segments[-1], "end", 0)
                )
                time_offset += last_segment_end

        final_transcript["text"] = " ".join(text_parts).strip()

        try:
            if self.ref_ids:
                # -- AWS: s3_client + dynamodb_table (was blob_service_client + table_service_client) --
                combined_text = await build_combined_text(
                    self.s3_client,
                    self.dynamodb_table,
                    self.ref_ids,
                    final_transcript["text"],
                )
                summary = await get_summary_from_text(combined_text)
                logging.info(f"[{self.session_id}] Generated summary using {len(self.ref_ids)} reference sessions.")
            else:
                summary = await get_summary_from_text(final_transcript["text"])
                logging.info(f"[{self.session_id}] Generated summary without reference sessions.")

            final_transcript["summary"] = summary
        except Exception as e:
            session_failed = True
            await self.error_reporter(self.session_id, "SUMMARY_FAILED", str(e))
            final_transcript["summary"] = ""

        blob_upload_success = False
        try:
            transcript_json_str = json.dumps(final_transcript, indent=2, ensure_ascii=False)

            # -- AWS: S3 put_object for transcript JSON --
            await self.s3_client.put_object(
                Bucket=settings.s3_bucket_name,
                Key=self.transcript_blob_name,
                Body=transcript_json_str.encode("utf-8"),
                ContentType="application/json",
            )
            # -- OLD Azure blob upload --
            # container_client = self.blob_service_client.get_container_client(settings.azure_blob_container_name)
            # await container_client.upload_blob(name=self.transcript_blob_name, data=transcript_json_str, overwrite=True)

            blob_upload_success = True
        except Exception as e:
            session_failed = True
            await self.error_reporter(self.session_id, "TRANSCRIPT_UPLOAD_FAILED", str(e))

        try:
            # -- AWS: DynamoDB upsert (replaces Azure Table upsert_entity) --
            if session_failed or not blob_upload_success:
                await dynamo_upsert(self.dynamodb_table, self.session_id, {
                    "Status": "Error",
                    "TranscriptBlobPath": "",
                    "AudioBlobPath": self.audio_base_path,
                    "ClinicId": self.clinic_id,
                    "PatientPin": self.patient_pin,
                    "ErrorCode": "PROCESSING_FAILED",
                    "ErrorMessage": "One or more steps failed during processing.",
                })
            else:
                await dynamo_upsert(self.dynamodb_table, self.session_id, {
                    "Status": "Completed",
                    "TranscriptBlobPath": self.transcript_blob_name,
                    "AudioBlobPath": self.audio_base_path,
                    "ClinicId": self.clinic_id,
                    "PatientPin": self.patient_pin,
                    "ErrorCode": "",
                    "ErrorMessage": "",
                })
            # -- OLD Azure Table upsert --
            # table_client = self.table_service_client.get_table_client(settings.azure_table_name)
            # await table_client.upsert_entity({...}, mode="merge")

        except Exception as e:
            await self.error_reporter(self.session_id, "TABLE_UPDATE_FAILED", str(e))

        await self._cleanup_resources()

    async def _cleanup_resources(self):
        self.transcription_buffer.clear()
        self.packet_reassembly_buffer.clear()
        for task in self.transcription_tasks:
            if not task.done():
                task.cancel()
        self.transcription_tasks.clear()
        del self.transcription_buffer
        del self.packet_reassembly_buffer
        del self.transcription_tasks
        gc.collect()


async def _transcribe_chunk(
    audio_file: io.BytesIO,
    chunk_index: int,
    session_id: str,
    error_reporter: Callable[[str, str, str], Awaitable[None]],
    retries: int = 3,
):
    attempt = 0
    try:
        while attempt < retries:
            try:
                audio_file.seek(0)
                transcription = await client.audio.transcriptions.create(
                    model="gpt-4o-transcribe",
                    file=audio_file,
                    response_format="json",
                )

                if hasattr(transcription, "model_dump"):
                    transcription = transcription.model_dump()

                return chunk_index, transcription

            except Exception as e:
                attempt += 1
                if attempt < retries:
                    await asyncio.sleep(2 ** attempt + random.random())
                else:
                    await error_reporter(
                        session_id,
                        "TRANSCRIPTION_FAILED",
                        f"Chunk {chunk_index + 1} failed all retries: {e}",
                    )
                    return chunk_index, None

    finally:
        if audio_file and not audio_file.closed:
            audio_file.close()
