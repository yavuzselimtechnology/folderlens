"use strict";

const assert = require("node:assert/strict");
const { chromium } = require("playwright");
const base = process.env.FOLDERLENS_BASE_URL || "http://127.0.0.1:5089";

(async () => {
  const browser = await chromium.launch(
    process.env.FOLDERLENS_BROWSER_CHANNEL ? { channel: process.env.FOLDERLENS_BROWSER_CHANNEL } : {}
  );
  try {
    for (const width of [360, 1280]) {
      const context = await browser.newContext({ viewport: { width, height: 800 } });
      const page = await context.newPage();
      const errors = [];
      page.on("pageerror", error => errors.push(error.message));
      await page.addInitScript(() => {
        Object.defineProperty(window, "showDirectoryPicker", { value: undefined });
      });
      let snapshot;
      try {
        await page.goto(`${base}/AgentArm/`);
        await page.waitForFunction(() => !document.getElementById("chooseFolder").disabled);
        assert.equal(await page.locator("#chooseFolder").innerText(), "Upload Folder");
        const chooser = page.waitForEvent("filechooser");
        await page.locator("#chooseFolder").click();
        assert.equal((await chooser).isMultiple(), true);

        const published = page.waitForResponse(response =>
          response.url().endsWith("/api/snapshots") && response.request().method() === "POST");
        await page.evaluate(() => {
          const file = new File(["# Browser fixture\n"], "README.md");
          Object.defineProperty(file, "webkitRelativePath", { value: "fixture/README.md" });
          const transfer = new DataTransfer();
          transfer.items.add(file);
          const input = document.getElementById("folderUpload");
          input.files = transfer.files;
          input.dispatchEvent(new Event("change", { bubbles: true }));
        });
        const response = await published;
        assert.equal(response.status(), 201);
        snapshot = await response.json();
        await page.waitForFunction(() => document.getElementById("connectionState").textContent === "Live");
        assert.equal(await page.locator("#workspaceName").innerText(), "fixture");
        const project = await (await context.request.get(new URL(snapshot.project_url, base).href)).json();
        assert.equal(project.documents.length, 1);
        assert.equal(project.documents[0].path, "README.md");
        const document = await (await context.request.get(new URL(project.documents[0].document_url, base).href)).json();
        assert.equal(document.document.content, "# Browser fixture\n");
        assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
        assert.deepEqual(errors, []);
      } finally {
        if (snapshot) {
          const result = await context.request.post(`${base}/AgentArm/api/snapshots/${snapshot.share_id}/bridge/disconnect`, {
            data: { bridge_token: snapshot.bridge_token },
          });
          assert.equal(result.status(), 200);
        }
        await context.close();
      }
    }
    console.log("FolderLens mobile/desktop: fallback button, file chooser, exact shared content, layout and startup passed.");
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
