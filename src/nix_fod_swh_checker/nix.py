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
from typing import Callable, Iterable, Iterator

from .models import FixedOutputDerivation

# Strip ANSI escape sequences (colors, cursor movements, etc.) from terminal
# output so we can extract the plain store path printed by nix build.
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


class NixCommandError(RuntimeError):
    """Raised when `nix derivation show` fails or returns unparseable output."""


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
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise NixCommandError(f"could not parse JSON output of '{' '.join(cmd)}'") from exc


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
    for token in reversed(text.split()):
        if token.startswith("/nix/store/"):
            return token
    return None


def iter_fixed_output_derivations(
    derivations: dict[str, dict],
) -> Iterator[FixedOutputDerivation]:
    """Yield every fixed-output derivation output found in `derivations`.

    `derivations` is expected to be in the JSON format produced by
    `nix derivation show`, i.e. a mapping of `.drv` store paths to derivation
    objects, each with an `outputs` mapping of output names to
    `{"path", "method", "hashAlgo", "hash"}` objects. An output is a FOD
    exactly when its `hash` field is set.

    Many Nix versions don't actually populate the `method` field in
    `outputs` and only expose it as the legacy `outputHashMode` environment
    variable (`"flat"` or `"recursive"`), so that's used as a fallback.
    """
    for drv_path, drv in derivations.items():
        outputs = drv.get("outputs", {}) or {}
        env = drv.get("env", {}) or {}
        for output_name, output in outputs.items():
            hash_hex = output.get("hash")
            if not hash_hex:
                continue
            yield FixedOutputDerivation(
                drv_path=drv_path,
                output_name=output_name,
                output_path=output.get("path"),
                name=drv.get("name", drv_path),
                method=_output_method(output, env),
                hash_algo=output.get("hashAlgo"),
                hash_hex=hash_hex,
            )


def _output_method(output: dict, env: dict) -> str | None:
    method = output.get("method")
    if method:
        return method
    output_hash_mode = env.get("outputHashMode")
    if output_hash_mode == "recursive":
        return "nar"
    return output_hash_mode or None
