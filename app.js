(() => {
  "use strict";

  const ignoredDirectories = new Set([
    ".agents", ".cache", ".codex", ".eggs", ".git", ".ipynb_checkpoints",
    ".mypy_cache", ".next", ".nox", ".parcel-cache", ".pytest_cache",
    ".ruff_cache", ".tmp", ".tox", ".turbo", ".venv", "__pycache__", "build",
    "coverage", "dist", "htmlcov", "logs", "node_modules", "target", "temp",
    "tmp", "var",
  ]);
  const privateNames = new Set([
    ".env", ".netrc", ".npmrc", ".pypirc", "credentials.json", "id_dsa",
    "id_ecdsa", "id_rsa", "id_ed25519", "secret.json", "secrets.json",
    "service-account.json", "service_account.json",
  ]);
  const textExtensions = new Set([
    ".bash", ".bat", ".cfg", ".cjs", ".conf", ".css", ".csv", ".dockerignore",
    ".gitattributes", ".gitignore", ".gql", ".graphql", ".htm", ".html", ".ini",
    ".js", ".json", ".jsonl", ".jsx", ".less", ".lock", ".markdown", ".md",
    ".mjs", ".pine", ".ps1", ".py", ".pyi", ".rst", ".scss", ".sh", ".sql",
    ".toml", ".ts", ".tsv", ".tsx", ".txt", ".xml", ".yaml", ".yml", ".zsh",
  ]);
  const textNames = new Set(["dockerfile", "license", "makefile", "procfile", "readme"]);
  const documentExtensions = new Set([
    ".docx", ".epub", ".ipynb", ".pdf", ".pptx", ".xlsx", ".xlsm",
  ]);
  const imageExtensions = new Set([
    ".apng", ".arw", ".avif", ".bmp", ".cr2", ".cr3", ".cur", ".dds",
    ".dib", ".dng", ".emf", ".exr", ".gif", ".hdr", ".heic", ".heif",
    ".ico", ".j2c", ".j2k", ".jfif", ".jp2", ".jpc", ".jpeg", ".jpf",
    ".jpg", ".jpm", ".jxl", ".mj2", ".nef", ".orf", ".pbm", ".pcx",
    ".pef", ".pgm", ".png", ".pnm", ".ppm", ".psd", ".qoi", ".raw",
    ".rw2", ".sgi", ".srw", ".svg", ".svgz", ".tga", ".tif", ".tiff",
    ".webp", ".wmf", ".xcf",
  ]);
  const commonImageExtensions = new Set([
    ".apng", ".avif", ".bmp", ".gif", ".ico", ".jfif", ".jpeg", ".jpg",
    ".png", ".svg", ".svgz", ".webp",
  ]);
  const idlePetDelay = 1600;
  const excludedPathLimit = 2000;
  const exclusionLabels = {
    ignored_directories: "Generated or ignored directories",
    protected_files: "Protected files",
    unsupported_files: "Unsupported files",
    unreadable_files: "Unreadable files",
    unreadable_directories: "Unreadable folders (contents not fully scanned)",
    oversized_files: "Files over the per-file limit",
    file_limit_files: "Files beyond the document-count limit",
    upload_budget_files: "Files beyond the workspace memory budget",
  };
  const windowsReserved = new Set([
    "CON", "PRN", "AUX", "NUL",
    ...Array.from({ length: 9 }, (_, index) => `COM${index + 1}`),
    ...Array.from({ length: 9 }, (_, index) => `LPT${index + 1}`),
  ]);
  const state = {
    busy: false,
    policy: null,
    directory: null,
    snapshot: null,
    polling: false,
    stopped: false,
    canWrite: false,
    pet: null,
    excludedPaths: null,
    useFolderUpload: false,
  };

  window.addEventListener("DOMContentLoaded", boot);

  async function boot() {
    const button = document.getElementById("chooseFolder");
    state.pet = setupIdlePet();
    button.disabled = true;
    state.useFolderUpload = !window.showDirectoryPicker;
    if (state.useFolderUpload) button.textContent = "Upload Folder";
    button.addEventListener("click", chooseFolder);
    document.getElementById("folderUpload").addEventListener("change", uploadSelectedFolder);
    document.getElementById("copyView").addEventListener("click", copyViewUrl);
    document.getElementById("enableChanges").addEventListener("click", enableLocalChanges);
    document.getElementById("copyAccess").addEventListener("click", copyActionAccess);
    document.getElementById("applyChanges").addEventListener("click", applyPastedChanges);
    document.getElementById("excludedDetails").addEventListener("toggle", (event) => {
      if (event.currentTarget.open) populateExcludedPaths();
    });
    window.addEventListener("beforeunload", disconnectBridge);
    try {
      state.policy = await fetchJson("/AgentArm/api/policy");
      button.disabled = false;
      if (new URLSearchParams(location.search).get("restore") === "1") {
        const saved = await loadHandle().catch(() => null);
        if (saved) {
          if (await saved.queryPermission({ mode: "read" }) === "granted") {
            await publish(saved);
          } else {
            setStatus("Choose Folder to restore access to the saved project.");
          }
        }
      }
    } catch (error) {
      setStatus(error.message, true);
    }
  }

  async function chooseFolder() {
    if (state.busy) return;
    if (state.useFolderUpload) return chooseUpload();
    state.pet?.suspend();
    let phase = "Folder selection";
    try {
      const directory = await window.showDirectoryPicker({ mode: "read" });
      phase = "Folder import";
      await publish(directory);
      // Remembering a folder is optional and must never prevent an import.
      await saveHandle(directory).catch(() => {});
    } catch (error) {
      if (error.name !== "AbortError") {
        state.useFolderUpload = true;
        document.getElementById("chooseFolder").textContent = "Upload Folder";
        setStatus(`${phase} failed: ${error.message} Press “Upload Folder” to read it as a read-only snapshot instead.`, true);
      }
    } finally {
      if (!state.busy) state.pet?.resume();
    }
  }

  function chooseUpload() {
    if (state.busy || !state.policy) return;
    const input = document.getElementById("folderUpload");
    input.value = "";
    input.click();
  }

  async function uploadSelectedFolder(event) {
    if (state.busy || !event.target.files.length) return;
    try {
      state.useFolderUpload = true;
      await publish(folderFromFiles(event.target.files));
    } catch (error) {
      setStatus(`Folder upload failed: ${error.message}`, true);
    } finally {
      event.target.value = "";
    }
  }

  // Reuse browser File objects without copying their contents. Both selection
  // methods use the same Git reader, filtering, ordering and upload budgets.
  function folderFromFiles(files) {
    function directory(name) {
      const entries = new Map();
      return {
        name, kind: "directory", uploadOnly: true, entries,
        async *values() { yield* entries.values(); },
        async getDirectoryHandle(name) {
          const entry = entries.get(name);
          if (entry?.kind !== "directory") throw new DOMException("Folder not included in selection.", "NotFoundError");
          return entry;
        },
        async getFileHandle(name) {
          const entry = entries.get(name);
          if (entry?.kind !== "file") throw new DOMException("File not included in selection.", "NotFoundError");
          return entry;
        },
        async queryPermission({ mode }) { return mode === "read" ? "granted" : "denied"; },
      };
    }
    let root;
    for (const file of files) {
      const parts = file.webkitRelativePath.split("/");
      if (parts.length < 2 || parts.some(part => !part || part === "." || part === ".." || part.includes("\\"))) {
        throw new Error("Choose a folder, not individual files.");
      }
      root ||= directory(parts[0]);
      if (root.name !== parts[0]) throw new Error("Choose one folder at a time.");
      let parent = root;
      for (const part of parts.slice(1, -1)) {
        if (!parent.entries.has(part)) parent.entries.set(part, directory(part));
        parent = parent.entries.get(part);
      }
      parent.entries.set(parts.at(-1), { name: parts.at(-1), kind: "file", async getFile() { return file; } });
    }
    if (!root) throw new Error("The selected folder contains no files.");
    return root;
  }

  async function publish(directory) {
    state.busy = true;
    state.pet?.suspend();
    document.getElementById("chooseFolder").disabled = true;
    renderExcludedPaths(null);
    document.getElementById("loadProgress").hidden = false;
    document.getElementById("folderStage").setAttribute("aria-busy", "true");
    setStatus("Reading the project in deterministic path order…");
    try {
      const prepared = await prepareSnapshot(directory);
      setStatus(
        `Loading ${prepared.count.toLocaleString("en")} documents into temporary memory…`
      );
      const result = await uploadSnapshot(prepared.body, prepared.bytes);
      state.directory = directory;
      state.snapshot = result;
      state.stopped = false;
      state.canWrite =
        await directory.queryPermission({ mode: "readwrite" }) === "granted";
      renderConnected();
      setStatus(importSummary(prepared, result));
      renderExcludedPaths(prepared);
      pollLoop();
    } finally {
      state.busy = false;
      document.getElementById("loadProgress").hidden = true;
      document.getElementById("folderStage").setAttribute("aria-busy", "false");
      document.getElementById("chooseFolder").disabled = false;
      state.pet?.resume();
    }
  }

  function setupIdlePet() {
    const stage = document.getElementById("folderStage");
    const motion = window.matchMedia("(prefers-reduced-motion: reduce)");
    if (!stage) return null;

    const listeners = new AbortController();
    let lastActivity = performance.now();
    let timer = null;
    let visible = false;
    let suspended = false;
    let reducedMotion = motion.matches;

    function hide() {
      if (!visible) return;
      visible = false;
      stage.classList.remove("pet-visible");
    }

    function clearTimer() {
      if (timer === null) return;
      window.clearTimeout(timer);
      timer = null;
    }

    function schedule() {
      if (timer !== null || suspended || reducedMotion || document.hidden) return;
      const elapsed = performance.now() - lastActivity;
      timer = window.setTimeout(checkIdle, Math.max(0, idlePetDelay - elapsed));
    }

    function checkIdle() {
      timer = null;
      if (suspended || reducedMotion || document.hidden) return;
      const elapsed = performance.now() - lastActivity;
      if (elapsed < idlePetDelay) {
        schedule();
        return;
      }
      visible = true;
      stage.classList.add("pet-visible");
    }

    function recordActivity() {
      lastActivity = performance.now();
      hide();
      schedule();
    }

    function handleVisibility() {
      if (document.hidden) {
        clearTimer();
        hide();
      } else {
        recordActivity();
      }
    }

    function handleMotionPreference(event) {
      reducedMotion = event.matches;
      clearTimer();
      hide();
      if (!reducedMotion) recordActivity();
    }

    window.addEventListener("pointermove", recordActivity, {
      passive: true,
      signal: listeners.signal,
    });
    window.addEventListener("pointerdown", recordActivity, {
      passive: true,
      signal: listeners.signal,
    });
    window.addEventListener("keydown", recordActivity, {
      signal: listeners.signal,
    });
    document.addEventListener("visibilitychange", handleVisibility, {
      signal: listeners.signal,
    });
    motion.addEventListener("change", handleMotionPreference, {
      signal: listeners.signal,
    });
    schedule();

    return {
      suspend() {
        suspended = true;
        clearTimer();
        hide();
      },
      resume() {
        if (!suspended) return;
        suspended = false;
        recordActivity();
      },
      destroy() {
        clearTimer();
        hide();
        listeners.abort();
      },
    };
  }

  async function prepareSnapshot(directory) {
    setStatus("Reading Git history…");
    let git;
    try {
      git = await readGit(directory);
    } catch (error) {
      git = { detected: true, commit_graph_complete: false,
        commit_graph_error: `Git could not be fully read: ${error.message}` };
    }
    setStatus("Checking files and available upload space…");
    const collection = await collectProject(directory);
    if (!collection.files.length) {
      throw new Error("No readable project documents fit the published workspace policy.");
    }
    const metadata = {
      project_name: directory.name,
      directories: collection.directories,
      excluded: collection.excluded,
      git,
    };
    const body = new FormData();
    body.append("metadata", JSON.stringify(metadata));
    for (const item of collection.files) {
      body.append("paths", item.path);
      body.append("files", item.file, item.file.name);
    }
    return {
      body,
      count: collection.files.length,
      bytes: collection.bytes,
      excluded: collection.excluded,
      excludedPaths: collection.excludedPaths,
    };
  }

  function uploadSnapshot(body, bytes) {
    return new Promise((resolve, reject) => {
      const request = new XMLHttpRequest();
      request.open("POST", "/AgentArm/api/snapshots", true);
      request.withCredentials = true;
      request.upload.addEventListener("progress", (event) => {
        if (!event.lengthComputable) return;
        const sent = Math.min(event.loaded, bytes || event.total);
        const percent = event.total ? Math.round((event.loaded / event.total) * 100) : 0;
        setStatus(`Uploading ${formatBytes(sent)} / ${formatBytes(bytes || event.total)} (${percent}%)…`);
      });
      request.addEventListener("load", () => {
        let result = {};
        try { result = JSON.parse(request.responseText || "{}"); } catch { /* server error below */ }
        if (request.status < 200 || request.status >= 300) {
          reject(new Error(result.detail || `Connection failed (${request.status}).`));
          return;
        }
        resolve(result);
      });
      request.addEventListener("error", () => reject(new Error("Upload connection failed.")));
      request.addEventListener("abort", () => reject(new Error("Upload was cancelled.")));
      request.send(body);
    });
  }

  function importSummary(prepared, result) {
    const excluded = prepared.excluded;
    const local = [];
    if (excluded.unreadable_files) {
      local.push(`${excluded.unreadable_files.toLocaleString("en")} unreadable`);
    }
    if (excluded.unreadable_directories) {
      local.push(`${excluded.unreadable_directories.toLocaleString("en")} folders not fully scanned`);
    }
    if (excluded.oversized_files) {
      local.push(`${excluded.oversized_files.toLocaleString("en")} oversized`);
    }
    if (excluded.file_limit_files) {
      local.push(`${excluded.file_limit_files.toLocaleString("en")} beyond the file limit`);
    }
    if (excluded.upload_budget_files) {
      local.push(
        `${excluded.upload_budget_files.toLocaleString("en")} beyond the memory budget`
      );
    }
    const intentionallyExcluded = excluded.ignored_directories +
      excluded.protected_files + excluded.unsupported_files;
    if (intentionallyExcluded) {
      local.push(
        `${intentionallyExcluded.toLocaleString("en")} unsupported, protected, or generated entries`
      );
    }
    const errors = result?.extraction_errors || 0;
    const errorNote = errors ? ` ${errors.toLocaleString("en")} document(s) could not be decoded; see their extraction errors in the AI View.` : "";
    const imported = `Imported ${prepared.count.toLocaleString("en")} readable files ` +
      `(${formatBytes(prepared.bytes)})`;
    return (local.length ? `${imported}; left local: ${local.join(", ")}.` : `${imported}.`) + errorNote;
  }

  function renderExcludedPaths(prepared) {
    const details = document.getElementById("excludedDetails");
    const groups = document.getElementById("excludedGroups");
    details.open = false;
    groups.replaceChildren();
    state.excludedPaths = prepared?.excludedPaths || null;
    const counts = prepared?.excluded || null;
    const total = counts
      ? Object.values(counts).reduce((sum, count) => sum + Number(count || 0), 0)
      : 0;
    details.hidden = total === 0;
    document.getElementById("excludedSummary").textContent = total === 1
      ? "Review 1 local-only excluded path"
      : `Review ${total.toLocaleString("en")} local-only excluded paths`;
  }

  function populateExcludedPaths() {
    const groups = document.getElementById("excludedGroups");
    if (groups.childElementCount || !state.excludedPaths) return;
    const { counts, paths, omitted } = state.excludedPaths;
    for (const key of Object.keys(exclusionLabels)) {
      const count = Number(counts[key] || 0);
      if (!count) continue;
      const section = document.createElement("section");
      const heading = document.createElement("h2");
      heading.textContent = `${exclusionLabels[key]} (${count.toLocaleString("en")})`;
      section.append(heading);
      const entries = paths[key] || [];
      if (entries.length) {
        const list = document.createElement("ul");
        for (const path of entries) {
          const item = document.createElement("li");
          const code = document.createElement("code");
          code.textContent = path;
          item.append(code);
          list.append(item);
        }
        section.append(list);
      }
      const hiddenCount = Number(omitted[key] || 0);
      if (hiddenCount) {
        const note = document.createElement("p");
        note.textContent = `${hiddenCount.toLocaleString("en")} additional path(s) ` +
          `not rendered after the ${excludedPathLimit.toLocaleString("en")} path UI limit.`;
        section.append(note);
      }
      groups.append(section);
    }
  }

  function renderConnected() {
    const workspace = document.getElementById("workspace");
    const link = document.getElementById("workspaceLink");
    workspace.hidden = false;
    document.getElementById("editPanel").hidden = Boolean(state.directory.uploadOnly);
    document.getElementById("uploadOnlyNote").hidden = !state.directory.uploadOnly;
    document.getElementById("workspaceName").textContent = state.directory.name;
    document.getElementById("connectionState").textContent = "Live";
    link.href = state.snapshot.view_url;
    document.getElementById("chooseFolder").textContent = "Choose Another Folder";
    renderWriteState();
  }

  function renderWriteState() {
    const button = document.getElementById("enableChanges");
    const enabled = Boolean(state.canWrite);
    button.textContent = enabled ? "Local Changes Enabled" : "Enable Local Changes";
    button.disabled = enabled;
    button.setAttribute("aria-pressed", enabled ? "true" : "false");
  }

  async function copyViewUrl() {
    if (!state.snapshot) return;
    const button = document.getElementById("copyView");
    try {
      await copyText(new URL(state.snapshot.view_url, location.origin).href);
      setChangeStatus("AI view URL copied.");
      flashButton(button, "Copied");
    } catch (error) {
      setChangeStatus(error.message, true);
      flashButton(button, null, true);
    }
  }

  async function copyActionAccess() {
    if (!state.snapshot?.write_token) return;
    try {
      await ensureWritePermission(true);
      const absoluteUrl = (path) => new URL(path, location.origin).href;
      const access = {
        format: "agentarm.action-access.v2",
        read_method: "GET",
        context_url: absoluteUrl(state.snapshot.context_url),
        view_url: absoluteUrl(state.snapshot.view_url),
        git_url: absoluteUrl(state.snapshot.git_url),
        git_commit_links_url: absoluteUrl(state.snapshot.git_commit_links_url),
        project_index_url: absoluteUrl(state.snapshot.project_url),
        document_links_url: absoluteUrl(state.snapshot.document_links_url),
        project_full_url: absoluteUrl(state.snapshot.project_full_url),
        project_document_url_template: absoluteUrl(
          `/AgentArm/share/${state.snapshot.share_id}/project/documents/{sequence}.json`
        ),
        endpoint: absoluteUrl(state.snapshot.changes_url),
        authorization: `Bearer ${state.snapshot.write_token}`,
        method: "POST",
        content_type: "application/json",
        change_format: "agentarm.changes.v1",
        instructions: [
          "START: Fetch context_url with your native web or URL-fetch tool.",
          "Do not switch to Bash, terminal, Python, curl, requests, sockets, or DNS tests when native web fetch is available.",
          "READ: Fetch git_url and project_index_url with HTTP GET.",
          "Open git_commit_links_url and fetch every commit page before claiming complete Git history coverage.",
          "Open document_links_url and follow its real HTML anchors in sequence.",
          "For every document with chunked=true, open chunk_links_url and fetch every chunk in order.",
          "Do not construct document URLs when your web tool requires links to appear on a fetched page.",
          "Fetch project_full_url only if your client accepts the complete response size.",
          "WRITE: Send a complete agentarm.changes.v1 body to endpoint with HTTP POST.",
          "Do not GET endpoint to read files; its GET response only provides workspace links.",
        ],
        note: "Temporary write capability. The local FolderLens page must remain open.",
      };
      await copyText(JSON.stringify(access, null, 2));
      setChangeStatus("Temporary action access copied.");
      flashButton(document.getElementById("copyAccess"), "Copied");
    } catch (error) {
      setChangeStatus(error.message, true);
      flashButton(document.getElementById("copyAccess"), null, true);
    }
  }

  async function copyText(value) {
    if (!navigator.clipboard) throw new Error("Clipboard access is unavailable.");
    await navigator.clipboard.writeText(value);
  }

  async function pollLoop() {
    if (state.polling) return;
    state.polling = true;
    try {
      while (!state.stopped && state.snapshot) {
        const active = state.snapshot;
        try {
          const response = await postJson(
            `/AgentArm/api/snapshots/${active.share_id}/bridge/poll`,
            { bridge_token: active.bridge_token }
          );
          if (active !== state.snapshot) continue;
          document.getElementById("connectionState").textContent = "Live";
          if (response.command) await answerCommand(active, response.command);
        } catch (error) {
          if (active !== state.snapshot) continue;
          if ([403, 404].includes(error.status)) {
            state.stopped = true;
            document.getElementById("connectionState").textContent = "Expired";
            setChangeStatus("This share has expired. Choose the folder again to reconnect.", true);
            break;
          }
          document.getElementById("connectionState").textContent = "Reconnecting";
          setChangeStatus(error.message, true);
          await delay(1500);
        }
      }
    } finally {
      state.polling = false;
    }
  }

  async function answerCommand(snapshot, command) {
    let result;
    let error;
    try {
      if (command.operation !== "apply_changes") {
        throw new Error(`Unsupported local operation: ${command.operation}`);
      }
      result = await applyLocalChanges(command.payload);
    } catch (caught) {
      error = caught.message || String(caught);
    }
    await postJson(
      `/AgentArm/api/snapshots/${snapshot.share_id}/bridge/respond`,
      {
        bridge_token: snapshot.bridge_token,
        request_id: command.id,
        result,
        error,
      }
    );
  }

  async function applyPastedChanges() {
    if (!state.directory || state.busy) return;
    const button = document.getElementById("applyChanges");
    button.disabled = true;
    try {
      await ensureWritePermission(true);
      const payload = JSON.parse(document.getElementById("changes").value);
      const result = await applyLocalChanges(payload);
      setChangeStatus(
        result.warning || `${result.operation_count.toLocaleString("en")} change(s) applied locally.`
      );
      document.getElementById("changes").value = "";
    } catch (error) {
      setChangeStatus(error.message, true);
    } finally {
      button.disabled = false;
    }
  }

  async function applyLocalChanges(payload) {
    if (state.busy) throw new Error("Another folder operation is running. Try again when it finishes.");
    state.busy = true;
    const choose = document.getElementById("chooseFolder");
    choose.disabled = true;
    try {
      return await applyLocalChangesExclusive(payload);
    } finally {
      state.busy = false;
      choose.disabled = false;
    }
  }

  async function applyLocalChangesExclusive(payload) {
    await ensureWritePermission(false);
    const operations = validateLocalChanges(payload);
    const originals = [];
    let backupBytes = 0;
    for (const operation of operations) {
      const file = await getLocalFile(operation.path, true);
      backupBytes += file?.size || 0;
      if (backupBytes > 16 * 1024 * 1024) {
        throw new Error("Original files exceed the 16 MiB rollback budget. Apply a smaller change package.");
      }
      const content = file ? await file.arrayBuffer() : null;
      const currentHash = content ? await hashBuffer(content) : null;
      if (currentHash !== operation.base_sha256) {
        throw new Error(`File changed before apply: ${operation.path}`);
      }
      if (operation.op === "delete" && !file) {
        throw new Error(`Cannot delete a missing file: ${operation.path}`);
      }
      originals.push({
        path: operation.path,
        existed: Boolean(file),
        content,
      });
    }
    const attempted = [];
    try {
      for (const [index, operation] of operations.entries()) {
        const current = await getLocalFile(operation.path, true);
        const currentHash = current ? await hashBuffer(await current.arrayBuffer()) : null;
        if (currentHash !== operation.base_sha256) {
          throw new Error(`File changed during apply: ${operation.path}`);
        }
        attempted.push(originals[index]);
        if (operation.op === "delete") {
          await removeLocalFile(operation.path);
        } else {
          await writeLocalFile(operation.path, operation.content);
        }
      }
    } catch (error) {
      const failures = [];
      for (const original of attempted.reverse()) {
        try {
          if (original.existed) {
            await writeLocalFile(original.path, new Uint8Array(original.content));
          } else {
            await removeLocalFile(original.path, true);
          }
        } catch {
          failures.push(original.path);
        }
      }
      if (failures.length) {
        throw new Error(`${error.message} Rollback incomplete; check these files: ${failures.join(", ")}`);
      }
      throw error;
    }
    let warning = null;
    setChangeStatus("Local files saved. Refreshing the shared view…");
    try {
      await refreshSnapshot();
    } catch {
      warning = "Files were saved locally, but the AI View could not be refreshed. Choose the folder again before sharing it. Do not apply the same changes again.";
    }
    return {
      state: "applied",
      view_refreshed: !warning,
      warning,
      title: payload.title,
      operation_count: operations.length,
      paths: operations.map((item) => item.path),
    };
  }

  function validateLocalChanges(payload) {
    if (!payload || payload.format !== "agentarm.changes.v1") {
      throw new Error("Change format must be agentarm.changes.v1.");
    }
    if (typeof payload.title !== "string" || !payload.title.trim() ||
        payload.title.length > 200) {
      throw new Error("A 1–200 character change title is required.");
    }
    if (!Array.isArray(payload.operations) ||
        payload.operations.length < 1 || payload.operations.length > 100) {
      throw new Error("Changes must contain 1–100 operations.");
    }
    const seen = new Set();
    let totalBytes = 0;
    return payload.operations.map((item) => {
      const path = safeLocalPath(item.path);
      if (seen.has(path)) throw new Error(`Duplicate change path: ${path}`);
      seen.add(path);
      if (!["write", "delete"].includes(item.op)) {
        throw new Error(`Unsupported change operation: ${item.op}`);
      }
      if (item.base_sha256 !== null &&
          !/^[0-9a-f]{64}$/i.test(item.base_sha256 || "")) {
        throw new Error(`Invalid base hash: ${path}`);
      }
      if (item.op === "write" && typeof item.content !== "string") {
        throw new Error(`Write content must be text: ${path}`);
      }
      if (item.op === "write") {
        const contentBytes = new TextEncoder().encode(item.content).length;
        if (contentBytes > state.policy.max_file_bytes) {
          throw new Error(`Write exceeds the per-file limit: ${path}`);
        }
        totalBytes += contentBytes;
        if (totalBytes > 16 * 1024 * 1024) {
          throw new Error("Change content exceeds the 16 MiB limit.");
        }
      }
      return {
        op: item.op,
        path,
        base_sha256: item.base_sha256?.toLowerCase() || null,
        ...(item.op === "write" ? { content: item.content } : {}),
      };
    });
  }

  async function refreshSnapshot() {
    const prepared = await prepareSnapshot(state.directory);
    const response = await fetch(
      `/AgentArm/api/snapshots/${state.snapshot.share_id}/refresh`,
      {
        method: "POST",
        credentials: "same-origin",
        headers: { "X-AgentArm-Bridge": state.snapshot.bridge_token },
        body: prepared.body,
      }
    );
    const result = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(result.detail || `Workspace refresh failed (${response.status}).`);
    }
    state.snapshot = { ...state.snapshot, ...result };
    setStatus(importSummary(prepared, result));
  }

  async function enableLocalChanges() {
    const button = document.getElementById("enableChanges");
    try {
      await ensureWritePermission(true);
      setChangeStatus("Local changes are enabled for this folder.");
      flashButton(button, null);
    } catch (error) {
      setChangeStatus(error.message, true);
      flashButton(button, null, true);
    }
  }

  async function ensureWritePermission(interactive) {
    if (!state.directory) throw new Error("Choose a folder first.");
    if (state.directory.uploadOnly) throw new Error("This upload is read-only. Choose a folder with browser access to enable local edits.");
    let permission = await state.directory.queryPermission({ mode: "readwrite" });
    if (permission !== "granted" && interactive) {
      permission = await state.directory.requestPermission({ mode: "readwrite" });
    }
    state.canWrite = permission === "granted";
    renderWriteState();
    if (!state.canWrite) {
      throw new Error("Local change permission is required on the folder owner's device.");
    }
  }

  async function getLocalFile(path, allowMissing = false) {
    const parts = safeLocalPath(path).split("/");
    try {
      const directory = await directoryAt(parts.slice(0, -1), false);
      const handle = await directory.getFileHandle(parts.at(-1));
      return await handle.getFile();
    } catch (error) {
      if (allowMissing && error.name === "NotFoundError") return null;
      throw error;
    }
  }

  async function writeLocalFile(path, content) {
    const parts = safeLocalPath(path).split("/");
    const directory = await directoryAt(parts.slice(0, -1), true);
    const handle = await directory.getFileHandle(parts.at(-1), { create: true });
    const writer = await handle.createWritable();
    try {
      await writer.write(content);
      await writer.close();
    } catch (error) {
      await writer.abort();
      throw error;
    }
  }

  async function removeLocalFile(path, ignoreMissing = false) {
    const parts = safeLocalPath(path).split("/");
    try {
      const directory = await directoryAt(parts.slice(0, -1), false);
      await directory.removeEntry(parts.at(-1));
    } catch (error) {
      if (!ignoreMissing || error.name !== "NotFoundError") throw error;
    }
  }

  async function directoryAt(parts, create) {
    let current = state.directory;
    for (const part of parts) {
      current = await current.getDirectoryHandle(part, { create });
    }
    return current;
  }

  function safeLocalPath(value) {
    const path = String(value || "");
    const parts = path.split("/");
    if (!path || path.startsWith("/") || path.includes("\\") ||
        path.includes(":") || parts.length > 48 ||
        new TextEncoder().encode(path).length > 1024 ||
        parts.some((part) => {
          const stem = part.replace(/[ .]+$/u, "").split(".", 1)[0].toUpperCase();
          return !part || part === "." || part === ".." ||
            part !== part.replace(/[ .]+$/u, "") || part.includes("~") ||
            windowsReserved.has(stem) || /[\u0000-\u001f]/u.test(part);
        })) {
      throw new Error(`Unsafe local path: ${path}`);
    }
    if (isSensitive(path) || isIgnoredPath(path) || !isTextSupported(path)) {
      throw new Error(`Protected or unsupported change path: ${path}`);
    }
    return path;
  }

  function isTextSupported(path) {
    const name = path.split("/").at(-1).toLocaleLowerCase("en");
    const dot = name.lastIndexOf(".");
    const extension = dot >= 0 ? name.slice(dot) : "";
    return textExtensions.has(extension) || textNames.has(name) ||
      [".env.example", ".env.sample"].includes(name);
  }

  async function hashBuffer(value) {
    const digest = await crypto.subtle.digest("SHA-256", value);
    return [...new Uint8Array(digest)]
      .map((byte) => byte.toString(16).padStart(2, "0")).join("");
  }

  async function postJson(url, body) {
    const response = await fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const result = await response.json().catch(() => ({}));
    if (!response.ok) {
      const error = new Error(result.detail || `FolderLens request failed (${response.status}).`);
      error.status = response.status;
      throw error;
    }
    return result;
  }

  function setChangeStatus(message, error = false) {
    const target = document.getElementById("changeStatus");
    target.textContent = message;
    target.classList.toggle("error", error);
    target.classList.remove("just-changed");
    void target.offsetWidth;
    target.classList.add("just-changed");
  }

  const FLASH_TIMERS = new WeakMap();
  function flashButton(button, temporaryLabel, error = false) {
    if (!button) return;
    const original = button.dataset.label || button.textContent;
    button.dataset.label = original;
    button.classList.toggle("flash-error", error);
    button.classList.add("flash");
    if (temporaryLabel) button.textContent = temporaryLabel;
    const previous = FLASH_TIMERS.get(button);
    if (previous) window.clearTimeout(previous);
    const timer = window.setTimeout(() => {
      button.classList.remove("flash", "flash-error");
      if (temporaryLabel) button.textContent = original;
      FLASH_TIMERS.delete(button);
    }, 1400);
    FLASH_TIMERS.set(button, timer);
  }

  function delay(milliseconds) {
    return new Promise((resolve) => window.setTimeout(resolve, milliseconds));
  }

  function disconnectBridge() {
    state.pet?.destroy();
    state.stopped = true;
    if (!state.snapshot) return;
    const body = new Blob(
      [JSON.stringify({ bridge_token: state.snapshot.bridge_token })],
      { type: "application/json" }
    );
    navigator.sendBeacon(
      `/AgentArm/api/snapshots/${state.snapshot.share_id}/bridge/disconnect`,
      body
    );
  }

  async function collectProject(root) {
    const directories = [];
    const candidates = [];
    const excluded = {
      ignored_directories: 0,
      protected_files: 0,
      unsupported_files: 0,
      unreadable_files: 0,
      unreadable_directories: 0,
      oversized_files: 0,
      file_limit_files: 0,
      upload_budget_files: 0,
    };
    const excludedPaths = Object.fromEntries(
      Object.keys(excluded).map((key) => [key, []])
    );
    const omittedPaths = Object.fromEntries(
      Object.keys(excluded).map((key) => [key, 0])
    );
    let recordedExcludedPaths = 0;

    function exclude(category, path) {
      excluded[category] += 1;
      if (recordedExcludedPaths < excludedPathLimit) {
        excludedPaths[category].push(path);
        recordedExcludedPaths += 1;
      } else {
        omittedPaths[category] += 1;
      }
    }

    async function walk(directory, prefix) {
      const entries = [];
      try {
        for await (const entry of directory.values()) entries.push(entry);
      } catch (error) {
        if (!["NotFoundError", "NotAllowedError", "NotReadableError", "SecurityError"].includes(error.name)) throw error;
        if (!prefix) throw new Error("The selected folder is no longer accessible. Select it again; the button switches to Upload Folder if browser access keeps failing.");
        exclude("unreadable_directories", prefix.slice(0, -1));
      }
      entries.sort((left, right) => compareNames(left.name, right.name));
      for (const entry of entries) {
        const path = `${prefix}${entry.name}`;
        if (entry.kind === "directory") {
          if (isIgnoredDirectory(entry.name)) {
            exclude("ignored_directories", path);
            continue;
          }
          directories.push(path);
          await walk(entry, `${path}/`);
          continue;
        }
        if (isSensitive(path)) {
          exclude("protected_files", path);
          continue;
        }
        if (!isSupported(path)) {
          exclude("unsupported_files", path);
          continue;
        }
        let file;
        try {
          file = await entry.getFile();
        } catch {
          exclude("unreadable_files", path);
          continue;
        }
        if (file.size > state.policy.max_file_bytes) {
          exclude("oversized_files", path);
          continue;
        }
        candidates.push({ path, file });
      }
    }

    await walk(root, "");
    candidates.sort((left, right) =>
      importPriority(left.path) - importPriority(right.path) ||
      left.file.size - right.file.size ||
      compareNames(left.path, right.path)
    );
    const files = [];
    let bytes = 0;
    for (const candidate of candidates) {
      if (files.length >= state.policy.max_files) {
        exclude("file_limit_files", candidate.path);
        continue;
      }
      if (bytes + candidate.file.size > state.policy.max_upload_bytes) {
        exclude("upload_budget_files", candidate.path);
        continue;
      }
      files.push(candidate);
      bytes += candidate.file.size;
    }
    return {
      directories: [...new Set(directories)].sort(compareNames),
      files,
      excluded,
      excludedPaths: {
        counts: { ...excluded },
        paths: excludedPaths,
        omitted: omittedPaths,
      },
      bytes,
    };
  }

  async function readGit(root) {
    if (window.AgentArmGit?.readGit) {
      return window.AgentArmGit.readGit(root, {
        maxCommits: state.policy.max_git_commits,
        compareNames,
        isSensitive,
        isIgnoredPath,
      });
    }
    let git;
    try {
      git = await root.getDirectoryHandle(".git");
    } catch {
      return {
        detected: false, head: null, branch: null, refs: [],
        history: [], tracked_paths: [], commits: [],
        commit_graph_complete: false,
        commit_graph_source: null,
        commit_graph_error: "No Git repository was detected.",
      };
    }
    const head = (await readNestedText(git, "HEAD"))?.trim() || null;
    const headRef = head?.startsWith("ref: ") ? head.slice(5).trim() : null;
    const branch = headRef?.startsWith("refs/heads/")
      ? headRef.slice("refs/heads/".length)
      : headRef;
    const refs = await readRefs(git);
    const packedRefs = await readNestedText(git, "packed-refs");
    if (packedRefs) {
      for (const line of packedRefs.split(/\r?\n/)) {
        if (!line || line.startsWith("#") || line.startsWith("^")) continue;
        const [sha, name] = line.split(" ");
        if (/^[0-9a-f]{40,64}$/i.test(sha || "") && name) refs.push({ name, sha });
      }
    }
    let historySource = "logs/HEAD";
    let reflog = null;
    if (headRef) {
      reflog = await readNestedText(git, `logs/${headRef}`);
      if (reflog) historySource = `logs/${headRef}`;
    }
    if (!reflog) reflog = await readNestedText(git, "logs/HEAD");
    const history = parseReflog(reflog || "");
    let trackedPaths = [];
    try {
      const indexHandle = await git.getFileHandle("index");
      trackedPaths = parseGitIndex(await (await indexHandle.getFile()).arrayBuffer());
    } catch {
      // A missing, split, or unsupported index still leaves refs and history readable.
    }
    refs.sort((left, right) => compareNames(left.name, right.name));
    return {
      detected: true,
      head,
      branch,
      history_source: historySource,
      refs: deduplicateRefs(refs),
      history,
      tracked_paths: trackedPaths
        .filter((path) => !isSensitive(path) && !isIgnoredPath(path))
        .sort(compareNames),
      commit_graph_complete: false,
      commit_graph_source: "reflog-only fallback",
      commit_graph_error: (
        "The complete Git object reader did not load; reflog metadata is not " +
        "presented as complete commit history."
      ),
      commits: [],
    };
  }

  async function readRefs(git) {
    const records = [];
    let refsRoot;
    try {
      refsRoot = await git.getDirectoryHandle("refs");
    } catch {
      return records;
    }
    async function walk(directory, prefix) {
      const entries = [];
      for await (const entry of directory.values()) entries.push(entry);
      entries.sort((left, right) => compareNames(left.name, right.name));
      for (const entry of entries) {
        const path = `${prefix}${entry.name}`;
        if (entry.kind === "directory") await walk(entry, `${path}/`);
        else {
          const sha = (await (await entry.getFile()).text()).trim();
          if (/^[0-9a-f]{40,64}$/i.test(sha)) {
            records.push({ name: `refs/${path}`, sha });
          }
        }
      }
    }
    await walk(refsRoot, "");
    return records;
  }

  function parseReflog(value) {
    const records = [];
    for (const line of value.split(/\r?\n/)) {
      if (!line) continue;
      const match = line.match(
        /^([0-9a-f]{40,64}) ([0-9a-f]{40,64}) (.*?) <([^>]*)> (\d+) ([+-]\d{4})\t(.*)$/i
      );
      if (!match) continue;
      records.push({
        sequence: records.length + 1,
        old_sha: match[1],
        new_sha: match[2],
        actor: match[3],
        email: match[4],
        timestamp: Number(match[5]),
        timezone: match[6],
        message: match[7],
      });
    }
    return records;
  }

  function parseGitIndex(buffer) {
    const bytes = new Uint8Array(buffer);
    const view = new DataView(buffer);
    if (bytes.length < 12 || new TextDecoder().decode(bytes.slice(0, 4)) !== "DIRC") {
      return [];
    }
    const version = view.getUint32(4);
    if (![2, 3].includes(version)) return [];
    const entryCount = Math.min(view.getUint32(8), 1_000_000);
    const decoder = new TextDecoder("utf-8", { fatal: false });
    const paths = [];
    let offset = 12;
    for (let index = 0; index < entryCount && offset + 62 <= bytes.length; index += 1) {
      const entryStart = offset;
      const flags = view.getUint16(offset + 60);
      const declaredLength = flags & 0x0fff;
      offset += 62;
      let end = offset;
      const maximum = Math.min(bytes.length, offset + 4096);
      if (declaredLength < 0x0fff) end = Math.min(offset + declaredLength, maximum);
      else while (end < maximum && bytes[end] !== 0) end += 1;
      const path = decoder.decode(bytes.slice(offset, end));
      if (path) paths.push(path);
      while (end < bytes.length && bytes[end] !== 0) end += 1;
      offset = end + 1;
      while ((offset - entryStart) % 8 !== 0) offset += 1;
    }
    return [...new Set(paths)];
  }

  async function readNestedText(root, path) {
    try {
      const parts = path.split("/");
      let directory = root;
      for (const part of parts.slice(0, -1)) {
        directory = await directory.getDirectoryHandle(part);
      }
      const handle = await directory.getFileHandle(parts.at(-1));
      return (await handle.getFile()).text();
    } catch {
      return null;
    }
  }

  function deduplicateRefs(refs) {
    const values = new Map();
    for (const ref of refs) values.set(ref.name, ref);
    return [...values.values()];
  }

  function isSensitive(path) {
    const parts = String(path).split("/");
    return parts.some((part) => {
      const name = part.toLocaleLowerCase("en");
      if (name === ".git" || privateNames.has(name) ||
          /\.(pem|key|p12|pfx)$/i.test(name) || name.endsWith(".env")) {
        return true;
      }
      return name.startsWith(".env.") &&
        ![".env.example", ".env.sample"].includes(name);
    });
  }

  function isIgnoredDirectory(name) {
    const normalized = String(name).toLocaleLowerCase("en");
    return ignoredDirectories.has(normalized) || normalized.endsWith(".egg-info");
  }

  function isIgnoredPath(path) {
    const parts = String(path).split("/");
    return parts.slice(0, -1).some(isIgnoredDirectory);
  }

  function isSupported(path) {
    const name = path.split("/").at(-1).toLocaleLowerCase("en");
    const dot = name.lastIndexOf(".");
    const extension = dot >= 0 ? name.slice(dot) : "";
    return textExtensions.has(extension) || documentExtensions.has(extension) ||
      imageExtensions.has(extension) || textNames.has(name) ||
      [".env.example", ".env.sample"].includes(name);
  }

  function importPriority(path) {
    const name = path.split("/").at(-1).toLocaleLowerCase("en");
    const dot = name.lastIndexOf(".");
    const extension = dot >= 0 ? name.slice(dot) : "";
    if (textExtensions.has(extension) || textNames.has(name) ||
        [".env.example", ".env.sample"].includes(name)) return 0;
    if (documentExtensions.has(extension)) return 1;
    if (commonImageExtensions.has(extension)) return 2;
    return 3;
  }

  function formatBytes(bytes) {
    const units = ["B", "KiB", "MiB", "GiB"];
    let value = Number(bytes) || 0;
    let unit = 0;
    while (value >= 1024 && unit < units.length - 1) {
      value /= 1024;
      unit += 1;
    }
    return `${value.toLocaleString("en", { maximumFractionDigits: 1 })} ${units[unit]}`;
  }

  function compareNames(left, right) {
    return String(left).localeCompare(String(right), "en", {
      numeric: true,
      sensitivity: "variant",
    });
  }

  function setStatus(message, error = false) {
    const status = document.getElementById("status");
    status.textContent = message;
    status.classList.toggle("error", error);
  }

  async function fetchJson(url) {
    const response = await fetch(url, { credentials: "same-origin" });
    if (!response.ok) throw new Error(`Unable to load FolderLens policy (${response.status}).`);
    return response.json();
  }

  function openDatabase() {
    return new Promise((resolve, reject) => {
      const request = indexedDB.open("agentarm", 2);
      request.onupgradeneeded = () => {
        if (!request.result.objectStoreNames.contains("handles")) {
          request.result.createObjectStore("handles");
        }
      };
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
    });
  }

  async function saveHandle(handle) {
    const database = await openDatabase();
    await new Promise((resolve, reject) => {
      const transaction = database.transaction("handles", "readwrite");
      transaction.objectStore("handles").put(handle, "workspace");
      transaction.oncomplete = resolve;
      transaction.onerror = () => reject(transaction.error);
    });
    database.close();
  }

  async function loadHandle() {
    const database = await openDatabase();
    const handle = await new Promise((resolve, reject) => {
      const request = database.transaction("handles").objectStore("handles").get("workspace");
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
    });
    database.close();
    return handle;
  }
})();
