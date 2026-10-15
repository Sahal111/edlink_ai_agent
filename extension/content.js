const MAX_ATTACHMENT_BYTES = 15 * 1024 * 1024;
const MAX_ATTACHMENTS = 3;
const ATTACHMENT_WAIT_MS = 3000;
const FILE_EXT = /\.(pdf|docx?|pptx?|xlsx?|png|jpe?g|gif|zip|rar|mp4|mp3)$/i;

function textOf(el) {
  return (el?.textContent || "").replace(/\s+/g, " ").trim();
}

function findLabel(box, labelText) {
  return [...box.querySelectorAll("p")].find((p) =>
    textOf(p).startsWith(labelText),
  );
}

function isPlaceholder(t) {
  return t === "" || t.toLowerCase().startsWith("tidak ada");
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function extractTask() {
  const box = document.querySelector("div.post-box");
  if (!box) return null;

  const title = textOf(box.querySelector("p.font-20.font-w-600"));
  const dueLabel = findLabel(box, "Batas tanggal");
  const descLabel = findLabel(box, "Deskripsi");
  if (!title || !dueLabel || !descLabel) return null; // belum selesai dirender

  const due = textOf(dueLabel.nextElementSibling);
  const desc = textOf(descLabel.nextElementSibling);

  return {
    title,
    due_text: isPlaceholder(due) ? "" : due,
    description: isPlaceholder(desc) ? "" : desc,
    url: location.origin + location.pathname, // tanpa query
  };
}

function waitForTask(timeoutMs = 10000) {
  return new Promise((resolve) => {
    const start = Date.now();
    const tick = () => {
      const task = extractTask();
      if (task || Date.now() - start > timeoutMs) return resolve(task);
      setTimeout(tick, 300);
    };
    tick();
  });
}

// ---------- Lampiran ----------

// Untuk log saja: tanpa query karena URL unduhan bisa memuat token.
function safeUrl(href) {
  try {
    const u = new URL(href, location.href);
    return u.origin + u.pathname;
  } catch {
    return "(url tidak valid)";
  }
}

function hasUsableHref(anchor) {
  const href = (anchor.getAttribute("href") || "").trim().toLowerCase();
  return (
    href !== "" && !href.startsWith("#") && !href.startsWith("javascript:")
  );
}

function findAttachmentAnchors() {
  const box = document.querySelector("div.post-box") || document.body;
  let root = box;
  let label = findLabel(root, "Lampiran");
  if (!label) {
    root = document.body;
    label = findLabel(root, "Lampiran");
  }
  if (!label) return { labelFound: false, anchors: [], unduh: [] };

  // Bagian berikutnya ("Anggota ...") menjadi batas akhir pencarian.
  let endLabel = findLabel(root, "Anggota");
  if (
    endLabel &&
    !(
      label.compareDocumentPosition(endLabel) & Node.DOCUMENT_POSITION_FOLLOWING
    )
  ) {
    endLabel = null;
  }

  const within = (el) =>
    Boolean(
      label.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING,
    ) &&
    (!endLabel ||
      Boolean(
        endLabel.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_PRECEDING,
      ));

  // 1) Baris lampiran bawaan EdLink: .post-attachment-item
  let anchors = [...root.querySelectorAll(".post-attachment-item")]
    .filter(within)
    .map((row) => row.querySelector("a"))
    .filter(Boolean);

  // 2) Cadangan: tautan atau tombol "Unduh" di antara label lampiran dan anggota
  if (anchors.length === 0) {
    anchors = [...root.querySelectorAll("a")].filter(
      (a) =>
        within(a) && (hasUsableHref(a) || textOf(a).toLowerCase() === "unduh"),
    );
  }

  // Bahan diagnosis kalau tombol "Unduh" bukan elemen <a>.
  const unduh = [...root.querySelectorAll("*")]
    .filter(
      (el) => el.children.length === 0 && textOf(el).toLowerCase() === "unduh",
    )
    .map((el) => `${el.tagName}<${el.parentElement?.tagName || "?"}`);

  return { labelFound: true, anchors, unduh };
}

function nameFor(anchor, anchors) {
  const download = anchor.getAttribute("download");
  if (download) return download;

  // Struktur EdLink: nama file ada di .truncated-text pada baris lampiran.
  const row = anchor.closest(".post-attachment-item");
  const named = row?.querySelector(".truncated-text");
  if (named && textOf(named)) return textOf(named);

  let node = anchor;
  for (let i = 0; i < 4 && node; i++, node = node.parentElement) {
    // Berhenti naik kalau sudah mencakup lampiran lain.
    if (anchors.filter((l) => node.contains(l)).length > 1) break;
    const hit = [...node.querySelectorAll("*")].find(
      (el) =>
        el.children.length === 0 &&
        textOf(el).length < 200 &&
        FILE_EXT.test(textOf(el)),
    );
    if (hit) return textOf(hit);
  }

  try {
    const last = new URL(anchor.href, location.href).pathname.split("/").pop();
    if (last) return decodeURIComponent(last);
  } catch {
    /* abaikan */
  }
  return "lampiran";
}

function blobToBase64(blob) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(",")[1] || "");
    reader.onerror = () => reject(new Error("gagal membaca file"));
    reader.readAsDataURL(blob);
  });
}

