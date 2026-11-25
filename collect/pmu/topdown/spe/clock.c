#define _GNU_SOURCE
#include <sched.h>
#include <stdint.h>

uint64_t read_counter(void) {
    uint64_t value;
    __asm__ volatile("isb; mrs %0, cntvct_el0" : "=r"(value));
    return value;
}

uint64_t counter_frequency(void) {
    uint64_t value;
    __asm__ volatile("mrs %0, cntfrq_el0" : "=r"(value));
    return value;
}

int current_cpu(void) { return sched_getcpu(); }
