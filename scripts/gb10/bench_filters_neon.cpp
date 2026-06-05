// License: Apache 2.0. See LICENSE file in root directory.
// Copyright(c) 2026 RealSense, Inc. All Rights Reserved.
//
// GB10 (DGX-Spark, ARM64 Cortex-X925/A725) measure-first feasibility microbench
// for NEON + OpenMP acceleration of librealsense depth post-process filters.
//
// HONEST go/no-go study. No camera, no SDK linkage: synthetic deterministic depth.
// Each filter kernel is COPIED verbatim (logic-identical) from src/proc/ so the
// scalar baseline is the real shipping code path. For each candidate filter we
// compare:
//   (a) true-scalar      : kernel compiled with -fno-tree-vectorize (no autovec)
//   (b) autovec-O3       : kernel compiled at -O3 -ftree-vectorize  (== shipping binary)
//   (c) hand-NEON        : explicit NEON intrinsics, single thread
//   (d) NEON + OpenMP    : explicit NEON intrinsics, parallel rows
//
// Correctness: every variant's output is asserted bit/threshold-equal to scalar.
// FP-contraction is forced OFF (-ffp-contract=off) for every translation unit so
// scalar/NEON FMA fusion cannot diverge -> bit-identity is meaningful.
//
// The shipping SDK aarch64 flags are: -O3 -ftree-vectorize -mstrict-align
// -ffp-contract=fast (no -march). We match -O3 -ftree-vectorize -mstrict-align and
// pin -ffp-contract=off across ALL variants for a fair, identical-output compare.
// (The autovec baseline is therefore conservative-representative of the shipping
// scalar path; the verdict is autovec-vs-handNEON, the honest comparison.)

#include <arm_neon.h>
#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <random>
#include <string>
#include <vector>
#ifdef _OPENMP
#include <omp.h>
#endif

