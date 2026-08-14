# nix-fod-swh-checker

List every [fixed-output derivation](https://nix.dev/manual/nix/stable/language/advanced-attributes#adv-attr-outputHash) (FOD) reachable from a Nix attribute, and check whether each one's source is already archived on [Software Heritage](https://www.softwareheritage.org/) (SWH).

For FODs that are archived, the tool can also generate alternative derivations that download the same content from Software Heritage, allowing a Nix build to succeed even when the original upstream sources are unavailable.

The idea is that this tool can be used when someone (for instance, a researcher who cares about long-term reproducibility of their work) wants to ensure that a Nix build will continue to work in the future even if the sources disappeared and the NixOS cache was wiped (even more relevant when building things that are not in the cache).

## How it works

The tool runs `nix derivation show --recursive <installable>` to discover every FOD in the dependency graph. For each FOD, it picks the most direct comparison strategy against Software Heritage:

- **`git`** hashes are compared as git object IDs via SWHID batch lookup.
- **`flat`** hashes are compared as raw content checksums via the SWH `/content/` endpoint.
- **`nar`** and other methods have no direct SWH equivalent, so the FOD is realised, its actual SWHID is computed with `swh identify`, and that SWHID is looked up.
- For archives that are not themselves archived but whose unpacked contents are, the tool uses GNU Guix `disarchive` to capture the archive metadata and reports the result as **known after disarchive**.

No heuristics or guessing are involved: every result is either a direct hash comparison or based on the actual archived content's own computed identifier.

See [docs/internals.md](docs/internals.md) for the full algorithm and [docs/specification.md](docs/specification.md) for an exhaustive command reference.

## Installation

This project is packaged as a [flake](https://nix.dev/concepts/flakes).

Run without installing:

```console
nix run .#nix-fod-swh-check -- check nixpkgs#hello
```

Install into your profile:

```console
nix profile install .#nix-fod-swh-checker
```

## Standard usage

### 1. Check FODs against Software Heritage

```console
nix run .#nix-fod-swh-check -- check nixpkgs#hello
```

Typical output:

```console
[KNOWN] /nix/store/....drv
    method=build_and_identify: built /nix/store/....-hello-2.10 and computed swh:1:dir:d198bc9d7a6bcf6db04f476d29314f157507d505
    https://archive.softwareheritage.org/swh:1:dir:d198bc9d7a6bcf6db04f476d29314f157507d505

1 FOD(s) checked: 1 known, 0 known after disarchive, 0 unknown, 0 undetermined
```

A Software Heritage API token is strongly recommended. Anonymous requests are heavily rate-limited. Set `SWH_API_TOKEN` or pass `--swh-api-token`.

Progress messages go to stderr; suppress them with `--quiet`/`-q`. Results are checkpointed as they are computed, so an interrupted run can be resumed by re-running the same command. Use `--no-checkpoint` to disable this, or `--checkpoint-file` to choose an explicit location.

To write machine-readable JSON results to a file in addition to the human-readable report:

```console
nix run .#nix-fod-swh-check -- check nixpkgs#hello -o results.json
```

Use `--quiet`/`-q` to suppress the human-readable report as well as progress messages.

### 2. Request the archiving of missing sources

For FODs that are not yet archived on Software Heritage, you can ask SWH to archive their upstream origins:

```console
nix run .#nix-fod-swh-check -- request-archiving nixpkgs#hello
```

The command reads the checkpoint produced by `check` and considers each unknown FOD. For FODs that declare `url`/`urls` in their derivation environment, it probes each URL with a `HEAD` request and keeps the first one that responds. This handles mirror lists while avoiding save requests for dead origins. The visit type is inferred from the FOD method (`git` for git-hashed FODs, `tarball` otherwise). Use `--dry-run` to preview the origins that would be requested.

FODs with no usable URL, or whose URLs are all unreachable, are skipped with a warning. FODs produced by complex build steps typically fall into this category.

After requesting archiving and giving some time for the archiving to complete, you may re-run `check --retry-unknown` to check whether the previously unknown FODs are now available in Software Heritage.

### 3. Generate SWH-backed FODs

```console
nix run .#nix-fod-swh-check -- generate-swh-fods nixpkgs#hello -o swh-backed-fods.nix
```

This produces a Nix expression that builds SWH-backed derivations for every known FOD. Building them populates the Nix store with the exact store paths the original derivation would have produced.

This command requires a checkpoint file in the standard location or the JSON results file from the previous step (use `-i` / `--json-input` in this case).

### 4. Cook required vault archives

Directory SWHIDs are fetched as vault flat bundles, which may need to be cooked on demand by Software Heritage. Submit cooking requests (or check existing ones) and exit:

```console
nix run .#nix-fod-swh-check -- cook-swh-fods swh-backed-fods.nix
```

### 5. Build the SWH-backed FODs

```console
nix run .#nix-fod-swh-check -- build-swh-fods swh-backed-fods.nix
```

Already-built outputs are skipped. Use `--no-substitute` to build from (Software Heritage) sources instead of using the NixOS cache. If a required vault archive is not cooked, the command fails and tells you to run `cook-swh-fods` first.

## Documentation

- [docs/specification.md](docs/specification.md) — exhaustive command reference, options, exit codes, and file formats.
- [docs/internals.md](docs/internals.md) — algorithms, data models, and implementation details.

## Development

Enter the dev shell (or let direnv do it via `.envrc`):

```console
nix develop
pytest
```

Run the full build, including the test suite, with:

```console
nix flake check
```
