// License: Apache 2.0. See LICENSE file in root directory.
// Copyright(c) 2026 RealSense, Inc. All Rights Reserved.
//
// H7 feasibility microbench: per-frame allocation on the rs.align CUDA hot path.
//
// QUESTION: does rs.align allocate device/host memory EVERY aligned frame (like the
// pointcloud mode-0 churn that made CUDA 0.57x NEON), or are the buffers cached?
//
// FACT ESTABLISHED FROM SOURCE (no bench needed for this part):
//   src/proc/cuda/cuda-align.h    -> align_cuda holds `std::map<...,align_cuda_helper> aligners;`
//                                    as a member of the persistent processing block.
//   src/proc/cuda/cuda-align.cuh  -> align_cuda_helper holds the 7 device shared_ptr buffers
//                                    as MEMBERS, guarded by `if (!_d_depth_in) ...`.
//   => steady-state align does ZERO cudaMalloc/cudaFree per frame. Buffers alloc once,
//      reused until reset_cache() (resolution change). This is the SAME optimization that
//      pointcloud mode-1 applies; align already ships it.
//
// THIS BENCH quantifies the COUNTERFACTUAL: what the existing caching already saves, by
// timing the align op two ways at vigil's resolutions:
//   CACHED   = the as-shipped path: alloc the 7 buffers ONCE, then time only the per-frame
//              memcpy+memset+kernels+D2H (what really runs every frame).
//   CHURN    = a hypothetical regressed path that cudaMalloc's + cudaFree's the 7 buffers
//              EVERY frame (what align would cost if the `if (!_d_)` guards were removed,
//              i.e. the pointcloud mode-0 mistake applied to align).
//
// The delta (CHURN - CACHED) is the alloc overhead. We report it as a fraction of the
// CACHED align op and against vigil's 33ms (30fps) frame budget.
//
// This file deliberately re-implements the helper body inline (rather than linking
// cuda-align.cu's align_cuda_helper, whose members are private) so we can toggle the
// alloc strategy. The kernels themselves are #included from the real source so the GPU
// work measured is byte-for-byte the shipped align kernel.
//
// Build (see bench_h7h8.sh):
//   nvcc -O3 -std=c++14 -DRS2_USE_CUDA -Wno-deprecated-gpu-targets
//        -I<repo>/include -I<repo>/src -I<repo>/src/cuda
//        bench_h7h8_align_alloc.cu -o bench_h7h8_align_alloc
//
// Usage: bench_h7h8_align_alloc [iters] [warmup]

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <cstdint>
#include <vector>
#include <string>
#include <algorithm>
#include <cmath>

#include <cuda_runtime.h>
#include "../../include/librealsense2/rs.h"
#include "../../include/librealsense2/rsutil.h"

// Pull in the REAL shipped align kernels + device helpers. cuda-align.cu defines the
// __global__ kernels and align_cuda_helper; we reuse the kernels via the .cu's symbols by
// re-declaring them here (they have external linkage as non-static __global__).
// To avoid duplicate-symbol issues we re-implement the per-frame body using the same kernel
// math, included from rscuda_utils.cuh (device deproject/transform/project) + local kernels
// copied verbatim from cuda-align.cu so timing reflects the identical GPU work.
#include "../../src/cuda/rscuda_utils.cuh"

#define RS2_CUDA_THREADS_PER_BLOCK 32
using namespace rscuda;

template<int N> struct bytes { unsigned char b[N]; };

static int calc_block_size(int pixel_count, int thread_count) {
    return ((pixel_count % thread_count) == 0) ? (pixel_count / thread_count) : (pixel_count / thread_count + 1);
}

