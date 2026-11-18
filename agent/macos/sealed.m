// Точка входа дистрибутива Developer ID. Весь исполняемый код принадлежит одному подписанному .app.
#import <Foundation/Foundation.h>
#import <Security/Security.h>
#include <Python.h>
#include <dlfcn.h>
#include <pwd.h>
#include <unistd.h>

#define LOAD(name) \
    __typeof__(&name) call_##name = (__typeof__(&name))dlsym(runtime, #name); \
    if (!call_##name) return 78

int main(int argc, char **argv) {
    @autoreleasepool {
        NSBundle *bundle = NSBundle.mainBundle;
        SecStaticCodeRef code = NULL;
        if (SecStaticCodeCreateWithPath((__bridge CFURLRef)bundle.bundleURL, kSecCSDefaultFlags, &code)
            != errSecSuccess) return 78;
        OSStatus valid = SecStaticCodeCheckValidity(code, kSecCSStrictValidate, NULL);
        CFRelease(code);
        if (valid != errSecSuccess) return 78;
        BOOL check = argc == 2 && strcmp(argv[1], "--check-runtime") == 0;
        // Старый LaunchAgent передавал путь выпуска. Загрузку определяет только собственный bundle.
        if (!(argc == 1 || check || (argc == 3 && strcmp(argv[1], "--studio-repo") == 0))) return 64;
        struct passwd *user = getpwuid(getuid());
        if (!user || setenv("HOME", user->pw_dir, 1) != 0) return 78;
        for (const char **key = (const char*[]){"STUDIO_AGENT_HOME", "TCLLIBPATH", "TCL_LIBRARY",
                                               "TK_LIBRARY", NULL}; *key; ++key) unsetenv(*key);
        NSString *resources = bundle.resourcePath;
#if defined(__arm64__)
        NSString *arch = @"arm64";
#else
        NSString *arch = @"x86_64";
#endif
        NSString *prefix = [[resources stringByAppendingPathComponent:@"runtime"] stringByAppendingPathComponent:arch];
        NSString *version = [NSString stringWithFormat:@"%d.%d", PY_MAJOR_VERSION, PY_MINOR_VERSION];
        NSString *lib = [prefix stringByAppendingPathComponent:@"lib"];
        // Tcl/Tk автономного runtime используют свои библиотеки независимо от сеанса запуска.
        NSString *tcl = [lib stringByAppendingPathComponent:@"tcl9.0"];
        NSString *tk = [lib stringByAppendingPathComponent:@"tk9.0"];
        if (setenv("TCL_LIBRARY", tcl.fileSystemRepresentation, 1) != 0 ||
            setenv("TK_LIBRARY", tk.fileSystemRepresentation, 1) != 0) return 78;
        NSString *stdlib = [lib stringByAppendingPathComponent:[@"python" stringByAppendingString:version]];
        NSString *repo = [resources stringByAppendingPathComponent:@"agent"];
        NSString *python = [prefix stringByAppendingPathComponent:@"bin/python3"];
        NSString *dylib = [lib stringByAppendingPathComponent:[NSString stringWithFormat:@"libpython%@.dylib", version]];
        void *runtime = dlopen(dylib.fileSystemRepresentation, RTLD_NOW | RTLD_GLOBAL);
        if (!runtime) return 78;
        LOAD(PyConfig_InitIsolatedConfig);
        LOAD(PyConfig_SetBytesString);
        LOAD(PyWideStringList_Append);
        LOAD(Py_DecodeLocale);
        LOAD(PyMem_RawFree);
        LOAD(PyStatus_Exception);
        LOAD(PyConfig_Clear);
        LOAD(Py_InitializeFromConfig);
        LOAD(Py_RunMain);
        PyConfig config;
        call_PyConfig_InitIsolatedConfig(&config);
        config.site_import = 0;
        config.write_bytecode = 0;
        config.module_search_paths_set = 1;
        PyStatus status = call_PyConfig_SetBytesString(&config, &config.home, prefix.fileSystemRepresentation);
        if (!call_PyStatus_Exception(status))
            status = call_PyConfig_SetBytesString(&config, &config.executable, python.fileSystemRepresentation);
        for (NSString *path in @[repo, stdlib, [stdlib stringByAppendingPathComponent:@"lib-dynload"],
                                 [stdlib stringByAppendingPathComponent:@"site-packages"]]) {
            if (call_PyStatus_Exception(status)) break;
            wchar_t *wide = call_Py_DecodeLocale(path.fileSystemRepresentation, NULL);
            if (!wide) { call_PyConfig_Clear(&config); return 78; }
            status = call_PyWideStringList_Append(&config.module_search_paths, wide);
            call_PyMem_RawFree(wide);
        }
        const char *startup = check
            ? "import tkinter, HIServices, ScreenCaptureKit, studio_agent.app; tkinter.Tcl(); from studio_agent import VERSION; print(VERSION)"
            : "import tkinter; tkinter.Tk.readprofile=lambda *args: None\nfrom studio_agent.app import main\nraise SystemExit(main([]))";
        if (!call_PyStatus_Exception(status))
            status = call_PyConfig_SetBytesString(&config, &config.run_command, startup);
        if (!call_PyStatus_Exception(status)) status = call_Py_InitializeFromConfig(&config);
        call_PyConfig_Clear(&config);
        if (call_PyStatus_Exception(status)) return 78;
        return call_Py_RunMain();
    }
}
