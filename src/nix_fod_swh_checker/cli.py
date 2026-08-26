"""Command line interface for nix-fod-swh-checker."""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.parse
from dataclasses import asdict
from pathlib import Path

import requests

from .cache import Cache, default_cache_path
from .checker import check_fod
from .checkpoint import default_checkpoint_path, load_checkpoint, save_checkpoint
from .models import FixedOutputDerivation, SWHCheckResult, SWHLookupMethod
from .nix import (
    NixCommandError,
    build_nix_file,
    dry_run_nix_file,
    iter_fixed_output_derivations,
    show_derivations_recursive,
)
from .swh import DEFAULT_API_URL, SWHClient, SWHError
from .swh_fod import vault_swhids_for_results, write_swh_fods_nix

_STATUS_LABELS = {True: "KNOWN", False: "UNKNOWN", None: "UNDETERMINED"}


def _status_label(result: SWHCheckResult) -> str:
    if result.known is True and result.method == SWHLookupMethod.KNOWN_AFTER_DISARCHIVE:
        return "KNOWN AFTER DISARCHIVE"
    return _STATUS_LABELS[result.known]


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nix-fod-swh-check",
        description=(
            "List every fixed-output derivation (FOD) reachable from a Nix "
            "attribute and check whether its source is already archived on "
            "Software Heritage."
        ),
    )
    subparsers = parser.add_subparsers(
        dest="command", help="available commands", required=True
    )

    check_parser = subparsers.add_parser(
        "check",
        help="check FODs against Software Heritage",
    )
    check_parser.add_argument(
        "installable",
        help="the Nix installable to inspect, e.g. 'nixpkgs#hello' or a store path",
    )

    archive_parser = subparsers.add_parser(
        "request-archiving",
        help="request the archiving of unknown FOD origins on Software Heritage",
    )
    archive_parser.add_argument(
        "installable",
        nargs="?",
        default=None,
        help="the Nix installable that was previously checked",
    )
    archive_parser.add_argument(
        "-i",
        "--json-input",
        default=None,
        help="path to a JSON file containing check results (as produced by 'check -o')",
    )
    archive_parser.add_argument(
        "--checkpoint-file",
        default=None,
        help="checkpoint to read results from (default: a per-installable file under $XDG_CACHE_HOME/nix-fod-swh-checker/)",
    )
    archive_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="list the origins that would be requested without contacting Software Heritage",
    )
    archive_parser.add_argument(
        "--swh-api-url",
        default=DEFAULT_API_URL,
        help="base URL of the Software Heritage API (default: %(default)s)",
    )
    archive_parser.add_argument(
        "--swh-api-token",
        default=None,
        help="bearer token for the Software Heritage API (or set SWH_API_TOKEN)",
    )
    archive_parser.add_argument(
        "--min-delay",
        type=float,
        default=1.0,
        help="minimum delay in seconds between Software Heritage API requests when unauthenticated (default: %(default)s)",
    )
    archive_parser.add_argument(
        "--quiet",
        "-q",
        action="store_true",
        help="do not print progress messages to stderr while requesting archiving",
    )

    generate_parser = subparsers.add_parser(
        "generate-swh-fods",
        help="generate a Nix expression with SWH-backed FODs from a checkpoint",
    )
    generate_parser.add_argument(
        "installable",
        nargs="?",
        default=None,
        help="the Nix installable that was previously checked",
    )
    generate_parser.add_argument(
        "-o",
        "--output",
        default="swh-backed-fods.nix",
        help="path to write the generated Nix expression (default: %(default)s)",
    )
    generate_parser.add_argument(
        "--checkpoint-file",
        default=None,
        help="checkpoint to read results from (default: a per-installable file under $XDG_CACHE_HOME/nix-fod-swh-checker/)",
    )
    generate_parser.add_argument(
        "--json-input",
        "-i",
        default=None,
        help="path to a JSON file containing check results (as produced by 'check -o')",
    )
    generate_parser.add_argument(
        "--quiet",
        "-q",
        action="store_true",
        help="do not print progress messages to stderr while generating",
    )

    cook_parser = subparsers.add_parser(
        "cook-swh-fods",
        help="request cooking of Software Heritage vault flat archives for known FODs",
    )
    cook_parser.add_argument(
        "input",
        help="a Nix installable previously checked, or a path to a generated swh-backed-fods.nix file",
    )
    cook_parser.add_argument(
        "--checkpoint-file",
        default=None,
        help="checkpoint to read results from (default: a per-installable file under $XDG_CACHE_HOME/nix-fod-swh-checker/)",
    )
    cook_parser.add_argument(
        "--swh-api-url",
        default=DEFAULT_API_URL,
        help="base URL of the Software Heritage API (default: %(default)s)",
    )
    cook_parser.add_argument(
        "--swh-api-token",
        default=None,
        help="bearer token for the Software Heritage API (or set SWH_API_TOKEN)",
    )
    cook_parser.add_argument(
        "--min-delay",
        type=float,
        default=1.0,
        help="minimum delay in seconds between Software Heritage API requests when unauthenticated (default: %(default)s)",
    )
    cook_parser.add_argument(
        "--quiet",
        "-q",
        action="store_true",
        help="do not print progress messages to stderr while cooking",
    )

    build_parser = subparsers.add_parser(
        "build-swh-fods",
        help="generate SWH-backed FODs and build them",
    )
    build_parser.add_argument(
        "input",
        help="a Nix installable previously checked, or a path to a generated swh-backed-fods.nix file",
    )
    build_parser.add_argument(
        "-o",
        "--output",
        default=None,
        help="path to write the generated Nix expression when <input> is an installable (default: swh-backed-fods.nix)",
    )
    build_parser.add_argument(
        "--checkpoint-file",
        default=None,
        help="checkpoint to read results from (default: a per-installable file under $XDG_CACHE_HOME/nix-fod-swh-checker/)",
    )
    build_parser.add_argument(
        "--nix-build-arg",
        action="append",
        default=[],
        help="extra argument to pass to `nix build` (can be given multiple times)",
    )
    build_parser.add_argument(
        "--no-substitute",
        action="store_true",
        help="do not use substituters when building",
    )
    build_parser.add_argument(
        "--swh-api-url",
        default=DEFAULT_API_URL,
        help="base URL of the Software Heritage API (default: %(default)s)",
    )
    build_parser.add_argument(
        "--swh-api-token",
        default=None,
        help="bearer token for the Software Heritage API (or set SWH_API_TOKEN)",
    )
    build_parser.add_argument(
        "--min-delay",
        type=float,
        default=1.0,
        help="minimum delay in seconds between Software Heritage API requests when unauthenticated (default: %(default)s)",
    )
    build_parser.add_argument(
        "--quiet",
        "-q",
        action="store_true",
        help="do not print progress messages to stderr while building",
    )
    check_parser.add_argument(
        "--swh-api-url",
        default=DEFAULT_API_URL,
        help="base URL of the Software Heritage API (default: %(default)s)",
    )
    check_parser.add_argument(
        "--swh-api-token",
        default=None,
        help="bearer token for the Software Heritage API (or set SWH_API_TOKEN)",
    )
    check_parser.add_argument(
        "--min-delay",
        type=float,
        default=1.0,
        help="minimum delay in seconds between Software Heritage API requests when unauthenticated (default: %(default)s)",
    )
    check_parser.add_argument(
        "--swh-identify-timeout",
        type=float,
        default=30.0,
        help="timeout in seconds for the 'swh identify' command on each realised FOD (default: %(default)s)",
    )
    check_parser.add_argument(
        "--disarchive-timeout",
        type=float,
        default=30.0,
        help="timeout in seconds for the 'disarchive disassemble' command on each archive (default: %(default)s)",
    )
    check_parser.add_argument(
        "--disarchive-db-url",
        type=str,
        default="https://disarchive.guix.gnu.org",
        help="base URL of the GNU Guix disarchive database used to cache archive metadata (default: %(default)s)",
    )
    check_parser.add_argument(
        "--skip-disarchive",
        action="store_true",
        help="do not query the GNU Guix disarchive database; always realise archives locally",
    )
    check_parser.add_argument(
        "-o",
        "--output",
        default=None,
        help="path to write machine-readable JSON results instead of printing a human-readable report",
    )
    check_parser.add_argument(
        "--only-unknown",
        action="store_true",
        help="only report FODs that are not known to Software Heritage (or undetermined)",
    )
    check_parser.add_argument(
        "--retry-unknown",
        action="store_true",
        help="re-check FODs that were previously reported as unknown",
    )
    check_parser.add_argument(
        "--retry-undetermined",
        action="store_true",
        help="re-check FODs that were previously reported as undetermined",
    )
    check_parser.add_argument(
        "--quiet",
        "-q",
        action="store_true",
        help="do not print progress messages to stderr or the human-readable report while checking FODs",
    )
    check_parser.add_argument(
        "--checkpoint-file",
        default=None,
        help=(
            "path to a JSON file used to save results as FODs are checked, so an "
            "interrupted run can resume without re-checking them (default: a "
            "per-installable file under $XDG_CACHE_HOME/nix-fod-swh-checker/)"
        ),
    )
    check_parser.add_argument(
        "--no-checkpoint",
        action="store_true",
        help="do not read or write a checkpoint file",
    )
    check_parser.add_argument(
        "--cache-file",
        default=None,
        help=(
            "path to the shared cache file used to avoid repeated API requests "
            "and tool runs (default: a file under $XDG_CACHE_HOME/nix-fod-swh-checker/)"
        ),
    )
    check_parser.add_argument(
        "--no-cache",
        action="store_true",
        help="do not read or write the shared cache",
    )
    return parser


