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

// Cegah listener ganda kalau script ini dipasang lagi oleh background.js.
if (!globalThis.__edlinkAssistantLoaded) {
  globalThis.__edlinkAssistantLoaded = true;

  chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
    if (msg?.type === "EXTRACT_TASK") {
      waitForTask().then((task) => sendResponse({ task }));
      return true; // respons asinkron
    }
  });
}
