"""Standalone, stdlib-only application update helper.

Freeze this file separately as a one-file executable. It runs from app-data,
not from the installation being replaced, and never imports Qt/the application.
The parent must obtain explicit install/restart approval and complete its safe
shutdown checks before launching it. No process is forcibly terminated.

GitHub is the download trust root (see app_updates); the helper independently
rechecks the authenticated size and SHA-256 before touching the installation.
Local state is not a defence against already-compromised same-user processes.
"""
from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import plistlib
import re
import shutil
import stat
import subprocess
import sys
import time
import zipfile

MAX_ARTIFACT_BYTES = 4 * 1024**3
MAX_EXPANDED_BYTES = 12 * 1024**3
MAX_ARCHIVE_FILES = 100_000
MAX_PLAN_BYTES = 64 * 1024
MAX_ZONE_IDENTIFIER_BYTES = 64 * 1024
MAX_QUARANTINE_BYTES = 64 * 1024
KINDS = {"windows-installed", "windows-portable", "macos-app"}
VERSION_RE = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z][0-9A-Za-z.-]*)?")
_IS_WINDOWS = os.name == "nt"


class AppUpdateError(RuntimeError):
    """An update failed closed; the website/manual path remains available."""


def _console_process_kwargs() -> dict:
    """Match utils.proc's hidden-console contract without importing the app.

    This helper is frozen independently and must remain stdlib-only. These
    flags apply only to subprocess console tools, not ShellExecute installer
    or application launches, whose OS permission/security UI stays visible.
    On macOS the tools have no console-window flags.
    """
    if not _IS_WINDOWS:
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    return {"creationflags": subprocess.CREATE_NO_WINDOW, "startupinfo": startupinfo}


def atomic_json(path: Path, value: dict) -> None:
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def read_json(path: Path) -> dict:
    if path.is_symlink() or path.stat().st_size > MAX_PLAN_BYTES:
        raise AppUpdateError("The update record is unsafe or too large.")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise AppUpdateError("The update record is invalid.")
    return value


def process_alive(pid: int) -> bool:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x00100000, False, pid)
        if handle:
            kernel.CloseHandle(handle)
            return True
        return ctypes.get_last_error() != 87
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _lock_file(path: Path):
    """Acquire an OS-held lock; never unlink/recreate a possibly locked inode."""
    if path.is_symlink():
        raise AppUpdateError("The updater lock path is unsafe.")
    handle = path.open("a+b")
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b" ")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return handle
    except BaseException:
        handle.close()
        raise


def update_lock_is_held(path: Path) -> bool:
    if path.is_symlink():
        raise AppUpdateError("The updater lock path is unsafe.")
    if not path.exists():
        return False
    try:
        handle = _lock_file(path)
    except OSError as exc:
        if exc.errno in {errno.EACCES, errno.EAGAIN}:
            return True
        raise
    handle.close()
    return False


def _acquire_lock(path: Path, transaction: str):
    """Only one helper may mutate shared app data/installation at a time."""
    try:
        handle = _lock_file(path)
        try:
            handle.seek(0)
            handle.truncate()
            handle.write(json.dumps({"pid": os.getpid(), "transaction": transaction}).encode())
            handle.flush()
            os.fsync(handle.fileno())
            return handle
        except BaseException:
            handle.close()
            raise
    except OSError as exc:
        if exc.errno in {errno.EACCES, errno.EAGAIN}:
            raise AppUpdateError("Another updater is already running.") from exc
        raise


