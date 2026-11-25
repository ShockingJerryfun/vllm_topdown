#define _GNU_SOURCE
#include <fcntl.h>
#include <limits.h>
#include <link.h>
#include <stddef.h>
#include <stdint.h>
#include <sys/mman.h>
#include <sys/syscall.h>

/* Opt-in LD_AUDIT module, built with -nostdlib -fno-builtin. No libc, malloc,
 * stdio, loader calls, symbol binding hooks, threads, or background work.
 * Loader callbacks collect complete maps before changing any VMA flags.
 * Only ordinary file-backed executable VMAs get MADV_NOHUGEPAGE.
 * Existing physical folios are not converted or represented as base pages.
 */
#define EXPORT __attribute__((visibility("default")))
#define RANGE_LIMIT 8192
#define LINE_LIMIT 16384

struct code_range { unsigned long begin, end; };
static struct code_range ranges[RANGE_LIMIT];
static char line_buffer[LINE_LIMIT];
static char read_buffer[8192];
static unsigned int scanning;

static long raw_call(long number, long a, long b, long c, long d) {
#if defined(__aarch64__)
    register long x8 __asm__("x8") = number;
    register long x0 __asm__("x0") = a;
    register long x1 __asm__("x1") = b;
    register long x2 __asm__("x2") = c;
    register long x3 __asm__("x3") = d;
    __asm__ volatile("svc 0" : "+r"(x0) : "r"(x8), "r"(x1), "r"(x2), "r"(x3) : "memory", "cc");
    return x0;
#elif defined(__x86_64__)
    register long r10 __asm__("r10") = d;
    long result;
    __asm__ volatile("syscall" : "=a"(result) : "a"(number), "D"(a), "S"(b), "d"(c), "r"(r10) : "rcx", "r11", "memory", "cc");
    return result;
#else
#error "code_guard supports Linux AArch64 and x86_64 only"
#endif
}

static size_t text_length(const char *value) {
    size_t length = 0;
    while (value[length]) length++;
    return length;
}

__attribute__((noreturn)) static void fail(const char *reason) {
    static const char prefix[] = "CODE_PAGE_GUARD failed: ";
    (void)raw_call(SYS_write, 2, (long)prefix, sizeof(prefix) - 1, 0);
    (void)raw_call(SYS_write, 2, (long)reason, text_length(reason), 0);
    (void)raw_call(SYS_write, 2, (long)"\n", 1, 0);
    (void)raw_call(SYS_exit_group, 126, 0, 0, 0);
    __builtin_trap();
}

static void spaces(const char **cursor) {
    while (**cursor == ' ' || **cursor == '\t') (*cursor)++;
}

static unsigned long number(const char **cursor, unsigned int base) {
    unsigned long result = 0;
    unsigned int count = 0;
    for (;;) {
        unsigned int digit;
        unsigned char c = (unsigned char)**cursor;
        if (c >= '0' && c <= '9') digit = c - '0';
        else if (c >= 'a' && c <= 'f') digit = c - 'a' + 10;
        else break;
        if (digit >= base || result > (ULONG_MAX - digit) / base) fail("maps number overflow");
        result = result * base + digit;
        (*cursor)++;
        count++;
    }
    if (!count) fail("maps number missing");
    return result;
}

static void separator(const char **cursor, char expected) {
    if (**cursor != expected) fail("maps separator malformed");
    (*cursor)++;
}

static void collect_range(size_t *count) {
    const char *cursor = line_buffer;
    unsigned long begin = number(&cursor, 16);
    separator(&cursor, '-');
    unsigned long end = number(&cursor, 16);
    separator(&cursor, ' ');
    spaces(&cursor);
    if (text_length(cursor) < 5 ||
        (cursor[0] != 'r' && cursor[0] != '-') ||
        (cursor[1] != 'w' && cursor[1] != '-') ||
        (cursor[2] != 'x' && cursor[2] != '-') ||
        (cursor[3] != 'p' && cursor[3] != 's')) fail("maps permissions malformed");
    int executable = cursor[2] == 'x';
    cursor += 4;
    separator(&cursor, ' ');
    spaces(&cursor);
    (void)number(&cursor, 16); /* File offset. */
    separator(&cursor, ' ');
    spaces(&cursor);
    (void)number(&cursor, 16);
    separator(&cursor, ':');
    (void)number(&cursor, 16);
    separator(&cursor, ' ');
    spaces(&cursor);
    unsigned long inode = number(&cursor, 10);
    spaces(&cursor);
    if (begin >= end) fail("maps range malformed");
    if (!executable || !inode || cursor[0] != '/') return;
    if (cursor[1] == 'd' && cursor[2] == 'e' && cursor[3] == 'v' && cursor[4] == '/') return;
    if (*count == RANGE_LIMIT) fail("too many executable mappings");
    ranges[*count].begin = begin;
    ranges[*count].end = end;
    (*count)++;
}

static void protect_code(void) {
    /* glibc serializes loader activity. Detect unexpected reentry rather than
     * skipping a required pass; no signal handler or library call is invoked. */
    if (__atomic_exchange_n(&scanning, 1, __ATOMIC_ACQUIRE)) fail("audit callback reentry");
    long fd = raw_call(SYS_openat, AT_FDCWD, (long)"/proc/self/maps", O_RDONLY | O_CLOEXEC, 0);
    if (fd < 0) fail("cannot open /proc/self/maps");
    size_t count = 0, length = 0;
    for (;;) {
        long got = raw_call(SYS_read, fd, (long)read_buffer, sizeof(read_buffer), 0);
        if (got == -4) continue; /* EINTR, without libc errno/TLS. */
        if (got < 0) fail("cannot read /proc/self/maps");
        if (!got) break;
        for (long index = 0; index < got; index++) {
            char c = read_buffer[index];
            if (c == '\n') {
                line_buffer[length] = '\0';
                collect_range(&count);
                length = 0;
            } else {
                if (length + 1 >= LINE_LIMIT) fail("maps line exceeds fixed buffer");
                line_buffer[length++] = c;
            }
        }
    }
    if (raw_call(SYS_close, fd, 0, 0, 0) < 0) fail("cannot close maps descriptor");
    if (length || !count) fail("incomplete or empty executable maps");
    for (size_t index = 0; index < count; index++) {
        long result;
        do {
            result = raw_call(SYS_madvise, ranges[index].begin,
                              ranges[index].end - ranges[index].begin,
                              MADV_NOHUGEPAGE, 0);
        } while (result == -4);
        if (result < 0) fail("MADV_NOHUGEPAGE rejected");
    }
    __atomic_store_n(&scanning, 0, __ATOMIC_RELEASE);
}

__attribute__((constructor)) static void guard_constructor(void) { protect_code(); }

EXPORT unsigned int la_version(unsigned int version) {
    if (version < LAV_CURRENT) fail("unsupported glibc audit interface");
    protect_code();
    return LAV_CURRENT;
}

EXPORT unsigned int la_objopen(struct link_map *map, Lmid_t namespace_id, uintptr_t *cookie) {
    (void)map; (void)namespace_id; (void)cookie;
    return 0; /* No symbol binding or PLT call auditing. */
}

EXPORT void la_activity(uintptr_t *cookie, unsigned int activity) {
    (void)cookie;
    if (activity == LA_ACT_CONSISTENT) protect_code();
}

EXPORT void la_preinit(uintptr_t *cookie) { (void)cookie; protect_code(); }
