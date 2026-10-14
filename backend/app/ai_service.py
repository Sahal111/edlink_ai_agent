import os
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

API_URL = "https://generativelanguage.googleapis.com/v1beta/interactions"
TIMEOUT = httpx.Timeout(45.0, connect=5.0)


class GeminiQuotaError(RuntimeError):
    """Kuota atau rate limit Gemini terlampaui (HTTP 429)."""


def generate_draft(title: str, description: str) -> str:
    api_key = os.getenv("GEMINI_API_KEY")
    model_name = os.getenv("GEMINI_MODEL")

    if not api_key:
        raise RuntimeError("GEMINI_API_KEY belum dikonfigurasi")

    if not model_name:
        raise RuntimeError("GEMINI_MODEL belum dikonfigurasi")

    prompt = f"""
Kamu adalah asisten belajar berbahasa Indonesia.
Buat draf jawaban yang akurat, jelas, dan terstruktur.
Jangan mengarang sumber atau fakta.
Draf harus diperiksa mahasiswa sebelum digunakan.

Judul tugas:
{title}

Instruksi tugas:
{description}
"""
    print(f"DEBUG: Memanggil Gemini model={model_name}", flush=True)

    for attempt in range(1, 4):
        try:
            response = httpx.post(
                API_URL,
                headers={"x-goog-api-key": api_key},
                json={"model": model_name, "input": prompt},
                timeout=TIMEOUT,
            )
        except httpx.TimeoutException as e:
            raise RuntimeError("Layanan AI tidak merespons tepat waktu") from e
        except httpx.HTTPError as e:
            raise RuntimeError(
                f"Gagal menghubungi layanan AI: {type(e).__name__}"
            ) from e

        print(
            f"DEBUG: Gemini status={response.status_code} (percobaan {attempt}/3)",
            flush=True,
        )

        if response.status_code != 503 or attempt == 3:
            break

        time.sleep(2 * attempt)  # jeda 2 detik, lalu 4 detik
    if response.status_code == 429:
        raise GeminiQuotaError("Kuota Gemini terlampaui")

    if response.status_code != 200:
        print(f"DEBUG: Gemini body={response.text[:300]}", flush=True)
        raise RuntimeError(f"Gemini mengembalikan status {response.status_code}")
    data = response.json()

    parts = []
    for step in data.get("steps", []):
        if step.get("type") == "model_output":
            for item in step.get("content", []):
                if item.get("type") == "text":
                    parts.append(item.get("text", ""))

    draft = "".join(parts).strip()

    if not draft:
        raise RuntimeError("Gemini tidak menghasilkan teks jawaban")

    return draft
