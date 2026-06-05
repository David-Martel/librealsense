// P4 async-pipelining microbench for the GB10 CUDA cached path  [NO CAMERA, SYNTHETIC].
//
// Question (#31): on top of the already-landed cached buffer pools (Finding A:
// pointcloud 3.3x, conversion ~NEON-parity, byte-identical, default mode 1), does adding
// double-buffering + multi-CUDA-stream overlap of H2D / kernel / D2H buy any throughput or
// latency on GB10's coherent/unified memory?  Finding A established that host<->device copies
// are CHEAP here and the alloc churn is already gone, so this is pre-registered as LIKELY
// MARGINAL.  This bench MEASURES it, honestly, with B *capable* of real overlap (pinned host
// memory + cudaMemcpyAsync on non-default streams) so a NO-GO actually means something.
//
// Three variants, SAME kernels, SAME total work, ONLY the scheduling/host-memory differs:
//   REF : the shipped cached path itself — rscuda::deproject_depth_cuda / unpack_yuy2_cuda_helper
//         with mode 1 (pageable host buffers + sync per frame).  Fidelity anchor.
//   A   : pinned host buffers, single stream, H2D -> kernel -> D2H, sync per frame (serialized).
//   B   : pinned host buffers, K-deep round-robin over multiple streams, cudaMemcpyAsync, so
//         frame N's D2H / frame N+1's H2D overlap frame N's / N+1's compute.  One sync at the
//         end of the batch for throughput; per-frame CUDA events for latency.
//
// Attribution that falls out of this:
//   pinning gain  = REF - A      (cheap-copy hypothesis: ~0 on GB10)
//   overlap gain  = A   - B      (the actual P4 question)
//   total P4 gain = REF - B
// Plus an ISOLATED per-stage table (H2D / kernel / D2H timed alone) so the verdict rests on
// physics (copy vs kernel ratio), not just on A-vs-B deltas that may be inside run-to-run noise.
//
// We launch the IDENTICAL library kernels (external linkage) by forward-declaration and link
// cuda-pointcloud.cu + cuda-conversion.cu — the shipped wrappers hard-code the default stream and
// an internal sync, so you cannot pipeline *through* them; you drive the same kernel yourself.
//
// Build (-O3, -Werror clean) and run: see async-pipeline-bench.sh  (`just bench-async`).
//   nvcc -DRS2_USE_CUDA -DRS2_GB10_PC_ZEROCOPY -DRS2_GB10_CONV_CACHE -O3
//        -I<repo>/include -I<repo>/src -I<repo>/src/cuda
//        bench_async_pipeline.cu cuda-pointcloud.cu cuda-conversion.cu
//        -Werror all-warnings -Xcompiler -Wall,-Wextra,-Werror -o /tmp/bench_async_pipeline

#include <cstdio>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <vector>
#include <string>
#include <algorithm>
#include <cmath>

#include <cuda_runtime.h>

#include "cuda-pointcloud.cuh"   // rscuda::deproject_depth_cuda + RS2_CUDA_THREADS_PER_BLOCK
#include "cuda-conversion.cuh"   // rscuda::unpack_yuy2_cuda_helper
#include "../../include/librealsense2/rs.h"

// ---- the IDENTICAL library kernels (file-scope / external linkage in the .cu we link) ----
__global__ void kernel_deproject_depth_cuda(float* points, const rs2_intrinsics* intrin,
                                            const uint16_t* depth, float depth_scale);
__global__ void kernel_unpack_yuy2_bgr8_cuda(const uint8_t* src, uint8_t* dst, int superPixCount);

// ---- tiny CUDA error guard (bench-local; mirrors the library's throw-on-failure intent) ----
#define CK(call) do {                                                              \
    cudaError_t _e = (call);                                                        \
    if (_e != cudaSuccess) {                                                        \
        std::fprintf(stderr, "CUDA error %s:%d : %s -> %s\n", __FILE__, __LINE__,    \
                     #call, cudaGetErrorString(_e));                                 \
        std::abort();                                                               \
    }                                                                               \
} while (0)

// ---- per-stage timings (microseconds) for overlap-efficiency accounting ----
struct StageTimes { double h2d, ker, d2h, sum, mx; };
// Overlap efficiency: how much of the hideable copy time variant B actually hid behind compute.
//   sum    = h2d + ker + d2h          (fully serialized, what A pays per frame)
//   mx     = max(ker, h2d + d2h)      (perfect overlap floor: copy fully behind compute or vice-versa)
//   periodB= 1e6 / fps_B (us/frame)   (what B actually achieves in steady state)
//   eff    = (sum - periodB) / (sum - mx)   -> 1.0 = perfect overlap, 0.0 = none
static double overlap_eff(const StageTimes& s, double fps_b) {
    double periodB = 1e6 / fps_b;
    double denom = s.sum - s.mx;
    if (denom <= 1e-9) return 0.0;
    return (s.sum - periodB) / denom;
}

