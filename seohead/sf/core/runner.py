"""ExportRunner — mode A: drive the Screaming Frog CLI to produce exports.

Requires a licensed SF install (headless export and ``--load-crawl`` are paid
features). Builds the ``--export-tabs`` / ``--bulk-export`` /
``--save-report`` command from the configured profile and runs it headless.
"""

from __future__ import annotations

import contextlib
import glob
import hashlib
import os
import plistlib
import re
import shutil
import signal
import subprocess
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from seohead.recon.net import validate_url

from ..config import deep_merge
from ..export_manifest import (
    CLI_HELP_FLAGS,
    help_fingerprint,
    help_names,
    is_full_manifest,
    requests_from_config,
    resolved_profile,
    unsupported_requests,
    validate_export_files,
)

# Executable names across platforms (Windows GUI ships a separate *Cli.exe;
# the macOS .app bundle ships ScreamingFrogSEOSpiderLauncher instead of *Cli).
SF_EXE_NAMES = (
    "ScreamingFrogSEOSpiderCli.exe",
    "ScreamingFrogSEOSpiderCli",
    "ScreamingFrogSEOSpiderLauncher",
    "screamingfrogseospider",
    "screaming-frog-seo-spider",
)

# Glob patterns for standard install locations on Windows / macOS / Linux.
# ``Program Files*`` matches both "Program Files" and "Program Files (x86)".
# On macOS the CLI entry point is the app-bundle *Launcher* (SF 19.x); the
# *Cli name is kept first in case a future build ships a dedicated binary.
SF_GLOBS = (
    r"C:\Program Files*\Screaming Frog SEO Spider\ScreamingFrogSEOSpiderCli.exe",
    r"C:\Program Files*\Screaming Frog SEO Spider\*Cli.exe",
    "/Applications/Screaming Frog SEO Spider.app/Contents/MacOS/ScreamingFrogSEOSpiderCli",
    "/Applications/Screaming Frog SEO Spider.app/Contents/MacOS/ScreamingFrogSEOSpiderLauncher",
    os.path.expanduser(
        "~/Applications/Screaming Frog SEO Spider.app/Contents/MacOS/ScreamingFrogSEOSpiderCli"
    ),
    os.path.expanduser(
        "~/Applications/Screaming Frog SEO Spider.app/Contents/MacOS/ScreamingFrogSEOSpiderLauncher"
    ),
    "/usr/bin/screamingfrogseospider",
    "/usr/local/bin/screamingfrogseospider",
    "/opt/screamingfrogseospider/screamingfrogseospider",
    "/snap/bin/screaming-frog-seo-spider",
)


def _candidate_paths(config: dict, override: str | None) -> Iterator[str]:
    """Yield every place the SF CLI could live, most specific first."""
    sf = config.get("sf_cli", {})
    yield from (
        override,
        os.environ.get("SF_CLI"),
        os.environ.get("SCREAMINGFROG_CLI"),
        sf.get("path"),
    )
    yield from sf.get("search_paths", [])
    for name in SF_EXE_NAMES:  # anything on PATH
        hit = shutil.which(name)
        if hit:
            yield hit
    for pattern in SF_GLOBS:  # standard install dirs (incl. versioned/x86)
        yield from glob.glob(pattern)


def find_sf_cli(config: dict, override: str | None = None) -> str | None:
    """Locate the Screaming Frog CLI anywhere it could reasonably be; None if absent."""
    seen: set[str] = set()
    for path in _candidate_paths(config, override):
        if not path or path in seen:
            continue
        seen.add(path)
        if os.path.isfile(path):
            return path
    return None


def resolve_cli(config: dict, override: str | None = None) -> str:
    path = find_sf_cli(config, override)
    if path:
        return path
    raise FileNotFoundError(
        "Screaming Frog CLI not found. Searched --sf-cli, $SF_CLI, config sf_cli.path, "
        "PATH, and standard install dirs (Program Files / Applications / /usr /opt /snap). "
        "Set sf_cli.path in config or pass --sf-cli, or use mode B (--exports-dir). "
        "Mode A also needs a licensed SF."
    )


