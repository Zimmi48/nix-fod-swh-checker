# nix-fod-swh-checker

List every [fixed-output derivation](https://nix.dev/manual/nix/stable/language/advanced-attributes#adv-attr-outputHash) (FOD) reachable from a Nix attribute, and check whether each one's source is already archived on [Software Heritage](https://www.softwareheritage.org/).

## How it works

1. Run `nix derivation show --recursive <installable>` and parse the resulting JSON graph of derivations.
2. Walk every derivation's `outputs`; an output is a FOD whenever it has a `hash` field (i.e. it's content-addressed with a fixed, expected hash).
3. For each FOD, pick the best available comparison strategy against the Software Heritage archive, depending on its content-addressing `method`:
   - **`git`**: Nix hashes the output the same way git hashes blobs/trees, so the hash is directly checked against Software Heritage via a batch [`/known/`](https://docs.softwareheritage.org/devel/swh-web/api/) SWHID lookup (tried as both `swh:1:cnt:...` and `swh:1:dir:...`).
   - **`flat`**: the hash is a plain checksum of the raw downloaded bytes, which maps directly onto Software Heritage's [`/content/{algo}:{hash}/`](https://docs.softwareheritage.org/devel/swh-web/api/) endpoint.
   - For **`git`** and **`flat`** FODs whose content is not directly known, the tool also tries to realise the output, unpack it as a standard archive (tar or zip), and look up the `swh:1:dir:` identifier of the unpacked contents. This catches archives that are not themselves archived on Software Heritage but whose contents are. When the unpacked directory is known, the result is reported as **known after disarchive**.
   - **Anything else** (most commonly `nar`, used by `fetchurl`/`fetchzip`-style directory outputs): the hash is computed over the [Nix Archive (NAR)](https://nix.dev/manual/nix/stable/store/file-system-object/content-address#serial-nix-archive) serialization, which has no Software Heritage equivalent, so there is no way to compare it directly. Instead of guessing, the tool:
     1. realises the FOD with `nix build --no-link --print-out-paths <drv>^<output>`, which fetches it from a binary cache (e.g. `cache.nixos.org`) whenever possible instead of rebuilding it from scratch;
     2. computes the resulting path's actual [SWHID](https://docs.softwareheritage.org/devel/swh-model/persistent-identifiers.html) using the reference [`swh identify`](https://docs.softwareheritage.org/devel/swh-model/cli.html) tool;
     3. looks up that exact SWHID via the `/known/` endpoint.
   - If none of the above apply, the FOD is reported as **undetermined** (e.g. the build or the `swh identify` call itself failed).

No heuristics or guessing are involved: every result is either a direct hash comparison, or based on the actual archived content's own computed identifier.

## Installation

This project is packaged as a [flake](https://nix.dev/concepts/flakes), and requires no Python installation of its own.

```console
nix run .#nix-fod-swh-check -- check nixpkgs#hello
```

or install it into your profile:

```console
nix profile install .#nix-fod-swh-checker
```

## Usage

```console
nix run .#nix-fod-swh-check -- check nixpkgs#hello
```

```console
[KNOWN] /nix/store/....drv
    method=build_and_identify: built /nix/store/....-hello-2.10 and computed swh:1:dir:d198bc9d7a6bcf6db04f476d29314f157507d505
    https://archive.softwareheritage.org/swh:1:dir:d198bc9d7a6bcf6db04f476d29314f157507d505

1 FOD(s) checked: 1 known, 0 known after disarchive, 0 unknown, 0 undetermined
```

A Software Heritage API token is strongly recommended. Anonymous requests are heavily rate-limited and may fail for large installables. Set `SWH_API_TOKEN` or pass `--swh-api-token` to `check`.

Checking FODs can be slow -- realising a `nar`-hashed FOD may need to download it, and the Software Heritage API is rate-limited -- so progress messages (what's being listed/built/queried, and rate-limit waits) are printed to stderr as the tool runs. Pass `--quiet`/`-q` to suppress them.

Results are also checkpointed to disk as each FOD is checked (by default under `$XDG_CACHE_HOME/nix-fod-swh-checker/`, in a file named after a hash of the installable). If the tool is interrupted or crashes, simply re-run the same command: already-checked FODs are loaded from the checkpoint and skipped instead of being re-checked. Use `--checkpoint-file` to pick an explicit location, or `--no-checkpoint` to disable this entirely.

Pressing Ctrl+C exits cleanly (no Python traceback), reporting how many FODs were checked before the interruption and where the checkpoint was saved, with the conventional `130` exit code.

## Generating SWH-backed FODs

For every FOD that is known to Software Heritage, the tool can generate an alternative fixed-output derivation that downloads the same content from SWH instead of the original upstream URL. Because the output hash is fixed to the same value, building these SWH-backed FODs populates the Nix store with the exact store paths the original derivation would have produced, allowing a subsequent build of the original installable to succeed even when upstream sources are unavailable.

After running the checker, generate a Nix expression containing SWH-backed FODs for all known results:

```console
nix run .#nix-fod-swh-check -- generate-swh-fods nixpkgs#hello -o swh-backed-fods.nix
```

Then build them:

```console
nix build -f swh-backed-fods.nix
```

Or use the `cook-swh-fods` and `build-swh-fods` commands to ensure any required vault flat archives are cooked on Software
Heritage, and build them. Because vault cooking can take a long time, it is
split into a separate command so you can request cooking, come back later, and
then build:

```console
nix run .#nix-fod-swh-check -- cook-swh-fods nixpkgs#hello
# ...wait until cooking is done...
nix run .#nix-fod-swh-check -- build-swh-fods nixpkgs#hello
```

The generated expression handles three cases:

- **Single files** (`method=flat` or `method=git` known as `swh:1:cnt:...`): download the raw bytes from the SWH `/content/` API.
- **Directories** (`method=nar` or `method=git` known as `swh:1:dir:...`): download the SWH vault `flat` bundle (a tarball of the directory) and extract it.
- **Archives known after disarchive**: for archives whose raw bytes are not in SWH but whose unpacked contents are, the tool captures a [GNU Guix `disarchive`](https://ngyro.com/software/disarchive.html) specification while checking. The generated derivation downloads the directory from SWH, reconstructs the exact original archive with `disarchive assemble`, and verifies it against the original flat hash.

The `disarchive` binary is automatically available in the flake's dev shell and wrapped into the packaged application.

### Typical workflow

1. **Check** which FODs reachable from your installable are already archived
   on Software Heritage. This writes a checkpoint file as it goes, so it can
   be interrupted and resumed:

   ```console
   nix run .#nix-fod-swh-check -- check nixpkgs#hello
   ```

2. **Generate** a Nix expression with SWH-backed FODs for every known result:

   ```console
   nix run .#nix-fod-swh-check -- generate-swh-fods nixpkgs#hello -o swh-backed-fods.nix
   ```

3. **Cook** any required Software Heritage vault flat archives. Directory
   SWHIDs (`swh:1:dir:...`) are fetched as pre-generated tarballs from the
   vault, and these tarballs may need to be cooked on demand by the SWH
   infrastructure. This step can take a long time, so `cook-swh-fods` only
   submits the cooking requests (or checks existing ones) and exits
   immediately:

   ```console
   nix run .#nix-fod-swh-check -- cook-swh-fods nixpkgs#hello
   ```

   You can also pass the generated Nix file directly:

   ```console
   nix run .#nix-fod-swh-check -- cook-swh-fods swh-backed-fods.nix
   ```

4. **Build** the SWH-backed FODs to populate the Nix store with the exact
   store paths the original derivation would have produced. The build command
   can read the checkpoint again, or you can pass the generated Nix file:

   ```console
   nix run .#nix-fod-swh-check -- build-swh-fods nixpkgs#hello
   # or
   nix run .#nix-fod-swh-check -- build-swh-fods swh-backed-fods.nix
   ```

   After this, a subsequent build of the original installable can succeed
   even if upstream sources are unavailable, because the required FOD outputs
   are already in the store.

## Commands and options

The top-level command is `nix-fod-swh-check`. It requires an explicit subcommand.

### `check <installable>`

Check every FOD reachable from `<installable>` against Software Heritage.

- `--json` — print machine-readable JSON instead of the human-readable report.
- `--only-unknown` — only report FODs that are not known to Software Heritage (or undetermined).
- `--quiet` / `-q` — suppress the stderr progress messages.
- `--checkpoint-file` — path to the checkpoint file (default: a per-installable file under `$XDG_CACHE_HOME/nix-fod-swh-checker/`).
- `--no-checkpoint` — do not read or write a checkpoint file.
- `--swh-api-token` / `SWH_API_TOKEN` — authenticate to the Software Heritage API to raise rate limits.
- `--min-delay` — minimum delay (seconds) between Software Heritage API requests (default `1.0`), to stay within the anonymous rate limit.
- `--swh-identify-timeout` — timeout (seconds) for computing the SWHID of each realised FOD with `swh identify` (default `30.0`).
- `--disarchive-timeout` — timeout (seconds) for capturing the `disarchive` specification of each archive (default `30.0`).

### `generate-swh-fods <installable>`

Generate a Nix expression with SWH-backed FODs from a previously written checkpoint.

- `-o`, `--output` — path to write the generated expression (default: `swh-backed-fods.nix`).
- `--checkpoint-file` — checkpoint to read results from (default: the same per-installable file used by `check`).

### `cook-swh-fods <input>`

Request the cooking of any Software Heritage vault flat archives required by
the SWH-backed FODs for `<input>`, and exit immediately. `<input>` can be a
Nix installable previously checked (results are read from the checkpoint), or
a path to a generated `swh-backed-fods.nix` file (vault SWHIDs are extracted
from the expression).

- `--checkpoint-file` — checkpoint to read results from when `<input>` is an installable (default: the same per-installable file used by `check`).
- `--swh-api-url` — base URL of the Software Heritage API.
- `--swh-api-token` / `SWH_API_TOKEN` — authenticate to the Software Heritage API to raise rate limits.
- `--min-delay` — minimum delay (seconds) between Software Heritage API requests (default `1.0`).
- `--quiet` / `-q` — suppress progress messages while cooking.

### `build-swh-fods <input>`

Generate a Nix expression with SWH-backed FODs and build it with `nix build`.
Vault archives must already be cooked (use `cook-swh-fods` first if needed).
`<input>` can be a Nix installable previously checked, or a path to an
already-generated `swh-backed-fods.nix` file.

- `-o`, `--output` — path to write the generated expression when `<input>` is an installable (default: `swh-backed-fods.nix`).
- `--checkpoint-file` — checkpoint to read results from when `<input>` is an installable (default: the same per-installable file used by `check`).
- `--nix-build-arg` — extra argument to pass to `nix build` (can be given multiple times).
- `--quiet` / `-q` — suppress progress messages while building.

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
