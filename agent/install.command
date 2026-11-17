#!/bin/sh
# Python из python.org или Homebrew должен включать Tk.
set -eu
SOURCE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
for PYTHON in /opt/homebrew/bin/python3 /usr/local/bin/python3 python3; do
    if "$PYTHON" -c 'import sys, tkinter; assert sys.version_info >= (3, 11)' >/dev/null 2>&1; then
        exec "$PYTHON" "$SOURCE/install_macos.py" "$@"
    fi
done
printf '%s\n' 'Нужен Python 3.11+ с Tk: установите Python с python.org; для Homebrew нужен также python-tk.'
exit 1
