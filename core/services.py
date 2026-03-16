import openai
import logging
import json
import re
from .config import get_settings
from .logging_config import setup_logging

setup_logging()

settings = get_settings()
client = openai.AsyncOpenAI(api_key=settings.openai_api_key)

# Updated to be general-purpose and request an array for bullet points
SYSTEM_PROMPT = """
You are a helpful assistant that extracts relevant information from Google Meet conversation transcripts.
Provide a summary of the conversation in a structured JSON format. 

## JSON STRUCTURE REQUIRED:
{
  "summary": "A concise paragraph summarizing the overall discussion.",
  "bullet_points": ["Point 1", "Point 2", "Point 3"],
  "participants": ["Name 1", "Name 2"],
  "action_items": ["Task 1", "Task 2"]
}

## RULES:
1. If no transcript is provided or it's empty, return the JSON with "No information found" for strings and empty arrays [] for lists.
2. Ensure the output is strictly valid JSON.
3. Do not include markdown code blocks (no ```json).
4. Maintain a professional and helpful tone.
"""

def extract_json_from_markdown(text: str) -> str:
    """Safety net to remove markdown code block formatting if present."""
    text = re.sub(r'^```json\s*', '', text, flags=re.MULTILINE)
    text = re.sub(r'^```\s*$', '', text, flags=re.MULTILINE)
    text = re.sub(r'```$', '', text)
    return text.strip()

async def get_summary_from_text(transcript_text: str):
    # CRITICAL: This now matches the new JSON structure
    DEFAULT_EMPTY_JSON = {
        "summary": "No relevant information found.",
        "bullet_points": [],
        "participants": [],
        "action_items": []
    }

    if not transcript_text or not transcript_text.strip():
        logging.warning("Empty transcript received. Returning default JSON.")
        return DEFAULT_EMPTY_JSON

    try:
        response = await client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": transcript_text},
            ],
            temperature=0.2,
            # NEW: This forces the model to output JSON
            response_format={"type": "json_object"} 
        )

        output_text = response.choices[0].message.content.strip()
        clean_output = extract_json_from_markdown(output_text)

        try:
            structured_data = json.loads(clean_output)
            # Ensure all keys exist even if model forgot one
            for key in DEFAULT_EMPTY_JSON:
                if key not in structured_data:
                    structured_data[key] = DEFAULT_EMPTY_JSON[key]
            return structured_data
            
        except json.JSONDecodeError as json_err:
            logging.error(f"JSON decode error: {json_err} | Raw: {clean_output}")
            return DEFAULT_EMPTY_JSON

    except Exception as e:
        logging.error(f"Error in get_summary_from_text: {e}")
        return DEFAULT_EMPTY_JSON