"""Mengubah lampiran tugas (PDF) menjadi teks untuk bahan draf AI.

Hanya PDF yang berisi teks yang bisa dibaca. PDF hasil scan (gambar) belum
didukung karena butuh OCR.
"""

import base64
import binascii
import io

from pypdf import PdfReader

MAX_FILE_BYTES = 15 * 1024 * 1024
MAX_PAGES = 60
MAX_TOTAL_CHARS = 30000
MAX_FILES = 3


def _extract_pdf(data: bytes) -> tuple[str, int]:
    reader = PdfReader(io.BytesIO(data))

    if reader.is_encrypted and not reader.decrypt(""):
        raise ValueError("PDF dikunci dengan password")

    pages = reader.pages
    total_pages = len(pages)
    parts = []
    for page in pages[:MAX_PAGES]:
        parts.append((page.extract_text() or "").strip())

    return "\n\n".join(p for p in parts if p), total_pages


def process_attachments(attachments) -> tuple[str, str]:
    """Kembalikan (teks gabungan, ringkasan status untuk ditampilkan ke user).

    `attachments` berisi objek dengan atribut `name` dan `content_base64`.
    """
    texts: list[str] = []
    infos: list[str] = []
    remaining = MAX_TOTAL_CHARS

    for item in list(attachments)[:MAX_FILES]:
        name = (item.name or "lampiran").strip()[:120]

        try:
            data = base64.b64decode(item.content_base64, validate=True)
        except (binascii.Error, ValueError):
            infos.append(f"{name}: data lampiran rusak")
            continue

        if len(data) > MAX_FILE_BYTES:
            infos.append(f"{name}: terlalu besar (maks {MAX_FILE_BYTES // (1024 * 1024)} MB)")
            continue

        if not data.startswith(b"%PDF"):
            infos.append(f"{name}: format belum didukung (hanya PDF)")
            continue

        try:
            text, total_pages = _extract_pdf(data)
        except ValueError as e:
            infos.append(f"{name}: {e}")
            continue
        except Exception as e:
            infos.append(f"{name}: gagal dibaca ({type(e).__name__})")
            continue

        if not text.strip():
            infos.append(
                f"{name}: {total_pages} halaman tetapi tidak ada teks "
                "(kemungkinan hasil scan, belum bisa dibaca)"
            )
            continue

        truncated = len(text) > remaining
        text = text[:remaining]
        remaining -= len(text)
        texts.append(f"[Lampiran: {name}]\n{text}")

        note = f"{name}: {total_pages} halaman, {len(text)} karakter terbaca"
        if truncated or total_pages > MAX_PAGES:
            note += " (dipotong)"
        infos.append(note)

        if remaining <= 0:
            break

    return "\n\n".join(texts), "; ".join(infos)