def build_command(
    cli_path: str, *, source_arg: str, source_value: str, output_folder: str, config: dict
) -> list[str]:
    sf = config.get("sf_cli", {})
    exports = config.get("exports", {})
    cmd = [
        cli_path,
        "--headless",
        source_arg,
        source_value,
        "--output-folder",
        output_folder,
        "--export-format",
        sf.get("export_format", "csv"),
        "--timestamped-output",
    ]
    # Auto-use the audit config only if it actually exists (set-up-once, all sites);
    # silently run with SF defaults otherwise — never break the crawl on a missing file.
    cfg_path = sf.get("seospiderconfig")
    if cfg_path and os.path.isfile(cfg_path):
        cmd += ["--config", os.path.abspath(cfg_path)]

    # For Basic-Auth staging environments and form-based logins, SF accepts an
    # authentication profile previously saved from the GUI under
    # Config -> Authentication -> Profiles -> Save. Unlike seospiderconfig,
    # this path is never a set-up-once default with a documented fallback —
    # it is only ever present because a caller explicitly asked for it, so a
    # typo or a deleted profile must stop the crawl rather than start it
    # unauthenticated: a login page can still produce exports that look like
    # a complete, ordinary crawl (#216).
    auth_path = sf.get("auth_config")
    if auth_path:
        if not os.path.isfile(auth_path):
            raise FileNotFoundError(f"sf_cli.auth_config not found: {auth_path!r}")
        cmd += ["--auth-config", os.path.abspath(auth_path)]

    tabs = list(exports.get("tabs", []))
    bulk = list(exports.get("bulk", []))
    if exports.get("fetch_all_inlinks"):
        bulk = ["Links:All Inlinks", *bulk]
    reports = list(exports.get("reports", []))

    if tabs:
        cmd += ["--export-tabs", ",".join(tabs)]
    if bulk:
        cmd += ["--bulk-export", ",".join(bulk)]
    if reports:
        cmd += ["--save-report", ",".join(reports)]
    return cmd


# --- timeout budgeting -------------------------------------------------------
# Screaming Frog writes its exports when the crawl finishes, so a run that is
# cut off produces nothing at all. The timeout is therefore not a safety valve
# but a deadline: set it below what the crawl needs and the entire crawl is
# discarded. That makes a flat default wrong by construction the moment a rate
# limit is set — 3 000 URLs at 1.5 URL/s is 33 minutes of request time alone.
DEFAULT_TIMEOUT_MINUTES = 30

# Requests are the floor, not the cost: startup, rendering and writing the
# exports all sit on top, and a sitemap lists pages while a crawl also fetches
# images, scripts and stylesheets.
TIMEOUT_MARGIN = 2.0
TIMEOUT_STARTUP_MINUTES = 5.0

# How often to say the crawl is still alive. A silent hour is
# indistinguishable from a hung process.
PROGRESS_INTERVAL_SECONDS = 60.0
SF_HELP_TIMEOUT_SECONDS = 30.0