def verify_artifact(path: Path, size: int, sha256: str) -> None:
    if isinstance(size, bool) or not isinstance(size, int) or not 0 < size <= MAX_ARTIFACT_BYTES:
        raise AppUpdateError("The package size is outside the safe limit.")
    if not isinstance(sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise AppUpdateError("The package SHA-256 is invalid.")
    if path.is_symlink() or not path.is_file() or path.stat().st_size != size:
        raise AppUpdateError("The prepared package is missing or its size changed.")
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            total += len(block)
            if total > size:
                raise AppUpdateError("The package exceeded its verified size.")
            digest.update(block)
    if total != size or digest.hexdigest() != sha256:
        raise AppUpdateError("The package failed SHA-256 verification.")


def _absolute_path(value: object) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise AppUpdateError("An update path is invalid.")
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or path.is_symlink():
        raise AppUpdateError("An update path is not an absolute non-link path.")
    if path.resolve() != path:
        raise AppUpdateError("An update path crosses a symbolic link.")
    return path


def validate_plan(path: Path) -> dict:
    path = _absolute_path(str(path))
    plan = read_json(path)
    if plan.get("schema") != 1 or plan.get("kind") not in KINDS:
        raise AppUpdateError("The update plan is unsupported.")
    if not VERSION_RE.fullmatch(str(plan.get("version", ""))):
        raise AppUpdateError("The update version is invalid.")
    target = _absolute_path(plan.get("target"))
    artifact = _absolute_path(plan.get("artifact"))
    if target == target.parent or target == Path.home().resolve():
        raise AppUpdateError("The update destination is unsafe.")
    if target == artifact or target in artifact.parents or target in path.parents:
        raise AppUpdateError("The update must be staged outside the installed application.")
    # Plans can reference only a sibling verified artifact, never an arbitrary
    # executable elsewhere. The actual target is selected from sys.executable.
    if artifact.parent != path.parent:
        raise AppUpdateError("The package is outside its prepared update directory.")
    expected_executable = (
        "Contents/MacOS/bananaflow" if plan["kind"] == "macos-app" else "bananaflow.exe"
    )
    if plan.get("executable") != expected_executable:
        raise AppUpdateError("The relaunch executable is not BananaFlow.")
    if plan["kind"] == "macos-app" and target.suffix != ".app":
        raise AppUpdateError("The update destination is not an application bundle.")
    if plan["kind"] == "windows-installed" and plan.get("install_mode") not in {"currentuser", "allusers"}:
        raise AppUpdateError("The Windows installation mode is unknown.")
    pid = plan.get("parent_pid")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        raise AppUpdateError("The parent process identifier is invalid.")
    transaction = plan.get("transaction")
    if not isinstance(transaction, str) or not re.fullmatch(r"[0-9a-f]{32}", transaction):
        raise AppUpdateError("The transaction identifier is invalid.")
    args = plan.get("relaunch_args", [])
    if args:
        if not isinstance(args, list) or len(args) != 2 or args[0] != "--app-update-smoke-result":
            raise AppUpdateError("Unexpected relaunch arguments.")
        _absolute_path(args[1])
    elif not isinstance(args, list):
        raise AppUpdateError("Invalid relaunch arguments.")
    return plan


def _paths(plan: dict) -> tuple[Path, Path, Path]:
    target = Path(plan["target"])
    suffix = plan["transaction"]
    # Exact transaction-owned siblings. Never remove a path from remote input.
    staging = target.parent / f".{target.name}.bananaflow-new-{suffix}"
    backup = target.parent / f".{target.name}.bananaflow-previous-{suffix}"
    return target, staging, backup


def wait_for_parent(pid: int, timeout: float = 300) -> None:
    """Wait without killing the app, with a bounded timeout."""
    if pid == os.getpid():
        raise AppUpdateError("The updater cannot wait for itself.")
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE only
        if not handle:
            if ctypes.get_last_error() == 87:  # process already exited
                return
            raise AppUpdateError("Cannot safely wait for the application to close.")
        try:
            if kernel.WaitForSingleObject(handle, int(timeout * 1000)) != 0:
                raise AppUpdateError("The application did not close; nothing was installed.")
        finally:
            kernel.CloseHandle(handle)
        return
    deadline = time.monotonic() + timeout
    while True:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        except PermissionError as exc:
            raise AppUpdateError("Cannot safely wait for the application to close.") from exc
        if time.monotonic() >= deadline:
            raise AppUpdateError("The application did not close; nothing was installed.")
        time.sleep(0.2)


def ensure_no_other_app_processes() -> None:
    """Conservatively defer while another GUI/CLI instance is still running.

    A process name match from another install also defers: that is safer than
    interrupting its work or starting two versions against shared app data.
    """
    if sys.platform == "darwin":
        result = subprocess.run(
            ["/usr/bin/pgrep", "-x", "bananaflow|bananaflow-cli"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15,
            **_console_process_kwargs(),
        )
        if result.returncode == 0:
            raise AppUpdateError("Another BananaFlow window or CLI is running. Close it before retrying.")
        if result.returncode != 1:
            raise AppUpdateError("Could not verify that other BananaFlow processes have closed.")
    elif sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
                ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260),
            ]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
        kernel.Process32FirstW.restype = wintypes.BOOL
        kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
        kernel.Process32NextW.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        snapshot = kernel.CreateToolhelp32Snapshot(2, 0)  # TH32CS_SNAPPROCESS
        if snapshot == ctypes.c_void_p(-1).value:
            raise AppUpdateError("Could not check other running application processes.")
        try:
            entry = PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(entry)
            if not kernel.Process32FirstW(snapshot, ctypes.byref(entry)):
                raise AppUpdateError("Could not enumerate running application processes.")
            while True:
                if entry.szExeFile.lower() in {"bananaflow.exe", "bananaflow-cli.exe"}:
                    raise AppUpdateError("Another BananaFlow window or CLI is running. Close it before retrying.")
                if not kernel.Process32NextW(snapshot, ctypes.byref(entry)):
                    if ctypes.get_last_error() != 18:  # ERROR_NO_MORE_FILES
                        raise AppUpdateError("Could not finish checking running application processes.")
                    break
        finally:
            kernel.CloseHandle(snapshot)


