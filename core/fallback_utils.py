# core/fallback_utils.py
#
# MIGRATION NOTE: Azure Blob + Table replaced with S3 + DynamoDB.
# Old Azure calls are kept as comments for reference.

import json
import logging

# from azure.storage.blob.aio import BlobServiceClient  # -- OLD --

from core.config import get_settings

settings = get_settings()
logger = logging.getLogger(__name__)


async def load_transcript(s3_client, transcript_blob_path: str):
    """
    Load a transcript JSON from S3.
    Was: load_transcript(blob_service_client, transcript_blob_path)
    """
    try:
        # -- AWS: S3 get_object --
        response = await s3_client.get_object(
            Bucket=settings.s3_bucket_name,
            Key=transcript_blob_path,
        )
        data = await response["Body"].read()
        return json.loads(data)

        # -- OLD Azure blob download --
        # blob_client = blob_service_client.get_blob_client(
        #     container=settings.azure_blob_container_name,
        #     blob=transcript_blob_path,
        # )
        # stream = await blob_client.download_blob()
        # data = await stream.readall()
        # return json.loads(data)

    except Exception as e:
        logger.error(f"Failed to load transcript from S3: {transcript_blob_path}", exc_info=True)
        return None


async def build_combined_text(s3_client, dynamodb_table, ref_ids: list[str], new_text: str):
    """
    Load transcripts of reference sessions and combine with new text.
    Was: build_combined_text(blob_service_client, table_service_client, ref_ids, new_text)
    Now: build_combined_text(s3_client, dynamodb_table, ref_ids, new_text)
    """
    combined_text_parts = []

    for sid in ref_ids:
        try:
            # -- AWS: DynamoDB get_item --
            response = await dynamodb_table.get_item(Key={"session_id": sid})
            entity = response.get("Item")

            # -- OLD Azure table query --
            # table_client = table_service_client.get_table_client(settings.azure_table_name)
            # entity = await table_client.get_entity(sid, sid)

            if not entity:
                continue

            old_blob = entity.get("TranscriptBlobPath")
            if not old_blob:
                continue

            transcript_json = await load_transcript(s3_client, old_blob)
            if not transcript_json:
                continue

            old_text = transcript_json.get("text", "")
            if old_text:
                combined_text_parts.append(old_text)

        except Exception as e:
            logger.error(f"Failed loading ref session {sid}", exc_info=True)

    combined_text_parts.append(new_text)
    return "\n".join(combined_text_parts)
