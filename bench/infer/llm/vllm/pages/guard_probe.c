#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <link.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/prctl.h>
#include <sys/wait.h>
#include <unistd.h>

/* Native diagnostic only. Build once as executable and once with
 * -DGUARD_PROBE_DSO -shared -fPIC. Its constructor checks audit timing before
 * dlopen/dlmopen return; no GPU, global THP settings, or cache eviction. */
static int check_flags(const char *stage) {
    FILE *stream = fopen("/proc/self/smaps", "r");
    if (!stream) { perror("smaps"); return 1; }
    char *line = NULL;
    size_t capacity = 0;
    unsigned long begin = 0, end = 0, inode = 0, offset = 0;
    char perms[5] = "", device[32], path[8192] = "";
    unsigned long executable = 0, missing = 0, data_nh = 0;
    while (getline(&line, &capacity, stream) >= 0) {
        int consumed = 0;
        if (sscanf(line, "%lx-%lx %4s %lx %31s %lu %n",
                   &begin, &end, perms, &offset, device, &inode, &consumed) == 6) {
            const char *name = line + consumed;
            while (*name == ' ') name++;
            size_t length = strcspn(name, "\r\n");
            if (length >= sizeof(path)) { free(line); fclose(stream); return 2; }
            memcpy(path, name, length);
            path[length] = 0;
        } else if (!strncmp(line, "VmFlags:", 8)) {
            int nh = strstr(line, " nh ") != NULL || strstr(line, " nh\n") != NULL;
            int code = perms[2] == 'x' && inode && path[0] == '/' && strncmp(path, "/dev/", 5);
            if (code) { executable++; missing += !nh; }
            else if (perms[2] != 'x') data_nh += nh;
        }
    }
    int failed = ferror(stream);
    free(line);
    fclose(stream);
    long thp_disabled = prctl(PR_GET_THP_DISABLE, 0, 0, 0, 0);
    fprintf(stderr, "guard_probe stage=%s pid=%ld code=%lu missing_nh=%lu nonexec_nh=%lu thp_disabled=%ld\n",
            stage, (long)getpid(), executable, missing, data_nh, thp_disabled);
    return failed || !executable || missing || data_nh || thp_disabled != 0;
}

__attribute__((constructor)) static void check_constructor(void) {
    if (check_flags("constructor")) _exit(91);
}

#ifdef GUARD_PROBE_DSO
int guard_probe_value(void) { return 37; }
#else
static int load_probe(const char *path, int new_namespace) {
    void *handle = new_namespace ? dlmopen(LM_ID_NEWLM, path, RTLD_NOW | RTLD_LOCAL)
                                 : dlopen(path, RTLD_NOW | RTLD_LOCAL);
    if (!handle) { fprintf(stderr, "probe load failed: %s\n", dlerror()); return 2; }
    int (*value)(void) = (int (*)(void))dlsym(handle, "guard_probe_value");
    if (!value || value() != 37 || check_flags(new_namespace ? "dlmopen" : "dlopen")) return 3;
    if (dlclose(handle)) return 4;
    return check_flags("dlclose");
}

int main(int argc, char **argv) {
    if (argc == 2 && !strcmp(argv[1], "--exec-child")) return check_flags("exec-child");
    if (argc != 3) { fprintf(stderr, "usage: guard_probe probe_dso.so hold_seconds\n"); return 2; }
    char *end;
    unsigned long seconds = strtoul(argv[2], &end, 10);
    if (*end || seconds > 3600) return 2;
    if (check_flags("main")) return 3;
    size_t bytes = 4UL * 1024 * 1024;
    unsigned char *data = mmap(NULL, bytes, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    if (data == MAP_FAILED) return 4;
    for (size_t offset = 0; offset < bytes; offset += 4096) data[offset] = (unsigned char)offset;
    if (load_probe(argv[1], 0) || load_probe(argv[1], 1)) return 5;
    pid_t child = fork();
    if (child < 0) return 6;
    if (!child) {
        if (check_flags("fork-child")) _exit(92);
        execl("/proc/self/exe", "/proc/self/exe", "--exec-child", (char *)NULL);
        _exit(93);
    }
    int status;
    if (waitpid(child, &status, 0) != child || !WIFEXITED(status) || WEXITSTATUS(status)) return 7;
    fprintf(stderr, "guard_probe holding pid=%ld seconds=%lu; inspect PFNs/folios independently\n", (long)getpid(), seconds);
    while (seconds) seconds = sleep((unsigned int)seconds);
    int result = check_flags("after-hold");
    if (munmap(data, bytes)) return 8;
    return result;
}
#endif
