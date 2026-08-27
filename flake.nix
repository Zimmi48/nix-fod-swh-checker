{
  description = "Check whether Nix sources are archived on Software Heritage and generate alternative derivations using those archives";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = import nixpkgs { inherit system; };

        nix-archive-src = pkgs.python3Packages.buildPythonApplication {
          pname = "nix-archive-src";
          version = "0.1.0";
          format = "pyproject";
          src = ./.;

          nativeBuildInputs = [ pkgs.python3Packages.hatchling pkgs.makeWrapper ];
          propagatedBuildInputs = [ pkgs.python3Packages.requests ];

          nativeCheckInputs = [
            pkgs.python3Packages.pytestCheckHook
            pkgs.swh
            pkgs.disarchive
          ];
          pythonImportsCheck = [ "nix_archive_src" ];

          makeWrapperArgs = [
            "--prefix" "PATH" ":" "${pkgs.swh}/bin"
            "--prefix" "PATH" ":" "${pkgs.disarchive}/bin"
          ];

          meta = {
            description = "Check whether Nix sources are archived on Software Heritage and generate alternative derivations using those archives";
            mainProgram = "nix-archive-src";
          };
        };
      in
      {
        packages.default = nix-archive-src;
        packages.nix-archive-src = nix-archive-src;
        # Re-export the locked nixpkgs input's hello package so the
        # integration job can use a reproducible, pinned installable.
        packages.hello = nixpkgs.legacyPackages.${system}.hello;
        # Empty derivation used to warm the Nix store cache in CI.
        packages.cache-warmer = import ./nix/cache-warmer.nix { inherit pkgs; };

        # Fixed-output derivations used to test parser normalization across
        # Nix implementations.  These derivations are never built; they only
        # need to evaluate successfully.
        legacyPackages.fod-fixtures =
          let
            fixtures = import ./tests/fixtures/fods.nix { inherit system; };
          in
          {
            flat-hex = fixtures.flat-hex;
            flat-sri = fixtures.flat-sri;
            nar-recursive = fixtures.nar-recursive;
          };

        checks.default = nix-archive-src;

        apps.default = {
          type = "app";
          program = "${nix-archive-src}/bin/nix-archive-src";
        };
        apps.nix-archive-src = self.apps.${system}.default;

        devShells.default = pkgs.mkShell {
          nativeBuildInputs = [
            pkgs.nixpkgs-fmt
            pkgs.swh
            pkgs.disarchive

            (pkgs.python3.withPackages (ps: [
              ps.requests
              ps.pytest
            ]))
          ];

          shellHook = ''
            export PYTHONPATH="$PWD/src:$PYTHONPATH"
          '';
        };
      });
}
