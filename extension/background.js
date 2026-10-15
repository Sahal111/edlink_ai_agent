const BACKEND = "http://127.0.0.1:8000";
const MAX_ATTACHMENT_BYTES = 15 * 1024 * 1024;
const CAPTURE_WAIT_MS = 12000;

function badge(tabId, text, color) {
  chrome.action.setBadgeText({ tabId, text });
  chrome.action.setBadgeBackgroundColor({ tabId, color });
}

async function extractTask(tabId) {
  try {
    return await chrome.tabs.sendMessage(tabId, { type: "EXTRACT_TASK" });
  } catch (e) {
    // Content script belum ada (tab dibuka sebelum extension dimuat/di-reload):
    // pasang sendiri lalu coba lagi.
    console.log("Content script belum ada, memasang otomatis:", e.message);
    await chrome.scripting.executeScript({
      target: { tabId },
      files: ["content.js"],
    });
    return await chrome.tabs.sendMessage(tabId, { type: "EXTRACT_TASK" });
  }
}

// Dijalankan di halaman (world MAIN), jadi harus berdiri sendiri tanpa variabel luar.
// Memicu klik pada tombol "Unduh" yang tidak punya alamat file, lalu menangkap
// file yang dibuat halaman (blob, XHR, fetch, atau window.open). Unduhan sungguhan
// ke folder Downloads diblokir selama penangkapan.
async function captureByClick(selector, maxBytes, waitMs) {
  const el = document.querySelector(selector);
  if (!el)
    return { ok: false, reason: "elemen Unduh tidak ditemukan", seen: {} };

  const seen = { requests: [], opened: [], anchors: [], blobs: [] };
  const undo = [];
  const realFetch =
    typeof window.fetch === "function" ? window.fetch.bind(window) : null;
  let captured = null;
  let pendingUrl = null;

  const pathOf = (u) => {
    try {
      const x = new URL(u, location.href);
      return x.origin + x.pathname;
    } catch {
      return "(url tidak valid)";
    }
  };
  const downloadType = (t) => /pdf|octet-stream|download/i.test(t || "");
  const take = (blob, via) => {
    if (!captured && blob && blob.size > 0) captured = { blob, via };
  };
  const patch = (obj, key, make) => {
    const orig = obj[key];
    if (typeof orig !== "function") return;
    obj[key] = make(orig);
    undo.push(() => {
      obj[key] = orig;
    });
  };
  const onBlockedAnchor = (a) => {
    const href = a.href || "";
    seen.anchors.push(
      `${a.hasAttribute("download") ? "download " : ""}${href.startsWith("blob:") ? "blob:" : pathOf(href)}`,
    );
    if (href.startsWith("blob:") || a.hasAttribute("download")) {
      if (!pendingUrl) pendingUrl = href;
    }
  };

  patch(
    URL,
    "createObjectURL",
    (orig) =>
      function (obj) {
        if (obj instanceof Blob) {
          seen.blobs.push(`${obj.type || "?"} ${obj.size}B`);
          if (!obj.type || downloadType(obj.type)) take(obj, "createObjectURL");
        }
        return orig.apply(this, arguments);
      },
  );

  patch(
    window,
    "fetch",
    (orig) =>
      function (input) {
        const url = typeof input === "string" ? input : input && input.url;
        seen.requests.push(`fetch ${pathOf(url)}`);
        const p = orig.apply(this, arguments);
        p.then((r) => {
          try {
            if (r.ok && downloadType(r.headers.get("content-type"))) {
              r.clone()
                .blob()
                .then((b) => take(b, "fetch"));
            }
          } catch {
            /* abaikan */
          }
        }).catch(() => {});
        return p;
      },
  );

  patch(
    XMLHttpRequest.prototype,
    "open",
    (orig) =>
      function (method, url) {
        seen.requests.push(`xhr ${method} ${pathOf(url)}`);
        return orig.apply(this, arguments);
      },
  );
  patch(
    XMLHttpRequest.prototype,
    "send",
    (orig) =>
      function () {
        this.addEventListener("load", () => {
          try {
            if (!downloadType(this.getResponseHeader("content-type"))) return;
            const r = this.response;
            if (r instanceof Blob) take(r, "xhr");
            else if (r instanceof ArrayBuffer)
              take(new Blob([r], { type: "application/pdf" }), "xhr");
          } catch {
            /* abaikan */
          }
        });
        return orig.apply(this, arguments);
      },
  );

  patch(
    window,
    "open",
    () =>
      function (url) {
        seen.opened.push(pathOf(url));
        if (url && !pendingUrl) pendingUrl = String(url);
        return null; // jangan buka tab/unduhan sungguhan
      },
  );

  patch(
    HTMLAnchorElement.prototype,
    "click",
    (orig) =>
      function () {
        if (this === el) return orig.apply(this, arguments);
        onBlockedAnchor(this); // blokir unduhan sungguhan
      },
  );

  // Anchor yang sudah ada di halaman lalu diklik lewat dispatchEvent.
  const onOtherAnchor = (e) => {
    const a = e.target && e.target.closest ? e.target.closest("a") : null;
    if (!a || a === el) return;
    if (a.hasAttribute("download") || (a.href || "").startsWith("blob:")) {
      onBlockedAnchor(a);
      e.preventDefault();
    }
  };
  // Cegah navigasi bawaan tombol bila href kosong dan halaman belum memanggil preventDefault.
  const stopNavigation = (e) => {
    const a = e.target && e.target.closest ? e.target.closest("a") : null;
    if (a === el) e.preventDefault();
  };
  window.addEventListener("click", onOtherAnchor, true);
  window.addEventListener("click", stopNavigation, false);
  el.addEventListener("click", stopNavigation, false);
  undo.push(() => {
    window.removeEventListener("click", onOtherAnchor, true);
    window.removeEventListener("click", stopNavigation, false);
    el.removeEventListener("click", stopNavigation, false);
  });

  try {
    el.dispatchEvent(
      new MouseEvent("click", {
        bubbles: true,
        cancelable: true,
        view: window,
      }),
    );

    const started = Date.now();
    while (!captured && Date.now() - started < waitMs) {
      await new Promise((r) => setTimeout(r, 200));
      if (!captured && pendingUrl && realFetch) {
        const url = pendingUrl;
        pendingUrl = null;
        try {
          const r = await realFetch(url);
          if (r.ok) take(await r.blob(), "url");
          else seen.requests.push(`ambil url: HTTP ${r.status}`);
        } catch (e) {
          seen.requests.push(`ambil url gagal: ${e.message}`);
        }
      }
    }
  } finally {
    for (const fn of undo) {
      try {
        fn();
      } catch {
        /* abaikan */
      }
    }
  }

  if (!captured) {
    return {
      ok: false,
      reason: "tidak ada file yang tertangkap setelah klik",
      seen,
    };
  }
  if (captured.blob.size > maxBytes) {
    return { ok: false, reason: "file terlalu besar", seen };
  }

  const bytes = new Uint8Array(await captured.blob.arrayBuffer());
  const magic = String.fromCharCode(...bytes.subarray(0, 4));
  if (magic !== "%PDF") {
    return {
      ok: false,
      reason: `bukan PDF (tipe ${captured.blob.type || "?"})`,
      seen,
    };
  }

  let binary = "";
  for (let i = 0; i < bytes.length; i += 0x8000) {
    binary += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  }
  return {
    ok: true,
    via: captured.via,
    size: bytes.length,
    content_base64: btoa(binary),
  };
}

