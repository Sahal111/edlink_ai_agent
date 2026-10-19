"""Robot browser EdLink (Playwright, headless) untuk dijalankan di server.

Memakai akun EdLink milik pengguna sendiri lewat halaman web biasa (tanpa
endpoint privat). Prinsip keselamatan:

- Username/password hanya dibaca dari environment (EDLINK_USERNAME /
  EDLINK_PASSWORD), tidak pernah dicatat di log, pesan, atau screenshot.
- Login gagal / captcha / OTP -> berhenti dan lapor; tidak mencoba berulang.
- Tombol Simpan hanya ditekan bila pemanggil meminta (press_save=True), yang
  hanya terjadi setelah pengguna menekan tombol persetujuan di Telegram.
- Kode DOM (pembacaan tugas, pengisian editor) memakai ulang
  extension/content.js yang sudah terbukti di EdLink asli.
"""

import base64
import json
import logging
import os
import re
import time
from contextlib import contextmanager
from pathlib import Path

logger = logging.getLogger("edlink.robot.browser")

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTENT_JS = REPO_ROOT / "extension" / "content.js"

DEFAULT_BASE_URL = "https://edlink.id"
NAV_TIMEOUT_MS = 30_000
MAX_ATTACHMENT_BYTES = 15 * 1024 * 1024

# Pola alamat detail tugas, mis. /panel/classes/12/groupteams/3/45
TASK_PATH = re.compile(r"/panel/classes/\d+/[^?#]+/\d+")

CHROME_SHIM = """(() => {
globalThis.chrome = globalThis.chrome || {};
chrome.runtime = chrome.runtime || {};
chrome.runtime.onMessage = { addListener(fn) { globalThis.__robotListener = fn; } };
globalThis.__robotCall = (msg) => new Promise((resolve) => {
  const keepOpen = globalThis.__robotListener(msg, {}, resolve);
  if (keepOpen !== true) resolve(null);
});
})()
"""


class RobotError(RuntimeError):
    """Kegagalan robot yang pesannya aman ditampilkan ke pengguna."""


class LoginBlocked(RobotError):
    """Login tidak bisa dilanjutkan otomatis (captcha, OTP, atau kredensial salah)."""


def base_url() -> str:
    return os.getenv("EDLINK_BASE_URL", DEFAULT_BASE_URL).rstrip("/")


def credentials_configured() -> bool:
    return bool(
        os.getenv("EDLINK_USERNAME", "").strip() and os.getenv("EDLINK_PASSWORD", "")
    )


def profile_dir() -> Path:
    custom = os.getenv("EDLINK_ROBOT_PROFILE")
    return Path(custom) if custom else REPO_ROOT / "data" / "robot_profile"


def proof_dir() -> Path:
    path = REPO_ROOT / "data" / "proofs"
    path.mkdir(parents=True, exist_ok=True)
    return path


# ---------------------------------------------------------------- sesi


@contextmanager
def open_session(headless: bool | None = None):
    """Buka browser dengan profil tersimpan lalu pastikan sudah login.

    Menghasilkan `page`. Profil disimpan di data/robot_profile agar sesi login
    bertahan antar-pekerjaan (jangan dibagikan/di-commit; berisi cookie sesi).
    """
    from playwright.sync_api import sync_playwright

    if headless is None:
        headless = os.getenv("EDLINK_ROBOT_HEADFUL", "").strip() not in ("1", "true")

    profile = profile_dir()
    profile.mkdir(parents=True, exist_ok=True)
    try:
        profile.chmod(0o700)
    except OSError:
        pass

    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(
            str(profile),
            headless=headless,
            viewport={"width": 1280, "height": 900},
            locale="id-ID",
            args=["--no-sandbox"] if os.getenv("EDLINK_ROBOT_NO_SANDBOX") else [],
        )
        context.set_default_timeout(NAV_TIMEOUT_MS)
        # Browser tanpa tampilan menolak izin notifikasi, sehingga EdLink menampilkan popup
        # yang menutupi halaman. Izinkan agar popup tidak muncul.
        try:
            context.grant_permissions(["notifications"], origin=base_url())
        except Exception:
            pass
        # Cookie sesi (tanpa tanggal kedaluwarsa) tidak ikut tersimpan di profil,
        # jadi disimpan/dipulihkan sendiri agar login tidak diulang tiap pekerjaan.
        state_file = profile / "cookies.json"
        if state_file.exists():
            try:
                context.add_cookies(json.loads(state_file.read_text()))
            except Exception:
                logger.info("Cookie tersimpan tidak bisa dipulihkan; login ulang")
        try:
            page = context.pages[0] if context.pages else context.new_page()
            ensure_logged_in(page)
            yield page
        finally:
            try:
                state_file.write_text(json.dumps(context.cookies()))
                state_file.chmod(0o600)
            except Exception:
                pass
            context.close()


