"""
Shared AWS helper utilities.

dynamo_upsert replicates Azure Table's upsert_entity(mode="merge"):
  - Creates the item if it doesn't exist (the session_id key is always set via Key=).
  - Updates ONLY the provided fields if it does (leaves all other fields untouched).
"""

import logging

logger = logging.getLogger(__name__)


async def dynamo_upsert(table, session_id: str, fields: dict):
    """
    Partial upsert of a DynamoDB session item.
    Equivalent to Azure Table upsert_entity({...}, mode="merge").
    """
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
        Key={"session_id": session_id},
        UpdateExpression=f"SET {', '.join(update_parts)}",
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
    )
    logger.debug(f"[{session_id}] DynamoDB upsert fields: {list(fields.keys())}")