def _exit_usage(parser: argparse.ArgumentParser, message: str) -> None:
    """Print a usage error and exit with code 2."""
    parser.print_usage(sys.stderr)
    print(f"{parser.prog}: error: {message}", file=sys.stderr)
    sys.exit(2)


def _validate_args(
    args: argparse.Namespace, parser: argparse.ArgumentParser
) -> argparse.Namespace:
    """Reject incompatible command-line argument combinations.

    Some options are mutually exclusive or meaningless in combination; this
    function turns the silent misbehaviour described in issue #28 into clear
    error messages.
    """
    if args.command == "check":
        if args.no_checkpoint and args.checkpoint_file:
            _exit_usage(
                parser,
                "--no-checkpoint and --checkpoint-file are mutually exclusive",
            )
        if args.no_cache and args.cache_file:
            _exit_usage(
                parser,
                "--no-cache and --cache-file are mutually exclusive",
            )
        if args.no_checkpoint:
            retry_flags = [
                flag
                for given, flag in (
                    (args.retry_unknown, "--retry-unknown"),
                    (args.retry_undetermined, "--retry-undetermined"),
                )
                if given
            ]
            if retry_flags:
                joined = " and ".join(retry_flags)
                _exit_usage(parser, f"{joined} require a checkpoint")

    if args.command in ("request-archiving", "generate-swh-fods"):
        if args.json_input and args.checkpoint_file:
            _exit_usage(
                parser,
                "--json-input and --checkpoint-file are mutually exclusive",
            )
        if args.json_input and args.installable:
            _exit_usage(
                parser,
                "<installable> cannot be combined with -i/--json-input",
            )
        if args.command == "generate-swh-fods" and not args.json_input and not args.installable:
            _exit_usage(
                parser,
                "either an installable or -i/--json-input is required",
            )

    if args.command in ("cook-swh-fods", "build-swh-fods"):
        if _is_nix_file(args.input) and args.checkpoint_file:
            _exit_usage(
                parser,
                "--checkpoint-file cannot be used when <input> is a .nix file",
            )

    if args.command == "build-swh-fods":
        if _is_nix_file(args.input) and args.output:
            _exit_usage(
                parser,
                "-o/--output cannot be used when <input> is a .nix file",
            )

    return args


