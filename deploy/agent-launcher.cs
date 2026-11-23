// Запускатель моста Windows «Studio Agent.exe»: своё имя, значок и сведения о версии в диспетчере задач и автозагрузке.
// Сам Python не встраивает: `pythonw.exe` среды `.venv` рядом — дочерний процесс (интерпретатор и `sys.executable`
// те же, что у прежней команды автозапуска), запускатель живёт его родителем и возвращает его код выхода.
// Версия — отдельным файлом сборки (`build-agent-launcher.ps1`).
using System;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;

[assembly: AssemblyTitle("Studio Agent")]
[assembly: AssemblyProduct("Studio Agent")]
[assembly: AssemblyDescription("Studio Agent")]

internal static class Launcher
{
    private const int NotReady = 78;  // нет среды или скрипта рядом — как у macOS launcher.m
    private const int Usage = 64;

    [StructLayout(LayoutKind.Sequential)]
    private struct BasicLimits
    {
        public long PerProcessUserTimeLimit, PerJobUserTimeLimit;
        public uint LimitFlags;
        public UIntPtr MinimumWorkingSetSize, MaximumWorkingSetSize;
        public uint ActiveProcessLimit;
        public UIntPtr Affinity;
        public uint PriorityClass, SchedulingClass;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct ExtendedLimits
    {
        public BasicLimits Basic;
        public ulong ReadOperationCount, WriteOperationCount, OtherOperationCount;
        public ulong ReadTransferCount, WriteTransferCount, OtherTransferCount;
        public UIntPtr ProcessMemoryLimit, JobMemoryLimit, PeakProcessMemoryUsed, PeakJobMemoryUsed;
    }

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern IntPtr CreateJobObject(IntPtr attributes, string name);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool SetInformationJobObject(IntPtr job, int infoClass, ref ExtendedLimits info, int size);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);

    // Мост гибнет вместе с запускателем (KILL_ON_JOB_CLOSE), а его собственные потомки в задание не входят
    // (SILENT_BREAKAWAY_OK): хелпер обновления, `claude.exe` и задания `jobs.py` живут как без запускателя.
    private static void Bind(Process child)
    {
        IntPtr job = CreateJobObject(IntPtr.Zero, null);
        if (job == IntPtr.Zero) return;
        var info = new ExtendedLimits();
        info.Basic.LimitFlags = 0x2000 | 0x1000;  // KILL_ON_JOB_CLOSE | SILENT_BREAKAWAY_OK
        if (SetInformationJobObject(job, 9, ref info, Marshal.SizeOf(typeof(ExtendedLimits))))
            AssignProcessToJobObject(job, child.Handle);
        // Дескриптор задания держится до выхода запускателя: его закрывает система.
    }

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
        using (var child = Process.Start(start))
        {
            Bind(child);
            child.WaitForExit();
            return child.ExitCode;
        }
    }
}