def _visible_count(page, selector: str) -> int:
    try:
        return sum(1 for el in page.query_selector_all(selector) if el.is_visible())
    except Exception:
        return 0


def _blocker(page) -> str | None:
    """Kenali halaman yang butuh manusia: captcha atau kode verifikasi."""
    if page.query_selector(
        'iframe[src*="recaptcha"], iframe[src*="hcaptcha"], '
        'iframe[src*="turnstile"], .g-recaptcha, .h-captcha, [class*="captcha" i]'
    ):
        return "captcha"
    try:
        body = page.inner_text("body", timeout=2000)
    except Exception:
        return None
    if re.search(
        r"kode\s+(verifikasi|otp)|one[- ]time|verifikasi\s+(dua|2)\s+langkah",
        body,
        re.I,
    ):
        return "kode verifikasi (OTP)"
    return None


def _on_dashboard(page) -> bool:
    try:
        return bool(
            re.search(
                r"perlu\s+dikerjakan", page.inner_text("body", timeout=2000), re.I
            )
        )
    except Exception:
        return False


def ensure_logged_in(page) -> None:
    """Buka dasbor; bila diarahkan ke halaman login, login dengan akun dari env."""
    page.goto(f"{base_url()}/classes", wait_until="domcontentloaded")
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if _visible_count(page, 'input[type="password"]') or _on_dashboard(page):
            break
        page.wait_for_timeout(500)

    if not _visible_count(page, 'input[type="password"]'):
        if _on_dashboard(page):
            return  # sesi tersimpan masih berlaku
        raise RobotError(
            "Halaman EdLink tidak dikenali setelah dibuka (bukan login, bukan dasbor)."
        )

    if not credentials_configured():
        raise LoginBlocked(
            "EdLink meminta login tetapi EDLINK_USERNAME / EDLINK_PASSWORD belum diisi."
        )

    blocker = _blocker(page)
    if blocker:
        raise LoginBlocked(
            f"Halaman login meminta {blocker}; robot tidak bisa melanjutkan."
        )

    username = os.environ["EDLINK_USERNAME"].strip()
    password = os.environ["EDLINK_PASSWORD"]

    pwd_input = next(
        el
        for el in page.query_selector_all('input[type="password"]')
        if el.is_visible()
    )
    # Kolom username = input teks/email terlihat terdekat sebelum kolom password.
    user_input = page.evaluate_handle(
        """(pwd) => {
          const form = pwd.closest('form') || document;
          const cands = [...form.querySelectorAll(
            'input[type="text"], input[type="email"], input:not([type])'
          )].filter(e => e.offsetWidth || e.offsetHeight);
          return cands[0] || null;
        }""",
        pwd_input,
    ).as_element()
    if user_input is None:
        raise RobotError("Kolom username tidak ditemukan di halaman login.")

    user_input.fill(username)
    pwd_input.fill(password)
    submit = (
        page.query_selector('form button[type="submit"], form input[type="submit"]')
        or page.get_by_role(
            "button", name=re.compile(r"^(masuk|login|log in|sign in)$", re.I)
        ).first
    )
    try:
        submit.click()
    except Exception:
        pwd_input.press("Enter")

    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        if _on_dashboard(page):
            return
        blocker = _blocker(page)
        if blocker:
            raise LoginBlocked(
                f"EdLink meminta {blocker} setelah login; robot berhenti."
            )
        page.wait_for_timeout(700)

    if _visible_count(page, 'input[type="password"]'):
        raise LoginBlocked(
            "Login ditolak (username/password mungkin salah). Robot tidak mencoba lagi "
            "agar akun tidak terkunci."
        )
    # Bukan login, bukan dasbor: coba buka dasbor sekali lagi.
    page.goto(f"{base_url()}/classes", wait_until="domcontentloaded")
    page.wait_for_timeout(1500)
    if not _on_dashboard(page):
        raise RobotError("Login selesai tetapi dasbor 'Perlu Dikerjakan' tidak muncul.")


DISMISS_JS = """() => {
  const heading = [...document.querySelectorAll('*')].find(
    e => e.children.length === 0 && /notifikasi dinonaktifkan/i.test(e.textContent || ''));
  if (!heading) return false;
  let box = heading;
  for (let i = 0; i < 6 && box && !box.querySelector('button, [role="button"]'); i++) box = box.parentElement;
  const close = box && box.querySelector('button, [role="button"]');
  if (!close) return false;
  close.click();
  return true;
}"""

