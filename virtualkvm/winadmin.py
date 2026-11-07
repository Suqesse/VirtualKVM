"""Windows elevation: restart as administrator and elevated sign-in autostart.

Windows blocks input from an ordinary process into windows of elevated
applications (UIPI). Running VirtualKVM elevated lifts that restriction. The
UAC secure desktop and the sign-in screen stay unreachable: those need a
SYSTEM service in the user's session, which VirtualKVM does not install.

The autostart is a Task Scheduler entry for the current user with
``HighestAvailable`` run level and an interactive token, so it needs no stored
password and starts without a UAC prompt after sign-in. Nothing here runs
through a shell; every command is an argument list.
"""
from __future__ import annotations

import ctypes as C
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
from xml.sax.saxutils import escape

from .native import is_windows_admin

TASK_NAME = "VirtualKVM Administrator"


class ElevationError(RuntimeError):
    """Elevation was refused, cancelled or is unavailable on this system."""


def supported() -> bool:
    return platform.system() == "Windows"


def is_admin() -> bool:
    return is_windows_admin()


def launch_command(arguments: list[str] | None = None) -> tuple[str, list[str]]:
    """Executable and arguments that start this same application again."""
    arguments = list(arguments or [])
    if getattr(sys, "frozen", False):
        return sys.executable, arguments
    # Prefer the windowless interpreter for the GUI when it exists.
    executable = Path(sys.executable)
    windowless = executable.with_name("pythonw.exe")
    if executable.name.lower() == "python.exe" and windowless.exists():
        executable = windowless
    return str(executable), ["-m", "virtualkvm", *arguments]


def current_user() -> str:
    user = os.environ.get("USERNAME", "").strip()
    if not user:
        raise ElevationError("Не удалось определить пользователя Windows")
    domain = os.environ.get("USERDOMAIN", "").strip()
    return f"{domain}\\{user}" if domain else user


def task_xml(command: str, arguments: list[str], user: str) -> str:
    """Task Scheduler definition: elevated start at this user's sign-in."""
    if not command or any(ord(c) < 32 for c in command + user + "".join(arguments)):
        raise ValueError("Invalid autostart command")
    joined = subprocess.list2cmdline(arguments)
    user, command, joined = escape(user), escape(command), escape(joined)
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>VirtualKVM с правами администратора: управление окнами программ, запущенных от администратора.</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{user}</UserId>
      <Delay>PT10S</Delay>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{user}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>5</Priority>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{command}</Command>
      <Arguments>{joined}</Arguments>
    </Exec>
  </Actions>
</Task>
"""


def _schtasks() -> str:
    root = os.environ.get("SystemRoot", r"C:\Windows")
    return str(Path(root) / "System32" / "schtasks.exe")


def _popen_options() -> dict:
    return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}


class _SHELLEXECUTEINFOW(C.Structure):
    _fields_ = [("cbSize", C.c_uint32), ("fMask", C.c_uint32), ("hwnd", C.c_void_p),
                ("lpVerb", C.c_wchar_p), ("lpFile", C.c_wchar_p), ("lpParameters", C.c_wchar_p),
                ("lpDirectory", C.c_wchar_p), ("nShow", C.c_int32), ("hInstApp", C.c_void_p),
                ("lpIDList", C.c_void_p), ("lpClass", C.c_wchar_p), ("hkeyClass", C.c_void_p),
                ("dwHotKey", C.c_uint32), ("hIconOrMonitor", C.c_void_p), ("hProcess", C.c_void_p)]


def _run_elevated(executable: str, arguments: list[str], *, wait: bool, show: int = 0) -> int | None:
    """ShellExecuteEx with the ``runas`` verb; returns the exit code when waiting."""
    if not supported():
        raise ElevationError("Повышение прав доступно только в Windows")
    info = _SHELLEXECUTEINFOW()
    info.cbSize = C.sizeof(_SHELLEXECUTEINFOW)
    info.fMask = 0x00000040 if wait else 0  # SEE_MASK_NOCLOSEPROCESS
    info.lpVerb, info.lpFile = "runas", executable
    info.lpParameters = subprocess.list2cmdline(arguments)
    info.nShow = show
    shell32 = C.WinDLL("shell32", use_last_error=True)
    shell32.ShellExecuteExW.argtypes = [C.POINTER(_SHELLEXECUTEINFOW)]
    shell32.ShellExecuteExW.restype = C.c_int32
    if not shell32.ShellExecuteExW(C.byref(info)):
        error = C.get_last_error()
        if error == 1223:  # ERROR_CANCELLED: the user declined the UAC prompt
            raise ElevationError("Запрос прав администратора отклонён")
        raise ElevationError(f"Windows не выдала права администратора (код {error})")
    if not wait:
        return None
    kernel32 = C.WinDLL("kernel32", use_last_error=True)
    kernel32.WaitForSingleObject.argtypes = [C.c_void_p, C.c_uint32]
    kernel32.GetExitCodeProcess.argtypes = [C.c_void_p, C.POINTER(C.c_uint32)]
    kernel32.CloseHandle.argtypes = [C.c_void_p]
    try:
        kernel32.WaitForSingleObject(info.hProcess, 60000)
        code = C.c_uint32()
        if not kernel32.GetExitCodeProcess(info.hProcess, C.byref(code)):
            raise ElevationError("Не удалось получить результат команды администратора")
        return code.value
    finally:
        kernel32.CloseHandle(info.hProcess)


def restart_elevated(arguments: list[str] | None = None) -> None:
    """Start an elevated copy; the caller should then quit this instance."""
    executable, args = launch_command(arguments)
    _run_elevated(executable, args, wait=False, show=1)


def _schtasks_run(arguments: list[str]) -> int:
    if is_admin():
        result = subprocess.run([_schtasks(), *arguments], stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=30, **_popen_options())
        return result.returncode
    code = _run_elevated(_schtasks(), arguments, wait=True)
    return 1 if code is None else code


def autostart_enabled() -> bool:
    if not supported():
        return False
    try:
        result = subprocess.run([_schtasks(), "/Query", "/TN", TASK_NAME], stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=15, **_popen_options())
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def enable_autostart() -> None:
    """Register the elevated sign-in task for the current user (asks UAC once)."""
    if not supported():
        raise ElevationError("Автозапуск с правами администратора доступен только в Windows")
    command, arguments = launch_command()
    definition = task_xml(command, arguments, current_user())
    handle, name = tempfile.mkstemp(prefix="virtualkvm-task-", suffix=".xml")
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(definition.encode("utf-16"))
        code = _schtasks_run(["/Create", "/TN", TASK_NAME, "/XML", name, "/F"])
    finally:
        try:
            os.unlink(name)
        except OSError:
            pass
    if code != 0:
        raise ElevationError(f"Планировщик заданий не создал автозапуск (код {code})")


def disable_autostart() -> None:
    if not supported():
        return
    code = _schtasks_run(["/Delete", "/TN", TASK_NAME, "/F"])
    if code != 0 and autostart_enabled():
        raise ElevationError(f"Планировщик заданий не удалил автозапуск (код {code})")
