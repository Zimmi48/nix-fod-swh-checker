{ pkgs ? import <nixpkgs> { } }:

pkgs.mkShell {
  # nativeBuildInputs is usually what you want -- tools you need to run
  nativeBuildInputs = with pkgs; [
    nixpkgs-fmt

    (python3.withPackages (ps: [
      ps.requests
      ps.pytest
    ]))
  ];

  shellHook = ''
    export PYTHONPATH="$PWD/src:$PYTHONPATH"
  '';
}
