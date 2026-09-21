/* Scan SPE packets once; materialize only records in formal target windows. */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

enum kind { PAD, END, ALIGNMENT, ADDRESS, COUNTER, TIMESTAMP,
            EVENT, SOURCE, CONTEXT, OPERATION, RECORDS };
enum exclusion { MISSING_TIMESTAMP, OUTSIDE_WINDOW, MISSING_CONTEXT,
                 OTHER_THREAD, WARMUP };
struct window { uint64_t start, end, formal; };
struct span { uint64_t start, end, ordinal, terminal; };

static uint64_t read_le(const unsigned char *p, unsigned width) {
    uint64_t value = 0;
    for (unsigned i = 0; i < width; ++i)
        value |= (uint64_t)p[i] << (8 * i);
    return value;
}

static int selection(uint64_t ticks, int has_ticks, uint64_t tid, int has_tid,
                     const struct window *windows, uint64_t count,
                     uint64_t target) {
    if (!has_ticks) return MISSING_TIMESTAMP;
    uint64_t low = 0, high = count;
    while (low < high) {
        uint64_t middle = low + (high - low) / 2;
        if (windows[middle].start <= ticks) low = middle + 1;
        else high = middle;
    }
    if (!low || ticks >= windows[low - 1].end) return OUTSIDE_WINDOW;
    if (!has_tid) return MISSING_CONTEXT;
    if (tid != target) return OTHER_THREAD;
    return windows[low - 1].formal ? -1 : WARMUP;
}

int spe_scan(const unsigned char *data, uint64_t size,
             const struct window *windows, uint64_t window_count,
             uint64_t target_tid, struct span **selected,
             uint64_t *selected_count, uint64_t *counts, uint64_t *excluded,
             char *error, uint64_t error_size) {
    uint64_t pos = 0, start = 0, ordinal = 0, capacity = 0;
    uint64_t addresses = 0, counters = 0, fields = 0, ticks = 0, tid = 0;
    int active = 0, has_ticks = 0, has_tid = 0;
    const char *failure = NULL;
    const char *names[] = {"pad", "end", "alignment", "address", "counter",
                           "timestamp", "event", "source", "context", "operation"};
    char detail[80];
    *selected = NULL;
    *selected_count = 0;
    while (pos < size) {
        unsigned header = data[pos], actual = header, index = 0, width = 0;
        unsigned extended = (header & 0xfc) == 0x20;
        uint64_t length = 1;
        enum kind kind;
        if (header == 0 || header == 1) {
            kind = header == 0 ? PAD : END;
        } else {
            if (extended) {
                if (size - pos < 2) {
                    failure = "Truncated extended SPE packet header"; goto fail;
                }
                actual = data[pos + 1];
            }
            if (extended && actual == 0) {
                uint64_t alignment = UINT64_C(1) << ((header & 15) + 1);
                kind = ALIGNMENT;
                length = alignment - pos % alignment;
                if (length > size - pos) {
                    failure = "Truncated SPE alignment packet"; goto fail;
                }
            } else {
                width = 1U << ((actual >> 4) & 3);
                length = 1 + extended + width;
                if (length > size - pos) {
                    failure = "Truncated SPE packet"; goto fail;
                }
                index = extended ? ((header & 3) << 3) | (actual & 7) : actual & 7;
                if ((actual & 0xf8) == 0xb0) kind = ADDRESS;
                else if ((actual & 0xf8) == 0x98) kind = COUNTER;
                else if (header == 0x71) kind = TIMESTAMP;
                else if ((header & 0xcf) == 0x42) kind = EVENT;
                else if ((header & 0xcf) == 0x43) kind = SOURCE;
                else if ((header & 0xfc) == 0x64) kind = CONTEXT;
                else if ((header & 0xfc) == 0x48) kind = OPERATION;
                else { failure = "Unknown SPE header"; goto fail; }
            }
        }
        counts[kind]++;
        if (kind == ADDRESS && index == 0) {
            if (active) { failure = "Unterminated SPE record"; goto fail; }
            active = 1;
            start = pos;
            ordinal++;
            addresses = counters = fields = 0;
            has_ticks = has_tid = 0;
        } else if (!active && kind != PAD && kind != ALIGNMENT) {
            snprintf(detail, sizeof(detail), "Orphan SPE %s packet", names[kind]);
            failure = detail; goto fail;
        }
        if (active) {
            if (kind != PAD && kind != ALIGNMENT && kind != END) {
                uint64_t *seen = kind == ADDRESS ? &addresses :
                                 kind == COUNTER ? &counters : &fields;
                uint64_t bit = UINT64_C(1) <<
                    ((kind == ADDRESS || kind == COUNTER) ? index : (unsigned)kind);
                if (*seen & bit) { failure = "Duplicate SPE field"; goto fail; }
                *seen |= bit;
            }
            if (kind == CONTEXT) {
                tid = read_le(data + pos + 1 + extended, width);
                has_tid = 1;
            } else if (kind == TIMESTAMP) {
                ticks = read_le(data + pos + 1 + extended, width);
                has_ticks = 1;
            }
            if (kind == TIMESTAMP || kind == END) {
                int reason = selection(ticks, has_ticks, tid, has_tid,
                                       windows, window_count, target_tid);
                counts[RECORDS]++;
                if (reason >= 0) excluded[reason]++;
                else {
                    if (*selected_count == capacity) {
                        uint64_t next = capacity ? capacity * 2 : 256;
                        if (next < capacity || next > SIZE_MAX / sizeof(struct span)) {
                            failure = "SPE selected span capacity overflow"; goto fail;
                        }
                        void *replacement = realloc(*selected, next * sizeof(struct span));
                        if (!replacement) { failure = "SPE span allocation failed"; goto fail; }
                        *selected = replacement;
                        capacity = next;
                    }
                    (*selected)[(*selected_count)++] =
                        (struct span){start, pos + length, ordinal, pos};
                }
                active = 0;
            }
        }
        pos += length;
    }
    if (active) { failure = "Unterminated final SPE record"; goto fail; }
    return 0;
fail:
    snprintf(error, (size_t)error_size, "%s at AUX offset %llu",
             failure, (unsigned long long)pos);
    free(*selected);
    *selected = NULL;
    *selected_count = 0;
    return 1;
}

void spe_scan_free(void *pointer) { free(pointer); }
