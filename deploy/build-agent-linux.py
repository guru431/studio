"""Build a POSIX sh installer from the profiled agent release ZIP."""
import argparse
import re
import zipfile
from pathlib import Path


def _write(target, name, data):
    # Fixed metadata and stored members give Windows and Linux identical
    # outputs even when the builders use different deflate implementations.
    member = zipfile.ZipInfo(name)
    member.create_system = 3
    member.external_attr = 0o100644 << 16
    target.writestr(member, data)


def build(archive: Path, output: Path) -> None:
    with zipfile.ZipFile(archive) as source:
        files = {name.removeprefix("studio-agent/"): name for name in source.namelist()
                 if name.startswith("studio-agent/")}
        members = ["requirements.txt", "constraints.txt", "studio_agent/__init__.py",
                   *(name for name in sorted(files) if re.fullmatch(r"studio_agent/(?!__init__\.py$)[\w]+\.py", name))]
        bootstrap = source.read(files["install_linux.py"])
        with output.open("wb") as stream:
            stream.write(b'#!/bin/sh\nexec "${STUDIO_PYTHON:-python3}" -I "$0" "$@"\nexit 1\n')
            with zipfile.ZipFile(stream, "w") as target:
                _write(target, "__main__.py", bootstrap)
                for member in members:
                    _write(target, member, source.read(files[member]))
    output.chmod(0o755)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    build(args.archive, args.output)
