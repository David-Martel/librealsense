#!/usr/bin/env bash
# License: Apache 2.0. See LICENSE file in root directory.
# Copyright(c) 2026 RealSense, Inc. All Rights Reserved.
#
# GB10 NEON+OpenMP depth-filter feasibility microbench harness.
# No camera, no SDK linkage. Synthetic depth only. Builds clean -Werror.
#
# It compiles the standalone bench in three flavors and runs each so the doc can
# report the HONEST three-way comparison:
#   1) true-scalar  : -fno-tree-vectorize  (defeats compiler autovec)
#   2) autovec      : -O3 -ftree-vectorize  == the shipping SDK aarch64 flags
#   3) neon+omp     : adds -fopenmp ; NEON intrinsics + parallel rows
#
# Shipping SDK aarch64 flags (CMake/unix_config.cmake): -O3 -ftree-vectorize
# -mstrict-align -ffp-contract=fast (no -march). We match -O3 -ftree-vectorize
# -mstrict-align and pin -ffp-contract=off across ALL flavors so scalar/NEON FMA
# fusion cannot diverge and the bit-identity assertions are meaningful.
#
# Single-thread flavors are pinned to one Cortex-X925 (perf core) to avoid
# cross-core migration polluting p95. OpenMP flavor reports thread count + binding.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="${SCRIPT_DIR}/bench_filters_neon.cpp"
OUTDIR="${SCRIPT_DIR}/.bench-build"   # disposable; not meant to be committed
mkdir -p "${OUTDIR}"
# Remove compiled binaries on exit so they are never swept into a commit.
trap 'rm -rf "${OUTDIR}"' EXIT

CXX="${CXX:-g++}"
ITERS="${ITERS:-300}"
WARMUP="${WARMUP:-30}"

# Common flags: match shipping aarch64 profile + strict warnings.
COMMON="-std=c++14 -Wall -Wextra -Werror -mstrict-align -ffp-contract=off"

# Pick a Cortex-X925 (perf, 3900MHz) core for single-thread pinning.
# On GB10 the big cores are odd indices 5-9,15-19; use 19.
PERF_CORE="${PERF_CORE:-19}"
PIN=""
if command -v taskset >/dev/null 2>&1; then
    PIN="taskset -c ${PERF_CORE}"
fi

echo "=== Compiler ==="
${CXX} --version | head -1
echo "=== Flags ==="
echo "  common : ${COMMON}"
echo "  perf-core pin : ${PIN:-<none>}"
echo

build() {
    local name="$1"; shift
    echo "--- building ${name} ---"
    ${CXX} ${COMMON} "$@" "${SRC}" -o "${OUTDIR}/bench_${name}" \
        || { echo "BUILD FAILED (${name})"; exit 1; }
}

# 1) true-scalar: defeat autovec so we can isolate the compiler's contribution.
build truescalar -O3 -fno-tree-vectorize

# 2) autovec: the actual shipping scalar path (compiler autovectorizes).
build autovec    -O3 -ftree-vectorize

# 3) neon+omp: NEON intrinsics are unconditional in source; add OpenMP.
build neonomp    -O3 -ftree-vectorize -fopenmp

echo
echo "############################################################"
echo "# RUN 1: true-scalar  (autovec OFF, pinned to X925 core ${PERF_CORE})"
echo "#   -> isolates raw scalar; not what ships, lower bound"
echo "############################################################"
${PIN} "${OUTDIR}/bench_truescalar" "${ITERS}" "${WARMUP}"

echo
echo "############################################################"
echo "# RUN 2: autovec-O3  (== SHIPPING SDK path, pinned to X925 core ${PERF_CORE})"
echo "#   -> 'scalar/autovec' rows here are the honest baseline."
echo "#   -> 'neon' rows = hand-NEON vs autovec on the SAME core."
echo "############################################################"
${PIN} "${OUTDIR}/bench_autovec" "${ITERS}" "${WARMUP}"

echo
echo "############################################################"
echo "# RUN 3: neon+omp  (OpenMP across rows)"
# Pin to a HOMOGENEOUS set of 10 Cortex-X925 perf cores (odd indices 5-9,15-19)
# so static-scheduled rows are not stranded on slower A725 little cores and
# placement is deterministic. taskset cpu-list + explicit single-cpu OMP_PLACES.
OMP_THREADS="${OMP_THREADS:-10}"
X925_LIST="${X925_LIST:-5,6,7,8,9,15,16,17,18,19}"
# OMP_PLACES=threads + a taskset cpuset of the 10 X925 cores pins each thread to
# one perf core (homogeneous, deterministic). 'threads' avoids brace-expansion
# quoting traps and binds 1 thread per HW thread within the cpuset.
OMP_PIN=""
if command -v taskset >/dev/null 2>&1; then
    OMP_PIN="taskset -c ${X925_LIST}"
fi
echo "#   OMP_NUM_THREADS=${OMP_THREADS} OMP_PROC_BIND=close OMP_PLACES=threads"
echo "#   pinned to X925 cores ${X925_LIST}"
echo "############################################################"
OMP_NUM_THREADS="${OMP_THREADS}" OMP_PROC_BIND=close OMP_PLACES=threads \
    ${OMP_PIN} "${OUTDIR}/bench_neonomp" "${ITERS}" "${WARMUP}"

echo
echo "All builds compiled clean under -Wall -Wextra -Werror. Outputs in ${OUTDIR}/"
