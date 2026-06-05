/* onlinecompile – client v14 */
(() => {
  "use strict";

  // ─── Helpers ────────────────────────────────────────────────────
  const qs = new URLSearchParams(window.location.search);

  function esc(v) {
    return String(v ?? "")
      .replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll(">","&gt;")
      .replaceAll('"',"&quot;").replaceAll("'","&#039;");
  }

  function loadScript(src) {
    return new Promise((res, rej) => {
      const existing = document.querySelector(`script[data-src="${src}"]`);
      if (existing) {
        if (existing.dataset.loaded === "true") { res(); return; }
        existing.addEventListener("load",  () => res(),                                 { once:true });
        existing.addEventListener("error", () => rej(new Error(`Load failed: ${src}`)), { once:true });
        return;
      }
      const s = document.createElement("script");
      s.src = src; s.async = true; s.dataset.src = src;
      s.addEventListener("load",  () => { s.dataset.loaded = "true"; res(); }, { once:true });
      s.addEventListener("error", () => rej(new Error(`Load failed: ${src}`)),          { once:true });
      document.head.appendChild(s);
    });
  }

  function loadCss(href) {
    if (!href || document.querySelector(`link[data-href="${href}"]`)) return;
    const l = document.createElement("link");
    l.rel = "stylesheet"; l.href = href; l.dataset.href = href;
    document.head.appendChild(l);
  }

  function colorToRgba(color, a = 0.22) {
    const m = String(color||"").trim().match(/^#?([0-9a-f]{6})$/i);
    if (!m) return `rgba(255,146,249,${a})`;
    const v = m[1];
    return `rgba(${parseInt(v.slice(0,2),16)},${parseInt(v.slice(2,4),16)},${parseInt(v.slice(4,6),16)},${a})`;
  }

  // ─── State ──────────────────────────────────────────────────────
  const state = {
    ws: null,
    me: null,
    role: (qs.get("role")||"").toLowerCase() === "student" ? "student" : "host",
    room: "",
    roomAction: "create",
    name: "", username: "", password: "",
    docText: "", editorValue: "", docVersion: 0,
    currentFilename: "main.py",
    applyingRemote: false,
    patchInFlight: null, queuedText: null, awaitingFullSync: false,
    reconnectTimer: null, reconnectDelayMs: 2000,
    pingTimer: null, autosaveTimer: null,
    manualClose: false,
    hasConnectedOnce: false,
    monaco: null, monacoEditor: null, monacoModel: null, monacoCompletionProvider: null,
    editorReadOnly: true,
    lspStatus: "idle",
    participants: [],
    isRunning: false, stopRequested: false,
    toastTimer: null, easterTimer: null, hostReturnTimer: null,
    chatMessages: [], chatLastOnly: false,
    chatSendTimes: [], chatSpamUntil: 0, chatNextAllowed: 0,
    // terminal
    xterm: null, xtermFit: null, xtermResizeObserver: null,
    termMode: "pending",      // "xterm" | "fallback" | "pending"
    fallbackEl: null,
    cursorBroadcastTimer: null,
  };

  // ─── DOM refs ────────────────────────────────────────────────────
  const $ = id => document.getElementById(id);
  const entryScreen        = $("entryScreen");
  const appLayout          = $("appLayout");
  const roleCards          = document.querySelectorAll(".role-card");
  const authErrorBox       = $("authError");
  const hostPane           = $("hostPane");
  const studentPane        = $("studentPane");
  const hostModeBtns       = document.querySelectorAll(".mode-btn");
  const hostNameInput      = $("hostNameInput");
  const hostUsernameInput  = $("hostUsernameInput");
  const hostPasswordInput  = $("hostPasswordInput");
  const hostPasswordToggle = $("hostPasswordToggle");
  const hostRoomInput      = $("hostRoomInput");
  const studentNameInput   = $("studentNameInput");
  const studentRoomInput   = $("studentRoomInput");
  const hostStartBtn       = $("hostStartBtn");
  const studentStartBtn    = $("studentStartBtn");
  const toastEl            = $("toast");
  const easterEggEl        = $("easterEgg");
  const closeEasterEggBtn  = $("closeEasterEgg");
  const editorMount        = $("editorMount");
  const editorUnavailable  = $("editorUnavailable");
  const editorBody         = document.querySelector(".editor-body");
  const remoteCursorsEl    = $("remoteCursors");
  const participantsEl     = $("participants");
  const participantCount   = $("participantCount");
  const participantSelect  = $("participantSelect");
  const grantBtn           = $("grantBtn");
  const revokeBtn          = $("revokeBtn");
  const clearRegionBtn     = $("clearRegionBtn");
  const setRegionBtn       = $("setRegionBtn");
  const regionStartEl      = $("regionStart");
  const regionEndEl        = $("regionEnd");
  const hostControls       = $("hostControls");
  const sessionInfo        = $("sessionInfo");
  const roomInfo           = $("roomInfo");
  const autosaveBadge      = $("autosaveBadge");
  const hostStatusBadge    = $("hostStatusBadge");
  const leaveBtn           = $("leaveBtn");
  const cursorPos          = $("cursorPos");
  const docVersionSpan     = $("docVersion");
  const currentFileSpan    = $("currentFile");
  const lspStatusEl        = $("lspStatus");
  const editingUsersEl     = $("editingUsers");
  const chatBox            = $("chatBox");
  const chatInput          = $("chatInput");
  const chatSend           = $("chatSend");
  const chatLastOnlyBtn    = $("chatLastOnlyBtn");
  const runBtn             = $("runBtn");
  const checkBtn           = $("checkBtn");
  const stopBtn            = $("stopBtn");
  const clearTermBtn       = $("clearTermBtn");
  const consoleActions     = $("consoleActions");
  const fileSelect         = $("fileSelect");
  const createFileBtn      = $("createFileBtn");
  const importFileBtn      = $("importFileBtn");
  const downloadFileBtn    = $("downloadFileBtn");
  const downloadAllBtn     = $("downloadAllBtn");
  const importFileInput    = $("importFileInput");
  const blameReportBtn     = $("blameReportBtn");
  const scoreReportBtn     = $("scoreReportBtn");
  const connectionBadge    = $("connectionBadge");
  const themeSelect        = $("themeSelect");
  const xtermMount         = $("xtermMount");
  const consolePanel       = $("consolePanel");
  const consoleResizeHandle= $("consoleResizeHandle");

  const MAX_CLIENT_DOC_BYTES = 1024 * 1024;
  const MAX_CHAT_MESSAGES    = 300;
  const MAX_FALLBACK_LINES   = 5000;
  // Chat anti-spam (mirrors the server thresholds for instant feedback).
  const CHAT_RAPID_WINDOW_MS  = 10000;
  const CHAT_RAPID_THRESHOLD  = 10;
  const CHAT_SPAM_PENALTY_MS  = 120000;
  const CHAT_SPAM_COOLDOWN_MS = 5000;

  // ─── Toast ──────────────────────────────────────────────────────
  function toast(msg) {
    if (!toastEl) { console.log("[toast]", msg); return; }
    toastEl.textContent = msg;
    toastEl.classList.add("is-visible");
    clearTimeout(state.toastTimer);
    state.toastTimer = setTimeout(() => toastEl.classList.remove("is-visible"), 2800);
  }

  // ─── Badge helpers ───────────────────────────────────────────────
  function setConnectionBadge(text, kind="idle") { connectionBadge.textContent = text; connectionBadge.dataset.state = kind; }
  function setAutosaveBadge(text, kind="idle") { if (autosaveBadge) { autosaveBadge.textContent = text; autosaveBadge.dataset.state = kind; } }
  function setHostStatusBadge(text, kind="ok") { hostStatusBadge.textContent = text; hostStatusBadge.dataset.state = kind; }
  function setLspStatus(text, kind="idle") { state.lspStatus = text; lspStatusEl.textContent = text; lspStatusEl.dataset.state = kind; }
  function showAuthError(msg="") { if (authErrorBox) { authErrorBox.hidden = !msg; authErrorBox.textContent = msg; } }

  // ─── Host return countdown ───────────────────────────────────────
  function formatCountdown(totalSec) {
    totalSec = Math.max(0, Math.floor(totalSec));
    const m = Math.floor(totalSec / 60), s = totalSec % 60;
    return `${m}:${String(s).padStart(2, "0")}`;
  }
  function stopHostReturnCountdown() {
    if (state.hostReturnTimer) { clearInterval(state.hostReturnTimer); state.hostReturnTimer = null; }
  }
  function startHostReturnCountdown(seconds) {
    stopHostReturnCountdown();
    // Count down locally from receipt to avoid client/server clock skew.
    const target = Date.now() + Math.max(0, Number(seconds) || 0) * 1000;
    const tick = () => {
      const remaining = (target - Date.now()) / 1000;
      if (remaining <= 0) {
        setHostStatusBadge("Ведущий: ожидание возврата…", "warn");
        stopHostReturnCountdown();
        return;
      }
      setHostStatusBadge(`Ведущий: возврат через ${formatCountdown(remaining)}`, "warn");
    };
    tick();
    state.hostReturnTimer = setInterval(tick, 1000);
  }

  // ─── Screen switching ─────────────────────────────────────────────
  function showEntryScreen(v)  { entryScreen.hidden = !v; document.body.classList.toggle("app-locked", v); }
  function showWorkspace(v)    { appLayout.hidden = !v; appLayout.classList.toggle("is-active", v); }

  // ─── Role / visibility ────────────────────────────────────────────
  function syncRoleVisibility() {
    const isHost = state.role === "host";
    document.body.dataset.role = state.role;
    roleCards.forEach(b => {
      const active = b.dataset.role === state.role;
      b.classList.toggle("active", active);
      b.setAttribute("aria-selected", active ? "true" : "false");
    });
    hostPane.hidden    = !isHost;
    studentPane.hidden =  isHost;
    hostModeBtns.forEach(b => b.classList.toggle("active", b.dataset.mode === state.roomAction));
    hostControls.style.display = "flex";
    // Hide host-only controls everywhere (toolbar AND console run/check/stop).
    document.querySelectorAll(".host-only").forEach(el => { el.hidden = !isHost; });
    // NOTE: console-actions container itself stays visible so the student keeps
    // the "Очистить" button; only the host-only buttons inside it are hidden.
    if (autosaveBadge) autosaveBadge.hidden = !isHost;
  }
  function setRole(role) {
    state.role = role === "student" ? "student" : "host";
    state.roomAction = state.role === "student" ? "join" : (["create","join"].includes(state.roomAction) ? state.roomAction : "create");
    localStorage.setItem("livepy:lastRole", state.role);
    syncRoleVisibility();
    updateHeaderInfo();
  }

  // ─── Theme ───────────────────────────────────────────────────────
  function isDark() {
    if (document.body.classList.contains("theme-dark")) return true;
    if (document.body.classList.contains("theme-light")) return false;
    return window.matchMedia("(prefers-color-scheme: dark)").matches;
  }
  function syncMonacoTheme() {
    if (!state.monaco?.editor) return;
    if (document.body.classList.contains("theme-avo")) { state.monaco.editor.setTheme("oc-avo"); return; }
    state.monaco.editor.setTheme(isDark() ? "oc-dark" : "vs");
  }
  function syncXtermTheme() {
    if (!state.xterm) return;
    // Derive the terminal palette from the active theme's CSS variables so the
    // console visibly recolours when the user switches Светлая/Тёмная/Avo.
    // The theme overrides live on <body> (not :root), so read from body.
    const cs = getComputedStyle(document.body);
    const v = (name, fallback) => (cs.getPropertyValue(name).trim() || fallback);
    const bg     = v("--terminal-bg", "#0e1525");
    const fg     = v("--terminal-fg", "#e7efff");
    const ok     = v("--ok",     "#32c46b");
    const bad    = v("--bad",    "#ff6f6f");
    const warn   = v("--warn",   "#f0b45a");
    const accent = v("--accent", "#89b4fa");
    const muted  = v("--muted",  "#8a93a6");
    state.xterm.options.theme = {
      background:bg, foreground:fg, cursor:accent, cursorAccent:bg,
      selectionBackground: colorToRgba(accent, .32),
      black:muted,  red:bad,     green:ok,     yellow:warn,
      blue:accent,  magenta:accent, cyan:ok,   white:fg,
      brightBlack:muted, brightRed:bad, brightGreen:ok,
      brightYellow:warn, brightBlue:accent, brightMagenta:accent,
      brightCyan:ok, brightWhite:fg,
    };
  }
  function applyTheme(theme) {
    const safe = ["system","light","dark","avo"].includes(theme) ? theme : "system";
    document.body.classList.remove("theme-light","theme-dark","theme-avo");
    if (safe !== "system") document.body.classList.add(`theme-${safe}`);
    localStorage.setItem("livepy:theme", safe);
    if (themeSelect) themeSelect.value = safe;
    syncMonacoTheme();
    syncXtermTheme();
  }
  function initTheme() {
    applyTheme(localStorage.getItem("livepy:theme") || "system");
    themeSelect?.addEventListener("change", () => applyTheme(themeSelect.value));
    window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
      if (!["light","dark","avo"].includes(localStorage.getItem("livepy:theme"))) {
        syncMonacoTheme(); syncXtermTheme();
      }
    });
  }

  // ═══════════════════════════════════════════════════════════════════
  // Terminal: xterm.js with graceful <pre> fallback (offline-safe)
  // ═══════════════════════════════════════════════════════════════════

  async function loadXtermEngine() {
    const src = window.__xtermSources || {};
    // xterm ships as a UMD bundle: if an AMD loader (Monaco's loader.js defines
    // window.define with .amd) is present, the bundle registers as an anonymous
    // AMD module and never sets window.Terminal — so the terminal silently falls
    // back to <pre>. Hide any AMD `define` while xterm loads so it binds globally.
    const savedDefine = window.define;
    const amdPresent = typeof savedDefine === "function" && savedDefine.amd;
    if (amdPresent) window.define = undefined;
    const restoreDefine = () => { if (amdPresent && !window.define) window.define = savedDefine; };
    try {
      // 1) try local vendor copy (fully offline)
      if (src.local) {
        try {
          await loadScript(src.local.js);
          if (src.local.fit) await loadScript(src.local.fit).catch(() => {});
          if (window.Terminal) return true;
        } catch (_) { /* fall through to CDN */ }
      }
      // 2) try CDN
      if (src.cdn) {
        try {
          if (src.cdn.css) loadCss(src.cdn.css);
          await loadScript(src.cdn.js);
          if (src.cdn.fit) await loadScript(src.cdn.fit).catch(() => {});
          if (window.Terminal) return true;
        } catch (_) { /* fall through to fallback */ }
      }
      return Boolean(window.Terminal);
    } finally {
      restoreDefine();
    }
  }

  async function initTerminal() {
    const ok = await loadXtermEngine();
    if (ok && window.Terminal) {
      initXterm();
    } else {
      initFallbackTerminal();
    }
  }

  function initXterm() {
    try {
      const term = new window.Terminal({
        convertEol: true,
        scrollback: 5000,
        fontSize: 14,
        fontFamily: '"JetBrains Mono", "Fira Code", Menlo, Consolas, monospace',
        cursorBlink: false,
        disableStdin: true,
        allowProposedApi: false,
        theme: { background:"#0e1525", foreground:"#e7efff" },
      });
      state.xterm = term;
      const FitAddonCtor = window.FitAddon?.FitAddon;
      if (FitAddonCtor) {
        const fit = new FitAddonCtor();
        term.loadAddon(fit);
        state.xtermFit = fit;
      }
      term.open(xtermMount);
      state.termMode = "xterm";
      syncXtermTheme();
      fitXterm();
      if (window.ResizeObserver) {
        state.xtermResizeObserver = new ResizeObserver(() => fitXterm());
        state.xtermResizeObserver.observe(xtermMount);
      }
      writeTermLine("\x1b[2m[onlinecompile]\x1b[0m Терминал готов.\r\n");
    } catch (e) {
      console.warn("xterm init failed, using fallback", e);
      initFallbackTerminal();
    }
  }

  function initFallbackTerminal() {
    state.termMode = "fallback";
    const pre = document.createElement("pre");
    pre.className = "console-fallback";
    pre.id = "consoleFallback";
    xtermMount.replaceWith(pre);
    state.fallbackEl = pre;
    fbLine("[onlinecompile] Терминал (упрощённый режим). xterm.js недоступен — вывод сохраняется.", "dim");
  }

  function fitXterm() { if (state.xtermFit) { try { state.xtermFit.fit(); } catch(_){} } }

  // Batch scroll-to-bottom via rAF so a flood of output never forces a reflow
  // per message (which would freeze the tab and make the Stop button feel dead).
  let fbScrollScheduled = false;
  function scheduleFbScroll() {
    if (fbScrollScheduled || !state.fallbackEl) return;
    fbScrollScheduled = true;
    requestAnimationFrame(() => {
      fbScrollScheduled = false;
      if (state.fallbackEl) state.fallbackEl.scrollTop = state.fallbackEl.scrollHeight;
    });
  }
  // Strip ANSI for fallback, but map common codes to CSS classes
  function fbLine(text, cls) {
    if (!state.fallbackEl) return;
    const span = document.createElement("span");
    if (cls) span.className = `ln-${cls}`;
    span.textContent = String(text).replace(/\x1b\[[0-9;]*m/g, "") + "\n";
    state.fallbackEl.appendChild(span);
    // trim
    while (state.fallbackEl.childNodes.length > MAX_FALLBACK_LINES)
      state.fallbackEl.removeChild(state.fallbackEl.firstChild);
    scheduleFbScroll();
  }
  function fbText(text) {
    if (!state.fallbackEl) return;
    const node = document.createTextNode(String(text).replace(/\x1b\[[0-9;]*m/g, ""));
    state.fallbackEl.appendChild(node);
    scheduleFbScroll();
  }

  // Unified write API – routes to xterm or fallback
  function writeTermText(text) {
    if (!text) return;
    if (state.termMode === "xterm" && state.xterm) {
      state.xterm.write(String(text).replace(/\r?\n/g, "\r\n"));
    } else {
      fbText(text);
    }
  }
  function writeTermLine(line) {
    if (state.termMode === "xterm" && state.xterm) {
      state.xterm.write(line.endsWith("\r\n") ? line : line.replace(/\n?$/, "\r\n"));
    } else {
      // detect semantic class from leading tag
      let cls = "";
      if (/\[(error|save-error)\]/.test(line)) cls = "err";
      else if (/\[(syntax|run|save|room)\]/.test(line) && /OK|Запуск|восстанов|Подключено/.test(line)) cls = "ok";
      else if (/\[(run|syntax)\]/.test(line)) cls = "warn";
      else if (/\[(system|onlinecompile|exit)\]/.test(line)) cls = "dim";
      fbLine(line.replace(/\r?\n$/, ""), cls);
    }
  }
  function clearTerm() {
    if (state.termMode === "xterm" && state.xterm) { state.xterm.clear(); state.xterm.reset(); }
    else if (state.fallbackEl) { state.fallbackEl.textContent = ""; }
  }

  // ─── Console-resize drag ─────────────────────────────────────────
  function initConsoleResize() {
    let dragging = false, startY = 0, startH = 0;
    const STORE_KEY = "livepy:consoleH";
    const saved = parseInt(localStorage.getItem(STORE_KEY) || "", 10);
    if (saved && saved >= 120) {
      consolePanel.style.height = `${saved}px`;
      consolePanel.style.setProperty("--console-h", `${saved}px`);
    }
    const clampH = h => Math.max(120, Math.min(window.innerHeight * 0.8, h));
    function onMove(y) {
      const newH = clampH(startH + (startY - y));
      consolePanel.style.height = `${newH}px`;
      consolePanel.style.setProperty("--console-h", `${newH}px`);
      fitXterm();
    }
    function endDrag() {
      if (!dragging) return;
      dragging = false; document.body.style.userSelect = "";
      const h = Math.round(consolePanel.getBoundingClientRect().height);
      localStorage.setItem(STORE_KEY, String(h));
    }
    consoleResizeHandle.addEventListener("mousedown", e => {
      dragging = true; startY = e.clientY; startH = consolePanel.getBoundingClientRect().height;
      document.body.style.userSelect = "none"; e.preventDefault();
    });
    window.addEventListener("mousemove", e => { if (dragging) onMove(e.clientY); });
    window.addEventListener("mouseup", endDrag);
    consoleResizeHandle.addEventListener("touchstart", e => {
      dragging = true; startY = e.touches[0].clientY; startH = consolePanel.getBoundingClientRect().height; e.preventDefault();
    }, { passive:false });
    window.addEventListener("touchmove", e => { if (dragging) onMove(e.touches[0].clientY); }, { passive:true });
    window.addEventListener("touchend", endDrag);
  }

  // ─── Easter egg ──────────────────────────────────────────────────
  function hideEasterEgg() { if (easterEggEl) { easterEggEl.hidden = true; document.title = "onlinecompile"; } }
  function triggerEasterEgg() {
    if (!easterEggEl) return;
    easterEggEl.hidden = false;
    document.title = "avoselection // onlinecompile";
    toast("Пасхалка avoselection активирована");
    clearTimeout(state.easterTimer);
    state.easterTimer = setTimeout(hideEasterEgg, 5000);
  }
  function initKonamiCode() {
    const seqs = [
      ["arrowup","arrowup","arrowdown","arrowdown","arrowleft","arrowright","arrowleft","arrowright","b","a"],
      ["arrowup","arrowup","arrowdown","arrowdown","arrowleft","arrowright","arrowleft","arrowright","a","b"],
    ];
    const cursors = seqs.map(() => 0);
    document.addEventListener("keydown", e => {
      const k = String(e.key||"").toLowerCase();
      seqs.forEach((seq, i) => {
        let c = cursors[i];
        c = k === seq[c] ? c + 1 : (k === seq[0] ? 1 : 0);
        if (c === seq.length) { c = 0; triggerEasterEgg(); }
        cursors[i] = c;
      });
    });
    closeEasterEggBtn?.addEventListener("click", hideEasterEgg);
    easterEggEl?.addEventListener("click", e => { if (e.target === easterEggEl) hideEasterEgg(); });
  }

  // ─── Chat ────────────────────────────────────────────────────────
  function colorForParticipant(fromId, fromName) {
    const byId = fromId ? state.participants.find(p => p.id === fromId) : null;
    if (byId?.color) return byId.color;
    const normName = String(fromName||"").trim().toLowerCase();
    const byName = normName ? state.participants.find(p => String(p.name||"").trim().toLowerCase() === normName) : null;
    return byName?.color || "#ff92f9";
  }
  function normChat(msg) {
    if (msg && typeof msg === "object") {
      const from = String(msg.from||"Система");
      return { from, fromId:msg.fromId||null, text:String(msg.text||""), color:msg.color||colorForParticipant(msg.fromId,from) };
    }
    const raw = String(msg??"");
    const sep = raw.indexOf(":");
    if (sep > 0) {
      const from = raw.slice(0,sep).trim()||"Система";
      return { from, fromId:null, text:raw.slice(sep+1).trimStart(), color:colorForParticipant(null,from) };
    }
    return { from:"Система", fromId:null, text:raw, color:"#ff92f9" };
  }
  function renderChat() {
    if (!chatBox) return;
    chatBox.innerHTML = "";
    const msgs = state.chatLastOnly ? state.chatMessages.slice(-1) : state.chatMessages;
    if (!msgs.length) {
      const d = document.createElement("div");
      d.className = "chat-empty"; d.textContent = "Сообщений пока нет";
      chatBox.appendChild(d);
    } else {
      msgs.forEach(m => {
        const n = normChat(m);
        const color = n.color||colorForParticipant(n.fromId,n.from);
        const item = document.createElement("div"); item.className = "chat-line";
        const auth = document.createElement("span"); auth.className = "chat-author";
        auth.style.color = color; auth.style.background = colorToRgba(color,.16); auth.style.borderColor = colorToRgba(color,.44);
        auth.textContent = n.from;
        const txt = document.createElement("span"); txt.className = "chat-text"; txt.textContent = n.text;
        item.append(auth,txt);
        chatBox.appendChild(item);
      });
    }
    chatBox.classList.toggle("is-last-only", state.chatLastOnly);
    if (chatLastOnlyBtn) {
      chatLastOnlyBtn.textContent = state.chatLastOnly ? "Вся история" : "Последнее";
      chatLastOnlyBtn.classList.toggle("active", state.chatLastOnly);
    }
    chatBox.scrollTop = chatBox.scrollHeight;
  }
  function appendChat(msg) {
    state.chatMessages.push(normChat(msg));
    if (state.chatMessages.length > MAX_CHAT_MESSAGES)
      state.chatMessages.splice(0, state.chatMessages.length - MAX_CHAT_MESSAGES);
    renderChat();
  }
  function clearChat() { state.chatMessages = []; renderChat(); }
  function sendChat() {
    const text = chatInput.value.trim();
    if (!text) return;
    const now = Date.now();
    if (now < state.chatNextAllowed) {
      const rem = Math.ceil((state.chatNextAllowed - now) / 1000);
      toast(`Антиспам: подождите ${rem} с перед следующим сообщением.`);
      return;
    }
    state.ws?.send(JSON.stringify({ type:"chat", text }));
    chatInput.value = "";
    // Local burst tracking, mirroring the server, for instant feedback.
    state.chatSendTimes.push(now);
    state.chatSendTimes = state.chatSendTimes.filter(t => now - t < CHAT_RAPID_WINDOW_MS);
    if (state.chatSendTimes.length >= CHAT_RAPID_THRESHOLD) {
      state.chatSpamUntil = now + CHAT_SPAM_PENALTY_MS;
    }
    if (now < state.chatSpamUntil) {
      state.chatNextAllowed = now + CHAT_SPAM_COOLDOWN_MS;
    }
  }

  // ─── Header / file list ──────────────────────────────────────────
  function updateHeaderInfo() {
    sessionInfo.textContent   = `Вы: ${state.name||"—"}`;
    roomInfo.textContent      = `Комната: ${state.room||"—"}`;
    currentFileSpan.textContent = state.currentFilename||"main.py";
  }
  function updateFileList(files=[], current="main.py") {
    fileSelect.innerHTML = "";
    files.forEach(f => {
      const o = document.createElement("option");
      o.value = f; o.textContent = f; o.selected = f === current;
      fileSelect.appendChild(o);
    });
    currentFileSpan.textContent = current;
  }

  // ─── Editor helpers ──────────────────────────────────────────────
  function getEditorText()     { return state.monacoModel ? state.monacoModel.getValue() : state.editorValue||state.docText||""; }
  function setEditorText(text) {
    const s = String(text??"");
    state.editorValue = s;
    if (state.monacoModel && state.monacoModel.getValue() !== s) state.monacoModel.setValue(s);
  }
  function getSelectionStart() {
    if (state.monacoEditor && state.monacoModel) {
      const sel = state.monacoEditor.getSelection();
      if (sel) return state.monacoModel.getOffsetAt(sel.getStartPosition());
    }
    return 0;
  }
  function focusEditor() { state.monacoEditor ? state.monacoEditor.focus() : editorUnavailable?.focus(); }
  function setReadOnly(ro) {
    state.editorReadOnly = Boolean(ro);
    editorMount?.classList.toggle("readonly", state.editorReadOnly);
    state.monacoEditor?.updateOptions({ readOnly: state.editorReadOnly });
  }
  function updateEditorLayout() { state.monacoEditor?.layout(); }

  function currentClientCanEdit() {
    if (state.role === "host") return true;
    const me = state.participants.find(p => p.id === state.me?.id);
    return Boolean(me ? me.can_edit : state.me?.can_edit);
  }

  // ─── Cursor ──────────────────────────────────────────────────────
  function indexToLineCol(text, idx) {
    let line=1, col=1;
    for (let i=0; i<idx; i++) { if (text[i]==="\n") { line++; col=1; } else col++; }
    return { line, col };
  }
  function updateCursorStatus() {
    const { line, col } = indexToLineCol(getEditorText(), getSelectionStart());
    cursorPos.textContent = `Ln ${line}, Col ${col}`;
    clearTimeout(state.cursorBroadcastTimer);
    if (state.ws?.readyState === WebSocket.OPEN && currentClientCanEdit()) {
      state.cursorBroadcastTimer = setTimeout(() => state.ws?.send(JSON.stringify({ type:"cursor", line, col })), 30);
    }
  }
  function lineColToCoords(line, col) {
    if (!state.monacoEditor || !state.monacoModel || !state.monaco) return null;
    try {
      const safeLine = Math.max(1, Math.min(line, state.monacoModel.getLineCount()));
      const safeCol  = Math.max(1, Math.min(col, state.monacoModel.getLineMaxColumn(safeLine)));
      const pos      = new state.monaco.Position(safeLine, safeCol);
      const coords   = state.monacoEditor.getScrolledVisiblePosition(pos);
      return coords ? { top:coords.top, left:coords.left, height:coords.height||18 } : null;
    } catch { return null; }
  }
  function renderRemoteCursors() {
    if (!state.me) return;
    remoteCursorsEl.innerHTML = "";
    state.participants.forEach(p => {
      if (!((p.role==="host"||p.can_edit) && p.cursor && p.id !== state.me.id)) return;
      const coords = lineColToCoords(p.cursor.line, p.cursor.col);
      if (!coords) return;
      const color = p.color||"#ff92f9";
      const w = document.createElement("div"); w.className = "remote-cursor";
      w.style.top = `${coords.top}px`; w.style.left = `${coords.left}px`;
      const caret = document.createElement("div"); caret.className = "remote-caret";
      caret.style.background = color; caret.style.height = `${coords.height}px`;
      const lbl = document.createElement("div"); lbl.className = "remote-cursor-label";
      lbl.style.background = colorToRgba(color,.74); lbl.style.borderColor = colorToRgba(color,.9);
      lbl.textContent = p.name || "Участник";
      w.append(caret,lbl); remoteCursorsEl.appendChild(w);
    });
  }

  // ─── Participants ────────────────────────────────────────────────
  function renderEditingUsers() {
    if (!editingUsersEl) return;
    const editors = state.participants.filter(p => p.role==="student" && p.can_edit);
    editingUsersEl.hidden = editors.length === 0;
    editingUsersEl.innerHTML = "";
    editors.forEach(p => {
      const badge = document.createElement("span");
      const color = p.color||"#ff92f9";
      badge.className = "editing-user";
      badge.style.background = colorToRgba(color,.22); badge.style.borderColor = colorToRgba(color,.5); badge.style.color = color;
      badge.textContent = p.name||"Студент";
      editingUsersEl.appendChild(badge);
    });
  }
  function renderParticipants(list) {
    state.participants = (list||[]).map(item => ({
      ...item,
      cursor: item.cursor || state.participants.find(p=>p.id===item.id)?.cursor || null,
    }));
    participantsEl.innerHTML = "";
    participantSelect.innerHTML = "";
    if (participantCount) participantCount.textContent = String(state.participants.length);

    state.participants.forEach(p => {
      const isStudent = p.role === "student";
      const isMe      = p.id === state.me?.id;
      const roleLabel = isStudent ? "студент" : "ведущий";
      const latencyLabel = p.latency_ms != null ? `${p.latency_ms} ms` : "— ms";

      const tags = [];
      if (isMe) tags.push(`<span class="participant-tag tag-you">вы</span>`);
      tags.push(`<span class="participant-tag">роль: ${esc(roleLabel)}</span>`);
      tags.push(`<span class="participant-tag">${esc(latencyLabel)}</span>`);

      const details = [];
      if (isStudent) {
        details.push(p.can_edit
          ? `<span class="participant-tag tag-editor">редактор</span>`
          : `<span class="participant-tag">просмотр</span>`);
        details.push(`<span class="participant-tag">доступов: ${p.access_grants??0}</span>`);
        if (p.region) details.push(`<span class="participant-tag">строки ${p.region[0]}-${p.region[1]}</span>`);
      }

      const row = document.createElement("li");
      row.className = "participant-item" + ((isStudent && p.can_edit) ? " is-editor" : "");
      row.innerHTML = `
        <span class="participant-color" style="background:${p.color||"var(--accent)"}"></span>
        <div class="participant-meta">
          <strong class="participant-name">${esc(p.name)}</strong>
          <div class="participant-line">${tags.join("")}</div>
          ${details.length ? `<div class="participant-details">${details.join("")}</div>` : ""}
        </div>`;
      participantsEl.appendChild(row);

      if (isStudent) {
        const opt = document.createElement("option");
        opt.value = p.id; opt.textContent = `${p.name} (${p.access_grants??0} выдач)`;
        participantSelect.appendChild(opt);
      }
    });

    const me = state.participants.find(p => p.id === state.me?.id);
    if (me) state.me = { ...state.me, can_edit: Boolean(me.can_edit) };
    renderEditingUsers();
    updateButtons(currentClientCanEdit());
    renderRemoteCursors();
  }

  // ─── Buttons ─────────────────────────────────────────────────────
  function setDocVersionLabel(suf="") { docVersionSpan.textContent = `v${state.docVersion}${suf}`; }
  function updateButtons() {
    setReadOnly(!currentClientCanEdit());
    if (stopBtn) stopBtn.textContent = state.stopRequested ? "Остановка…" : "■ Стоп";
    if (downloadFileBtn) downloadFileBtn.disabled = false;
    const host = state.role === "host";
    if (runBtn)         runBtn.disabled         = !host || state.isRunning;
    if (checkBtn)       checkBtn.disabled       = !host || state.isRunning;
    if (stopBtn)        stopBtn.disabled        = !host || !state.isRunning || state.stopRequested;
    if (fileSelect)     fileSelect.disabled     = !host;
    if (createFileBtn)  createFileBtn.disabled  = !host;
    if (importFileBtn)  importFileBtn.disabled  = !host;
    if (downloadAllBtn) downloadAllBtn.disabled = !host;
    if (blameReportBtn) blameReportBtn.disabled = !host;
    if (scoreReportBtn) scoreReportBtn.disabled = !host;
  }

  // ─── Patch / sync ────────────────────────────────────────────────
  function hasPendingLocalEdit() { return Boolean(state.patchInFlight) || state.queuedText !== null; }
  function resetPendingSync()    { state.patchInFlight = null; state.queuedText = null; state.awaitingFullSync = false; }
  function computePatch(oldText, newText) {
    if (oldText === newText) return { start:0, end:0, text:"" };
    let s = 0;
    while (s < oldText.length && s < newText.length && oldText[s] === newText[s]) s++;
    let oe = oldText.length, ne = newText.length;
    while (oe > s && ne > s && oldText[oe-1] === newText[ne-1]) { oe--; ne--; }
    return { start:s, end:oe, text:newText.slice(s,ne) };
  }
  function flushDocumentPatch() {
    if (state.applyingRemote || state.awaitingFullSync || state.patchInFlight) return;
    if (state.ws?.readyState !== WebSocket.OPEN) return;
    if (!currentClientCanEdit() || state.queuedText === null) return;
    const nextText = String(state.queuedText);
    if (nextText === state.docText) { state.queuedText = null; setDocVersionLabel(); return; }
    const patch = computePatch(state.docText, nextText);
    if (!patch.start && !patch.end && !patch.text) { state.queuedText = null; setDocVersionLabel(); return; }
    state.patchInFlight = { baseVersion:state.docVersion, text:nextText, patch };
    state.queuedText = null;
    try {
      state.ws.send(JSON.stringify({ type:"patch", baseVersion:state.docVersion, start:patch.start, end:patch.end, text:patch.text }));
      setDocVersionLabel(" •");
    } catch(e) {
      console.warn("patch send failed", e);
      state.patchInFlight = null; state.queuedText = nextText; setDocVersionLabel(" ⟳");
    }
  }
  function sendDocumentPatch(nextText) {
    if (state.applyingRemote || state.awaitingFullSync) return;
    if (state.ws?.readyState !== WebSocket.OPEN) return;
    if (!currentClientCanEdit()) return;
    state.queuedText = String(nextText??"");
    flushDocumentPatch();
  }
  function confirmOwnPatch(version) {
    state.docVersion = Number(version)||state.docVersion;
    state.docText = state.patchInFlight ? state.patchInFlight.text : getEditorText();
    state.patchInFlight = null;
    setDocVersionLabel(state.queuedText !== null ? " •" : "");
    flushDocumentPatch();
  }
  function applyRemotePatch(patch) {
    const cur = getEditorText();
    if (!patch || patch.start < 0 || patch.end < patch.start || patch.end > cur.length) { requestFull(); return false; }
    const ins = String(patch.text??"");
    const next = cur.slice(0, patch.start) + ins + cur.slice(patch.end);
    state.applyingRemote = true;
    try {
      if (state.monacoModel && state.monaco) {
        const sp = state.monacoModel.getPositionAt(patch.start);
        const ep = state.monacoModel.getPositionAt(patch.end);
        const scrollTop = state.monacoEditor?.getScrollTop?.();
        const scrollLeft = state.monacoEditor?.getScrollLeft?.();
        state.monacoModel.applyEdits([{
          range: new state.monaco.Range(sp.lineNumber, sp.column, ep.lineNumber, ep.column),
          text: ins, forceMoveMarkers:true,
        }]);
        if (state.monacoModel.getValue() !== next) state.monacoModel.setValue(next);
        if (scrollTop  != null) state.monacoEditor?.setScrollTop?.(scrollTop);
        if (scrollLeft != null) state.monacoEditor?.setScrollLeft?.(scrollLeft);
      } else {
        setEditorText(next);
      }
    } catch(e) { console.warn("remote patch failed", e); setEditorText(next); }
    finally { state.applyingRemote = false; }
    if (getEditorText() !== next) { requestFull(); return false; }
    state.docText = next;
    updateEditorLayout(); updateCursorStatus(); renderRemoteCursors();
    return true;
  }
  function requestFull() {
    state.awaitingFullSync = true; state.patchInFlight = null; state.queuedText = null;
    setDocVersionLabel(" ⟳");
    if (state.ws?.readyState === WebSocket.OPEN) state.ws.send(JSON.stringify({ type:"request_full" }));
  }

  // ─── Ping / autosave loops ────────────────────────────────────────
  function startPingLoop() {
    clearInterval(state.pingTimer);
    state.pingTimer = setInterval(() => {
      if (state.ws?.readyState === WebSocket.OPEN) state.ws.send(JSON.stringify({ type:"ping", ts:Date.now() }));
    }, 5000);
  }
  function stopPingLoop() { clearInterval(state.pingTimer); state.pingTimer = null; }
  function startAutosaveLoop() {
    clearInterval(state.autosaveTimer);
    if (state.role !== "host") return;
    setAutosaveBadge("Автосохранение: активно", "ok");
    state.autosaveTimer = setInterval(() => {
      if (state.ws?.readyState === WebSocket.OPEN)
        state.ws.send(JSON.stringify({ type:"autosave", code:getEditorText(), filename:state.currentFilename }));
    }, 15000);
  }
  function stopAutosaveLoop() { clearInterval(state.autosaveTimer); state.autosaveTimer = null; }

  // ─── Monaco ──────────────────────────────────────────────────────
  function defineMonacoThemes() {
    if (!state.monaco?.editor) return;
    state.monaco.editor.defineTheme("oc-dark", {
      base:"vs-dark", inherit:true, rules:[],
      colors:{
        "editor.background":"#0b0b0b","editor.foreground":"#f4f0eb",
        "editorLineNumber.foreground":"#706a62","editorCursor.foreground":"#ff9a62",
        "editor.lineHighlightBackground":"#17120f","editor.selectionBackground":"#553420",
        "editor.inactiveSelectionBackground":"#33241b","editorGutter.background":"#0b0b0b",
      },
    });
    state.monaco.editor.defineTheme("oc-avo", {
      base:"vs", inherit:true,
      rules:[
        { token:"keyword", foreground:"1e63ff", fontStyle:"bold" },
        { token:"string",  foreground:"087d67" },
        { token:"number",  foreground:"c83243" },
        { token:"comment", foreground:"7b6f55", fontStyle:"italic" },
      ],
      colors:{
        "editor.background":"#fff9fd","editor.foreground":"#26112d",
        "editorLineNumber.foreground":"#9d6a95","editorCursor.foreground":"#1e63ff",
        "editor.lineHighlightBackground":"#ffe6f7","editor.selectionBackground":"#b9d0ff",
        "editor.inactiveSelectionBackground":"#f2c8ec","editorGutter.background":"#fff0fb",
      },
    });
  }
  function collectLocalSuggestions() {
    const keywords = [
      "False","None","True","and","as","assert","async","await","break","class","continue",
      "def","del","elif","else","except","finally","for","from","global","if","import",
      "in","is","lambda","nonlocal","not","or","pass","raise","return","try","while",
      "with","yield","match","case","print","len","range","list","dict","set","tuple",
      "str","int","float","bool","enumerate","zip","map","filter","sum","min","max",
    ];
    const snippets = [
      { label:"def",   detail:"функция",            apply:"def function_name():\n    pass" },
      { label:"class", detail:"класс",              apply:"class ClassName:\n    pass" },
      { label:"for",   detail:"цикл",               apply:"for item in iterable:\n    pass" },
      { label:"while", detail:"цикл",               apply:"while condition:\n    pass" },
      { label:"if",    detail:"условие",            apply:"if condition:\n    pass" },
      { label:"try",   detail:"обработка исключений", apply:"try:\n    pass\nexcept Exception as e:\n    print(e)" },
      { label:"with",  detail:"контекст",           apply:'with open("file.txt","r",encoding="utf-8") as f:\n    data = f.read()' },
    ];
    const docWords = Array.from(new Set((getEditorText().match(/[A-Za-z_][A-Za-z0-9_]*/g)||[])))
      .filter(w => w.length >= 2).slice(0,400)
      .map(w => ({ label:w, type:"variable", detail:"из текущего файла" }));
    return [
      ...snippets.map(s => ({ ...s, type:"keyword" })),
      ...keywords.map(k => ({ label:k, type:"keyword", detail:"Python" })),
      ...docWords,
    ];
  }
  async function initMonacoEditor() {
    const candidate = {
      base:   "/static/vendor/monaco",
      loader: "/static/vendor/monaco/loader.js",
      css:    "/static/vendor/monaco/editor/editor.main.css",
      workers: {
        editorWorkerService: "/static/vendor/monaco/assets/editor.worker-Be8ye1pW.js",
        json:       "/static/vendor/monaco/assets/json.worker-DKiEKt88.js",
        css:        "/static/vendor/monaco/assets/css.worker-HnVq6Ewq.js",
        scss:       "/static/vendor/monaco/assets/css.worker-HnVq6Ewq.js",
        less:       "/static/vendor/monaco/assets/css.worker-HnVq6Ewq.js",
        html:       "/static/vendor/monaco/assets/html.worker-B51mlPHg.js",
        handlebars: "/static/vendor/monaco/assets/html.worker-B51mlPHg.js",
        razor:      "/static/vendor/monaco/assets/html.worker-B51mlPHg.js",
        typescript: "/static/vendor/monaco/assets/ts.worker-CMbG-7ft.js",
        javascript: "/static/vendor/monaco/assets/ts.worker-CMbG-7ft.js",
      },
    };
    try {
      loadCss(candidate.css);
      if (!window.require) await loadScript(candidate.loader);
      window.MonacoEnvironment = {
        getWorkerUrl(_id, label) { return candidate.workers[label] || candidate.workers.editorWorkerService; },
      };
      await new Promise((res, rej) => {
        window.require.config({ paths:{ vs: candidate.base } });
        window.require(["vs/editor/editor.main"], res, rej);
      });
      if (!window.monaco) throw new Error("monaco global missing after require");
      state.monaco = window.monaco;
      defineMonacoThemes();
      syncMonacoTheme();

      state.monacoCompletionProvider?.dispose();
      state.monacoCompletionProvider = state.monaco.languages.registerCompletionItemProvider("python", {
        triggerCharacters:[".","_"],
        provideCompletionItems(model, position) {
          const word = model.getWordUntilPosition(position);
          const typed = String(word?.word||"").toLowerCase();
          const range = new state.monaco.Range(position.lineNumber, word?.startColumn||position.column, position.lineNumber, word?.endColumn||position.column);
          const suggestions = collectLocalSuggestions()
            .filter(o => !typed || o.label.toLowerCase().includes(typed))
            .slice(0,200)
            .map((o,i) => {
              let kind = state.monaco.languages.CompletionItemKind.Text;
              if (o.type==="keyword")  kind = state.monaco.languages.CompletionItemKind.Keyword;
              if (o.type==="variable") kind = state.monaco.languages.CompletionItemKind.Variable;
              return {
                label:o.label, kind, detail:o.detail||"",
                insertText:o.apply||o.label,
                insertTextRules:o.apply ? state.monaco.languages.CompletionItemInsertTextRule.InsertAsSnippet : undefined,
                range, sortText:String(i).padStart(4,"0"),
              };
            });
          return { suggestions };
        },
      });

      state.monacoModel = state.monaco.editor.createModel(state.editorValue||state.docText||"","python");
      state.monacoEditor = state.monaco.editor.create(editorMount, {
        model: state.monacoModel,
        automaticLayout: true,
        minimap:{ enabled:false }, lineNumbers:"on",
        glyphMargin:false, folding:false,
        lineDecorationsWidth:6, lineNumbersMinChars:3,
        overviewRulerLanes:0, hideCursorInOverviewRuler:true,
        scrollBeyondLastLine:false, wordWrap:"off",
        tabSize:4, insertSpaces:true, detectIndentation:false,
        fontSize:14, lineHeight:21,
        fontFamily:'"JetBrains Mono","Fira Code",Menlo,Consolas,monospace',
        quickSuggestions:{ other:true, comments:false, strings:true },
        suggestOnTriggerCharacters:true, acceptSuggestionOnEnter:"on",
        renderLineHighlight:"line", scrollbar:{ alwaysConsumeMouseWheel:false },
        padding:{ top:14, bottom:14 }, readOnly:state.editorReadOnly,
      });

      state.monacoEditor.onDidChangeModelContent(() => {
        const text = state.monacoModel.getValue();
        state.editorValue = text;
        updateEditorLayout(); updateCursorStatus(); renderRemoteCursors();
        sendDocumentPatch(text);
      });
      state.monacoEditor.onDidChangeCursorSelection(() => { updateCursorStatus(); renderRemoteCursors(); });
      state.monacoEditor.onDidScrollChange(() => renderRemoteCursors());

      editorMount.classList.add("is-active");
      editorBody?.classList.add("monaco-active");
      editorMount.hidden = false;
      if (editorUnavailable) editorUnavailable.hidden = true;
      requestAnimationFrame(() => state.monacoEditor?.layout());
      setLspStatus("Monaco Editor", "ok");
      updateEditorLayout(); updateCursorStatus();
    } catch(e) {
      console.warn("Monaco unavailable", e);
      setLspStatus("Редактор недоступен", "error");
      editorBody?.classList.remove("monaco-active");
      editorMount.hidden = true;
      if (editorUnavailable) editorUnavailable.hidden = false;
      remoteCursorsEl.innerHTML = "";
      toast("Редактор кода не загрузился — проверьте файлы в static/vendor/monaco/");
    }
  }

  // ─── Download helpers ─────────────────────────────────────────────
  function normalizeDownloadFilename(fn) {
    const raw = String(fn||"main.py").trim().replace(/[/\\]+/g,"_");
    const cleaned = raw.replace(/[^A-Za-z0-9А-Яа-яЁё._ -]/g,"_").replace(/\s+/g,"_").replace(/^\.+/,"") || "main.py";
    return cleaned.toLowerCase().endsWith(".py") ? cleaned : `${cleaned}.py`;
  }
  function normalizeArchiveFilename(fn) {
    const raw = String(fn||"onlinecompile_files.zip").trim().replace(/[/\\]+/g,"_");
    const cleaned = raw.replace(/[^A-Za-z0-9А-Яа-яЁё._ -]/g,"_").replace(/\s+/g,"_").replace(/^\.+/,"") || "onlinecompile_files.zip";
    return cleaned.toLowerCase().endsWith(".zip") ? cleaned : `${cleaned}.zip`;
  }
  function triggerBrowserDownload(filename, blob) {
    const link = document.createElement("a");
    const url = URL.createObjectURL(blob);
    link.href = url; link.download = filename; link.rel = "noopener"; link.style.display = "none";
    document.body.appendChild(link); link.click();
    setTimeout(() => { URL.revokeObjectURL(url); link.remove(); }, 1000);
  }
  function downloadCurrentEditorFile() {
    const fn = normalizeDownloadFilename(state.currentFilename||"main.py");
    triggerBrowserDownload(fn, new Blob([getEditorText()], { type:"text/x-python;charset=utf-8" }));
    toast(`Скачивание начато: ${fn}`);
  }
  async function downloadRoomArchive() {
    if (!state.room) return;
    const fn = normalizeArchiveFilename(`${state.room}_files.zip`);
    if (downloadAllBtn) downloadAllBtn.disabled = true;
    try {
      const resp = await fetch(`/api/rooms/${encodeURIComponent(state.room)}/download-all?_=${Date.now()}`,{ cache:"no-store" });
      if (!resp.ok) {
        let msg = `Не удалось скачать архив: ${resp.status}`;
        try { const p = await resp.json(); if (p?.message) msg = p.message; } catch {}
        throw new Error(msg);
      }
      triggerBrowserDownload(fn, await resp.blob());
      toast(`Скачивание начато: ${fn}`);
    } catch(e) { toast(e?.message||"Не удалось скачать архив"); }
    finally { if (downloadAllBtn) downloadAllBtn.disabled = false; }
  }

  // ─── Session start / leave ────────────────────────────────────────
  function applyAuthFromUI() {
    if (state.role === "host") {
      state.name     = (hostNameInput.value||"Ведущий").trim()||"Ведущий";
      state.username = (hostUsernameInput.value||"HOST").trim()||"HOST";
      state.password = hostPasswordInput.value||"";
      state.room     = (hostRoomInput.value||"onlinecompile").trim()||"onlinecompile";
      localStorage.setItem("livepy:name:host",   state.name);
      localStorage.setItem("livepy:hostUsername", state.username);
      localStorage.setItem("livepy:hostRoom",     state.room);
    } else {
      state.name     = (studentNameInput.value||"Student").trim()||"Student";
      state.username = ""; state.password = ""; state.roomAction = "join";
      state.room     = (studentRoomInput.value||"onlinecompile").trim()||"onlinecompile";
      localStorage.setItem("livepy:name:student", state.name);
      localStorage.setItem("livepy:studentRoom",  state.room);
    }
    updateHeaderInfo();
  }
  function fillAuthFormFromStorage() {
    hostNameInput.value     = (qs.get("name")||localStorage.getItem("livepy:name:host")||"Ведущий").trim()||"Ведущий";
    hostUsernameInput.value = (qs.get("username")||localStorage.getItem("livepy:hostUsername")||"HOST").trim()||"HOST";
    hostRoomInput.value     = (qs.get("room")||localStorage.getItem("livepy:hostRoom")||"onlinecompile").trim()||"onlinecompile";
    studentNameInput.value  = (qs.get("name")||localStorage.getItem("livepy:name:student")||"Student").trim()||"Student";
    studentRoomInput.value  = (qs.get("room")||localStorage.getItem("livepy:studentRoom")||"onlinecompile").trim()||"onlinecompile";
    const qsMode = (qs.get("roomMode")||"").toLowerCase();
    state.roomAction = qsMode === "join" ? "join" : "create";
    const storedRole = localStorage.getItem("livepy:lastRole")||"host";
    const roleParam = (qs.get("role")||"").toLowerCase();
    setRole(roleParam === "student" ? "student" : roleParam === "host" ? "host" : (storedRole === "student" ? "student" : "host"));
    showAuthError("");
  }
  function startSession() {
    showAuthError("");
    applyAuthFromUI();
    if (state.role === "host" && !state.password) {
      showAuthError("Введите пароль преподавателя.");
      hostPasswordInput.focus();
      return;
    }
    state.manualClose = false; state.hasConnectedOnce = false;
    showWorkspace(true); showEntryScreen(false);
    setReadOnly(true);
    clearTerm(); clearChat();
    writeTermLine("\x1b[2m[system]\x1b[0m Подготовка подключения…\r\n");
    connect();
    requestAnimationFrame(fitXterm);
  }
  function leaveRoom() {
    state.manualClose = true;
    stopPingLoop(); stopAutosaveLoop(); stopHostReturnCountdown();
    clearTimeout(state.reconnectTimer);
    try { state.ws?.close(); } catch {}
    state.ws = null; state.me = null; state.hasConnectedOnce = false;
    setConnectionBadge("Не подключено", "idle");
    showWorkspace(false); showEntryScreen(true);
  }

  // ─── WebSocket message handler ────────────────────────────────────
  function handleMessage(msg) {
    switch (msg.type) {
      case "auth_error":
        state.isRunning = false; updateButtons();
        setConnectionBadge("Ошибка", "error");
        showWorkspace(false); showEntryScreen(true);
        showAuthError(msg.message||"Ошибка авторизации");
        toast(msg.message||"Ошибка авторизации");
        state.manualClose = true; state.ws?.close();
        break;
      case "welcome":
        state.hasConnectedOnce = true;
        state.me = msg.you;
        state.room = msg.room||state.room;
        state.docText = msg.doc.text||"";
        state.docVersion = Number(msg.doc.version)||0;
        state.currentFilename = msg.filename||"main.py";
        resetPendingSync();
        state.applyingRemote = true; setEditorText(state.docText); state.applyingRemote = false;
        showAuthError(""); showWorkspace(true); showEntryScreen(false);
        updateHeaderInfo();
        updateFileList(msg.files||[], state.currentFilename);
        renderParticipants(msg.participants||[]);
        updateEditorLayout(); updateCursorStatus(); setDocVersionLabel();
        setConnectionBadge("Подключено","ok");
        stopHostReturnCountdown();
        setHostStatusBadge("Ведущий: онлайн","ok");
        startAutosaveLoop();
        writeTermLine(`\x1b[32m[room]\x1b[0m Подключено к комнате \x1b[1m${msg.room||state.room}\x1b[0m\r\n`);
        window.history.replaceState({},"","/onlinecompile");
        focusEditor(); fitXterm();
        break;
      case "participants":
        renderParticipants(msg.participants||[]);
        break;
      case "doc_update": {
        const ver = Number(msg.version)||state.docVersion;
        if (msg.by_id === state.me?.id) { confirmOwnPatch(ver); break; }
        if (state.awaitingFullSync || hasPendingLocalEdit() || ver !== state.docVersion+1) { requestFull(); break; }
        if (applyRemotePatch(msg.patch)) { state.docVersion = ver; setDocVersionLabel(); }
        break;
      }
      case "doc_full":
        state.docText = msg.doc.text||"";
        state.docVersion = Number(msg.doc.version)||state.docVersion;
        state.currentFilename = msg.filename||state.currentFilename;
        resetPendingSync();
        state.applyingRemote = true; setEditorText(state.docText); state.applyingRemote = false;
        updateEditorLayout(); updateCursorStatus();
        currentFileSpan.textContent = state.currentFilename;
        setDocVersionLabel(); renderRemoteCursors();
        break;
      case "cursor": {
        const target = state.participants.find(p => p.id === msg.id);
        if (target && (target.role==="host"||target.can_edit)) target.cursor = { line:msg.line, col:msg.col };
        renderRemoteCursors();
        break;
      }
      case "error": {
        const err = String(msg.message||"");
        writeTermLine(`\x1b[31m[error]\x1b[0m ${err}\r\n`);
        if (/верс|патч|границ|документ|редактирован|диапазон/i.test(err)) requestFull();
        break;
      }
      case "chat":
        appendChat({ from:msg.from, fromId:msg.from_id, text:msg.text, color:msg.color });
        break;
      case "chat_throttled": {
        const now = Date.now();
        const retry = Math.max(0, Number(msg.retry_after) || 0);
        state.chatNextAllowed = Math.max(state.chatNextAllowed, now + retry * 1000);
        state.chatSpamUntil   = Math.max(state.chatSpamUntil, now + CHAT_SPAM_PENALTY_MS);
        toast(msg.message || "Антиспам: слишком много сообщений.");
        break;
      }
      case "syntax_result":
        if (msg.ok) writeTermLine("\x1b[32m[syntax]\x1b[0m OK\r\n");
        else        writeTermLine(`\x1b[33m[syntax]\x1b[0m ${msg.error||"Ошибка синтаксиса"}\r\n`);
        break;
      case "run_state":
        state.isRunning = Boolean(msg.running); state.stopRequested = false;
        updateButtons();
        if (msg.clear) clearTerm();
        if (msg.running) writeTermLine(`\x1b[32m[run]\x1b[0m Запуск \x1b[1m${msg.filename||state.currentFilename}\x1b[0m\r\n`);
        else             writeTermLine("\x1b[2m[run]\x1b[0m Завершено\r\n");
        break;
      case "run_output":
        writeTermText(msg.text||"");
        break;
      case "run_result":
        writeTermLine(`\r\n\x1b[2m[exit]\x1b[0m code=${msg.returncode??"-"} | timeout=${msg.timeout?"yes":"no"} | stopped=${msg.stopped?"yes":"no"} | ${msg.elapsed_ms}ms\r\n`);
        break;
      case "save_result": {
        const isStudent = msg.scope === "student_file";
        const okLabel = isStudent ? `личная копия: ${msg.filename}` : (msg.filename||state.currentFilename);
        const errLabel = msg.error||"Ошибка сохранения";
        if (state.role==="host") setAutosaveBadge(msg.ok ? `Сохранено: ${okLabel}` : "Сохранение: ошибка", msg.ok?"ok":"error");
        if (msg.ok) writeTermLine(`\x1b[32m[save]\x1b[0m ${okLabel}\r\n`);
        else        writeTermLine(`\x1b[31m[save-error]\x1b[0m ${errLabel}\r\n`);
        break;
      }
      case "files":
        updateFileList(msg.files||[], msg.current||state.currentFilename);
        state.currentFilename = msg.current||state.currentFilename;
        currentFileSpan.textContent = state.currentFilename;
        break;
      case "host_disconnected":
        startHostReturnCountdown(msg.timeout_seconds || 300);
        writeTermLine(`\x1b[33m[room]\x1b[0m ${msg.message}\r\n`);
        break;
      case "host_restored":
        stopHostReturnCountdown();
        setHostStatusBadge("Ведущий: онлайн","ok");
        writeTermLine("\x1b[32m[room]\x1b[0m Преподаватель восстановил подключение\r\n");
        break;
      case "pong": {
        const rtt = Math.max(0, Date.now() - Number(msg.ts||Date.now()));
        state.ws?.send(JSON.stringify({ type:"latency_update", latency_ms:rtt }));
        break;
      }
      case "room_closed":
        state.isRunning = false; updateButtons();
        writeTermLine(`\x1b[31m[room]\x1b[0m ${msg.message||"Комната закрыта."}\r\n`);
        toast(msg.message||"Комната закрыта.");
        state.manualClose = true; stopPingLoop(); stopAutosaveLoop(); stopHostReturnCountdown();
        setConnectionBadge("Комната закрыта","error");
        state.ws?.close(); setReadOnly(true);
        break;
      default: break;
    }
  }

  // ─── WebSocket connection ─────────────────────────────────────────
  function connect() {
    clearTimeout(state.reconnectTimer);
    setConnectionBadge("Подключение…","idle");
    const proto = location.protocol === "https:" ? "wss://" : "ws://";
    state.ws = new WebSocket(`${proto}${location.host}/ws`);
    state.ws.onopen = () => {
      state.reconnectDelayMs = 2000;
      setConnectionBadge("Авторизация…","idle");
      showAuthError("");
      state.ws.send(JSON.stringify({
        type:"hello", name:state.name, role:state.role,
        username:state.username, password:state.password,
        room:state.room, room_action:state.roomAction,
      }));
      startPingLoop();
    };
    state.ws.onmessage = e => {
      try { handleMessage(JSON.parse(e.data)); }
      catch(err) { console.warn("invalid ws message", err); writeTermLine("\x1b[31m[error]\x1b[0m Получено некорректное сообщение от сервера\r\n"); }
    };
    state.ws.onclose = () => {
      stopPingLoop(); stopAutosaveLoop();
      state.isRunning = false; state.stopRequested = false;
      updateButtons();
      if (state.manualClose) return;
      setReadOnly(true);
      const offline = navigator.onLine === false;
      setConnectionBadge(offline?"Нет сети":"Переподключение…", offline?"warn":"idle");
      if (!state.hasConnectedOnce) {
        showWorkspace(false); showEntryScreen(true);
        showAuthError("Не удалось установить соединение. Проверьте данные и доступность сервера.");
      }
      const delay = offline ? Math.max(state.reconnectDelayMs, 5000) : state.reconnectDelayMs;
      state.reconnectTimer = setTimeout(connect, delay);
      state.reconnectDelayMs = Math.min(Math.round(state.reconnectDelayMs * 1.5), 15000);
    };
    state.ws.onerror = () => setConnectionBadge("Ошибка соединения","error");
  }

  // ─── Event bindings ───────────────────────────────────────────────
  function bindEntryEvents() {
    roleCards.forEach(b => b.addEventListener("click", () => setRole(b.dataset.role)));
    hostModeBtns.forEach(b => b.addEventListener("click", () => {
      state.roomAction = b.dataset.mode === "join" ? "join" : "create";
      syncRoleVisibility();
    }));
    hostStartBtn?.addEventListener("click",    () => { setRole("host");    startSession(); });
    studentStartBtn?.addEventListener("click", () => { setRole("student"); startSession(); });
    [hostPasswordInput, hostRoomInput, studentRoomInput].forEach(inp => {
      inp?.addEventListener("keydown", e => {
        if (e.key !== "Enter") return;
        e.preventDefault();
        (state.role === "host" ? hostStartBtn : studentStartBtn)?.click();
      });
    });
    // password show/hide
    hostPasswordToggle?.addEventListener("click", () => {
      const show = hostPasswordInput.type === "password";
      hostPasswordInput.type = show ? "text" : "password";
      hostPasswordToggle.textContent = show ? "Скрыть" : "Показать";
      hostPasswordToggle.setAttribute("aria-label", show ? "Скрыть пароль" : "Показать пароль");
    });
  }

  function bindCommonEvents() {
    window.addEventListener("resize", () => { updateEditorLayout(); renderRemoteCursors(); fitXterm(); });
    window.addEventListener("offline", () => { if (!state.manualClose) setConnectionBadge("Нет сети","warn"); });
    window.addEventListener("online",  () => {
      if (state.manualClose) return;
      state.reconnectDelayMs = 1000;
      if (!state.ws || state.ws.readyState===WebSocket.CLOSED || state.ws.readyState===WebSocket.CLOSING) {
        clearTimeout(state.reconnectTimer); connect();
      }
    });

    leaveBtn?.addEventListener("click", () => {
      if (confirm("Выйти из комнаты? Несохранённые личные изменения будут потеряны.")) leaveRoom();
    });

    grantBtn.addEventListener("click", () => {
      if (!participantSelect.value) { toast("Выберите студента в списке"); return; }
      state.ws?.send(JSON.stringify({ type:"grant_edit", target_id:participantSelect.value }));
    });
    revokeBtn.addEventListener("click", () => {
      if (!participantSelect.value) { toast("Выберите студента в списке"); return; }
      state.ws?.send(JSON.stringify({ type:"revoke_edit", target_id:participantSelect.value }));
    });
    setRegionBtn.addEventListener("click", () => {
      if (!participantSelect.value) { toast("Выберите студента в списке"); return; }
      const s = Number(regionStartEl.value||1), e = Number(regionEndEl.value||1);
      if (s < 1 || e < 1) { toast("Укажите корректный диапазон строк"); return; }
      state.ws?.send(JSON.stringify({ type:"set_region", target_id:participantSelect.value, start_line:s, end_line:e }));
    });
    clearRegionBtn.addEventListener("click", () => {
      if (!participantSelect.value) { toast("Выберите студента в списке"); return; }
      state.ws?.send(JSON.stringify({ type:"clear_region", target_id:participantSelect.value }));
    });

    chatSend.addEventListener("click", sendChat);
    chatInput.addEventListener("keydown", e => { if (e.key==="Enter") { e.preventDefault(); sendChat(); } });
    chatLastOnlyBtn?.addEventListener("click", () => { state.chatLastOnly = !state.chatLastOnly; renderChat(); });
    renderChat();

    runBtn.addEventListener("click", () => state.ws?.send(JSON.stringify({ type:"run_code", code:getEditorText(), timeout:5 })));
    checkBtn.addEventListener("click", () => state.ws?.send(JSON.stringify({ type:"check_syntax", code:getEditorText() })));
    stopBtn.addEventListener("click", () => {
      if (!state.isRunning || state.stopRequested) return;
      state.stopRequested = true; updateButtons();
      writeTermLine("\x1b[33m[run]\x1b[0m Запрошена остановка…\r\n");
      state.ws?.send(JSON.stringify({ type:"stop_code" }));
    });
    clearTermBtn.addEventListener("click", clearTerm);

    createFileBtn.addEventListener("click", () => {
      const fn = prompt("Имя нового файла:", "new_file.py");
      if (!fn) return;
      state.ws?.send(JSON.stringify({ type:"create_file", filename:fn }));
    });
    importFileBtn.addEventListener("click", () => { if (!importFileInput) return; importFileInput.value=""; importFileInput.click(); });
    importFileInput?.addEventListener("change", async e => {
      const file = e.target?.files?.[0];
      if (!file) return;
      try {
        if (file.size > MAX_CLIENT_DOC_BYTES) { toast("Файл больше 1 МБ — импорт отменён"); return; }
        if (state.ws?.readyState !== WebSocket.OPEN) { toast("Нет подключения к комнате"); return; }
        state.ws.send(JSON.stringify({ type:"import_file_content", filename:file.name||state.currentFilename||"main.py", content:await file.text() }));
        toast(`Импортирован файл: ${file.name||"main.py"}`);
      } catch(err) { toast(`Не удалось прочитать файл: ${err?.message||err}`); }
      finally { importFileInput.value = ""; }
    });

    downloadFileBtn?.addEventListener("click", downloadCurrentEditorFile);
    downloadAllBtn?.addEventListener("click",  downloadRoomArchive);

    fileSelect.addEventListener("change", () => {
      if (!fileSelect.value) return;
      state.ws?.send(JSON.stringify({ type:"switch_file", filename:fileSelect.value }));
    });

    blameReportBtn.addEventListener("click", () =>
      window.open(`/api/rooms/${encodeURIComponent(state.room)}/reports/blame?filename=${encodeURIComponent(state.currentFilename)}&format=html`,"_blank","noopener"));
    scoreReportBtn.addEventListener("click", () =>
      window.open(`/api/rooms/${encodeURIComponent(state.room)}/reports/access?format=html`,"_blank","noopener"));
  }

  // ─── Init ─────────────────────────────────────────────────────────
  async function init() {
    initTheme();
    fillAuthFormFromStorage();
    updateHeaderInfo();
    syncRoleVisibility();
    showWorkspace(false);
    showEntryScreen(true);
    setReadOnly(true);
    renderChat();

    initConsoleResize();
    bindEntryEvents();
    bindCommonEvents();
    initKonamiCode();
    // Load the terminal engine FIRST: xterm's UMD bundle must bind window.Terminal
    // before Monaco's AMD loader (loader.js → window.define.amd) is added, otherwise
    // xterm registers as an AMD module and the terminal drops to the <pre> fallback.
    // Guarded so a terminal hiccup can never block the (critical) editor from loading.
    try { await initTerminal(); } catch (e) { console.warn("terminal init failed", e); }
    await initMonacoEditor();
  }

  init();
})();
