"""Helpers for extracting fixed-output derivations (FODs) from Nix.

See https://nix.dev/manual/nix/stable/command-ref/new-cli/nix3-derivation-show
for the JSON format produced by `nix derivation show`.
"""
from __future__ import annotations

import fcntl
import json
import os
import pty
import re
import subprocess
import time
from dataclasses import dataclass
from typing import Callable, Iterable, Iterator

from .models import FixedOutputDerivation

# Strip ANSI escape sequences (colors, cursor movements, etc.) from terminal
# output so we can extract the plain store path printed by nix build.
# The character class includes '?' so that DEC private sequences such as
# \\x1b[?25l (hide cursor) and \\x1b[?25h (show cursor), which Determinate
# Nix emits on its progress UI, are also removed.
_ANSI_ESCAPE = re.compile(r"\x1b\[[\?0-9;]*[a-zA-Z]")


class NixCommandError(RuntimeError):
    """Raised when `nix derivation show` fails or returns unparseable output."""


@dataclass
class DryRunPlan:
    """Result of `nix build --dry-run --json`.

    ``plan`` is the JSON list returned by Nix.  ``will_build`` is the set of
    derivation paths that must be built locally.  ``will_fetch`` is the set of
    output paths that will be fetched from a substituter.
    """

    plan: list[dict]
    will_build: set[str]
    will_fetch: set[str]


def show_derivations_recursive(
    installable: str,
    *,
    nix_binary: str = "nix",
    extra_args: Iterable[str] | None = None,
    on_log: Callable[[str], None] | None = None,
) -> dict[str, dict]:
    """Run `nix derivation show --recursive <installable>` and parse its JSON output.

    Returns a mapping of `.drv` store paths to derivation objects.
    """
    cmd = [nix_binary, "derivation", "show", "--recursive", *(extra_args or []), installable]
    if on_log:
        on_log(
            f"running 'nix derivation show --recursive {installable}' "
            "(this can take a while for large dependency graphs)..."
        )
    proc = _run_nix(cmd, nix_binary)
    try:
        return _parse_derivations_json(proc.stdout)
    except json.JSONDecodeError as exc:
        raise NixCommandError(f"could not parse JSON output of '{' '.join(cmd)}'") from exc


def _parse_derivations_json(stdout: str) -> dict[str, dict]:
    payload = json.loads(stdout)
    if not isinstance(payload, dict):
        raise NixCommandError("could not parse derivations JSON: top-level value is not an object")
    if "derivations" in payload:
        derivations = payload["derivations"]
        if not isinstance(derivations, dict):
            raise NixCommandError("could not parse wrapped derivations JSON: 'derivations' is not an object")
        if not any(_is_drv_store_path(key) for key in derivations):
            raise NixCommandError("could not parse derivations JSON: no derivation entries found")
        return _normalize_derivation_keys(derivations)
    if any(_is_drv_store_path(key) for key in payload):
        return _normalize_derivation_keys(payload)
    raise NixCommandError("could not parse derivations JSON: no derivation entries found")


def _is_drv_store_path(key: str) -> bool:
    """Return True when ``key`` looks like a `.drv` store path.

    Newer Nix versions emit only the store path basename (e.g.
    ``013mqc5ymx4cih72blz21l6ync49i3jg-expr-strcmp.patch.drv``) as the
    JSON object key, while older versions use the full path. Accept both
    forms and let callers normalize keys to full paths when needed.
    """
    return isinstance(key, str) and key.endswith(".drv") and (
        key.startswith("/nix/store/") or not key.startswith("/")
    )


def _normalize_derivation_keys(derivations: dict[str, object]) -> dict[str, dict]:
    """Return ``derivations`` with all keys converted to full store paths.

    Basename-only keys are prefixed with ``/nix/store/``. Non-derivation
    entries are dropped so callers only receive real derivation objects.
    """
    normalized: dict[str, dict] = {}
    for key, drv in derivations.items():
        if not _is_drv_store_path(key):
            continue
        if not isinstance(drv, dict):
            continue
        drv_path = key if key.startswith("/nix/store/") else f"/nix/store/{key}"
        normalized[drv_path] = drv
    return normalized


