// License: Apache 2.0. See LICENSE file in root directory.
// Copyright(c) 2026 RealSense, Inc. All Rights Reserved.
//
// H8 feasibility microbench (PROXY): USB-event-thread vs CUDA CPU affinity/pinning on GB10.
//
// QUESTION: on GB10's heterogeneous CPU (Cortex-X925 perf cores + A725 eff cores), does
// pinning the libusb event thread (src/libusb/context-libusb.cpp event loop) to a core
// DISTINCT from the CUDA/align work reduce latency jitter (p99/p99.9) of the USB thread?
//
// HARD LIMIT: no camera. This is a PROXY. We model:
//   "USB" thread  = tight loop doing small CPU work (parse-ish: memmove + checksum over a
//                   small buffer) at a fixed cadence, measuring per-iteration latency. This
//                   stands in for libusb's event-handling callback latency. We measure its
//                   tail; jitter in this thread is what would manifest as frame-arrival jitter.
//   "CUDA" thread = a busy thread doing tight FP/ALU work (stand-in for the CPU-side cost of
//                   CUDA launch + align host marshalling). It contends for CPU/cache.
//
// We do NOT submit real GPU work here: the question is CPU-scheduler contention, not GPU
// throughput. A real on-camera test must confirm, since true libusb latency, IRQ affinity,
// and CUDA driver threads differ from this proxy.
//
// REGIMES compared (each measures the USB thread's iteration-latency distribution):
//   FREE   = no pinning (scheduler free to place both threads anywhere on 20 cores)
//   SPLIT  = USB thread pinned to an A725 eff core, CUDA thread to an X925 perf core (DISTINCT)
//   SAME   = adversarial: BOTH threads pinned to the SAME X925 core (contention ceiling)
//   USB_X  = USB thread on a dedicated X925 perf core, CUDA thread on a different X925 core
//
// GB10 topology (probed): X925 perf cores = {5..9, 15..19} @3.9GHz; A725 eff = {0..4,10..14} @2.8GHz.
//
// Build: g++ -O2 -std=c++14 -Wall -Wextra -Werror -pthread bench_h7h8_affinity.cpp -o bench_h7h8_affinity
// Usage: bench_h7h8_affinity [seconds] [usb_cadence_us]

#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif
#include <sched.h>
#include <pthread.h>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <cstdint>
#include <vector>
#include <algorithm>
#include <atomic>
#include <thread>
#include <chrono>
#include <cmath>
#include <ctime>

// All timing in CLOCK_MONOTONIC timespec domain so sleep-target and wake-read agree.
static inline void mono_now(struct timespec* ts) { clock_gettime(CLOCK_MONOTONIC, ts); }
static inline double ts_diff_us(const struct timespec& a, const struct timespec& b) {
    // a - b in microseconds
    return (a.tv_sec - b.tv_sec) * 1e6 + (a.tv_nsec - b.tv_nsec) / 1e3;
}
static inline void ts_add_us(struct timespec* t, long us) {
    t->tv_nsec += us * 1000L;
    while (t->tv_nsec >= 1000000000L) { t->tv_nsec -= 1000000000L; t->tv_sec += 1; }
}

static const int X925_CORES[] = {5,6,7,8,9,15,16,17,18,19};
static const int A725_CORES[] = {0,1,2,3,4,10,11,12,13,14};

static bool pin_to(std::thread& t, int core) {
    if (core < 0) return true; // no pin
    cpu_set_t set; CPU_ZERO(&set); CPU_SET(core, &set);
    int rc = pthread_setaffinity_np(t.native_handle(), sizeof(set), &set);
    return rc == 0;
}

static double pct(std::vector<double>& v, double p) {
    if (v.empty()) return 0.0;
    std::sort(v.begin(), v.end());
    size_t i = (size_t)(p / 100.0 * (v.size() - 1) + 0.5);
    return v[std::min(i, v.size() - 1)];
}

// "CUDA-host" contender: tight FP work; runs until stop flag.
static void cuda_contender(std::atomic<bool>* stop) {
    volatile double acc = 1.0;
    while (!stop->load(std::memory_order_relaxed)) {
        for (int i = 0; i < 4096; ++i) {
            acc = acc * 1.0000001 + 0.5;
            acc = acc / 1.0000002 - 0.25;
            if (acc > 1e18) acc = 1.0;
        }
    }
    if (acc == 42.0) putchar('\0'); // keep acc live (prevent dead-code elimination)
}

// "USB-event" thread: models libusb's poll()-driven event loop. It SLEEPS to the next
// cadence target via clock_nanosleep (ABSTIME) — exactly as a blocking poll() would yield
// the CPU — then measures WAKEUP OVERRUN = (actual wake time - target time). That overrun is
// the scheduler-induced jitter: if a CPU hog occupies the only available core, the wakeup
// waits for it to be preempted, inflating the tail. Measuring overrun (not work time) is the
// faithful proxy: contention lands in the wait window, which work-time timing cannot see.
struct UsbResult { std::vector<double> lat_us; };

