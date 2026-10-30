# FolderLens

## Build the downloadable agent source

After committing reviewed source, run
`python build_agent_release.py --commit HEAD --output-dir downloads`.
The ZIP, SHA-256 sidecar and JSON manifest come only from an explicit public
file allowlist at that exact Git commit. Working-tree edits, secrets, local
profiles, environments, private connection/deploy records and existing archives
are excluded. The package contains Python source and a generic quickstart;
dependencies install separately. Generated `downloads/` stays outside Git.

## Terminal agent for ChatGPT

FolderLens now also has a separate **MCP agent** with 49 tools for files,
paged search, Office/PDF documents, images, Git inspection, managed processes,
conflict-checked edits, previews, undo and optional private persistent history. The browser
sharing server below keeps its existing behavior and never gains terminal access.

```powershell
python folderlens.py --doctor
python folderlens.py --root "C:\path\to\your\project" --allow-write --allow-exec --print-config toml
```

Add the printed configuration to a compatible MCP client, or enter its command
and arguments in ChatGPT desktop **Settings → Plugins → MCPs → Add → STDIO**.
The client launches the process; keep stdout reserved for the MCP protocol.
Read-only is the default. `--allow-exec` grants the process your OS-user privileges;
`--root` restricts filesystem tools but **does not sandbox commands**.
Choose a project/data workspace outside the FolderLens installation: file tools
keep the running application's import root and Python environments read-only.
Runtime maintenance requires owner-authorized command execution.
History defaults to metadata in memory. `--history metadata` persists metadata;
`--history content` also retains bounded argument/result text, which can contain
sensitive data. Both persistent modes use private, bounded local storage.
Long tools can also run through `start_tool_call`, with a client `request_id`
for deduplication, followed by `get_tool_result` and `list_remote_tool_calls`.
Accepted work survives the initiating HTTP request; results/IDs remain bounded
process memory and are lost on server restart. Never infer that a missing result
means a write or command did not happen.
For an explicit owner request to disconnect an HTTP device, `shutdown(device_id)`
acknowledges the request before stopping its server. It requires execution
authority and the matching device alias; restoring access requires a local restart.