def _parse_dry_run_stderr(stderr: str) -> tuple[set[str], set[str]]:
    """Parse the stderr of `nix build --dry-run` to extract planned actions.

    Returns ``(will_build, will_fetch)`` where ``will_build`` is a set of
    derivation paths and ``will_fetch`` is a set of output paths.
    """
    will_build: set[str] = set()
    will_fetch: set[str] = set()
    section: str | None = None
    for line in stderr.splitlines():
        stripped = line.strip()
        if not stripped:
            section = None
            continue
        if "will be built" in stripped and stripped.endswith(":"):
            section = "build"
            continue
        if "will be fetched" in stripped and stripped.endswith(":"):
            section = "fetch"
            continue
        if stripped.startswith("/nix/store/"):
            if section == "build":
                will_build.add(stripped)
            elif section == "fetch":
                will_fetch.add(stripped)
    return will_build, will_fetch


def dry_run_nix_file(
    path: str,
    attrs: list[str] | None = None,
    *,
    nix_binary: str = "nix",
    extra_args: Iterable[str] | None = None,
    on_log: Callable[[str], None] | None = None,
    no_substitute: bool = False,
) -> DryRunPlan:
    """Run `nix build --dry-run -f <path> <attrs> --json` and parse the output.

    Returns a :class:`DryRunPlan` containing the JSON plan plus the sets of
    derivation paths that will be built and output paths that will be fetched.
    Passing ``no_substitute=True`` adds ``--no-substitute`` so the dry run
    ignores configured substituters.
    """
    cmd = [nix_binary, "build", "--dry-run", "-f", path, "--json"]
    if no_substitute:
        cmd.append("--no-substitute")
    if attrs:
        cmd.extend(attrs)
    cmd.extend(extra_args or [])
    if on_log:
        on_log(f"running '{' '.join(cmd)}'...")
    proc = _run_nix(cmd, nix_binary)
    try:
        plan = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise NixCommandError(f"could not parse JSON output of '{' '.join(cmd)}'") from exc
    will_build, will_fetch = _parse_dry_run_stderr(proc.stderr or "")
    return DryRunPlan(plan=plan, will_build=will_build, will_fetch=will_fetch)


def build_nix_file(
    path: str,
    attrs: list[str] | None = None,
    *,
    nix_binary: str = "nix",
    extra_args: Iterable[str] | None = None,
    on_log: Callable[[str], None] | None = None,
) -> None:
    """Run `nix build -f <path> [<attr> ...]` to build derivations in a Nix file.

    The file is expected to evaluate to an attribute set of derivations,
    such as the expression produced by `write_swh_fods_nix`. When ``attrs`` is
    provided, only those attribute names are built; otherwise every attribute
    is built. Progress messages from Nix are streamed to ``on_log`` when
    provided.
    """
    cmd = [nix_binary, "build", "-f", path, *(attrs or []), *(extra_args or [])]
    if on_log:
        on_log(f"running 'nix build -f {path}'...")
    _run_nix(cmd, nix_binary, on_log=on_log, stream_stderr=True)


def realise_fod(
    fod: FixedOutputDerivation,
    *,
    nix_binary: str = "nix",
    extra_args: Iterable[str] | None = None,
    on_log: Callable[[str], None] | None = None,
) -> str:
    """Realise a single FOD output and return its resulting store path.

    This runs `nix build --no-link --print-out-paths <drv>^<output>`, which
    fetches the output from any configured substituter (e.g. the NixOS
    binary cache) whenever possible, only falling back to actually
    downloading/building it from scratch if it isn't substitutable.
    """
    installable = f"{fod.drv_path}^{fod.output_name}"
    cmd = [nix_binary, "build", "--no-link", "--print-out-paths", *(extra_args or []), installable]
    if on_log:
        on_log(
            f"realising {fod.label} (fetching from a binary cache or building it, "
            "this can be slow)..."
        )
    # When a progress callback is provided, run nix build with both stdout
    # and stderr attached to a pseudo-terminal. Nix only renders its
    # interactive progress UI (download/build bars) when it has a TTY, and it
    # also hangs if stdout is a pipe while stderr is a PTY. By using a single
    # PTY for both streams we capture the progress UI and the resulting store
    # path, which we extract from the terminal output. Without a callback we
    # use the simpler pipe path.
    if on_log is not None:
        proc = _run_nix_pty(cmd, nix_binary, on_log=on_log)
    else:
        proc = _run_nix(cmd, nix_binary)
    out_paths = proc.stdout.split()
    if not out_paths:
        raise NixCommandError(f"'{' '.join(cmd)}' produced no output path")
    return out_paths[0]