// ---- stats over a vector of per-frame latencies (microseconds) ----
struct Stats { double mean, p50, p95, mn, mx; };
static Stats summarize(std::vector<double> v) {
    Stats s{0, 0, 0, 0, 0};
    if (v.empty()) return s;
    std::sort(v.begin(), v.end());
    double sum = 0.0;
    for (double x : v) sum += x;
    s.mean = sum / static_cast<double>(v.size());
    auto pick = [&](double q) {
        double idx = q * static_cast<double>(v.size() - 1);
        size_t lo = static_cast<size_t>(std::floor(idx));
        size_t hi = static_cast<size_t>(std::ceil(idx));
        double frac = idx - static_cast<double>(lo);
        return v[lo] + (v[hi] - v[lo]) * frac;
    };
    s.p50 = pick(0.50);
    s.p95 = pick(0.95);
    s.mn  = v.front();
    s.mx  = v.back();
    return s;
}

// ---- deterministic synthetic inputs (match the cached-pool test's generators) ----
static void make_depth(uint16_t* d, int W, int H) {
    float cx = W * 0.5f, cy = H * 0.5f;
    for (int y = 0; y < H; ++y)
        for (int x = 0; x < W; ++x) {
            float dx = x - cx, dy = y - cy, r = dx * dx + dy * dy;
            uint16_t z = static_cast<uint16_t>(500 + (static_cast<int>(r * 0.01f) % 4000));
            if ((x + y) % 9 == 0) z = 0;
            d[static_cast<size_t>(y) * W + x] = z;
        }
}
static void make_yuyv(uint8_t* b, int W, int H) {
    int sp = W * H / 2;
    for (int i = 0; i < sp; ++i) {
        b[4 * i + 0] = static_cast<uint8_t>(i * 3);
        b[4 * i + 1] = static_cast<uint8_t>(i * 5 + 17);
        b[4 * i + 2] = static_cast<uint8_t>(i * 7);
        b[4 * i + 3] = static_cast<uint8_t>(i * 11 + 29);
    }
}

struct Res { int W, H; const char* name; };

static const int   WARMUP = 30;
static const int   ITERS  = 400;   // steady-state frames timed per variant
static const int   KDEPTH = 4;     // pipeline depth / number of streams for variant B

