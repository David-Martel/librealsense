// Correctness harness for RS2_GB10_CONV_CACHE cached-buffer path.
// Proves that mode 1 (cached device buffers) is BYTE-IDENTICAL to mode 0 (baseline)
// on synthetic YUYV gradient inputs at 1280x720 and 848x480 for RS2_FORMAT_BGR8 and RS2_FORMAT_RGB8.
//
// Usage (two-process approach to defeat the static-mode latch in rs2_conv_mode()):
//   # compile with RS2_GB10_CONV_CACHE defined so the #if block is live
//   LRS=/home/damartel/dev/repos/librealsense
//   nvcc -DRS2_USE_CUDA -DRS2_GB10_CONV_CACHE \
//        -I$LRS/include -I$LRS/src/cuda \
//        $LRS/scripts/gb10/test_conv_cache_correctness.cu \
//        $LRS/src/cuda/cuda-conversion.cu \
//        -O2 -o /tmp/test_conv_cache_correctness
//
//   # Run baseline (mode 0): dump output to /tmp/conv_mode0.bin
//   RS2_CONV_MODE=0 /tmp/test_conv_cache_correctness 1280 720 bgr8 /tmp/conv_mode0.bin
//   RS2_CONV_MODE=1 /tmp/test_conv_cache_correctness 1280 720 bgr8 /tmp/conv_mode1.bin
//   # then diff or let the harness's cmp_and_report() path do it
//
// The harness also runs INLINE comparison when --cmp flag is passed to a separate mode=1 run
// against a pre-saved mode=0 dump.  See main() below.

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <cassert>
#include <string>
#include <vector>
#include <algorithm>

#include "cuda-conversion.cuh"

// Minimal rs2_format values needed (mirror librealsense2/rs.h)
#ifndef RS2_FORMAT_BGR8
#include "../../include/librealsense2/rs.h"
#endif

// Generate a deterministic YUYV gradient: each 4-byte macro-pixel encodes a ramp
// so every (Y0, U, Y1, V) is a predictable function of its macro-pixel index.
static void make_yuyv_gradient(uint8_t* buf, int W, int H) {
    // YUYV: buf[4*i+0]=Y0, buf[4*i+1]=U, buf[4*i+2]=Y1, buf[4*i+3]=V
    int superpix = (W * H) / 2;
    for (int i = 0; i < superpix; i++) {
        buf[4*i+0] = (uint8_t)((i * 7 + 16)  & 0xFF);  // Y0: range 16..235 preferred, wrap ok
        buf[4*i+1] = (uint8_t)((i * 3 + 80)  & 0xFF);  // U
        buf[4*i+2] = (uint8_t)((i * 11 + 32) & 0xFF);  // Y1
        buf[4*i+3] = (uint8_t)((i * 5 + 120) & 0xFF);  // V
    }
}

// Write raw bytes to file
static void write_bin(const char* path, const uint8_t* data, size_t bytes) {
    FILE* f = fopen(path, "wb");
    if (!f) { perror(path); exit(1); }
    fwrite(data, 1, bytes, f);
    fclose(f);
}

// Read raw bytes from file
static std::vector<uint8_t> read_bin(const char* path) {
    FILE* f = fopen(path, "rb");
    if (!f) { perror(path); exit(1); }
    fseek(f, 0, SEEK_END); long sz = ftell(f); rewind(f);
    std::vector<uint8_t> buf(sz);
    size_t nread = fread(buf.data(), 1, sz, f);
    (void)nread;
    fclose(f);
    return buf;
}