def _query_export_help(cli_path: str, group: str) -> str:
    """Return SF's per-export help without starting a crawl or contacting a target."""
    try:
        process = _run_watched(
            [cli_path, "--help", CLI_HELP_FLAGS[group]],
            SF_HELP_TIMEOUT_SECONDS,
            os.getcwd(),
            lambda _message: None,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(
            f"cannot verify Screaming Frog --help {CLI_HELP_FLAGS[group]} capability: {exc}"
        ) from None
    output = f"{process.stdout or ''}\n{process.stderr or ''}"
    if process.returncode != 0:
        raise RuntimeError(
            f"Screaming Frog --help {CLI_HELP_FLAGS[group]} failed "
            f"(exit {process.returncode}); no crawl was started"
        )
    return output


def _sf_version(cli_path: str, help_output: str) -> str | None:
    match = re.search(r"Screaming Frog SEO Spider\s+(\d+(?:\.\d+)+)", help_output)
    if match:
        return match.group(1)
    # The macOS launcher does not expose --version; its installed app metadata
    # is a read-only version source. Other platforms may report no version.
    path = Path(cli_path)
    if ".app" in path.as_posix():
        info = path.parents[1] / "Info.plist"
        try:
            with info.open("rb") as stream:
                metadata = plistlib.load(stream)
        except (OSError, ValueError, plistlib.InvalidFileException):
            return None
        version = metadata.get("CFBundleShortVersionString") or metadata.get("CFBundleVersion")
        return str(version) if version else None
    return None


def _verify_export_capabilities(cli_path: str, config: dict) -> dict[str, Any]:
    """Fail before a crawl if SF's own help does not support a requested export."""
    output_format = str(config.get("sf_cli", {}).get("export_format", "csv")).lower()
    if output_format not in {"csv", "xls", "xlsx"}:
        raise RuntimeError(
            f"Mode A audit manifests require a local CSV/XLS/XLSX export format; "
            f"{output_format!r} is not read locally. No crawl was started."
        )
    requests = requests_from_config(config.get("exports", {}))
    if not requests:
        return {"state": "no_exports_requested", "requests": []}
    capabilities: dict[str, frozenset[str]] = {}
    help_outputs: dict[str, str] = {}
    for group in dict.fromkeys(request.group for request in requests):
        output = _query_export_help(cli_path, group)
        try:
            capabilities[group] = help_names(output, group)
        except ValueError as exc:
            raise RuntimeError(f"cannot verify Screaming Frog exports: {exc}") from None
        help_outputs[group] = output
    unsupported = unsupported_requests(requests, capabilities)
    if unsupported:
        names = "; ".join(f"{request.group}: {request.name}" for request in unsupported)
        versions = {_sf_version(cli_path, output) for output in help_outputs.values()}
        version = next(iter(versions)) if len(versions) == 1 else None
        label = f"Screaming Frog {version}" if version else "the installed Screaming Frog CLI"
        raise RuntimeError(
            f"{label} does not list requested export(s): {names}. "
            "No crawl or audit analysis was started; choose supported exports or update the SF configuration."
        )
    versions = {_sf_version(cli_path, output) for output in help_outputs.values()}
    version = next(iter(versions)) if len(versions) == 1 else None
    return {
        "state": "help_verified",
        "version": version,
        "fingerprint": help_fingerprint(capabilities),
        "requests": [{"group": request.group, "name": request.name} for request in requests],
    }


def _validate_requested_rate(config: dict, mode: str) -> None:
    """Validate an explicit rate and its base config before launching SF help or crawl."""
    if mode == "load-crawl":
        return
    rate = config.get("sf_cli", {}).get("max_urls_per_second")
    if rate is None:
        return
    if isinstance(rate, bool):
        raise ValueError("sf_cli.max_urls_per_second must be a number, not a boolean")
    from .spiderconfig import find_base_config, patch_speed

    base = find_base_config(config.get("sf_cli", {}).get("seospiderconfig") or None)
    if not base:
        raise RuntimeError(
            "cannot apply a crawl-rate limit because no base Screaming Frog config was found. "
            "Save a config from the GUI (Config → Speed → File → Config → Save As), then "
            "set sf_cli.seospiderconfig or run `seohead sf save-config`. A requested limit "
            "will never fall back to an unthrottled crawl."
        )
    try:
        blob = Path(base).read_bytes()
    except OSError as exc:
        raise RuntimeError(
            f"cannot read the configured Screaming Frog base config: {exc}"
        ) from None
    # Exercise the exact validator/patch on memory only. No output file or SF
    # process is created if the request cannot be applied.
    patch_speed(blob, float(rate))


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _rate_limit_provenance(
    config: dict,
    mode: str,
    derived_path: str | None = None,
    base_path: str | None = None,
) -> dict[str, Any]:
    sf = config.get("sf_cli", {})
    raw_requested = sf.get("max_urls_per_second")
    requested = float(raw_requested) if raw_requested is not None else None
    if mode == "load-crawl":
        return {
            "state": "not_applicable",
            "requested_urls_per_second": requested,
            "effective_urls_per_second": None,
        }

    from .spiderconfig import find_base_config, read_speed

    configured = sf.get("seospiderconfig") or None
    base_path = base_path or find_base_config(configured)
    base_source = (
        "configured"
        if configured and base_path and os.path.abspath(configured) == os.path.abspath(base_path)
        else "latest_saved_sf_config"
    )

    if derived_path:
        try:
            enabled, effective = read_speed(Path(derived_path).read_bytes())
            derived_hash = _sha256_file(derived_path)
            base_hash = _sha256_file(base_path) if base_path else None
        except (OSError, ValueError):
            return {
                "state": "unverified",
                "requested_urls_per_second": requested,
                "effective_urls_per_second": None,
            }
        if not enabled or abs(effective - float(requested)) > 1e-9:
            return {
                "state": "readback_mismatch",
                "requested_urls_per_second": requested,
                "effective_urls_per_second": effective if enabled else None,
                "base_config_sha256": base_hash,
                "derived_config_sha256": derived_hash,
            }
        return {
            "state": "verified",
            "requested_urls_per_second": float(requested),
            "effective_urls_per_second": effective,
            "base_source": base_source,
            "base_config_sha256": base_hash,
            "derived_config_sha256": derived_hash,
        }

    if requested is None and base_path:
        try:
            enabled, effective = read_speed(Path(base_path).read_bytes())
            return {
                "state": "verified_from_base" if enabled else "base_config_unlimited",
                "effective_urls_per_second": effective if enabled else None,
                "base_source": base_source,
                "base_config_sha256": _sha256_file(base_path),
            }
        except (OSError, ValueError):
            return {"state": "base_rate_unverified", "effective_urls_per_second": None}
    return {
        "state": "not_requested_unverified",
        "requested_urls_per_second": None,
        "effective_urls_per_second": None,
    }


def expected_url_count(
    mode: str, source: str, config: dict, log=print, sitemap_counter=None
) -> int | None:
    """How many URLs this run is likely to request, when that is knowable.

    An explicit count wins, a URL list can simply be counted, and for a crawl
    the sitemap is the only cheap estimate available before the crawl itself.
    None means unknown, which is honest: a derived timeout must not rest on a
    number nobody measured.
    """
    explicit = config.get("sf_cli", {}).get("expected_urls")
    if explicit:
        return int(explicit)
    if mode == "crawl-list":
        try:
            with open(source, encoding="utf-8", errors="replace") as handle:
                return sum(1 for line in handle if line.strip())
        except OSError as exc:
            log(f"[runner] cannot count URLs in {source}: {exc}")
            return None
    if mode != "crawl":
        return None
    counter = sitemap_counter or _sitemap_url_count
    try:
        return counter(source)
    except Exception as exc:  # an estimate is never worth failing a run over
        log(f"[runner] sitemap URL count unavailable: {exc}")
        return None


def _sitemap_url_count(start_url: str) -> int | None:
    from seohead.tools.sitemap import crawl as crawl_sitemap

    parts = urlsplit(start_url)
    result = crawl_sitemap(f"{parts.scheme}://{parts.netloc}/sitemap.xml")
    count = result.get("count") if result.get("ok") else None
    return int(count) if count else None


def derive_timeout_minutes(
    configured: float, url_count: int | None, rate: float | None
) -> tuple[float, str]:
    """The timeout to use, and the sentence explaining it.

    When the arithmetic says the crawl cannot finish in the configured window,
    the window is widened rather than the run refused: the alternative is to
    spend an hour crawling a third party's site politely and then throw the
    result away.
    """
    if not rate or not url_count:
        return configured, ""
    needed = (url_count / float(rate)) / 60.0 * TIMEOUT_MARGIN + TIMEOUT_STARTUP_MINUTES
    if needed <= configured:
        return configured, (
            f"{url_count} URLs at {rate}/s need about {needed:.0f} min; "
            f"timeout is {configured:.0f} min"
        )
    return needed, (
        f"{url_count} URLs at {rate}/s need about {needed:.0f} min, but "
        f"sf_cli.timeout_minutes is {configured:.0f}. Raising the timeout to "
        f"{needed:.0f} min: Screaming Frog writes its exports only when the crawl "
        "ends, so stopping early would discard the whole crawl"
    )


# --- process control ---------------------------------------------------------
# The crawler processes this module currently has running, published so a caller
# that can be cancelled -- the MCP request layer -- can stop them through
# ``_terminate_tree`` below.
#
# There is exactly one ``subprocess.Popen`` in this repository and it is in this
# module, which is why registering it here is enough. Reaching the same end by
# replacing ``subprocess.Popen`` process-wide for the duration of a crawl is not:
# ``subprocess.run`` resolves the module global at call time, so every unrelated
# child started anywhere in the process during that window would be collected
# too, and cancelling the crawl would send SIGTERM to its process group.
_live_processes: set[subprocess.Popen] = set()
_live_lock = threading.Lock()


def terminate_live_crawls() -> list[str]:
    """Stop every crawler process this module has running; say what was done to each."""
    with _live_lock:
        procs = list(_live_processes)
    return [_terminate_tree(proc) for proc in procs]


def _terminate_tree(proc: subprocess.Popen) -> str:
    """Stop the crawler and everything it started. Returns what was done.

    The CLI entry point is a launcher that starts a JVM, so killing the direct
    child can leave the crawler running and still requesting a third party's
    site with no parent left to collect the result. The whole process group
    goes, which is why it was given one at launch.
    """
    if proc.poll() is not None:
        return "the crawler had already exited"
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                capture_output=True,
                check=False,
            )
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (OSError, ProcessLookupError):
        pass
    try:
        proc.wait(timeout=10)
        return "the crawler process group was terminated"
    except subprocess.TimeoutExpired:
        pass
    try:
        if os.name != "nt":
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        else:
            proc.kill()
    except (OSError, ProcessLookupError):
        pass
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=10)
    return "the crawler process group was killed"