def _run_nix(
    cmd: list[str],
    nix_binary: str,
    *,
    on_log: Callable[[str], None] | None = None,
    stream_stderr: bool = False,
) -> subprocess.CompletedProcess:
    try:
        if stream_stderr:
            return _run_nix_streaming(cmd, on_log)
        return subprocess.run(cmd, check=True, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise NixCommandError(f"could not find the '{nix_binary}' executable") from exc
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.strip() if exc.stderr else ""
        raise NixCommandError(
            f"'{' '.join(cmd)}' failed with exit code {exc.returncode}: {stderr}"
        ) from exc


def _run_nix_streaming(
    cmd: list[str], on_log: Callable[[str], None] | None
) -> subprocess.CompletedProcess:
    """Run a nix command, capturing stdout while streaming stderr.

    This lets the user see ``nix build`` progress (download/build logs) as it
    happens, while still returning the command's stdout to the caller.
    """
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    stderr_lines: list[str] = []
    if proc.stderr is not None:
        for line in iter(proc.stderr.readline, ""):
            stripped = line.rstrip("\n")
            stderr_lines.append(stripped)
            if on_log:
                on_log(stripped)
    stdout, _ = proc.communicate()
    if proc.returncode != 0:
        raise subprocess.CalledProcessError(
            proc.returncode,
            cmd,
            output=stdout,
            stderr="\n".join(stderr_lines),
        )
    return subprocess.CompletedProcess(
        cmd, proc.returncode, stdout=stdout, stderr="\n".join(stderr_lines)
    )


def _run_nix_pty(
    cmd: list[str],
    nix_binary: str,
    *,
    on_log: Callable[[str], None] | None = None,
) -> subprocess.CompletedProcess:
    """Run a nix command on a PTY so Nix renders its interactive progress UI.

    Both stdout and stderr are attached to the same PTY, matching a real
    terminal. The raw terminal output is streamed to ``on_log`` and also
    captured so the caller can extract the store path printed by ``nix build``.

    The PTY master is read in non-blocking mode and the loop exits once the
    process has finished and no new data has arrived for a short grace period.
    This avoids the hangs observed with ``select``-only blocking reads.
    """
    master_fd = -1
    slave_fd = -1
    try:
        master_fd, slave_fd = pty.openpty()
        # Non-blocking reads let us poll the PTY and the process state together,
        # which is simpler and more reliable than select() with mixed PTY/pipe
        # file descriptors.
        flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
        fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=slave_fd,
            stderr=slave_fd,
            close_fds=True,
        )
        os.close(slave_fd)
        slave_fd = -1

        lines: list[str] = []
        buffer = b""

        def emit(line: str, is_progress: bool) -> None:
            if not line:
                return
            lines.append(line)
            if on_log:
                on_log(("\r" if is_progress else "") + line)

        last_data_time = time.time()
        while True:
            try:
                data = os.read(master_fd, 4096)
            except BlockingIOError:
                data = b""
            except OSError:
                data = b""

            if data:
                last_data_time = time.time()
                buffer += data
                while b"\n" in buffer or b"\r" in buffer:
                    if b"\n" in buffer and (
                        b"\r" not in buffer
                        or buffer.index(b"\n") < buffer.index(b"\r")
                    ):
                        line, buffer = buffer.split(b"\n", 1)
                        decoded = line.decode("utf-8", errors="replace").rstrip("\r")
                        emit(decoded, is_progress=False)
                    else:
                        line, buffer = buffer.split(b"\r", 1)
                        decoded = line.decode("utf-8", errors="replace")
                        emit(decoded, is_progress=True)
            elif proc.poll() is not None and time.time() - last_data_time > 0.5:
                break
            else:
                time.sleep(0.05)

        # Flush any trailing bytes that arrived just before the loop exited.
        while True:
            try:
                data = os.read(master_fd, 4096)
            except BlockingIOError:
                break
            except OSError:
                break
            if not data:
                break
            buffer += data
        while b"\n" in buffer or b"\r" in buffer:
            if b"\n" in buffer and (
                b"\r" not in buffer or buffer.index(b"\n") < buffer.index(b"\r")
            ):
                line, buffer = buffer.split(b"\n", 1)
                decoded = line.decode("utf-8", errors="replace").rstrip("\r")
                emit(decoded, is_progress=False)
            else:
                line, buffer = buffer.split(b"\r", 1)
                decoded = line.decode("utf-8", errors="replace")
                emit(decoded, is_progress=True)
        if buffer:
            decoded = buffer.decode("utf-8", errors="replace").rstrip("\r\n")
            emit(decoded, is_progress=False)

        # The store path is mixed with ANSI progress output on the PTY. Strip
        # escape sequences and take the last whitespace-delimited token that
        # looks like a Nix store path.
        raw_output = "\n".join(lines)
        clean_output = _ANSI_ESCAPE.sub("", raw_output)
        stdout = _last_store_path(clean_output) or clean_output
        stderr = raw_output
        returncode = proc.wait()
        if returncode != 0:
            raise subprocess.CalledProcessError(
                returncode,
                cmd,
                output=stdout,
                stderr=stderr,
            )
        return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr=stderr)
    except FileNotFoundError as exc:
        raise NixCommandError(f"could not find the '{nix_binary}' executable") from exc
    finally:
        for fd in (master_fd, slave_fd):
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass


