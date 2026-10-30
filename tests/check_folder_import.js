"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { execFileSync } = require("node:child_process");
const source = fs.readFileSync(path.join(__dirname, "../app.js"), "utf8");
function harness(code = source) {
  const elements = new Map();
  const context = vm.createContext({
    TextEncoder, TextDecoder, DOMException, FormData, File,
    window: { addEventListener() {} },
    document: { getElementById(id) {
      if (!elements.has(id)) elements.set(id, { click() { this.clicked = true; }, classList: { toggle() {} } });
      return elements.get(id);
    } },
  });
  const exports = code.includes("function folderFromFiles") ? ", folderFromFiles, chooseFolder, ensureWritePermission" : "";
  vm.runInContext(code.replace(/\}\)\(\);\s*$/, `
    state.policy = {max_file_bytes:128*1024*1024,max_upload_bytes:384*1024*1024,max_files:25000};
    globalThis.test = {collectProject, prepareSnapshot, state ${exports},
      setHooks(h) { if(h.publish) publish=h.publish; if(h.save) saveHandle=h.save; if(h.git) readGit=h.git; }
    };
  })();`), context);
  return { ...context.test, context, elements };
}
const file = (name, content = "İçerik\r\n") => ({name,kind:"file",async getFile() { return new File([content],name); }});
const dir = (name, entries, failure) => ({name,kind:"directory",async *values() { yield* entries; if(failure) throw new DOMException("Missing directory",failure); }});

