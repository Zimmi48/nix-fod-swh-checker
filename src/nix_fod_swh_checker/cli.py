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
from .models import SWHCheckResult
from .nix import NixCommandError, iter_fixed_output_derivations, show_derivations_recursive
from .swh import DEFAULT_API_URL, SWHClient, SWHError

_STATUS_LABELS = {True: "KNOWN", False: "UNKNOWN", None: "UNDETERMINED"}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nix-fod-swh-check",
        description=(
            "List every fixed-output derivation (FOD) reachable from a Nix "
            "attribute and check whether its source is already archived on "
            "Software Heritage."
        ),
    )
    parser.add_argument(
        "installable",
        help="the Nix installable to inspect, e.g. 'nixpkgs#hello' or a store path",
    )
    parser.add_argument(
        "--nix-binary",
        default="nix",
        help="path to the nix executable to use (default: %(default)s)",
    )
    parser.add_argument(
        "--swh-binary",
        default="swh",
        help=(
            "path to the Software Heritage 'swh' CLI (providing 'swh identify'), "
            "used to compute SWHIDs for FODs whose hash cannot be checked directly "
            "(default: %(default)s)"
        ),
    )
    parser.add_argument(
        "--swh-api-url",
        default=DEFAULT_API_URL,
        help="base URL of the Software Heritage API (default: %(default)s)",
    )
    parser.add_argument(
        "--swh-api-token",
        default=None,
        help="bearer token for the Software Heritage API (or set SWH_API_TOKEN)",
    )
    parser.add_argument(
        "--min-delay",
        type=float,
        default=1.0,
        help="minimum delay in seconds between Software Heritage API requests (default: %(default)s)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print machine-readable JSON instead of a human-readable report",
    )
    parser.add_argument(
        "--only-unknown",
        action="store_true",
        help="only report FODs that are not known to Software Heritage (or undetermined)",
    )
    parser.add_argument(
        "--quiet",
        "-q",
        action="store_true",
        help="do not print progress messages to stderr while checking FODs",
    )
    parser.add_argument(
        "--checkpoint-file",
        default=None,
        help=(
            "path to a JSON file used to save results as FODs are checked, so an "
            "interrupted run can resume without re-checking them (default: a "
            "per-installable file under $XDG_CACHE_HOME/nix-fod-swh-checker/)"
        ),
    )
    parser.add_argument(
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
        "swh_url": result.swh_url,
    }


def _print_report(results: list[SWHCheckResult]) -> None:
    if not results:
        print("nothing to report")
        return
    for result in results:
        print(f"[{_STATUS_LABELS[result.known]}] {result.fod.label}")
        print(f"    method={result.method.value}: {result.detail}")
        if result.swh_url:
            print(f"    {result.swh_url}")

    known = sum(1 for r in results if r.known is True)
    unknown = sum(1 for r in results if r.known is False)
    undetermined = sum(1 for r in results if r.known is None)
    print(
        f"\n{len(results)} FOD(s) checked: "
        f"{known} known, {unknown} unknown, {undetermined} undetermined"
    )


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    api_token = args.swh_api_token or os.environ.get("SWH_API_TOKEN")
    on_log = None if args.quiet else lambda msg: print(msg, file=sys.stderr, flush=True)

    try:
        derivations = show_derivations_recursive(
            args.installable, nix_binary=args.nix_binary, on_log=on_log
        )
    except NixCommandError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    fods = list(iter_fixed_output_derivations(derivations))
    if not fods:
        print("no fixed-output derivations found", file=sys.stderr)
        return 0

    if on_log:
        on_log(f"found {len(fods)} fixed-output derivation(s) to check against Software Heritage")

    checkpoint_path = None
    checked: dict[str, SWHCheckResult] = {}
    if not args.no_checkpoint:
        checkpoint_path = (
            Path(args.checkpoint_file) if args.checkpoint_file else default_checkpoint_path(args.installable)
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
        api_url=args.swh_api_url, api_token=api_token, min_delay=args.min_delay, on_log=on_log
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
                    nix_binary=args.nix_binary,
                    swh_binary=args.swh_binary,
                    on_log=on_log,
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


if __name__ == "__main__":
    sys.exit(main())
