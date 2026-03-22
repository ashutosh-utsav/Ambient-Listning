"""
Knowledge Base service — progressive summarization layer.

Stores per-client session summaries and maintains a running "client brief"
that is updated via GPT-4o after each completed session.

DynamoDB table: clientKB (composite key)
  PK: client_id  = "{clinic_id}#{patient_pin}"
  SK: record_type = "BRIEF" | "SESSION#{session_id}"

The brief itself (500-1000 words) lives in S3 at:
  kb/{clinic_id}/{patient_pin}/brief.json
"""

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Optional

import aioboto3
import openai
from boto3.dynamodb.conditions import Key as DynamoKey

from core.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()
llm_client = openai.AsyncOpenAI(api_key=settings.openai_api_key)


def _client_id(clinic_id: str, patient_pin: str) -> str:
    return f"{clinic_id}#{patient_pin}"


def _brief_s3_key(clinic_id: str, patient_pin: str) -> str:
    return f"kb/{clinic_id}/{patient_pin}/brief.json"


async def _kb_upsert(table, client_id: str, record_type: str, fields: dict):
    """DynamoDB upsert for the clientKB table (composite key)."""
    if not fields:
        return
    update_parts = []
    names = {}
    values = {}
    for i, (k, v) in enumerate(fields.items()):
        name_key = f"#f{i}"
        val_key = f":v{i}"
        update_parts.append(f"{name_key} = {val_key}")
        names[name_key] = k
        values[val_key] = v
    await table.update_item(
        Key={"client_id": client_id, "record_type": record_type},
        UpdateExpression=f"SET {', '.join(update_parts)}",
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
    )


# ── Session indexing ────────────────────────────────────────────────

async def index_session(
    clinic_id: str,
    patient_pin: str,
    session_id: str,
    summary_dict: dict,
    transcript_s3_key: str,
    kb_table,
):
    """Write a SESSION item to the KB table."""
    cid = _client_id(clinic_id, patient_pin)
    record_type = f"SESSION#{session_id}"

    await _kb_upsert(kb_table, cid, record_type, {
        "session_id": session_id,
        "session_date": datetime.now(timezone.utc).isoformat(),
        "transcript_s3_key": transcript_s3_key,
        "summary_text": summary_dict.get("summary", ""),
        "bullet_points": summary_dict.get("bullet_points", []),
        "action_items": summary_dict.get("action_items", []),
        "participants": summary_dict.get("participants", []),
    })
    logger.info(f"[KB] Indexed session {session_id} for client {cid}")


# ── Progressive brief ──────────────────────────────────────────────

BRIEF_SYSTEM_PROMPT = """\
You are a clinical psychologist's assistant. Your job is to maintain a concise, \
up-to-date "client brief" that a therapist reads before each session.

You will receive:
1. The EXISTING client brief (may be empty for the first session).
2. A list of recent SESSION SUMMARIES (most recent last).

Produce a JSON object with these fields:
{
  "brief_text": "A 500-1000 word narrative covering: client background, presenting concerns, \
treatment progress, current status, risk factors, and upcoming focus areas.",
  "active_goals": ["goal 1", "goal 2"],
  "key_themes": ["theme 1", "theme 2"],
  "last_sessions_highlights": [
    {"session_id": "...", "date": "...", "highlight": "one-line summary"}
  ]
}

Rules:
- Integrate new information, don't just append.
- Drop stale details that are no longer clinically relevant.
- Always note any safety concerns or risk factors prominently.
- Keep the brief between 500-1000 words regardless of how many sessions exist.
- The last_sessions_highlights should cover the 3 most recent sessions only.
- Output ONLY valid JSON. No markdown, no code blocks.
"""


async def update_brief(
    clinic_id: str,
    patient_pin: str,
    kb_table,
    s3_client,
):
    """Regenerate the progressive brief using GPT-4o."""
    cid = _client_id(clinic_id, patient_pin)
    s3_key = _brief_s3_key(clinic_id, patient_pin)

    # 1. Fetch existing brief from S3 (if any)
    existing_brief_text = ""
    try:
        resp = await s3_client.get_object(Bucket=settings.s3_bucket_name, Key=s3_key)
        data = await resp["Body"].read()
        existing_brief = json.loads(data)
        existing_brief_text = existing_brief.get("brief_text", "")
    except Exception:
        existing_brief_text = "(No prior brief — this is the first session.)"

    # 2. Fetch recent sessions from DynamoDB
    response = await kb_table.query(
        KeyConditionExpression=DynamoKey("client_id").eq(cid) & DynamoKey("record_type").begins_with("SESSION#"),
    )
    items = response.get("Items", [])
    items.sort(key=lambda x: x.get("session_date", ""))

    # Take last N sessions for the prompt
    recent = items[-settings.kb_brief_recent_sessions:]

    sessions_text = ""
    for item in recent:
        sid = item.get("session_id", "unknown")
        date = item.get("session_date", "unknown")
        summary = item.get("summary_text", "No summary available.")
        bullets = item.get("bullet_points", [])
        bullets_str = "\n".join(f"  - {b}" for b in bullets) if bullets else "  (none)"
        actions = item.get("action_items", [])
        actions_str = "\n".join(f"  - {a}" for a in actions) if actions else "  (none)"

        sessions_text += (
            f"\n--- Session {sid} ({date}) ---\n"
            f"Summary: {summary}\n"
            f"Key points:\n{bullets_str}\n"
            f"Action items:\n{actions_str}\n"
        )

    user_prompt = (
        f"EXISTING BRIEF:\n{existing_brief_text}\n\n"
        f"RECENT SESSIONS ({len(recent)} total):\n{sessions_text}"
    )

    # 3. Call GPT-4o
    try:
        response = await llm_client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": BRIEF_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.3,
            max_tokens=1500,
            response_format={"type": "json_object"},
        )
        brief_json = json.loads(response.choices[0].message.content.strip())
    except Exception as e:
        logger.error(f"[KB] GPT-4o brief generation failed for {cid}: {e}")
        return

    brief_json["generated_at"] = datetime.now(timezone.utc).isoformat()
    brief_json["based_on_sessions"] = len(items)

    # 4. Write brief to S3
    await s3_client.put_object(
        Bucket=settings.s3_bucket_name,
        Key=s3_key,
        Body=json.dumps(brief_json, ensure_ascii=False).encode("utf-8"),
        ContentType="application/json",
    )

    # 5. Update BRIEF item in DynamoDB (atomic version increment)
    now = datetime.now(timezone.utc).isoformat()
    await kb_table.update_item(
        Key={"client_id": cid, "record_type": "BRIEF"},
        UpdateExpression="SET brief_s3_key = :s3key, last_updated = :ts, session_count = :sc ADD brief_version :one",
        ExpressionAttributeValues={
            ":s3key": s3_key,
            ":ts": now,
            ":sc": len(items),
            ":one": 1,
        },
    )
    logger.info(f"[KB] Brief updated for {cid} ({len(items)} sessions)")


