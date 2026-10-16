// Menguji fillEditor() di content.js pada halaman TIRUAN yang meniru struktur
// modal EdLink (dari screenshot DevTools). Ini BUKAN pengujian di EdLink asli.
//
// Jalankan:  npm i --no-save playwright && node extension/test/fill_editor.test.cjs
// (butuh Chromium; di sandbox ini dipakai /opt/pw-browsers)
const fs = require("fs");
const path = require("path");
const { chromium } = require("playwright");

const contentJs = fs.readFileSync(path.join(__dirname, "..", "content.js"), "utf8");

function mockPage({ mode = "prosemirror", prefill = "", addButton = true, startOpen = false }) {
  return `<!doctype html><meta charset="utf-8"><body>
<div class="post-box"><p class="font-20 font-w-600">TK#1_SysAdmin1</p></div>
${addButton ? '<a id="add" href="#">Tambahkan Jawaban</a>' : ""}
<div id="root"></div>
<script>
const MODE = ${JSON.stringify(mode)}, PREFILL = ${JSON.stringify(prefill)};
const root = document.getElementById("root");
function modal(inner) {
  const m = document.createElement("div");
  m.className = "modal is-active";
  m.innerHTML = '<div class="modal-card">' + inner + "</div>";
  root.appendChild(m);
  return m;
}
function head(t) {
  return '<header class="modal-card-head"><div class="modal-card-title"><p class="font-20 font-w-600">' + t + "</p></div></header>";
}
function openChoice() {
  const m = modal(head("Tambahkan Jawaban") + '<section class="modal-card-body"><div class="columns">' +
    '<div class="column is-6"><div class="is-centered has-text-centered option-container" id="file">Tambahkan File</div></div>' +
    '<div class="column is-6"><div class="is-centered has-text-centered option-container" id="te">Teks Editor</div></div></div></section>');
  m.querySelector("#file").onclick = () => { window.__fileClicked = true; };
  m.querySelector("#te").onclick = () => { m.remove(); openEditor(); };
}
function openEditor() {
  const m = modal(head("Teks Editor") + '<section class="modal-card-body"><div class="content"><div>' +
    '<div class="menubar buttons"></div><div class="content text-editor-surface"><div class="ProseMirror" contenteditable="true"></div></div></div>' +
    '<div class="is-pulled-right"><button id="cancel">Batal</button><button id="save">Simpan</button></div></div></section>');
  const ed = m.querySelector("[contenteditable]");
  ed.textContent = PREFILL;
  if (MODE === "prosemirror") {
    ed.addEventListener("paste", (e) => {
      e.preventDefault();
      const t = e.clipboardData.getData("text/plain");
      ed.innerHTML = "";
      t.split(/\\n+/).forEach((l) => { const p = document.createElement("p"); p.textContent = l; ed.appendChild(p); });
    });
  }
  if (MODE === "dead") {
    // Meniru editor terkelola yang membuang perubahan DOM di luar state-nya.
    const obs = new MutationObserver(() => {
      obs.disconnect();
      ed.innerHTML = "";
      obs.observe(ed, { childList: true, characterData: true, subtree: true });
    });
    obs.observe(ed, { childList: true, characterData: true, subtree: true });
  }
  m.querySelector("#save").onclick = () => { window.__saved = true; };
}
const add = document.getElementById("add");
if (add) add.onclick = (e) => { e.preventDefault(); openChoice(); };
if (${JSON.stringify(startOpen)}) openEditor();
</script></body>`;
}

const DRAFT = "Paragraf pertama jawaban.\n\nParagraf kedua dengan <tag> & simbol.";

async function run(browser, opts, text = DRAFT) {
  const page = await browser.newPage();
  await page.setContent(mockPage(opts));
  await page.evaluate(() => {
    window.chrome = { runtime: { onMessage: { addListener: (fn) => (window.__listener = fn) } } };
  });
  await page.addScriptTag({ content: contentJs });
  const result = await page.evaluate(
    (t) => new Promise((res) => window.__listener({ type: "FILL_EDITOR", text: t }, {}, res)),
    text,
  );
  const state = await page.evaluate(() => {
    const ed = document.querySelector("[contenteditable]");
    return {
      html: ed ? ed.innerHTML : null,
      text: ed ? ed.innerText : null,
      saved: Boolean(window.__saved),
      fileClicked: Boolean(window.__fileClicked),
    };
  });
  await page.close();
  return { result, state };
}

let failed = 0;
function check(name, ok, detail = "") {
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${ok ? "" : "  [" + detail + "]"}`);
  if (!ok) failed++;
}

(async () => {
  const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH || undefined });

  let r = await run(browser, { mode: "prosemirror" });
  check("editor tipe ProseMirror: berhasil", r.result.ok === true, JSON.stringify(r.result));
  check("dua paragraf terbentuk dan simbol HTML aman", (r.state.html.match(/<p>/g) || []).length === 2 && r.state.text.includes("<tag> & simbol"), r.state.html);
  check('tombol "Simpan" TIDAK ditekan', r.state.saved === false);
  check('opsi "Tambahkan File" TIDAK ditekan', r.state.fileClicked === false);

  r = await run(browser, { mode: "naive" });
  check("editor tanpa handler paste: fallback ketik berhasil", r.result.ok === true && r.state.text.includes("Paragraf kedua"), JSON.stringify(r));
  check('fallback juga tidak menekan "Simpan"', r.state.saved === false);

  r = await run(browser, { mode: "prosemirror", startOpen: true });
  check("editor sudah terbuka: langsung diisi", r.result.ok === true && r.state.text.includes("Paragraf pertama"), JSON.stringify(r.result));

  r = await run(browser, { mode: "prosemirror", prefill: "Jawaban lama saya" });
  check("editor berisi teks: TIDAK ditimpa", r.result.ok === false && /sudah berisi/.test(r.result.reason) && r.state.text === "Jawaban lama saya", JSON.stringify(r));

  r = await run(browser, { mode: "prosemirror", addButton: false });
  check('tanpa tombol "Tambahkan Jawaban": gagal dengan alasan jelas', r.result.ok === false && /Tambahkan Jawaban/.test(r.result.reason), JSON.stringify(r.result));

  r = await run(browser, { mode: "dead" });
  check("editor menolak input: dilaporkan gagal, bukan sukses palsu", r.result.ok === false && /tidak masuk/.test(r.result.reason), JSON.stringify(r.result));

  r = await run(browser, { mode: "prosemirror" }, "   ");
  check("draf kosong ditolak", r.result.ok === false && /kosong/.test(r.result.reason), JSON.stringify(r.result));

  await browser.close();
  console.log(failed ? `\n${failed} pemeriksaan GAGAL` : "\nSemua pemeriksaan lulus");
  process.exit(failed ? 1 : 0);
})();
