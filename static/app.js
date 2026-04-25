(() => {
  const qs = new URLSearchParams(window.location.search);
  const storedRole = localStorage.getItem("livepy:lastRole") || "host";
  const roleParam = (qs.get("role") || "").trim().toLowerCase();
  const initialRole = roleParam === "student" ? "student" : (roleParam === "host" ? "host" : (storedRole === "student" ? "student" : "host"));

  const state = {
    ws: null,
    me: null,
    role: initialRole,
    room: "",
    roomAction: initialRole === "host" ? "create" : "join",
    cursorBroadcastTimer: null,
    name: "",
    username: "",
    password: "",
    docText: "",
    docVersion: 0,
    currentFilename: "main.py",
    applyingRemote: false,
    reconnectTimer: null,
    pingTimer: null,
    autosaveTimer: null,
    manualClose: false,
    monaco: null,
    monacoEditor: null,
    monacoModel: null,
    monacoCompletionProvider: null,
    lspStatus: "local",
    participants: [],
    isRunning: false,
    hasConnectedOnce: false,
    toastTimer: null,
    easterTimer: null,
  };

  const entryScreen = document.getElementById("entryScreen");
  const appLayout = document.getElementById("appLayout");
  const roleCards = document.querySelectorAll(".role-card");
  const authErrorBox = document.getElementById("authError");
  const hostPane = document.getElementById("hostPane");
  const studentPane = document.getElementById("studentPane");
  const hostModeBtns = document.querySelectorAll(".mode-btn");
  const hostNameInput = document.getElementById("hostNameInput");
  const hostUsernameInput = document.getElementById("hostUsernameInput");
  const hostPasswordInput = document.getElementById("hostPasswordInput");
  const hostRoomInput = document.getElementById("hostRoomInput");
  const studentNameInput = document.getElementById("studentNameInput");
  const studentRoomInput = document.getElementById("studentRoomInput");
  const hostStartBtn = document.getElementById("hostStartBtn");
  const studentStartBtn = document.getElementById("studentStartBtn");
  const toastEl = document.getElementById("toast");
  const easterEggEl = document.getElementById("easterEgg");
  const closeEasterEggBtn = document.getElementById("closeEasterEgg");

  const editorTextarea = document.getElementById("editor");
  const editorMount = document.getElementById("editorMount");
  const gutterEl = document.getElementById("gutter");
  const remoteCursorsEl = document.getElementById("remoteCursors");
  const participantsEl = document.getElementById("participants");
  const participantSelect = document.getElementById("participantSelect");
  const grantBtn = document.getElementById("grantBtn");
  const revokeBtn = document.getElementById("revokeBtn");
  const clearRegionBtn = document.getElementById("clearRegionBtn");
  const setRegionBtn = document.getElementById("setRegionBtn");
  const regionStartEl = document.getElementById("regionStart");
  const regionEndEl = document.getElementById("regionEnd");
  const hostControls = document.getElementById("hostControls");
  const sessionInfo = document.getElementById("sessionInfo");
  const roleInfo = document.getElementById("roleInfo");
  const roomInfo = document.getElementById("roomInfo");
  const autosaveBadge = document.getElementById("autosaveBadge");
  const hostStatusBadge = document.getElementById("hostStatusBadge");
  const cursorPos = document.getElementById("cursorPos");
  const docVersionSpan = document.getElementById("docVersion");
  const currentFileSpan = document.getElementById("currentFile");
  const lspStatusEl = document.getElementById("lspStatus");
  const chatBox = document.getElementById("chatBox");
  const chatInput = document.getElementById("chatInput");
  const chatSend = document.getElementById("chatSend");
  const runBtn = document.getElementById("runBtn");
  const checkBtn = document.getElementById("checkBtn");
  const saveBtn = document.getElementById("saveBtn");
  const stopBtn = document.getElementById("stopBtn");
  const runOutput = document.getElementById("runOutput");
  const fileSelect = document.getElementById("fileSelect");
  const createFileBtn = document.getElementById("createFileBtn");
  const importFileBtn = document.getElementById("importFileBtn");
  const downloadFileBtn = document.getElementById("downloadFileBtn");
  const importFileInput = document.getElementById("importFileInput");
  const blameReportBtn = document.getElementById("blameReportBtn");
  const scoreReportBtn = document.getElementById("scoreReportBtn");
  const connectionBadge = document.getElementById("connectionBadge");
  const themeBtns = document.querySelectorAll(".theme-btn");

  function toast(message) {
    if (!toastEl) {
      console.log("[toast]", message);
      return;
    }
    toastEl.textContent = message;
    toastEl.classList.add("is-visible");
    clearTimeout(state.toastTimer);
    state.toastTimer = window.setTimeout(() => toastEl.classList.remove("is-visible"), 2600);
  }

  function loadScript(src) {
    return new Promise((resolve, reject) => {
      const existing = document.querySelector(`script[data-src="${src}"]`);
      if (existing) {
        if (existing.dataset.loaded === "true") {
          resolve();
          return;
        }
        existing.addEventListener("load", () => resolve(), { once: true });
        existing.addEventListener("error", () => reject(new Error(`Не удалось загрузить ${src}`)), { once: true });
        return;
      }

      const script = document.createElement("script");
      script.src = src;
      script.async = true;
      script.dataset.src = src;
      script.addEventListener("load", () => {
        script.dataset.loaded = "true";
        resolve();
      }, { once: true });
      script.addEventListener("error", () => reject(new Error(`Не удалось загрузить ${src}`)), { once: true });
      document.head.appendChild(script);
    });
  }

  function loadCss(href) {
    if (!href || document.querySelector(`link[data-href="${href}"]`)) return;
    const link = document.createElement("link");
    link.rel = "stylesheet";
    link.href = href;
    link.dataset.href = href;
    document.head.appendChild(link);
  }

  function isDarkThemeActive() {
    if (document.body.classList.contains("theme-dark")) return true;
    if (document.body.classList.contains("theme-light")) return false;
    return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
  }

  function syncMonacoTheme() {
    if (!state.monaco?.editor) return;
    state.monaco.editor.setTheme(isDarkThemeActive() ? "vs-dark" : "vs");
  }

  function getEditorText() {
    if (state.monacoModel) return state.monacoModel.getValue();
    return editorTextarea.value;
  }

  function setEditorText(text) {
    const safeText = String(text ?? "");
    if (state.monacoModel) {
      const current = state.monacoModel.getValue();
      if (current === safeText) return;
      state.monacoModel.setValue(safeText);
      return;
    }
    editorTextarea.value = safeText;
  }

  function getSelectionStart() {
    if (state.monacoEditor && state.monacoModel) {
      const selection = state.monacoEditor.getSelection();
      if (!selection) return 0;
      return state.monacoModel.getOffsetAt(selection.getStartPosition());
    }
    return editorTextarea.selectionStart || 0;
  }

  function getSelectionEnd() {
    if (state.monacoEditor && state.monacoModel) {
      const selection = state.monacoEditor.getSelection();
      if (!selection) return 0;
      return state.monacoModel.getOffsetAt(selection.getEndPosition());
    }
    return editorTextarea.selectionEnd || 0;
  }

  function focusEditor() {
    if (state.monacoEditor) {
      state.monacoEditor.focus();
    } else {
      editorTextarea.focus();
    }
  }

  function setReadOnly(readOnly) {
    editorTextarea.readOnly = readOnly;
    editorTextarea.classList.toggle("readonly", readOnly);

    if (state.monacoEditor) {
      state.monacoEditor.updateOptions({ readOnly });
      editorMount.classList.toggle("readonly", readOnly);
    }
  }

  function setConnectionBadge(text, kind = "idle") {
    connectionBadge.textContent = text;
    connectionBadge.dataset.state = kind;
  }

  function setAutosaveBadge(text, kind = "idle") {
    autosaveBadge.textContent = text;
    autosaveBadge.dataset.state = kind;
  }

  function setHostStatusBadge(text, kind = "ok") {
    hostStatusBadge.textContent = text;
    hostStatusBadge.dataset.state = kind;
  }

  function setLspStatus(text, kind = "idle") {
    state.lspStatus = text;
    lspStatusEl.textContent = text;
    lspStatusEl.parentElement.dataset.state = kind;
  }

  function showAuthError(message = "") {
    if (!authErrorBox) return;
    authErrorBox.hidden = !message;
    authErrorBox.textContent = message;
  }

  function showEntryScreen(visible) {
    entryScreen.hidden = !visible;
    document.body.classList.toggle("app-locked", visible);
  }

  function showWorkspace(visible) {
    appLayout.hidden = !visible;
    appLayout.classList.toggle("is-active", visible);
  }

  function syncRoleVisibility() {
    roleCards.forEach((button) => button.classList.toggle("active", button.dataset.role === state.role));
    hostPane.hidden = state.role !== "host";
    studentPane.hidden = state.role !== "student";
    hostModeBtns.forEach((button) => button.classList.toggle("active", button.dataset.mode === state.roomAction));
    hostControls.style.display = state.role === "host" ? "flex" : "none";
  }

  function setRole(role) {
    state.role = role === "student" ? "student" : "host";
    if (state.role !== "host") {
      state.roomAction = "join";
    } else if (!["create", "join"].includes(state.roomAction)) {
      state.roomAction = "create";
    }
    localStorage.setItem("livepy:lastRole", state.role);
    syncRoleVisibility();
    updateHeaderInfo();
  }

  function fillAuthFormFromQueryOrStorage() {
    hostNameInput.value = (qs.get("name") || localStorage.getItem("livepy:name:host") || "Ведущий").trim() || "Ведущий";
    hostUsernameInput.value = (qs.get("username") || localStorage.getItem("livepy:hostUsername") || "Ведущий").trim() || "Ведущий";
    hostRoomInput.value = (qs.get("room") || localStorage.getItem("livepy:hostRoom") || "onlinecompile").trim() || "onlinecompile";

    studentNameInput.value = (qs.get("name") || localStorage.getItem("livepy:name:student") || "Student").trim() || "Student";
    studentRoomInput.value = (qs.get("room") || localStorage.getItem("livepy:studentRoom") || "onlinecompile").trim() || "onlinecompile";

    const qsMode = (qs.get("roomMode") || "").trim().toLowerCase();
    state.roomAction = qsMode === "join" ? "join" : "create";
    setRole(initialRole);
    showAuthError("");
  }

  function applyAuthFromUI() {
    if (state.role === "host") {
      state.name = (hostNameInput.value || "Ведущий").trim() || "Ведущий";
      state.username = (hostUsernameInput.value || "Ведущий").trim() || "Ведущий";
      state.password = hostPasswordInput.value || "";
      state.room = (hostRoomInput.value || "onlinecompile").trim() || "onlinecompile";
      localStorage.setItem("livepy:name:host", state.name);
      localStorage.setItem("livepy:hostUsername", state.username);
      localStorage.setItem("livepy:hostRoom", state.room);
    } else {
      state.name = (studentNameInput.value || "Student").trim() || "Student";
      state.username = "";
      state.password = "";
      state.room = (studentRoomInput.value || "onlinecompile").trim() || "onlinecompile";
      state.roomAction = "join";
      localStorage.setItem("livepy:name:student", state.name);
      localStorage.setItem("livepy:studentRoom", state.room);
    }

    updateHeaderInfo();
  }

  function startSession() {
    showAuthError("");
    applyAuthFromUI();
    state.manualClose = false;
    state.hasConnectedOnce = false;
    showWorkspace(true);
    showEntryScreen(false);
    setReadOnly(true);
    clearConsole();
    appendConsole(`[system] Подготовка подключения\n`);
    connect();
  }

  function applyTheme(theme) {
    const safeTheme = ["light", "dark", "system"].includes(theme) ? theme : "system";
    document.body.classList.remove("theme-light", "theme-dark");
    if (safeTheme === "light") {
      document.body.classList.add("theme-light");
    } else if (safeTheme === "dark") {
      document.body.classList.add("theme-dark");
    }
    localStorage.setItem("livepy:theme", safeTheme);
    themeBtns.forEach((btn) => btn.classList.toggle("active", btn.dataset.theme === safeTheme));
    syncMonacoTheme();
  }

  function initTheme() {
    const savedTheme = localStorage.getItem("livepy:theme") || "light";
    applyTheme(savedTheme);
    themeBtns.forEach((btn) => btn.addEventListener("click", () => applyTheme(btn.dataset.theme)));
  }

  function hideEasterEgg() {
    if (!easterEggEl) return;
    easterEggEl.hidden = true;
    document.title = "onlinecompile";
  }

  function triggerEasterEgg() {
    if (!easterEggEl) return;
    easterEggEl.hidden = false;
    document.title = "avoselection // onlinecompile";
    toast("Пасхалка avoselection активирована");
    clearTimeout(state.easterTimer);
    state.easterTimer = window.setTimeout(hideEasterEgg, 5000);
  }

  function initKonamiCode() {
    const sequence = ["arrowup", "arrowup", "arrowdown", "arrowdown", "arrowleft", "arrowright", "arrowleft", "arrowright", "a", "b"];
    let cursor = 0;

    document.addEventListener("keydown", (event) => {
      const key = String(event.key || "").toLowerCase();
      if (key === sequence[cursor]) {
        cursor += 1;
        if (cursor === sequence.length) {
          cursor = 0;
          triggerEasterEgg();
        }
        return;
      }
      cursor = key === sequence[0] ? 1 : 0;
    });

    closeEasterEggBtn?.addEventListener("click", hideEasterEgg);
    easterEggEl?.addEventListener("click", (event) => {
      if (event.target === easterEggEl) hideEasterEgg();
    });
  }

  function appendConsole(text) {
    runOutput.textContent += text;
    runOutput.scrollTop = runOutput.scrollHeight;
  }

  function clearConsole() {
    runOutput.textContent = "";
  }

  function appendChat(line) {
    const item = document.createElement("div");
    item.className = "chat-line";
    item.textContent = line;
    chatBox.appendChild(item);
    chatBox.scrollTop = chatBox.scrollHeight;
  }

  function updateHeaderInfo() {
    sessionInfo.textContent = `Вы: ${state.name}`;
    roleInfo.textContent = state.role === "host" ? "Роль: ведущий" : "Роль: студент";
    roomInfo.textContent = `Комната: ${state.room || "onlinecompile"}`;
    currentFileSpan.textContent = state.currentFilename || "main.py";
  }

  function currentClientCanEdit() {
    if (state.role === "host") return true;
    const meInfo = state.participants.find((participant) => participant.id === state.me?.id);
    if (meInfo) return Boolean(meInfo.can_edit);
    return Boolean(state.me?.can_edit);
  }

  function updateFileList(files = [], current = "main.py") {
    fileSelect.innerHTML = "";
    files.forEach((filename) => {
      const option = document.createElement("option");
      option.value = filename;
      option.textContent = filename;
      option.selected = filename === current;
      fileSelect.appendChild(option);
    });
    currentFileSpan.textContent = current;
  }

  function updateGutter() {
    const lines = getEditorText().split("\n").length;
    const digits = String(lines).length;
    gutterEl.style.width = `${Math.max(48, digits * 12 + 20)}px`;
    gutterEl.textContent = Array.from({ length: lines }, (_, index) => String(index + 1)).join("\n");
  }

  function indexToLineCol(text, idx) {
    let line = 1;
    let col = 1;
    for (let i = 0; i < idx; i += 1) {
      if (text[i] === "\n") {
        line += 1;
        col = 1;
      } else {
        col += 1;
      }
    }
    return { line, col };
  }

  function updateCursorStatus() {
    const pos = getSelectionStart();
    const { line, col } = indexToLineCol(getEditorText(), pos);
    cursorPos.textContent = `Ln ${line}, Col ${col}`;

    if (state.cursorBroadcastTimer) {
      window.clearTimeout(state.cursorBroadcastTimer);
      state.cursorBroadcastTimer = null;
    }

    if (state.ws && state.ws.readyState === WebSocket.OPEN && currentClientCanEdit()) {
      state.cursorBroadcastTimer = window.setTimeout(() => {
        state.ws?.send(JSON.stringify({ type: "cursor", line, col }));
      }, 30);
    }
  }

  function lineColToCoords(line, col) {
    if (state.monacoEditor && state.monacoModel && state.monaco) {
      try {
        const safeLine = Math.max(1, Math.min(line, state.monacoModel.getLineCount()));
        const maxColumn = state.monacoModel.getLineMaxColumn(safeLine);
        const safeColumn = Math.max(1, Math.min(col, maxColumn));
        const position = new state.monaco.Position(safeLine, safeColumn);
        const coords = state.monacoEditor.getScrolledVisiblePosition(position);
        if (coords) {
          return { top: coords.top, left: coords.left };
        }
      } catch (error) {
        console.debug("monaco cursor coords fallback", error);
      }
    }
    return {
      top: 14 + (line - 1) * 21,
      left: 14 + (col - 1) * 8,
    };
  }

  function renderRemoteCursors() {
    if (!state.me) return;
    remoteCursorsEl.innerHTML = "";
    if (!currentClientCanEdit()) return;
    const participants = state.participants || [];

    participants.forEach((participant) => {
      if (!participant.can_edit || !participant.cursor || participant.id === state.me.id) return;
      const { top, left } = lineColToCoords(participant.cursor.line, participant.cursor.col);

      const wrapper = document.createElement("div");
      wrapper.className = "remote-cursor";
      wrapper.style.top = `${top}px`;
      wrapper.style.left = `${left}px`;

      const caret = document.createElement("div");
      caret.className = "remote-caret";
      caret.style.background = participant.color;
      caret.style.height = "18px";

      const label = document.createElement("div");
      label.className = "remote-cursor-label";
      label.style.background = participant.color;
      label.textContent = participant.name;

      wrapper.appendChild(caret);
      wrapper.appendChild(label);
      remoteCursorsEl.appendChild(wrapper);
    });
  }

  function updateButtons(canEdit) {
    setReadOnly(!canEdit);

    if (state.role === "host") {
      runBtn.disabled = state.isRunning;
      checkBtn.disabled = state.isRunning;
      saveBtn.disabled = false;
      stopBtn.disabled = !state.isRunning;
      fileSelect.disabled = false;
      createFileBtn.disabled = false;
      importFileBtn.disabled = false;
      blameReportBtn.disabled = false;
      scoreReportBtn.disabled = false;
      return;
    }

    runBtn.disabled = true;
    checkBtn.disabled = true;
    saveBtn.disabled = true;
    stopBtn.disabled = true;
    fileSelect.disabled = true;
    createFileBtn.disabled = true;
    importFileBtn.disabled = true;
    blameReportBtn.disabled = true;
    scoreReportBtn.disabled = true;
  }

  function renderParticipants(list) {
    state.participants = (list || []).map((item) => ({
      ...item,
      cursor: state.participants?.find((p) => p.id === item.id)?.cursor || null,
    }));

    participantsEl.innerHTML = "";
    participantSelect.innerHTML = "";

    state.participants.forEach((participant) => {
      const row = document.createElement("li");
      row.className = "participant-item";
      const roleLabel = participant.role === "host" ? "ведущий" : `студент · ${participant.can_edit ? "ред." : "просмотр"}`;
      const metricsLabel = participant.role === "host"
        ? `${participant.latency_ms != null ? `Задержка: ${participant.latency_ms} ms` : ""}`
        : `Выдач доступа: ${participant.access_grants ?? 0}${participant.latency_ms != null ? ` · ${participant.latency_ms} ms` : ""}`;
      row.innerHTML = `
        <span class="participant-color" style="background:${participant.color}"></span>
        <div class="participant-meta">
          <strong>${participant.name}</strong>
          <span>${roleLabel}${participant.region ? ` · строки ${participant.region[0]}-${participant.region[1]}` : ""}</span>
          ${metricsLabel ? `<span>${metricsLabel}</span>` : ""}
        </div>
      `;
      participantsEl.appendChild(row);

      if (participant.role === "student") {
        const option = document.createElement("option");
        option.value = participant.id;
        option.textContent = `${participant.name} (${participant.access_grants ?? 0} выдач)`;
        participantSelect.appendChild(option);
      }
    });

    const meInfo = state.participants.find((participant) => participant.id === state.me?.id);
    if (meInfo) state.me = { ...state.me, can_edit: Boolean(meInfo.can_edit) };
    updateButtons(Boolean(meInfo?.can_edit));
    renderRemoteCursors();
  }

  function computePatch(oldText, newText) {
    if (oldText === newText) {
      return { start: 0, end: 0, text: "" };
    }
    let start = 0;
    while (start < oldText.length && start < newText.length && oldText[start] === newText[start]) start += 1;

    let oldEnd = oldText.length;
    let newEnd = newText.length;
    while (oldEnd > start && newEnd > start && oldText[oldEnd - 1] === newText[newEnd - 1]) {
      oldEnd -= 1;
      newEnd -= 1;
    }

    return { start, end: oldEnd, text: newText.slice(start, newEnd) };
  }

  function applyRemotePatch(patch) {
    const currentText = getEditorText();
    const nextText = currentText.slice(0, patch.start) + patch.text + currentText.slice(patch.end);
    state.applyingRemote = true;

    if (state.monacoEditor && state.monacoModel) {
      const startPos = state.monacoModel.getPositionAt(patch.start);
      const endPos = state.monacoModel.getPositionAt(patch.end);
      state.monacoEditor.executeEdits("remote-sync", [{
        range: new state.monaco.Range(startPos.lineNumber, startPos.column, endPos.lineNumber, endPos.column),
        text: patch.text,
        forceMoveMarkers: true,
      }]);
    } else {
      setEditorText(nextText);
    }

    state.applyingRemote = false;
    state.docText = nextText;
    updateGutter();
    updateCursorStatus();
  }

  function requestFull() {
    if (state.ws && state.ws.readyState === WebSocket.OPEN) {
      state.ws.send(JSON.stringify({ type: "request_full" }));
    }
  }

  function sendChat() {
    const text = chatInput.value.trim();
    if (!text) return;
    state.ws?.send(JSON.stringify({ type: "chat", text }));
    chatInput.value = "";
  }

  function startPingLoop() {
    clearInterval(state.pingTimer);
    state.pingTimer = setInterval(() => {
      if (state.ws && state.ws.readyState === WebSocket.OPEN) {
        state.ws.send(JSON.stringify({ type: "ping", ts: Date.now() }));
      }
    }, 5000);
  }

  function stopPingLoop() {
    clearInterval(state.pingTimer);
    state.pingTimer = null;
  }

  function startAutosaveLoop() {
    clearInterval(state.autosaveTimer);
    if (state.role !== "host") {
      setAutosaveBadge("Автосохранение: через сервер", "idle");
      return;
    }
    setAutosaveBadge("Автосохранение: активно", "ok");
    state.autosaveTimer = setInterval(() => {
      if (!state.ws || state.ws.readyState !== WebSocket.OPEN) return;
      state.ws.send(JSON.stringify({ type: "autosave", code: getEditorText(), filename: state.currentFilename }));
    }, 15000);
  }

  function stopAutosaveLoop() {
    clearInterval(state.autosaveTimer);
    state.autosaveTimer = null;
  }

  function collectLocalSuggestions() {
    const pythonKeywords = [
      "False", "None", "True", "and", "as", "assert", "async", "await", "break", "class", "continue",
      "def", "del", "elif", "else", "except", "finally", "for", "from", "global", "if", "import",
      "in", "is", "lambda", "nonlocal", "not", "or", "pass", "raise", "return", "try", "while",
      "with", "yield", "match", "case", "print", "len", "range", "list", "dict", "set", "tuple",
      "str", "int", "float", "bool", "input", "enumerate", "zip", "map", "filter", "sum", "min", "max"
    ];

    const snippetOptions = [
      { label: "def", type: "keyword", detail: "функция", apply: `def function_name():
    pass` },
      { label: "class", type: "keyword", detail: "класс", apply: `class ClassName:
    pass` },
      { label: "for", type: "keyword", detail: "цикл", apply: `for item in iterable:
    pass` },
      { label: "while", type: "keyword", detail: "цикл", apply: `while condition:
    pass` },
      { label: "if", type: "keyword", detail: "условие", apply: `if condition:
    pass` },
      { label: "try", type: "keyword", detail: "обработка исключений", apply: `try:
    pass
except Exception as error:
    print(error)` },
      { label: "with", type: "keyword", detail: "контекст", apply: `with open("file.txt", "r", encoding="utf-8") as file:
    data = file.read()` },
    ];

    const documentWords = Array.from(new Set((getEditorText().match(/[A-Za-z_][A-Za-z0-9_]*/g) || [])))
      .filter((word) => word.length >= 2)
      .slice(0, 400)
      .map((word) => ({ label: word, type: "variable", detail: "из текущего файла" }));

    const keywordOptions = pythonKeywords.map((word) => ({ label: word, type: "keyword", detail: "Python" }));
    return [...snippetOptions, ...keywordOptions, ...documentWords];
  }

  async function initMonacoEditor() {
    try {
      const monacoCandidates = [
        {
          name: "bundled",
          base: "/static/vendor/monaco",
          loader: "/static/vendor/monaco/loader.js",
          css: "/static/vendor/monaco/editor/editor.main.css",
          workers: {
            editorWorkerService: "/static/vendor/monaco/assets/editor.worker-Be8ye1pW.js",
            json: "/static/vendor/monaco/assets/json.worker-DKiEKt88.js",
            css: "/static/vendor/monaco/assets/css.worker-HnVq6Ewq.js",
            scss: "/static/vendor/monaco/assets/css.worker-HnVq6Ewq.js",
            less: "/static/vendor/monaco/assets/css.worker-HnVq6Ewq.js",
            html: "/static/vendor/monaco/assets/html.worker-B51mlPHg.js",
            handlebars: "/static/vendor/monaco/assets/html.worker-B51mlPHg.js",
            razor: "/static/vendor/monaco/assets/html.worker-B51mlPHg.js",
            typescript: "/static/vendor/monaco/assets/ts.worker-CMbG-7ft.js",
            javascript: "/static/vendor/monaco/assets/ts.worker-CMbG-7ft.js",
          },
        },
      ];

      let monacoBase = null;
      let lastError = null;

      for (const candidate of monacoCandidates) {
        try {
          loadCss(candidate.css);
          if (!window.require) {
            await loadScript(candidate.loader);
          }

          window.MonacoEnvironment = {
            getWorkerUrl(_moduleId, label) {
              if (candidate.workers) {
                return candidate.workers[label] || candidate.workers.editorWorkerService;
              }
              const workerSource = `self.MonacoEnvironment = { baseUrl: '${candidate.base}/' }; importScripts('${candidate.base}/base/worker/workerMain.js');`;
              return `data:text/javascript;charset=utf-8,${encodeURIComponent(workerSource)}`;
            },
          };

          await new Promise((resolve, reject) => {
            window.require.config({ paths: { vs: candidate.base } });
            window.require(["vs/editor/editor.main"], resolve, reject);
          });

          monacoBase = candidate.base;
          break;
        } catch (error) {
          lastError = error;
          console.warn(`Monaco load failed from ${candidate.name}`, error);
        }
      }

      if (!monacoBase || !window.monaco) {
        throw lastError || new Error("Monaco was not loaded");
      }

      state.monaco = window.monaco;
      syncMonacoTheme();

      if (state.monacoCompletionProvider) {
        state.monacoCompletionProvider.dispose();
      }

      state.monacoCompletionProvider = state.monaco.languages.registerCompletionItemProvider("python", {
        triggerCharacters: [".", "_"],
        provideCompletionItems(model, position) {
          const word = model.getWordUntilPosition(position);
          const typed = String(word?.word || "").toLowerCase();
          const range = new state.monaco.Range(
            position.lineNumber,
            word?.startColumn || position.column,
            position.lineNumber,
            word?.endColumn || position.column,
          );

          const suggestions = collectLocalSuggestions()
            .filter((option) => !typed || option.label.toLowerCase().includes(typed))
            .slice(0, 200)
            .map((option, index) => {
              let kind = state.monaco.languages.CompletionItemKind.Text;
              if (option.type === "keyword") kind = state.monaco.languages.CompletionItemKind.Keyword;
              if (option.type === "variable") kind = state.monaco.languages.CompletionItemKind.Variable;
              return {
                label: option.label,
                kind,
                detail: option.detail || "",
                insertText: option.apply || option.label,
                insertTextRules: option.apply
                  ? state.monaco.languages.CompletionItemInsertTextRule.InsertAsSnippet
                  : undefined,
                range,
                sortText: String(index).padStart(4, "0"),
              };
            });

          return { suggestions };
        },
      });

      state.monacoModel = state.monaco.editor.createModel(editorTextarea.value || "", "python");
      state.monacoEditor = state.monaco.editor.create(editorMount, {
        model: state.monacoModel,
        automaticLayout: true,
        minimap: { enabled: false },
        lineNumbers: "off",
        glyphMargin: false,
        folding: false,
        lineDecorationsWidth: 0,
        lineNumbersMinChars: 0,
        overviewRulerLanes: 0,
        hideCursorInOverviewRuler: true,
        scrollBeyondLastLine: false,
        wordWrap: "off",
        tabSize: 4,
        insertSpaces: true,
        detectIndentation: false,
        fontSize: 14,
        lineHeight: 21,
        fontFamily: 'JetBrains Mono, Fira Code, Menlo, Consolas, monospace',
        quickSuggestions: { other: true, comments: false, strings: true },
        suggestOnTriggerCharacters: true,
        acceptSuggestionOnEnter: "on",
        renderLineHighlight: "line",
        scrollbar: { alwaysConsumeMouseWheel: false },
        padding: { top: 14, bottom: 14 },
        readOnly: editorTextarea.readOnly,
      });

      state.monacoEditor.onDidChangeModelContent(() => {
        const text = state.monacoModel.getValue();
        editorTextarea.value = text;
        updateGutter();
        updateCursorStatus();
        renderRemoteCursors();

        if (state.applyingRemote) return;
        if (!state.ws || state.ws.readyState !== WebSocket.OPEN) return;

        const patch = computePatch(state.docText, text);
        if (patch.start === 0 && patch.end === 0 && patch.text === "") return;

        state.ws.send(JSON.stringify({
          type: "patch",
          baseVersion: state.docVersion,
          start: patch.start,
          end: patch.end,
          text: patch.text,
        }));

        state.docText = text;
        state.docVersion += 1;
        docVersionSpan.textContent = `v${state.docVersion}`;
      });

      state.monacoEditor.onDidChangeCursorSelection(() => {
        updateCursorStatus();
        renderRemoteCursors();
      });
      state.monacoEditor.onDidScrollChange(() => renderRemoteCursors());

      editorMount.classList.add("is-active");
      editorMount.style.display = "block";
      editorTextarea.style.display = "none";
      setLspStatus("Monaco Editor: подсветка и локальные подсказки активны", "ok");
      updateGutter();
      updateCursorStatus();
      renderRemoteCursors();
    } catch (error) {
      console.warn("Monaco unavailable, fallback to textarea.", error);
      setLspStatus("Monaco недоступен, включён текстовый режим", "error");
      editorTextarea.style.display = "block";
      editorMount.style.display = "none";
    }
  }

  function handleMessage(msg) {
    switch (msg.type) {
      case "auth_error":
        state.isRunning = false;
        updateButtons(currentClientCanEdit());
        setConnectionBadge("Ошибка", "error");
        showWorkspace(false);
        showEntryScreen(true);
        showAuthError(msg.message || "Ошибка авторизации");
        toast(msg.message || "Ошибка авторизации");
        state.manualClose = true;
        state.ws?.close();
        break;

      case "welcome":
        state.hasConnectedOnce = true;
        state.me = msg.you;
        state.room = msg.room || state.room;
        state.docText = msg.doc.text || "";
        state.docVersion = msg.doc.version || 0;
        state.currentFilename = msg.filename || "main.py";

        state.applyingRemote = true;
        setEditorText(state.docText);
        state.applyingRemote = false;

        showAuthError("");
        showWorkspace(true);
        showEntryScreen(false);
        updateHeaderInfo();
        updateFileList(msg.files || [], state.currentFilename);
        renderParticipants(msg.participants || []);
        updateGutter();
        updateCursorStatus();
        docVersionSpan.textContent = `v${state.docVersion}`;
        setConnectionBadge("Подключено", "ok");
        setHostStatusBadge("Ведущий: онлайн", "ok");
        appendConsole(`[room] Подключено к комнате ${state.room}\n`);
        window.history.replaceState({}, "", "/onlinecompile");
        focusEditor();
        break;

      case "participants":
        renderParticipants(msg.participants || []);
        break;

      case "doc_update":
        if (msg.by_id === state.me?.id) {
          state.docVersion = msg.version;
          docVersionSpan.textContent = `v${state.docVersion}`;
          break;
        }
        applyRemotePatch(msg.patch);
        state.docVersion = msg.version;
        docVersionSpan.textContent = `v${state.docVersion}`;
        break;

      case "doc_full":
        state.docText = msg.doc.text || "";
        state.docVersion = msg.doc.version || state.docVersion;
        state.currentFilename = msg.filename || state.currentFilename;
        state.applyingRemote = true;
        setEditorText(state.docText);
        state.applyingRemote = false;
        updateGutter();
        updateCursorStatus();
        currentFileSpan.textContent = state.currentFilename;
        docVersionSpan.textContent = `v${state.docVersion}`;
        break;

      case "cursor": {
        const target = state.participants.find((item) => item.id === msg.id);
        if (target && target.can_edit) {
          target.cursor = { line: msg.line, col: msg.col };
        }
        renderRemoteCursors();
        break;
      }

      case "error":
        appendConsole(`[error] ${msg.message}\n`);
        requestFull();
        break;

      case "chat":
        appendChat(`${msg.from}: ${msg.text}`);
        break;

      case "syntax_result":
        appendConsole(msg.ok ? "[syntax] OK\n" : `[syntax] ${msg.error}\n`);
        break;

      case "run_state":
        state.isRunning = Boolean(msg.running);
        updateButtons(currentClientCanEdit());
        if (msg.clear) clearConsole();
        appendConsole(msg.running ? `[run] Запуск ${msg.filename}
` : `[run] Завершено
`);
        break;

      case "run_output":
        appendConsole(msg.text || "");
        break;

      case "run_result":
        appendConsole(`\n[exit] code=${msg.returncode ?? "none"} | timeout=${msg.timeout ? "yes" : "no"} | ${msg.elapsed_ms} ms\n`);
        break;

      case "save_result":
        setAutosaveBadge(msg.ok ? `Автосохранение: ${msg.filename}` : "Автосохранение: ошибка", msg.ok ? "ok" : "error");
        appendConsole(msg.ok ? `[save] ${msg.filename}\n` : `[save-error] ${msg.error}\n`);
        break;

      case "files":
        updateFileList(msg.files || [], msg.current || state.currentFilename);
        state.currentFilename = msg.current || state.currentFilename;
        currentFileSpan.textContent = state.currentFilename;
        break;

      case "host_disconnected":
        setHostStatusBadge(`Ведущий: переподключение до ${msg.deadline_human}`, "warn");
        appendConsole(`[room] ${msg.message}\n`);
        break;

      case "host_restored":
        setHostStatusBadge("Ведущий: онлайн", "ok");
        appendConsole("[room] Преподаватель восстановил подключение\n");
        break;

      case "pong": {
        const now = Date.now();
        const rtt = Math.max(0, now - Number(msg.ts || now));
        if (state.ws && state.ws.readyState === WebSocket.OPEN) {
          state.ws.send(JSON.stringify({ type: "latency_update", latency_ms: rtt }));
        }
        break;
      }

      case "room_closed":
        state.isRunning = false;
        updateButtons(currentClientCanEdit());
        appendConsole(`[room] ${msg.message}
`);
        toast(msg.message || "Комната закрыта.");
        state.manualClose = true;
        stopPingLoop();
        stopAutosaveLoop();
        setConnectionBadge("Комната закрыта", "error");
        state.ws?.close();
        setReadOnly(true);
        break;

      default:
        break;
    }
  }

  function connect() {
    clearTimeout(state.reconnectTimer);
    setConnectionBadge("Подключение…", "idle");

    const protocol = window.location.protocol === "https:" ? "wss://" : "ws://";
    state.ws = new WebSocket(`${protocol}${window.location.host}/ws`);

    state.ws.onopen = () => {
      setConnectionBadge("Авторизация…", "idle");
      showAuthError("");
      state.ws.send(JSON.stringify({
        type: "hello",
        name: state.name,
        role: state.role,
        username: state.username,
        password: state.password,
        room: state.room,
        room_action: state.roomAction,
      }));
      startPingLoop();
      startAutosaveLoop();
    };

    state.ws.onmessage = (event) => {
      const msg = JSON.parse(event.data);
      handleMessage(msg);
    };

    state.ws.onclose = () => {
      stopPingLoop();
      stopAutosaveLoop();
      state.isRunning = false;
      updateButtons(currentClientCanEdit());
      if (state.manualClose) return;
      setConnectionBadge("Переподключение…", "idle");
      if (!state.hasConnectedOnce) {
        showWorkspace(false);
        showEntryScreen(true);
        showAuthError("Не удалось установить соединение. Проверьте введённые данные и доступность сервера.");
      }
      state.reconnectTimer = window.setTimeout(connect, 2000);
    };

    state.ws.onerror = () => setConnectionBadge("Ошибка соединения", "error");
  }

  function bindFallbackEvents() {
    editorTextarea.addEventListener("input", () => {
      updateGutter();
      updateCursorStatus();

      if (state.applyingRemote) return;
      if (!state.ws || state.ws.readyState !== WebSocket.OPEN) return;

      const newText = editorTextarea.value;
      const patch = computePatch(state.docText, newText);

      state.ws.send(JSON.stringify({
        type: "patch",
        baseVersion: state.docVersion,
        start: patch.start,
        end: patch.end,
        text: patch.text,
      }));

      state.docText = newText;
      state.docVersion += 1;
      docVersionSpan.textContent = `v${state.docVersion}`;
    });

    ["keyup", "click", "select"].forEach((eventName) => {
      editorTextarea.addEventListener(eventName, updateCursorStatus);
    });

    editorTextarea.addEventListener("keydown", (event) => {
      if (event.key === "Tab") {
        event.preventDefault();
        const start = editorTextarea.selectionStart;
        const end = editorTextarea.selectionEnd;
        const value = editorTextarea.value;
        editorTextarea.value = `${value.slice(0, start)}    ${value.slice(end)}`;
        editorTextarea.selectionStart = editorTextarea.selectionEnd = start + 4;
        editorTextarea.dispatchEvent(new Event("input"));
      }
    });
  }

  function openReport(url) {
    window.open(url, "_blank", "noopener");
  }

  function bindEntryEvents() {
    roleCards.forEach((button) => {
      button.addEventListener("click", () => setRole(button.dataset.role));
    });

    hostModeBtns.forEach((button) => {
      button.addEventListener("click", () => {
        state.roomAction = button.dataset.mode === "join" ? "join" : "create";
        syncRoleVisibility();
      });
    });

    hostStartBtn?.addEventListener("click", () => {
      setRole("host");
      startSession();
    });

    studentStartBtn?.addEventListener("click", () => {
      setRole("student");
      startSession();
    });

    [hostPasswordInput, hostRoomInput, studentRoomInput].forEach((input) => {
      input?.addEventListener("keydown", (event) => {
        if (event.key === "Enter") {
          event.preventDefault();
          if (state.role === "host") {
            hostStartBtn?.click();
          } else {
            studentStartBtn?.click();
          }
        }
      });
    });
  }

  function bindCommonEvents() {
    window.addEventListener("resize", () => {
      updateGutter();
      renderRemoteCursors();
    });

    grantBtn.addEventListener("click", () => {
      if (!participantSelect.value) return;
      state.ws?.send(JSON.stringify({ type: "grant_edit", target_id: participantSelect.value }));
    });

    revokeBtn.addEventListener("click", () => {
      if (!participantSelect.value) return;
      state.ws?.send(JSON.stringify({ type: "revoke_edit", target_id: participantSelect.value }));
    });

    setRegionBtn.addEventListener("click", () => {
      if (!participantSelect.value) return;
      state.ws?.send(JSON.stringify({
        type: "set_region",
        target_id: participantSelect.value,
        start_line: Number(regionStartEl.value || 1),
        end_line: Number(regionEndEl.value || 1),
      }));
    });

    clearRegionBtn.addEventListener("click", () => {
      if (!participantSelect.value) return;
      state.ws?.send(JSON.stringify({ type: "clear_region", target_id: participantSelect.value }));
    });

    chatSend.addEventListener("click", sendChat);
    chatInput.addEventListener("keydown", (event) => {
      if (event.key === "Enter") sendChat();
    });

    runBtn.addEventListener("click", () => {
      state.ws?.send(JSON.stringify({ type: "run_code", code: getEditorText(), timeout: 5 }));
    });

    checkBtn.addEventListener("click", () => {
      state.ws?.send(JSON.stringify({ type: "check_syntax", code: getEditorText() }));
    });

    saveBtn.addEventListener("click", () => {
      const filename = prompt("Имя файла (.py):", state.currentFilename || "main.py");
      if (!filename) return;
      state.ws?.send(JSON.stringify({ type: "save_py", filename, code: getEditorText() }));
    });

    stopBtn.addEventListener("click", () => {
      if (!state.isRunning) return;
      state.ws?.send(JSON.stringify({ type: "stop_code" }));
    });

    createFileBtn.addEventListener("click", () => {
      const filename = prompt("Имя нового файла:", "new_file.py");
      if (!filename) return;
      state.ws?.send(JSON.stringify({ type: "create_file", filename }));
    });

    importFileBtn.addEventListener("click", () => {
      if (!importFileInput) return;
      importFileInput.value = "";
      importFileInput.click();
    });

    importFileInput?.addEventListener("change", async (event) => {
      const file = event.target?.files?.[0];
      if (!file) return;
      try {
        const content = await file.text();
        state.ws?.send(JSON.stringify({
          type: "import_file_content",
          filename: file.name || state.currentFilename || "main.py",
          content,
        }));
      } catch (error) {
        toast(`Не удалось прочитать файл: ${error?.message || error}`);
      } finally {
        importFileInput.value = "";
      }
    });

    downloadFileBtn?.addEventListener("click", () => {
      if (!state.room) return;
      const url = `/api/rooms/${encodeURIComponent(state.room)}/download?filename=${encodeURIComponent(state.currentFilename || "main.py")}`;
      window.open(url, "_blank", "noopener");
    });

    fileSelect.addEventListener("change", () => {
      if (!fileSelect.value) return;
      state.ws?.send(JSON.stringify({ type: "switch_file", filename: fileSelect.value }));
    });

    blameReportBtn.addEventListener("click", () => {
      openReport(`/api/rooms/${encodeURIComponent(state.room)}/reports/blame?filename=${encodeURIComponent(state.currentFilename)}&format=html`);
    });

    scoreReportBtn.addEventListener("click", () => {
      openReport(`/api/rooms/${encodeURIComponent(state.room)}/reports/access?format=html`);
    });
  }

  async function init() {
    initTheme();
    fillAuthFormFromQueryOrStorage();
    updateHeaderInfo();
    syncRoleVisibility();
    showWorkspace(false);
    showEntryScreen(true);
    setReadOnly(true);

    bindFallbackEvents();
    bindEntryEvents();
    bindCommonEvents();
    initKonamiCode();
    await initMonacoEditor();
  }

  init();
})();