ADD_ANSWER_JS = """() => [...document.querySelectorAll('a, button, [role="button"], .button')]
  .some(el => (el.textContent || '').replace(/\\s+/g, ' ').trim().toLowerCase() === 'tambahkan jawaban')"""

def dismiss_popups(page) -> None:
    """Tutup popup 'Notifikasi Dinonaktifkan' dari EdLink bila muncul."""
    try:
        if page.evaluate(DISMISS_JS):
            page.wait_for_timeout(500)
    except Exception:
        pass


# ----------------------------------------------------------- pemindaian


def scan_tasks(page) -> list[dict]:
    """Daftar tugas pada bagian 'Perlu Dikerjakan': [{url, text}, ...]."""
    page.goto(f"{base_url()}/classes", wait_until="domcontentloaded")
    # Tunggu dasbor selesai dimuat (ada spinner dulu), bukan hanya jeda tetap.
    try:
        page.wait_for_function(
            "() => /perlu\\s+dikerjakan/i.test(document.body.innerText)", timeout=20_000
        )
    except Exception:
        pass
    page.wait_for_timeout(1500)
    dismiss_popups(page)
    found = page.evaluate("""() => {
          const header = [...document.querySelectorAll('*')].find(
            e => e.children.length === 0 && /^perlu dikerjakan$/i.test(e.textContent.trim()));
          if (!header) return null;
          let root = header;
          while (root && root !== document.body) {
            if (root.querySelectorAll('a[href*="/panel/classes/"]').length) break;
            root = root.parentElement;
          }
          if (!root) return [];
          return [...root.querySelectorAll('a[href]')].map(a => ({
            href: a.href, text: (a.textContent || '').replace(/\\s+/g, ' ').trim()
          }));
        }""")
    if found is None:
        raise RobotError("Bagian 'Perlu Dikerjakan' tidak ditemukan di dasbor.")

    seen, tasks = set(), []
    for item in found:
        href = item["href"].split("?")[0].split("#")[0]
        if TASK_PATH.search(href) and href not in seen:
            seen.add(href)
            tasks.append({"url": href, "text": item["text"][:200]})
    return tasks


# -------------------------------------------------------------- tugas


def _inject_content_script(page) -> None:
    if not CONTENT_JS.exists():
        raise RobotError("extension/content.js tidak ditemukan di server.")
    page.evaluate(CHROME_SHIM)
    page.evaluate("delete globalThis.__edlinkAssistantLoaded")
    page.add_script_tag(content=CONTENT_JS.read_text(encoding="utf-8"))


def _goto_task(page, url: str) -> None:
    # Alamat disimpan tanpa query, tetapi tugas kelompok butuh team_type agar halamannya terisi.
    if "/groupteams/" in url and "team_type=" not in url:
        url += "?team_type=group_team"
        page.goto(url, wait_until="domcontentloaded")
    # Halaman EdLink menampilkan spinner dulu; tunggu isi tugas benar-benar muncul.
    try:
        page.wait_for_selector("div.post-box", timeout=25_000)
    except Exception:
        pass
    page.wait_for_timeout(2000)
    dismiss_popups(page)


def read_task(page, url: str) -> dict:
    """Baca judul, deskripsi, batas waktu, dan lampiran PDF sebuah tugas."""
    _goto_task(page, url)
    try:
        page.wait_for_function(ADD_ANSWER_JS, timeout=15_000)
    except Exception:
        pass  # fillEditor akan melapor bila tombolnya memang tidak ada
    _inject_content_script(page)
    res = page.evaluate("(t) => globalThis.__robotCall({type: 'FILL_EDITOR', text: t})", text)
    task = (res or {}).get("task")
    if not task:
        info = page.evaluate("""() => ({
          path: location.pathname,
          title: document.title,
          postBox: !!document.querySelector('div.post-box'),
          labels: [...document.querySelectorAll('p')]
            .map(p => p.textContent.replace(/\\s+/g, ' ').trim())
            .filter(t => t && t.length < 40).slice(0, 15)
        })""")
        err = RobotError(
            "Detail tugas tidak terbaca (struktur halaman mungkin berubah)."
        )
        err.info = info
        err.proof = screenshot(page, "tidak-terbaca")
        raise err
    for line in res.get("log", []):
        logger.info("Lampiran: %s", line)

    attachments = list(task.get("attachments", []))
    # Lampiran tanpa tautan langsung: picu unduhan dengan klik dan tangkap file-nya.
    for target in res.get("clickTargets", []):
        try:
            with page.expect_download(timeout=12_000) as info:
                page.click(target["selector"])
            path = info.value.path()
            data = Path(path).read_bytes()
            if len(data) > MAX_ATTACHMENT_BYTES:
                logger.info("Lampiran %s dilewati: terlalu besar", target["name"])
                continue
            attachments.append(
                {
                    "name": target["name"],
                    "content_base64": base64.b64encode(data).decode(),
                }
            )
        except Exception as e:
            logger.info(
                "Lampiran %s gagal ditangkap: %s", target.get("name"), type(e).__name__
            )

    task["attachments"] = attachments
    return task


