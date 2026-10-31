# FolderLens Agent — quickstart

This is Python source, not an EXE or a preinstalled virtual environment. You need
Python 3.11+ and internet for the initial dependency installation. The package
contains no preauthorized access, account, API key or tunnel profile.

1. Extract the ZIP. It already contains a `FolderLens/` directory. For example,
   extract into `C:\Tools` so the script is `C:\Tools\FolderLens\folderlens.py`.
2. Open a terminal in that directory. Run `python folderlens.py --doctor`.
   Missing agent dependencies are prepared in the local `.venv`. For explicit
   installation, use `python -m venv .venv` then
   `.\.venv\Scripts\python.exe -m pip install -r requirements-agent.txt`.
3. Choose a separate existing workspace and print the client configuration:

   ```powershell
   .\.venv\Scripts\python.exe folderlens.py --root "C:\Work\MyProject" --print-config toml
   ```

4. Add a local MCP STDIO entry in your desktop client using the printed Python
   command and separate args. Use `--print-config json` for a JSON client.
   The client launches Python; this is not a website URL.
5. Select FolderLens in chat and call `get_config`. Check device, workspace and
   capabilities before changing any files.

Read-only is the default. Add `--allow-write` for file changes and separately
`--allow-exec` for commands, then update the client arguments. Commands have
your OS-user privileges; `--root` does not sandbox them. Use a workspace outside
the installation. On macOS/Linux use `.venv/bin/python` instead of Scripts.

For rich PDF generation with write_pdf, optionally install Chromium with the same venv:
`python -m playwright install chromium`. Legacy XLS writing has a separate
Windows-x64-only `--setup-xls` setup; it is unnecessary for normal startup.

ChatGPT web requires your own private Secure MCP Tunnel or a reachable
HTTPS/OAuth endpoint. The tunnel client uses a separate restricted runtime key
with Tunnels Read + Use. Local STDIO does not need an OpenAI model API key.
Keep credentials out of this website/chat and use protected local storage.
Computer, network and tunnel runtime must remain on for remote use.

[Detailed English setup](https://yavuzselimtech.tr/folderlens/agent/?lang=en) ·
[Ayrıntılı Türkçe kurulum](https://yavuzselimtech.tr/folderlens/agent/?lang=tr).
`AGENT_GUIDE.md` contains advanced tool details in Turkish.

Download the adjacent .sha256 sidecar and compare it using
`Get-FileHash .\folderlens-agent.zip -Algorithm SHA256` on Windows. RELEASE.json
records the source commit and each source file SHA-256. Dependencies are
downloaded separately; environments and private runtime state are excluded.

`python run.py` opens the separate browser-sharing website. For the full agent,
use `folderlens.py`. Existing /AgentArm/ URLs remain compatible; FolderLens is
the product name and /folderlens/ is the preferred public entry.


Process diagnostics: request `read_process_output` with `verbose_timing=true`
for monotonic elapsed/first-output times and the last 200 observed pipe reads.
Completion time includes process-tree/reader cleanup and stays fixed afterward.
No output means a null first-output time. Events contain absolute byte ranges,
sequence numbers and explicit dropped-event counts, without duplicate text.
These are observed reads, not individual child write calls.

`start_process` also accepts `wait_ms`, `wait_for_prompt`, and `verbose_timing`
to return initial output without a separate read. For a Python REPL, use
`wait_ms=3000, wait_for_prompt=true`. This wait is separate from the process
lifetime `timeout_seconds`; a running process continues after the wait expires.
Continue reading with the returned `next_offset`. The default zero wait and
disabled flags preserve the original immediate start response.

`interact_with_process` accepts the same three options to send input and return
only the response observed after that input in one call. Its response includes
`next_offset`, prompt state and optional timing. Invalid wait options are rejected
before stdin is changed; omitting the options preserves the immediate queued reply.

`get_recent_tool_calls` can combine an inclusive, timezone-aware RFC 3339
`since` instant with exact `tool_name` and boolean `success` filters. Persistent
history keeps `before_id` pagination under the same filters; memory history remains
process-local and does not expose arguments or results.

`list_processes` is a Remote Desktop Commander-compatible name for the same
identity-safe engine as `get_processes`. Unlike a flat process dump, both names
support paging, one-PID inspection, CPU/memory fields and a PID-reuse-resistant
`process_key`; neither exposes command lines or environment credentials.
