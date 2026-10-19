# EdLink AI Agent

Asisten pribadi untuk tugas kuliah di EdLink. Extension Chrome membaca tugas dari halaman EdLink, backend lokal menyimpannya (tanpa duplikat), Gemini membuat draf jawaban, lalu bot Telegram meminta persetujuan Anda. Setelah Anda setuju, extension menempelkan draf ke **Teks Editor** EdLink. **Anda sendiri yang menekan Simpan/Kumpulkan** — tidak ada yang dikirim otomatis.

Dokumentasi teknis dan hasil audit: [PROJECT_GUIDE.md](PROJECT_GUIDE.md).

## Alur

1. Buka halaman tugas di EdLink (`https://edlink.id/panel/classes/...`), klik ikon extension.
2. Tugas tersimpan di backend → Telegram mengirim ringkasan dan draf AI.
3. Di Telegram tekan **Setuju**, **Revisi** (ketik instruksi), atau **Batal**.
4. Setelah **Setuju**, kembali ke halaman tugas di EdLink dan klik ikon extension lagi. Draf ditempel ke Teks Editor.
5. Periksa isinya, lalu tekan **Simpan** sendiri di EdLink.

Badge ikon extension: `OK` tugas baru tersimpan · `=` sudah pernah tersimpan · `FILL` draf sudah ditempel · `WAIT` belum disetujui · `?` tugas tidak terbaca · `ERR` gagal.

## Prasyarat

- Python 3.10+ (diuji 3.13)
- Google Chrome / Chromium
- Kunci API Gemini dan nama model yang tersedia untuk kunci Anda
- Bot Telegram (buat lewat @BotFather)

## Menjalankan backend

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                 # lalu isi nilainya
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

- Cek kesehatan: `curl http://127.0.0.1:8000/health`
- Dokumentasi API interaktif: http://127.0.0.1:8000/docs
- Jangan pakai `--reload` selama Telegram aktif (dua proses polling menyebabkan error 409).
- Jangan ubah `--host` ke `0.0.0.0` kecuali `EDLINK_API_TOKEN` sudah diisi.

### Konfigurasi `backend/.env`

| Variabel | Fungsi |
|----------|--------|
| `GEMINI_API_KEY`, `GEMINI_MODEL` | Wajib untuk draf AI. Daftar model: `python list_models.py` |
| `GEMINI_FALLBACK_MODELS` | Opsional, model cadangan dipisah koma |
| `TELEGRAM_BOT_TOKEN` | Token bot dari BotFather |
| `TELEGRAM_ALLOWED_CHAT_ID` | Hanya chat ini yang dilayani. Kosongkan dulu, kirim `/start` ke bot, salin ID yang dibalas, isi, lalu restart |
| `EDLINK_API_TOKEN` | Disarankan. Bila diisi, semua endpoint `/tasks*` wajib header `X-API-Token` |
| `LOG_LEVEL`, `EDLINK_DB_PATH` | Opsional |

## Robot browser (tanpa laptop, untuk VPS)

Robot membuka EdLink di browser tanpa tampilan (Playwright/Chromium) dengan akunmu sendiri,
sehingga alurnya bisa penuh dari Telegram: tugas baru -> notifikasi + draf AI -> kamu tekan
**Setuju** -> robot mengisi Teks Editor dan menekan Simpan -> screenshot bukti dikirim ke Telegram.

Mode (`EDLINK_ROBOT_MODE` di `backend/.env`):

| Mode | Yang dilakukan |
|---|---|
| `off` | robot mati (default; alur lama dengan extension tetap jalan) |
| `scan` | memindai "Perlu Dikerjakan" tiap `EDLINK_POLL_MINUTES`, kirim tugas baru ke Telegram |
| `dryrun` | seperti scan; saat Setuju, robot mengisi editor lalu **Batal** (tidak ada yang tersimpan) + screenshot |
| `send` | seperti scan; saat Setuju, robot mengisi editor dan **menekan Simpan** + screenshot |

