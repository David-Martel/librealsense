// Kernel-only microbench for the depth-format-split CUDA helpers on GB10.
// Measures the wall time of:
//   rscuda::split_frame_y8_y8_from_y8i_cuda<y8i_pixel>   (Y8I -> Y8 + Y8, both-IR)
//   rscuda::split_frame_y16_y16_from_y12i_cuda<y12i_pixel> (Y12I -> Y16 + Y16, calib)
// exactly as the y8i_to_y8y8 / y12i_to_y16y16 converters call them (D435, USB,
// non-MIPI path).  NO camera, NO plumbing overhead.
//
// *** LABEL: kernel-only, synthetic, NOT end-to-end ***
// Finding A (color/YUYV) established that end-to-end CUDA conversion is
// plumbing-bound (~2.00 ms/frame CUDA vs ~2.03 ms NEON), NOT malloc-bound.  These
// depth helpers have the SAME per-frame churn shape (3x cudaMalloc + H2D + kernel +
// 2x D2H + 3x cudaFree via alloc_dev RAII), so a cached-buffer variant would shave
// the alloc/free time at the KERNEL level but is pre-registered to be e2e-neutral.
//
// This harness does NOT implement a cache (the library helpers have no compiled-in
// cached path, unlike RS2_GB10_CONV_CACHE in the YUYV helper).  Instead it measures
// the cache CEILING by isolation:
//     baseline      = time of the real helper (alloc + copy + kernel + free)
//     churn         = time of 3x alloc_dev + drop, alone, at the same element count
//     cached_ceiling ~= baseline - churn   (best case a buffer cache could achieve)
//
// Compile (-Werror clean):
//   LRS=/home/damartel/realsense-gb10-validation/wt-dfbench
//   nvcc -DRS2_USE_CUDA -I$LRS/include -I$LRS/src/cuda
//        $LRS/scripts/gb10/bench_depth_format_cuda.cu
//        $LRS/src/cuda/cuda-conversion.cu
//        -O2 -Werror all-warnings -Xcompiler -Wall,-Wextra,-Werror
//        -o /tmp/bench_depth_format_cuda
//   (single line; line breaks above are for readability only)
//
// Run:
//   /tmp/bench_depth_format_cuda

#include <cstdio>
#include <cstdint>
#include <cstdlib>
#include <vector>
#include <chrono>
#include <algorithm>

#include "cuda-conversion.cuh"   // rscuda::split_frame_* wrappers + pixel structs
#include "rscuda_utils.cuh"      // rscuda::alloc_dev (header template)

using clock_type = std::chrono::high_resolution_clock;

struct BenchResult { double mean_us; double min_us; double max_us; };

template <typename Fn>
static BenchResult time_loop(Fn&& fn, int iters, int warmup)
{
    for (int i = 0; i < warmup; ++i)
        fn();
    cudaStreamSynchronize(0);

    std::vector<double> times;
    times.reserve(static_cast<size_t>(iters));
    for (int i = 0; i < iters; ++i)
    {
        auto t0 = clock_type::now();
        fn();
        auto t1 = clock_type::now();
        times.push_back(std::chrono::duration<double, std::micro>(t1 - t0).count());
    }

    double sum = 0.0, mn = times[0], mx = times[0];
    for (double v : times) { sum += v; mn = std::min(mn, v); mx = std::max(mx, v); }
    return { sum / iters, mn, mx };
}

// Fill a Y8I interleaved buffer with a deterministic L/R gradient.
static void make_y8i(rscuda::y8i_pixel* buf, int count)
{
    for (int i = 0; i < count; ++i)
    {
        buf[i].l = static_cast<uint8_t>((i * 7 + 16) & 0xFF);
        buf[i].r = static_cast<uint8_t>((i * 11 + 32) & 0xFF);
    }
}

