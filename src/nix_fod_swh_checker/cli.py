"""Command line interface for nix-fod-swh-checker."""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

from .checker import check_fod
from .checkpoint import default_checkpoint_path, load_checkpoint, save_checkpoint
from .models import SWHCheckResult, SWHLookupMethod
from .nix import NixCommandError, iter_fixed_output_derivations, show_derivations_recursive
from .swh import DEFAULT_API_URL, SWHClient, SWHError
from .swh_fod import UnsupportedSWHFodError, write_swh_fods_nix

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
    subparsers = parser.add_subparsers(dest="command", help="available commands")

    check_parser = subparsers.add_parser(
        "check",
        help="check FODs against Software Heritage (default)",
    )
    check_parser.add_argument(
        "installable",
        help="the Nix installable to inspect, e.g. 'nixpkgs#hello' or a store path",
    )

    generate_parser = subparsers.add_parser(
        "generate-swh-fods",
        help="generate a Nix expression with SWH-backed FODs from a checkpoint",
    )
    generate_parser.add_argument(
        "installable",
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
        "--json",
        action="store_true",
        help="print machine-readable JSON instead of a human-readable report",
    )
    check_parser.add_argument(
        "--only-unknown",
        action="store_true",
        help="only report FODs that are not known to Software Heritage (or undetermined)",
    )
    check_parser.add_argument(
        "--quiet",
        "-q",
        action="store_true",
        help="do not print progress messages to stderr while checking FODs",
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
    return parser


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

        with SWHClient(
            api_url=args.swh_api_url,
            api_token=api_token,
            min_delay=args.min_delay,
            on_log=on_log,
        ) as client:
            total = len(fods)
            for index, fod in enumerate(fods, start=1):
                if fod.label in checked:
                    if on_log:
                        on_log(
                            f"[{index}/{total}] {fod.label}: already checked "
                            f"({_STATUS_LABELS[checked[fod.label].known]}), skipping"
                        )
                    continue
                if on_log:
                    on_log(f"[{index}/{total}] checking {fod.label}")
                try:
                    result = check_fod(
                        fod,
                        client,
                        on_log=on_log,
                        swh_identify_timeout=args.swh_identify_timeout,
                        disarchive_timeout=args.disarchive_timeout,
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

        if args.json:
            print(json.dumps([_result_to_dict(r) for r in results], indent=2))
        else:
            _print_report(results)

        return 0
    except KeyboardInterrupt:
        return _handle_interrupt(checked, checkpoint_path)


def _run_generate_command(args: argparse.Namespace) -> int:
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
    try:
        expressions = write_swh_fods_nix(args.output, results)
    except UnsupportedSWHFodError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(
        f"wrote {len(expressions)} SWH-backed FOD expression(s) to {args.output}",
        file=sys.stderr,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "generate-swh-fods":
        return _run_generate_command(args)

    return _run_check_command(args)


def _handle_interrupt(
    checked: dict[str, SWHCheckResult], checkpoint_path: Path | None
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
    return 130


if __name__ == "__main__":
    sys.exit(main())
