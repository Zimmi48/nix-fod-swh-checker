{ pkgs }:
pkgs.stdenv.mkDerivation {
  name = "cache-warmer";
  nativeBuildInputs = [ pkgs.disarchive pkgs.curl pkgs.cacert pkgs.gnutar pkgs.bzip2 pkgs.xz ];
  buildCommand = "mkdir -p $out";
}