namespace {

// ---------------------------------------------------------------------------
// Synthetic depth generation: deterministic gradient + structured invalids +
// noise. Includes a realistic invalid(zero) fraction so the zero-skip branches
// in temporal/threshold/disparity are actually exercised.
// ---------------------------------------------------------------------------
constexpr float DEPTH_UNITS = 0.001f;   // 1mm, typical D400

std::vector<uint16_t> make_depth_z16(int w, int h, uint32_t seed, float invalid_frac) {
    std::vector<uint16_t> v(static_cast<size_t>(w) * h);
    std::mt19937 rng(seed);
    std::uniform_real_distribution<float> noise(-40.f, 40.f);
    std::uniform_real_distribution<float> u01(0.f, 1.f);
    for (int y = 0; y < h; ++y) {
        for (int x = 0; x < w; ++x) {
            // base ramp 300mm..6000mm across the frame + radial bump + noise
            float base = 300.f + 5700.f * (float(x) / w);
            float radial = 600.f * std::sin(0.01f * x) * std::cos(0.013f * y);
            float d = base + radial + noise(rng);
            uint16_t val = static_cast<uint16_t>(std::max(0.f, std::min(65000.f, d)));
            if (u01(rng) < invalid_frac) val = 0;   // structured holes
            v[size_t(y) * w + x] = val;
        }
    }
    return v;
}

std::vector<float> make_depth_f32(int w, int h, uint32_t seed, float invalid_frac) {
    auto z = make_depth_z16(w, h, seed, invalid_frac);
    std::vector<float> v(z.size());
    for (size_t i = 0; i < z.size(); ++i) v[i] = float(z[i]);
    return v;
}

// ---------------------------------------------------------------------------
// Timing harness: warmup discarded, many iters, mean/p50/p95 + Mpix/s + GB/s.
// Single-thread variants are expected to be pinned via taskset by the harness.
// ---------------------------------------------------------------------------
struct Stats { double mean_ms, p50_ms, p95_ms, mpix_s, gb_s; };

template <typename F>
Stats time_kernel(F&& fn, int warmup, int iters, size_t pixels, size_t bytes_touched) {
    for (int i = 0; i < warmup; ++i) fn();
    std::vector<double> ms;
    ms.reserve(iters);
    for (int i = 0; i < iters; ++i) {
        auto t0 = std::chrono::steady_clock::now();
        fn();
        auto t1 = std::chrono::steady_clock::now();
        ms.push_back(std::chrono::duration<double, std::milli>(t1 - t0).count());
    }
    std::sort(ms.begin(), ms.end());
    double sum = 0; for (double m : ms) sum += m;
    double mean = sum / ms.size();
    double p50 = ms[size_t(0.50 * (ms.size() - 1))];
    double p95 = ms[size_t(0.95 * (ms.size() - 1))];
    double mpix = (pixels / (mean / 1000.0)) / 1e6;
    double gbs = (bytes_touched / (mean / 1000.0)) / 1e9;
    return {mean, p50, p95, mpix, gbs};
}

void print_row(const char* filter, const char* variant, int w, int h, Stats s) {
    printf("%-12s %-14s %4dx%-4d  %8.4f  %8.4f  %8.4f  %9.1f  %7.2f\n",
           filter, variant, w, h, s.mean_ms, s.p50_ms, s.p95_ms, s.mpix_s, s.gb_s);
}

// ===========================================================================
// FILTER 1: THRESHOLD  (src/proc/threshold.cpp, process_frame inner loop)
//   out[i] = (du*d[i] in [min,max]) ? d[i] : 0   ; out pre-zeroed
// ===========================================================================
namespace threshold {

// (a) scalar -- verbatim from threshold.cpp lines 69-74
__attribute__((noinline))
void scalar(const uint16_t* in, uint16_t* out, int n, float du, float mn, float mx) {
    memset(out, 0, size_t(n) * sizeof(uint16_t));
    for (int i = 0; i < n; i++) {
        float dist = du * in[i];
        if (dist >= mn && dist <= mx) out[i] = in[i];
    }
}

// (c) hand-NEON: 8 px/iter. dist = du*d ; keep where mn<=dist<=mx.
__attribute__((noinline))
void neon(const uint16_t* in, uint16_t* out, int n, float du, float mn, float mx) {
    int i = 0;
    float32x4_t vdu = vdupq_n_f32(du);
    float32x4_t vmn = vdupq_n_f32(mn);
    float32x4_t vmx = vdupq_n_f32(mx);
    for (; i + 8 <= n; i += 8) {
        uint16x8_t d = vld1q_u16(in + i);
        uint32x4_t lo = vmovl_u16(vget_low_u16(d));
        uint32x4_t hi = vmovl_u16(vget_high_u16(d));
        float32x4_t fl = vmulq_f32(vcvtq_f32_u32(lo), vdu);
        float32x4_t fh = vmulq_f32(vcvtq_f32_u32(hi), vdu);
        uint32x4_t ml = vandq_u32(vcgeq_f32(fl, vmn), vcleq_f32(fl, vmx));
        uint32x4_t mh = vandq_u32(vcgeq_f32(fh, vmn), vcleq_f32(fh, vmx));
        // narrow 32-bit masks back to 16-bit and AND with input
        uint16x8_t m = vcombine_u16(vmovn_u32(ml), vmovn_u32(mh));
        vst1q_u16(out + i, vandq_u16(d, m));
    }
    for (; i < n; i++) {
        float dist = du * in[i];
        out[i] = (dist >= mn && dist <= mx) ? in[i] : 0;
    }
}

#ifdef _OPENMP
__attribute__((noinline))
void neon_omp(const uint16_t* in, uint16_t* out, int w, int h, float du, float mn, float mx) {
    #pragma omp parallel for schedule(static)
    for (int y = 0; y < h; ++y)
        neon(in + size_t(y) * w, out + size_t(y) * w, w, du, mn, mx);
}
#endif
} // namespace threshold

// ===========================================================================
// FILTER 2: TEMPORAL  (src/proc/temporal-filter.h temp_jw_smooth<uint16_t>)
//   per-pixel IIR with history mask + persistence LUT. Fully per-pixel.
// ===========================================================================
namespace temporal {

// (a) scalar -- verbatim logic from temporal-filter.h
__attribute__((noinline))
void scalar(uint16_t* frame, uint16_t* last, uint8_t* history,
            const uint8_t* persistence_map, int n, float alpha, uint16_t delta_z,
            uint8_t mask) {
    float one_minus_alpha = 1.f - alpha;
    for (int i = 0; i < n; i++) {
        uint16_t cur = frame[i];
        uint16_t prev = last[i];
        if (cur) {
            if (!prev) { last[i] = cur; history[i] = mask; }
            else {
                uint16_t diff = (uint16_t)std::fabs((float)cur - (float)prev);
                if (diff < delta_z) {
                    history[i] |= mask;
                    float filtered = alpha * cur + one_minus_alpha * prev;
                    uint16_t result = (uint16_t)filtered;
                    frame[i] = result; last[i] = result;
                } else { last[i] = cur; history[i] = mask; }
            }
        } else {
            if (prev) {
                uint8_t cls = persistence_map[history[i]];
                if (cls & mask) frame[i] = prev;
            }
            history[i] &= (uint8_t)~mask;
        }
    }
}

// (c) hand-NEON. The "smallDifference filter" arithmetic path is vectorized
// (the common case); the scatter-y history/persistence bookkeeping is done with
// vectorized masks. 8 px/iter. Branchless via select.
__attribute__((noinline))
void neon(uint16_t* frame, uint16_t* last, uint8_t* history,
          const uint8_t* persistence_map, int n, float alpha, uint16_t delta_z,
          uint8_t mask) {
    float one_minus_alpha = 1.f - alpha;
    int i = 0;
    float32x4_t valpha = vdupq_n_f32(alpha);
    float32x4_t voma = vdupq_n_f32(one_minus_alpha);
    uint16x8_t vdelta = vdupq_n_u16(delta_z);
    uint8x8_t vmask8 = vdup_n_u8(mask);
    uint8x8_t vnmask8 = vdup_n_u8((uint8_t)~mask);
    for (; i + 8 <= n; i += 8) {
        uint16x8_t cur = vld1q_u16(frame + i);
        uint16x8_t prev = vld1q_u16(last + i);
        uint8x8_t hist = vld1_u8(history + i);

        uint16x8_t cur_nz = vtstq_u16(cur, cur);     // cur!=0
        uint16x8_t prev_nz = vtstq_u16(prev, prev);  // prev!=0

        // abs diff (uint16 saturating-safe via wider not needed: |a-b| fits)
        uint16x8_t diff = vabdq_u16(cur, prev);
        uint16x8_t agree = vcltq_u16(diff, vdelta);  // diff < delta_z

        // filtered = alpha*cur + (1-alpha)*prev  (FP, 8 lanes via two halves)
        uint32x4_t cl = vmovl_u16(vget_low_u16(cur));
        uint32x4_t ch = vmovl_u16(vget_high_u16(cur));
        uint32x4_t pl = vmovl_u16(vget_low_u16(prev));
        uint32x4_t ph = vmovl_u16(vget_high_u16(prev));
        float32x4_t fl = vaddq_f32(vmulq_f32(valpha, vcvtq_f32_u32(cl)),
                                   vmulq_f32(voma, vcvtq_f32_u32(pl)));
        float32x4_t fh = vaddq_f32(vmulq_f32(valpha, vcvtq_f32_u32(ch)),
                                   vmulq_f32(voma, vcvtq_f32_u32(ph)));
        uint16x8_t filtered = vcombine_u16(vmovn_u32(vcvtq_u32_f32(fl)),
                                           vmovn_u32(vcvtq_u32_f32(fh)));

        // --- decide new frame value ---
        // case cur && prev && agree  -> filtered
        // case cur && !prev          -> cur (frame unchanged: still cur)
        // case cur && prev && !agree -> cur (frame unchanged)
        // case !cur && prev && credible -> prev
        // case !cur && (!prev||!cred)-> cur (==0, unchanged)
        uint16x8_t both = vandq_u16(cur_nz, prev_nz);
        uint16x8_t do_filter = vandq_u16(both, agree);
        uint16x8_t new_frame = vbslq_u16(do_filter, filtered, cur);

        // hole-fill: cur==0 && prev!=0 && (persistence_map[hist] & mask)
        // persistence_map is a 256-entry LUT -> gather per-lane (scalar gather,
        // 8 small loads; cheap, stays correct & bit-identical).
        uint8_t htmp[8]; vst1_u8(htmp, hist);
        uint8_t cred[8];
        for (int k = 0; k < 8; ++k) cred[k] = (persistence_map[htmp[k]] & mask) ? 0xFF : 0;
        uint8x8_t credb = vld1_u8(cred);
        // widen 0xFF->0x00FF then expand to a full 0xFFFF lane mask, so the
        // bitwise vbslq below selects whole 16-bit values, not just low bytes.
        uint16x8_t credible = vtstq_u16(vmovl_u8(credb), vdupq_n_u16(0xFFFF));
        uint16x8_t hole = vandq_u16(vmvnq_u16(cur_nz), prev_nz);
        hole = vandq_u16(hole, credible);
        new_frame = vbslq_u16(hole, prev, new_frame);
        vst1q_u16(frame + i, new_frame);

        // --- new last value ---
        // cur && !prev            -> cur
        // cur && prev && agree     -> filtered
        // cur && prev && !agree    -> cur
        // !cur                     -> unchanged (prev)
        uint16x8_t last_when_cur = vbslq_u16(do_filter, filtered, cur);
        uint16x8_t new_last = vbslq_u16(cur_nz, last_when_cur, prev);
        vst1q_u16(last + i, new_last);

        // --- history update (8-bit lanes) ---
        // cur && !prev          -> mask
        // cur && prev && agree  -> hist | mask
        // cur && prev && !agree -> mask
        // !cur                  -> hist & ~mask
        uint8x8_t cur_nz8 = vmovn_u16(cur_nz);
        uint8x8_t agree8 = vmovn_u16(agree);
        uint8x8_t prev_nz8 = vmovn_u16(prev_nz);
        uint8x8_t hist_or = vorr_u8(hist, vmask8);
        uint8x8_t hist_and = vand_u8(hist, vnmask8);
        // when cur: if (prev && agree) -> hist|mask else -> mask
        uint8x8_t when_cur = vbsl_u8(vand_u8(prev_nz8, agree8), hist_or, vmask8);
        uint8x8_t new_hist = vbsl_u8(cur_nz8, when_cur, hist_and);
        vst1_u8(history + i, new_hist);
    }
    // scalar tail
    float oma = one_minus_alpha;
    for (; i < n; i++) {
        uint16_t cur = frame[i], prev = last[i];
        if (cur) {
            if (!prev) { last[i] = cur; history[i] = mask; }
            else {
                uint16_t d = (uint16_t)std::fabs((float)cur - (float)prev);
                if (d < delta_z) {
                    history[i] |= mask;
                    uint16_t r = (uint16_t)(alpha * cur + oma * prev);
                    frame[i] = r; last[i] = r;
                } else { last[i] = cur; history[i] = mask; }
            }
        } else {
            if (prev) { if (persistence_map[history[i]] & mask) frame[i] = prev; }
            history[i] &= (uint8_t)~mask;
        }
    }
}

#ifdef _OPENMP
__attribute__((noinline))
void neon_omp(uint16_t* frame, uint16_t* last, uint8_t* history,
              const uint8_t* persistence_map, int w, int h, float alpha,
              uint16_t delta_z, uint8_t mask) {
    #pragma omp parallel for schedule(static)
    for (int y = 0; y < h; ++y) {
        size_t off = size_t(y) * w;
        neon(frame + off, last + off, history + off, persistence_map, w,
             alpha, delta_z, mask);
    }
}
#endif
} // namespace temporal

// ===========================================================================
// FILTER 3: DISPARITY transform (src/proc/disparity-transform.h convert<>)
//   depth->disparity: out = isnormal(in) ? factor/in + round : 0
//   Z16 in (uint16), DISPARITY32 out (float), round = 0 for integer input.
// ===========================================================================
namespace disparity {

// (a) scalar -- verbatim from disparity-transform.h convert<uint16_t,float>
__attribute__((noinline))
void scalar(const uint16_t* in, float* out, int n, float factor) {
    const float round = 0.f;  // integer input path
    for (int i = 0; i < n; i++) {
        float input = (float)in[i];
        if (std::isnormal(input))
            out[i] = (factor / input) + round;
        else
            out[i] = 0.f;
    }
}

// (c) hand-NEON. true IEEE divide (vdivq_f32), NOT reciprocal-estimate, so output
// is bit-identical. isnormal(float-from-uint16) is true iff value != 0 (uint16
// cast yields 0 or a normal float, never subnormal/inf/nan).
__attribute__((noinline))
void neon(const uint16_t* in, float* out, int n, float factor) {
    int i = 0;
    float32x4_t vf = vdupq_n_f32(factor);
    float32x4_t vz = vdupq_n_f32(0.f);
    for (; i + 4 <= n; i += 4) {
        uint16x4_t d = vld1_u16(in + i);
        uint32x4_t d32 = vmovl_u16(d);
        float32x4_t fin = vcvtq_f32_u32(d32);
        uint32x4_t nz = vtstq_u32(d32, d32);          // input != 0  <=> isnormal
        float32x4_t q = vdivq_f32(vf, fin);           // true divide
        vst1q_f32(out + i, vbslq_f32(nz, q, vz));
    }
    for (; i < n; i++) {
        float input = (float)in[i];
        out[i] = std::isnormal(input) ? (factor / input) : 0.f;
    }
}

#ifdef _OPENMP
__attribute__((noinline))
void neon_omp(const uint16_t* in, float* out, int w, int h, float factor) {
    #pragma omp parallel for schedule(static)
    for (int y = 0; y < h; ++y)
        neon(in + size_t(y) * w, out + size_t(y) * w, w, factor);
}
#endif
} // namespace disparity

// ===========================================================================
// FILTER 4: SPATIAL horizontal recursive IIR (src/proc/spatial-filter.cpp)
//   NEON-IMPOSSIBLE within a row (serial dependency along scan). Rows are
//   independent -> OpenMP-across-rows is the only lever. We bench scalar vs
//   scalar+OpenMP (no NEON variant -- documenting why).
// ===========================================================================
namespace spatial {

// Faithful, strict-aliasing-clean equivalent of the SDK's `*(int*)&f > 0`
// validity test (sign bit clear and not +0.0). memcpy is the portable type-pun.
static inline bool bits_positive(float f) {
    int32_t bits;
    std::memcpy(&bits, &f, sizeof(bits));
    return bits > 0;
}

// One recursive forward+backward IIR pass along a single line of `count`
// elements with the given `stride` (stride=1 -> horizontal row;
// stride=width -> vertical column). Verbatim structure from the SDK's
// recursive_filter_{horizontal,vertical}_fp inner body.
static inline void recursive_line(float* base, int count, int stride,
                                  float alpha, float deltaZ) {
    // forward
    float* p = base;
    float state = p[0];
    float prev = state;
    int u = count - 1;
    p += stride;
    float innov = *p;
    bool valid = bits_positive(prev);
    while (u > 0) {
        if (bits_positive(innov)) {
            if (valid) {
                float delta = prev - innov;
                if (delta < deltaZ && delta > -deltaZ)
                    *p = state = innov * alpha + state * (1.0f - alpha);
                else state = innov;
            } else { prev = state = innov; valid = true; }
        } else valid = false;
        u--; prev = innov; p += stride; innov = *p;
    }
    // backward
    p = base + size_t(count - 2) * stride;
    prev = state = p[stride];
    u = count - 1;
    innov = *p;
    valid = bits_positive(prev);
    while (u > 0) {
        if (bits_positive(innov)) {
            if (valid) {
                float delta = prev - innov;
                if (delta < deltaZ && delta > -deltaZ)
                    *p = state = innov * alpha + state * (1.0f - alpha);
                else state = innov;
            } else { prev = state = innov; valid = true; }
        } else valid = false;
        u--; prev = innov; p -= stride; innov = *p;
    }
}

// (a) scalar -- one full separable pass: horizontal (rows) then vertical
// (columns). This matches the real filter's per-iteration cost; the SDK runs
// this 1-5 times depending on the magnitude option (default 2).
__attribute__((noinline))
void scalar_hv(float* image, int width, int height, float alpha, float deltaZ) {
    for (int v = 0; v < height; ++v)              // horizontal rows
        recursive_line(image + size_t(v) * width, width, 1, alpha, deltaZ);
    for (int u = 0; u < width; ++u)               // vertical columns
        recursive_line(image + u, height, width, alpha, deltaZ);
}

#ifdef _OPENMP
// OpenMP across rows (H pass) and across columns (V pass). Each line is an
// independent recursive chain, so the outer loop parallelizes cleanly.
__attribute__((noinline))
void scalar_hv_omp(float* image, int width, int height, float alpha, float deltaZ) {
    #pragma omp parallel for schedule(static)
    for (int v = 0; v < height; ++v)
        recursive_line(image + size_t(v) * width, width, 1, alpha, deltaZ);
    #pragma omp parallel for schedule(static)
    for (int u = 0; u < width; ++u)
        recursive_line(image + u, height, width, alpha, deltaZ);
}
#endif
} // namespace spatial

// ===========================================================================
// FILTER 5: DECIMATION depth, scale=2 median path (src/proc/decimation-filter.cpp
//   decimate_depth). NEON is NOT applicable (data-dependent compaction of
//   non-zero pixels + median selection network), but OUTPUT ROWS ARE INDEPENDENT
//   -> OpenMP across output rows is valid. We bench scalar vs scalar+OMP.
// ===========================================================================
namespace decimation {

#define PIX_SWAP(a,b) { uint16_t t=(a);(a)=(b);(b)=t; }
#define PIX_SORT(a,b) { if ((a)>(b)) PIX_SWAP((a),(b)); }
#define PIX_MIN(a,b)  (((a)>(b)) ? (b) : (a))

static inline uint16_t med3(uint16_t* p) {
    PIX_SORT(p[0],p[1]); PIX_SORT(p[1],p[2]); PIX_SORT(p[0],p[1]); return p[1];
}
static inline uint16_t med4(uint16_t* p) {
    PIX_SORT(p[0],p[1]); PIX_SORT(p[2],p[3]); PIX_SORT(p[0],p[2]);
    PIX_SORT(p[1],p[3]); return PIX_MIN(p[1],p[2]);
}

// Process one band of `scale` input rows -> one output row of `real_width`
// median pixels, then zero-pad to `padded_width`. Verbatim logic from
// decimate_depth scale==2/3 branch (kernel <= 4 for scale 2).
static inline void decimate_row(const uint16_t* in_band, uint16_t* out_row,
                                int width_in, int real_width, int padded_width,
                                int scale) {
    uint16_t wk[9];
    for (int i = 0, chunk = 0; i < real_width; ++i, chunk += scale) {
        int ks = 0;
        for (int n = 0; n < scale; ++n) {
            const uint16_t* p = in_band + size_t(n) * width_in + chunk;
            for (int m = 0; m < scale; ++m)
                if (p[m]) wk[ks++] = p[m];
        }
        uint16_t v;
        switch (ks) {
            case 0: v = 0; break;
            case 1: v = wk[0]; break;
            case 2: v = PIX_MIN(wk[0], wk[1]); break;
            case 3: v = med3(wk); break;
            default: v = med4(wk); break;   // scale==2 -> max kernel 4
        }
        out_row[i] = v;
    }
    for (int i = real_width; i < padded_width; ++i) out_row[i] = 0;
}

// (a) scalar -- full frame, sequential over output rows.
__attribute__((noinline))
void scalar(const uint16_t* in, uint16_t* out, int width_in, int real_height,
            int real_width, int padded_width, int scale) {
    for (int j = 0; j < real_height; ++j) {
        const uint16_t* band = in + size_t(j) * scale * width_in;
        decimate_row(band, out + size_t(j) * padded_width, width_in,
                     real_width, padded_width, scale);
    }
}

#ifdef _OPENMP
// OpenMP across output rows: each band -> one output row, fully independent.
__attribute__((noinline))
void scalar_omp(const uint16_t* in, uint16_t* out, int width_in, int real_height,
                int real_width, int padded_width, int scale) {
    #pragma omp parallel for schedule(static)
    for (int j = 0; j < real_height; ++j) {
        const uint16_t* band = in + size_t(j) * scale * width_in;
        decimate_row(band, out + size_t(j) * padded_width, width_in,
                     real_width, padded_width, scale);
    }
}
#endif
#undef PIX_SWAP
#undef PIX_SORT
#undef PIX_MIN
} // namespace decimation

// ---------------------------------------------------------------------------
// Verification helpers
// ---------------------------------------------------------------------------
template <typename T>
bool eq_exact(const std::vector<T>& a, const std::vector<T>& b, const char* tag) {
    if (a.size() != b.size()) { printf("  [FAIL %s] size mismatch\n", tag); return false; }
    for (size_t i = 0; i < a.size(); ++i)
        if (a[i] != b[i]) {
            printf("  [FAIL %s] idx %zu: %.6g != %.6g\n", tag, i,
                   double(a[i]), double(b[i]));
            return false;
        }
    return true;
}

// ---------------------------------------------------------------------------
// Per-filter benchmark drivers
// ---------------------------------------------------------------------------
struct Res { int w, h; };

void bench_threshold(int w, int h, int warmup, int iters) {
    size_t n = size_t(w) * h;
    auto in = make_depth_z16(w, h, 1, 0.15f);
    std::vector<uint16_t> out_s(n), out_n(n);
    float du = DEPTH_UNITS, mn = 0.5f, mx = 4.0f;

    threshold::scalar(in.data(), out_s.data(), int(n), du, mn, mx);
    threshold::neon(in.data(), out_n.data(), int(n), du, mn, mx);
    if (!eq_exact(out_s, out_n, "threshold neon")) std::exit(2);
#ifdef _OPENMP
    std::vector<uint16_t> out_o(n);
    threshold::neon_omp(in.data(), out_o.data(), w, h, du, mn, mx);
    if (!eq_exact(out_s, out_o, "threshold neon+omp")) std::exit(2);
#endif
    size_t bytes = n * sizeof(uint16_t) * 2;  // read+write
    print_row("threshold", "scalar/autovec",  w, h,
        time_kernel([&]{ threshold::scalar(in.data(), out_s.data(), int(n), du, mn, mx);}, warmup, iters, n, bytes));
    print_row("threshold", "neon",  w, h,
        time_kernel([&]{ threshold::neon(in.data(), out_n.data(), int(n), du, mn, mx);}, warmup, iters, n, bytes));
#ifdef _OPENMP
    print_row("threshold", "neon+omp",  w, h,
        time_kernel([&]{ threshold::neon_omp(in.data(), out_o.data(), w, h, du, mn, mx);}, warmup, iters, n, bytes));
#endif
}

void bench_disparity(int w, int h, int warmup, int iters) {
    size_t n = size_t(w) * h;
    auto in = make_depth_z16(w, h, 2, 0.15f);
    std::vector<float> out_s(n), out_n(n);
    float factor = 67000.f;  // representative baseline*focal*fractions/units

    disparity::scalar(in.data(), out_s.data(), int(n), factor);
    disparity::neon(in.data(), out_n.data(), int(n), factor);
    if (!eq_exact(out_s, out_n, "disparity neon")) std::exit(2);
#ifdef _OPENMP
    std::vector<float> out_o(n);
    disparity::neon_omp(in.data(), out_o.data(), w, h, factor);
    if (!eq_exact(out_s, out_o, "disparity neon+omp")) std::exit(2);
#endif
    size_t bytes = n * sizeof(uint16_t) + n * sizeof(float);
    print_row("disparity", "scalar/autovec",  w, h,
        time_kernel([&]{ disparity::scalar(in.data(), out_s.data(), int(n), factor);}, warmup, iters, n, bytes));
    print_row("disparity", "neon",  w, h,
        time_kernel([&]{ disparity::neon(in.data(), out_n.data(), int(n), factor);}, warmup, iters, n, bytes));
#ifdef _OPENMP
    print_row("disparity", "neon+omp",  w, h,
        time_kernel([&]{ disparity::neon_omp(in.data(), out_o.data(), w, h, factor);}, warmup, iters, n, bytes));
#endif
}

void bench_temporal(int w, int h, int warmup, int iters) {
    size_t n = size_t(w) * h;
    // Build a stable persistence LUT (persistence_param==3 semantics).
    std::vector<uint8_t> pmap(256, 0);
    {
        std::vector<uint8_t> base(256, 0);
        for (int i = 0; i < 256; ++i) {
            int sum = !!(i&128)+!!(i&64)+!!(i&32)+!!(i&16);
            if (sum >= 2) base[i] = 1;
        }
        std::vector<uint8_t> cred(256, 0);
        for (int phase = 0; phase < 8; ++phase) {
            uint8_t mask = 1 << phase;
            for (int i = 0; i < 256; ++i) {
                uint8_t pos = (uint8_t)((i << (8 - phase)) | (i >> phase));
                if (base[pos]) cred[i] |= mask;
            }
        }
        pmap = cred;
    }
    float alpha = 0.4f; uint16_t delta_z = 20; uint8_t mask = 1 << 3;

    // populate prior frame + history so pass-2 (hole/persistence) actually runs
    auto frame0 = make_depth_z16(w, h, 3, 0.15f);
    auto last0  = make_depth_z16(w, h, 7, 0.15f);
    std::vector<uint8_t> hist0(n);
    { std::mt19937 r(11); for (auto& x : hist0) x = uint8_t(r() & 0xFF); }

    // scalar reference (operates in place -> work on copies)
    auto fs = frame0, ls = last0; auto hs = hist0;
    temporal::scalar(fs.data(), ls.data(), hs.data(), pmap.data(), int(n), alpha, delta_z, mask);
    auto fn = frame0, ln = last0; auto hn = hist0;
    temporal::neon(fn.data(), ln.data(), hn.data(), pmap.data(), int(n), alpha, delta_z, mask);
    if (!eq_exact(fs, fn, "temporal frame") ||
        !eq_exact(ls, ln, "temporal last") ||
        !eq_exact(hs, hn, "temporal history")) std::exit(2);
#ifdef _OPENMP
    auto fo = frame0, lo = last0; auto ho = hist0;
    temporal::neon_omp(fo.data(), lo.data(), ho.data(), pmap.data(), w, h, alpha, delta_z, mask);
    if (!eq_exact(fs, fo, "temporal frame omp") ||
        !eq_exact(ls, lo, "temporal last omp") ||
        !eq_exact(hs, ho, "temporal history omp")) std::exit(2);
#endif
    size_t bytes = n * (2*sizeof(uint16_t) + sizeof(uint8_t)) * 2;  // frame+last+hist rw
    // Temporal mutates in place, so each timed iter must reset the 3 buffers.
    // That reset (~3 vector copies) would otherwise inflate every variant's ms
    // and compress the speedup. We measure the reset-only cost and SUBTRACT it
    // so the reported numbers are NET KERNEL time (the honest per-frame cost).
    auto wf = frame0, wl = last0; auto wh = hist0;
    Stats reset = time_kernel([&]{ wf=frame0; wl=last0; wh=hist0; }, warmup, iters, n, bytes);
    printf("# temporal reset-only overhead (subtracted below): %.4f ms mean @ %dx%d\n",
           reset.mean_ms, w, h);
    auto net = [&](Stats s) {
        Stats o = s;
        o.mean_ms = std::max(1e-6, s.mean_ms - reset.mean_ms);
        o.p50_ms  = std::max(1e-6, s.p50_ms  - reset.mean_ms);
        o.p95_ms  = std::max(1e-6, s.p95_ms  - reset.mean_ms);
        o.mpix_s  = (n / (o.mean_ms / 1000.0)) / 1e6;
        o.gb_s    = (bytes / (o.mean_ms / 1000.0)) / 1e9;
        return o;
    };
    print_row("temporal", "scalar/autovec",  w, h, net(
        time_kernel([&]{ wf=frame0; wl=last0; wh=hist0;
            temporal::scalar(wf.data(), wl.data(), wh.data(), pmap.data(), int(n), alpha, delta_z, mask);}, warmup, iters, n, bytes)));
    print_row("temporal", "neon",  w, h, net(
        time_kernel([&]{ wf=frame0; wl=last0; wh=hist0;
            temporal::neon(wf.data(), wl.data(), wh.data(), pmap.data(), int(n), alpha, delta_z, mask);}, warmup, iters, n, bytes)));
#ifdef _OPENMP
    print_row("temporal", "neon+omp",  w, h, net(
        time_kernel([&]{ wf=frame0; wl=last0; wh=hist0;
            temporal::neon_omp(wf.data(), wl.data(), wh.data(), pmap.data(), w, h, alpha, delta_z, mask);}, warmup, iters, n, bytes)));
#endif
}

void bench_spatial(int w, int h, int warmup, int iters) {
    size_t n = size_t(w) * h;
    // disparity-domain float image (spatial runs on disparity)
    auto base = make_depth_f32(w, h, 5, 0.15f);
    float alpha = 0.5f, deltaZ = 20.f;

    auto ref = base;
    spatial::scalar_hv(ref.data(), w, h, alpha, deltaZ);
#ifdef _OPENMP
    auto omp = base;
    spatial::scalar_hv_omp(omp.data(), w, h, alpha, deltaZ);
    if (!eq_exact(ref, omp, "spatial hv omp")) std::exit(2);
#endif
    size_t bytes = n * sizeof(float) * 4;  // H rw + V rw
    auto work = base;
    // In-place mutation -> subtract the per-iter buffer-reset overhead, same as
    // temporal, so reported numbers are net kernel time.
    Stats reset = time_kernel([&]{ work=base; }, warmup, iters, n, bytes);
    printf("# spatial reset-only overhead (subtracted below): %.4f ms mean @ %dx%d\n",
           reset.mean_ms, w, h);
    auto net = [&](Stats s) {
        Stats o = s;
        o.mean_ms = std::max(1e-6, s.mean_ms - reset.mean_ms);
        o.p50_ms  = std::max(1e-6, s.p50_ms  - reset.mean_ms);
        o.p95_ms  = std::max(1e-6, s.p95_ms  - reset.mean_ms);
        o.mpix_s  = (n / (o.mean_ms / 1000.0)) / 1e6;
        o.gb_s    = (bytes / (o.mean_ms / 1000.0)) / 1e9;
        return o;
    };
    print_row("spatial-hv", "scalar/autovec",  w, h, net(
        time_kernel([&]{ work=base; spatial::scalar_hv(work.data(), w, h, alpha, deltaZ);}, warmup, iters, n, bytes)));
#ifdef _OPENMP
    print_row("spatial-hv", "scalar+omp",  w, h, net(
        time_kernel([&]{ work=base; spatial::scalar_hv_omp(work.data(), w, h, alpha, deltaZ);}, warmup, iters, n, bytes)));
#endif
}

void bench_decimation(int w, int h, int warmup, int iters) {
    const int scale = 2;                       // default decimation factor
    int real_w = w / scale, real_h = h / scale;
    int padded_w = ((real_w + 3) / 4) * 4;     // SDK rounds width up to mult of 4
    auto in = make_depth_z16(w, h, 9, 0.15f);
    size_t out_n = size_t(padded_w) * real_h;
    std::vector<uint16_t> out_s(out_n, 0xABCD), out_o(out_n, 0xABCD);

    decimation::scalar(in.data(), out_s.data(), w, real_h, real_w, padded_w, scale);
#ifdef _OPENMP
    decimation::scalar_omp(in.data(), out_o.data(), w, real_h, real_w, padded_w, scale);
    if (!eq_exact(out_s, out_o, "decimation omp")) std::exit(2);
#endif
    // work = full-res read + decimated write; report on INPUT pixels (work scale)
    size_t in_pix = size_t(w) * h;
    size_t bytes = in_pix * sizeof(uint16_t) + out_n * sizeof(uint16_t);
    print_row("decimation", "scalar/autovec",  w, h,
        time_kernel([&]{ decimation::scalar(in.data(), out_s.data(), w, real_h, real_w, padded_w, scale);},
                    warmup, iters, in_pix, bytes));
#ifdef _OPENMP
    print_row("decimation", "scalar+omp",  w, h,
        time_kernel([&]{ decimation::scalar_omp(in.data(), out_o.data(), w, real_h, real_w, padded_w, scale);},
                    warmup, iters, in_pix, bytes));
#endif
}

} // namespace

