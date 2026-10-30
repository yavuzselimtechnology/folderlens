"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const { webcrypto, createHash } = require("node:crypto");
const source = fs.readFileSync(require("node:path").join(__dirname, "../app.js"), "utf8");
const hash = (text) => createHash("sha256").update(text).digest("hex");

function fixture(initial, hooks = {}) {
  const files = new Map(Object.entries(initial));
  const context = vm.createContext({
    TextEncoder, Uint8Array, crypto: webcrypto,
    window: { addEventListener() {} },
    document: { getElementById: () => ({ disabled: false }) },
    io: {
      async get(path) {
        await hooks.read?.(path, files);
        return files.has(path) ? new Blob([files.get(path)]) : null;
      },
      async write(path, value) {
        await hooks.write?.(path, value, files);
        files.set(path, typeof value === "string" ? value : Buffer.from(value).toString());
      },
      async remove(path) { files.delete(path); },
      refresh: hooks.refresh || (async () => {}),
    },
  });
  vm.runInContext(source.replace(/\}\)\(\);\s*$/, `
    state.policy = { max_file_bytes: 128 * 1024 * 1024 };
    ensureWritePermission = async () => {};
    getLocalFile = io.get; writeLocalFile = io.write; removeLocalFile = io.remove;
    refreshSnapshot = io.refresh; setChangeStatus = () => {};
    globalThis.test = { applyLocalChanges, answerCommand, state, setPost(fn) { postJson = fn; } };
  })();`), context);
  return { files, ...context.test };
}
const payload = (...operations) => ({ format: "agentarm.changes.v1", title: "Test", operations });
const write = (path, before, content) => ({ op: "write", path, base_sha256: before === null ? null : hash(before), content });

(async () => {
  let f = fixture({ "a.txt": "old" });
  assert.equal((await f.applyLocalChanges(payload(write("a.txt", "old", "Türkçe\r\n")))).state, "applied");
  assert.equal(f.files.get("a.txt"), "Türkçe\r\n");
  await assert.rejects(f.applyLocalChanges(payload(write("a.txt", "old", "bad"))), /changed|hash/i);

  let reads = 0;
  f = fixture({ "a.txt": "old" }, { read: (_, files) => { if (++reads === 2) files.set("a.txt", "external edit"); } });
  await assert.rejects(f.applyLocalChanges(payload(write("a.txt", "old", "bad"))), /changed during apply/);
  assert.equal(f.files.get("a.txt"), "external edit");

  f = fixture({ "a.txt": "old", "b.txt": "before" }, { write: (path, value) => {
    if (path === "b.txt" && typeof value === "string") throw new Error("Disk failure");
  } });
  await assert.rejects(f.applyLocalChanges(payload(write("a.txt", "old", "new"), write("b.txt", "before", "new"))), /Disk failure/);
  assert.equal(f.files.get("a.txt"), "old");
  assert.equal(f.files.get("b.txt"), "before");
  assert.equal(f.state.busy, false);

  f = fixture({ "a.txt": "old", "b.txt": "before" }, { write: (path) => {
    if (path === "b.txt") throw new Error("Disk failure");
  } });
  await assert.rejects(f.applyLocalChanges(payload(write("a.txt", "old", "new"), write("b.txt", "before", "new"))), /Rollback incomplete.*b.txt/);
  assert.equal(f.files.get("a.txt"), "old", "rollback must continue after another restore fails");

  f = fixture({}, { refresh: async () => { throw new Error("Offline"); } });
  const result = await f.applyLocalChanges(payload(write("new.txt", null, "saved")));
  assert.equal(result.state, "applied");
  assert.equal(result.view_refreshed, false);
  assert.match(result.warning, /Do not apply/);
  assert.equal(f.files.get("new.txt"), "saved");

  let release;
  f = fixture({}, { refresh: () => new Promise(resolve => { release = resolve; }) });
  const running = f.applyLocalChanges(payload(write("new.txt", null, "saved")));
  await assert.rejects(f.applyLocalChanges(payload(write("other.txt", null, "no"))), /operation is running/i);
  while (!release) await new Promise(resolve => setImmediate(resolve));
  release(); await running;
  assert.equal(f.state.busy, false);
  // A refused late answer must not look like an expired share to the poll loop.
  f = fixture({});
  const refused = Object.assign(new Error("expired"), { status: 403 });
  f.setPost(async () => { throw refused; });
  await f.answerCommand({ share_id: "s", bridge_token: "t" }, { id: "1", operation: "unknown" });
  const offline = Object.assign(new Error("down"), { status: 502 });
  f.setPost(async () => { throw offline; });
  await assert.rejects(f.answerCommand({ share_id: "s", bridge_token: "t" }, { id: "1", operation: "unknown" }), /down/);
  console.log("Local changes: success, stale hashes, concurrent edits, rollback, refresh failure and locking passed.");
})().catch(error => { console.error(error); process.exitCode = 1; });