// --- kernels copied verbatim from src/proc/cuda/cuda-align.cu (depth->other path) ---
__device__ void k_transfer_pixels(int2* mapped_pixels, const rs2_intrinsics* depth_intrin,
    const rs2_intrinsics* other_intrin, const rs2_extrinsics* depth_to_other, float depth_val,
    int depth_x, int depth_y, int block_index)
{
    float shift = block_index ? 0.5f : -0.5f;
    auto depth_size = depth_intrin->width * depth_intrin->height;
    auto mapped_index = block_index * depth_size + (depth_y * depth_intrin->width + depth_x);
    if (mapped_index >= depth_size * 2) return;
    if (depth_val == 0) { mapped_pixels[mapped_index] = { -1, -1 }; return; }
    float depth_pixel[2] = { depth_x + shift, depth_y + shift }, depth_point[3], other_point[3], other_pixel[2];
    rscuda::rs2_deproject_pixel_to_point(depth_point, depth_intrin, depth_pixel, depth_val);
    rscuda::rs2_transform_point_to_point(other_point, depth_to_other, depth_point);
    rscuda::rs2_project_point_to_pixel(other_pixel, other_intrin, other_point);
    mapped_pixels[mapped_index].x = static_cast<int>(other_pixel[0] + 0.5f);
    mapped_pixels[mapped_index].y = static_cast<int>(other_pixel[1] + 0.5f);
}
__global__ void k_map_depth_to_other(int2* mapped_pixels, const uint16_t* depth_in,
    const rs2_intrinsics* depth_intrin, const rs2_intrinsics* other_intrin,
    const rs2_extrinsics* depth_to_other, float depth_scale)
{
    int depth_x = blockIdx.x * blockDim.x + threadIdx.x;
    int depth_y = blockIdx.y * blockDim.y + threadIdx.y;
    int depth_pixel_index = depth_y * depth_intrin->width + depth_x;
    if (depth_pixel_index >= depth_intrin->width * depth_intrin->height) return;
    float depth_val = depth_in[depth_pixel_index] * depth_scale;
    k_transfer_pixels(mapped_pixels, depth_intrin, other_intrin, depth_to_other, depth_val, depth_x, depth_y, blockIdx.z);
}
__global__ void k_depth_to_other(uint16_t* aligned_out, const uint16_t* depth_in, const int2* mapped_pixels,
    const rs2_intrinsics* depth_intrin, const rs2_intrinsics* other_intrin)
{
    int depth_x = blockIdx.x * blockDim.x + threadIdx.x;
    int depth_y = blockIdx.y * blockDim.y + threadIdx.y;
    auto depth_size = depth_intrin->width * depth_intrin->height;
    int depth_pixel_index = depth_y * depth_intrin->width + depth_x;
    if (depth_pixel_index >= depth_intrin->width * depth_intrin->height) return;
    int2 p0 = mapped_pixels[depth_pixel_index];
    int2 p1 = mapped_pixels[depth_size + depth_pixel_index];
    if (p0.x < 0 || p0.y < 0 || p1.x >= other_intrin->width || p1.y >= other_intrin->height) return;
    unsigned int new_val = depth_in[depth_pixel_index];
    unsigned int* arr = (unsigned int*)aligned_out;
    for (int y = p0.y; y <= p1.y; ++y)
        for (int x = p0.x; x <= p1.x; ++x) {
            new_val = new_val << 16 | new_val;
            atomicMin(&arr[(y * other_intrin->width + x) / 2], new_val);
        }
}
__global__ void k_replace_to_zero(uint16_t* aligned_out, const rs2_intrinsics* other_intrin) {
    int x = blockIdx.x * blockDim.x + threadIdx.x;
    int y = blockIdx.y * blockDim.y + threadIdx.y;
    auto idx = y * other_intrin->width + x;
    if (aligned_out[idx] == 0xffff) aligned_out[idx] = 0;
}

// ----- timing helpers -----
static double pct(std::vector<double>& v, double p) {
    if (v.empty()) return 0.0;
    std::sort(v.begin(), v.end());
    size_t i = (size_t)(p / 100.0 * (v.size() - 1) + 0.5);
    return v[std::min(i, v.size() - 1)];
}
static double median(std::vector<double> v) { return pct(v, 50.0); }

struct DevBufs {
    uint16_t* d_depth = nullptr;
    unsigned char* d_aligned = nullptr;
    int2* d_pixmap = nullptr;
    rs2_intrinsics* d_depth_intr = nullptr;
    rs2_intrinsics* d_other_intr = nullptr;
    rs2_extrinsics* d_extr = nullptr;
};

