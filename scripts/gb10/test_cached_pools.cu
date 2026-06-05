// R4 unit test: GB10 CUDA cached-pool correctness (pointcloud + conversion).
//
// Exercises the now-DEFAULT cached device-buffer pools over a SEQUENCE of resolutions in one process,
// so the grow-only buffer logic is tested across size changes (grow, reuse-when-fits, no-shrink). The
// runner (test-cached-pools.sh) runs this binary in mode 0 (baseline, per-frame malloc) and mode 1
// (cached) over the same sequence and asserts BYTE-IDENTICAL output at every resolution -> proves the
// cache never corrupts as the buffer grows/reuses, and that mode selection is honored.
//
// Build (both pools, both defines so the gated #if blocks are live) and run: see test-cached-pools.sh.
// nvcc -DRS2_USE_CUDA -DRS2_GB10_PC_ZEROCOPY -DRS2_GB10_CONV_CACHE -I<repo>/{include,src,src/cuda}
//   test_cached_pools.cu cuda-pointcloud.cu cuda-conversion.cu
//
// Usage:  test_cached_pools <conv|pc> <out_prefix> <WxH> [<WxH> ...]     (mode via RS2_{CONV,PC}_MODE)
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <cstdint>
#include <vector>
#include <string>

#include "cuda-conversion.cuh"
#include "cuda-pointcloud.cuh"
#include "../../include/librealsense2/rs.h"

static void make_yuyv(uint8_t* b, int W, int H) {           // deterministic YUYV gradient
    int sp = W * H / 2;
    for (int i = 0; i < sp; ++i) {
        b[4*i+0] = (uint8_t)(i * 3);  b[4*i+1] = (uint8_t)(i * 5 + 17);
        b[4*i+2] = (uint8_t)(i * 7);  b[4*i+3] = (uint8_t)(i * 11 + 29);
    }
}
static void make_depth(uint16_t* d, int W, int H) {         // deterministic radial Z16
    float cx = W * 0.5f, cy = H * 0.5f;
    for (int y = 0; y < H; ++y)
        for (int x = 0; x < W; ++x) {
            float dx = x - cx, dy = y - cy, r = dx*dx + dy*dy;
            uint16_t z = (uint16_t)(500 + (int)(r * 0.01f) % 4000);
            if ((x + y) % 9 == 0) z = 0;                    // some invalid pixels
            d[(size_t)y*W + x] = z;
        }
}
static uint64_t fnv(const void* p, size_t n) {              // tiny content hash for the log
    const uint8_t* b = (const uint8_t*)p; uint64_t h = 1469598103934665603ULL;
    for (size_t i = 0; i < n; ++i) { h ^= b[i]; h *= 1099511628211ULL; }
    return h;
}
static void dump(const std::string& path, const void* p, size_t n) {
    FILE* f = fopen(path.c_str(), "wb"); if (f) { fwrite(p, 1, n, f); fclose(f); }
}

int main(int argc, char** argv) {
    if (argc < 4) { fprintf(stderr, "usage: %s <conv|pc> <out_prefix> <WxH> [<WxH>...]\n", argv[0]); return 2; }
    std::string pool = argv[1], prefix = argv[2];
    bool is_conv = (pool == "conv");
    const char* menv = getenv(is_conv ? "RS2_CONV_MODE" : "RS2_PC_MODE");
    int mode = menv ? atoi(menv) : 0;

#if defined(RS2_GB10_CONV_CACHE) && defined(RS2_GB10_PC_ZEROCOPY)
    const char* compiled = "COMPILED";
#else
    const char* compiled = "NOT_COMPILED";
    if (mode == 1) { fprintf(stderr, "ERROR: mode 1 requested but cache define not compiled in.\n"); return 3; }
#endif
    printf("[cached-pools] pool=%s mode=%d cache=%s  sequence:", pool.c_str(), mode, compiled);
    for (int i = 3; i < argc; ++i) printf(" %s", argv[i]);
    printf("\n");

    for (int i = 3; i < argc; ++i) {
        int W = 0, H = 0; sscanf(argv[i], "%dx%d", &W, &H);
        std::string out = prefix + "-" + std::to_string(i - 3) + ".bin";
        if (is_conv) {
            int n = W * H; size_t srcN = (size_t)(n/2)*4, dstN = (size_t)n*3;
            std::vector<uint8_t> src(srcN), dst(dstN, 0xCC);
            make_yuyv(src.data(), W, H);
            for (int k = 0; k < 3; ++k) rscuda::unpack_yuy2_cuda_helper(src.data(), dst.data(), n, RS2_FORMAT_BGR8);
            printf("   %dx%d conv -> %zu bytes  hash=%016llx\n", W, H, dstN, (unsigned long long)fnv(dst.data(), dstN));
            dump(out, dst.data(), dstN);
        } else {
            int n = W * H; std::vector<uint16_t> depth(n); std::vector<float> pts((size_t)n*3, -7.f);
            make_depth(depth.data(), W, H);
            rs2_intrinsics in{ W, H, W*0.5f, H*0.5f, 600.f, 600.f, RS2_DISTORTION_NONE, {0,0,0,0,0} };
            for (int k = 0; k < 3; ++k) rscuda::deproject_depth_cuda(pts.data(), in, depth.data(), 0.001f);
            size_t dstN = pts.size()*sizeof(float);
            printf("   %dx%d pc -> %zu bytes  hash=%016llx\n", W, H, dstN, (unsigned long long)fnv(pts.data(), dstN));
            dump(out, pts.data(), dstN);
        }
    }
    return 0;
}