int main(int argc, char** argv) {
    int warmup = 30, iters = 300;
    if (argc > 1) iters = std::atoi(argv[1]);
    if (argc > 2) warmup = std::atoi(argv[2]);

#ifdef _OPENMP
    printf("# OpenMP enabled, max_threads=%d\n", omp_get_max_threads());
#else
    printf("# OpenMP disabled (single-thread build)\n");
#endif
    printf("# warmup=%d iters=%d  depth_units=%.3f invalid_frac=0.15\n", warmup, iters, DEPTH_UNITS);
    printf("%-12s %-14s %-11s  %8s  %8s  %8s  %9s  %7s\n",
           "filter", "variant", "res", "mean_ms", "p50_ms", "p95_ms", "Mpix/s", "GB/s");
    printf("%s\n", std::string(86, '-').c_str());

    const Res sizes[] = {{848, 480}, {1280, 720}};
    for (auto s : sizes) {
        bench_threshold(s.w, s.h, warmup, iters);
        bench_disparity(s.w, s.h, warmup, iters);
        bench_temporal(s.w, s.h, warmup, iters);
        bench_decimation(s.w, s.h, warmup, iters);
        bench_spatial(s.w, s.h, warmup, iters);
        printf("%s\n", std::string(86, '-').c_str());
    }
    printf("# All variants verified bit-identical to scalar reference.\n");
    return 0;
}
