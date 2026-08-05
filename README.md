# nix-fod-swh-checker

List every [fixed-output derivation](https://nix.dev/manual/nix/stable/language/advanced-attributes#adv-attr-outputHash) (FOD) reachable from a Nix attribute, and check whether each one's source is already archived on [Software Heritage](https://www.softwareheritage.org/).

## How it works

1. Run `nix derivation show --recursive <installable>` and parse the resulting JSON graph of derivations.
2. Walk every derivation's `outputs`; an output is a FOD whenever it has a `hash` field (i.e. it's content-addressed with a fixed, expected hash).
3. For each FOD, pick the best available comparison strategy against the Software Heritage archive, depending on its content-addressing `method`:
   - **`git`**: Nix hashes the output the same way git hashes blobs/trees, so the hash is directly checked against Software Heritage via a batch [`/known/`](https://docs.softwareheritage.org/devel/swh-web/api/) SWHID lookup (tried as both `swh:1:cnt:...` and `swh:1:dir:...`).
   - **`flat`**: the hash is a plain checksum of the raw downloaded bytes, which maps directly onto Software Heritage's [`/content/{algo}:{hash}/`](https://docs.softwareheritage.org/devel/swh-web/api/) endpoint.
   - **`nar`** (the common case for `fetchurl`/`fetchzip`-style directory outputs): the hash is computed over the [Nix Archive (NAR)](https://nix.dev/manual/nix/stable/store/file-system-object/content-address#serial-nix-archive) serialization, which has no Software Heritage equivalent. There is no way to directly compare such a hash, so instead the tool looks up the FOD's source URL(s) (from the derivation's `url`/`urls` environment variables) as a Software Heritage **origin**.
   - If none of the above apply and no source URL is available, the FOD is reported as **undetermined**.

Because of this, a result of "unknown" for a `nar`-hashed FOD means "we could not find this exact source URL archived" rather than "we proved the content itself is missing" — Software Heritage could still hold the same bytes under a different origin.

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
    method=origin_url: origin https://ftp.gnu.org/gnu/hello/hello-2.10.tar.gz has been archived
    https://archive.softwareheritage.org/browse/origin/?origin_url=https://ftp.gnu.org/gnu/hello/hello-2.10.tar.gz

1 FOD(s) checked: 1 known, 0 unknown, 0 undetermined
```

Useful options:

- `--json` — print machine-readable JSON instead of the human-readable report.
- `--only-unknown` — only report FODs that are not known to Software Heritage (or undetermined).
- `--swh-api-token` / `SWH_API_TOKEN` — authenticate to the Software Heritage API to raise rate limits.
- `--min-delay` — minimum delay (seconds) between Software Heritage API requests (default `1.0`), to stay within the anonymous rate limit.
- `--nix-binary` — path to a specific `nix` executable.

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
