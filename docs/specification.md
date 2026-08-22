# nix-fod-swh-checker specification

This document is the authoritative user-facing reference for `nix-fod-swh-check`. It describes every subcommand, option, output format, exit code, and file format exactly as the current implementation behaves.

For a gentler introduction, see [README.md](../README.md). For implementation details, see [internals.md](internals.md).

## Invocation

```
nix-fod-swh-check <subcommand> [options] <arguments>
```

The program is also exposed as the Python module `nix_fod_swh_checker.cli:main` and can be run with `python -m nix_fod_swh_checker`.

A subcommand is required. Running without one prints a usage message and exits with code `2`.

The available subcommands are: `check`, `request-archiving`, `generate-swh-fods`, `cook-swh-fods`, and `build-swh-fods`.

## Global behavior

- All progress and diagnostic messages are written to **stderr**.
- Ordinary command output (the human-readable report or generated file contents) is written to **stdout** or to a file, depending on the subcommand.
- The program never prompts interactively.
- Network requests to Software Heritage are throttled for anonymous users; see [Rate limiting](#rate-limiting).

## Subcommands

### `check`

```
nix-fod-swh-check check [options] <installable>
```

Discover every fixed-output derivation (FOD) reachable from `<installable>` and check whether each one is already archived on Software Heritage.

`<installable>` is any single argument accepted by `nix derivation show --recursive`, for example:

- a flake reference: `nixpkgs#hello`
- a store path: `/nix/store/...-foo.drv`

The command performs the following steps:

1. Run `nix derivation show --recursive <installable>`.
2. Walk the returned derivation graph and collect every output that has a fixed hash.
3. For each FOD, extract any upstream origin URLs from the derivation environment (see [Origin URLs](#origin-urls)).
4. Choose a comparison strategy based on its `method` and hash algorithm (see [internals.md](internals.md#comparison-strategies)).
5. Query the Software Heritage API. For archives that are not directly known, the GNU Guix disarchive database may be queried first as a fast cache (see [internals.md](internals.md#try_disarchive)).
6. Save each result to a checkpoint file as it is computed, unless `--no-checkpoint` is given.
7. Print a human-readable report. If `-o`/`--output` is given, also write JSON results to the given path.

If the checkpoint already contains results for some FODs, those FODs are skipped and only the missing ones are checked, unless `--retry-unknown` or `--retry-undetermined` is given.

#### `check` options

| Option | Default | Description |
|--------|---------|-------------|
| `-o`, `--output` `<path>` | none | Write machine-readable JSON results to `<path>`. The human-readable report is still printed to stdout unless `--quiet` is given. The file is overwritten if it exists. A trailing newline is appended. |
| `--only-unknown` | false | In the human-readable report, only show FODs whose `known` status is not `true` (i.e. unknown or undetermined). |
| `--retry-unknown` | false | Re-check FODs that were previously reported as unknown. Useful after requesting archiving of missing origins to see whether the FODs have since been added to Software Heritage. |
| `--retry-undetermined` | false | Re-check FODs that were previously reported as undetermined. Useful when retrying a previous check with a different timeout value for instance. |
| `-q`, `--quiet` | false | Suppress stderr progress messages and the human-readable report. Warnings and errors are still printed. |
| `--checkpoint-file` `<path>` | per-installable cache file | Read from and write to the given checkpoint file. See [Checkpoint file](#checkpoint-file). |
| `--no-checkpoint` | false | Do not read or write a checkpoint file. |
| `--swh-api-url` `<url>` | `https://archive.softwareheritage.org/api/1` | Base URL of the Software Heritage API. |
| `--swh-api-token` `<token>` | value of `SWH_API_TOKEN` | Bearer token for authenticated Software Heritage API requests. |
| `--min-delay` `<seconds>` | `1.0` | Minimum delay between anonymous Software Heritage API requests. No delay is inserted when authenticated. |
| `--swh-identify-timeout` `<seconds>` | `30.0` | Timeout for the `swh identify` command when realising a FOD. |
| `--disarchive-timeout` `<seconds>` | `30.0` | Timeout for the `disarchive disassemble` command when capturing an archive specification. |
| `--disarchive-db-url` `<url>` | `https://disarchive.guix.gnu.org` | Base URL of the GNU Guix disarchive database used to cache archive metadata. |
| `--skip-disarchive` | false | Do not query the GNU Guix disarchive database; always realise archives locally to capture their specification. |

Incompatible option combinations are rejected with exit code `2`:

- `--no-checkpoint` cannot be combined with `--checkpoint-file`.
- `--retry-unknown` and `--retry-undetermined` require a checkpoint, so they cannot be combined with `--no-checkpoint`.

#### `check` human-readable report

The report printed to stdout has the following form (it is suppressed when `--quiet` is given):

```
[KNOWN] <drv-path>[^<output>]
    method=<strategy>: <detail>
    <swh-url>

[<N> FOD(s) checked: <known> known, <known-after-disarchive> known after disarchive, <unknown> unknown, <undetermined> undetermined]
```

Status labels:

- `KNOWN` — the FOD is known to Software Heritage.
- `KNOWN AFTER DISARCHIVE` — the raw FOD is not known, but its unpacked contents are known as a directory.
- `UNKNOWN` — the FOD is not known to Software Heritage.
- `UNDETERMINED` — the check could not be completed (for example, the FOD could not be realised, `swh identify` failed, or the archive contents are known but `disarchive` could not capture a usable specification for the archive format).

If `--only-unknown` is given, only `UNKNOWN` and `UNDETERMINED` entries are printed, but the summary line still counts all FODs.

If no FODs are found, the message `no fixed-output derivations found` is printed to stderr and the command exits `0`.

If no results are available to report (for example, every check was interrupted before producing a result), the message `nothing to report` is printed to stdout.

#### `check` JSON output

When `-o`/`--output` is given, the file contains a JSON array of result objects. Each object has the following fields:

| Field | Type | Description |
|-------|------|-------------|
| `fod` | object | The checked FOD. See [FOD object](#fod-object). |
| `known` | boolean or `null` | `true` if known, `false` if not known, `null` if undetermined. |
| `method` | string | One of `content_hash`, `swhid_known`, `build_and_identify`, `known_after_disarchive`, `undetermined`. |
| `detail` | string | Human-readable explanation of the result. |
| `swhid` | string or `null` | The Software Heritage persistent identifier, if one was computed or looked up. |
| `swh_url` | string or `null` | URL of the object on `https://archive.softwareheritage.org`, if known. |
| `disarchive_spec` | string or `null` | The GNU Guix `disarchive` specification, if captured. `null` when no specification was obtained, including when `disarchive` could not produce a usable specification for the archive format. |
| `disarchive_swhid` | string or `null` | The SWHID embedded in the disarchive specification, if any. |
| `disarchive_top_dir` | string or `null` | The name of the single top-level directory from the disarchive specification, if any. This is the directory Nix normally strips when unpacking an archive. |
| `origin_urls` | list of strings | Upstream origin URLs extracted from the FOD's derivation environment, if any. Empty when no URLs are declared. |

The `fod` field is a [FOD object](#fod-object).

#### `check` exit codes

| Code | Meaning |
|------|---------|
| `0` | Success. |
| `1` | A `nix` command failed, a Software Heritage API error occurred, or another recoverable error was reported. |
| `2` | Argument parsing failed (e.g. missing subcommand) or incompatible options were given. |
| `130` | The user interrupted the command with Ctrl+C. A checkpoint is saved unless `--no-checkpoint` was used. |

Unhandled exceptions propagate and produce a Python traceback.

---

### `request-archiving`

```
nix-fod-swh-check request-archiving [options] [<installable>]
```

Request the archiving of upstream origins for FODs that were not found on Software Heritage.

The command reads results from either:

- the checkpoint file for `<installable>` (default), or
- the JSON file given with `-i`/`--json-input` (no installable allowed).

For every result whose `known` field is not `true` and which declares at least one [origin URL](#origin-urls), the command tries to pick a single reachable origin URL and sends a save request to Software Heritage. Each URL is probed with a `HEAD` request in order, and the first one that responds successfully is kept. If none of the URLs respond, the FOD is skipped with a warning.

The visit type is inferred from the FOD's `method`:

- `git` → `git`
- `flat`, `nar`, or any other method → `tarball`

Use `--dry-run` to list the origins that would be requested without contacting Software Heritage. In dry-run mode the origin URLs are not probed.

Origins are deduplicated by `(visit_type, url)` before any request is sent. If a request fails (for example because the origin is blocked), a warning is printed and the command continues with the remaining origins.
FODs that are skipped are reported with a warning on stderr: either because they have no `origin_urls`, or because none of their URLs are reachable.

#### `request-archiving` options

| Option | Default | Description |
|--------|---------|-------------|
| `-i`, `--json-input` `<path>` | none | Read results from a JSON file produced by `check -o`. |
| `--checkpoint-file` `<path>` | per-installable cache file | Checkpoint to read results from. |
| `--dry-run` | false | List origins that would be requested without making API calls. |
| `--swh-api-url` `<url>` | `https://archive.softwareheritage.org/api/1` | Base URL of the Software Heritage API. |
| `--swh-api-token` `<token>` | value of `SWH_API_TOKEN` | Bearer token for authenticated requests. |
| `--min-delay` `<seconds>` | `1.0` | Minimum delay between anonymous API requests. |
| `-q`, `--quiet` | false | Suppress stderr progress messages. |

`-i`/`--json-input` is mutually exclusive with `<installable>` and `--checkpoint-file`. Provide either an installable (optionally with `--checkpoint-file`) or `-i`/`--json-input`.

#### `request-archiving` exit codes

| Code | Meaning |
|------|---------|
| `0` | Success, or no origins needed archiving. |
| `1` | A Software Heritage API error occurred, or the checkpoint/JSON input could not be read. |
| `2` | Neither an installable nor `-i`/`--json-input` was provided, or incompatible options were given. |

---

### `generate-swh-fods`

```
nix-fod-swh-check generate-swh-fods [options] [<installable>]
```

Generate a Nix expression containing SWH-backed fixed-output derivations from previously checked results.

The command reads results from either:

- the checkpoint file for `<installable>` (default), or
- the JSON file given with `-i`/`--json-input` (no installable allowed).

For every result whose `known` field is `true`, it attempts to produce a Nix expression that downloads the same content from Software Heritage.
If a known result cannot be turned into an expression (for example, because its content-addressing method cannot be mapped to a Nix `outputHashMode`, or a disarchive result is missing required metadata), a warning is printed to stderr and the result is skipped.

The generated file evaluates to a function `{ pkgs ? <pinned-nixpkgs> }: { ... }` mapping safe attribute names to SWH-backed derivations. See [Generated Nix expression](internals.md#generated-nix-expression) for the exact format.

#### `generate-swh-fods` options

| Option | Default | Description |
|--------|---------|-------------|
| `-o`, `--output` `<path>` | `swh-backed-fods.nix` | Path to write the generated Nix expression. Overwritten if it exists. |
| `--checkpoint-file` `<path>` | per-installable cache file | Checkpoint to read results from. |
| `-i`, `--json-input` `<path>` | none | Read results from a JSON file produced by `check -o`. |
| `-q`, `--quiet` | false | Suppress stderr progress messages. Warnings and errors are still printed. |

`-i`/`--json-input` is mutually exclusive with `<installable>` and `--checkpoint-file`. Provide either an installable (optionally with `--checkpoint-file`) or `-i`/`--json-input`.


#### `generate-swh-fods` exit codes

| Code | Meaning |
|------|---------|
| `0` | Success. The file is written even if it contains zero expressions. |
| `1` | The checkpoint or JSON input could not be read, or contained no results. Known results that cannot be turned into expressions are skipped with a warning but do not cause exit code `1`. |
| `2` | Neither an installable nor `-i`/`--json-input` was provided, or incompatible options were given. |

---

### `cook-swh-fods`

```
nix-fod-swh-check cook-swh-fods [options] <input>
```

Request cooking of Software Heritage vault flat archives for directory SWHIDs required by the SWH-backed FODs.

`<input>` may be either:

- a Nix installable previously checked (results are read from the checkpoint), or
- a path to a generated `swh-backed-fods.nix` file (vault SWHIDs are extracted from the expression).

For each directory SWHID (`swh:1:dir:...`) that is fetched via `/vault/flat/.../raw` in the generated expressions, the command checks whether a cooking task already exists and creates one if not. It then exits immediately; it does not wait for cooking to finish.

When not `--quiet`, the command prints each SWHID along with the status of its cooking task (for example `new`, `pending`, `done`, or `failed`). This lets you see the current state of previously requested tasks and whether the `build-swh-fods` command may be run.

#### `cook-swh-fods` options

| Option | Default | Description |
|--------|---------|-------------|
| `--checkpoint-file` `<path>` | per-installable cache file | Checkpoint to read results from when `<input>` is an installable. Not compatible with providing a `.nix` file as `<input>`. |
| `--swh-api-url` `<url>` | `https://archive.softwareheritage.org/api/1` | Base URL of the Software Heritage API. |
| `--swh-api-token` `<token>` | value of `SWH_API_TOKEN` | Bearer token for authenticated requests. |
| `--min-delay` `<seconds>` | `1.0` | Minimum delay between anonymous API requests. |
| `-q`, `--quiet` | false | Suppress stderr progress messages. |

#### `cook-swh-fods` exit codes

| Code | Meaning |
|------|---------|
| `0` | Success, or no vault flat archives need cooking. |
| `1` | A Software Heritage API error occurred, or the checkpoint could not be read. |
| `2` | Incompatible options were given. |

---

### `build-swh-fods`

```
nix-fod-swh-check build-swh-fods [options] <input>
```

Generate a Nix expression with SWH-backed FODs and build the missing ones with `nix build`.

`<input>` may be either:

- a Nix installable previously checked (results are read from the checkpoint), or
- a path to an already-generated `swh-backed-fods.nix` file.

When `<input>` is an installable, the command first generates the expression and writes it to the path given by `-o`/`--output` (default `swh-backed-fods.nix`). When `<input>` is a `.nix` file, that file is used directly; `-o`/`--output` cannot be used in that case.

The command then:

1. Lists the attribute names defined in the Nix file.
2. Runs `nix build --dry-run -f <file> <attrs> --json` to determine output paths and which derivations would be built locally versus fetched from a substituter.
3. Checks which output paths are already in the local Nix store.
4. For any missing vault-backed FODs whose derivation would be built locally, verifies that the corresponding vault flat archives are cooked on Software Heritage. FODs that would be fetched from a substituter do not require vault cooking. FODs whose vault archive is not cooked are skipped with a warning instead of failing the command.
5. Builds the remaining missing attributes with `nix build -f <file> <attrs>`.

If all outputs are already in the store or only uncooked vault archives remain, the command reports this and exits `0` without building.

#### `build-swh-fods` options

| Option | Default | Description |
|--------|---------|-------------|
| `-o`, `--output` `<path>` | `swh-backed-fods.nix` | Path to write the generated expression when `<input>` is an installable. Not compatible with providing a `.nix` file as `<input>`. |
| `--checkpoint-file` `<path>` | per-installable cache file | Checkpoint to read results from when `<input>` is an installable. Not compatible with providing a `.nix` file as `<input>`. |
| `--nix-build-arg` `<arg>` | none | Extra argument to pass to `nix build`. May be given multiple times. |
| `--no-substitute` | false | Do not use substituters when building. Passed to both the dry run and the real build. This means that all the missing outputs must be built from the Software Heritage source rather than fetched from a Nix cache. |
| `--swh-api-url` `<url>` | `https://archive.softwareheritage.org/api/1` | Base URL of the Software Heritage API. |
| `--swh-api-token` `<token>` | value of `SWH_API_TOKEN` | Bearer token for authenticated requests. |
| `--min-delay` `<seconds>` | `1.0` | Minimum delay between anonymous API requests. |
| `-q`, `--quiet` | false | Suppress stderr progress messages. |

#### `build-swh-fods` exit codes

| Code | Meaning |
|------|---------|
| `0` | Success, or all outputs were already in the store, or there were no SWH-backed FODs to build. |
| `1` | A `nix` command failed, a Software Heritage API error occurred, or the checkpoint could not be read. |
| `2` | Incompatible options were given. |

---

## Checkpoint file

The checkpoint file is a JSON document used to resume interrupted `check` runs and to feed results into `generate-swh-fods`, `cook-swh-fods`, `build-swh-fods`, and `request-archiving`.

### Default location

The default checkpoint path is:

```
$XDG_CACHE_HOME/nix-fod-swh-checker/<sha256(installable)[:16]>.json
```

If `XDG_CACHE_HOME` is unset, `$HOME/.cache` is used.

### Format

```json
{
  "installable": "<installable>",
  "results": {
    "<label>": {
      "fod": { "drv_path": "...", "output_name": "...", ... },
      "known": true,
      "method": "content_hash",
      "detail": "...",
      "swhid": "...",
      "swh_url": "...",
      "disarchive_spec": "...",
      "disarchive_swhid": "...",
      "disarchive_top_dir": "...",
      "origin_urls": ["..."]
    }
  }
}
```

The checkpoint is written atomically: a temporary file is created in the same directory and renamed into place. This means the file is never observed in a partially-written state.

Each result object in `results` has the same fields as a JSON output result object, except that the `fod` field is a [FOD object](#fod-object) and the `label` key is the object key rather than a field inside the object.

### Loading behavior

- If the checkpoint file does not exist, loading returns an empty result set and checking proceeds normally.
- If the checkpoint file exists but cannot be parsed as JSON, loading returns an empty result set and a new checkpoint is written over the invalid file.
- Results are keyed by FOD label. If the derivation graph changes between runs, only labels that still exist are used; obsolete entries are dropped when the checkpoint is next saved.

---

## FOD object

Both the `check` JSON output and checkpoint files describe each checked FOD with the same object structure:

| Field | Type | Description |
|-------|------|-------------|
| `drv_path` | string | Store path of the `.drv` file. |
| `output_name` | string | Name of the derivation output (e.g. `out`). |
| `output_path` | string or `null` | Store path of the output, if present in the derivation metadata. |
| `name` | string | Derivation `name` field. |
| `method` | string or `null` | Content-addressing method (`flat`, `nar`, `git`, or `null`). |
| `hash_algo` | string or `null` | Hash algorithm reported by Nix. |
| `hash_hex` | string or `null` | Fixed output hash. |
| `label` | string | Computed label `<drv-path>` or `<drv-path>^<output-name>` for non-`out` outputs. |
| `origin_urls` | list of strings | Upstream origin URLs extracted from the derivation environment. Empty when no URLs are declared. |

`label` is a computed property. In the `check` JSON output it is included as a field of the `fod` object for convenience. In checkpoint files it is the key of the `results` object, so it is not repeated inside the `fod` object. When JSON output is read back by `generate-swh-fods -i`, the label is recomputed from the other fields and any redundant `label` field is ignored.

---

## Origin URLs

When `nix derivation show` emits a FOD, the derivation environment usually contains the upstream URL(s) from which the content is downloaded. The checker extracts these from the `url` and `urls` environment variables:

- `env.url` — a single origin URL.
- `env.urls` — a whitespace-separated list of mirror URLs.

These URLs are stored in the `origin_urls` field of each result and in checkpoint files. They are used by `request-archiving` to ask Software Heritage to archive origins that are not yet in the archive.

Not every FOD has a usable origin URL. Some FODs are produced by complex build steps, and their environment contains no `url`/`urls` variable. `request-archiving` simply skips such FODs.

---

## Rate limiting

Software Heritage rate-limits anonymous API requests. The client behaves as follows:

- When no API token is provided, at least `--min-delay` seconds (default `1.0`) are waited between consecutive requests.
- When an API token is provided, no artificial delay is inserted.
- HTTP `429` responses are retried after the time indicated by `X-RateLimit-Reset` (a Unix timestamp), with a fallback of `min-delay * 2`.
- Failed requests are retried up to `3` times with exponential backoff.
- When the `X-RateLimit-Remaining` header drops to `3` or below, a warning is printed to stderr.
- Every API request has a hard timeout of `20` seconds.

---

## Environment variables

| Variable | Used by | Description |
|----------|---------|-------------|
| `SWH_API_TOKEN` | all subcommands that talk to SWH | Default bearer token for Software Heritage API requests. Overridden by `--swh-api-token`. |
| `XDG_CACHE_HOME` | `check`, `generate-swh-fods`, `cook-swh-fods`, `build-swh-fods`, `request-archiving` | Base directory for the default checkpoint file. |
| `HOME` | checkpoint code | Fallback for `XDG_CACHE_HOME`. |