static void alloc_bufs(DevBufs& b, int depth_px, int other_px) {
    cudaMalloc(&b.d_depth, sizeof(uint16_t) * depth_px);
    cudaMalloc(&b.d_aligned, sizeof(uint16_t) * other_px);
    cudaMalloc(&b.d_pixmap, sizeof(int2) * depth_px * 2);
    cudaMalloc(&b.d_depth_intr, sizeof(rs2_intrinsics));
    cudaMalloc(&b.d_other_intr, sizeof(rs2_intrinsics));
    cudaMalloc(&b.d_extr, sizeof(rs2_extrinsics));
}
static void free_bufs(DevBufs& b) {
    cudaFree(b.d_depth); cudaFree(b.d_aligned); cudaFree(b.d_pixmap);
    cudaFree(b.d_depth_intr); cudaFree(b.d_other_intr); cudaFree(b.d_extr);
    b = DevBufs{};
}

// One align depth->other op. If churn==true, alloc+free the 6 device buffers this call
// (the counterfactual "guards removed" regression). intrinsics/extrinsics H2D copy also
// happens per-call in churn mode (mirrors make_device_copy being re-run uncached).
static void run_align_op(const rs2_intrinsics& dintr, const rs2_intrinsics& ointr,
    const rs2_extrinsics& extr, const uint16_t* h_depth, unsigned char* h_aligned,
    float depth_scale, DevBufs& cached, bool churn)
{
    int depth_px = dintr.width * dintr.height;
    int other_px = ointr.width * ointr.height;

    DevBufs local;
    DevBufs& b = churn ? local : cached;
    if (churn) alloc_bufs(b, depth_px, other_px);

    // per-frame copies (these happen EVERY frame in BOTH paths in the real code)
    cudaMemcpy(b.d_depth, h_depth, sizeof(uint16_t) * depth_px, cudaMemcpyHostToDevice);
    // intrinsics/extrinsics: cached path copies once (we did it at setup); churn re-copies
    if (churn) {
        cudaMemcpy(b.d_depth_intr, &dintr, sizeof(dintr), cudaMemcpyHostToDevice);
        cudaMemcpy(b.d_other_intr, &ointr, sizeof(ointr), cudaMemcpyHostToDevice);
        cudaMemcpy(b.d_extr, &extr, sizeof(extr), cudaMemcpyHostToDevice);
    }
    cudaMemset(b.d_aligned, 0xff, sizeof(uint16_t) * other_px);

    dim3 threads(RS2_CUDA_THREADS_PER_BLOCK, RS2_CUDA_THREADS_PER_BLOCK);
    dim3 depth_blocks(calc_block_size(dintr.width, threads.x), calc_block_size(dintr.height, threads.y));
    dim3 other_blocks(calc_block_size(ointr.width, threads.x), calc_block_size(ointr.height, threads.y));
    dim3 mapping_blocks(depth_blocks.x, depth_blocks.y, 2);

    k_map_depth_to_other<<<mapping_blocks, threads>>>(b.d_pixmap, b.d_depth, b.d_depth_intr, b.d_other_intr, b.d_extr, depth_scale);
    k_depth_to_other<<<depth_blocks, threads>>>((uint16_t*)b.d_aligned, b.d_depth, b.d_pixmap, b.d_depth_intr, b.d_other_intr);
    k_replace_to_zero<<<other_blocks, threads>>>((uint16_t*)b.d_aligned, b.d_other_intr);
    cudaStreamSynchronize(0);
    cudaMemcpy(h_aligned, b.d_aligned, sizeof(uint16_t) * other_px, cudaMemcpyDeviceToHost);

    if (churn) free_bufs(b);
}

static rs2_intrinsics make_intr(int w, int h) {
    rs2_intrinsics in{};
    in.width = w; in.height = h;
    in.ppx = w * 0.5f; in.ppy = h * 0.5f;
    in.fx = w * 0.9f; in.fy = w * 0.9f;
    in.model = RS2_DISTORTION_BROWN_CONRADY;
    for (int i = 0; i < 5; ++i) in.coeffs[i] = 0.0f;
    return in;
}

