# nix-archive-src

**Preserve your Nix builds forever by archiving sources on Software Heritage**

## What is this?

This tool helps you ensure that Nix builds can be reproduced indefinitely, even if the original upstream sources disappear.

This tool helps you ensure that Nix builds can be reproduced indefinitely, even if the original upstream sources disappear. It does this by:

1. **Checking** whether all the source code your Nix builds depend on is already preserved in [Software Heritage](https://www.softwareheritage.org/), a universal archive of public software
2. **Requesting archiving** for sources that aren't yet preserved
3. **Generating alternative Nix expressions** that download your sources from Software Heritage instead of the original upstream locations
4. **Building** from those alternative expressions to verify everything still works

## Why does this matter?

Nix is known for exceptional reproducibility, but this only works if:
- You have access to all the source code your builds depend on, or
- You can retrieve it from a binary cache

The problem: upstream sources can disappear (servers go offline, projects get deleted, hosting changes). Long-term reproducibility becomes impossible.

**nix-archive-src solves this** by using Software Heritage as an alternative, permanent source. Your Nix builds can then download from SWH instead, ensuring reproducibility for decades to come.

## Quick start

### Try it without installing

```console
nix run github:Zimmi48/nix-archive-src#nix-archive-src -- check nixpkgs#hello
```

### Install globally

```console
nix profile install github:Zimmi48/nix-archive-src
```

### Enter development environment

```console
nix develop github:Zimmi48/nix-archive-src
```

## Typical workflow

### 1. Check your sources against Software Heritage

```console
nix-archive-src check nixpkgs#hello
```

### 2. Request archiving of missing sources

```console
nix-archive-src request nixpkgs#hello
```

### 3. Generate SWH-backed Nix expressions

```console
nix-archive-src generate nixpkgs#hello -o swh-backed-hello.nix
```

### 4. Cook vault archives on SWH (if needed)

```console
nix-archive-src cook swh-backed-hello.nix
```

### 5. Build from archived sources

```console
nix-archive-src build swh-backed-hello.nix
```

## Commands reference

| Command | Purpose |
|---------|---------|
| `check` | Discover FODs and check if sources are archived |
| `request` | Request archiving of missing sources on Software Heritage |
| `generate` | Create Nix expressions using SWH as source |
| `cook` | Request on-demand processing of vault archives |
| `build` | Build using SWH-backed expressions |

For detailed documentation, see:
- [docs/specification.md](docs/specification.md) — complete command reference with all options
- [docs/internals.md](docs/internals.md) — algorithms, data models, and implementation details

## Development

Enter development shell with all dependencies:

```console
nix develop
```

Run tests:

```console
pytest
```

Full build including tests:

```console
nix build
```

## How it works in detail

The tool runs `nix derivation show --recursive <installable>` to discover every FOD in the dependency graph. For each FOD, it picks the most direct comparison strategy:

- **`git`** hashes → compared as git object IDs via SWHID batch lookup
- **`flat`** hashes → compared as raw content checksums via SWH content endpoint
- **`nar`** and others → FOD is realized, its SWHID computed with `swh identify`, then looked up
- **Archives** → first queries GNU Guix disarchive database by FOD hash (fast cache), falls back to locally capturing archive metadata with `disarchive` if needed

See [docs/internals.md](docs/internals.md) for the complete algorithm.

## References

This tool would not be possible without:

- [Software Heritage](https://www.softwareheritage.org/) — universal archive of public code
- [Guix disarchive](https://guix.gnu.org/manual/devel/en/Bootstrapping-Guix.html#Substitutes) — database and tools for unpacking archived sources
- Research on Nix reproducibility

## Notes

This tool was developed by Théo Zimmermann with extensive use of generative AI. All documentation and CI files have been thoroughly reviewed. The code and tests are usable as-is, but can also be considered a prototype for a more extensively human-engineered version if desired.

## License

[MIT](LICENSE)
