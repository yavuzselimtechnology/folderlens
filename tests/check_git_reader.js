"use strict";

const fs = require("node:fs");
const path = require("node:path");
const os = require("node:os");
const { execFileSync } = require("node:child_process");

global.window = global;
require(path.resolve(__dirname, "..", "git-reader.js"));

class LocalFileHandle {
  constructor(filePath) {
    this.kind = "file";
    this.name = path.basename(filePath);
    this.filePath = filePath;
  }

  async getFile() {
    return new Blob([fs.readFileSync(this.filePath)]);
  }
}

class LocalDirectoryHandle {
  constructor(directoryPath) {
    this.kind = "directory";
    this.name = path.basename(directoryPath);
    this.directoryPath = directoryPath;
  }

  async getDirectoryHandle(name) {
    const target = path.join(this.directoryPath, name);
    if (!fs.statSync(target, { throwIfNoEntry: false })?.isDirectory()) {
      const error = new Error(`Directory not found: ${name}`);
      error.name = "NotFoundError";
      throw error;
    }
    return new LocalDirectoryHandle(target);
  }

  async getFileHandle(name) {
    const target = path.join(this.directoryPath, name);
    if (!fs.statSync(target, { throwIfNoEntry: false })?.isFile()) {
      const error = new Error(`File not found: ${name}`);
      error.name = "NotFoundError";
      throw error;
    }
    return new LocalFileHandle(target);
  }

  async *values() {
    for (const entry of fs.readdirSync(this.directoryPath, { withFileTypes: true })) {
      const target = path.join(this.directoryPath, entry.name);
      if (entry.isDirectory()) yield new LocalDirectoryHandle(target);
      else if (entry.isFile()) yield new LocalFileHandle(target);
    }
  }
}

async function checkRepository(repository) {
  const expected = execFileSync("git", ["rev-list", "--all"], {
    cwd: repository,
    encoding: "utf8",
  }).trim().split(/\r?\n/).filter(Boolean);
  const expectedHead = execFileSync("git", ["rev-parse", "HEAD"], {
    cwd: repository,
    encoding: "utf8",
  }).trim();
  const expectedSubject = execFileSync("git", ["log", "-1", "--format=%s"], {
    cwd: repository,
    encoding: "utf8",
  }).trim();
  const result = await AgentArmGit.readGit(new LocalDirectoryHandle(repository), {
    maxCommits: 100_000,
  });
  if (!result.commit_graph_complete) {
    throw new Error(`Commit graph incomplete: ${result.commit_graph_error}`);
  }
  const actual = new Set(result.commits.map((commit) => commit.sha));
  const missing = expected.filter((sha) => !actual.has(sha));
  if (actual.size !== expected.length || missing.length) {
    throw new Error(
      `Commit coverage mismatch: expected=${expected.length} actual=${actual.size} missing=${missing.slice(0, 5)}`
    );
  }
  const head = result.commits.find((commit) => commit.sha === expectedHead);
  if (!head || head.subject !== expectedSubject) {
    throw new Error("HEAD commit metadata does not match git log");
  }
  if (result.history.length < 1 || result.tracked_paths.length < 1) {
    throw new Error("Reflog or tracked-path coverage is empty");
  }
  const legacyCommit = Buffer.concat([
    Buffer.from(
      `tree ${"f".repeat(40)}\nauthor QA <qa@example.test> 1 +0000\n` +
      "committer QA <qa@example.test> 1 +0000\nencoding windows-1252\n\n",
      "ascii"
    ),
    Buffer.from([0x43, 0x61, 0x66, 0xe9, 0x0a]),
  ]);
  const parsedLegacy = AgentArmGit._test.parseCommit(
    "a".repeat(40), new Uint8Array(legacyCommit)
  );
  if (parsedLegacy.message !== "Café\n") {
    throw new Error("Declared legacy commit encoding was not preserved");
  }
  process.stdout.write(
    `Git reader OK: ${actual.size} commits, ${result.history.length} reflog records, ` +
    `${result.tracked_paths.length} tracked paths.\n`
  );
}

async function main() {
  const repository = fs.mkdtempSync(path.join(os.tmpdir(), "folderlens-git-test-"));
  const git = (...args) => execFileSync("git", args, { cwd: repository, stdio: "pipe" });
  try {
    git("init", "--initial-branch=main");
    git("config", "user.name", "FolderLens Test");
    git("config", "user.email", "qa@example.test");
    git("config", "commit.gpgsign", "false");
    git("config", "tag.gpgsign", "false");
    fs.writeFileSync(path.join(repository, "README.md"), "# Fixture\n");
    git("add", "README.md");
    git("commit", "-m", "Initial fixture");
    git("tag", "-a", "v1", "-m", "Annotated fixture tag");
    git("checkout", "-b", "feature");
    fs.writeFileSync(path.join(repository, "notes.txt"), "İçerik α\n");
    git("add", "notes.txt");
    git("commit", "-m", "Add Unicode notes");
    git("checkout", "main");
    fs.appendFileSync(path.join(repository, "README.md"), "Second revision\n");
    git("add", "README.md");
    git("commit", "-m", "Revise fixture");
    await checkRepository(repository);
    git("gc", "--prune=now");
    await checkRepository(repository);
  } finally {
    fs.rmSync(repository, { recursive: true, force: true });
  }
}

main().catch((error) => {
  process.stderr.write(`${error.stack || error}\n`);
  process.exitCode = 1;
});