def _run_watched(
    cmd: list[str], timeout: float, output_folder: str, log
) -> subprocess.CompletedProcess:
    """Run the CLI to completion or to the deadline, reporting that it is alive.

    Raises :class:`subprocess.TimeoutExpired` after stopping the process tree,
    with the outcome of that in ``output`` so the caller can say what happened
    to the crawler rather than leaving the operator to go looking for it.
    """
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        text=True,
        start_new_session=os.name != "nt",
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
    )
    with _live_lock:
        _live_processes.add(proc)
    try:
        return _watch(proc, cmd, timeout, output_folder, log)
    finally:
        with _live_lock:
            _live_processes.discard(proc)


def _watch(
    proc: subprocess.Popen,
    cmd: list[str],
    timeout: float,
    output_folder: str,
    log,
) -> subprocess.CompletedProcess:
    started = time.monotonic()
    next_report = started + PROGRESS_INTERVAL_SECONDS
    while True:
        remaining = timeout - (time.monotonic() - started)
        if remaining <= 0:
            outcome = _terminate_tree(proc)
            raise subprocess.TimeoutExpired(cmd, timeout, output=outcome)
        try:
            stdout, stderr = proc.communicate(timeout=min(remaining, PROGRESS_INTERVAL_SECONDS))
        except subprocess.TimeoutExpired:
            now = time.monotonic()
            if now >= next_report:
                next_report = now + PROGRESS_INTERVAL_SECONDS
                log(
                    f"[runner] still crawling: {(now - started) / 60:.0f} min elapsed, "
                    f"{_output_size(output_folder)} in {output_folder}"
                )
            continue
        return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)