// Fill a Y12I packed buffer (3-byte bitfield) deterministically.
static void make_y12i(rscuda::y12i_pixel* buf, int count)
{
    for (int i = 0; i < count; ++i)
    {
        int lv = (i * 5 + 11) & 0xFFF;   // 12-bit left
        int rv = (i * 9 + 71) & 0xFFF;   // 12-bit right
        buf[i].ll = static_cast<uint8_t>(lv & 0x0F);
        buf[i].lh = static_cast<uint8_t>((lv >> 4) & 0xFF);
        buf[i].rl = static_cast<uint8_t>(rv & 0xFF);
        buf[i].rh = static_cast<uint8_t>((rv >> 8) & 0x0F);
    }
}

// Isolated allocation churn: 3 device buffers of the same byte sizes the helper
// allocates, then dropped (RAII cudaFree) — the cost a buffer-cache would remove.
static void churn_y8i(int count)
{
    auto s = rscuda::alloc_dev<rscuda::y8i_pixel>(count);
    auto a = rscuda::alloc_dev<uint8_t>(count);
    auto b = rscuda::alloc_dev<uint8_t>(count);
    // touch to prevent the optimizer from eliding the allocations
    if (!s || !a || !b) std::abort();
}

static void churn_y12i(int count)
{
    auto s = rscuda::alloc_dev<rscuda::y12i_pixel>(count);
    auto a = rscuda::alloc_dev<uint16_t>(count);
    auto b = rscuda::alloc_dev<uint16_t>(count);
    if (!s || !a || !b) std::abort();
}

// Correctness sanity: a handful of Y8I output pixels must equal the CPU split.
static bool verify_y8i(const rscuda::y8i_pixel* src,
                       const uint8_t* outL, const uint8_t* outR, int count)
{
    int checks = std::min(count, 4096);
    for (int i = 0; i < checks; ++i)
        if (outL[i] != src[i].l || outR[i] != src[i].r)
        {
            printf("  [Y8I VERIFY FAIL] i=%d got(L=%u,R=%u) want(L=%u,R=%u)\n",
                   i, outL[i], outR[i], src[i].l, src[i].r);
            return false;
        }
    return true;
}

// Correctness sanity: Y12I output = (v<<6 | v>>4) of the 12-bit channel value.
static bool verify_y12i(const rscuda::y12i_pixel* src,
                        const uint16_t* outL, const uint16_t* outR, int count)
{
    int checks = std::min(count, 4096);
    for (int i = 0; i < checks; ++i)
    {
        uint16_t wantL = static_cast<uint16_t>(src[i].l() << 6 | src[i].l() >> 4);
        uint16_t wantR = static_cast<uint16_t>(src[i].r() << 6 | src[i].r() >> 4);
        if (outL[i] != wantL || outR[i] != wantR)
        {
            printf("  [Y12I VERIFY FAIL] i=%d got(L=%u,R=%u) want(L=%u,R=%u)\n",
                   i, outL[i], outR[i], wantL, wantR);
            return false;
        }
    }
    return true;
}

struct Config { int W, H; const char* name; };