def _result_to_dict(result: SWHCheckResult) -> dict:
    fod_dict = asdict(result.fod)
    fod_dict["label"] = result.fod.label
    return {
        "fod": fod_dict,
        "known": result.known,
        "method": result.method.value,
        "detail": result.detail,
        "swhid": result.swhid,
        "swh_url": result.swh_url,
        "disarchive_spec": result.disarchive_spec,
        "disarchive_swhid": result.disarchive_swhid,
        "disarchive_top_dir": result.disarchive_top_dir,
        "origin_urls": result.origin_urls,
    }


def _log_to_stderr(msg: str) -> None:
    """Print a log message to stderr.

    Messages that start with a carriage return are progress updates: they
    overwrite the current terminal line instead of starting a new one, matching
    the behaviour of Nix's interactive progress UI.
    """
    if msg.startswith("\r"):
        sys.stderr.write(msg[1:] + "\r")
    else:
        sys.stderr.write(msg + "\n")
    sys.stderr.flush()


def _print_report(results: list[SWHCheckResult]) -> None:
    if not results:
        print("nothing to report")
        return
    for result in results:
        print(f"[{_status_label(result)}] {result.fod.label}")
        print(f"    method={result.method.value}: {result.detail}")
        if result.swh_url:
            print(f"    {result.swh_url}")

    known = sum(
        1
        for r in results
        if r.known is True and r.method != SWHLookupMethod.KNOWN_AFTER_DISARCHIVE
    )
    known_after_disarchive = sum(
        1
        for r in results
        if r.known is True and r.method == SWHLookupMethod.KNOWN_AFTER_DISARCHIVE
    )
    unknown = sum(1 for r in results if r.known is False)
    undetermined = sum(1 for r in results if r.known is None)
    print(
        f"\n{len(results)} FOD(s) checked: "
        f"{known} known, {known_after_disarchive} known after disarchive, "
        f"{unknown} unknown, {undetermined} undetermined"
    )