def _output_size(folder: str) -> str:
    """A coarse sign of life: how much SF has written so far."""
    total = 0
    for root, _dirs, names in os.walk(folder):
        for name in names:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                continue
    return f"{total / 1024:.0f} KB written"


def _apply_rate_limit(
    config: dict, output_folder: str, log, *, mode: str = "crawl", run_info=None
) -> dict:
    """Build and inject a rate-limited .seospiderconfig when requested.

    The SF CLI has no speed flag, so the limit can only live in its config; see
    :mod:`spiderconfig` for the serialization details. If a safe config cannot
    be built, fail with an explanation instead of crawling a third-party site at
    full speed after the caller explicitly requested throttling.
    """
    sf = config.get("sf_cli", {})
    rate = sf.get("max_urls_per_second")
    if mode == "load-crawl":
        if run_info is not None:
            run_info["rate_limit"] = _rate_limit_provenance(config, mode)
        return config
    if rate is None:
        if run_info is not None:
            run_info["rate_limit"] = _rate_limit_provenance(config, mode)
        return config
    from .spiderconfig import build_throttled_config

    base = sf.get("seospiderconfig") or None
    from .spiderconfig import find_base_config

    base_path = find_base_config(base)
    dest = os.path.join(output_folder, "throttled.seospiderconfig")
    path = build_throttled_config(dest, urls_per_second=float(rate), base=base_path)
    log(f"[runner] crawl rate limited to {rate} URLs/s via config {path}")
    if run_info is not None:
        run_info["rate_limit"] = _rate_limit_provenance(
            config, mode, derived_path=path, base_path=base_path
        )
    return deep_merge(config, {"sf_cli": {"seospiderconfig": path}})


