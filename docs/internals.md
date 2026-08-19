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
- `origin_urls`: upstream origin URLs extracted from the FOD environment.

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

`nix.py:iter_fixed_output_derivations` walks every derivation's `outputs` mapping. An output is considered a FOD exactly when it has a non-empty `hash` field.

Because Nix implementations and versions differ in how they serialize FOD metadata, the extraction normalizes several fields:

- `method` is taken from `output.method`. If that is missing, the legacy `env.outputHashMode` is used, with `recursive` mapped to `nar`. Some Nix implementations emit the raw value `recursive` in `output.method`; that is normalized to `nar` as well. If both are missing, the hash format is used as a last resort: SRI hashes (`<algo>-<base64>`) indicate recursive/NAR mode, while a plain hex string indicates `flat` mode.
- `hash_algo` is taken from `output.hashAlgo`. If that is missing, `env.outputHashAlgo` is used. Nix represents recursive hashing as `r:<algo>` (e.g. `r:sha256`); the `r:` prefix is stripped. If both fields are missing or empty, the algorithm prefix of an SRI hash value (e.g. `sha256-...`) is used.
- `hash_hex` is the raw hash value, with any SRI algorithm prefix stripped. Some Nix implementations (e.g. Determinate Nix) emit flat `sha256`/`sha512` hashes as base64 even when the value is not SRI-prefixed; those are decoded and re-encoded as hex because Software Heritage's content lookup endpoint expects hex for these algorithms.

`nix.py:_extract_origin_urls` collects upstream URLs from the derivation environment: the `url` variable (single URL) and the `urls` variable (whitespace-separated mirror URLs). These are stored on `FixedOutputDerivation.origin_urls` and copied into each `SWHCheckResult`.

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

This fetches from substituters when possible. The command is run on a pseudo-terminal when progress logging is enabled so that Nix renders its interactive progress bars; the raw terminal output is parsed to extract the final store path. ANSI escape sequences (including DEC private cursor sequences such as `\x1b[?25l` emitted by some Nix implementations) are stripped before parsing.

The resulting store path is passed to `swh identify --no-filename <path>` to compute its intrinsic SWHID, which is then looked up via `POST /known/`. Failures to realise or identify are reported as `UNDETERMINED` / `known=None`.

### `try_disarchive`

When a `git` or `flat` FOD is not directly known, `disarchive.py:try_disarchive` first queries the GNU Guix disarchive database as a fast cache, then falls back to realising the FOD locally.

The database lookup (`_try_disarchive_database`) sends a `GET` request to `<disarchive-db-url>/<hash_algo>/<hash_hex>`. The FOD must have both a hash algorithm and a hash value; otherwise the lookup is skipped and the local path is used. On HTTP 200 it receives a disarchive specification, extracts the embedded `swh:1:dir:...` SWHID, and checks it via `/known/`. If the SWHID is known, the function returns `KNOWN_AFTER_DISARCHIVE` immediately, using the database spec as `disarchive_spec` and the `directory-ref` `name` field as `disarchive_top_dir`. No local `nix build`, unpacking, or `disarchive disassemble` is performed.

If the database returns 404, the spec has no embedded SWHID, the database request fails, or the FOD has no usable hash, the function falls back to `_try_disarchive_local`. If the database returned a spec but its embedded SWHID is unknown, the spec is still forwarded to `_try_disarchive_local` so that, if the stripped directory SWHID is known locally, `disarchive disassemble` can be skipped and the fetched spec can be reused:

1. Realise the FOD.
2. If it is not a regular file, give up.
3. Unpack it as a tar or zip archive to a temporary directory.
4. If the archive contains a single top-level directory, descend into it (Nix `stripHash` semantics).
5. Compute the stripped directory SWHID with `swh identify`.
6. Look up the stripped SWHID via `/known/`.
7. If the stripped SWHID is known, obtain a disarchive specification. If `_try_disarchive_database` already fetched a spec, reuse it; otherwise capture a fresh `disarchive disassemble` specification so the exact original archive can be reconstructed later.
8. If the disarchive specification contains its own directory SWHID, look that up too.
9. Report `KNOWN_AFTER_DISARCHIVE` if either SWHID is known; otherwise report `UNKNOWN`.

