{ system }:

{
  # Flat-hashed FOD with a plain hex hash.
  # Lix emits this combination unchanged.
  flat-hex = builtins.derivation {
    name = "flat-hex";
    inherit system;
    builder = "builtin:fetchurl";
    outputHashMode = "flat";
    outputHashAlgo = "sha256";
    outputHash = "0000000000000000000000000000000000000000000000000000000000000000";
    url = "https://example.com/flat-hex";
  };

  # Flat-hashed FOD with an SRI hash.
  # Determinate Nix prefers this representation for flat hashes.
  # The base64 payload AAAAAA... decodes to 32 zero bytes, so the normalized
  # hash_hex must equal the flat-hex case above.
  flat-sri = builtins.derivation {
    name = "flat-sri";
    inherit system;
    builder = "builtin:fetchurl";
    outputHashMode = "flat";
    outputHash = "sha256-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=";
    url = "https://example.com/flat-sri";
  };

  # Recursive-hashed FOD with an SRI hash.
  # Some Nix implementations emit method="recursive" or hashAlgo="r:sha256"
  # for this case; the parser must normalize all of those to method="nar"
  # and hash_algo="sha256".
  nar-recursive = builtins.derivation {
    name = "nar-recursive";
    inherit system;
    builder = "builtin:fetchurl";
    outputHashMode = "recursive";
    outputHash = "sha256-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=";
    url = "https://example.com/nar-recursive";
  };
}