int main(int argc, char** argv) {
    // args: <W> <H> <format: bgr8|rgb8|bgra8|rgba8|y16> <output.bin> [--cmp <reference.bin>]
    if (argc < 5) {
        fprintf(stderr, "Usage: %s <W> <H> <format> <output.bin> [--cmp <ref.bin>]\n", argv[0]);
        return 2;
    }
    int W = atoi(argv[1]);
    int H = atoi(argv[2]);
    const char* fmt_str = argv[3];
    const char* out_path = argv[4];

    rs2_format format;
    int bpp; // output bytes per pixel
    if      (strcmp(fmt_str, "bgr8")  == 0) { format = RS2_FORMAT_BGR8;  bpp = 3; }
    else if (strcmp(fmt_str, "rgb8")  == 0) { format = RS2_FORMAT_RGB8;  bpp = 3; }
    else if (strcmp(fmt_str, "bgra8") == 0) { format = RS2_FORMAT_BGRA8; bpp = 4; }
    else if (strcmp(fmt_str, "rgba8") == 0) { format = RS2_FORMAT_RGBA8; bpp = 4; }
    else if (strcmp(fmt_str, "y16")   == 0) { format = RS2_FORMAT_Y16;   bpp = 2; }
    else { fprintf(stderr, "Unknown format: %s\n", fmt_str); return 2; }

    int n = W * H; // total pixels
    size_t src_bytes = (size_t)(n / 2) * 4; // YUYV
    size_t dst_bytes = (size_t)n * bpp;

    // Determine current mode (static latch fires here for the first and only time)
    const char* mode_env = getenv("RS2_CONV_MODE");
    int mode = mode_env ? atoi(mode_env) : 0;

    printf("[conv_cache_correctness] RS2_GB10_CONV_CACHE=%s  RS2_CONV_MODE=%d  %dx%d  fmt=%s\n",
#if defined(RS2_GB10_CONV_CACHE)
           "COMPILED",
#else
           "NOT_COMPILED",
#endif
           mode, W, H, fmt_str);

#if !defined(RS2_GB10_CONV_CACHE)
    if (mode == 1) {
        fprintf(stderr, "ERROR: RS2_CONV_MODE=1 requested but RS2_GB10_CONV_CACHE is NOT compiled in."
                        " This would test baseline vs baseline — aborting to prevent false 0-diff.\n");
        return 3;
    }
#endif

    std::vector<uint8_t> src(src_bytes);
    make_yuyv_gradient(src.data(), W, H);

    std::vector<uint8_t> dst(dst_bytes, 0xCC); // poison output

    // Run conversion (3 calls: warm-up + 2 measured to exercise the caching path)
    rscuda::unpack_yuy2_cuda_helper(src.data(), dst.data(), n, format);
    rscuda::unpack_yuy2_cuda_helper(src.data(), dst.data(), n, format);
    rscuda::unpack_yuy2_cuda_helper(src.data(), dst.data(), n, format);

    printf("[conv_cache_correctness] Conversion done. Writing %zu bytes to %s\n", dst_bytes, out_path);
    write_bin(out_path, dst.data(), dst_bytes);

    // Optional: compare against a reference (mode=0) dump
    int cmp_idx = -1;
    for (int i = 5; i < argc; i++) {
        if (strcmp(argv[i], "--cmp") == 0 && i + 1 < argc) { cmp_idx = i + 1; break; }
    }
    if (cmp_idx >= 0) {
        printf("[conv_cache_correctness] Comparing against reference: %s\n", argv[cmp_idx]);
        auto ref = read_bin(argv[cmp_idx]);
        if (ref.size() != dst_bytes) {
            fprintf(stderr, "ERROR: size mismatch: ref=%zu dst=%zu\n", ref.size(), dst_bytes);
            return 1;
        }
        int max_diff = 0;
        for (size_t i = 0; i < dst_bytes; i++) {
            int d = (int)dst[i] - (int)ref[i];
            if (d < 0) d = -d;
            if (d > max_diff) max_diff = d;
        }
        printf("[conv_cache_correctness] max-abs-diff = %d  (MUST be 0 for correctness)\n", max_diff);
        if (max_diff != 0) {
            fprintf(stderr, "CORRECTNESS FAIL: max-abs-diff=%d != 0\n", max_diff);
            return 1;
        }
        printf("[conv_cache_correctness] PASS: outputs are BYTE-IDENTICAL\n");
    }
    return 0;
}
