// Kernel-only microbench for RS2_GB10_CONV_CACHE cached-buffer path.
// Measures the wall time of rscuda::unpack_yuy2_cuda_helper() in isolation
// (H2D copy + kernel + sync + D2H copy, NO camera, NO plumbing overhead).
//
// *** LABEL: kernel-only, synthetic, NOT end-to-end ***
// Finding A established that the end-to-end CPU time is ~2 ms/frame for CUDA color
// conversion (vs ~2.03 ms NEON) — plumbing-bound, NOT malloc-bound.  This bench
// measures the isolated kernel path; it will show faster for mode 1 (no per-call
// cudaMalloc/cudaFree), but that speedup does NOT propagate to the end-to-end number.
//
// Compile:
//   LRS=/home/damartel/dev/repos/librealsense
//   nvcc -DRS2_USE_CUDA -DRS2_GB10_CONV_CACHE \
//        -I$LRS/include -I$LRS/src/cuda \
//        $LRS/scripts/gb10/bench_conv_cache_kernel.cu \
//        $LRS/src/cuda/cuda-conversion.cu \
//        -O2 -o /tmp/bench_conv_cache_kernel
//
// Run:
//   RS2_CONV_MODE=0 /tmp/bench_conv_cache_kernel   # baseline
//   RS2_CONV_MODE=1 /tmp/bench_conv_cache_kernel   # cached

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <chrono>
#include <vector>

#include "cuda-conversion.cuh"
#ifndef RS2_FORMAT_BGR8
#include "../../include/librealsense2/rs.h"
#endif

static void make_yuyv_gradient(uint8_t* buf, int W, int H) {
    int superpix = (W * H) / 2;
    for (int i = 0; i < superpix; i++) {
        buf[4*i+0] = (uint8_t)((i * 7 + 16)  & 0xFF);
        buf[4*i+1] = (uint8_t)((i * 3 + 80)  & 0xFF);
        buf[4*i+2] = (uint8_t)((i * 11 + 32) & 0xFF);
        buf[4*i+3] = (uint8_t)((i * 5 + 120) & 0xFF);
    }
}

struct BenchResult { double mean_us; double min_us; double max_us; };

static BenchResult bench(const uint8_t* src, uint8_t* dst, int n, rs2_format fmt, int iters, int warmup) {
    // warmup
    for (int i = 0; i < warmup; i++)
        rscuda::unpack_yuy2_cuda_helper(src, dst, n, fmt);

    std::vector<double> times;
    times.reserve(iters);
    for (int i = 0; i < iters; i++) {
        auto t0 = std::chrono::high_resolution_clock::now();
        rscuda::unpack_yuy2_cuda_helper(src, dst, n, fmt);
        auto t1 = std::chrono::high_resolution_clock::now();
        times.push_back(std::chrono::duration<double, std::micro>(t1 - t0).count());
    }

    double sum = 0, mn = times[0], mx = times[0];
    for (double v : times) { sum += v; mn = std::min(mn, v); mx = std::max(mx, v); }
    return {sum / iters, mn, mx};
}

int main() {
    const char* mode_env = getenv("RS2_CONV_MODE");
    int mode = mode_env ? atoi(mode_env) : 0;

    printf("=============================================================\n");
    printf("  bench_conv_cache_kernel  [KERNEL-ONLY, SYNTHETIC, NOT E2E]\n");
    printf("  RS2_GB10_CONV_CACHE=%s  RS2_CONV_MODE=%d\n",
#if defined(RS2_GB10_CONV_CACHE)
           "COMPILED",
#else
           "NOT_COMPILED",
#endif
           mode);
    printf("=============================================================\n\n");

    struct Config { int W, H; rs2_format fmt; const char* name; };
    Config cfgs[] = {
        {1280, 720, RS2_FORMAT_BGR8,  "1280x720 BGR8"},
        {1280, 720, RS2_FORMAT_RGB8,  "1280x720 RGB8"},
        { 848, 480, RS2_FORMAT_BGR8,  " 848x480 BGR8"},
        { 848, 480, RS2_FORMAT_RGB8,  " 848x480 RGB8"},
    };
    const int WARMUP = 20;
    const int ITERS  = 200;

    for (auto& c : cfgs) {
        int n = c.W * c.H;
        size_t src_bytes = (size_t)(n / 2) * 4;
        int bpp = (c.fmt == RS2_FORMAT_BGR8 || c.fmt == RS2_FORMAT_RGB8) ? 3 : 4;
        size_t dst_bytes = (size_t)n * bpp;

        std::vector<uint8_t> src(src_bytes), dst(dst_bytes, 0);
        make_yuyv_gradient(src.data(), c.W, c.H);

        auto r = bench(src.data(), dst.data(), n, c.fmt, ITERS, WARMUP);
        printf("  %-20s  mean=%7.1f us  min=%6.1f us  max=%7.1f us  (n=%d, warmup=%d)\n",
               c.name, r.mean_us, r.min_us, r.max_us, ITERS, WARMUP);
    }
    printf("\nNOTE: These times include H2D copy + kernel + cudaStreamSynchronize + D2H copy.\n");
    printf("      mode=0 has additional cudaMalloc+cudaFree per call.\n");
    printf("      mode=1 eliminates those allocs; kernels are byte-identical.\n");
    printf("      End-to-end pipeline latency is plumbing-bound (Finding A: ~2.00 ms/frame\n");
    printf("      CUDA vs ~2.03 ms NEON) — kernel-only gains do NOT move that needle.\n");
    return 0;
}
