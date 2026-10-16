from types import SimpleNamespace

from app.attachment_service import process_attachments

from conftest import b64, make_pdf


def att(name, data: bytes):
    return SimpleNamespace(name=name, content_base64=b64(data))


def test_text_pdf_is_extracted():
    text, info = process_attachments([att("a.pdf", make_pdf("Halo dunia"))])
    assert "Halo dunia" in text and "1 halaman" in info


def test_scanned_pdf_is_reported():
    text, info = process_attachments([att("scan.pdf", make_pdf(None))])
    assert text == "" and "tidak ada teks" in info


def test_non_pdf_and_broken_base64():
    bad = SimpleNamespace(name="y.pdf", content_base64="###")
    text, info = process_attachments([att("x.docx", b"PK\x03\x04"), bad])
    assert text == "" and "belum didukung" in info and "rusak" in info


def test_corrupt_pdf_does_not_crash():
    text, info = process_attachments([att("c.pdf", b"%PDF-1.4 sampah")])
    assert text == "" and "gagal dibaca" in info


def test_only_first_three_files_are_read():
    files = [att(f"{i}.pdf", make_pdf(f"File{i}")) for i in range(5)]
    text, _ = process_attachments(files)
    assert "File0" in text and "File2" in text and "File3" not in text