# ── Coordinator (called from worker) ───────────────────────────────

async def index_session_and_update_brief(
    clinic_id: str,
    patient_pin: str,
    session_id: str,
    summary_dict: dict,
    transcript_s3_key: str,
    s3_client,
):
    """
    Fire-and-forget coordinator: index the session, then regenerate the brief.
    Opens its own DynamoDB resource for the KB table to avoid threading it
    through the worker's constructor chain.
    """
    try:
        boto_session = aioboto3.Session(
            aws_access_key_id=settings.aws_access_key_id,
            aws_secret_access_key=settings.aws_secret_access_key,
            region_name=settings.aws_region,
        )
        async with boto_session.resource(
            "dynamodb", endpoint_url=settings.dynamodb_endpoint_url
        ) as dynamodb:
            kb_table = await dynamodb.Table(settings.kb_dynamodb_table_name)

            await index_session(
                clinic_id, patient_pin, session_id,
                summary_dict, transcript_s3_key, kb_table,
            )
            await update_brief(
                clinic_id, patient_pin, kb_table, s3_client,
            )
    except Exception:
        logger.exception(f"[KB] Failed to index session {session_id} for {clinic_id}#{patient_pin}")


# ── Read APIs (called from web server) ─────────────────────────────

async def get_brief(
    clinic_id: str,
    patient_pin: str,
    kb_table,
    s3_client,
) -> Optional[dict]:
    """Return the client's progressive brief, or None if not found."""
    cid = _client_id(clinic_id, patient_pin)

    response = await kb_table.get_item(
        Key={"client_id": cid, "record_type": "BRIEF"}
    )
    item = response.get("Item")
    if not item:
        return None

    s3_key = item.get("brief_s3_key")
    if not s3_key:
        return None

    try:
        resp = await s3_client.get_object(Bucket=settings.s3_bucket_name, Key=s3_key)
        data = await resp["Body"].read()
        brief = json.loads(data)
        brief["session_count"] = item.get("session_count", 0)
        brief["last_updated"] = item.get("last_updated", "")
        return brief
    except Exception as e:
        logger.error(f"[KB] Failed to fetch brief from S3 for {cid}: {e}")
        return None


async def get_session_timeline(
    clinic_id: str,
    patient_pin: str,
    kb_table,
) -> list[dict]:
    """Return all sessions for a client, sorted chronologically."""
    cid = _client_id(clinic_id, patient_pin)

    response = await kb_table.query(
        KeyConditionExpression=DynamoKey("client_id").eq(cid) & DynamoKey("record_type").begins_with("SESSION#"),
    )
    items = response.get("Items", [])
    items.sort(key=lambda x: x.get("session_date", ""))

    return [
        {
            "session_id": item.get("session_id"),
            "session_date": item.get("session_date"),
            "summary_text": item.get("summary_text"),
            "bullet_points": item.get("bullet_points", []),
            "action_items": item.get("action_items", []),
            "participants": item.get("participants", []),
        }
        for item in items
    ]


async def get_session_detail(
    clinic_id: str,
    patient_pin: str,
    session_id: str,
    kb_table,
    s3_client,
) -> Optional[dict]:
    """Return a specific session's summary + full transcript from S3."""
    cid = _client_id(clinic_id, patient_pin)
    record_type = f"SESSION#{session_id}"

    response = await kb_table.get_item(
        Key={"client_id": cid, "record_type": record_type}
    )
    item = response.get("Item")
    if not item:
        return None

    result = {
        "session_id": item.get("session_id"),
        "session_date": item.get("session_date"),
        "summary_text": item.get("summary_text"),
        "bullet_points": item.get("bullet_points", []),
        "action_items": item.get("action_items", []),
        "participants": item.get("participants", []),
    }

    # Enrich with full transcript from S3
    transcript_key = item.get("transcript_s3_key")
    if transcript_key:
        try:
            resp = await s3_client.get_object(
                Bucket=settings.s3_bucket_name, Key=transcript_key
            )
            data = await resp["Body"].read()
            transcript = json.loads(data)
            result["full_text"] = transcript.get("text", "")
            result["segments"] = transcript.get("segments", [])
        except Exception as e:
            logger.warning(f"[KB] Could not fetch transcript for {session_id}: {e}")
            result["full_text"] = None
            result["segments"] = []

    return result
