// macOS видит Studio Agent как отдельное приложение при проверке прав.
#import <Foundation/Foundation.h>
#include <Python.h>
#include <dlfcn.h>

#define LOAD(name) \
    __typeof__(&name) call_##name = (__typeof__(&name))dlsym(runtime, #name); \
    if (!call_##name) return 78

int main(int argc, char **argv) {
    @autoreleasepool {
        NSString *repo;
        if (argc == 3 && strcmp(argv[1], "--studio-repo") == 0) {
            repo = [NSString stringWithUTF8String:argv[2]];
        } else if (argc == 1) {
            const char *override = getenv("STUDIO_AGENT_HOME");
            NSString *home = override ? [NSString stringWithUTF8String:override]
                : [NSHomeDirectory() stringByAppendingPathComponent:@"Library/Application Support/Studio Agent"];
            repo = [home stringByAppendingPathComponent:@"app/current"];
        } else {
            return 64;
        }
        if (![repo isAbsolutePath]) return 78;
        NSString *venv = [repo stringByAppendingPathComponent:@".venv"];
        NSString *configPath = [venv stringByAppendingPathComponent:@"pyvenv.cfg"];
        NSString *text = [NSString stringWithContentsOfFile:configPath encoding:NSUTF8StringEncoding error:NULL];
        NSString *pythonHome = nil, *version = nil;
        for (NSString *line in [text componentsSeparatedByCharactersInSet:[NSCharacterSet newlineCharacterSet]]) {
            NSRange separator = [line rangeOfString:@" = "];
            if (separator.location == NSNotFound) continue;
            NSString *key = [line substringToIndex:separator.location];
            NSString *value = [line substringFromIndex:separator.location + separator.length];
            if ([key isEqualToString:@"home"]) pythonHome = value;
            if ([key isEqualToString:@"version"]) version = value;
        }
        NSArray *parts = [version componentsSeparatedByString:@"."];
        if (![pythonHome isAbsolutePath] || parts.count < 2) return 78;
        if (![parts[0] isEqualToString:[NSString stringWithFormat:@"%d", PY_MAJOR_VERSION]] ||
            ![parts[1] isEqualToString:[NSString stringWithFormat:@"%d", PY_MINOR_VERSION]]) return 78;
        NSString *library = [NSString stringWithFormat:@"lib/libpython%@.%@.dylib", parts[0], parts[1]];
        NSString *prefix = [pythonHome stringByDeletingLastPathComponent];
        NSArray *libraries = @[[prefix stringByAppendingPathComponent:library],
                              [prefix stringByAppendingPathComponent:@"Python"],
                              [prefix stringByAppendingPathComponent:@"Python3"]];
        void *runtime = NULL;
        for (NSString *path in libraries) {
            runtime = dlopen(path.fileSystemRepresentation, RTLD_NOW | RTLD_GLOBAL);
            if (runtime) break;
        }
        if (!runtime) return 78;
        LOAD(PyConfig_InitPythonConfig);
        LOAD(PyConfig_SetBytesString);
        LOAD(PyConfig_SetBytesArgv);
        LOAD(PyConfig_Clear);
        LOAD(PyStatus_Exception);
        LOAD(Py_InitializeFromConfig);
        LOAD(Py_RunMain);
        PyConfig config;
        call_PyConfig_InitPythonConfig(&config);
        config.parse_argv = 0;
        // Python сохраняет пути venv, а исполняемый файл процесса — Studio Agent.
        NSString *python = [venv stringByAppendingPathComponent:@"bin/python"];
        NSString *script = [repo stringByAppendingPathComponent:@"studio_agent.pyw"];
        PyStatus status = call_PyConfig_SetBytesString(&config, &config.program_name, python.fileSystemRepresentation);
        if (!call_PyStatus_Exception(status))
            status = call_PyConfig_SetBytesString(&config, &config.executable, python.fileSystemRepresentation);
        if (!call_PyStatus_Exception(status))
            status = call_PyConfig_SetBytesString(&config, &config.run_filename, script.fileSystemRepresentation);
        char *arguments[] = {(char *)script.fileSystemRepresentation};
        if (!call_PyStatus_Exception(status)) status = call_PyConfig_SetBytesArgv(&config, 1, arguments);
        if (!call_PyStatus_Exception(status)) status = call_Py_InitializeFromConfig(&config);
        call_PyConfig_Clear(&config);
        if (call_PyStatus_Exception(status)) return 78;
        return call_Py_RunMain();
    }
}