def run_sf(
    *,
    mode: str,
    source: str,
    output_folder: str,
    config: dict,
    cli_override: str | None = None,
    log=print,
    run_info: dict[str, Any] | None = None,
) -> str:
    """Run SF headless and return the folder containing the fresh exports.

    ``mode`` is one of ``crawl`` (url), ``crawl-list`` (file), ``load-crawl``
    (.seospider). SF writes a timestamped subfolder; we return it.
    """
    if mode == "crawl":
        trusted_proxy = config.get("sf_cli", {}).get("_trusted_loopback_proxy")
        source_parts = urlsplit(source)
        proxy_parts = urlsplit(trusted_proxy or "")
        is_trusted_loopback = (
            bool(trusted_proxy)
            and source_parts.scheme == proxy_parts.scheme == "http"
            and source_parts.hostname == proxy_parts.hostname == "127.0.0.1"
            and source_parts.port == proxy_parts.port
        )
        if not is_trusted_loopback:
            validate_url(source)
    cli = resolve_cli(config, cli_override)
    _validate_requested_rate(config, mode)
    capability = _verify_export_capabilities(cli, config)
    if run_info is not None:
        run_info["sf_capability"] = capability
    arg = {"crawl": "--crawl", "crawl-list": "--crawl-list", "load-crawl": "--load-crawl"}[mode]
    # SF starts in its own working directory and does not resolve a relative
    # --output-folder as expected. It emits "FATAL - Directory does not exist"
    # yet still exits with status 0, causing export loading to fail later.
    output_folder = os.path.abspath(output_folder)
    os.makedirs(output_folder, exist_ok=True)
    # Snapshot before anything runs so a reused --out cannot let a prior run's
    # export folder stand in for this one (#215): the folder this run
    # produced is judged by what appeared after this point, never by what was
    # already sitting there.
    before = _dir_entries(output_folder)
    config = _apply_rate_limit(config, output_folder, log, mode=mode, run_info=run_info)
    cmd = build_command(
        cli, source_arg=arg, source_value=source, output_folder=output_folder, config=config
    )
    log(f"[runner] {' '.join(cmd)}")

    sf = config.get("sf_cli", {})
    rate = sf.get("max_urls_per_second")
    url_count = (
        expected_url_count(mode, source, config, log)
        if rate is not None and mode != "load-crawl"
        else None
    )
    if mode == "load-crawl":
        rate = None
    configured = float(sf.get("timeout_minutes", DEFAULT_TIMEOUT_MINUTES))
    minutes, note = derive_timeout_minutes(configured, url_count, rate)
    if note:
        log(f"[runner] {note}")
    timeout = minutes * 60

    try:
        proc = _run_watched(cmd, timeout, output_folder, log)
    except subprocess.TimeoutExpired as err:
        budget = f"{url_count} URLs at {rate}/s" if url_count and rate else "the crawl"
        raise RuntimeError(
            f"Screaming Frog CLI timed out after {minutes:.0f} min ({budget}); "
            f"{err.output or 'the crawler was stopped'}. Screaming Frog writes its "
            "exports only when a crawl ends, so nothing from this run was kept. "
            "Raise sf_cli.timeout_minutes, raise sf_cli.max_urls_per_second, or "
            "narrow the crawl, then run it again."
        ) from err
    if proc.returncode != 0:
        raise RuntimeError(
            f"Screaming Frog CLI failed (exit {proc.returncode}).\n{(proc.stderr or '')[-4000:]}"
        )
    exports_dir = _new_export_dir(output_folder, before)
    if exports_dir is None or not _has_exports(exports_dir):
        # SF may return status 0 even after a startup failure, such as a FATAL
        # output-directory error, or after reusing an --out where nothing new
        # got written at all. Without this guard, callers see "Required export
        # 'internal_all' not found" (or worse, a stale prior run's export) and
        # investigate exports instead of the failed process launch.
        tail = ((proc.stdout or "") + (proc.stderr or "")).strip()[-2000:]
        raise RuntimeError(
            f"Screaming Frog CLI exited 0 but wrote no exports to {output_folder} — "
            "the crawl produced nothing, SF never started, or it reused an existing "
            "--out without writing anything new (check the output below).\n"
            f"SF output (tail):\n{tail or '(empty)'}"
        )
    exports_config = config.get("exports", {})
    derive_sitemap_status = is_full_manifest(exports_config)
    manifest = validate_export_files(
        exports_dir,
        requests_from_config(exports_config),
        derive_sitemap_status=derive_sitemap_status,
    )
    if run_info is not None:
        profile = config.get("profile", "full")
        run_info["export_manifest"] = {
            "format": "seohead.sf-export-manifest.v1",
            "requested_profile": profile,
            "resolved_profile": resolved_profile(profile, config.get("exports", {})),
            "uses_full_manifest": derive_sitemap_status,
            "state": "files_present",
            **manifest,
        }
    return exports_dir


