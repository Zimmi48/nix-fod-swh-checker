{
  description = "List the fixed-output derivations (FODs) reachable from a Nix attribute and check whether their sources are already archived on Software Heritage";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = import nixpkgs { inherit system; };

        nix-fod-swh-checker = pkgs.python3Packages.buildPythonApplication {
          pname = "nix-fod-swh-checker";
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
          pythonImportsCheck = [ "nix_fod_swh_checker" ];

          makeWrapperArgs = [
            "--prefix" "PATH" ":" "${pkgs.swh}/bin"
            "--prefix" "PATH" ":" "${pkgs.disarchive}/bin"
          ];

          meta = {
            description = "List the FODs reachable from a Nix attribute and check whether their sources are archived on Software Heritage";
            mainProgram = "nix-fod-swh-check";
          };
        };
      in
      {
        packages.default = nix-fod-swh-checker;
        packages.nix-fod-swh-checker = nix-fod-swh-checker;

        checks.default = nix-fod-swh-checker;

        apps.default = {
          type = "app";
          program = "${nix-fod-swh-checker}/bin/nix-fod-swh-check";
        };
        apps.nix-fod-swh-check = self.apps.${system}.default;

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