(async () => {
  const tree = dir("project", [file("ok.py"), dir("gone",[file("partial.txt")],"NotFoundError"), dir("later",[file("after.py")])]);
  if (process.env.FOLDERLENS_BASELINE) {
    const before = harness(execFileSync("git",["show",`${process.env.FOLDERLENS_BASELINE}:AgentArm/app.js`],{encoding:"utf8"}));
    await assert.rejects(before.collectProject(tree), {name:"NotFoundError"});
    console.log("Baseline reproduced: one unavailable directory aborted the old scan.");
  }
  let h = harness();
  let result = await h.collectProject(tree);
  assert.equal(result.files.length,3);
  assert.equal(result.excluded.unreadable_directories,1);
  assert.equal(result.excludedPaths.paths.unreadable_directories[0],"gone");
  await assert.rejects(h.collectProject(dir("root",[],"NotFoundError")),/no longer accessible/);
  await assert.rejects(h.collectProject(dir("root",[],"TypeError")),{name:"TypeError"});
  console.log("Partial scan preserves siblings and reports incomplete folders; root/programming failures remain visible.");

  const files = [new File(["line\r\n"],"a.py"),new File(["private"],".env"),new File(["cache"],"a.pyc")];
  ["project/src/a.py","project/.env","project/__pycache__/a.pyc"].forEach((p,i)=>Object.defineProperty(files[i],"webkitRelativePath",{value:p}));
  const root = h.folderFromFiles(files);
  result = await h.collectProject(root);
  assert.equal(result.files.length,1);
  assert.equal(result.files[0].file,files[0],"must retain original File, not a text conversion/copy");
  assert.equal(result.excluded.protected_files,1);
  assert.equal(result.excluded.ignored_directories,1);
  // Names the server's path policy rejects must stay local instead of failing the whole upload.
  const odd = ["ok.md", "~$lock.docx", "a:b.md", "CON.txt", "~tmp/inner.md", "trail./x.md"]
    .map(name => { const f = new File(["x"], name.split("/").pop()); Object.defineProperty(f, "webkitRelativePath", {value: "project/" + name}); return f; });
  result = await h.collectProject(h.folderFromFiles(odd));
  assert.deepEqual(Array.from(result.files, item => item.path), ["ok.md"]);
  assert.equal(result.excluded.unsupported_files, 3);
  assert.equal(result.excluded.ignored_directories, 2);
  h.state.directory=root;
  await assert.rejects(h.ensureWritePermission(true),/read-only/);
  h.setHooks({git:async()=>{throw new DOMException("Missing Git ref","NotFoundError");}});
  const prepared=await h.prepareSnapshot(root);
  const metadata=JSON.parse(prepared.body.get("metadata"));
  assert.equal(metadata.git.commit_graph_complete,false);
  assert.equal(prepared.count,1);
  assert.equal(await prepared.body.get("files").text(),"line\r\n");

  let published=0;
  h=harness();
  h.context.window.showDirectoryPicker=async()=>({name:"new"});
  h.state.savedHandle={requestPermission(){throw new Error("Stale saved handle reused");}};
  h.setHooks({publish:async()=>{published++;},save:async()=>{throw new DOMException("Storage unavailable","NotFoundError");}});
  await h.chooseFolder(); assert.equal(published,1); assert.equal(h.state.useFolderUpload,false);
  h.context.window.showDirectoryPicker=async()=>{throw new DOMException("Picker unavailable","NotFoundError");};
  await h.chooseFolder(); assert.equal(h.state.useFolderUpload,true);
  await h.chooseFolder(); assert.equal(h.elements.get("folderUpload").clicked,true);
  console.log("Fallback: exact File reuse, exclusions, read-only guard, incomplete Git, stale handle/storage and retry passed.");

  if (process.argv[2]) {
    const folder=path.resolve(process.argv[2]);
    const selection=[];
    function visit(directory) {
      for (const entry of fs.readdirSync(directory,{withFileTypes:true})) {
        const full=path.join(directory,entry.name);
        if(entry.isSymbolicLink()) continue;
        if(entry.isDirectory()) { visit(full); continue; }
        // Only metadata is needed for selection. No file content is read here.
        const stat=fs.statSync(full);
        selection.push({name:entry.name,size:stat.size,webkitRelativePath:path.basename(folder)+"/"+path.relative(folder,full).split(path.sep).join("/")});
      }
    }
    visit(folder);
    h=harness();
    const start=performance.now();
    result=await h.collectProject(h.folderFromFiles(selection));
    console.log(JSON.stringify({selected:result.files.length,bytes:result.bytes,excluded:result.excluded,selection_ms:Math.round(performance.now()-start)}));
    assert(result.files.length>0);
    if (process.argv[3]) {
      const base = process.argv[3];
      const selected = new Set(result.files.map(item => item.file));
      const originals = new Map();
      const browserFiles = selection.map(item => {
        const relative = item.webkitRelativePath.slice(path.basename(folder).length + 1);
        const bytes = selected.has(item) ? fs.readFileSync(path.join(folder, relative)) : Buffer.alloc(0);
        if (selected.has(item)) originals.set(relative, bytes);
        const f = new File([bytes], item.name);
        Object.defineProperty(f, "webkitRelativePath", {value:item.webkitRelativePath});
        return f;
      });
      const prepared = await h.prepareSnapshot(h.folderFromFiles(browserFiles));
      const start = performance.now();
      const response = await fetch(base + "/AgentArm/api/snapshots", {method:"POST",body:prepared.body});
      assert.equal(response.status,201,await response.clone().text());
      const share = await response.json();
      const loadMs = Math.round(performance.now()-start);
      const get = async url => {
        const r=await fetch(new URL(url,base)); assert.equal(r.status,200); return r.json();
      };
      try {
        const project=await get(share.project_url);
        assert.equal(project.documents.length,originals.size);
        assert.equal(project.summary.excluded.protected_files,1);
        for (const item of project.documents) {
          const doc=(await get(item.document_url)).document;
          const original=originals.get(item.path);
          assert.equal(doc.sha256,require("node:crypto").createHash("sha256").update(original).digest("hex"));
          let content=doc.content;
          if(item.chunked) {
            const chunks=[];
            for(let i=1;i<=item.chunk_count;i++) chunks.push((await get(item.chunk_url_template.replace("{chunk}",i))).chunk.content);
            content=chunks.join("");
          }
          assert.deepEqual(Buffer.from(content,"utf8"),original,item.path);
        }
        console.log(JSON.stringify({http_import_ms:loadMs,verified_files:originals.size,exact_source_bytes:true}));
      } finally {
        const r=await fetch(base+`/AgentArm/api/snapshots/${share.share_id}/bridge/disconnect`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({bridge_token:share.bridge_token})});
        assert.equal(r.status,200);
      }
    }
  }
})().catch(error=>{console.error(error);process.exitCode=1;});