If `swh identify` fails or times out after unpacking, the result is `UNDETERMINED`. If a fresh `disarchive disassemble` fails or times out, the result is also `UNDETERMINED`, but the stripped SWHID and URL are preserved: without a disarchive specification the exact original archive cannot be reconstructed, so the result cannot be turned into a SWH-backed FOD. When a database spec is reused, the local `disarchive disassemble` step is skipped entirely, so it cannot fail.

The reported SWHID prefers the disarchive SWHID when it is known, because that is the directory `disarchive assemble` can rebuild from directly. Otherwise the stripped SWHID is used.

## Software Heritage API client

`SWHClient` in `swh.py` wraps a subset of the Software Heritage Web API:

- `lookup_content(algo, hash_hex)` → `GET /content/{algo}:{hash}/`
- `lookup_known_swhids(swhids)` → `POST /known/`
- `request_origin_save(url, visit_type)` → `POST /origin/save/`
- `get_origin_save_request(request_id)` → `GET /origin/save/{request_id}/`
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

## Requesting archival of unknown origins

`request-archiving` reads previously checked results and asks Software Heritage to archive the upstream origins of FODs that are not yet in the archive.

1. Load results from a checkpoint or JSON file.
2. Keep only results where `known` is not `true`.
3. For each result, look at `origin_urls`:
   - If the list is empty, warn that the FOD is being skipped and continue.
   - Otherwise, probe each URL with a `HEAD` request in order and keep the first one that responds. A response is considered live when its status is `< 400` or `405`; redirects are followed. If none respond, warn that the FOD is being skipped and continue.
4. Infer a visit type for each remaining result: `git` when `fod.method == "git"`, otherwise `tarball`.
5. Deduplicate `(visit_type, url)` pairs.
6. For each pair, call `SWHClient.request_origin_save`.
7. Print the save request status and identifier for each submitted origin.

The command is intentionally fire-and-forget: it does not wait for Software Heritage to actually visit the origin. A `--dry-run` flag lists the origins that would be requested without making any API calls and without probing URLs.

Skipped FODs and failed save requests are reported as warnings on stderr; the command continues with the remaining origins.

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
- `KeyboardInterrupt` is caught in `_run_check_command` and `_run_request_archiving_command` and converted to a clean message and exit code `130`.
- During the FOD checking loop, individual `SWHError`s are printed as warnings and the loop continues.
- Known results that cannot be turned into expressions are skipped with a warning.
- Argument parsing errors exit with code `2` via `argparse`.

## Test suite

The tests live under `tests/` and are run with `pytest` during the Nix check phase (`nix flake check`). The suite mixes unit tests for pure logic with integration-style tests that invoke the real external tools used by the checker.

### Required binaries

Several tests require the same binaries that the application uses at runtime:

- `swh identify` (from `swh-model`) is used by `tests/test_swhid.py` and by any test that computes a real directory SWHID.
- `disarchive disassemble` is used by `tests/test_disarchive.py` and by `tests/test_checker.py` cases that exercise the archive fallback.
- `nix` is never invoked for real during tests; `realise_fod` is always mocked.

These tools are declared as `nativeCheckInputs` in `flake.nix`, so they are available when the package is built with Nix.

### Mocking strategy

The goal is to test the actual integration with `swh identify` and `disarchive` while keeping the Nix side of the codebase fast and deterministic:

- `realise_fod` is stubbed to return a temporary archive or directory path instead of running `nix build`.
- The SWH API client is replaced by a fake that returns canned answers for `lookup_content` and `lookup_known_swhids`.
- `swh identify` and `disarchive disassemble` are invoked for real, so the computed SWHIDs and disarchive specifications are genuine.

`disarchive` is a required test dependency and is always available during the Nix check phase; no tests skip based on its presence.

### Coverage by module

- `test_swhid.py` — happy-path `compute_swhid` calls create real files and directories and assert against actual `swh identify` output. Error paths (missing binary, command failure, timeout, unexpected output, custom binary) exercise the wrapper's error handling, usually by stubbing `subprocess.run`.
- `test_disarchive.py` — unknown-directory tests compute the real stripped SWHID with `swh identify`. Known-directory tests run real `disarchive disassemble` and assert on the returned specification and top-level directory. Disarchive failures are triggered by passing a non-existent `disarchive_binary`.
- `test_checker.py` — `flat` and `git` unknown-fallback tests run real `try_disarchive` on a real archive. `nar` tests compute real directory SWHIDs from temporary directories. The identify-failure path triggers a real `SWHIdentifyError` by using a missing `swh_binary`.
