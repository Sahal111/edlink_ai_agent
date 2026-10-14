import os
from dotenv import load_dotenv
from google import genai

load_dotenv(".env")

api_key = os.getenv("GEMINI_API_KEY")

if not api_key:
    raise SystemExit("GEMINI_API_KEY belum diisi di backend/.env")

client = genai.Client(api_key=api_key)

print("Model yang tersedia untuk API key kamu:\n")

for model in client.models.list():
    actions = getattr(model, "supported_actions", []) or []

    if "generateContent" in actions:
        print(model.name)