# ------------------------------------------------- mengisi & menyimpan


def screenshot(page, name: str) -> str:
    path = proof_dir() / f"{name}-{int(time.time())}.png"
    page.screenshot(path=str(path), full_page=True)
    return str(path)


SAVE_LABEL = re.compile(r"^simpan$", re.I)
FINAL_LABEL = re.compile(r"^(kumpulkan|serahkan|kirim|submit)( tugas| jawaban)?$", re.I)


def _modal(page):
    return page.locator(".modal.is-active").last


def fill_and_save(page, url: str, text: str, press_save: bool) -> dict:
    """Isi Teks Editor dengan `text`; tekan Simpan hanya bila press_save=True.

    Hasil: {ok, saved, reason, proof, final_button}. Bila press_save=False,
    editor ditutup lewat Batal sehingga tidak ada yang tersimpan.
    """
    _goto_task(page, url)
    _inject_content_script(page)
    res = page.evaluate(
        "(t) => globalThis.__robotCall({type: 'FILL_EDITOR', text: t})", text
    )
    if not res or not res.get("ok"):
        return {
            "ok": False,
            "saved": False,
            "reason": (res or {}).get("reason", "editor tidak merespons"),
            "proof": screenshot(page, "gagal-isi"),
        }

    if not press_save:
        proof = screenshot(page, "dryrun")
        cancel = _modal(page).get_by_role("button", name=re.compile(r"^batal$", re.I))
        try:
            cancel.first.click(timeout=4000)
        except Exception:
            pass
        return {
            "ok": True,
            "saved": False,
            "reason": "",
            "proof": proof,
            "final_button": "",
        }

    save = _modal(page).get_by_role("button", name=SAVE_LABEL)
    if save.count() == 0:
        return {
            "ok": False,
            "saved": False,
            "reason": 'tombol "Simpan" tidak ditemukan di jendela editor',
            "proof": screenshot(page, "tanpa-simpan"),
        }
    save.first.click()

    # Berhasil bila jendela editor tertutup; bila tetap terbuka, anggap gagal.
    closed = False
    for _ in range(40):
        if page.locator(".modal.is-active").count() == 0:
            closed = True
            break
        page.wait_for_timeout(500)
    page.wait_for_timeout(1000)
    proof = screenshot(page, "setelah-simpan")
    if not closed:
        return {
            "ok": False,
            "saved": False,
            "reason": "jendela editor tidak tertutup setelah Simpan (hasil tidak pasti)",
            "proof": proof,
        }
    return {
        "ok": True,
        "saved": True,
        "reason": "",
        "proof": proof,
        "final_button": _find_final_button(page),
    }


def _find_final_button(page) -> str:
    """Nama tombol pengumpulan terpisah (mis. Kumpulkan) bila ada, tanpa menekannya."""
    for el in page.query_selector_all('a, button, [role="button"], .button'):
        try:
            label = re.sub(r"\s+", " ", el.inner_text()).strip()
            if FINAL_LABEL.match(label) and el.is_visible():
                return label
        except Exception:
            continue
    return ""


def press_final(page, url: str, label: str) -> dict:
    """Tekan tombol pengumpulan terpisah (hanya setelah persetujuan kedua)."""
    _goto_task(page, url)
    for el in page.query_selector_all('a, button, [role="button"], .button'):
        try:
            if (
                re.sub(r"\s+", " ", el.inner_text()).strip() == label
                and el.is_visible()
            ):
                el.click()
                page.wait_for_timeout(2500)
                # Dialog konfirmasi bawaan situs, bila ada.
                for confirm in page.query_selector_all(".modal.is-active button"):
                    if re.match(
                        r"^(ya|kirim|kumpulkan|konfirmasi|ok)",
                        (confirm.inner_text() or "").strip(),
                        re.I,
                    ):
                        confirm.click()
                        page.wait_for_timeout(2000)
                        break
                return {"ok": True, "proof": screenshot(page, "final")}
        except Exception:
            continue
    return {
        "ok": False,
        "reason": f'tombol "{label}" tidak ditemukan',
        "proof": screenshot(page, "final-gagal"),
    }