See [Agent setup and capability guide (Türkçe)](AGENT_GUIDE.md) for complete startup,
HTTP authentication, capabilities, limits, verification and rollback instructions.
For ChatGPT web, see the [detailed TR/EN setup](https://yavuzselimtech.tr/folderlens/agent/?lang=en#web): a private
OpenAI Secure MCP Tunnel, or a reachable HTTPS endpoint with terminal-approved
OAuth, scoped access and local client revocation.
Agent dependencies install into the local `.venv`; production's `requirements.txt`
does not include the MCP/terminal extensions. No OpenAI API key is needed for this
MCP server; the connected client supplies the model. The optional OpenAI tunnel
client requires its own restricted tunnel runtime credential.

[Reference scope and limitations](https://yavuzselimtech.tr/folderlens/agent/?lang=en#limits)
describe the comparison without claiming universal superiority or zero defects.

## Browser workspace sharing

The home page links to `/folderlens/agent/`, a Turkish/English guide explaining
local STDIO, private web tunnels, authority and connection checks. It works
without JavaScript and does not probe or control a visitor's local agent.
The guide offers a source ZIP, SHA-256 and commit manifest. The agent itself
works independently of this website. Publishing this page
does not install an agent or grant access to a computer.

FolderLens (formerly AgentArm) exposes a browser-selected project as ordered, fetchable HTML and JSON
while keeping the workspace only in bounded process memory. The selected
folder remains on its owner's device.

[Try FolderLens](https://yavuzselimtech.tr/AgentArm/) ·
[Source code](https://github.com/yavuzselimtechnology/folderlens)

To run your own local server, install Python 3.11 or newer, clone this repo,
then run `python run.py --server`. The launcher prepares an isolated `.venv`
when dependencies are missing. Running `python run.py` without `--server`
opens the hosted app instead. Remote AI clients need a reachable HTTPS server;
they cannot normally access your loopback address.

## How it works

There is a single **Choose Folder** button. Browsers without folder access
(or when access fails) switch that same button to **Upload Folder**. This standard
folder input does not require a persistent filesystem handle or IndexedDB.
The selected File objects pass through the same Git reader, exclusions,
ordering and size budgets without text conversion or duplicate content copies.
This mode is a read-only snapshot: select the folder again for later edits.
The browser may enumerate generated directories before FolderLens filters
them, and empty directories are not represented by standard folder inputs.

An unavailable subdirectory no longer aborts readable sibling files. The local
exclusion panel and AI index explicitly report folders not fully scanned;
an inaccessible root still fails rather than pretending to be a complete scan.
Unavailable Git metadata is marked incomplete without blocking source documents.
Failure to remember a folder locally does not prevent uploading it.

Regression: `node tests/check_folder_import.js`. Optionally pass a local folder
to test selection using only file metadata. Passing a second argument (server
origin) additionally uploads selected content, checks every source hash/content,
and disconnects the temporary test share. Use only explicitly authorized folders
and destinations. The native browser picker is a separate integration boundary.

1. Open `/AgentArm/` and choose a folder.
2. The browser reads supported project documents, original image assets, and
   reachable Git commits from loose and packed objects.
3. AgentArm creates a temporary, unguessable AI view with consecutive
   **AI Context**, **Git**, and **Project** sections.
4. Fetch-only clients read the HTML or direct JSON resources.
5. While the owner's AgentArm page remains open, an action-capable client can
   send a bounded change package through the temporary capability.
6. A fetch-only client cannot submit a safe write request. It can return the
   same JSON change package for the owner to paste into AgentArm and apply.

There is no account or persistent server-side project storage. Workspace
content, capabilities, and pending operations disappear when the single
service process restarts or the two-hour expiry is reached. Closing the local
owner page also sends a best-effort disconnect that forgets the workspace
immediately.

Folders saved by an older read-only AgentArm release are restored for reading
without forcing a new picker. **Enable Local Changes** upgrades that existing
browser permission before any write capability is copied or pasted changes are
applied. Newly selected folders request read access only. Editing controls are
in the optional **Apply AI changes** panel. Existing `/AgentArm/` URLs, API
headers, browser storage and `agentarm.*` protocol identifiers remain compatible.

Local apply serializes folder operations, checks original hashes before each
write, and bounds rollback copies to the existing 16 MiB change-package budget.
If a write fails, all touched files are offered rollback; any failed restores
are reported by path. This is best-effort recovery, not a filesystem transaction.
After saving, the full shared view is rebuilt. A refresh failure is reported
separately from successful local edits; do not reapply the package. Editing is
unnecessary for reading/sharing and full refresh can be costly for large folders.

Run `node tests/check_local_changes.js` for apply/rollback/concurrency regressions.

The folder picker includes a decorative, inline SVG idle pet. It peeks out
after 1.6 seconds without pointer, touch, or keyboard activity and hides again
on the next interaction. Its gaze and blink use compositor-friendly CSS
transforms, require no network asset or interval, pause while project work is
busy, and remain hidden when reduced motion or forced colors are requested.

## Read resources

- `/AgentArm/share/{id}/context.json` — canonical English instructions for AI
  clients: purpose, native-fetch transport, read order, path/line identity,
  coverage requirements, trust boundary, and write boundary.
- `/AgentArm/share/{id}/` — AI Context, Git, and compact Project Index summaries
  displayed in order, without embedding the complete Git/path manifests.
- `/AgentArm/share/{id}/git.json` — safe Git manifest.
- `/AgentArm/share/{id}/git-commit-links.html` — real links to every bounded
  commit-history page.
- `/AgentArm/share/{id}/git/commits/{page}.json` — up to 200 complete commit
  records with tree, parents, author, committer, subject, message, and refs.
- `/AgentArm/share/{id}/project.json` — compact ordered document index.
- `/AgentArm/share/{id}/document-links.html` — real HTML anchors for every
  ordered document. Restricted web readers can follow these links without
  constructing derived URLs from JSON text.
- `/AgentArm/share/{id}/document-links/{page}.html` — additional bounded link
  pages when a workspace has more than 400 documents.
- `/AgentArm/share/{id}/project-full.json` — streamed aggregate project
  response. Large text entries remain compact chunk manifests.
- `/AgentArm/share/{id}/project/documents/{n}.json` — one ordered document.
- `/AgentArm/share/{id}/project/documents/{n}/chunk-links.html` — real ordered
  links for every lossless chunk of a large text document.
- `/AgentArm/share/{id}/project/documents/{n}/chunks/{chunk}.json` — one
  32-KiB-or-smaller UTF-8 chunk with byte offsets and SHA-256.
- `/AgentArm/share/{id}/project/media/{n}/{filename}` — unchanged original
  image bytes with their specific media type and a restrictive sandbox CSP.
- `GET /AgentArm/api/snapshots/{id}/changes` — machine-readable discovery
  document with absolute Git, project index, and full-project URLs. The same
  path accepts authorized `POST` requests only for actual changes.

JSON is canonical. Paths, sequence numbers, source byte lengths, and SHA-256
hashes keep files and content unambiguous.

The AI view emits the complete context before Git and the compact Project
Index, keeping the entry response small enough for common URL readers. Every other
workspace JSON begins with a compact `ai_context` reference and carries an HTTP
`Link: ...; rel="describedby"` header to the same canonical context. This avoids
duplicating the instructions in every document while ensuring that clients
entering through the view, Git, project index, full project, or one document
are directed to the same protocol.

The context tells models to prefer their native web/URL-fetch tool. An isolated
shell DNS failure is identified as a client-sandbox limitation, not proof that
the workspace is unavailable. It also requires a sequence/path coverage ledger
and forbids claiming a complete review until every document has been read or
each unread sequence has been reported.

Some AI web-security layers only open a derived URL after it appeared as an
actual link on a previously fetched page. The context therefore sends clients
through `document_links_url`; each manifest page contains native `<a href>`
anchors in authoritative sequence order plus real Previous/Next links. Pages
hold at most 400 documents and are rejected if their UTF-8 response would
exceed 4 MiB.

**Copy Action Access** emits `agentarm.action-access.v2`. It carries the direct
`context_url`, `git_url`, `git_commit_links_url`, `document_links_url`, and
`project_full_url` beside the temporary write endpoint, with explicit GET/POST
instructions. This lets a fetch-only AI read the workspace and complete Git
history without mistaking the write endpoint for project content.

Large-project clients should fetch the compact `project_index_url` first, then
open `document_links_url` and follow its ordered anchors. A document whose serialized representation exceeds
32 KiB is represented by a small document manifest; the client opens its real
`chunk_links_url`, fetches every chunk in order, concatenates `chunk.content`
exactly, and can verify every chunk and the decoded representation with `content_utf8_sha256`.
`sha256` always identifies the original source bytes, including its BOM/encoding.
`chunk_content_field` identifies `content`, `markdown`, or `document_json`; for
`document_json`, concatenate first and then parse the resulting JSON document. The
access package also includes an absolute `project_document_url_template`.
`project_full_url` remains available for clients that can accept the aggregate
response, but chunked documents still require their chunk links.

## Change format

```json
{
  "format": "agentarm.changes.v1",
  "title": "Update the example",
  "operations": [
    {
      "op": "write",
      "path": "src/example.py",
      "base_sha256": "current 64-character SHA-256",
      "content": "complete replacement text\n"
    }
  ]
}
```

`delete` uses the same path and current hash without `content`. New files use
`base_sha256: null`. AgentArm rejects stale hashes, duplicate paths, protected
locations, binary writes, more than 100 operations, and more than 16 MiB of
change text. If a browser write fails, already applied operations are rolled
back before an error is returned. A successful change immediately refreshes
the same temporary AI view.

## Data and safety policy

- Native browser folder permission is the owner authorization boundary.
- Real environment files, credentials, private keys, certificates, and Git
  internals are never included.
- `.env.example` and `.env.sample` are allowed as templates.
- Names the server's path policy cannot represent safely (`:`, `~`, trailing
  space or dot, Windows reserved names such as `CON`, control characters, more
  than 48 levels or 1,024 bytes) stay local, so one Word lock file such as
  `~$report.docx` cannot make the whole import fail.
- `.venv`, `node_modules`, `__pycache__`, `.pytest_cache`, `.ruff_cache`,
  `.tmp`, caches, logs, build output, and runtime state are excluded.
- The server accepts at most 128 MiB per file, 384 MiB per workspace, 25,000
  documents, and 100,000 reachable commits. At most 10 workspaces and 512 MiB
  of normalized content plus original image bytes are held in process memory.
- A project is not rejected merely because some eligible files exceed those
  limits. The browser imports the best readable subset in deterministic
  priority order: text/source files, extractable documents, common
  browser-readable images, then specialist image formats. Smaller files win
  within each class so one large asset cannot displace many useful documents.
  Unreadable, oversized, count-limited, and memory-budget-limited files remain
  local. The connected page reports every skipped category and count and can
  reveal up to 2,000 exact paths on demand. Exact excluded paths never enter
  snapshot metadata or the public AI view; they remain in the owner's tab.
- Multipart files are processed one at a time after bounded request parsing;
  the application no longer duplicates the whole upload into a second Python
  byte list. The aggregate project response is JSON-streamed.
- Public publishing is rate-limited to 20 workspaces per source IP per hour.
- Every response is `no-store`; Nginx request and response buffering is off.
- The service has no writable production filesystem path.

The Git view contains HEAD, branch, all readable refs and reflogs, the tracked
path tree, and every reachable commit reconstructed in the browser from loose
objects and Git pack/index v2 storage, including OFS/REF deltas and annotated
tags. Git configuration, credentials, and raw object bytes are never
published. A failed/partial graph is explicitly marked incomplete; AgentArm
never silently presents reflog movement as full commit history.

## Supported reads

- Text/code: Markdown, JSON/JSONL, Python, Pine Script,
  JavaScript/TypeScript, HTML/CSS, XML, YAML, TOML, INI, SQL,
  shell/PowerShell/batch, CSV/TSV, dotfiles such as `.gitignore`, and common
  repository files.
- Word `.docx`: ordered paragraphs and tables.
- Excel `.xlsx`/`.xlsm`: ordered sheets, rows, coordinates, values, data types,
  and formulas.
- PDF: ordered extracted page text.
- PowerPoint `.pptx`: slide-order Markdown with top-to-bottom/left-to-right
  text, tables, chart values, embedded-image descriptions, and speaker notes.
- EPUB `.epub`: metadata and spine-order HTML/XHTML converted to Markdown;
  scripts and styles are removed before conversion.
- Jupyter Notebook `.ipynb`: ordered Markdown, code, and raw cells converted to
  fenced Markdown. Output payloads are deliberately omitted and counted.
- Images: unchanged originals for PNG/APNG, JPEG/JFIF, GIF, WebP, AVIF,
  SVG/SVGZ, BMP/DIB, TIFF, ICO/CUR, HEIC/HEIF, JPEG 2000/JPM/MJ2, JPEG XL,
  PSD, XCF, DDS, OpenEXR/HDR, Netpbm, PCX, QOI, SGI, TGA, EMF/WMF, DNG, and
  common camera RAW extensions (ARW, CR2/CR3, NEF, ORF, PEF, RW2, SRW, RAW).
  Browser/model preview support varies for specialist formats, but their exact
  bytes, path, media type, length, and SHA-256 remain fetchable.

Images are deliberately not re-encoded, resampled, rasterized, or base64
expanded: conversion would change pixels/metadata and consume additional RAM.
Only supported text documents can be changed. Archives, executables, legacy
Office formats, other binary media, and encrypted documents are excluded.

The document layer adopts the useful in-memory, Markdown-first conversion
patterns evaluated in Microsoft MarkItDown 0.1.7 without installing its full
general-purpose runtime. AgentArm uses only format-specific parsers, never
enables converter plugins, never resolves URLs, and never performs remote OCR
or writes conversion artifacts to disk. Archive expansion, entry size,
compression ratio, slide/shape/table/chart, EPUB spine, notebook cell, and
derived Markdown limits fail closed per document.

## Local development

```powershell
git clone https://github.com/yavuzselimtechnology/folderlens.git
cd folderlens
.\start.ps1 -DevelopmentServer
```

The local server prints `http://127.0.0.1:8765/`. That address redirects to
`/AgentArm/`, so copying the short PowerShell URL no longer produces a 404.

## Verification

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"
.\.venv\Scripts\python.exe -m ruff check agentarm run.py tests
.\.venv\Scripts\python.exe -m ruff format --check agentarm run.py tests
node --check app.js
node --check git-reader.js
node tests\check_git_reader.js
npm ci
npm test
npx playwright install chromium
# Start python run.py --server in another terminal, then:
$env:FOLDERLENS_BASE_URL = "http://127.0.0.1:8765"
npm run test:browser
```

Node.js 22 or newer and Git are required for the JavaScript tests. Git-reader
tests create and remove their own repository and cover loose and packed objects,
branches, annotated tags, reflogs and Unicode. Browser tests cover mobile and
desktop folder-upload fallback with synthetic content. Set
`FOLDERLENS_BROWSER_CHANNEL=msedge` to use an installed Edge instead of Chromium.

On Linux/macOS, use `python3 -m venv .venv`, `.venv/bin/python -m pip install
-r requirements-dev.txt`, and `.venv/bin/python run.py --server`.

## Production

FastAPI listens on `127.0.0.1:8765`; Nginx proxies only `/AgentArm/`.
The restricted `agentarm` service has a strict systemd sandbox, a single
worker, a 1 GiB hard memory limit, and no writable project-data directory.
Deployment assets are in `deploy/`.

The deployment assets are templates: adapt their paths and server name to your
own host. Keep one worker because workspaces and relay queues are process-local.
The public repository excludes private operations records and local environments.

## Bounded reading and fidelity

Large Word, Excel, PDF and Markdown-derived documents use the same bounded
chunk protocol as source text. Chunk JSON preserves every extracted character;
line ranges count physical CRLF, LF and CR endings. Complete lines stay together
when they fit; oversized lines have explicit continuation flags. The HTML
`chunks/{chunk}.html` reader exposes real previous/next and canonical JSON links,
escapes source markup and requires no JavaScript. HTML is a reading view; use
JSON for exact whitespace and newline reconstruction.

The 32 KiB chunk size is an application transport budget, not a promise about
any AI provider's context window. The entry HTML contains summaries only;
full manifests remain at their existing URLs. URL readers should follow paged
Document Links and report any missing files/chunks rather than claim completeness.

Text decoding accepts strict UTF-8 and BOM-marked UTF-16/UTF-32. Ambiguous legacy
encodings now produce a visible extraction error instead of silently guessing
Windows-1252 (which can corrupt Turkish text). Export those files as UTF-8 before
sharing. Office/PDF extraction remains a derived representation: scanned PDFs
need OCR, and layout or embedded content may not be represented by extracted text.
