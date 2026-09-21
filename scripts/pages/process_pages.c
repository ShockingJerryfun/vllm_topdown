#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/syscall.h>
#include <unistd.h>

/* Read-only snapshot of resident pages. Device mappings are explicitly excluded;
 * querying ordinary VMAs does not fault pages or migrate them. */
int main(int argc, char **argv) {
    if (argc != 4) return 2;
    long pid = strtol(argv[1], NULL, 10);
    if (pid <= 0) return 2;
    char path[128];
    snprintf(path, sizeof(path), "/proc/%ld/maps", pid);
    FILE *maps = fopen(path, "r");
    snprintf(path, sizeof(path), "/proc/%ld/pagemap", pid);
    int pagemap = open(path, O_RDONLY | O_CLOEXEC);
    FILE *summary = fopen(argv[2], "wx");
    FILE *detail = fopen(argv[3], "wx");
    if (!maps || pagemap < 0 || !summary || !detail) { perror("open"); return 3; }
    fprintf(summary, "begin,end,perms,offset,device,inode,path,pages,present,N0,N1,N2,N3,errors,scope\n");
    fprintf(detail, "address,pfn,node\n");
    size_t page_size = sysconf(_SC_PAGESIZE);
    char line[16384];
    while (fgets(line, sizeof(line), maps)) {
        unsigned long lo, hi, offset, inode;
        char perms[5], device[32];
        int consumed = 0;
        if (sscanf(line, "%lx-%lx %4s %lx %31s %lu %n",
                   &lo, &hi, perms, &offset, device, &inode, &consumed) != 6) return 4;
        char *name = line + consumed;
        while (*name == ' ') name++;
        name[strcspn(name, "\r\n")] = 0;
        /* CSV quotes in paths are not expected in this fixed baseline. */
        if (strchr(name, ',') || strchr(name, '"')) return 5;
        int shared_ram = strncmp(name, "/dev/shm/", 9) == 0 ||
                         strcmp(name, "/dev/zero") == 0 ||
                         strcmp(name, "/dev/zero (deleted)") == 0;
        int special = (strncmp(name, "/dev/", 5) == 0 && !shared_ram) || strcmp(name, "[vdso]") == 0 ||
                      strncmp(name, "[vvar", 5) == 0;
        int inaccessible = perms[0] == '-' && perms[1] == '-' && perms[2] == '-';
        size_t count = (hi - lo) / page_size, present = 0, nodes[4] = {0}, errors = 0;
        if (!special && !inaccessible) {
            for (size_t begin = 0; begin < count; begin += 4096) {
                size_t n = count - begin < 4096 ? count - begin : 4096;
                uint64_t entries[4096], pfns[4096];
                void *addresses[4096];
                int status[4096];
                ssize_t got = pread(pagemap, entries, n * sizeof(uint64_t),
                                    (lo / page_size + begin) * sizeof(uint64_t));
                if (got != (ssize_t)(n * sizeof(uint64_t))) { perror("pagemap read"); return 6; }
                size_t batch = 0;
                for (size_t j = 0; j < n; j++) {
                    if (!(entries[j] & (1ULL << 63))) continue;
                    addresses[batch] = (void *)(lo + (begin + j) * page_size);
                    pfns[batch] = entries[j] & ((1ULL << 55) - 1);
                    status[batch++] = -999;
                }
                if (!batch) continue;
                present += batch;
                long rc = syscall(SYS_move_pages, pid, batch, addresses, NULL, status, 0);
                int saved_errno = errno;
                for (size_t j = 0; j < batch; j++) {
                    int node = rc < 0 ? -saved_errno : status[j];
                    fprintf(detail, "%lx,%lx,%d\n", (unsigned long)addresses[j],
                            (unsigned long)pfns[j], node);
                    if (node >= 0 && node < 4) nodes[node]++;
                    else errors++;
                }
            }
        }
        fprintf(summary, "%lx,%lx,%s,%lx,%s,%lu,%s,%zu,%zu,%zu,%zu,%zu,%zu,%zu,%s\n",
                lo, hi, perms, offset, device, inode, name, count, present,
                nodes[0], nodes[1], nodes[2], nodes[3], errors,
                special ? "special_not_queried" : inaccessible ? "no_access_not_queried" : "ordinary");
    }
    fclose(maps);
    fclose(summary);
    fclose(detail);
    close(pagemap);
    return 0;
}
