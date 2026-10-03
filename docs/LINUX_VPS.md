# Linux VPS over SSH

This runbook installs SEOHEAD as a versioned command for an SSH operator. Each crawl is a
foreground CLI process that reads a local configuration and writes scan/report files. It does not
install a daemon, open a port, or expose the local stdio MCP server over the network. The remote
HTTP API is a separate optional package and contract; see issue #784 when that API is needed.

The target covered here is **Ubuntu Server 24.04 LTS with Python 3.12**. The lifecycle smoke in
`.github/workflows/ci.yml` uses a fresh `ubuntu-24.04` runner, a localhost-only synthetic HTTP/JS
site, and two source revisions: the previous build gets a static artifact, while the candidate
build must capture the JavaScript-rendered page through the runner's preinstalled sandboxed Chrome.
This is evidence for that disposable runner environment; it is not a claim that a particular VPS
provider or every Linux distribution has been tested.

## Host and operator layout

Use a dedicated Unix account for the tool and its project data. SSH access, firewall policy, and
account creation belong to the host operator; this repository does not configure them. The account
needs a normal shell for SSH commands and no root privileges to run crawls. Use `sudo` only for
the OS packages required by Chromium.

The examples below keep each application revision in its own directory and keep projects outside
those directories:

| Data | Example path | Access |
|---|---|---|
| Versioned source and Python environment | `~/.local/share/seohead/releases/<commit>/` | Operator account; preserve old revisions for rollback |
| Active revision pointer | `~/.local/share/seohead/current` | Operator account |
| Configuration and default run journal | `~/.config/seohead/` | Mode `0700`; files with credentials mode `0600` |
| Projects, `scans/`, and reports | `~/seohead-projects/<project>/` | Mode `0700`, local filesystem |
| Playwright browser files | `<release>/browsers/` | Operator account; one browser build per release |
| Temporary files | `~/.cache/seohead/tmp/` | Mode `0700` |

Native scan artifacts default to `./scans/` below the working directory. Run commands from the
chosen project directory or pass an absolute `--scan-out` path. Active SQLite scans require a
local filesystem with the locking and hard-link behavior documented in [STORAGE.md](STORAGE.md);
do not write an active scan to NFS, SMB, or a live cloud-synchronized directory. The default run
journal is `~/.config/seohead/runs.jsonl`; it records command arguments, including URLs and paths,
so protect it as project data.

```bash
umask 077
install -d -m 700 \
  "$HOME/.local/share/seohead/releases" \
  "$HOME/.local/bin" \
  "$HOME/.config/seohead" \
  "$HOME/.cache/seohead/tmp" \
  "$HOME/seohead-projects"
```

## Install a pinned revision

Pick a full 40-character commit SHA from reviewed `main` history. Do not install from a moving
`main` branch or an unverified release tag. The package currently has no published GitHub release
artifact; a clean commit checkout is the source for this procedure. `uv.lock` pins Python package
versions, and the build embeds its source revision and package file hashes.

Install the operating-system tools and the pinned `uv` release:

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl git python3.12 python3.12-venv

curl -LsSf https://astral.sh/uv/0.11.26/install.sh \
  | env UV_INSTALL_DIR="$HOME/.local/bin" sh
"$HOME/.local/bin/uv" --version
```

Select the source revision, clone it into a versioned release directory, and sync only the
runtime dependencies required for reports and JavaScript rendering. `UV_PROJECT_ENVIRONMENT`
places the environment beside the checkout instead of in the source tree.

```bash
REVISION=0123456789abcdef0123456789abcdef01234567
SEOHEAD_ROOT="$HOME/.local/share/seohead"
RELEASE="$SEOHEAD_ROOT/releases/$REVISION"
mkdir -m 700 "$RELEASE"
git clone --no-checkout https://github.com/PavloSEO/seotools.git "$RELEASE/source"
git -C "$RELEASE/source" checkout --detach "$REVISION"
test "$(git -C "$RELEASE/source" rev-parse HEAD)" = "$REVISION"
test -z "$(git -C "$RELEASE/source" status --porcelain)"