Urutan yang disarankan: `scan` -> `dryrun` -> `send`. Tombol Setuju di Telegram adalah satu-satunya
persetujuan; di mode `send` labelnya "Setuju & kirim ke EdLink". Bila setelah Simpan ada tombol
pengumpulan terpisah (mis. "Kumpulkan"), robot **tidak** menekannya sendiri: kamu diberi tombol
Telegram kedua.

Perintah Telegram: `/cek` (pindai sekarang), `/robot` (status), `/robot lanjut` (lanjutkan setelah dijeda).

Keamanan: username/password hanya dari `.env` (jangan commit; di VPS `chmod 600 backend/.env`), tidak
dicatat di log atau pesan. Bila login ditolak, ada captcha, atau diminta kode OTP, robot **berhenti dan
menjeda diri** (tidak mencoba berulang agar akun tidak terkunci) sampai kamu kirim `/robot lanjut`.
Sesi login disimpan di `data/robot_profile/` (berisi cookie; jangan dibagikan).

Pasang di VPS (Ubuntu):

```bash
sudo apt update && sudo apt install -y python3-venv git
git clone https://github.com/Sahal111/edlink_ai_agent.git && cd edlink_ai_agent/backend
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt -r requirements-robot.txt
playwright install --with-deps chromium
cp .env.example .env && chmod 600 .env && nano .env   # isi GEMINI, TELEGRAM, EDLINK_*; mode scan dulu
uvicorn app.main:app --host 127.0.0.1 --port 8000      # uji dulu di terminal
```

Agar jalan terus, buat `/etc/systemd/system/edlink-agent.service`:

```ini
[Unit]
Description=EdLink AI Agent
After=network-online.target

[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/edlink_ai_agent/backend
ExecStart=/home/ubuntu/edlink_ai_agent/backend/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
Restart=always

[Install]
WantedBy=multi-user.target
```

lalu `sudo systemctl enable --now edlink-agent` dan `journalctl -u edlink-agent -f` untuk log.
Port 8000 sengaja hanya di 127.0.0.1; jangan dibuka ke internet.

Yang BELUM terbukti di EdLink asli (hanya diuji di situs tiruan): halaman login, bentuk daftar
"Perlu Dikerjakan", dan apa yang terjadi setelah Simpan. Karena itu mulai dari `scan` dan `dryrun`.

## Memasang extension

1. Chrome → `chrome://extensions` → aktifkan **Developer mode** → **Load unpacked** → pilih folder `extension/`.
2. Bila `EDLINK_API_TOKEN` diisi, simpan token yang sama di extension. Buka `chrome://extensions` → klik **service worker** pada EdLink Assistant → di Console jalankan:
   ```js
   chrome.storage.local.set({ apiToken: "ISI_TOKEN_ANDA" })
   ```
3. Buka halaman tugas di EdLink dan klik ikon extension.

## Menjalankan test

```bash
cd backend
pip install -r requirements-dev.txt
python -m pytest -q
```

Test backend memakai database sementara dan pengganti (mock) untuk Gemini dan Telegram, jadi tidak memanggil layanan eksternal. Fungsi pengisian editor di extension diuji terpisah, lihat `extension/test/`.

## Endpoint

| Method & path | Fungsi |
|---------------|--------|
| `GET /health` | Cek hidup (tanpa token) |
| `POST /tasks` | Simpan tugas dari extension, deteksi duplikat |
| `GET /tasks` | Daftar tugas |
| `POST /tasks/{id}/draft` | Buat draf AI manual (ditolak 409 bila tugas sudah approved/filled/cancelled) |
| `GET /tasks/{id}/approved-draft` | Draf yang sudah disetujui (dipakai extension) |
| `POST /tasks/{id}/filled` | Extension melapor draf sudah ditempel ke editor |

## Batasan saat ini

- Hanya lampiran PDF berisi teks (tanpa OCR untuk PDF hasil scan).
- Satu pengguna, satu chat Telegram, backend lokal.
- Pengisian editor diuji pada halaman tiruan; perilaku pada EdLink asli perlu dicoba langsung (lihat PROJECT_GUIDE.md).
- File kosong `models.py`, `edlink_service.py`, `whatsapp_service.py`, `popup.html` adalah sisa rencana awal dan belum dipakai.