// ============================== POINTCLOUD WORKLOAD ==============================
// Per-frame device work mirrors deproject_depth_cuda's cached path exactly:
//   H2D depth (count*u16) + H2D intrin (sizeof) ; kernel<<<count/TPB>>> ; D2H points (count*3*f32)
namespace pc {

struct DevBuf { float* d_points = nullptr; uint16_t* d_depth = nullptr; rs2_intrinsics* d_intrin = nullptr; };

static void alloc_dev(DevBuf& b, int count) {
    CK(cudaMalloc(&b.d_points, static_cast<size_t>(count) * 3 * sizeof(float)));
    CK(cudaMalloc(&b.d_depth,  static_cast<size_t>(count) * sizeof(uint16_t)));
    CK(cudaMalloc(&b.d_intrin, sizeof(rs2_intrinsics)));
}
static void free_dev(DevBuf& b) {
    if (b.d_points) CK(cudaFree(b.d_points));
    if (b.d_depth)  CK(cudaFree(b.d_depth));
    if (b.d_intrin) CK(cudaFree(b.d_intrin));
    b = DevBuf{};
}

// Per-stage isolation: time H2D, kernel, D2H separately (events) — copy-vs-compute ratio.
static StageTimes stage_table(const Res& r, const rs2_intrinsics& in, const uint16_t* h_depth_pinned,
                        float* h_points_pinned) {
    const int count = r.W * r.H;
    const int nb = count / RS2_CUDA_THREADS_PER_BLOCK;
    DevBuf b; alloc_dev(b, count);
    cudaStream_t s; CK(cudaStreamCreate(&s));
    cudaEvent_t e0, e1; CK(cudaEventCreate(&e0)); CK(cudaEventCreate(&e1));

    auto time_ms = [&](auto&& body) {
        for (int i = 0; i < WARMUP; ++i) body();
        CK(cudaStreamSynchronize(s));
        std::vector<double> t; t.reserve(ITERS);
        for (int i = 0; i < ITERS; ++i) {
            CK(cudaEventRecord(e0, s));
            body();
            CK(cudaEventRecord(e1, s));
            CK(cudaEventSynchronize(e1));
            float ms = 0; CK(cudaEventElapsedTime(&ms, e0, e1));
            t.push_back(ms * 1000.0);  // -> microseconds
        }
        return summarize(t).mean;
    };

    double h2d = time_ms([&]{
        CK(cudaMemcpyAsync(b.d_depth, h_depth_pinned, static_cast<size_t>(count) * sizeof(uint16_t),
                           cudaMemcpyHostToDevice, s));
        CK(cudaMemcpyAsync(b.d_intrin, &in, sizeof(rs2_intrinsics), cudaMemcpyHostToDevice, s));
    });
    // prime device depth so the kernel reads valid data
    CK(cudaMemcpy(b.d_depth, h_depth_pinned, static_cast<size_t>(count) * sizeof(uint16_t), cudaMemcpyHostToDevice));
    CK(cudaMemcpy(b.d_intrin, &in, sizeof(rs2_intrinsics), cudaMemcpyHostToDevice));
    double ker = time_ms([&]{
        kernel_deproject_depth_cuda<<<nb, RS2_CUDA_THREADS_PER_BLOCK, 0, s>>>(b.d_points, b.d_intrin, b.d_depth, 0.001f);
    });
    double d2h = time_ms([&]{
        CK(cudaMemcpyAsync(h_points_pinned, b.d_points, static_cast<size_t>(count) * 3 * sizeof(float),
                           cudaMemcpyDeviceToHost, s));
    });

    StageTimes st_t{ h2d, ker, d2h, h2d + ker + d2h, std::max(ker, h2d + d2h) };
    printf("  [stage]  H2D=%7.1f us   kernel=%7.1f us   D2H=%7.1f us   "
           "(sum=%7.1f, overlap floor=max=%7.1f) us\n",
           h2d, ker, d2h, st_t.sum, st_t.mx);

    CK(cudaEventDestroy(e0)); CK(cudaEventDestroy(e1));
    CK(cudaStreamDestroy(s)); free_dev(b);
    return st_t;
}

// Variant A: pinned host, single stream, sync per frame (serialized H2D->kernel->D2H).
static Stats variant_A(const Res& r, const rs2_intrinsics& in, const uint16_t* h_depth,
                       float* h_points, double& fps) {
    const int count = r.W * r.H;
    const int nb = count / RS2_CUDA_THREADS_PER_BLOCK;
    DevBuf b; alloc_dev(b, count);
    cudaStream_t s; CK(cudaStreamCreate(&s));
    cudaEvent_t e0, e1; CK(cudaEventCreate(&e0)); CK(cudaEventCreate(&e1));

    auto frame = [&]{
        CK(cudaMemcpyAsync(b.d_depth, h_depth, static_cast<size_t>(count) * sizeof(uint16_t), cudaMemcpyHostToDevice, s));
        CK(cudaMemcpyAsync(b.d_intrin, &in, sizeof(rs2_intrinsics), cudaMemcpyHostToDevice, s));
        kernel_deproject_depth_cuda<<<nb, RS2_CUDA_THREADS_PER_BLOCK, 0, s>>>(b.d_points, b.d_intrin, b.d_depth, 0.001f);
        CK(cudaMemcpyAsync(h_points, b.d_points, static_cast<size_t>(count) * 3 * sizeof(float), cudaMemcpyDeviceToHost, s));
        CK(cudaStreamSynchronize(s));
    };
    for (int i = 0; i < WARMUP; ++i) frame();

    std::vector<double> lat; lat.reserve(ITERS);
    cudaEvent_t t0, t1; CK(cudaEventCreate(&t0)); CK(cudaEventCreate(&t1));
    CK(cudaEventRecord(t0));
    for (int i = 0; i < ITERS; ++i) {
        CK(cudaEventRecord(e0, s));
        frame();
        CK(cudaEventRecord(e1, s));
        CK(cudaEventSynchronize(e1));
        float ms = 0; CK(cudaEventElapsedTime(&ms, e0, e1));
        lat.push_back(ms * 1000.0);
    }
    CK(cudaEventRecord(t1)); CK(cudaEventSynchronize(t1));
    float wall_ms = 0; CK(cudaEventElapsedTime(&wall_ms, t0, t1));
    fps = ITERS / (wall_ms / 1000.0);

    CK(cudaEventDestroy(t0)); CK(cudaEventDestroy(t1));
    CK(cudaEventDestroy(e0)); CK(cudaEventDestroy(e1));
    CK(cudaStreamDestroy(s)); free_dev(b);
    return summarize(lat);
}

// Variant B: pinned host, K streams round-robin, async copies, single end-of-batch sync.
// Per-frame latency = event pair straddling that frame's H2D..D2H on its own stream.
static Stats variant_B(const Res& r, const rs2_intrinsics& in, const uint16_t* h_depth,
                       float* h_points, double& fps) {
    const int count = r.W * r.H;
    const int nb = count / RS2_CUDA_THREADS_PER_BLOCK;
    std::vector<DevBuf> b(KDEPTH);
    std::vector<cudaStream_t> st(KDEPTH);
    for (int k = 0; k < KDEPTH; ++k) { alloc_dev(b[k], count); CK(cudaStreamCreate(&st[k])); }

    std::vector<cudaEvent_t> ev0(ITERS), ev1(ITERS);
    for (int i = 0; i < ITERS; ++i) { CK(cudaEventCreate(&ev0[i])); CK(cudaEventCreate(&ev1[i])); }

    auto enqueue = [&](int i, bool record) {
        int k = i % KDEPTH;
        cudaStream_t s = st[k];
        if (record) CK(cudaEventRecord(ev0[i], s));
        CK(cudaMemcpyAsync(b[k].d_depth, h_depth, static_cast<size_t>(count) * sizeof(uint16_t), cudaMemcpyHostToDevice, s));
        CK(cudaMemcpyAsync(b[k].d_intrin, &in, sizeof(rs2_intrinsics), cudaMemcpyHostToDevice, s));
        kernel_deproject_depth_cuda<<<nb, RS2_CUDA_THREADS_PER_BLOCK, 0, s>>>(b[k].d_points, b[k].d_intrin, b[k].d_depth, 0.001f);
        CK(cudaMemcpyAsync(h_points, b[k].d_points, static_cast<size_t>(count) * 3 * sizeof(float), cudaMemcpyDeviceToHost, s));
        if (record) CK(cudaEventRecord(ev1[i], s));
    };

    for (int i = 0; i < WARMUP; ++i) enqueue(i, false);
    for (int k = 0; k < KDEPTH; ++k) CK(cudaStreamSynchronize(st[k]));

    cudaEvent_t t0, t1; CK(cudaEventCreate(&t0)); CK(cudaEventCreate(&t1));
    CK(cudaEventRecord(t0));
    for (int i = 0; i < ITERS; ++i) enqueue(i, true);
    for (int k = 0; k < KDEPTH; ++k) CK(cudaStreamSynchronize(st[k]));
    CK(cudaEventRecord(t1)); CK(cudaEventSynchronize(t1));
    float wall_ms = 0; CK(cudaEventElapsedTime(&wall_ms, t0, t1));
    fps = ITERS / (wall_ms / 1000.0);

    std::vector<double> lat; lat.reserve(ITERS);
    for (int i = 0; i < ITERS; ++i) {
        float ms = 0; CK(cudaEventElapsedTime(&ms, ev0[i], ev1[i]));
        lat.push_back(ms * 1000.0);
    }

    for (int i = 0; i < ITERS; ++i) { CK(cudaEventDestroy(ev0[i])); CK(cudaEventDestroy(ev1[i])); }
    CK(cudaEventDestroy(t0)); CK(cudaEventDestroy(t1));
    for (int k = 0; k < KDEPTH; ++k) { CK(cudaStreamDestroy(st[k])); free_dev(b[k]); }
    return summarize(lat);
}

// REF: the shipped cached path itself (pageable host, internal sync), via the library wrapper.
static Stats variant_REF(const rs2_intrinsics& in, const uint16_t* h_depth,
                         std::vector<float>& h_points, double& fps) {
    for (int i = 0; i < WARMUP; ++i)
        rscuda::deproject_depth_cuda(h_points.data(), in, h_depth, 0.001f);
    CK(cudaDeviceSynchronize());

    std::vector<double> lat; lat.reserve(ITERS);
    cudaEvent_t t0, t1; CK(cudaEventCreate(&t0)); CK(cudaEventCreate(&t1));
    CK(cudaEventRecord(t0));
    for (int i = 0; i < ITERS; ++i) {
        cudaEvent_t e0, e1; CK(cudaEventCreate(&e0)); CK(cudaEventCreate(&e1));
        CK(cudaEventRecord(e0));
        rscuda::deproject_depth_cuda(h_points.data(), in, h_depth, 0.001f);
        CK(cudaEventRecord(e1)); CK(cudaEventSynchronize(e1));
        float ms = 0; CK(cudaEventElapsedTime(&ms, e0, e1));
        lat.push_back(ms * 1000.0);
        CK(cudaEventDestroy(e0)); CK(cudaEventDestroy(e1));
    }
    CK(cudaEventRecord(t1)); CK(cudaEventSynchronize(t1));
    float wall_ms = 0; CK(cudaEventElapsedTime(&wall_ms, t0, t1));
    fps = ITERS / (wall_ms / 1000.0);
    CK(cudaEventDestroy(t0)); CK(cudaEventDestroy(t1));
    return summarize(lat);
}

static void run(const Res& r) {
    const int count = r.W * r.H;
    rs2_intrinsics in{ r.W, r.H, r.W * 0.5f, r.H * 0.5f, 600.f, 600.f, RS2_DISTORTION_NONE, {0,0,0,0,0} };

    // pinned host buffers for A/B; pageable vector for REF (matches shipped path)
    uint16_t* h_depth_pin = nullptr; float* h_points_pin = nullptr;
    CK(cudaHostAlloc(&h_depth_pin,  static_cast<size_t>(count) * sizeof(uint16_t), cudaHostAllocDefault));
    CK(cudaHostAlloc(&h_points_pin, static_cast<size_t>(count) * 3 * sizeof(float), cudaHostAllocDefault));
    make_depth(h_depth_pin, r.W, r.H);
    std::vector<uint16_t> h_depth_page(count);
    std::memcpy(h_depth_page.data(), h_depth_pin, static_cast<size_t>(count) * sizeof(uint16_t));
    std::vector<float> h_points_page(static_cast<size_t>(count) * 3, -7.f);

    printf("--- POINTCLOUD  %s  (count=%d px, %d streams) ---\n", r.name, count, KDEPTH);
    StageTimes st_t = stage_table(r, in, h_depth_pin, h_points_pin);

    double fps_ref = 0, fps_a = 0, fps_b = 0;
    Stats ref = variant_REF(in, h_depth_page.data(), h_points_page, fps_ref);
    Stats a   = variant_A(r, in, h_depth_pin, h_points_pin, fps_a);
    Stats b   = variant_B(r, in, h_depth_pin, h_points_pin, fps_b);

    printf("  REF (shipped cached, pageable+sync) : %7.1f fps   lat mean=%7.1f p50=%7.1f p95=%7.1f us\n",
           fps_ref, ref.mean, ref.p50, ref.p95);
    printf("  A   (pinned, 1 stream, sync/frame)  : %7.1f fps   lat mean=%7.1f p50=%7.1f p95=%7.1f us\n",
           fps_a, a.mean, a.p50, a.p95);
    printf("  B   (pinned, %d-stream, async pipe)  : %7.1f fps   lat mean=%7.1f p50=%7.1f p95=%7.1f us\n",
           KDEPTH, fps_b, b.mean, b.p50, b.p95);
    printf("  attribution: pinning gain (REF->A) %+5.1f%%   overlap gain (A->B) %+5.1f%%   "
           "total P4 (REF->B) %+5.1f%%   [fps]\n",
           100.0 * (fps_a - fps_ref) / fps_ref,
           100.0 * (fps_b - fps_a)   / fps_a,
           100.0 * (fps_b - fps_ref) / fps_ref);
    printf("  overlap efficiency (B): %5.1f%%   (period_B=%.1f us vs sum=%.1f floor=%.1f us)\n",
           100.0 * overlap_eff(st_t, fps_b), 1e6 / fps_b, st_t.sum, st_t.mx);
    printf("\n");

    CK(cudaFreeHost(h_depth_pin)); CK(cudaFreeHost(h_points_pin));
}

} // namespace pc

