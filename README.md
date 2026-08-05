# nix-fod-swh-checker

List every [fixed-output derivation](https://nix.dev/manual/nix/stable/language/advanced-attributes#adv-attr-outputHash) (FOD) reachable from a Nix attribute, and check whether each one's source is already archived on [Software Heritage](https://www.softwareheritage.org/).

## How it works

1. Run `nix derivation show --recursive <installable>` and parse the resulting JSON graph of derivations.
2. Walk every derivation's `outputs`; an output is a FOD whenever it has a `hash` field (i.e. it's content-addressed with a fixed, expected hash).
3. For each FOD, pick the best available comparison strategy against the Software Heritage archive, depending on its content-addressing `method`:
   - **`git`**: Nix hashes the output the same way git hashes blobs/trees, so the hash is directly checked against Software Heritage via a batch [`/known/`](https://docs.softwareheritage.org/devel/swh-web/api/) SWHID lookup (tried as both `swh:1:cnt:...` and `swh:1:dir:...`).
   - **`flat`**: the hash is a plain checksum of the raw downloaded bytes, which maps directly onto Software Heritage's [`/content/{algo}:{hash}/`](https://docs.softwareheritage.org/devel/swh-web/api/) endpoint.
   - **Anything else** (most commonly `nar`, used by `fetchurl`/`fetchzip`-style directory outputs): the hash is computed over the [Nix Archive (NAR)](https://nix.dev/manual/nix/stable/store/file-system-object/content-address#serial-nix-archive) serialization, which has no Software Heritage equivalent, so there is no way to compare it directly. Instead of guessing, the tool:
     1. realises the FOD with `nix build --no-link --print-out-paths <drv>^<output>`, which fetches it from a binary cache (e.g. `cache.nixos.org`) whenever possible instead of rebuilding it from scratch;
     2. computes the resulting path's actual [SWHID](https://docs.softwareheritage.org/devel/swh-model/persistent-identifiers.html) using the reference [`swh identify`](https://docs.softwareheritage.org/devel/swh-model/cli.html) tool;
     3. looks up that exact SWHID via the `/known/` endpoint.
   - If none of the above apply, the FOD is reported as **undetermined** (e.g. the build or the `swh identify` call itself failed).

No heuristics or guessing are involved: every result is either a direct hash comparison, or based on the actual archived content's own computed identifier.

## Installation

This project is packaged as a [flake](https://nix.dev/concepts/flakes), and requires no Python installation of its own.

```console
nix run .#nix-fod-swh-check -- nixpkgs#hello
```

or install it into your profile:

```console
nix profile install .#nix-fod-swh-checker
```

## Usage

```console
nix run .#nix-fod-swh-check -- nixpkgs#hello
```

```console
[KNOWN] /nix/store/....drv
    method=build_and_identify: built /nix/store/....-hello-2.10 and computed swh:1:dir:d198bc9d7a6bcf6db04f476d29314f157507d505
    https://archive.softwareheritage.org/swh:1:dir:d198bc9d7a6bcf6db04f476d29314f157507d505

1 FOD(s) checked: 1 known, 0 unknown, 0 undetermined
```

Useful options:

- `--json` — print machine-readable JSON instead of the human-readable report.
- `--only-unknown` — only report FODs that are not known to Software Heritage (or undetermined).
- `--swh-api-token` / `SWH_API_TOKEN` — authenticate to the Software Heritage API to raise rate limits.
- `--min-delay` — minimum delay (seconds) between Software Heritage API requests (default `1.0`), to stay within the anonymous rate limit.
- `--nix-binary` — path to a specific `nix` executable.
- `--swh-binary` — path to a specific `swh` executable (providing `swh identify`); the flake's package wraps this automatically.

Any installable accepted by `nix derivation show` works, e.g. a flake reference (`nixpkgs#hello`), an attribute path (`-f '<nixpkgs>' hello`, passed via extra `nix` invocation), or a store path.

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
