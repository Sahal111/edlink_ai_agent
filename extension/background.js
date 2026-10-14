const BACKEND = "http://127.0.0.1:8000";

function badge(tabId, text, color) {
  chrome.action.setBadgeText({ tabId, text });
  chrome.action.setBadgeBackgroundColor({ tabId, color });
}

chrome.action.onClicked.addListener(async (tab) => {
  try {
    const res = await chrome.tabs.sendMessage(tab.id, { type: "EXTRACT_TASK" });
    const task = res?.task;
    if (!task) {
      console.log("Tugas tidak ditemukan di halaman ini");
      badge(tab.id, "?", "#888888");
      return;
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