// ============================== CONVERSION WORKLOAD ==============================
// YUYV -> BGR8 (the Finding-A color path).  Per frame: H2D src (superPix*4) ; kernel ; D2H dst (n*3).
namespace conv {

struct DevBuf { uint8_t* d_src = nullptr; uint8_t* d_dst = nullptr; };
static void alloc_dev(DevBuf& b, int superPix, int n) {
    CK(cudaMalloc(&b.d_src, static_cast<size_t>(superPix) * 4));
    CK(cudaMalloc(&b.d_dst, static_cast<size_t>(n) * 3));
}
static void free_dev(DevBuf& b) {
    if (b.d_src) CK(cudaFree(b.d_src));
    if (b.d_dst) CK(cudaFree(b.d_dst));
    b = DevBuf{};
}

static StageTimes stage_table(const Res& r, const uint8_t* h_src_pin, uint8_t* h_dst_pin) {
    const int n = r.W * r.H, superPix = n / 2, nb = superPix / RS2_CUDA_THREADS_PER_BLOCK;
    const size_t src_bytes = static_cast<size_t>(superPix) * 4, dst_bytes = static_cast<size_t>(n) * 3;
    DevBuf b; alloc_dev(b, superPix, n);
    cudaStream_t s; CK(cudaStreamCreate(&s));
    cudaEvent_t e0, e1; CK(cudaEventCreate(&e0)); CK(cudaEventCreate(&e1));
    auto time_ms = [&](auto&& body) {
        for (int i = 0; i < WARMUP; ++i) body();
        CK(cudaStreamSynchronize(s));
        std::vector<double> t; t.reserve(ITERS);
        for (int i = 0; i < ITERS; ++i) {
            CK(cudaEventRecord(e0, s)); body(); CK(cudaEventRecord(e1, s)); CK(cudaEventSynchronize(e1));
            float ms = 0; CK(cudaEventElapsedTime(&ms, e0, e1)); t.push_back(ms * 1000.0);
        }
        return summarize(t).mean;
    };
    double h2d = time_ms([&]{ CK(cudaMemcpyAsync(b.d_src, h_src_pin, src_bytes, cudaMemcpyHostToDevice, s)); });
    CK(cudaMemcpy(b.d_src, h_src_pin, src_bytes, cudaMemcpyHostToDevice));
    double ker = time_ms([&]{ kernel_unpack_yuy2_bgr8_cuda<<<nb, RS2_CUDA_THREADS_PER_BLOCK, 0, s>>>(b.d_src, b.d_dst, superPix); });
    double d2h = time_ms([&]{ CK(cudaMemcpyAsync(h_dst_pin, b.d_dst, dst_bytes, cudaMemcpyDeviceToHost, s)); });
    StageTimes st_t{ h2d, ker, d2h, h2d + ker + d2h, std::max(ker, h2d + d2h) };
    printf("  [stage]  H2D=%7.1f us   kernel=%7.1f us   D2H=%7.1f us   (sum=%7.1f, overlap floor=max=%7.1f) us\n",
           h2d, ker, d2h, st_t.sum, st_t.mx);
    CK(cudaEventDestroy(e0)); CK(cudaEventDestroy(e1)); CK(cudaStreamDestroy(s)); free_dev(b);
    return st_t;
}

static Stats variant_A(const Res& r, const uint8_t* h_src, uint8_t* h_dst, double& fps) {
    const int n = r.W * r.H, superPix = n / 2, nb = superPix / RS2_CUDA_THREADS_PER_BLOCK;
    const size_t src_bytes = static_cast<size_t>(superPix) * 4, dst_bytes = static_cast<size_t>(n) * 3;
    DevBuf b; alloc_dev(b, superPix, n);
    cudaStream_t s; CK(cudaStreamCreate(&s));
    cudaEvent_t e0, e1; CK(cudaEventCreate(&e0)); CK(cudaEventCreate(&e1));
    auto frame = [&]{
        CK(cudaMemcpyAsync(b.d_src, h_src, src_bytes, cudaMemcpyHostToDevice, s));
        kernel_unpack_yuy2_bgr8_cuda<<<nb, RS2_CUDA_THREADS_PER_BLOCK, 0, s>>>(b.d_src, b.d_dst, superPix);
        CK(cudaMemcpyAsync(h_dst, b.d_dst, dst_bytes, cudaMemcpyDeviceToHost, s));
        CK(cudaStreamSynchronize(s));
    };
    for (int i = 0; i < WARMUP; ++i) frame();
    std::vector<double> lat; lat.reserve(ITERS);
    cudaEvent_t t0, t1; CK(cudaEventCreate(&t0)); CK(cudaEventCreate(&t1));
    CK(cudaEventRecord(t0));
    for (int i = 0; i < ITERS; ++i) {
        CK(cudaEventRecord(e0, s)); frame(); CK(cudaEventRecord(e1, s)); CK(cudaEventSynchronize(e1));
        float ms = 0; CK(cudaEventElapsedTime(&ms, e0, e1)); lat.push_back(ms * 1000.0);
    }
    CK(cudaEventRecord(t1)); CK(cudaEventSynchronize(t1));
    float wall_ms = 0; CK(cudaEventElapsedTime(&wall_ms, t0, t1)); fps = ITERS / (wall_ms / 1000.0);
    CK(cudaEventDestroy(t0)); CK(cudaEventDestroy(t1));
    CK(cudaEventDestroy(e0)); CK(cudaEventDestroy(e1)); CK(cudaStreamDestroy(s)); free_dev(b);
    return summarize(lat);
}

static Stats variant_B(const Res& r, const uint8_t* h_src, uint8_t* h_dst, double& fps) {
    const int n = r.W * r.H, superPix = n / 2, nb = superPix / RS2_CUDA_THREADS_PER_BLOCK;
    const size_t src_bytes = static_cast<size_t>(superPix) * 4, dst_bytes = static_cast<size_t>(n) * 3;
    std::vector<DevBuf> b(KDEPTH); std::vector<cudaStream_t> st(KDEPTH);
    for (int k = 0; k < KDEPTH; ++k) { alloc_dev(b[k], superPix, n); CK(cudaStreamCreate(&st[k])); }
    std::vector<cudaEvent_t> ev0(ITERS), ev1(ITERS);
    for (int i = 0; i < ITERS; ++i) { CK(cudaEventCreate(&ev0[i])); CK(cudaEventCreate(&ev1[i])); }
    auto enqueue = [&](int i, bool record) {
        int k = i % KDEPTH; cudaStream_t s = st[k];
        if (record) CK(cudaEventRecord(ev0[i], s));
        CK(cudaMemcpyAsync(b[k].d_src, h_src, src_bytes, cudaMemcpyHostToDevice, s));
        kernel_unpack_yuy2_bgr8_cuda<<<nb, RS2_CUDA_THREADS_PER_BLOCK, 0, s>>>(b[k].d_src, b[k].d_dst, superPix);
        CK(cudaMemcpyAsync(h_dst, b[k].d_dst, dst_bytes, cudaMemcpyDeviceToHost, s));
        if (record) CK(cudaEventRecord(ev1[i], s));
    };
    for (int i = 0; i < WARMUP; ++i) enqueue(i, false);
    for (int k = 0; k < KDEPTH; ++k) CK(cudaStreamSynchronize(st[k]));
    cudaEvent_t t0, t1; CK(cudaEventCreate(&t0)); CK(cudaEventCreate(&t1));
    CK(cudaEventRecord(t0));
    for (int i = 0; i < ITERS; ++i) enqueue(i, true);
    for (int k = 0; k < KDEPTH; ++k) CK(cudaStreamSynchronize(st[k]));
    CK(cudaEventRecord(t1)); CK(cudaEventSynchronize(t1));
    float wall_ms = 0; CK(cudaEventElapsedTime(&wall_ms, t0, t1)); fps = ITERS / (wall_ms / 1000.0);
    std::vector<double> lat; lat.reserve(ITERS);
    for (int i = 0; i < ITERS; ++i) { float ms = 0; CK(cudaEventElapsedTime(&ms, ev0[i], ev1[i])); lat.push_back(ms * 1000.0); }
    for (int i = 0; i < ITERS; ++i) { CK(cudaEventDestroy(ev0[i])); CK(cudaEventDestroy(ev1[i])); }
    CK(cudaEventDestroy(t0)); CK(cudaEventDestroy(t1));
    for (int k = 0; k < KDEPTH; ++k) { CK(cudaStreamDestroy(st[k])); free_dev(b[k]); }
    return summarize(lat);
}

static Stats variant_REF(int n, const uint8_t* h_src, std::vector<uint8_t>& h_dst, double& fps) {
    for (int i = 0; i < WARMUP; ++i)
        rscuda::unpack_yuy2_cuda_helper(h_src, h_dst.data(), n, RS2_FORMAT_BGR8);
    CK(cudaDeviceSynchronize());
    std::vector<double> lat; lat.reserve(ITERS);
    cudaEvent_t t0, t1; CK(cudaEventCreate(&t0)); CK(cudaEventCreate(&t1));
    CK(cudaEventRecord(t0));
    for (int i = 0; i < ITERS; ++i) {
        cudaEvent_t e0, e1; CK(cudaEventCreate(&e0)); CK(cudaEventCreate(&e1));
        CK(cudaEventRecord(e0));
        rscuda::unpack_yuy2_cuda_helper(h_src, h_dst.data(), n, RS2_FORMAT_BGR8);
        CK(cudaEventRecord(e1)); CK(cudaEventSynchronize(e1));
        float ms = 0; CK(cudaEventElapsedTime(&ms, e0, e1)); lat.push_back(ms * 1000.0);
        CK(cudaEventDestroy(e0)); CK(cudaEventDestroy(e1));
    }
    CK(cudaEventRecord(t1)); CK(cudaEventSynchronize(t1));
    float wall_ms = 0; CK(cudaEventElapsedTime(&wall_ms, t0, t1)); fps = ITERS / (wall_ms / 1000.0);
    CK(cudaEventDestroy(t0)); CK(cudaEventDestroy(t1));
    return summarize(lat);
}

static void run(const Res& r) {
    const int n = r.W * r.H, superPix = n / 2;
    uint8_t* h_src_pin = nullptr; uint8_t* h_dst_pin = nullptr;
    CK(cudaHostAlloc(&h_src_pin, static_cast<size_t>(superPix) * 4, cudaHostAllocDefault));
    CK(cudaHostAlloc(&h_dst_pin, static_cast<size_t>(n) * 3, cudaHostAllocDefault));
    make_yuyv(h_src_pin, r.W, r.H);
    std::vector<uint8_t> h_src_page(static_cast<size_t>(superPix) * 4);
    std::memcpy(h_src_page.data(), h_src_pin, static_cast<size_t>(superPix) * 4);
    std::vector<uint8_t> h_dst_page(static_cast<size_t>(n) * 3, 0xCC);

    printf("--- CONVERSION (YUYV->BGR8)  %s  (count=%d px, %d streams) ---\n", r.name, n, KDEPTH);
    StageTimes st_t = stage_table(r, h_src_pin, h_dst_pin);

    double fps_ref = 0, fps_a = 0, fps_b = 0;
    Stats ref = variant_REF(n, h_src_page.data(), h_dst_page, fps_ref);
    Stats a   = variant_A(r, h_src_pin, h_dst_pin, fps_a);
    Stats b   = variant_B(r, h_src_pin, h_dst_pin, fps_b);

    printf("  REF (shipped cached, pageable+sync) : %7.1f fps   lat mean=%7.1f p50=%7.1f p95=%7.1f us\n",
           fps_ref, ref.mean, ref.p50, ref.p95);
    printf("  A   (pinned, 1 stream, sync/frame)  : %7.1f fps   lat mean=%7.1f p50=%7.1f p95=%7.1f us\n",
           fps_a, a.mean, a.p50, a.p95);
    printf("  B   (pinned, %d-stream, async pipe)  : %7.1f fps   lat mean=%7.1f p50=%7.1f p95=%7.1f us\n",
           KDEPTH, fps_b, b.mean, b.p50, b.p95);
    printf("  attribution: pinning gain (REF->A) %+5.1f%%   overlap gain (A->B) %+5.1f%%   "
           "total P4 (REF->B) %+5.1f%%   [fps]\n",
           100.0 * (fps_a - fps_ref) / fps_ref,
           100.0 * (fps_b - fps_a)   / fps_a,
           100.0 * (fps_b - fps_ref) / fps_ref);
    printf("  overlap efficiency (B): %5.1f%%   (period_B=%.1f us vs sum=%.1f floor=%.1f us)\n",
           100.0 * overlap_eff(st_t, fps_b), 1e6 / fps_b, st_t.sum, st_t.mx);
    printf("\n");

    CK(cudaFreeHost(h_src_pin)); CK(cudaFreeHost(h_dst_pin));
}

} // namespace conv

