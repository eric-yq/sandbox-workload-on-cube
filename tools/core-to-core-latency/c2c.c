/*
 * c2c.c - core-to-core latency matrix via single-cache-line ping-pong.
 *
 *   gcc -O2 -pthread -o c2c c2c.c
 *   ./c2c [iters] [reps] [warmup]  > matrix.csv
 *
 * Two threads pinned to cores A and B bounce one atomic int between them:
 * the initiator stores 1 and spins for 2, the responder spins for 1 and
 * stores 2. That is exactly one cache-line transfer each way, so the
 * measured round trip divided by two is the one-way core-to-core latency.
 *
 * Each pair is timed `reps` times and the MINIMUM is kept: an interrupt or
 * a migration can only ever make a sample slower, so the floor is the
 * cleanest estimator of the hardware path.
 *
 * Output: CSV "a,b,one_way_ns" for every unordered pair a<b.
 */
#define _GNU_SOURCE
#include <pthread.h>
#include <sched.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <time.h>
#include <unistd.h>

/* 4 KiB alignment: comfortably isolates the line on any plausible
 * cache-line size (64 B on both of these CPUs, 128 B on some arm64) so
 * nothing else false-shares with it. */
static _Alignas(4096) atomic_int g_flag;

static int  g_responder_cpu;
static long g_total; /* iterations the responder must serve */

static void pin_self(int cpu) {
    cpu_set_t set;
    CPU_ZERO(&set);
    CPU_SET(cpu, &set);
    if (sched_setaffinity(0, sizeof(set), &set) != 0) {
        fprintf(stderr, "sched_setaffinity(cpu=%d) failed\n", cpu);
        exit(1);
    }
}

static inline uint64_t now_ns(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000000000ull + (uint64_t)ts.tv_nsec;
}

static void *responder(void *arg) {
    (void)arg;
    pin_self(g_responder_cpu);
    for (long i = 0; i < g_total; i++) {
        while (atomic_load_explicit(&g_flag, memory_order_acquire) != 1) {
        }
        atomic_store_explicit(&g_flag, 2, memory_order_release);
    }
    return NULL;
}

static double measure(int a, int b, long iters, int reps, long warmup) {
    pthread_t th;

    g_responder_cpu = b;
    g_total = warmup + (long)reps * iters;
    atomic_store(&g_flag, 0);

    if (pthread_create(&th, NULL, responder, NULL) != 0) {
        fprintf(stderr, "pthread_create failed\n");
        exit(1);
    }
    pin_self(a);

    for (long i = 0; i < warmup; i++) {
        atomic_store_explicit(&g_flag, 1, memory_order_release);
        while (atomic_load_explicit(&g_flag, memory_order_acquire) != 2) {
        }
    }

    double best = 1e18;
    for (int r = 0; r < reps; r++) {
        uint64_t t0 = now_ns();
        for (long i = 0; i < iters; i++) {
            atomic_store_explicit(&g_flag, 1, memory_order_release);
            while (atomic_load_explicit(&g_flag, memory_order_acquire) != 2) {
            }
        }
        uint64_t t1 = now_ns();
        /* round trip = two transfers; report one way */
        double one_way = (double)(t1 - t0) / (double)iters / 2.0;
        if (one_way < best) best = one_way;
    }

    pthread_join(th, NULL);
    return best;
}

int main(int argc, char **argv) {
    long iters  = (argc > 1) ? atol(argv[1]) : 4000;
    int  reps   = (argc > 2) ? atoi(argv[2]) : 3;
    long warmup = (argc > 3) ? atol(argv[3]) : 2000;

    int n = (int)sysconf(_SC_NPROCESSORS_ONLN);
    if (n < 2) {
        fprintf(stderr, "need >= 2 online cpus\n");
        return 1;
    }

    fprintf(stderr, "cpus=%d iters=%ld reps=%d warmup=%ld pairs=%d\n",
            n, iters, reps, warmup, n * (n - 1) / 2);

    printf("a,b,one_way_ns\n");
    long done = 0, total = (long)n * (n - 1) / 2;
    for (int a = 0; a < n - 1; a++) {
        for (int b = a + 1; b < n; b++) {
            double ns = measure(a, b, iters, reps, warmup);
            printf("%d,%d,%.1f\n", a, b, ns);
            if ((++done % 500) == 0) {
                fprintf(stderr, "  %ld/%ld pairs\n", done, total);
            }
        }
        fflush(stdout);
    }
    fprintf(stderr, "done\n");
    return 0;
}
