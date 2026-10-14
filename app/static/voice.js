/* =============================================================================
   Ovozli intervyu — interfeys qatlami.

   Bu fayl butunlay mustaqil: index.html'dan faqat bitta <script> satri va
   `go()` ichidagi bitta hook chaqiruvi bilan bog'langan (VOICE:BEGIN/END
   belgilari orasida). Faylni o'chirish loyihaning qolgan qismiga ta'sir qilmaydi.

   Xizmatlar ishlamasa hech narsa ko'rsatilmaydi — matn bilan javob berish
   har doim ishlaydi.
   ========================================================================== */
(function () {
  "use strict";

  const V = {
    ready: false, stt: false, tts: false,
    autoplay: false, rec: null, chunks: [], audio: null,
    recStart: 0, recTimer: null, busy: false,
  };

  // Emoji o'rniga SVG: emoji shrifti yo'q tizimda bo'sh kvadrat chiqadi
  const ICON_SPEAK =
    '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<path d="M11 5 6 9H2v6h4l5 4V5z"/><path d="M15.5 8.5a5 5 0 0 1 0 7"/>' +
    '<path d="M18.5 5.5a9 9 0 0 1 0 13"/></svg>';
  const ICON_MIC =
    '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<rect x="9" y="2" width="6" height="11" rx="3"/>' +
    '<path d="M5 10v1a7 7 0 0 0 14 0v-1"/><path d="M12 18v4"/></svg>';

  const $$ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  function key() {
    try { return localStorage.getItem("ra_key"); } catch (_) { return null; }
  }
  function hdrs(json) {
    const h = json ? { "Content-Type": "application/json" } : {};
    const k = key();
    if (k) h["X-API-Key"] = k;
    return h;
  }

  /* ---------- uslublar (index.html'ga tegmaslik uchun shu yerda) ---------- */
  const CSS = `
  .v-bar { display:flex; align-items:center; gap:9px; flex-wrap:wrap; margin-top:12px;
    padding-top:12px; border-top:1px dashed var(--border); }
  .v-btn { appearance:none; border:1px solid var(--border); background:var(--surface);
    color:var(--text-dim); font:inherit; font-size:13px; padding:7px 13px;
    border-radius:8px; cursor:pointer; display:inline-flex; align-items:center; gap:7px; }
  .v-btn svg { flex:none; }
  .v-btn:hover:not(:disabled) { color:var(--text); border-color:var(--accent); }
  .v-btn:disabled { opacity:.5; cursor:not-allowed; }
  .v-btn.rec { background:var(--red-soft); color:var(--red); border-color:transparent; }
  .v-btn.play { background:var(--accent-soft); color:var(--accent); border-color:transparent; }
  .v-dot { width:9px; height:9px; border-radius:50%; background:currentColor; flex:none; }
  .v-dot.pulse { animation:vpulse 1s ease-in-out infinite; }
  @keyframes vpulse { 0%,100%{opacity:1} 50%{opacity:.25} }
  .v-note { font-size:12px; color:var(--text-faint); }
  .v-note.warn { color:var(--amber); }
  .v-note.err { color:var(--red); }
  .v-toggle { display:inline-flex; align-items:center; gap:6px; font-size:12.5px;
    color:var(--text-dim); cursor:pointer; user-select:none; }
  .v-badge { font-size:11px; padding:2px 8px; border-radius:999px;
    background:var(--surface-2); color:var(--text-faint); }
  `;

  function injectCss() {
    if ($$("ra-voice-css")) return;
    const st = document.createElement("style");
    st.id = "ra-voice-css";
    st.textContent = CSS;
    document.head.appendChild(st);
  }

  /* ---------- ishga tushirish ---------- */
  async function init() {
    try {
      const r = await fetch("/voice/status", { headers: hdrs(false) });
      if (!r.ok) return;
      const s = await r.json();
      if (!s.enabled) return;
      V.stt = !!(s.stt && s.stt.up);
      V.tts = !!(s.tts && s.tts.up);
      V.sttDetail = s.stt && s.stt.detail;
      V.ready = V.stt || V.tts;
      if (V.ready) injectCss();
    } catch (_) { /* ovoz xizmati yo'q — jim qolamiz */ }
  }

  /* ---------- savolni o'qib berish ---------- */
  function stopAudio() {
    if (V.audio) { V.audio.pause(); V.audio.currentTime = 0; V.audio = null; }
  }

  async function speak(sid, qid, btn) {
    stopAudio();
    const old = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = '<span class="spin"></span> tayyorlanmoqda…';
    try {
      const r = await fetch(`/voice/question/${sid}/${qid}`, { headers: hdrs(false) });
      if (!r.ok) {
        const d = await r.json().catch(() => ({}));
        throw new Error(d.detail || `HTTP ${r.status}`);
      }
      const url = URL.createObjectURL(await r.blob());
      const a = new Audio(url);
      V.audio = a;
      btn.classList.add("play");
      btn.innerHTML = '<span class="v-dot pulse"></span> to‘xtatish';
      btn.disabled = false;
      btn.onclick = () => { stopAudio(); reset(); };
      const reset = () => {
        btn.classList.remove("play");
        btn.innerHTML = old;
        btn.onclick = () => speak(sid, qid, btn);
        URL.revokeObjectURL(url);
      };
      a.onended = reset;
      a.onerror = reset;
      await a.play();
    } catch (e) {
      btn.innerHTML = old;
      btn.disabled = false;
      note(`Savolni o‘qib bo‘lmadi: ${e.message}`, "err");
    }
  }

  /* ---------- ovozli javob ---------- */
  function fmt(sec) {
    const m = String(Math.floor(sec / 60)).padStart(2, "0");
    const s = String(sec % 60).padStart(2, "0");
    return `${m}:${s}`;
  }

  async function startRec(sid, qid, btn) {
    if (!window.isSecureContext) {
      note("Mikrofon faqat xavfsiz ulanishda ishlaydi. http://localhost orqali " +
           "oching (SSH tunnel) yoki HTTPS sozlang.", "warn");
      return;
    }
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      note("Brauzer mikrofonni qo‘llamaydi.", "err");
      return;
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const rec = new MediaRecorder(stream);
      V.rec = rec; V.chunks = [];
      rec.ondataavailable = (e) => { if (e.data.size) V.chunks.push(e.data); };
      rec.onstop = () => {
        stream.getTracks().forEach((t) => t.stop());
        upload(sid, qid, btn, new Blob(V.chunks, { type: rec.mimeType || "audio/webm" }));
      };
      rec.start();
      V.recStart = Date.now();
      btn.classList.add("rec");
      btn.onclick = () => stopRec(btn);
      const tick = () => {
        const sec = Math.floor((Date.now() - V.recStart) / 1000);
        btn.innerHTML = `<span class="v-dot pulse"></span> yozilmoqda ${fmt(sec)} — to‘xtatish`;
      };
      tick();
      V.recTimer = setInterval(tick, 1000);
      note("Gapiring. Tugatgach “to‘xtatish” ni bosing.", "");
    } catch (e) {
      note(`Mikrofonga ruxsat berilmadi: ${e.message}`, "err");
    }
  }

  function stopRec(btn) {
    clearInterval(V.recTimer);
    if (V.rec && V.rec.state !== "inactive") V.rec.stop();
    btn.classList.remove("rec");
    btn.innerHTML = '<span class="spin"></span> matnga o‘girilmoqda…';
    btn.disabled = true;
  }

  async function upload(sid, qid, btn, blob) {
    const fd = new FormData();
    fd.append("voice", blob, "javob.webm");
    try {
      const r = await fetch(`/voice/answer/${sid}/${qid}`, {
        method: "POST", headers: hdrs(false), body: fd,
      });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(d.detail || `HTTP ${r.status}`);

      const ta = $$("ivAns");
      if (ta) {
        ta.value = d.answer || d.text || ta.value;
        ta.dispatchEvent(new Event("input", { bubbles: true }));
      }
      const w = d.words_count != null ? `${d.words_count} so‘z` : "";
      const p = d.processing_seconds != null ? `${d.processing_seconds}s` : "";
      note(`Matnga o‘girildi${w ? " · " + w : ""}${p ? " · " + p : ""}. ` +
           "Kerak bo‘lsa qo‘lda tahrirlang.", "");
    } catch (e) {
      note(`Ovozni matnga o‘girib bo‘lmadi: ${e.message}`, "err");
    } finally {
      resetRecBtn(btn, sid, qid);
    }
  }

  function resetRecBtn(btn, sid, qid) {
    btn.disabled = false;
    btn.classList.remove("rec");
    btn.innerHTML = ICON_MIC + " Ovoz bilan javob berish";
    btn.onclick = () => startRec(sid, qid, btn);
  }

  function note(text, cls) {
    const el = $$("vNote");
    if (el) { el.className = "v-note " + (cls || ""); el.textContent = text; }
  }

  /* ---------- index.html chaqiradigan yagona funksiya ---------- */
  window.RA_VOICE_HOOK = function (question, session) {
    if (!V.ready || !question || !session) return;
    const host = $$("ivQ");
    if (!host) return;

    const bar = document.createElement("div");
    bar.className = "v-bar";
    bar.innerHTML = `
      ${V.tts ? `<button class="v-btn" id="vSpeak">${ICON_SPEAK} Savolni tinglash</button>` : ""}
      ${V.tts ? `<label class="v-toggle"><input type="checkbox" id="vAuto"
          ${V.autoplay ? "checked" : ""}> avtomatik o‘qish</label>` : ""}
      ${V.stt ? `<button class="v-btn" id="vRec">${ICON_MIC} Ovoz bilan javob berish</button>`
              : '<span class="v-badge" title="' + esc(V.sttDetail || "") +
                '">ovozli javob hozir ishlamayapti</span>'}
      <span class="v-note" id="vNote"></span>`;
    host.appendChild(bar);

    const sid = session.session_id, qid = question.id;
    stopAudio();

    if (V.tts) {
      const sp = $$("vSpeak");
      sp.onclick = () => speak(sid, qid, sp);
      $$("vAuto").onchange = (e) => {
        V.autoplay = e.target.checked;
        if (V.autoplay) speak(sid, qid, sp);
      };
      if (V.autoplay) setTimeout(() => speak(sid, qid, sp), 250);
    }
    if (V.stt) resetRecBtn($$("vRec"), sid, qid);
  };

  init();
})();