int main() {
    int dev = 0; cudaDeviceProp prop;
    CK(cudaGetDevice(&dev)); CK(cudaGetDeviceProperties(&prop, dev));
    printf("==================================================================\n");
    printf("  bench_async_pipeline  [NO CAMERA, SYNTHETIC]   #31 P4 async pipelining\n");
    printf("  GPU: %s  sm_%d%d  unified=%d  warmup=%d iters=%d K=%d\n",
           prop.name, prop.major, prop.minor, prop.unifiedAddressing, WARMUP, ITERS, KDEPTH);
    printf("  REF=shipped cached path (mode 1)  A=pinned+1-stream+sync  B=pinned+K-stream+async\n");
    printf("==================================================================\n\n");

    const Res resolutions[] = {
        { 848, 480, " 848x480" },
        { 1280, 720, "1280x720" },
    };
    for (const auto& r : resolutions) pc::run(r);
    for (const auto& r : resolutions) conv::run(r);

    printf("Interpretation (VERDICT: NO-GO for single-camera real-time):\n");
    printf("  * Overlap efficiency = (sum_stages - period_B) / (sum_stages - max_stage); 100%% means\n");
    printf("    B fully hid copies behind compute (period_B == max_stage), 0%% means no overlap.\n");
    printf("    The [stage] table shows D2H >> kernel here: the kernel is SMALL and the copy is the\n");
    printf("    cost, so overlap DOES move B from sum(stages) toward max(stage) -- the mechanism is\n");
    printf("    real, NOT zero.  But on GB10's unified memory concurrent copies + compute contend\n");
    printf("    for ONE memory pool, so efficiency collapses as resolution grows (see pc-1280).\n");
    printf("  * The decisive reason for NO-GO is NOT latency (B's worst ~1.1 ms is well inside an\n");
    printf("    11-33 ms camera budget) and NOT 'copies are free'.  It is: the whole CUDA op is\n");
    printf("    ~57-280 us, i.e. already 80-270x the 30-90 fps camera rate, and Finding A's ~2 ms/frame\n");
    printf("    e2e cost is PLUMBING that pipelining cannot touch.  Speeding a stage worth <2.5%% of\n");
    printf("    the frame budget -- by any factor -- is invisible end-to-end.  Cost side is pure\n");
    printf("    downside: K-stream breaks the byte-identical single-buffer cache + raises latency 3-4x.\n");
    printf("  * Clock-robust: at full SM clock the kernel shrinks while bandwidth-bound copies scale\n");
    printf("    less, so the op gets MORE copy-bound -> overlap would help throughput MORE, never less\n");
    printf("    -> the 'unusable headroom' verdict only deepens.\n");
    printf("  * Scope nuance: conversion shows up to ~+50%% aggregate throughput; if a future OFFLINE/\n");
    printf("    batch or multi-camera aggregate-throughput workload appears, revisit THEN.\n");
    return 0;
}