def _has_exports(folder: str) -> bool:
    """Return whether the folder contains any SF export from a completed crawl."""
    try:
        names = os.listdir(folder)
    except OSError:
        return False
    return any(n.lower().endswith((".csv", ".xlsx", ".xls", ".gsheet")) for n in names)


def _dir_entries(folder: str) -> tuple[set[str], set[str]]:
    """Split a folder's direct entries into (subdirectory names, file names)."""
    dirs: set[str] = set()
    files: set[str] = set()
    for name in os.listdir(folder):
        (dirs if os.path.isdir(os.path.join(folder, name)) else files).add(name)
    return dirs, files


def _new_export_dir(output_folder: str, before: tuple[set[str], set[str]]) -> str | None:
    """The folder this run wrote exports to, or None if nothing new appeared.

    SF writes a fresh timestamped subfolder on a real run; comparing today's
    listing against the ``before`` snapshot — rather than picking "whatever
    subfolder is newest" — is what keeps an already-present, older export
    folder from being mistaken for this run's output (#215).
    """
    before_dirs, before_files = before
    dirs, files = _dir_entries(output_folder)
    new_dirs = dirs - before_dirs
    if new_dirs:
        return max((os.path.join(output_folder, d) for d in new_dirs), key=os.path.getmtime)
    if files - before_files:
        return output_folder
    return None
