"""Cross-implementation consistency tests for FOD extraction.

These tests exercise ``nix derivation show --recursive`` against real Nix
expressions and assert that the parser normalizes the various serialization
styles used by upstream Nix, Lix, and Determinate Nix to the same FOD
metadata.

Run with a specific Nix binary via the ``NIX_BINARY`` environment variable.
"""

import os
import pathlib
import shutil

import pytest

from nix_archive_src.nix import (
    iter_fixed_output_derivations,
    show_derivations_recursive,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
NIX_BINARY = os.environ.get("NIX_BINARY", "nix")


@pytest.mark.skipif(
    shutil.which(NIX_BINARY) is None,
    reason=f"'{NIX_BINARY}' executable not found",
)
@pytest.mark.parametrize(
    "name,method,hash_algo,hash_hex",
    [
        ("flat-hex", "flat", "sha256", "0" * 64),
        # SRI base64 payload decodes to the same 32 zero bytes as the hex case.
        ("flat-sri", "flat", "sha256", "0" * 64),
        ("nar-recursive", "nar", "sha256", "0" * 64),
    ],
)
def test_fod_fixture_normalizes(name, method, hash_algo, hash_hex):
    """Each fixture FOD normalizes to the expected method/algo/hash."""
    installable = f"{REPO_ROOT}#fod-fixtures.{name}"
    derivations = show_derivations_recursive(installable, nix_binary=NIX_BINARY)
    fods = list(iter_fixed_output_derivations(derivations))

    assert len(fods) == 1, f"expected one FOD, got: {[f.name for f in fods]}"
    fod = fods[0]
    assert fod.method == method
    assert fod.hash_algo == hash_algo
    assert fod.hash_hex == hash_hex