(
  cd "$RELEASE/source"
  UV_PROJECT_ENVIRONMENT="$RELEASE/venv" \
    "$HOME/.local/bin/uv" sync --locked --no-dev --extra render --extra reports --no-editable
)
```

Install Chromium's system libraries with administrator rights, then download the matching browser
as the operator account. The browser files stay with this release, so switching back also restores
the browser revision expected by its locked Playwright dependency.

```bash
sudo "$RELEASE/venv/bin/python" -m playwright install-deps chromium
PLAYWRIGHT_BROWSERS_PATH="$RELEASE/browsers" \
  "$RELEASE/venv/bin/python" -m playwright install chromium
```

The renderer uses that release-local browser by default. On Ubuntu 24.04, AppArmor may block
unprivileged user namespaces for Playwright's downloaded headless shell. If that prevents a
sandboxed launch, an operator may select an installed Google Chrome executable for local Chromium
runs by setting `SEOHEAD_CHROME` (for example, `/opt/google/chrome/chrome`). The executable must
exist and be executable; an invalid explicit path fails without falling back. The renderer keeps
Playwright's Chromium sandbox enabled. This override is local-only; it does not configure a remote
browser transport or non-Chromium engines. Keep the release-local Playwright browser installed for
the default path and verify compatibility when using a system Chrome version.

The Ubuntu lifecycle CI uses the runner's preinstalled Google Chrome executable for the candidate
render smoke. Its metrics artifact records the executable path and browser version. The job does
not change host security settings or launch Chromium with `--no-sandbox`.

Create or switch the active pointer atomically. Keep the previous release directory until the new
revision passes its smoke run. Create the stable command symlink once; it follows `current` after
each switch.

```bash
ln -s "$RELEASE" "$SEOHEAD_ROOT/.current-next"
mv -Tf "$SEOHEAD_ROOT/.current-next" "$SEOHEAD_ROOT/current"
ln -s "$SEOHEAD_ROOT/current/venv/bin/seohead" "$HOME/.local/bin/seohead"
```

Set the project and config paths once for each operator account:

```bash
PROJECT="$HOME/seohead-projects/example"
install -d -m 700 "$PROJECT/scans" "$PROJECT/reports"
df -h "$PROJECT" "$SEOHEAD_ROOT"
du -sh "$RELEASE/venv" "$RELEASE/browsers" "$PROJECT/scans"
```

The native scan storage default keeps at least 1 GiB free on the scan filesystem and warns at a
20 GiB scan-history threshold. Check free space before large crawls and snapshots; these are storage
guards, not a per-site disk estimate. See [STORAGE.md](STORAGE.md) for the other capture limits.

Set the active paths in each SSH session, or add these non-secret values to that operator account's
shell profile:

```bash
export SEOHEAD_ROOT="$HOME/.local/share/seohead"
export PATH="$HOME/.local/bin:$PATH"
export PLAYWRIGHT_BROWSERS_PATH="$SEOHEAD_ROOT/current/browsers"
export TMPDIR="$HOME/.cache/seohead/tmp"
```

Store crawl configuration at `~/.config/seohead/crawl.json`, mode `0600`. Keep credentials outside
the checkout and pass them by the existing environment-variable or private config-file references
documented in [SETUP.md](SETUP.md). Do not put secret values in shell history, CLI arguments,
run-journal fixtures, or reports. Private-network overrides are off by default; do not set a broad
allow-private-network option for ordinary public-site work.

## Verify and identify the active build

The CLI version alone is not enough to distinguish two builds with the same package version. Check
both the CLI and the embedded package provenance from outside the source checkout:

```bash
"$HOME/.local/bin/seohead" --version
"$SEOHEAD_ROOT/current/venv/bin/python" -c \
  'from seohead.build_provenance import packaged_provenance; p=packaged_provenance(); print(p.version, p.revision)'
```

The provenance call validates the package source hashes and installed wheel metadata, then prints
the package version and full producer revision. A crawl records its producer version, revision,
Python/SQLite runtime versions, and effective config in the scan artifact. `seohead scan status
--scan <path>` reads those recorded values without resuming or modifying the scan.

Run a bounded smoke against a site you are authorized to crawl, from its project directory:

```bash
cd "$PROJECT"
PLAYWRIGHT_BROWSERS_PATH="$SEOHEAD_ROOT/current/browsers" \
  "$HOME/.local/bin/seohead" crawl-site \
    --url https://example.com/ \
    --config "$HOME/.config/seohead/crawl.json" \
    --max-urls 10 \
    --scan-out "$PROJECT/scans/first-check.sqlite"