def _run_check_command(args: argparse.Namespace) -> int:
    api_token = args.swh_api_token or os.environ.get("SWH_API_TOKEN")
    on_log = None if args.quiet else _log_to_stderr

    if not api_token and on_log:
        on_log(
            "warning: no Software Heritage API token provided; "
            "anonymous requests are heavily rate-limited. "
            "Use --swh-api-token or set SWH_API_TOKEN."
        )

    checkpoint_path: Path | None = None
    checked: dict[str, SWHCheckResult] = {}
    cache: Cache | None = None

    try:
        try:
            derivations = show_derivations_recursive(args.installable, on_log=on_log)
        except NixCommandError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

        fods = list(iter_fixed_output_derivations(derivations))
        if not fods:
            print("no fixed-output derivations found", file=sys.stderr)
            return 0

        if on_log:
            on_log(
                f"found {len(fods)} fixed-output derivation(s) to check against Software Heritage"
            )

        if not args.no_checkpoint:
            checkpoint_path = (
                Path(args.checkpoint_file)
                if args.checkpoint_file
                else default_checkpoint_path(args.installable)
            )
            checked = load_checkpoint(checkpoint_path)
            if on_log:
                if checked:
                    on_log(
                        f"resuming from checkpoint {checkpoint_path} "
                        f"({len(checked)} FOD(s) already checked)"
                    )
                else:
                    on_log(f"saving progress to checkpoint {checkpoint_path}")

        if not args.no_cache:
            cache_path = (
                Path(args.cache_file)
                if args.cache_file
                else default_cache_path()
            )
            cache = Cache(
                cache_path,
                ignore_misses=args.retry_unknown or args.retry_undetermined,
            )
            if on_log:
                on_log(f"using shared cache {cache_path}")

        with SWHClient(
            api_url=args.swh_api_url,
            api_token=api_token,
            min_delay=args.min_delay,
            on_log=on_log,
            cache=cache,
        ) as client:
            total = len(fods)
            for index, fod in enumerate(fods, start=1):
                previous = checked.get(fod.label)
                if previous is not None:
                    should_retry = (
                        (args.retry_unknown and previous.known is False)
                        or (args.retry_undetermined and previous.known is None)
                    )
                    if not should_retry:
                        if on_log:
                            on_log(
                                f"[{index}/{total}] {fod.label}: already checked "
                                f"({_STATUS_LABELS[previous.known]}), skipping"
                            )
                        continue
                    if on_log:
                        on_log(
                            f"[{index}/{total}] {fod.label}: already checked "
                            f"({_STATUS_LABELS[previous.known]}), retrying"
                        )
                elif on_log:
                    on_log(f"[{index}/{total}] checking {fod.label}")
                try:
                    result = check_fod(
                        fod,
                        client,
                        on_log=on_log,
                        swh_identify_timeout=args.swh_identify_timeout,
                        disarchive_timeout=args.disarchive_timeout,
                        disarchive_db_url=args.disarchive_db_url,
                        skip_disarchive=args.skip_disarchive,
                        cache=cache,
                    )
                except SWHError as exc:
                    print(f"warning: {exc}", file=sys.stderr)
                    continue
                if on_log:
                    on_log(f"[{index}/{total}] {fod.label}: {_STATUS_LABELS[result.known]}")
                checked[fod.label] = result
                if checkpoint_path is not None:
                    save_checkpoint(checkpoint_path, args.installable, checked)

        results = [checked[fod.label] for fod in fods if fod.label in checked]

        if args.only_unknown:
            results = [r for r in results if r.known is not True]

        if args.output:
            Path(args.output).write_text(
                json.dumps([_result_to_dict(r) for r in results], indent=2) + "\n"
            )

        if not args.quiet:
            _print_report(results)

        if cache is not None:
            cache.save()

        return 0
    except KeyboardInterrupt:
        return _handle_interrupt(checked, checkpoint_path, cache)


