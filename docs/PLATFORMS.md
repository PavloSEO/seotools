# Windows, Linux, and macOS

The CLI and local stdio MCP server use the same Python package on all three
platforms. Native SQLite collection does not require Screaming Frog. Live SF
collection still requires a separately installed licensed application.

## Windows (PowerShell)

From the repository directory, use an explicit virtual-environment interpreter;
activating a script is not required and does not depend on PowerShell's execution policy:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install ".[mcp,reports]"
.\.venv\Scripts\python.exe -m seohead --version
.\.venv\Scripts\python.exe -m seohead mcp --profile router
```

For an MCP client, use the absolute path to `.venv\Scripts\python.exe` as the
command and `-m`, `seohead`, `mcp` as separate arguments. Do not put a combined
shell command into the executable field. Paths with spaces and Unicode are
supported.

Native scan writer exclusion uses Windows CRT byte-range locks on the existing
`.writer.lock` sidecar. Locks are nonblocking and released when their handle or
process closes; a second writer is refused. Keep scan workspaces on a local
filesystem supporting hard links, such as NTFS. Network shares and live cloud-sync
folders are not covered by the local SQLite concurrency guarantee.

File contents are flushed and SQLite uses `synchronous=FULL`. The Windows CRT
does not provide POSIX directory `fsync`: publication remains atomic, but the
package does not claim the same directory-entry durability across a power failure.

## Linux and macOS

```bash
python3 -m venv .venv
.venv/bin/python -m pip install ".[mcp,reports]"
.venv/bin/python -m seohead --version
.venv/bin/python -m seohead mcp --profile router
```

POSIX platforms retain their advisory writer locks and directory flushing.
Snapshots use the portable filesystem free-space API. Both Windows and POSIX
refuse symlinked or hard-linked lock aliases, and readers never migrate a scan.

## Verification scope

The Windows CI job exercises real OS lock contention across processes, project
publication/checklists in Unicode and spaced paths, native scan creation,
read-only reopening, snapshots, retry/resume, offline reanalysis, and a real
stdio MCP session. It also runs CLI/MCP registration and interface contracts.
Linux CI retains the broader Python-version matrix. These checks do not imply
that paid APIs or a licensed Screaming Frog installation were exercised.
