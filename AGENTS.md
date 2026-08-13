Use `nix develop` (should already be loaded thanks to `direnv` when running in local mode) to get the required dependencies.

Always make sure to keep the documentation files (`README.md` + `docs/`) in sync with the code. A discrepancy between the documentation and the code should be considered as a bug.

When making a feature change, the changes to the documentation files (in particular, `docs/specification.md`) can be used to refine and iterate on the design between the user and the agent. In case anything in the design needs to be clarified, make use of this document to propose design changes and iterate on them with the user. For any change needing significant updates to the code, the changes to the specification should be made and approved before implementing the code changes.

Always run `nix build` (also runs the tests) before committing.
