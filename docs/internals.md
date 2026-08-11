# nix-fod-swh-checker internals

This document describes the internal algorithms, data models, and code paths of `nix-fod-swh-check`. For command-line usage, see [README.md](../README.md) and the [specification](specification.md).

## Overview

The tool is structured as a small Python package with the following modules:

| Module | Responsibility |
|--------|----------------|
| `cli.py` | Argument parsing, subcommand dispatch, and user-facing orchestration. |
| `checker.py` | Decide how to check a single FOD and run the chosen strategy. |
| `nix.py` | Run `nix derivation show`, `nix build --dry-run`, and extract FODs from Nix JSON. |
| `swh.py` | Software Heritage Web API client. |
| `swhid.py` | Compute SWHIDs with the `swh identify` command. |
| `disarchive.py` | Unpack archives and capture GNU Guix `disarchive` specifications. |
| `swh_fod.py` | Generate Nix expressions for SWH-backed FODs. |
| `checkpoint.py` | Save and resume in-progress results. |
| `models.py` | Shared data classes and enumerations. |

## Data models

### `FixedOutputDerivation`

A single FOD output extracted from `nix derivation show --recursive`:

- `drv_path`: store path of the `.drv` file.
- `output_name`: output name (usually `out`).
- `output_path`: store path of the output, when known.
- `name`: derivation `name`.
- `method`: content-addressing method (`flat`, `nar`, `git`, or `None`).
- `hash_algo`: hash algorithm.
- `hash_hex`: fixed output hash.
- `label` (property): `drv_path` for `out`, otherwise `drv_path^output_name`.

### `SWHCheckResult`

Outcome of checking one FOD:

- `fod`: the `FixedOutputDerivation`.
- `known`: `True`, `False`, or `None` (undetermined).
- `method`: the strategy used, as a `SWHLookupMethod` value.
- `detail`: human-readable explanation.
- `swhid`, `swh_url`: SWHID and archive URL, when applicable.
- `disarchive_spec`, `disarchive_swhid`, `disarchive_top_dir`: disarchive metadata, when applicable.

### `SWHLookupMethod`

- `CONTENT_HASH` — direct lookup by raw content hash (`/content/{algo}:{hash}/`).
- `SWHID_KNOWN` — direct batch lookup of a git-style SWHID (`/known/`).
- `BUILD_AND_IDENTIFY` — realise the FOD, run `swh identify`, then look up the computed SWHID.
- `KNOWN_AFTER_DISARCHIVE` — the raw FOD is not known, but its unpacked contents are known as a directory.
- `UNDETERMINED` — the check could not be completed.

## Extracting FODs from Nix

`nix.py:show_derivations_recursive` runs:

```
nix derivation show --recursive <installable>
```

The JSON output is normalized so that both full store paths and basename-only keys are accepted. Non-derivation entries are dropped.

`nix.py:iter_fixed_output_derivations` walks every derivation's `outputs` mapping. An output is considered a FOD exactly when it has a non-empty `hash` field. The `method` is taken from `output.method`; if that is missing, the legacy `env.outputHashMode` is used, with `recursive` mapped to `nar`.

## Comparison strategies

`checker.py:check_fod` selects a strategy in this order:

1. **`git` method with `sha1` hash** → `_check_via_swhid`
2. **`flat` method with a supported content hash algorithm** → `_check_via_content_hash`
3. **Anything else** → `_check_via_build_and_identify`

### `_check_via_swhid`

Used for `method="git"` and `hash_algo="sha1"`. The FOD hash is a git object hash, so it is tried as both a content (`swh:1:cnt:<hash>`) and a directory (`swh:1:dir:<hash>`) SWHID via `POST /known/`. If either is known, the result is `SWHID_KNOWN`. Otherwise, `try_disarchive` is attempted.

### `_check_via_content_hash`

Used for `method="flat"` and `hash_algo` in `{"sha1", "sha1_git", "sha256", "blake2s256"}`. The hash is looked up via `GET /content/{algo}:{hash}/`. If known, the response's `checksums.sha1_git` is used to build a content SWHID and archive URL. If not known, `try_disarchive` is attempted.

### `_check_via_build_and_identify`

Used for all other methods (most commonly `nar`). The FOD is realised with:

```
nix build --no-link --print-out-paths <drv>^<output>
```

This fetches from substituters when possible. The resulting store path is passed to `swh identify --no-filename <path>` to compute its intrinsic SWHID, which is then looked up via `POST /known/`. Failures to realise or identify are reported as `UNDETERMINED` / `known=None`.

### `try_disarchive`

When a `git` or `flat` FOD is not directly known, `disarchive.py:try_disarchive` attempts to treat it as an archive:

1. Realise the FOD.
2. If it is not a regular file, give up.
3. Unpack it as a tar or zip archive to a temporary directory.
4. If the archive contains a single top-level directory, descend into it (Nix `stripHash` semantics).
5. Compute the stripped directory SWHID with `swh identify`.
6. Look up the stripped SWHID via `/known/`.
7. If the stripped SWHID is known, capture a `disarchive disassemble` specification so the exact original archive can be reconstructed later.
8. If the disarchive specification contains its own directory SWHID, look that up too.
9. Report `KNOWN_AFTER_DISARCHIVE` if either SWHID is known; otherwise report `UNKNOWN`.

If `swh identify` fails or times out after unpacking, the result is `UNDETERMINED`. If `disarchive disassemble` fails or times out, the result is also `UNDETERMINED`, but the stripped SWHID and URL are preserved: without a disarchive specification the exact original archive cannot be reconstructed, so the result cannot be turned into a SWH-backed FOD.

