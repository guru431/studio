"""Настоящий pythonw и консольный CLI: оба запуска SDK остаются без консоли Windows."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

CLI_SOURCE = r'''
using System;
using System.IO;
using System.Runtime.InteropServices;

class TestCli {
    [DllImport("kernel32.dll")]
    static extern IntPtr GetConsoleWindow();

    static void Main(string[] args) {
        bool version = Array.IndexOf(args, "-v") >= 0;
        string phase = version ? "version" : "turn";
        File.AppendAllText(Environment.GetEnvironmentVariable("STUDIO_TEST_CONSOLE_FILE"),
                           phase + ":" + GetConsoleWindow().ToInt64() + "\n");
        Console.WriteLine(version ? "2.1.312 (Claude Code)" : "{\"ready\":true}");
        if (!version) Console.ReadLine();
    }
}
'''

GUI_SOURCE = '''
import asyncio
import ctypes
import json
import sys
import traceback
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions
from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport
from studio_agent import cli

async def check():
    assert ctypes.windll.kernel32.GetConsoleWindow() == 0
    options = ClaudeAgentOptions(cli_path=sys.argv[1], cwd=Path(sys.argv[1]).parent,
                                 stderr=cli._drop)
    transport = SubprocessCLITransport(prompt="", options=options)
    try:
        await transport.connect()
        assert transport.is_ready()
        assert await anext(transport.read_messages()) == {"ready": True}
    finally:
        await transport.close()
    assert not transport.is_ready()

report = {}
try:
    asyncio.run(check())
    report["ok"] = True
except BaseException:
    report["error"] = traceback.format_exc()
Path(sys.argv[2]).write_text(json.dumps(report), encoding="utf-8")
'''


def test_sdk_version_and_turn_have_no_console_when_started_from_pythonw(tmp_path):
    compiler = Path(os.environ["WINDIR"]) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    source = tmp_path / "TestCli.cs"
    binary = tmp_path / "claude.exe"
    source.write_text(CLI_SOURCE, encoding="utf-8")
    built = subprocess.run([str(compiler), "/nologo", "/target:exe", f"/out:{binary}", str(source)],
                           capture_output=True, timeout=20, creationflags=subprocess.CREATE_NO_WINDOW)
    assert built.returncode == 0, built.stdout.decode("utf-8", "replace")
    gui = tmp_path / "gui.pyw"
    gui.write_text(GUI_SOURCE, encoding="utf-8")
    report = tmp_path / "report.json"
    consoles = tmp_path / "consoles.txt"
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
           "STUDIO_TEST_CONSOLE_FILE": str(consoles)}
    env.pop("CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK", None)
    result = subprocess.run([str(Path(sys.executable).with_name("pythonw.exe")), str(gui), str(binary), str(report)],
                            cwd=tmp_path, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, timeout=20, creationflags=subprocess.CREATE_NO_WINDOW)
    assert result.returncode == 0
    assert report.is_file(), "pythonw не создал отчёт"
    assert json.loads(report.read_text("utf-8")) == {"ok": True}
    assert consoles.read_text("utf-8").splitlines() == ["version:0", "turn:0"]
