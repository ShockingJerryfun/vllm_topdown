#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <linux/mempolicy.h>
#include <sched.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <unistd.h>

/* Derived from numa_locality/exec_64k/exec_prefault.c and cache_reload/file_pages.c.
 * Only task-owned inode copies are mapped. Never alter sysfs or live code VMAs.
 * 64K is a requested file-cache folio path, not a promised base page or PTE size. */
int main(int argc, char **argv) {
    if (argc != 8 || (strcmp(argv[1], "4k") && strcmp(argv[1], "64k"))) {
        fprintf(stderr, "usage: prefault 4k|64k cpu node segments.tsv copy-root output.tsv require-cold(0|1)\n");
        return 2;
    }
    char *end;
    long cpu = strtol(argv[2], &end, 10);
    if (*end || cpu < 0 || cpu >= CPU_SETSIZE) return 2;
    long node = strtol(argv[3], &end, 10);
    if (*end || node < 0 || node >= (long)(sizeof(unsigned long) * 8)) return 2;
    if (strcmp(argv[7], "0") && strcmp(argv[7], "1")) return 2;
    int large = !strcmp(argv[1], "64k");
    int require_cold = !strcmp(argv[7], "1");
    if (sysconf(_SC_PAGESIZE) != 4096) {
        fprintf(stderr, "Requires 4096-byte kernel base pages\n"); return 3;
    }
    if (large) {
        FILE *setting = fopen("/sys/kernel/mm/transparent_hugepage/thp_exec_enabled", "r");
        unsigned long bits = 0;
        if (!setting || fscanf(setting, "%lx", &bits) != 1 || !(bits & 2)) {
            fprintf(stderr, "64K executable-folio path requires existing thp_exec_enabled BIT1; no setting changed\n");
            return 4;
        }
        fclose(setting);
    }
    cpu_set_t cpus;
    CPU_ZERO(&cpus);
    CPU_SET(cpu, &cpus);
    unsigned long mask = 1UL << node;
    if (sched_setaffinity(0, sizeof(cpus), &cpus) ||
        syscall(SYS_set_mempolicy, MPOL_BIND, &mask, sizeof(mask) * 8)) {
        perror("bind prefault CPU/node"); return 5;
    }
    char root[PATH_MAX], marker[PATH_MAX];
    if (!realpath(argv[5], root) || !strcmp(root, "/") ||
        snprintf(marker, sizeof(marker), "%s/.task_owned_copies", root) >= (int)sizeof(marker) ||
        access(marker, R_OK)) {
        perror("validate task-owned copy root"); return 6;
    }
    FILE *input = fopen(argv[4], "r");
    FILE *output = fopen(argv[6], "wx");
    if (!input || !output) { perror("open manifest/output"); return 7; }
    fprintf(output, "path\toffset\tsize\tmode\tresident_before\tfault_touches\tcpu\tnode\n");
    char path[8192], full[16384];
    unsigned long offset, size;
    int fields;
    while ((fields = fscanf(input, "%8191s %lu %lu", path, &offset, &size)) == 3) {
        if (!size || path[0] != '/' || strstr(path, "/../") || strstr(path, "/./") ||
            snprintf(full, sizeof(full), "%s%s", root, path) >= (int)sizeof(full)) return 8;
        int fd = open(full, O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
        struct stat st;
        if (fd < 0 || fstat(fd, &st) || !S_ISREG(st.st_mode) || st.st_nlink != 1 ||
            offset > (unsigned long)st.st_size || size > (unsigned long)st.st_size - offset) {
            perror("validate independent file"); return 9;
        }
        unsigned long granule = large ? 65536UL : 4096UL;
        unsigned long start = offset & ~(granule - 1);
        size_t length = (offset + size - start + 4095UL) & ~4095UL;
        size_t reserve_size = length + granule;
        void *reserved = mmap(NULL, reserve_size, PROT_NONE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
        if (reserved == MAP_FAILED) { perror("reserve own VMA"); return 10; }
        unsigned char *aligned = (void *)(((uintptr_t)reserved + granule - 1) & ~(granule - 1));
        unsigned char *mapped = mmap(aligned, length, PROT_READ | (large ? PROT_EXEC : 0),
                                    MAP_PRIVATE | MAP_FIXED, fd, start);
        if (mapped == MAP_FAILED) { perror("map own VMA"); return 11; }
        unsigned char *resident = calloc(length / 4096, 1);
        if (!resident || mincore(mapped, length, resident)) { perror("mincore"); return 12; }
        size_t present = 0;
        for (size_t index = 0; index < length / 4096; index++) present += resident[index] & 1;
        free(resident);
        if (require_cold && present) {
            fprintf(stderr, "Target code range already cached: %s pages=%zu\n", path, present);
            return 13;
        }
        if (!large && madvise(mapped, length, MADV_RANDOM)) { perror("MADV_RANDOM"); return 14; }
        volatile unsigned char accumulator = 0;
        unsigned long touches = 0;
        for (unsigned long position = start; position < offset + size; position += granule) {
            unsigned long target = position < offset ? offset : position;
            accumulator ^= mapped[target - start];
            touches++;
        }
        (void)accumulator;
        fprintf(output, "%s\t%lu\t%lu\t%s\t%zu\t%lu\t%ld\t%ld\n",
                path, offset, size, argv[1], present, touches, cpu, node);
        fflush(output);
        if (munmap(reserved, reserve_size) || close(fd)) { perror("release own VMA"); return 15; }
    }
    if (fields != EOF || ferror(input)) return 16;
    fclose(input);
    return fclose(output) ? 17 : 0;
}
