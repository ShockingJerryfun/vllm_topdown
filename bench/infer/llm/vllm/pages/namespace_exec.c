#define _GNU_SOURCE
#include <errno.h>
#include <limits.h>
#include <sched.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mount.h>
#include <sys/stat.h>
#include <unistd.h>

/* Static Linux launcher: no target address-space remapping. Mount changes are
 * confined to a fresh, recursively private mount namespace inherited by exec. */
int main(int argc, char **argv) {
    if (argc < 4) {
        fprintf(stderr, "usage: namespace_exec paths.txt copy-root command [args...]\n");
        return 2;
    }
    char root[PATH_MAX], marker[PATH_MAX], source[PATH_MAX], line[PATH_MAX];
    const char *guard = getenv("CODE_PAGE_GUARD");
    if (guard && (!*guard || guard[0] != '/' || strpbrk(guard, ":\t\r\n ") ||
                  strstr(guard, "/../") || strstr(guard, "/./") ||
                  (getenv("LD_AUDIT") && *getenv("LD_AUDIT")))) {
        fprintf(stderr, "Invalid code guard or conflicting LD_AUDIT\n"); return 12;
    }
    if (!realpath(argv[2], root) || strcmp(root, "/") == 0 ||
        snprintf(marker, sizeof(marker), "%s/.task_owned_copies", root) >= (int)sizeof(marker) ||
        access(marker, R_OK)) {
        perror("validate task-owned copy root"); return 3;
    }
    FILE *manifest = fopen(argv[1], "r");
    if (!manifest) { perror("open paths"); return 4; }
    if (unshare(CLONE_NEWNS) || mount(NULL, "/", NULL, MS_REC | MS_PRIVATE, NULL)) {
        perror("create private mount namespace"); return 5;
    }
    char namespace_id[128];
    ssize_t length = readlink("/proc/self/ns/mnt", namespace_id, sizeof(namespace_id) - 1);
    if (length < 0) { perror("read mount namespace"); return 6; }
    namespace_id[length] = '\0';
    fprintf(stderr, "private_mount_namespace=%s\n", namespace_id);
    unsigned long count = 0, guard_count = 0;
    while (fgets(line, sizeof(line), manifest)) {
        line[strcspn(line, "\r\n")] = '\0';
        if (line[0] != '/' || strstr(line, "/../") || strstr(line, "/./") ||
            strpbrk(line, "\t ") ||
            snprintf(source, sizeof(source), "%s%s", root, line) >= (int)sizeof(source)) {
            fprintf(stderr, "Invalid manifest path\n"); return 7;
        }
        struct stat original, copied;
        if (stat(line, &original) || lstat(source, &copied) ||
            !S_ISREG(original.st_mode) || !S_ISREG(copied.st_mode) || copied.st_nlink != 1 ||
            copied.st_size != original.st_size ||
            (copied.st_dev == original.st_dev && copied.st_ino == original.st_ino)) {
            fprintf(stderr, "Independent file validation failed: %s errno=%d\n", line, errno);
            return 8;
        }
        if (mount(source, line, NULL, MS_BIND, NULL) ||
            mount(NULL, line, NULL, MS_REMOUNT | MS_BIND | MS_RDONLY, NULL)) {
            perror("bind task-owned copy"); return 9;
        }
        if (guard && !strcmp(guard, line)) guard_count++;
        count++;
    }
    if (ferror(manifest) || !count) { fprintf(stderr, "Empty/invalid paths manifest\n"); return 10; }
    fclose(manifest);
    fprintf(stderr, "isolated_elf_files=%lu\n", count);
    if (guard) {
        char canonical[PATH_MAX];
        if (guard_count != 1 || !realpath(guard, canonical) || strcmp(canonical, guard) ||
            setenv("LD_AUDIT", guard, 1)) {
            fprintf(stderr, "Code guard must name one canonical copied ELF\n"); return 12;
        }
        /* Apply to this private namespace's descendants after NUMA binding and
         * copy mounts. No host/container-wide loader environment is modified. */
        fprintf(stderr, "code_page_guard=%s\n", guard);
    }
    execvp(argv[3], argv + 3);
    perror("execvp");
    return 11;
}
