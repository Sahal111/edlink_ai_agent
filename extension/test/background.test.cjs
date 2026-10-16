// Menguji alur background.js (klik ikon -> POST /tasks -> isi editor -> lapor filled)
// dengan chrome.* dan fetch palsu. Tanpa browser dan tanpa jaringan.
//
// Jalankan:  node extension/test/background.test.cjs
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const code = fs.readFileSync(path.join(__dirname, "..", "background.js"), "utf8");

async function scenario({ post, approvedDraft, fill, token }) {
  const calls = [];
  const badges = [];
  let listener;

  const chrome = {
    action: {
      onClicked: { addListener: (fn) => (listener = fn) },
      setBadgeText: ({ text }) => badges.push(text),
      setBadgeBackgroundColor: () => {},
    },
    storage: { local: { get: async () => (token ? { apiToken: token } : {}) } },
    scripting: { executeScript: async () => [] },
    tabs: {
      sendMessage: async (_id, msg) => {
        if (msg.type === "EXTRACT_TASK") {
          return {
            task: { title: "T", description: "d", due_text: "", url: "https://edlink.id/panel/classes/1/x", attachments: [] },
            log: [],
            clickTargets: [],
          };
        }
        if (msg.type === "FILL_EDITOR") {
          calls.push({ fill: msg.text });
          return fill;
        }
      },
    },
  };

  const fetchStub = async (url, opts = {}) => {
    const route = `${opts.method || "GET"} ${url.replace("http://127.0.0.1:8000", "")}`;
    calls.push({ route, token: (opts.headers || {})["X-API-Token"] });
    const reply = (status, body) => ({ ok: status < 400, status, json: async () => body });
    if (route === "POST /tasks") return reply(post.status || 200, post.body);
    if (route.endsWith("/approved-draft")) return approvedDraft ? reply(200, { draft: approvedDraft }) : reply(409, {});
    if (route.endsWith("/filled")) return reply(200, { changed: true });
    return reply(404, {});
  };

  vm.runInNewContext(code, { chrome, fetch: fetchStub, console: { log() {} }, setTimeout, btoa, URL });
  await listener({ id: 7 });
  return { calls, badges };
}

let failed = 0;
function check(name, ok, detail = "") {
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${ok ? "" : "  [" + detail + "]"}`);
  if (!ok) failed++;
}
const routes = (r) => r.calls.filter((c) => c.route).map((c) => c.route);

(async () => {
  let r = await scenario({ post: { body: { created: true, task_id: 1, status: "detected" } } });
  check("tugas baru -> badge OK, tidak mengisi editor", r.badges.at(-1) === "OK" && !r.calls.some((c) => c.fill));

  r = await scenario({ post: { body: { created: false, task_id: 1, status: "draft_ready" } } });
  check("belum disetujui -> badge WAIT, tidak mengisi", r.badges.at(-1) === "WAIT" && !r.calls.some((c) => c.fill));

  r = await scenario({ post: { body: { created: false, task_id: 1, status: "cancelled" } } });
  check("dibatalkan -> badge =", r.badges.at(-1) === "=");

  r = await scenario({ post: { body: { created: false, task_id: 5, status: "approved" } }, approvedDraft: "ISI DRAF", fill: { ok: true, chars: 8 } });
  check("approved -> ambil draf, isi editor, lapor filled, badge FILL",
    r.badges.at(-1) === "FILL" &&
      r.calls.some((c) => c.fill === "ISI DRAF") &&
      routes(r).join("|") === "POST /tasks|GET /tasks/5/approved-draft|POST /tasks/5/filled",
    routes(r).join("|"));

  r = await scenario({ post: { body: { created: false, task_id: 5, status: "filled" } }, approvedDraft: "ISI DRAF", fill: { ok: true, chars: 8 } });
  check("sudah filled -> isi ulang tanpa lapor filled lagi", r.badges.at(-1) === "FILL" && !routes(r).some((x) => x.endsWith("/filled")));

  r = await scenario({ post: { body: { created: false, task_id: 5, status: "approved" } }, approvedDraft: "ISI DRAF", fill: { ok: false, reason: "x" } });
  check("gagal mengisi editor -> badge ERR dan TIDAK lapor filled", r.badges.at(-1) === "ERR" && !routes(r).some((x) => x.endsWith("/filled")));

  r = await scenario({ post: { body: { created: false, task_id: 5, status: "approved" } }, approvedDraft: null, fill: { ok: true } });
  check("draf tidak tersedia -> badge ERR, editor tidak disentuh", r.badges.at(-1) === "ERR" && !r.calls.some((c) => c.fill));

  r = await scenario({ post: { status: 500, body: {} } });
  check("backend error -> badge ERR", r.badges.at(-1) === "ERR");

  r = await scenario({ post: { body: { created: false, task_id: 5, status: "approved" } }, approvedDraft: "D", fill: { ok: true, chars: 1 }, token: "tok-uji" });
  check("token dari storage dikirim di setiap request API", r.calls.filter((c) => c.route).every((c) => c.token === "tok-uji"));

  r = await scenario({ post: { body: { created: true, task_id: 1, status: "detected" } } });
  check("tanpa token tersimpan -> tidak ada header token", r.calls.filter((c) => c.route).every((c) => c.token === undefined));

  console.log(failed ? `\n${failed} pemeriksaan GAGAL` : "\nSemua pemeriksaan lulus");
  process.exit(failed ? 1 : 0);
})();
