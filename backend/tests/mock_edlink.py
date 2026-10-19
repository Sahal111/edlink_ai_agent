"""Situs EdLink TIRUAN untuk menguji robot browser (bukan EdLink asli).

Struktur HTML mengikuti yang terlihat di EdLink asli (post-box, modal
.option-container, editor contenteditable), tetapi login, dasbor, dan perilaku
Simpan di sini hanya tebakan yang masuk akal.
"""

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

from conftest import make_pdf

TASK_PATH = "/panel/classes/1/groupteams/2/3"

STYLE = "<meta charset=utf-8><body>"


class Site:
    def __init__(self, *, user="mhs", password="rahasia-uji", captcha=False,
                 final_button=False, save_works=True, dashboard_tasks=1):
        self.user, self.password = user, password
        self.captcha, self.final_button = captcha, final_button
        self.save_works = save_works
        self.dashboard_tasks = dashboard_tasks
        self.logins = 0
        self.answers: list[str] = []
        self.finalized = 0
        self.server = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    def start(self):
        site = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, body, code=200, ctype="text/html; charset=utf-8", headers=()):
                data = body if isinstance(body, bytes) else body.encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                for k, v in headers:
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _authed(self):
                return "sid=ok" in (self.headers.get("Cookie") or "")

            def do_GET(self):
                path = self.path.split("?")[0]
                if path == "/classes":
                    if not self._authed():
                        return self._send("", 302, headers=[("Location", "/login")])
                    links = "".join(
                        f'<li><a href="{TASK_PATH}?team_type=group_team">Tugas {i}</a></li>'
                        if i == 0 else f'<li><a href="/panel/classes/1/groupteams/2/{3 + i}">Tugas {i}</a></li>'
                        for i in range(site.dashboard_tasks)
                    )
                    return self._send(
                        f"{STYLE}<h2>Kelas saya</h2><a href='/panel/classes/1/home'>Kelas A</a>"
                        f"<div><div><span>Perlu Dikerjakan</span></div><ul>{links}</ul></div>"
                    )
                if path == "/login":
                    cap = '<div class="g-recaptcha"></div>' if site.captcha else ""
                    return self._send(
                        f'{STYLE}<form method="post" action="/login"><input type="text" name="u">'
                        f'<input type="password" name="p">{cap}<button type="submit">Masuk</button></form>'
                    )
                if path == "/files/soal.pdf":
                    return self._send(make_pdf("Jelaskan tiga fungsi sistem operasi"), ctype="application/pdf")
                if path.startswith("/panel/classes/1/groupteams/2/"):
                    if not self._authed():
                        return self._send("", 302, headers=[("Location", "/login")])
                    return self._send(task_page(path, site))
                self._send("tidak ada", 404)

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length).decode()
                if self.path == "/login":
                    form = parse_qs(body)
                    site.logins += 1
                    if form.get("u", [""])[0] == site.user and form.get("p", [""])[0] == site.password:
                        return self._send("", 302, headers=[("Location", "/classes"), ("Set-Cookie", "sid=ok; Path=/")])
                    return self._send(f'{STYLE}<p>Login gagal</p><form method="post"><input type="text" name="u"><input type="password" name="p"><button>Masuk</button></form>')
                if self.path == "/save":
                    if site.save_works:
                        site.answers.append(parse_qs(body).get("t", [""])[0])
                    return self._send("ok")
                if self.path == "/final":
                    site.finalized += 1
                    return self._send("ok")
                self._send("tidak ada", 404)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


def task_page(path, site):
    final = '<button id="final" onclick="fetch(\'/final\',{method:\'POST\'})">Kumpulkan</button>' if site.final_button else ""
    answered = "<p>Jawaban: ada</p>" if site.answers else ""
    return f"""{STYLE}
<div class="post-box">
 <p class="font-20 font-w-600">TK#1 Sistem Operasi</p>
 <p>Batas tanggal</p><p>25 Okt 2026 23:59</p>
 <p>Deskripsi</p><p>Kerjakan soal pada lampiran.</p>
 <p>Lampiran</p>
 <div class="post-attachment-item"><span class="truncated-text">soal.pdf</span><a href="/files/soal.pdf">Unduh</a></div>
 <p>Anggota kelompok</p><p>Budi</p>
</div>
{answered}
<a id="add" href="#">Tambahkan Jawaban</a>{final}
<div id="root"></div>
<script>
const root = document.getElementById('root');
function modal(inner) {{ const m = document.createElement('div'); m.className = 'modal is-active';
  m.innerHTML = '<div class="modal-card">' + inner + '</div>'; root.appendChild(m); return m; }}
const head = t => '<header class="modal-card-head"><div class="modal-card-title"><p>' + t + '</p></div></header>';
document.getElementById('add').onclick = (e) => {{ e.preventDefault();
  const m = modal(head('Tambahkan Jawaban') + '<section class="modal-card-body">' +
   '<div class="option-container">Tambahkan File</div><div class="option-container" id="te">Teks Editor</div></section>');
  m.querySelector('#te').onclick = () => {{ m.remove();
    const e2 = modal(head('Teks Editor') + '<section class="modal-card-body"><div class="menubar buttons"></div>' +
     '<div class="content text-editor-surface"><div class="ProseMirror" contenteditable="true"></div></div>' +
     '<button id="cancel">Batal</button><button id="save">Simpan</button></section>');
    const ed = e2.querySelector('[contenteditable]');
    ed.addEventListener('paste', (ev) => {{ ev.preventDefault(); const t = ev.clipboardData.getData('text/plain');
      ed.innerHTML = ''; t.split(/\\n+/).forEach(l => {{ const p = document.createElement('p'); p.textContent = l; ed.appendChild(p); }}); }});
    e2.querySelector('#cancel').onclick = () => e2.remove();
    e2.querySelector('#save').onclick = async () => {{
      await fetch('/save', {{method: 'POST', body: 't=' + encodeURIComponent(ed.innerText)}}); e2.remove(); }};
  }};
}};
</script>"""