The reported SWHID prefers the disarchive SWHID when it is known, because that is the directory `disarchive assemble` can rebuild from directly. Otherwise the stripped SWHID is used.

## Software Heritage API client

`SWHClient` in `swh.py` wraps a subset of the Software Heritage Web API:

- `lookup_content(algo, hash_hex)` → `GET /content/{algo}:{hash}/`
- `lookup_known_swhids(swhids)` → `POST /known/`
- `cook_vault_flat(swhid)` → `POST /vault/flat/{swhid}/`
- `get_vault_flat_task(swhid)` → `GET /vault/flat/{swhid}/`
- `ensure_vault_flat_cooking(swhid)` — returns an existing task or creates one.
- `wait_for_vault_flat(swhid, ...)` — polls until the task is `done` or `failed`.

The client uses `requests.Session`, sets `Accept: application/json`, and adds `Authorization: Bearer <token>` when authenticated.

Rate-limiting behavior:

- Anonymous requests are throttled to one every `min_delay` seconds.
- `429` responses are retried after `X-RateLimit-Reset` (Unix timestamp) or `Retry-After`, with a fallback of `min_delay * 2`.
- Network failures are retried up to `max_retries` times with exponential backoff.
- Low quota warnings are emitted when `X-RateLimit-Remaining` is `3` or less.

## Generated Nix expression

`swh_fod.py` produces a file of the form:

```nix
{ pkgs ? import (builtins.fetchTarball {
  url = "https://github.com/NixOS/nixpkgs/archive/<pin>.tar.gz";
  sha256 = "...";
}) {} }:
{
  <safe-attr-name> = <expression>;
  ...
}
```

Attribute names are derived from FOD labels by replacing non-alphanumeric characters with underscores, collapsing runs of underscores, stripping leading/trailing underscores, and prefixing a leading digit with `attr_`. Collisions are resolved by appending `_1`, `_2`, etc.

### Expression types

| Case | Nix expression | Notes |
|------|----------------|-------|
| `CONTENT_HASH` | `builtins.derivation` with `builtin:fetchurl`, `outputHashMode = "flat"`, URL `/content/{algo}:{hash}/raw/` | Uses only Nix builtins. |
| `SWHID_KNOWN` / `BUILD_AND_IDENTIFY` as `swh:1:cnt:...` | `builtins.derivation` with `builtin:fetchurl`, `outputHashMode` mapped from FOD `method`, URL `/content/sha1_git:<hash>/raw/` | Uses only Nix builtins. |
| `SWHID_KNOWN` / `BUILD_AND_IDENTIFY` as `swh:1:dir:...` | `pkgs.stdenv.mkDerivation` that downloads the vault flat bundle and extracts it | Requires `pkgs`. |
| `KNOWN_AFTER_DISARCHIVE` with matching disarchive SWHID | `pkgs.stdenv.mkDerivation` that downloads the vault flat bundle, extracts it, and runs `disarchive assemble` | Requires `pkgs`. |
| `KNOWN_AFTER_DISARCHIVE` with only stripped SWHID known | Same as above, but wraps the stripped directory back inside its original top-level directory before `disarchive assemble` | Requires `pkgs`. |

The `outputHashMode` mapping for content SWHIDs is:

- `flat` → `"flat"`
- `nar` → `"recursive"`
- `git` → `"git"`

If the FOD method is missing or unsupported, `swh_fod_expression` returns `None` and the result is skipped.

## Building SWH-backed FODs

`build-swh-fods` first generates or loads a `swh-backed-fods.nix` file, then:

1. Lists the attribute names in the file.
2. Runs `nix build --dry-run -f <file> <attrs> --json` via `nix.py:dry_run_nix_file`.
3. Evaluates the output paths with `nix eval --json`.
4. Skips attributes whose outputs are already in the local Nix store.
5. Maps each remaining output path back to its derivation path using the JSON plan returned by the dry run.
6. For vault-backed attributes, checks the Software Heritage vault flat cooking status only when the derivation path appears in the dry run's `will be built` list. Attributes that would be fetched from a substituter are not required to be cooked.
7. Builds the remaining attributes with `nix build -f <file> <attrs>`.

`nix.py:dry_run_nix_file` returns a `DryRunPlan` containing the raw JSON plan plus two sets parsed from the dry-run stderr: `will_build` (derivation paths that must be built locally) and `will_fetch` (output paths that will be fetched from a substituter).

## Checkpoint persistence

`checkpoint.py` saves results atomically:

1. Serialize the current payload to JSON.
2. Write to a temporary file in the same directory (`.<tmp-checkpoint-><random>.json`).
3. Rename the temporary file to the final path with `os.replace`.

Loading tolerates missing or malformed files by returning an empty dictionary.

## Progress logging

All subcommands accept `--quiet`/`-q`. When not quiet, progress messages are emitted via `cli._log_to_stderr`, which:

- Writes messages ending in `\n` as new lines.
- Writes messages starting with `\r` as carriage-return progress updates (to mimic Nix's interactive UI).

`nix build` is run on a pseudo-terminal when progress logging is enabled so that Nix renders its interactive progress bars; the raw terminal output is parsed to extract the final store path.

## Error handling

- `NixCommandError` and `SWHError` are caught at subcommand boundaries and reported to stderr with exit code `1`.
- `KeyboardInterrupt` is caught in `_run_check_command` and converted to a clean message and exit code `130`.
- During the FOD checking loop, individual `SWHError`s are printed as warnings and the loop continues.
- Known results that cannot be turned into expressions are skipped with a warning.
- Argument parsing errors exit with code `2` via `argparse`.