int main()
{
    printf("==================================================================\n");
    printf("  bench_depth_format_cuda  [KERNEL-ONLY, SYNTHETIC, NOT E2E]\n");
    printf("  D435 USB path: y8i_to_y8y8 + y12i_to_y16y16 (non-MIPI helpers)\n");
    printf("==================================================================\n\n");

    Config cfgs[] = {
        { 848, 480, " 848x480" },   // typical IR streaming res
        { 1280, 800, "1280x800" },  // max D435 IR / calibration res
    };
    const int WARMUP = 20;
    const int ITERS  = 200;

    for (auto& c : cfgs)
    {
        const int count = c.W * c.H;
        printf("--- %s  (count=%d px) ---\n", c.name, count);
        if (count % RS2_CUDA_THREADS_PER_BLOCK != 0)
            printf("  WARNING: count not a multiple of %d; kernel skips tail pixels "
                   "(pre-existing helper quirk, not fixed here).\n",
                   RS2_CUDA_THREADS_PER_BLOCK);

        // ---------- Y8I -> Y8 + Y8 (both-IR, the hot path) ----------
        {
            std::vector<rscuda::y8i_pixel> src(static_cast<size_t>(count));
            std::vector<uint8_t> outL(static_cast<size_t>(count), 0);
            std::vector<uint8_t> outR(static_cast<size_t>(count), 0);
            make_y8i(src.data(), count);
            uint8_t* dest[2] = { outL.data(), outR.data() };

            rscuda::split_frame_y8_y8_from_y8i_cuda<rscuda::y8i_pixel>(
                dest, count, src.data());
            bool ok = verify_y8i(src.data(), outL.data(), outR.data(), count);

            BenchResult base = time_loop(
                [&]{ rscuda::split_frame_y8_y8_from_y8i_cuda<rscuda::y8i_pixel>(
                         dest, count, src.data()); },
                ITERS, WARMUP);
            BenchResult chn = time_loop([&]{ churn_y8i(count); }, ITERS, WARMUP);

            printf("  Y8I  split  helper : mean=%7.1f us  min=%6.1f  max=%7.1f  %s\n",
                   base.mean_us, base.min_us, base.max_us, ok ? "[verify OK]" : "[VERIFY FAIL]");
            printf("  Y8I  alloc  churn  : mean=%7.1f us  min=%6.1f  max=%7.1f\n",
                   chn.mean_us, chn.min_us, chn.max_us);
            printf("  Y8I  cache ceiling : mean=%7.1f us  (baseline - churn, kernel-only best case)\n",
                   base.mean_us - chn.mean_us);
        }

        // ---------- Y12I -> Y16 + Y16 (calibration) ----------
        {
            std::vector<rscuda::y12i_pixel> src(static_cast<size_t>(count));
            std::vector<uint16_t> outL(static_cast<size_t>(count), 0);
            std::vector<uint16_t> outR(static_cast<size_t>(count), 0);
            make_y12i(src.data(), count);
            uint8_t* dest[2] = { reinterpret_cast<uint8_t*>(outL.data()),
                                 reinterpret_cast<uint8_t*>(outR.data()) };

            rscuda::split_frame_y16_y16_from_y12i_cuda<rscuda::y12i_pixel>(
                dest, count, src.data());
            bool ok = verify_y12i(src.data(), outL.data(), outR.data(), count);

            BenchResult base = time_loop(
                [&]{ rscuda::split_frame_y16_y16_from_y12i_cuda<rscuda::y12i_pixel>(
                         dest, count, src.data()); },
                ITERS, WARMUP);
            BenchResult chn = time_loop([&]{ churn_y12i(count); }, ITERS, WARMUP);

            printf("  Y12I split  helper : mean=%7.1f us  min=%6.1f  max=%7.1f  %s\n",
                   base.mean_us, base.min_us, base.max_us, ok ? "[verify OK]" : "[VERIFY FAIL]");
            printf("  Y12I alloc  churn  : mean=%7.1f us  min=%6.1f  max=%7.1f\n",
                   chn.mean_us, chn.min_us, chn.max_us);
            printf("  Y12I cache ceiling : mean=%7.1f us  (baseline - churn, kernel-only best case)\n",
                   base.mean_us - chn.mean_us);
        }
        printf("\n");
    }

    printf("NOTE: times include 3x cudaMalloc + H2D + kernel + cudaStreamSynchronize\n");
    printf("      + 2x D2H + 3x cudaFree per call (via alloc_dev RAII).\n");
    printf("      'cache ceiling' = the MOST a buffer-cache could remove at the kernel\n");
    printf("      level.  Per Finding A, end-to-end conversion is plumbing-bound, so this\n");
    printf("      kernel-only gain is pre-registered as e2e-NEUTRAL.\n");
    return 0;
}
