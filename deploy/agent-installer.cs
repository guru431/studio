// Один установочный EXE: пакет внутри, установка прежним install.ps1 в сеансе владельца.
using System;
using System.Diagnostics;
using System.IO;
using System.IO.Compression;
using System.Reflection;
using System.Text;

[assembly: AssemblyTitle("Установка Studio Agent")]
[assembly: AssemblyProduct("Studio Agent")]

internal static class Installer
{
    // Экранирование argv по правилам Windows, без cmd.exe и подстановок PowerShell.
    private static string Quote(string value)
    {
        var result = new StringBuilder("\"");
        int slashes = 0;
        foreach (char c in value)
        {
            if (c == '\\') { slashes++; continue; }
            result.Append('\\', slashes * (c == '"' ? 2 : 1) + (c == '"' ? 1 : 0));
            result.Append(c);
            slashes = 0;
        }
        result.Append('\\', slashes * 2);
        return result.Append('"').ToString();
    }

    private static void Extract(string directory)
    {
        const string prefix = "studio-agent/";
        string root = Path.GetFullPath(directory) + Path.DirectorySeparatorChar;
        using (var stream = Assembly.GetExecutingAssembly().GetManifestResourceStream("studio-agent.zip"))
        using (var archive = new ZipArchive(stream, ZipArchiveMode.Read))
        {
            foreach (var entry in archive.Entries)
            {
                if (!entry.FullName.StartsWith(prefix, StringComparison.Ordinal) ||
                    entry.FullName.IndexOf('\\') >= 0 || entry.FullName.IndexOf(':') >= 0)
                    throw new InvalidDataException("Неверный путь в пакете.");
                string target = Path.GetFullPath(Path.Combine(root, entry.FullName.Substring(prefix.Length)));
                if (target + Path.DirectorySeparatorChar == root) continue;
                if (!target.StartsWith(root, StringComparison.OrdinalIgnoreCase))
                    throw new InvalidDataException("Неверный путь в пакете.");
                if (entry.FullName.EndsWith("/")) { Directory.CreateDirectory(target); continue; }
                Directory.CreateDirectory(Path.GetDirectoryName(target));
                using (var source = entry.Open())
                using (var dest = new FileStream(target, FileMode.CreateNew, FileAccess.Write))
                    source.CopyTo(dest);
            }
        }
    }

    private static int Main(string[] args)
    {
        Console.OutputEncoding = new UTF8Encoding(false);
        Console.Title = "Установка Studio Agent";
        Console.WriteLine("Установка Studio Agent\n");
        string directory = Path.Combine(Path.GetTempPath(), "studio-agent-setup-" + Guid.NewGuid().ToString("N"));
        int code = 1;
        try
        {
            Directory.CreateDirectory(directory);
            Extract(directory);
            string script = Path.Combine(directory, "install.ps1");
            var command = new StringBuilder("-NoLogo -NoProfile -ExecutionPolicy Bypass -File ");
            command.Append(Quote(script));
            foreach (string arg in args) command.Append(' ').Append(Quote(arg));
            var start = new ProcessStartInfo
            {
                FileName = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.System),
                                        @"WindowsPowerShell\v1.0\powershell.exe"),
                Arguments = command.ToString(),
                WorkingDirectory = directory,
                UseShellExecute = false
            };
            using (var process = Process.Start(start))
            {
                process.WaitForExit();
                code = process.ExitCode;
            }
            if (code == 0) Console.WriteLine("\nStudio Agent установлен.");
        }
        catch (Exception)
        {
            Console.Error.WriteLine("Не удалось запустить установку Studio Agent. Скачайте установщик заново и повторите запуск.");
        }
        finally
        {
            // Убирается только созданная этим запуском временная папка; установленная копия остаётся.
            try { if (Directory.Exists(directory)) Directory.Delete(directory, true); }
            catch (IOException) { }
            catch (UnauthorizedAccessException) { }
        }
        if (code != 0 && !Console.IsInputRedirected)
        {
            Console.Error.WriteLine("\nУстановка не завершена. Сообщение об ошибке — выше. Нажмите Enter, чтобы закрыть окно.");
            Console.ReadLine();
        }
        return code;
    }
}
