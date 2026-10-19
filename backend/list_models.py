"""Menampilkan model Gemini yang tersedia untuk API key di backend/.env.

Jalankan dari folder backend/:  python list_models.py
Skrip ini memanggil layanan Gemini (read-only, tanpa biaya generasi).
"""

import os
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

api_key = os.getenv("GEMINI_API_KEY")

if not api_key:
    raise SystemExit("GEMINI_API_KEY belum diisi di backend/.env")

response = httpx.get(
    "https://generativelanguage.googleapis.com/v1beta/models",
    headers={"x-goog-api-key": api_key},
    params={"pageSize": 200},
    timeout=20.0,
)
response.raise_for_status()

print("Model yang tersedia untuk API key kamu:\n")

for model in response.json().get("models", []):
    methods = model.get("supportedGenerationMethods", [])
    if "generateContent" in methods:
        print(model.get("name", "").removeprefix("models/"))