async function collectAttachments() {
  const log = [];
  const attachments = [];
  const clickTargets = [];

  let found = findAttachmentAnchors();
  const started = Date.now();
  while (
    found.labelFound &&
    found.anchors.length === 0 &&
    Date.now() - started < ATTACHMENT_WAIT_MS
  ) {
    await sleep(300);
    found = findAttachmentAnchors();
  }

  if (!found.labelFound) {
    log.push("Bagian lampiran tidak ada di halaman ini");
    return { attachments, log, clickTargets };
  }

  if (found.anchors.length === 0) {
    log.push(
      "Bagian lampiran ada, tetapi tidak ada tautan yang bisa diunduh. " +
        `Elemen "Unduh" yang ditemukan: ${found.unduh.join(", ") || "tidak ada"}`,
    );
    return { attachments, log, clickTargets };
  }

  const list = found.anchors.slice(0, MAX_ATTACHMENTS);
  for (let i = 0; i < list.length; i++) {
    const anchor = list[i];
    const name = nameFor(anchor, found.anchors);

    // Hanya PDF yang bisa dibaca backend; lewati format lain yang jelas.
    if (FILE_EXT.test(name) && !/\.pdf$/i.test(name)) {
      log.push(`${name}: dilewati (hanya PDF yang didukung)`);
      continue;
    }

    // Tombol tanpa alamat file: unduhannya dibuat oleh JavaScript halaman.
    // background.js memicu klik dan menangkap hasilnya.
    if (!hasUsableHref(anchor)) {
      anchor.setAttribute("data-edlink-attach", String(i));
      clickTargets.push({ selector: `a[data-edlink-attach="${i}"]`, name });
      log.push(
        `${name}: tidak ada tautan langsung, unduhan akan dipicu lewat klik`,
      );
      continue;
    }

    const where = safeUrl(anchor.href);
    try {
      const resp = await fetch(anchor.href);
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);

      const declared = Number(resp.headers.get("content-length") || 0);
      if (declared > MAX_ATTACHMENT_BYTES)
        throw new Error("file terlalu besar");

      const blob = await resp.blob();
      if (blob.size > MAX_ATTACHMENT_BYTES)
        throw new Error("file terlalu besar");

      attachments.push({ name, content_base64: await blobToBase64(blob) });
      log.push(
        `${name}: diunduh ${Math.round(blob.size / 1024)} KB dari ${where}`,
      );
    } catch (e) {
      log.push(`${name}: gagal diunduh dari ${where} (${e.message})`);
    }
  }

  return { attachments, log, clickTargets };
}

// Cegah listener ganda kalau script ini dipasang lagi oleh background.js.
if (!globalThis.__edlinkAssistantLoaded) {
  globalThis.__edlinkAssistantLoaded = true;

  chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
    if (msg?.type === "EXTRACT_TASK") {
      (async () => {
        const task = await waitForTask();
        if (!task)
          return sendResponse({ task: null, log: [], clickTargets: [] });

        const { attachments, log, clickTargets } = await collectAttachments();
        sendResponse({ task: { ...task, attachments }, log, clickTargets });
      })().catch((e) =>
        sendResponse({
          task: null,
          log: [`error: ${e.message}`],
          clickTargets: [],
        }),
      );
      return true; // respons asinkron
    }
  });
}