static void usb_thread(std::atomic<bool>* stop, int cadence_us, UsbResult* out) {
    const int BUF = 1024;
    std::vector<uint8_t> a(BUF), b(BUF);
    for (int i = 0; i < BUF; ++i) a[i] = (uint8_t)(i * 7 + 3);
    out->lat_us.reserve(2000000);

    struct timespec target; mono_now(&target);
    ts_add_us(&target, cadence_us);
    while (!stop->load(std::memory_order_relaxed)) {
        clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &target, nullptr);
        struct timespec woke; mono_now(&woke);
        double overrun = ts_diff_us(woke, target);    // how late did we actually resume
        if (overrun < 0) overrun = 0;                 // woke early (rare) -> 0 jitter
        // small "parse" payload (libusb callback work) so the thread isn't a pure sleeper
        memmove(b.data(), a.data(), BUF);
        uint32_t sum = 0;
        for (int i = 0; i < BUF; ++i) sum += b[i] * (i & 31);
        if (sum == 0xFFFFFFFFu) a[0]++; // prevent optimize-out
        out->lat_us.push_back(overrun);
        ts_add_us(&target, cadence_us);
        // if we fell badly behind, resync target to now+cadence so we don't spiral
        struct timespec nowts; mono_now(&nowts);
        if (ts_diff_us(target, nowts) < -((double)cadence_us)) {
            target = nowts; ts_add_us(&target, cadence_us);
        }
    }
}

struct Stats { double p50, p95, p99, p999, maxv; size_t n; };
static Stats summarize(std::vector<double> v) {
    Stats s{};
    s.n = v.size();
    s.p50 = pct(v, 50); s.p95 = pct(v, 95); s.p99 = pct(v, 99);
    s.p999 = pct(v, 99.9);
    s.maxv = v.empty() ? 0 : *std::max_element(v.begin(), v.end());
    return s;
}

static Stats run_regime(const char* name, int usb_core, int cuda_core, int seconds, int cadence_us) {
    std::atomic<bool> stop{false};
    UsbResult res;

    std::thread cuda(cuda_contender, &stop);
    std::thread usb(usb_thread, &stop, cadence_us, &res);

    bool p1 = pin_to(usb, usb_core);
    bool p2 = pin_to(cuda, cuda_core);

    std::this_thread::sleep_for(std::chrono::seconds(seconds));
    stop.store(true);
    usb.join(); cuda.join();

    Stats s = summarize(res.lat_us);
    printf("  %-7s usb_core=%-3d cuda_core=%-3d  pin_ok=%d/%d  n=%zu\n",
        name, usb_core, cuda_core, (int)p1, (int)p2, s.n);
    printf("          p50=%.2f  p95=%.2f  p99=%.2f  p99.9=%.2f  max=%.2f us\n",
        s.p50, s.p95, s.p99, s.p999, s.maxv);
    return s;
}

int main(int argc, char** argv) {
    int seconds = (argc > 1) ? atoi(argv[1]) : 8;
    int cadence_us = (argc > 2) ? atoi(argv[2]) : 200; // ~5kHz event cadence proxy

    unsigned ncpu = std::thread::hardware_concurrency();
    printf("== H8: USB-event vs CUDA CPU affinity jitter microbench (PROXY, no camera) ==\n");
    printf("cores=%u  seconds/regime=%d  usb_cadence=%dus\n", ncpu, seconds, cadence_us);
    printf("X925 perf cores: 5-9,15-19 (3.9GHz) | A725 eff cores: 0-4,10-14 (2.8GHz)\n");
    printf("Measuring USB-thread WAKEUP OVERRUN tail (clock_nanosleep ABSTIME; p99/p99.9 = jitter)\n\n");

    int a725 = A725_CORES[0];     // 0
    int x925a = X925_CORES[0];    // 5
    int x925b = X925_CORES[1];    // 6

    Stats free_s = run_regime("FREE", -1, -1, seconds, cadence_us);
    Stats split  = run_regime("SPLIT", a725, x925a, seconds, cadence_us);
    Stats usb_x  = run_regime("USB_X", x925a, x925b, seconds, cadence_us);
    Stats same   = run_regime("SAME", x925a, x925a, seconds, cadence_us);

    printf("\n== Tail-jitter comparison (lower p99.9 = less jitter) ==\n");
    printf("  regime   p99(us)  p99.9(us)   vs FREE p99.9\n");
    auto row = [&](const char* nm, Stats& s) {
        double d = free_s.p999 > 0 ? (s.p999 - free_s.p999) / free_s.p999 * 100.0 : 0.0;
        printf("  %-7s  %7.2f  %9.2f   %+.1f%%\n", nm, s.p99, s.p999, d);
    };
    row("FREE", free_s); row("SPLIT", split); row("USB_X", usb_x); row("SAME", same);

    double best_split = std::min(split.p999, usb_x.p999);
    double improve = free_s.p999 > 0 ? (free_s.p999 - best_split) / free_s.p999 * 100.0 : 0.0;
    printf("\n  Best distinct-core pinning vs FREE: p99.9 %+.1f%% (%s)\n",
        -improve, improve > 0 ? "pinning helps" : "pinning does not help");
    printf("  Contention ceiling (SAME core) vs FREE: p99.9 %+.1f%%\n",
        free_s.p999 > 0 ? (same.p999 - free_s.p999) / free_s.p999 * 100.0 : 0.0);
    printf("\nCAVEAT: PROXY only. Real libusb event latency, IRQ/MSI affinity, and CUDA driver\n");
    printf("        threads differ. A GO here means 'promising, needs on-camera confirmation',\n");
    printf("        never 'proven'. Run with the heavy core build IDLE or the tail is contaminated.\n");
    return 0;
}
