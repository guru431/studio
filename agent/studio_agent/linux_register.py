"""Register one root-owned, non-setuid entry point for installed Linux accounts."""
import ast
import json
import os
import secrets
import stat
import subprocess
import sys

SYSTEM_BIN = "/usr/local/bin"
COMMAND = "studio-agent"


def _script(source, uid, name):
    return b"#!/usr/bin/python3 -IS\n" + source + f"\ndispatch({uid!r}, {name!r})\n".encode()


def existing_binding(directory, source):
    fd = os.open(COMMAND, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    with os.fdopen(fd, "rb") as current:
        info = os.fstat(current.fileno())
        max_size = len(source) + 4096
        if (stat.S_ISREG(info.st_mode) and info.st_uid == 0 and stat.S_IMODE(info.st_mode) == 0o755
                and info.st_size <= max_size):
            data = current.read(max_size + 1)
            tail = data.splitlines()[-1:]  # Never import or execute an existing command.
            if tail and len(tail[0]) <= 4096 and tail[0].startswith(b"dispatch("):
                try:
                    binding = ast.literal_eval(tail[0][len(b"dispatch"):].decode())
                except (ValueError, SyntaxError, RecursionError):
                    binding = ()
                if (type(binding) is tuple and len(binding) == 2 and type(binding[0]) is int
                        and 0 <= binding[0] < 2**32 - 1 and isinstance(binding[1], str)
                        and binding[1] and data == _script(source, *binding)):
                    return binding
    raise FileExistsError("Путь системной команды занят другим файлом. Файл сохранён.")


def publish_command(account, dispatcher, source, destination=SYSTEM_BIN):
    if os.geteuid() != 0:
        raise PermissionError("Системную команду регистрирует администратор")
    dispatcher.checked_launcher(account)
    binding = (account.pw_uid, account.pw_name)
    payload = _script(source, *binding)
    with dispatcher.safe_directory(destination, {0}, create=True) as directory:
        temp = ".studio-agent-" + secrets.token_hex(12)
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(payload)
                output.flush()
                os.fchmod(output.fileno(), 0o755)
                os.fsync(output.fileno())
            try:
                os.link(temp, COMMAND, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
                os.fsync(directory)
            except FileExistsError:
                return existing_binding(directory, source)
        finally:
            os.unlink(temp, dir_fd=directory)
    return binding


def register(account):
    import pkgutil
    from . import linux_dispatch

    source = pkgutil.get_data("studio_agent", "linux_dispatch.py")
    try:
        with linux_dispatch.safe_directory(SYSTEM_BIN, {0}) as directory:
            found = existing_binding(directory, source)
    except FileNotFoundError:
        pass
    else:
        linux_dispatch.checked_launcher(account)
        return found
    if os.geteuid() == 0:
        return publish_command(account, linux_dispatch, source)
    # sudo runs the distribution interpreter with isolated imports. Include both
    # trusted installer modules by value; root never reopens a user-owned Python file.
    registrar = pkgutil.get_data("studio_agent", "linux_register.py")
    program = "\n".join((source.decode(), registrar.decode(), "import pwd",
        "try:", f"    print(json.dumps(publish_command(pwd.getpwuid({os.getuid()}), sys.modules['__main__'], {source!r})))",
        "except (OSError, ValueError, KeyError) as error:", "    print(error)", "    sys.exit(3)"))
    try:
        result = subprocess.run(["/usr/bin/sudo", "-n", "--", "/usr/bin/python3", "-IS", "-c", program],
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                text=True, timeout=20)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode == 3:
        raise OSError(result.stdout.strip())
    return tuple(json.loads(result.stdout)) if result.returncode == 0 else None