def extract_portable(artifact: Path, destination: Path) -> None:
    """Extract the release's flat ZIP with Windows-safe, bounded paths."""
    zone_identifier = _read_windows_zone_identifier(artifact) if _IS_WINDOWS else None
    seen: set[str] = set()
    total = 0
    with zipfile.ZipFile(artifact) as archive:
        entries = archive.infolist()
        if len(entries) > MAX_ARCHIVE_FILES:
            raise AppUpdateError("The application archive contains too many files.")
        for entry in entries:
            # ZipInfo sanitizes filename while reading: Windows backslashes
            # become slashes and NUL suffixes are truncated on every platform.
            # Validate the actual archive name before accepting normalization.
            raw = entry.orig_filename
            relative = PurePosixPath(raw)
            if (
                not raw or raw != entry.filename
                or "\\" in raw or "\x00" in raw or relative.is_absolute()
                or any(p in {"", ".", ".."} for p in raw.rstrip("/").split("/"))
                or any(":" in p or p.endswith((".", " ")) for p in relative.parts)
                or any(re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", p) for p in relative.parts)
            ):
                raise AppUpdateError("The application archive contains an unsafe path.")
            key = relative.as_posix().casefold()
            if key in seen:
                raise AppUpdateError("The application archive contains duplicate paths.")
            seen.add(key)
            mode = entry.external_attr >> 16
            if stat.S_ISLNK(mode) or (stat.S_IFMT(mode) not in {0, stat.S_IFREG, stat.S_IFDIR}):
                raise AppUpdateError("The application archive contains a link or special file.")
            if entry.flag_bits & 1:
                raise AppUpdateError("Encrypted application archives are unsupported.")
            total += entry.file_size
            if total > MAX_EXPANDED_BYTES:
                raise AppUpdateError("The expanded application exceeds the safe limit.")
        if "bananaflow.exe" not in seen or not any(p.startswith("_internal/") for p in seen):
            raise AppUpdateError("The archive is not a BananaFlow portable application.")
        for entry in entries:
            destination_path = destination.joinpath(*PurePosixPath(entry.filename).parts)
            if entry.is_dir():
                destination_path.mkdir(parents=True, exist_ok=True)
                continue
            destination_path.parent.mkdir(parents=True, exist_ok=True)
            written = 0
            with archive.open(entry) as source, destination_path.open("xb") as output:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    written += len(chunk)
                    if written > entry.file_size:
                        raise AppUpdateError("The expanded package file exceeded its declared size.")
                    output.write(chunk)
                if written != entry.file_size:
                    raise AppUpdateError("The expanded package file size is incorrect.")
            if zone_identifier is not None:
                # Propagate the archive's actual origin, never erase a mark
                # or invent Internet provenance for a locally built artifact.
                # Production downloads are marked by app_updates._quarantine.
                with open(str(destination_path) + ":Zone.Identifier", "wb") as marker:
                    marker.write(zone_identifier)


def _read_windows_zone_identifier(artifact: Path) -> bytes | None:
    """Read the existing Mark of the Web without changing its trust meaning."""
    try:
        with open(str(artifact) + ":Zone.Identifier", "rb") as marker:
            data = marker.read(MAX_ZONE_IDENTIFIER_BYTES + 1)
    except FileNotFoundError:
        return None
    if len(data) > MAX_ZONE_IDENTIFIER_BYTES:
        raise AppUpdateError("The package origin metadata exceeds the safe limit.")
    return data


def _check_macos_tree(root: Path, version: str) -> None:
    """Allow framework symlinks only when they remain inside this app bundle."""
    total = 0
    count = 0
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            path = Path(directory) / name
            count += 1
            if path.is_symlink():
                if not path.resolve().is_relative_to(root.resolve()):
                    raise AppUpdateError("The application bundle contains an escaping symlink.")
            elif path.is_file():
                total += path.stat().st_size
            elif not path.is_dir():
                raise AppUpdateError("The application bundle contains a special file.")
            if total > MAX_EXPANDED_BYTES or count > MAX_ARCHIVE_FILES:
                raise AppUpdateError("The application bundle exceeds the safe limit.")
    info_path = root / "Contents/Info.plist"
    if info_path.stat().st_size > MAX_PLAN_BYTES:
        raise AppUpdateError("The application bundle metadata is too large.")
    with info_path.open("rb") as handle:
        info = plistlib.load(handle)
    if (
        info.get("CFBundleIdentifier") != "media.bananaflow.app"
        or info.get("CFBundleShortVersionString") != version.split("-")[0]
        or not (root / "Contents/MacOS/bananaflow").is_file()
    ):
        raise AppUpdateError("The disk image does not contain the expected BananaFlow version.")


def _macos_xattr_api():
    """Bind Darwin's six-argument API, not the Linux-only Python os API.

    ABI/constants: Apple's xnu bsd/sys/{xattr,errno}.h. The absolute system
    library is also present through the dyld shared cache on modern macOS.
    """
    import ctypes
    if sys.platform != "darwin":
        raise AppUpdateError("Native macOS origin metadata is unavailable.")
    try:
        library = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        arguments = [
            ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p,
            ctypes.c_size_t, ctypes.c_uint32, ctypes.c_int,
        ]
        library.getxattr.argtypes = arguments
        library.getxattr.restype = ctypes.c_ssize_t
        library.setxattr.argtypes = arguments
        library.setxattr.restype = ctypes.c_int
    except (OSError, AttributeError) as exc:
        raise AppUpdateError("Native macOS origin metadata is unavailable.") from exc
    return library


def read_macos_quarantine(path: Path) -> bytes | None:
    """Read exact quarantine bytes; only Darwin ENOATTR means absent."""
    import ctypes
    library = _macos_xattr_api()
    # One bounded read avoids the size-query/read race. ERANGE and every error
    # except ENOATTR fail closed; a larger/unreadable mark is never ignored.
    buffer = ctypes.create_string_buffer(MAX_QUARANTINE_BYTES + 1)
    ctypes.set_errno(0)
    size = library.getxattr(
        os.fsencode(path), b"com.apple.quarantine", buffer, len(buffer), 0, 1,
    )  # position=0; XATTR_NOFOLLOW=1 (never XATTR_NOSECURITY).
    if size == -1:
        error = ctypes.get_errno()
        if error == 93:  # Darwin ENOATTR, not Linux ENODATA.
            return None
        raise AppUpdateError(f"Cannot read macOS origin metadata (errno={error}).")
    if not 0 <= size <= MAX_QUARANTINE_BYTES:
        raise AppUpdateError("The package origin metadata exceeds the safe limit.")
    return buffer.raw[:size]


def write_macos_quarantine(path: Path, value: bytes) -> None:
    """Preserve the byte value without text decoding or warning bypass flags."""
    import ctypes
    if not isinstance(value, bytes) or len(value) > MAX_QUARANTINE_BYTES:
        raise AppUpdateError("The package origin metadata is invalid or too large.")
    library = _macos_xattr_api()
    buffer = ctypes.create_string_buffer(value)
    ctypes.set_errno(0)
    result = library.setxattr(
        os.fsencode(path), b"com.apple.quarantine", buffer, len(value), 0, 1,
    )
    if result != 0:
        raise AppUpdateError(
            f"Cannot preserve macOS origin metadata (errno={ctypes.get_errno()})."
        )


def stage_macos(artifact: Path, staging: Path, version: str, work: Path) -> None:
    mount = work / "mount"
    mount.mkdir(exist_ok=False)
    attached = False
    try:
        subprocess.run([
            "/usr/bin/hdiutil", "attach", str(artifact), "-readonly", "-nobrowse",
            "-noautoopen", "-mountpoint", str(mount),
        ], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120,
            **_console_process_kwargs())
        attached = True
        source = mount / "BananaFlow.app"
        if source.is_symlink() or not source.is_dir():
            raise AppUpdateError("The disk image does not contain BananaFlow.app.")
        _check_macos_tree(source, version)
        # ditto preserves framework symlinks, modes, resource forks and xattrs.
        # Never clear quarantine or disable Gatekeeper/code-signing checks.
        subprocess.run(
            ["/usr/bin/ditto", str(source), str(staging)], check=True, timeout=600,
            **_console_process_kwargs(),
        )
        _check_macos_tree(staging, version)
        # The downloaded image's quarantine attribute must travel to the new
        # app even when the mounted filesystem did not expose it.
        quarantine = read_macos_quarantine(artifact)
        if quarantine is not None:
            write_macos_quarantine(staging, quarantine)
    finally:
        if attached:
            subprocess.run(
                ["/usr/bin/hdiutil", "detach", str(mount)],
                check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120,
                **_console_process_kwargs(),
            )
        if mount.exists():
            mount.rmdir()


def install_windows(plan: dict) -> None:
    """Run Inno Setup in the existing scope; keep UAC/OS trust prompts intact."""
    import ctypes
    from ctypes import wintypes

    class SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD), ("fMask", wintypes.ULONG), ("hwnd", wintypes.HWND),
            ("lpVerb", wintypes.LPCWSTR), ("lpFile", wintypes.LPCWSTR),
            ("lpParameters", wintypes.LPCWSTR), ("lpDirectory", wintypes.LPCWSTR),
            ("nShow", ctypes.c_int), ("hInstApp", wintypes.HINSTANCE),
            ("lpIDList", ctypes.c_void_p), ("lpClass", wintypes.LPCWSTR),
            ("hkeyClass", wintypes.HKEY), ("dwHotKey", wintypes.DWORD),
            ("hIcon", wintypes.HANDLE), ("hProcess", wintypes.HANDLE),
        ]

    parameters = [
        "/SILENT", "/NORESTART", "/NOCLOSEAPPLICATIONS", "/NORESTARTAPPLICATIONS",
        f"/DIR={plan['target']}",
        "/ALLUSERS" if plan["install_mode"] == "allusers" else "/CURRENTUSER",
    ]
    shell = ctypes.WinDLL("shell32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    shell.ShellExecuteExW.argtypes = [ctypes.POINTER(SHELLEXECUTEINFOW)]
    shell.ShellExecuteExW.restype = wintypes.BOOL
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    execute = SHELLEXECUTEINFOW()
    execute.cbSize = ctypes.sizeof(execute)
    execute.fMask = 0x00000040  # SEE_MASK_NOCLOSEPROCESS, not NOZONECHECKS
    execute.lpVerb = "runas" if plan["install_mode"] == "allusers" else "open"
    execute.lpFile = plan["artifact"]
    execute.lpParameters = subprocess.list2cmdline(parameters)
    execute.nShow = 1
    if not shell.ShellExecuteExW(ctypes.byref(execute)) or not execute.hProcess:
        raise AppUpdateError("Windows did not start the installer or permission was declined.")
    try:
        # Do not abandon a live installer and release the transaction lock.
        # It may be waiting for an OS/user decision; only actual process exit
        # establishes a safe result. No process is killed on a timeout.
        while True:
            wait = kernel.WaitForSingleObject(execute.hProcess, 1000)
            if wait == 0:
                break
            if wait != 258:  # WAIT_TIMEOUT
                raise AppUpdateError("Windows could not determine the installer result.")
        exit_code = wintypes.DWORD()
        if not kernel.GetExitCodeProcess(execute.hProcess, ctypes.byref(exit_code)) or exit_code.value != 0:
            raise AppUpdateError("The installer did not complete successfully.")
    finally:
        kernel.CloseHandle(execute.hProcess)


def _pe_machine(path: Path) -> str:
    """Bounded PE-header diagnostics, never execution or a trust decision."""
    try:
        with path.open("rb") as executable:
            header = executable.read(64)
            if len(header) != 64 or header[:2] != b"MZ":
                return "invalid"
            offset = int.from_bytes(header[60:64], "little")
            if not 64 <= offset <= 1024 * 1024:
                return "invalid"
            executable.seek(offset)
            pe = executable.read(6)
        if len(pe) != 6 or pe[:4] != b"PE\x00\x00":
            return "invalid"
        machine = int.from_bytes(pe[4:6], "little")
        return {0x8664: "x64", 0x14C: "x86", 0xAA64: "arm64"}.get(machine, f"0x{machine:04x}")
    except OSError:
        return "unreadable"


def _relaunch(plan: dict) -> None:
    target = Path(plan["target"])
    arguments = plan.get("relaunch_args", [])
    os.environ["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    if plan["kind"] == "macos-app":
        # LaunchServices, not a direct exec, preserves Gatekeeper/user prompts.
        command = ["/usr/bin/open", "-n", str(target)]
        if arguments:
            command += ["--args", *arguments]
        subprocess.run(command, check=True, timeout=30, **_console_process_kwargs())
    else:
        import ctypes
        from ctypes import wintypes
        shell = ctypes.WinDLL("shell32", use_last_error=True)
        shell.ShellExecuteW.argtypes = [
            wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR,
            wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_int,
        ]
        shell.ShellExecuteW.restype = ctypes.c_void_p
        ctypes.set_last_error(0)
        executable = target / plan["executable"]
        result = shell.ShellExecuteW(
            None, "open", str(executable),
            subprocess.list2cmdline(arguments), str(target), 1,
        )
        if not result or result <= 32:
            error = ctypes.get_last_error()
            # Capture numeric shell/native diagnostics and local existence,
            # not command arguments, profile paths or origin metadata.
            raise AppUpdateError(
                "Windows did not launch the updated application "
                f"(ShellExecute={result or 0}, WinError={error}, "
                f"executable_exists={executable.is_file()}, "
                f"target_exists={target.is_dir()}, "
                f"executable_pe_machine={_pe_machine(executable)}, "
                f"helper_pointer_bits={ctypes.sizeof(ctypes.c_void_p) * 8})."
            )


def apply_update_plan(plan_path: Path, *, wait_timeout: float = 300) -> int:
    """Apply a trusted local plan and retain actionable status/recovery data."""
    plan_path = Path(plan_path)
    status_path = plan_path.parent / "status.json"
    plan: dict | None = None
    parent_exited = False
    installed = False
    installer_started = False
    others_closed = False
    lock_path = plan_path.parent.parent / "apply.lock"
    lock_handle = None
    owned_transaction = False
    launch_error_message = ""
    rollback = ""
    try:
        plan = validate_plan(plan_path)
        lock_handle = _acquire_lock(lock_path, plan["transaction"])
        owned_transaction = True
        target, staging, backup = _paths(plan)
        if plan["kind"].startswith("windows") and sys.platform != "win32":
            raise AppUpdateError("A Windows update requires Windows.")
        if plan["kind"] == "macos-app" and sys.platform != "darwin":
            raise AppUpdateError("A macOS update requires macOS.")
        atomic_json(status_path, {
            "status": "waiting", "version": plan["version"], "transaction": plan["transaction"],
        })
        wait_for_parent(plan["parent_pid"], wait_timeout)
        parent_exited = True
        if plan_path.with_suffix(".cancel").exists():
            raise AppUpdateError("The updater launch was cancelled before installation.")
        # Frozen bootloaders can briefly outlive their Python child. Give
        # graceful teardown time, but never terminate an additional instance.
        deadline = time.monotonic() + 10
        while True:
            try:
                ensure_no_other_app_processes()
                others_closed = True
                break
            except AppUpdateError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.2)
        # Recovery after a crash between the two directory renames. A
        # transaction backup is retained, never silently deleted. Restore the
        # old app BEFORE verifying the new download: a missing/corrupt package
        # must not strand an otherwise recoverable previous installation.
        if not target.exists() and backup.is_dir() and not backup.is_symlink():
            if not (backup / plan["executable"]).is_file():
                raise AppUpdateError("The previous application backup is incomplete.")
            os.replace(backup, target)
        if not (target / plan["executable"]).is_file():
            raise AppUpdateError("The original application is missing.")
        verify_artifact(Path(plan["artifact"]), plan["size"], plan["sha256"])
        atomic_json(status_path, {
            "status": "installing", "version": plan["version"], "transaction": plan["transaction"],
        })
        if plan["kind"] == "windows-installed":
            others_closed = False
            ensure_no_other_app_processes()
            others_closed = True
            installer_started = True
            install_windows(plan)
        else:
            if backup.exists():
                raise AppUpdateError("A previous transaction backup exists; manual recovery is required.")
            if staging.exists():
                if staging.is_symlink():
                    raise AppUpdateError("The staging directory is unsafe.")
                shutil.rmtree(staging)
            staging.mkdir()
            if plan["kind"] == "windows-portable":
                extract_portable(Path(plan["artifact"]), staging)
            else:
                stage_macos(Path(plan["artifact"]), staging, plan["version"], plan_path.parent)
            # Hashing/extraction may take time. A user might have reopened
            # another instance while preparation was in progress.
            others_closed = False
            ensure_no_other_app_processes()
            others_closed = True
            os.replace(target, backup)
            try:
                os.replace(staging, target)
            except BaseException:
                os.replace(backup, target)
                raise
        try:
            installed = True
            atomic_json(status_path, {
                "status": "restarting", "version": plan["version"], "transaction": plan["transaction"],
            })
            # Replacement has finished. A legitimate new GUI must not trip
            # the startup "updater active" guard. Release only at this point.
            lock_handle.close()
            lock_handle = None
            _relaunch(plan)
        except Exception as launch_error:
            launch_error_message = str(launch_error)[:1000]
            if plan["kind"] != "windows-installed" and backup.is_dir():
                # Someone may have independently opened the completed new
                # app after the lock release. Never roll back underneath it.
                try:
                    if lock_handle is None:
                        lock_handle = _acquire_lock(lock_path, plan["transaction"])
                    others_closed = False
                    ensure_no_other_app_processes()
                    others_closed = True
                    os.replace(target, staging)
                    os.replace(backup, target)
                    rollback = "restored_previous"
                except Exception as recovery_error:
                    rollback = "not_completed"
                    raise AppUpdateError(
                        f"{launch_error_message} Previous application recovery could not complete: {recovery_error}"
                    ) from launch_error
            else:
                rollback = "not_available"
            raise
        atomic_json(status_path, {
            "status": "launched", "version": plan["version"],
            "transaction": plan["transaction"],
            "backup": str(backup) if backup.exists() else "",
        })
        return 0
    except Exception as exc:
        if owned_transaction or plan is None:
            try:
                atomic_json(status_path, {
                    "status": "error", "version": plan.get("version", "") if plan else "",
                    "transaction": plan.get("transaction", "") if plan else "",
                    "message": str(exc)[:1000],
                    "launch_error": launch_error_message,
                    "rollback": rollback,
                })
            except OSError:
                pass
        # Old-app recovery is also a legitimate application startup.
        if lock_handle is not None:
            lock_handle.close()
            lock_handle = None
        # A handled failure leaves the user's existing application usable.
        # Bring it back only after the original process actually exited, and
        # only when it was not replaced or the directory swap was rolled back.
        if plan and parent_exited and others_closed and not installer_started:
            target, _, backup = _paths(plan)
            if (target / plan["executable"]).is_file() and (not installed or not backup.exists()):
                try:
                    # The only permitted nonempty arguments are the explicit
                    # disposable packaged-smoke result hook. Preserve it so
                    # failure acceptance proves a real old process returned.
                    _relaunch(plan)
                except Exception:
                    pass
        return 1
    finally:
        if lock_handle is not None:
            lock_handle.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply an explicitly approved BananaFlow update.")
    parser.add_argument("--apply", required=True, type=Path)
    args = parser.parse_args()
    return apply_update_plan(args.apply)


if __name__ == "__main__":
    raise SystemExit(main())