async function captureViaClick(tabId, target) {
  const [injection] = await chrome.scripting.executeScript({
    target: { tabId },
    world: "MAIN",
    func: captureByClick,
    args: [target.selector, MAX_ATTACHMENT_BYTES, CAPTURE_WAIT_MS],
  });
  return injection?.result;
}

chrome.action.onClicked.addListener(async (tab) => {
  try {
    const res = await extractTask(tab.id);
    for (const line of res?.log || []) console.log("Lampiran:", line);
    const task = res?.task;
    if (!task) {
      console.log("Tugas tidak ditemukan di halaman ini");
      badge(tab.id, "?", "#888888");
      return;
    }

    // Lampiran yang unduhannya dibuat oleh JavaScript halaman.
    for (const target of res.clickTargets || []) {
      try {
        const r = await captureViaClick(tab.id, target);
        if (r?.ok) {
          task.attachments.push({
            name: target.name,
            content_base64: r.content_base64,
          });
          console.log(
            `Lampiran: ${target.name}: tertangkap lewat klik (${r.via}, ${Math.round(r.size / 1024)} KB)`,
          );
        } else {
          console.log(
            `Lampiran: ${target.name}: gagal ditangkap (${r?.reason || "tanpa hasil"})`,
            r?.seen,
          );
        }
      } catch (e) {
        console.log(
          `Lampiran: ${target.name}: error saat menangkap (${e.message})`,
        );
      }
    }

    const resp = await fetch(`${BACKEND}/tasks`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(task),
    });
    const data = await resp.json();
    console.log("Respons backend:", resp.status, data);

    if (!resp.ok) {
      badge(tab.id, "ERR", "#d93025");
    } else if (data.created) {
      badge(tab.id, "OK", "#188038"); // tugas baru tersimpan
    } else {
      badge(tab.id, "=", "#f9ab00"); // sudah pernah tersimpan
    }
  } catch (e) {
    console.log("Gagal:", e.message);
    badge(tab.id, "ERR", "#d93025");
  }
});
