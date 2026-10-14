import os
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

API_URL = "https://generativelanguage.googleapis.com/v1beta/interactions"

# Batas waktu agar endpoint tidak menggantung lebih lama dari curl/extension.
TOTAL_BUDGET = 45.0  # detik, total untuk semua percobaan
PER_ATTEMPT_TIMEOUT = 25.0  # detik, maksimum satu percobaan
CONNECT_TIMEOUT = 5.0
ATTEMPTS_PER_MODEL = 2
RETRY_STATUS = {500, 502, 503, 504}


class GeminiQuotaError(RuntimeError):
    """Kuota atau rate limit Gemini terlampaui (HTTP 429)."""


def _log(message: str) -> None:
    print(f"DEBUG: {message}", flush=True)


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

    # Model utama dulu, lalu model cadangan (GEMINI_FALLBACK_MODELS, dipisah koma).
    fallbacks = [
        m.strip()
        for m in os.getenv("GEMINI_FALLBACK_MODELS", "").split(",")
        if m.strip()
    ]
    models = [model_name] + [m for m in fallbacks if m != model_name]

    started = time.monotonic()
    response = None
    last_error = "tidak ada respons"
    attempt = 0
    done = False

    for model in models:
        for _ in range(ATTEMPTS_PER_MODEL):
            remaining = TOTAL_BUDGET - (time.monotonic() - started)
            if remaining <= 1:
                done = True
                break

            attempt += 1
            timeout = httpx.Timeout(
                min(PER_ATTEMPT_TIMEOUT, remaining),
                connect=CONNECT_TIMEOUT,
            )
            _log(f"Memanggil Gemini model={model} (percobaan {attempt})")

            try:
                response = httpx.post(
                    API_URL,
                    headers={"x-goog-api-key": api_key},
                    json={"model": model, "input": prompt},
                    timeout=timeout,
                )
            except httpx.TimeoutException:
                last_error = "Layanan AI tidak merespons tepat waktu"
                _log(f"Timeout setelah {time.monotonic() - started:.1f} detik")
                response = None
            except httpx.HTTPError as e:
                raise RuntimeError(
                    f"Gagal menghubungi layanan AI: {type(e).__name__}"
                ) from e
            else:
                _log(
                    f"Gemini model={model} status={response.status_code} "
                    f"({time.monotonic() - started:.1f} detik)"
                )
                if response.status_code not in RETRY_STATUS:
                    done = True
                    break
                _log(f"Gemini body={response.text[:300]}")
                last_error = f"Gemini mengembalikan status {response.status_code}"

            pause = 2
            if TOTAL_BUDGET - (time.monotonic() - started) > pause + 1:
                time.sleep(pause)
        if done:
            break

    if response is None or response.status_code in RETRY_STATUS:
        raise RuntimeError(last_error)

    if response.status_code == 429:
        raise GeminiQuotaError("Kuota Gemini terlampaui")

    if response.status_code != 200:
        _log(f"Gemini body={response.text[:300]}")
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
        # Format respons belum terverifikasi: catat bentuknya untuk diagnosis.
        _log(f"Respons tanpa teks. Kunci tingkat atas: {list(data.keys())}")
        raise RuntimeError("Gemini tidak menghasilkan teks jawaban")

    return draft
