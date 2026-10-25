import json
import os
import urllib.request
import urllib.error
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

key = os.getenv("GOOGLE_GEMINI_KEY", "").strip()
if not key:
    raise SystemExit("GOOGLE_GEMINI_KEY is missing or empty in .env")

model = "gemini-2.5-flash"
url = (
    f"https://generativelanguage.googleapis.com/v1beta/"
    f"models/{model}:generateContent"
)

payload = {
    "contents": [
        {"parts": [{"text": "Explain what an API is in one simple sentence."}]}
    ]
}

request = urllib.request.Request(
    url,
    data=json.dumps(payload).encode("utf-8"),
    headers={
        "x-goog-api-key": key,
        "Content-Type": "application/json",
    },
    method="POST",
)

try:
    with urllib.request.urlopen(request, timeout=60) as response:
        result = json.load(response)

    candidates = result.get("candidates", [])
    parts = candidates[0].get("content", {}).get("parts", []) if candidates else []
    text = "\n".join(part["text"] for part in parts if "text" in part)

    print(text or json.dumps(result, indent=2))
except urllib.error.HTTPError as error:
    print(f"API error {error.code}: {error.read().decode()}")
except urllib.error.URLError as error:
    print(f"Network error: {error.reason}")