def _run_generate_command(args: argparse.Namespace) -> int:
    if args.json_input:
        try:
            results = _load_results_from_json(Path(args.json_input))
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            print(f"error: could not read JSON results from {args.json_input}: {exc}", file=sys.stderr)
            return 1
        if not results:
            print(
                f"error: no check results found in {args.json_input}; "
                "run 'nix-fod-swh-check check <installable> -o <file>' first",
                file=sys.stderr,
            )
            return 1
    else:
        checkpoint_path = (
            Path(args.checkpoint_file)
            if args.checkpoint_file
            else default_checkpoint_path(args.installable)
        )
        checked = load_checkpoint(checkpoint_path)
        if not checked:
            print(
                f"error: no checkpoint found at {checkpoint_path}; "
                "run 'nix-fod-swh-check check <installable>' first",
                file=sys.stderr,
            )
            return 1
        results = list(checked.values())

    on_log = None if args.quiet else _log_to_stderr
    expressions = write_swh_fods_nix(args.output, results, on_log=on_log)

    print(
        f"wrote {len(expressions)} SWH-backed FOD expression(s) to {args.output}",
        file=sys.stderr,
    )
    return 0


def _load_results_from_json(path: Path) -> list[SWHCheckResult]:
    """Load check results from the JSON format written by `check -o`."""
    raw_list = json.loads(path.read_text())
    if not isinstance(raw_list, list):
        raise ValueError("top-level value is not a list")
    results: list[SWHCheckResult] = []
    for raw in raw_list:
        if not isinstance(raw, dict):
            continue
        try:
            fod_data = dict(raw["fod"])
            # ``label`` is a computed property on ``FixedOutputDerivation`` and
            # is included in JSON output for convenience; drop it here so the
            # dataclass constructor does not complain about an unexpected field.
            fod_data.pop("label", None)
            fod = FixedOutputDerivation(**fod_data)
            results.append(
                SWHCheckResult(
                    fod=fod,
                    known=raw["known"],
                    method=SWHLookupMethod(raw["method"]),
                    detail=raw["detail"],
                    swhid=raw.get("swhid"),
                    swh_url=raw.get("swh_url"),
                    disarchive_spec=raw.get("disarchive_spec"),
                    disarchive_swhid=raw.get("disarchive_swhid"),
                    disarchive_top_dir=raw.get("disarchive_top_dir"),
                    origin_urls=raw.get("origin_urls") or [],
                )
            )
        except KeyError as exc:
            raise ValueError(f"missing required field {exc.args[0]!r}") from exc
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid result entry: {exc}") from exc
    return results


def _is_nix_file(path: str) -> bool:
    return path.endswith(".nix") and Path(path).is_file()


def _load_results_for_build(args: argparse.Namespace) -> list[SWHCheckResult] | None:
    if _is_nix_file(args.input):
        return []

    checkpoint_path = (
        Path(args.checkpoint_file)
        if args.checkpoint_file
        else default_checkpoint_path(args.input)
    )
    checked = load_checkpoint(checkpoint_path)
    if not checked:
        print(
            f"error: no checkpoint found at {checkpoint_path}; "
            "run 'nix-fod-swh-check check <installable>' first, "
            "or pass a generated swh-backed-fods.nix file",
            file=sys.stderr,
        )
        return None
    return list(checked.values())


def _swh_client_from_args(args: argparse.Namespace, on_log) -> SWHClient:
    api_token = args.swh_api_token or os.environ.get("SWH_API_TOKEN")
    if not api_token and on_log:
        on_log(
            "warning: no Software Heritage API token provided; "
            "anonymous requests are heavily rate-limited. "
            "Use --swh-api-token or set SWH_API_TOKEN."
        )
    return SWHClient(
        api_url=args.swh_api_url,
        api_token=api_token,
        min_delay=args.min_delay,
        on_log=on_log,
    )


# Archive-like extensions that SWH's ``tarball`` visit type can handle.
# The path component of the URL (query strings and fragments ignored) must
# end with one of these suffixes for the origin to be requested as a tarball.
_TARBALL_EXTENSIONS = frozenset(
    {
        ".tar",
        ".tar.gz",
        ".tar.bz2",
        ".tar.xz",
        ".tar.lz",
        ".tar.zst",
        ".tar.lzma",
        ".tgz",
        ".tbz",
        ".tbz2",
        ".txz",
        ".tlz",
        ".tzst",
        ".zip",
        ".jar",
        ".war",
        ".ear",
        ".7z",
        ".rar",
    }
)


def _looks_like_tarball_url(url: str) -> bool:
    """Return True when ``url`` points at an archive-like file.

    Query strings and fragments are ignored, and the comparison is
    case-insensitive so that extensions such as ``.TAR.GZ`` are accepted.
    """
    path = urllib.parse.urlparse(url).path
    lower_path = path.lower()
    return any(lower_path.endswith(ext) for ext in _TARBALL_EXTENSIONS)