def _last_store_path(text: str) -> str | None:
    """Return the last token that looks like a Nix store path, if any."""
    clean_text = _ANSI_ESCAPE.sub("", text)
    for token in reversed(clean_text.split()):
        if token.startswith("/nix/store/"):
            return token
    return None


def iter_fixed_output_derivations(
    derivations: dict[str, object],
) -> Iterator[FixedOutputDerivation]:
    """Yield every fixed-output derivation output found in `derivations`.

    `derivations` is expected to be in the JSON format produced by
    `nix derivation show`, i.e. a mapping of `.drv` store paths to derivation
    objects, each with an `outputs` mapping of output names to
    `{"path", "method", "hashAlgo", "hash"}` objects. Some newer Nix
    versions emit basename-only store paths as keys and may also include
    non-derivation metadata entries in the same top-level object; both are
    handled by normalizing keys to full store paths and skipping unrelated
    entries. An output is a FOD exactly when its `hash` field is set.

    Many Nix versions don't actually populate the `method` field in
    `outputs` and only expose it as the legacy `outputHashMode` environment
    variable (`"flat"` or `"recursive"`), so that's used as a fallback.
    """
    normalized = _normalize_derivation_keys(derivations)
    for drv_path, drv in normalized.items():
        outputs = drv.get("outputs", {}) or {}
        if not isinstance(outputs, dict):
            continue
        env = drv.get("env", {}) or {}
        if not isinstance(env, dict):
            env = {}
        for output_name, output in outputs.items():
            if not isinstance(output, dict):
                continue
            hash_hex = output.get("hash")
            if not hash_hex:
                continue
            hash_algo = _hash_algo(hash_hex, output.get("hashAlgo"), env.get("outputHashAlgo"))
            yield FixedOutputDerivation(
                drv_path=drv_path,
                output_name=output_name,
                output_path=output.get("path"),
                name=drv.get("name", drv_path),
                method=_output_method(output, env),
                hash_algo=hash_algo,
                hash_hex=_hash_hex(hash_hex, hash_algo),
                origin_urls=_extract_origin_urls(env),
                executable=_is_executable_fod(env),
            )


