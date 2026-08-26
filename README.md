# nix-fod-swh-checker

[Nix](https://nixos.org/) gives you very high level of reproducibility [[1,2,3]](#references), but this only works if you still have the sources of the packages you want to build (and their dependencies) or if you can get them through a binary cache.

The goal of this tool is to enable longer-term preservation, without reliance on binary caches and unstable sources that may disappear, by checking that all the sources that you need, i.e., every [fixed-output derivation](https://nix.dev/manual/nix/stable/language/advanced-attributes#adv-attr-outputHash) (FOD) reachable from a Nix attribute, are archived on [Software Heritage](https://www.softwareheritage.org/) (SWH), the universal archive of public code.

For FODs that are archived, the tool can also generate alternative derivations that download the same content from Software Heritage, allowing a Nix build to succeed even when the original upstream sources are unavailable.

The idea is that this tool can be used when someone (for instance, a researcher who cares about long-term reproducibility of their work) wants to ensure that a Nix build will continue to work in the future even if the sources disappeared and the NixOS cache was wiped (even more relevant when building things that are not in the cache).

This tool would not be possible without the existence of Software Heritage [[4]](#references) and of the `disarchive` [tool](https://ngyro.com/software/disarchive.html) and [database](https://disarchive.guix.gnu.org/) [[5]](#references), which were created in the context of the Guix project.

*Note:* This tool was developed by Théo Zimmermann with extensive use of generative AI. All documentation (and CI) files have been reviewed extensively, but code and tests have not. The tool is usable as is, but it can also be considered as a prototype if someone wishes to produce (and maintain!) a more human-engineered version.

### References

- [1] Julien Malka, Stefano Zacchiroli, and Théo Zimmermann. "Reproducibility of build environments through space and time." Proceedings of the 2024 ACM/IEEE 44th International Conference on Software Engineering: New Ideas and Emerging Results. 2024.
- [2] Julien Malka, Stefano Zacchiroli, and Théo Zimmermann. "Does Functional Package Management Enable Reproducible Builds at Scale? Yes." 2025 IEEE/ACM 22nd International Conference on Mining Software Repositories (MSR). IEEE, 2025.
- [3] Julien Malka, Stefano Zacchiroli, and Théo Zimmermann. "A Decade of Software Reproducibility in the Nix Package Ecosystem." (2026).
- [4] Di Cosmo, Roberto, and Stefano Zacchiroli. "Software heritage: Why and how to preserve software source code." iPRES 2017-14th International Conference on Digital Preservation. 2017.
- [5] Ludovic Courtès, Timothy Sample, Stefano Zacchiroli, and Simon Tournier. "Source code archiving to the rescue of reproducible deployment." Proceedings of the 2nd ACM Conference on Reproducibility and Replicability. 2024.

## How it works

The tool runs `nix derivation show --recursive <installable>` to discover every FOD in the dependency graph. For each FOD, it picks the most direct comparison strategy against Software Heritage:

- **`git`** hashes are compared as git object IDs via SWHID batch lookup.
- **`flat`** hashes are compared as raw content checksums via the SWH `/content/` endpoint.
- **`nar`** and other methods have no direct SWH equivalent, so the FOD is realised, its actual SWHID is computed with `swh identify`, and that SWHID is looked up.
- For archives that are not themselves archived but whose unpacked contents are, the tool first queries the GNU Guix `disarchive` database by the FOD's hash, and falls back to locally capturing the archive metadata with `disarchive` if needed. It reports the result as **known after disarchive**.

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
[UNKNOWN] /nix/store/0nrb3h17g2hhf8ijisi7frcfvqwhya3w-coreutils-9.11.tar.xz.drv
    method=known_after_disarchive: unpacked /nix/store/p59cz49l5faa67c72wxdql3jfj9gy2fn-coreutils-9.11.tar.xz and computed swh:1:dir:dae7aff18109b2d9b63aff073f6d557a44835450; contents not known
[KNOWN] /nix/store/0p6bbqs599hggq6xvxn67x91pxszgqil-bison-3.8.2.tar.xz.drv
    method=content_hash: content lookup by sha256:9bba0214ccf7f1079c5d59210045227bcf619519840ebfa80cd3849cff5a5bf2
    https://archive.softwareheritage.org/swh:1:cnt:508b4a78e162c90fa0ee6346778476c285c9b764
[KNOWN AFTER DISARCHIVE] /nix/store/1bxagw21mzw0pr7y9jcyjfyrlya5fnxz-hello-2.12.3.tar.gz.drv
    method=known_after_disarchive: disarchive database returned swh:1:dir:5866e34da0c9663c2d93103f50c254a1a272ea36
    https://archive.softwareheritage.org/swh:1:dir:5866e34da0c9663c2d93103f50c254a1a272ea36
[KNOWN AFTER DISARCHIVE] /nix/store/1c0za6f5rxl66ch0lgihniwgipfnvfm6-byacc-20260126.tgz.drv
    method=known_after_disarchive: unpacked /nix/store/rjf7864yrw8d9csm3kvfzxgc0ccl8c9j-byacc-20260126.tgz and computed swh:1:dir:e1efad6b896a4a66ef43eb068cbe1771c5e26b6f
    https://archive.softwareheritage.org/swh:1:dir:e1efad6b896a4a66ef43eb068cbe1771c5e26b6f
[KNOWN] /nix/store/zf8dvv680i9z2wpggik614r20n54hyii-hex0-1.9.1.drv
    method=build_and_identify: built /nix/store/k7ilqdwdxx8v3vw6p0c4i06xfm6crygm-hex0-1.9.1 and computed swh:1:cnt:cd4e550fca3320c6f2f97605e238c222ae623621
    https://archive.softwareheritage.org/swh:1:cnt:cd4e550fca3320c6f2f97605e238c222ae623621

5 FOD(s) checked: 2 known, 2 known after disarchive, 1 unknown, 0 undetermined
```

A Software Heritage API token is strongly recommended. Anonymous requests are heavily rate-limited. Set `SWH_API_TOKEN` or pass `--swh-api-token`.

Progress messages go to stderr; suppress them with `--quiet`/`-q`. Results are checkpointed as they are computed, so an interrupted run can be resumed by re-running the same command. Use `--no-checkpoint` to disable this, or `--checkpoint-file` to choose an explicit location.

A shared cache is also maintained across installables to avoid repeating Software Heritage API requests and expensive tool runs (`swh identify` and `disarchive`). Positive responses and successful tool runs are cached forever; negative API responses are cached for one day and ignored when `--retry-unknown` or `--retry-undetermined` is given. Use `--no-cache` to disable the shared cache, or `--cache-file` to choose an explicit location.

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

Already-built outputs are skipped. Use `--no-substitute` to build from (Software Heritage) sources instead of using the NixOS cache. If a required vault archive is not cooked, the corresponding FOD is skipped with a warning and you should run `cook-swh-fods` first.

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