def _visit_type_for_result(result: SWHCheckResult, url: str) -> str | None:
    """Infer the Software Heritage visit type for a FOD's origin URL.

    ``git``-method FODs are assumed to come from a version-control origin.
    For other methods the URL path is inspected: archive-like URLs are
    requested as ``tarball``, while plain file URLs (e.g. ``.patch`` or
    ``.mk`` files) are skipped because SWH has no ``file`` visit type.
    """
    if result.fod.method == "git":
        return "git"
    if _looks_like_tarball_url(url):
        return "tarball"
    return None


def _first_live_url(
    urls: list[str],
    *,
    timeout: float = 10.0,
    on_log: Callable[[str], None] | None = None,
) -> str | None:
    """Return the first reachable URL, or ``None`` if none respond.

    A URL is considered reachable when a ``HEAD`` request succeeds with a
    status code below ``400`` (including redirects). ``405 Method Not
    Allowed`` is also accepted, because some servers reject ``HEAD`` even
    though the origin exists.
    """
    for url in urls:
        try:
            response = requests.head(url, timeout=timeout, allow_redirects=True)
        except requests.RequestException as exc:
            if on_log:
                on_log(f"{url}: unreachable ({exc})")
            continue
        if response.status_code == 405 or response.status_code < 400:
            return url
        if on_log:
            on_log(f"{url}: unreachable (HTTP {response.status_code})")
    return None


def _extract_vault_swhids_from_nix_file(path: str) -> set[str]:
    """Parse a generated swh-backed-fods.nix file for directory SWHIDs.

    The generated expressions use URLs of the form
    ``https://archive.softwareheritage.org/api/1/vault/flat/{swhid}/raw``.
    This function extracts every ``swh:1:dir:...`` identifier from those URLs.
    """
    return set(_extract_vault_swhids_by_attr(path).values())


def _extract_vault_swhids_by_attr(path: str) -> dict[str, str]:
    """Parse a generated swh-backed-fods.nix file for directory SWHIDs per attribute.

    Returns a mapping from attribute name to the ``swh:1:dir:...`` identifier
    used in that attribute's vault flat URL, if any.
    """
    text = Path(path).read_text()
    swhids: dict[str, str] = {}
    current_attr: str | None = None
    for line in text.splitlines():
        attr_match = re.match(r'^\s*"([^"]+)"\s*=', line)
        if attr_match:
            current_attr = attr_match.group(1)
        if current_attr is None or "/vault/flat/" not in line:
            continue
        start = line.find("/vault/flat/")
        rest = line[start + len("/vault/flat/") :]
        end = rest.find("/raw")
        if end == -1:
            continue
        candidate = rest[:end]
        if candidate.startswith("swh:1:dir:"):
            swhids[current_attr] = candidate
    return swhids