static void bench_res(int W, int H, int iters, int warmup) {
    rs2_intrinsics dintr = make_intr(W, H);
    rs2_intrinsics ointr = make_intr(W, H);
    rs2_extrinsics extr{};
    extr.rotation[0] = extr.rotation[4] = extr.rotation[8] = 1.0f;
    extr.translation[0] = 0.015f; // 15mm baseline
    float depth_scale = 0.001f;

    int depth_px = W * H, other_px = W * H;
    std::vector<uint16_t> h_depth(depth_px);
    for (int y = 0; y < H; ++y) for (int x = 0; x < W; ++x) {
        float dx = x - W * 0.5f, dy = y - H * 0.5f;
        uint16_t z = (uint16_t)(800 + ((int)(dx * dx + dy * dy) / 50) % 3000);
        if ((x + y) % 11 == 0) z = 0;
        h_depth[(size_t)y * W + x] = z;
    }
    std::vector<unsigned char> h_aligned(other_px * 2);

    DevBufs cached;
    alloc_bufs(cached, depth_px, other_px);
    // cached path: copy intrinsics/extrinsics ONCE (mirrors make_device_copy + if(!_d_) guard)
    cudaMemcpy(cached.d_depth_intr, &dintr, sizeof(dintr), cudaMemcpyHostToDevice);
    cudaMemcpy(cached.d_other_intr, &ointr, sizeof(ointr), cudaMemcpyHostToDevice);
    cudaMemcpy(cached.d_extr, &extr, sizeof(extr), cudaMemcpyHostToDevice);

    std::vector<double> t_cached, t_churn;

    for (int i = 0; i < warmup; ++i) {
        run_align_op(dintr, ointr, extr, h_depth.data(), h_aligned.data(), depth_scale, cached, false);
        DevBufs dummy; run_align_op(dintr, ointr, extr, h_depth.data(), h_aligned.data(), depth_scale, dummy, true);
    }

    cudaEvent_t a, b;
    cudaEventCreate(&a); cudaEventCreate(&b);
    for (int i = 0; i < iters; ++i) {
        cudaEventRecord(a);
        run_align_op(dintr, ointr, extr, h_depth.data(), h_aligned.data(), depth_scale, cached, false);
        cudaEventRecord(b); cudaEventSynchronize(b);
        float ms = 0; cudaEventElapsedTime(&ms, a, b); t_cached.push_back(ms);

        DevBufs dummy;
        cudaEventRecord(a);
        run_align_op(dintr, ointr, extr, h_depth.data(), h_aligned.data(), depth_scale, dummy, true);
        cudaEventRecord(b); cudaEventSynchronize(b);
        cudaEventElapsedTime(&ms, a, b); t_churn.push_back(ms);
    }
    cudaEventDestroy(a); cudaEventDestroy(b);
    free_bufs(cached);

    double c50 = median(t_cached), ch50 = median(t_churn);
    double c95 = pct(t_cached, 95), ch95 = pct(t_churn, 95);
    double alloc_overhead = ch50 - c50;
    double frac = (alloc_overhead / c50) * 100.0;
    double budget_pct = (alloc_overhead / 33.33) * 100.0;

    printf("  %4dx%-4d  CACHED p50=%.3f ms p95=%.3f | CHURN p50=%.3f ms p95=%.3f\n",
        W, H, c50, c95, ch50, ch95);
    printf("            alloc-churn overhead = %.3f ms (= %.1f%% of cached op, = %.2f%% of 33.3ms frame budget)\n",
        alloc_overhead, frac, budget_pct);
}

int main(int argc, char** argv) {
    int iters = (argc > 1) ? atoi(argv[1]) : 400;
    int warmup = (argc > 2) ? atoi(argv[2]) : 50;

    int dev = 0; cudaSetDevice(dev);
    cudaDeviceProp prop; cudaGetDeviceProperties(&prop, dev);
    printf("== H7: rs.align per-frame allocation microbench ==\n");
    printf("GPU: %s  sm_%d%d  iters=%d warmup=%d\n", prop.name, prop.major, prop.minor, iters, warmup);
    printf("SHIPPED FACT: align_cuda_helper caches all 7 device buffers (members + if(!_d_) guards);\n");
    printf("              steady-state align does 0 cudaMalloc/frame. CHURN column = counterfactual\n");
    printf("              'guards removed' regression. Delta = what the existing caching already saves.\n\n");

    bench_res(640, 480, iters, warmup);  // vigil primary
    bench_res(848, 480, iters, warmup);  // vigil alt
    bench_res(1280, 720, iters, warmup); // scaling reference

    printf("\nNOTE: cached p50 here is the FULL op (H2D depth + memset + 3 kernels + D2H), which\n");
    printf("      includes plumbing the HIL 0.293 ms figure also includes. Synthetic timing on an\n");
    printf("      idle GPU is a lower bound on real on-camera variance.\n");
    return 0;
}
