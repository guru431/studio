// Запускатель моста Windows «Studio Agent.exe»: своё имя, значок и сведения о версии в диспетчере задач и автозагрузке.
// Сам Python не встраивает: `pythonw.exe` среды `.venv` рядом — дочерний процесс (интерпретатор и `sys.executable`
// те же, что у прежней команды автозапуска), запускатель живёт его родителем и возвращает его код выхода.
// Версия — отдельным файлом сборки (`build-agent-launcher.ps1`).
using System;
using System.Diagnostics;
using System.IO;
using System.Reflection;

[assembly: AssemblyTitle("Studio Agent")]
[assembly: AssemblyProduct("Studio Agent")]
[assembly: AssemblyDescription("Studio Agent")]

internal static class Launcher
{
    private const int NotReady = 78;  // нет среды или скрипта рядом — как у macOS launcher.m
    private const int Usage = 64;

    private static int Main(string[] args)
    {
        string repo = AppDomain.CurrentDomain.BaseDirectory;
        string python = Path.Combine(repo, @".venv\Scripts\pythonw.exe");
        string script = Path.Combine(repo, "studio_agent.pyw");
        if (args.Length > 1 || (args.Length == 1 && args[0] != "--probe")) return Usage;
        if (!File.Exists(python) || !File.Exists(script)) return NotReady;
        if (args.Length == 1) return 0;  // --probe: запускатель исполняется, среда и скрипт на месте
        var start = new ProcessStartInfo
        {
            FileName = python,
            Arguments = "\"" + script + "\"",  // в пути Windows кавычки быть не может
            WorkingDirectory = repo,
            UseShellExecute = false
        };
        // Без Job Object: антивирус, завершивший exe перед карантином, не должен убить мост. Мост сам следит за этим
        // pid (`autostart.follow_launcher`): exe на месте — выходит, пропал — возвращает автозапуск на pythonw.exe.
        start.EnvironmentVariables["STUDIO_AGENT_LAUNCHER"] = Process.GetCurrentProcess().Id.ToString();
        using (var child = Process.Start(start))
        {
            child.WaitForExit();
            return child.ExitCode;
        }
    }
}