def _eval_nix_file_outputs(path: str) -> dict[str, str]:
    """Evaluate a Nix file and return a mapping of attribute names to output paths.

    The file is expected to evaluate to a function returning an attribute set
    of derivations, as produced by ``write_swh_fods_nix``.
    """
    cmd = ["nix", "eval", "--json", "-f", path, "--apply", "f: f {}"]
    try:
        proc = subprocess.run(cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        raise NixCommandError(
            f"'{' '.join(cmd)}' failed with exit code {exc.returncode}: {exc.stderr.strip()}"
        ) from exc
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise NixCommandError(f"could not parse JSON output of '{' '.join(cmd)}'") from exc


def _list_attrs_in_nix_file(path: str) -> list[str]:
    """Return the attribute names defined in a Nix file."""
    return list(_eval_nix_file_outputs(path).keys())


def _run_request_archiving_command(args: argparse.Namespace) -> int:
    if args.json_input:
        try:
            results = _load_results_from_json(Path(args.json_input))
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            print(f"error: could not read JSON results from {args.json_input}: {exc}", file=sys.stderr)
            return 1
        if not results:
            print(
                f"error: no check results found in {args.json_input}; "
                "run 'nix-fod-swh-check check <installable> -o <file>' first",
                file=sys.stderr,
            )
            return 1
    elif args.installable:
        checkpoint_path = (
            Path(args.checkpoint_file)
            if args.checkpoint_file
            else default_checkpoint_path(args.installable)
        )
        checked = load_checkpoint(checkpoint_path)
        if not checked:
            print(
                f"error: no checkpoint found at {checkpoint_path}; "
                "run 'nix-fod-swh-check check <installable>' first",
                file=sys.stderr,
            )
            return 1
        results = list(checked.values())
    else:
        print(
            "error: either an installable or -i/--json-input is required",
            file=sys.stderr,
        )
        return 2

    on_log = None if args.quiet else _log_to_stderr

    # Collect (visit_type, url) pairs for FODs that are unknown to SWH.
    # Only strictly UNKNOWN results are requested for archiving; UNDETERMINED
    # results (e.g. failed realisations or lookups) are skipped because there
    # is no confirmation that SWH actually lacks the source.
    origins: dict[tuple[str, str], list[SWHCheckResult]] = {}
    for result in results:
        if result.known is not False:
            continue
        if not result.origin_urls:
            print(
                f"warning: skipping {result.fod.label}: no origin URLs",
                file=sys.stderr,
            )
            continue
        if args.dry_run:
            # In dry-run mode we do not probe URLs, we just list candidates.
            for url in result.origin_urls:
                visit_type = _visit_type_for_result(result, url)
                if visit_type is None:
                    print(
                        f"warning: skipping {result.fod.label}: "
                        f"file URL not supported for archiving: {url}",
                        file=sys.stderr,
                    )
                    continue
                origins.setdefault((visit_type, url), []).append(result)
            continue
        url = _first_live_url(result.origin_urls, on_log=on_log)
        if url is None:
            print(
                f"warning: skipping {result.fod.label}: no reachable origin URL",
                file=sys.stderr,
            )
            continue
        visit_type = _visit_type_for_result(result, url)
        if visit_type is None:
            print(
                f"warning: skipping {result.fod.label}: "
                f"file URL not supported for archiving: {url}",
                file=sys.stderr,
            )
            continue
        origins.setdefault((visit_type, url), []).append(result)

    if not origins:
        print(
            "no origins to request archiving for",
            file=sys.stderr,
        )
        return 0

    if on_log:
        on_log(
            f"requesting archiving of {len(origins)} origin(s) on Software Heritage..."
        )

    if args.dry_run:
        for (visit_type, url), associated in sorted(origins.items()):
            labels = ", ".join(sorted({r.fod.label for r in associated}))
            print(f"{url} ({visit_type}) [{labels}]")
        return 0

    try:
        with _swh_client_from_args(args, on_log) as client:
            for (visit_type, url), associated in sorted(origins.items()):
                try:
                    request = client.request_origin_save(url, visit_type=visit_type)
                    status = f"{request.save_request_status} / {request.save_task_status}"
                except SWHError as exc:
                    print(f"warning: {exc}", file=sys.stderr)
                    continue
                if on_log:
                    labels = ", ".join(sorted({r.fod.label for r in associated}))
                    on_log(
                        f"{url} ({visit_type}): {status} (request {request.id}) [{labels}]"
                    )
    except KeyboardInterrupt:
        print(file=sys.stderr)
        print("interrupted; some archiving requests may not have been submitted.", file=sys.stderr)
        return 130
    except SWHError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if on_log:
        on_log("archiving requests submitted")
    return 0


def _run_cook_swh_fods_command(args: argparse.Namespace) -> int:
    if _is_nix_file(args.input):
        vault_swhids = _extract_vault_swhids_from_nix_file(args.input)
    else:
        results = _load_results_for_build(args)
        if results is None:
            return 1
        vault_swhids = vault_swhids_for_results(results)

    if not vault_swhids:
        print(
            f"no vault flat archives need cooking for {args.input}",
            file=sys.stderr,
        )
        return 0

    on_log = None if args.quiet else _log_to_stderr
    if on_log:
        on_log(
            f"requesting cooking of {len(vault_swhids)} vault flat archive(s) on Software Heritage..."
        )

    try:
        with _swh_client_from_args(args, on_log) as client:
            for swhid in sorted(vault_swhids):
                task = client.ensure_vault_flat_cooking(swhid)
                status = task.status if task else "unknown"
                if on_log:
                    on_log(f"{swhid}: {status}")
    except SWHError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if on_log:
        on_log("cooking requests submitted")
    return 0


def _run_build_swh_fods_command(args: argparse.Namespace) -> int:
    if _is_nix_file(args.input):
        nix_file = args.input
    else:
        results = _load_results_for_build(args)
        if results is None:
            return 1
        output_path = args.output or "swh-backed-fods.nix"
        expressions = write_swh_fods_nix(output_path, results)

        if not expressions:
            print(
                f"no SWH-backed FODs to build for {args.input}",
                file=sys.stderr,
            )
            return 0
        nix_file = output_path

    on_log = None if args.quiet else _log_to_stderr

    try:
        all_attrs = _list_attrs_in_nix_file(nix_file)
        attr_outputs = _eval_nix_file_outputs(nix_file)
    except NixCommandError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if not all_attrs:
        print(
            f"no SWH-backed FODs to build in {nix_file}",
            file=sys.stderr,
        )
        return 0

    dry_run_extra = list(args.nix_build_arg)

    try:
        dry_run_plan = dry_run_nix_file(
            nix_file,
            all_attrs,
            extra_args=dry_run_extra,
            on_log=on_log,
            no_substitute=args.no_substitute,
        )
    except NixCommandError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    missing_attrs = [attr for attr in all_attrs if not os.path.exists(attr_outputs[attr])]

    if not missing_attrs:
        print(
            f"all SWH-backed FOD(s) from {nix_file} are already in the Nix store",
            file=sys.stderr,
        )
        return 0

    if on_log:
        on_log(
            f"{len(missing_attrs)} SWH-backed FOD(s) missing from the Nix store"
        )

    # Map each top-level output path to its derivation path so we can tell
    # from the dry-run plan whether the attribute will be built locally or
    # fetched from a substituter.  Vault archives only need to be cooked for
    # attributes that will actually be built.
    output_path_to_drv_path = {
        entry["outputs"]["out"]: entry["drvPath"]
        for entry in dry_run_plan.plan
        if isinstance(entry.get("outputs"), dict) and "out" in entry["outputs"]
    }

    vault_swhids_by_attr = _extract_vault_swhids_by_attr(nix_file)
    uncooked: list[tuple[str, str, str]] = []
    try:
        with _swh_client_from_args(args, on_log) as client:
            for attr in missing_attrs:
                swhid = vault_swhids_by_attr.get(attr)
                if not swhid:
                    continue
                output_path = attr_outputs[attr]
                drv_path = output_path_to_drv_path.get(output_path)
                if drv_path is not None and drv_path not in dry_run_plan.will_build:
                    # The attribute will be fetched from a substituter (or is
                    # already accounted for otherwise), so no vault cooking is
                    # required for it.
                    continue
                task = client.get_vault_flat_task(swhid)
                status = task.status if task is not None else "not requested"
                if status != "done":
                    uncooked.append((attr, swhid, status))
    except SWHError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if uncooked:
        uncooked_attrs = {u[0] for u in uncooked}
        for attr, swhid, status in uncooked:
            print(
                f"warning: vault flat archive for {swhid} (attribute {attr!r}) "
                f"is not cooked (status: {status}); skipping; "
                f"run 'cook-swh-fods' first",
                file=sys.stderr,
            )
        missing_attrs = [attr for attr in missing_attrs if attr not in uncooked_attrs]

    if not missing_attrs:
        print(
            "all remaining SWH-backed FOD(s) are already in the Nix store "
            "or skipped due to uncooked vault archives",
            file=sys.stderr,
        )
        return 0

    build_extra = list(args.nix_build_arg)
    if args.no_substitute:
        build_extra.append("--no-substitute")

    try:
        build_nix_file(
            nix_file,
            missing_attrs,
            extra_args=build_extra,
            on_log=on_log,
        )
    except NixCommandError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(
        f"built SWH-backed FOD(s) from {nix_file}",
        file=sys.stderr,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = _validate_args(parser.parse_args(argv), parser)

    if args.command == "request-archiving":
        return _run_request_archiving_command(args)

    if args.command == "generate-swh-fods":
        return _run_generate_command(args)

    if args.command == "cook-swh-fods":
        return _run_cook_swh_fods_command(args)

    if args.command == "build-swh-fods":
        return _run_build_swh_fods_command(args)

    return _run_check_command(args)


def _handle_interrupt(
    checked: dict[str, SWHCheckResult],
    checkpoint_path: Path | None,
    cache: Cache | None = None,
) -> int:
    """Print a clean, traceback-free message when interrupted (e.g. Ctrl+C),
    and return the conventional 128+SIGINT exit code.
    """
    print(file=sys.stderr)  # move past any '^C' echoed by the terminal
    message = "interrupted"
    if checked:
        message += f"; {len(checked)} FOD(s) were checked before being interrupted"
    if checkpoint_path is not None:
        message += f"; progress was saved to {checkpoint_path}, re-run the same command to resume"
    print(f"{message}.", file=sys.stderr)
    if cache is not None:
        try:
            cache.save()
        except OSError:
            pass
    return 130


if __name__ == "__main__":
    sys.exit(main())