"$HOME/.local/bin/seohead" scan list --directory "$PROJECT/scans"
"$HOME/.local/bin/seohead" scan status --scan "$PROJECT/scans/first-check.sqlite"
```

Choose the target site's request rate and crawl limits deliberately. A `finished` status describes
the requested bounded crawl; it does not claim that an arbitrary site was exhaustively collected.
Reports can be written under the project directory with `report-build --audit <scan-path> --format
md --out <report-path>`.

## Upgrade and rollback

Do not replace an active environment in place. Install a new full commit SHA in a new release
directory using the steps above, install its matching browser into its own `browsers/` directory,
and run the provenance check and a bounded smoke through that release's direct executable. Then
switch `current` using a fresh temporary symlink and `mv -Tf`:

```bash
ln -s "$NEW_RELEASE" "$SEOHEAD_ROOT/.current-upgrade"
mv -Tf "$SEOHEAD_ROOT/.current-upgrade" "$SEOHEAD_ROOT/current"
"$HOME/.local/bin/seohead" --version
```

If verification fails, or a later run requires rollback, switch `current` back to the recorded
previous release. This restores the prior executable and browser path without reinstalling it:

```bash
ln -s "$OLD_RELEASE" "$SEOHEAD_ROOT/.current-rollback"
mv -Tf "$SEOHEAD_ROOT/.current-rollback" "$SEOHEAD_ROOT/current"
"$HOME/.local/bin/seohead" --version
"$SEOHEAD_ROOT/current/venv/bin/python" -c \
  'from seohead.build_provenance import packaged_provenance; p=packaged_provenance(); print(p.version, p.revision)'
```

Keep project data outside release directories, so an application rollback does not roll data back
or remove it. Back up important completed scans with `seohead scan snapshot` before moving or
retiring their storage. Do not remove a release while an active process may still be using it.

Application rollback and scan recovery are separate operations. A native scan records its producer
revision and effective configuration; the crawler refuses to resume it under a different build or
configuration. To resume an interrupted scan, switch to its exact producer revision, restore the
same config and credentials out of band, and follow [RECOVERY.md](RECOVERY.md). Keep the artifact
and inspect its state if those inputs are unavailable; do not force or migrate the scan to make it
appear resumable. Completed scans remain readable through the supported status/report paths when
the stored format is supported.

## JavaScript and terminal jobs

The `render` extra installs Playwright; the separate browser installation and Linux system
libraries above are also required for JavaScript rendering. The default renderer refuses to run as
root. Static crawling can omit the `render` extra, but JavaScript evidence will then be unavailable
and must not be reported as measured.

The documented execution model is an SSH operator starting the CLI. The command runs in the
foreground and writes JSON to stdout plus progress/errors to stderr. If the SSH session must be
closed while a long run continues, use a terminal multiplexer already approved for that host and
reattach before reading its output. A resident HTTP service, remote MCP endpoint, and public listener
are outside this runbook; the separately designed remote scan API is tracked in #784.

## Measured resource profile

The `linux-lifecycle` CI job writes a `linux-lifecycle-metrics.json` artifact for each run. It records
the Ubuntu image, Python/SQLite/uv versions, locked dependency profile, install duration and
environment size, downloaded Chromium size, synthetic fixture bytes, scan artifact bytes,
workspace bytes, crawl duration, and sampled RSS for the CLI plus Chromium process group. RSS is
sampled at 100 ms through Linux procfs, so a shorter transient can be missed.

Those measurements cover one localhost page, one rendered DOM, two adjacent builds, and an
install/upgrade/rollback sequence on a disposable GitHub Actions runner. Use them to reproduce that
small workflow and to see the added cost of the optional browser. They do not estimate the RAM,
disk, or runtime needed for an arbitrary site or a production VPS; record a workload-specific
measurement before choosing host capacity.