def _output_method(output: dict, env: dict) -> str | None:
    method = output.get("method")
    if method:
        # Nix versions vary in whether they emit the normalized method name
        # ("nar") or the raw outputHashMode value ("recursive"). Normalize
        # to the names the checker expects.
        if method == "recursive":
            return "nar"
        return method
    output_hash_mode = env.get("outputHashMode")
    if output_hash_mode == "recursive":
        return "nar"
    # Some Nix implementations (e.g. Lix) leave both output.method and
    # env.outputHashMode empty for flat-hashed FODs. In that case the only
    # available hint is the hash format: SRI hashes ("<algo>-<base64>")
    # correspond to recursive/NAR mode, while a plain hex string means flat.
    hash_value = output.get("hash", "")
    if "-" in hash_value:
        return "nar"
    return output_hash_mode or "flat"


def _hash_algo(
    hash_value: str,
    output_hash_algo: str | None,
    env_hash_algo: str | None,
) -> str | None:
    """Return the hash algorithm for a FOD output.

    Newer Nix versions put the algorithm in ``outputs.<name>.hashAlgo``,
    but older versions and some Nix implementations leave it empty and
    instead embed the algorithm as a prefix in the ``hash`` field itself
    (e.g. ``sha256-...``). The legacy ``env.outputHashAlgo`` is used as a
    fallback, and the hash prefix is used as a last resort.

    Nix also uses a ``r:<algo>`` form to indicate recursive/NAR hashing;
    the ``r:`` prefix is stripped so the algorithm matches what Software
    Heritage expects.
    """
    algo = output_hash_algo or env_hash_algo
    if algo:
        if isinstance(algo, str) and algo.startswith("r:"):
            return algo[2:]
        return algo
    if hash_value and "-" in hash_value:
        return hash_value.split("-", 1)[0]
    return None


def _hash_hex(hash_value: str, hash_algo: str | None = None) -> str:
    """Return the raw hash value, stripping any algorithm prefix.

    Nix represents SRI hashes as ``<algo>-<base64>`` and flat hashes as a
    plain hex string. Software Heritage expects the raw hash without the
    algorithm prefix.

    Some Nix implementations (e.g. Determinate Nix) emit flat hashes as
    base64 even though the algorithm is not SRI-prefixed. SWH's content
    lookup endpoint requires hex for ``sha256`` flat hashes, so base64
    values are decoded and re-encoded as hex.
    """
    raw = hash_value.split("-", 1)[1] if hash_value and "-" in hash_value else hash_value
    if (
        raw
        and hash_algo in {"sha256", "sha512"}
        and len(raw) == _base64_length_for_hash_algo(hash_algo)
        and _looks_like_base64(raw)
    ):
        import binascii

        return binascii.hexlify(binascii.a2b_base64(raw)).decode("ascii")
    return raw


def _base64_length_for_hash_algo(hash_algo: str) -> int:
    """Return the length in characters of a base64-encoded hash for ``hash_algo``."""
    byte_lengths = {"sha256": 32, "sha512": 64}
    # Base64 encoding produces 4 characters for every 3 bytes, rounded up.
    byte_len = byte_lengths.get(hash_algo, 0)
    return (byte_len + 2) // 3 * 4 if byte_len else 0


def _looks_like_base64(value: str) -> bool:
    """Return True when ``value`` looks like a base64-encoded string."""
    if not value:
        return False
    import base64

    try:
        return base64.b64encode(base64.b64decode(value, validate=True)) == value.encode()
    except Exception:
        return False


def _is_executable_fod(env: dict) -> bool:
    """Return True when a FOD's environment marks the downloaded file as executable.

    ``builtin:fetchurl`` honours ``env.executable = "1"`` by setting the
    output file's executable bit.  This matters for recursive/NAR-hashed
    single-file FODs because the NAR hash depends on the file's permissions.
    """
    value = env.get("executable")
    return value == "1" or value is True


def _extract_origin_urls(env: dict) -> list[str]:
    """Return the upstream origin URLs declared in a FOD's environment.

    Nix download helpers such as ``fetchurl`` and ``fetchzip`` store their
    URLs in the ``url`` or ``urls`` environment variables.  Multiple URLs
    are whitespace-separated in ``urls``.
    """
    urls: list[str] = []
    if "urls" in env:
        urls.extend(env["urls"].split())
    if "url" in env:
        urls.append(env["url"])
    return urls
