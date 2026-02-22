/* LYRA User UI logic
   - Keeps UI layout identical; only adds behavior.
   - Features: persistent chat log, Clear, Copy, speech lang selector, stable dictation, TTS toggle, smart autoscroll,
     Enter=send, Shift+Enter=newline, stop dictation on send.
*/

(function () {
  "use strict";

  const STORAGE_KEY = "lyra_user_chat_v1";

  let web = false;

  // Speech state
  let rec = null;
  let micIsOn = false;
  let micBaseText = "";
  let micInterim = "";
  let micLastUpdateTs = 0;

  let ttsIsOn = false;
  let currentUtterance = null;

  // Chat memory in-page + localStorage
  /** @type {{role:"user"|"assistant", content:string, ts:number}[]} */
  let chat = [];

  function $(id) { return document.getElementById(id); }

  function getSpeechLang() {
    const sel = $("speechLang");
    const v = (sel && sel.value) ? sel.value : "auto";
    if (v !== "auto") return v;

    // AUTO: prefer RO if page language is RO; otherwise browser language
    const docLang = (document.documentElement.getAttribute("lang") || "").toLowerCase();
    if (docLang.startsWith("ro")) return "ro-RO";

    const nav = (navigator.language || "").trim();
    return nav || "en-US";
  }

  function setMicButtonState(on) {
    const b = $("micBtn");
    if (!b) return;
    b.textContent = on ? "⏹️ Dictare ON" : "🎙️ Dictare";
  }

  function setSpeakButtonState(on) {
    const b = $("speakBtn");
    if (!b) return;
    b.textContent = on ? "⏹️ Stop" : "🔊 Citește";
  }

  function isNearBottom(scroller, thresholdPx = 60) {
    if (!scroller) return true;
    return (scroller.scrollTop + scroller.clientHeight) >= (scroller.scrollHeight - thresholdPx);
  }

  function renderChat() {
    const log = $("chatlog");
    if (!log) return;

    // preserve whether user is near bottom BEFORE we rerender/append
    const box = $("answerBox");
    const shouldScroll = isNearBottom(box);

    log.innerHTML = "";
    if (!chat.length) {
      const p = document.createElement("div");
      p.className = "small";
      p.textContent = "—";
      log.appendChild(p);
    } else {
      for (const m of chat) {
        const div = document.createElement("div");
        div.className = "msg " + m.role;

        const role = document.createElement("div");
        role.className = "role";
        role.textContent = m.role;

        const content = document.createElement("div");
        content.className = "content";
        content.textContent = m.content;

        div.appendChild(role);
        div.appendChild(content);
        log.appendChild(div);
      }
    }

    if (box && shouldScroll) {
      box.scrollTop = box.scrollHeight;
    }
  }

  function saveChat() {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(chat.slice(-500))); // keep last 500 messages as safety
    } catch (_) { /* ignore */ }
  }

  function loadChat() {
    try {
      const raw = localStorage.getItem(STORAGE_KEY);
      if (!raw) return;
      const parsed = JSON.parse(raw);
      if (Array.isArray(parsed)) {
        chat = parsed
          .filter(x => x && (x.role === "user" || x.role === "assistant") && typeof x.content === "string")
          .map(x => ({ role: x.role, content: x.content, ts: Number(x.ts) || Date.now() }));
      }
    } catch (_) { /* ignore */ }
  }

  function appendMsg(role, content) {
    chat.push({ role, content, ts: Date.now() });
    renderChat();
    saveChat();
  }

  async function postJSON(url, data) {
    const r = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data)
    });
    return { ok: r.ok, status: r.status, json: await r.json().catch(() => ({})) };
  }

  // --- Public API (used by inline onclick in HTML) ---

  window.toggleWeb = function toggleWeb() {
    web = !web;
    const w = $("w");
    if (w) w.textContent = web ? "ON" : "OFF";
  };

  window.send = async function send() {
    const qEl = $("q");
    if (!qEl) return;

    const q = (qEl.value || "").trim();
    if (!q) { alert("Scrie o întrebare."); return; }

    // Stop mic on send (prevents ghost dictation)
    if (micIsOn) {
      stopMic();
    }

    appendMsg("user", q);

    // Clear input (history stays visible)
    qEl.value = "";

    // Insert a visible "processing" assistant bubble to keep context
    const processingToken = "__LYRA_PROCESSING__";
    appendMsg("assistant", "Se procesează…");

    const res = await postJSON("/api/chat", { query: q, web: web });

    // If auth expired, redirect (keeps behavior)
    if (res.status === 401) { window.location.href = "/"; return; }

    // Replace last assistant "processing" message with answer (or error)
    const answer = res.ok
      ? (res.json.answer || res.json.error || "")
      : (`Eroare (${res.status})`);

    // Find last assistant message that equals processing
    for (let i = chat.length - 1; i >= 0; i--) {
      if (chat[i].role === "assistant" && chat[i].content === "Se procesează…") {
        chat[i].content = answer || "—";
        chat[i].ts = Date.now();
        break;
      }
    }
    renderChat();
    saveChat();
  };

  window.clearChat = function clearChat() {
    if (!confirm("Ștergi tot istoricul din pagină?")) return;
    chat = [];
    saveChat();
    renderChat();
    // also stop audio
    stopSpeak();
    stopMic();
  };

  window.copyLast = async function copyLast() {
    const last = [...chat].reverse().find(m => m.role === "assistant" && (m.content || "").trim());
    const text = last ? last.content : "";
    if (!text) { alert("Nu există încă un răspuns de copiat."); return; }

    try {
      await navigator.clipboard.writeText(text);
    } catch (_) {
      // fallback
      const ta = document.createElement("textarea");
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      document.execCommand("copy");
      document.body.removeChild(ta);
    }
  };

  function stopMic() {
    if (rec) {
      try { rec.onresult = null; rec.onerror = null; rec.onend = null; rec.stop(); } catch (_) { /* ignore */ }
    }
    rec = null;
    micIsOn = false;
    micBaseText = "";
    micInterim = "";
    setMicButtonState(false);
  }

  window.mic = function mic() {
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SR) { alert("Browserul nu suportă dictare pe acest dispozitiv."); return; }

    // Toggle off
    if (micIsOn) {
      stopMic();
      return;
    }

    const qEl = $("q");
    if (!qEl) return;

    micBaseText = (qEl.value || "").trim();
    micInterim = "";
    micLastUpdateTs = 0;

    rec = new SR();
    rec.lang = getSpeechLang();
    rec.interimResults = true;
    rec.continuous = true;

    micIsOn = true;
    setMicButtonState(true);

    rec.onresult = (e) => {
      let finalChunk = "";
      let interimChunk = "";

      for (let i = e.resultIndex; i < e.results.length; i++) {
        const transcript = (e.results[i][0] && e.results[i][0].transcript) ? e.results[i][0].transcript : "";
        if (e.results[i].isFinal) finalChunk += transcript;
        else interimChunk += transcript;
      }

      finalChunk = (finalChunk || "").trim();
      interimChunk = (interimChunk || "").trim();

      if (finalChunk) {
        micBaseText = (micBaseText ? (micBaseText + " ") : "") + finalChunk;
        micBaseText = micBaseText.trim();
      }
      micInterim = interimChunk;

      // small debounce to avoid "spamming" textarea updates
      const now = Date.now();
      if (now - micLastUpdateTs < 80) return;
      micLastUpdateTs = now;

      const base = micBaseText ? (micBaseText + " ") : "";
      qEl.value = (base + micInterim).trim();
    };

    rec.onerror = () => {
      // Do not spam user with errors; just stop.
      stopMic();
    };

    rec.onend = () => {
      // If user didn't explicitly stop, keep OFF state.
      stopMic();
    };

    try { rec.start(); } catch (_) { stopMic(); }
  };

  function stopSpeak() {
    if (!("speechSynthesis" in window)) return;
    try { window.speechSynthesis.cancel(); } catch (_) { /* ignore */ }
    ttsIsOn = false;
    currentUtterance = null;
    setSpeakButtonState(false);
  }

  window.speak = function speak() {
    if (!("speechSynthesis" in window)) { alert("Browserul nu suportă audio."); return; }

    // Toggle OFF
    if (ttsIsOn) {
      stopSpeak();
      return;
    }

    // Speak last assistant message
    const last = [...chat].reverse().find(m => m.role === "assistant" && (m.content || "").trim());
    const txt = last ? (last.content || "").trim() : "";
    if (!txt || txt === "—") return;

    stopSpeak(); // ensure clean start

    const u = new SpeechSynthesisUtterance(txt);
    u.lang = getSpeechLang();

    u.onend = () => {
      ttsIsOn = false;
      currentUtterance = null;
      setSpeakButtonState(false);
    };
    u.onerror = () => {
      ttsIsOn = false;
      currentUtterance = null;
      setSpeakButtonState(false);
    };

    ttsIsOn = true;
    currentUtterance = u;
    setSpeakButtonState(true);

    try { window.speechSynthesis.speak(u); } catch (_) { stopSpeak(); }
  };

  // --- Library / upload / contact / status (ported from original user.html) ---

  let selectedFile = "";

  window.downloadSelected = function downloadSelected() {
    if (!selectedFile) return;
    window.open("/api/library/download?name=" + encodeURIComponent(selectedFile), "_blank");
  };

  async function lib() {
    const ul = $("lib");
    if (!ul) return;

    ul.innerHTML = "<li>Se încarcă…</li>";
    const r = await fetch("/api/library");
    const j = await r.json().catch(() => ({ files: [] }));

    ul.innerHTML = "";
    (j.files || []).forEach(f => {
      const li = document.createElement("li");
      li.textContent = f;
      li.style.cursor = "pointer";
      li.onclick = async () => {
        selectedFile = f;
        const dlBtn = $("dlBtn");
        if (dlBtn) dlBtn.disabled = false;

        [...ul.querySelectorAll("li")].forEach(x => (x.style.color = "var(--muted)"));
        li.style.color = "var(--text)";

        const libView = $("libView");
        if (libView) libView.textContent = "Se încarcă…";

        const vr = await fetch("/api/library/view?name=" + encodeURIComponent(f));
        if (vr.ok) {
          const txt = await vr.text();
          if (libView) libView.textContent = txt || "—";
        } else {
          if (libView) libView.textContent =
            "Previzualizare indisponibilă pentru acest tip de fișier. Folosește Download.";
        }
      };
      ul.appendChild(li);
    });

    const libInfo = $("libInfo");
    if (libInfo) libInfo.textContent = `Fișiere: ${(j.files || []).length}`;
  }

  window.up = async function up() {
    const fileInput = $("f");
    const file = fileInput && fileInput.files ? fileInput.files[0] : null;
    if (!file) { alert("Alege un fișier."); return; }

    const fd = new FormData();
    fd.append("file", file);

    const r = await fetch("/api/user_upload", { method: "POST", body: fd });
    if (r.status === 401) { window.location.href = "/"; return; }
    const j = await r.json().catch(() => ({}));

    const upInfo = $("upInfo");
    if (upInfo) {
      upInfo.textContent = r.ok
        ? ("Trimis la admin ✅ (" + (j.saved_as || "") + ")")
        : ("Eroare: " + (j.error || r.status));
    }
  };

  window.sendContact = async function sendContact() {
    const info = $("cInfo");
    if (info) info.textContent = "Trimit…";

    const fd = new FormData();
    fd.append("name", ($("c_name") && $("c_name").value) ? $("c_name").value : "");
    fd.append("email", ($("c_email") && $("c_email").value) ? $("c_email").value : "");
    fd.append("message", ($("c_msg") && $("c_msg").value) ? $("c_msg").value : "");
    const f = $("c_file") && $("c_file").files ? $("c_file").files[0] : null;
    if (f) fd.append("file", f);

    const r = await fetch("/api/contact/message", { method: "POST", body: fd });
    if (r.status === 401) { window.location.href = "/"; return; }
    const j = await r.json().catch(() => ({}));

    if (info) info.textContent = r.ok ? "Mesaj trimis ✅" : ("Eroare: " + (j.error || r.status));

    if (r.ok) {
      const msg = $("c_msg");
      const file = $("c_file");
      if (msg) msg.value = "";
      if (file) file.value = "";
    }
  };

  async function status() {
    try {
      const r = await fetch("/api/status");
      const j = await r.json();
      const dot = $("dot");
      const st = $("statusText");
      if (dot) dot.className = "dot ok";
      if (st) st.textContent = `Online • DOCS:${j.docs} • Pending:${j.pending}`;
    } catch (_) {
      const dot = $("dot");
      const st = $("statusText");
      if (dot) dot.className = "dot bad";
      if (st) st.textContent = "Offline";
    }
  }

  // --- Keyboard UX: Enter=send, Shift+Enter=newline ---
  function installEnterToSend() {
    const q = $("q");
    if (!q) return;
    q.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        window.send();
      }
    });
  }

  // --- Boot ---
  function boot() {
    loadChat();
    renderChat();
    installEnterToSend();
    lib();
    status();
    setInterval(status, 3000);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();


// ------------------------------
// Admin Tools wiring (append-only)
// ------------------------------
// This block is intentionally isolated and runs ONLY if the Admin panel exists on the page.
// It does not change any existing chat/mic/TTS logic above.
(function () {
  try {
    const ingestBtn = document.getElementById('ingest');
    const uploadBtn = document.getElementById('upload');
    const fileInput = document.getElementById('file');
    const ingestStatus = document.getElementById('ingestStatus');
    const uploadStatus = document.getElementById('uploadStatus');
    const inboxBtn = document.getElementById('inboxRefresh');
    const inboxList = document.getElementById('inboxList');
    const inboxItem = document.getElementById('inboxItem');

    // If Admin elements are not present, do nothing.
    if (!ingestBtn && !uploadBtn && !inboxBtn) return;

    const setText = (el, txt) => { if (el) el.textContent = (txt == null ? '' : String(txt)); };

    async function doIngest(forceFull) {
      setText(ingestStatus, 'Ingest started...');
      try {
        const url = '/api/ingest' + (forceFull ? '?force_full=1' : '');
        const r = await fetch(url, { method: 'POST' });
        const j = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error((j && j.error) || ('HTTP ' + r.status));
        setText(ingestStatus, (j.message || 'OK') + (typeof j.chunks === 'number' ? (' (chunks=' + j.chunks + ')') : ''));
      } catch (e) {
        setText(ingestStatus, 'Ingest error: ' + (e && e.message ? e.message : String(e)));
      }
    }

    async function doUpload() {
      if (!fileInput) return;
      const files = (fileInput.files || []);
      if (!files.length) {
        setText(uploadStatus, 'Select a file first.');
        return;
      }
      setText(uploadStatus, 'Uploading...');
      let ok = 0, fail = 0;
      for (const f of files) {
        try {
          const fd = new FormData();
          fd.append('file', f, f.name);
          const r = await fetch('/admin/upload', { method: 'POST', body: fd });
          const j = await r.json().catch(() => ({}));
          if (!r.ok) throw new Error((j && j.error) || ('HTTP ' + r.status));
          ok++;
        } catch (e) {
          fail++;
        }
      }
      setText(uploadStatus, 'Upload done. ok=' + ok + ' fail=' + fail);
    }

    async function refreshInbox() {
      if (!inboxList) return;
      setText(inboxList, 'Loading...');
      try {
        const r = await fetch('/api/admin/inbox');
        const j = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error((j && j.error) || ('HTTP ' + r.status));
        const items = (j.items || []);
        if (!items.length) {
          inboxList.innerHTML = '<div class="muted">Inbox empty.</div>';
          setText(inboxItem, '');
          return;
        }
        inboxList.innerHTML = '';
        for (const it of items) {
          const row = document.createElement('div');
          row.className = 'inboxRow';
          const btn = document.createElement('button');
          btn.textContent = (it.ts || '') + ' — ' + (it.name || 'anonymous') + ' — ' + (it.email || '');
          btn.onclick = () => openInboxItem(it.id);
          row.appendChild(btn);
          inboxList.appendChild(row);
        }
      } catch (e) {
        setText(inboxList, 'Inbox error: ' + (e && e.message ? e.message : String(e)));
      }
    }

    async function openInboxItem(id) {
      if (!id || !inboxItem) return;
      setText(inboxItem, 'Loading...');
      try {
        const r = await fetch('/api/admin/inbox_item?id=' + encodeURIComponent(id));
        const j = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error((j && j.error) || ('HTTP ' + r.status));
        const msg = j.message || {};
        inboxItem.textContent = JSON.stringify(msg, null, 2);
      } catch (e) {
        setText(inboxItem, 'Item error: ' + (e && e.message ? e.message : String(e)));
      }
    }

    // Click ingest: normal => incremental; Shift+Click => full rebuild.
    if (ingestBtn) ingestBtn.addEventListener('click', (ev) => doIngest(!!(ev && ev.shiftKey)));
    if (uploadBtn) uploadBtn.addEventListener('click', doUpload);
    if (inboxBtn) inboxBtn.addEventListener('click', refreshInbox);

  } catch (_) {
    // never break the rest of the UI
  }
})();

