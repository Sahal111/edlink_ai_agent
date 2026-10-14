chrome.action.onClicked.addListener(async (tab) => {
  try {
    const res = await chrome.tabs.sendMessage(tab.id, { type: "EXTRACT_TASK" });
    console.log("Hasil ekstraksi (BELUM dikirim):", res?.task);
  } catch (e) {
    console.log("Gagal:", e.message);
  }
});
