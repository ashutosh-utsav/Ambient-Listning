# core/fallback_utils.py

import json
import logging
from azure.storage.blob.aio import BlobServiceClient
from core.config import get_settings

settings = get_settings()
logger = logging.getLogger(__name__)


async def load_transcript(blob_service_client: BlobServiceClient, transcript_blob_path: str):
    try:
        blob_client = blob_service_client.get_blob_client(
            container=settings.azure_blob_container_name,
            blob=transcript_blob_path
        )
        stream = await blob_client.download_blob()
        data = await stream.readall()
        return json.loads(data)
    except Exception as e:
        logger.error(f"Failed to load transcript blob: {transcript_blob_path}", exc_info=True)
        return None


async def build_combined_text(blob_service_client, table_service_client, ref_ids: list[str], new_text: str):
    """
    Load transcript of each reference session and append them + new text.
    """
    combined_text_parts = []

    for sid in ref_ids:
        try:
            table_client = table_service_client.get_table_client(settings.azure_table_name)
            entity = await table_client.get_entity(sid, sid)

            old_blob = entity.get("TranscriptBlobPath")
            if not old_blob:
                continue

            transcript_json = await load_transcript(blob_service_client, old_blob)
            if not transcript_json:
                continue

            old_text = transcript_json.get("text", "")
            if old_text:
                combined_text_parts.append(old_text)

        except Exception as e:
            logger.error(f"Failed loading ref session {sid}", exc_info=True)

    combined_text_parts.append(new_text)

    return "\n".join(combined_text_parts)
