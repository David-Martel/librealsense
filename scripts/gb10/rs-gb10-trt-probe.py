#!/usr/bin/env python3
"""TensorRT CAPABILITY PROBE for GB10 — NOT a librealsense integration.

The core RealSense pipeline (convert/align/pointcloud) is deterministic geometry — there is no neural
network in it, so TensorRT has nothing to accelerate there. TensorRT becomes relevant only if a LEARNED
stage is added: e.g. a CNN depth denoise / hole-completion / super-resolution replacing the scalar
spatial/temporal/hole-filling filters (which today have zero acceleration), or a downstream perception
model (vigil's). This probe answers the only question that's measurable today: *does a small learned
depth filter fit the per-frame budget on GB10?* It synthesizes a representative small CNN (a few 3x3
convs at the depth resolution) and times it with trtexec (FP16). It builds NOTHING into the SDK.

Output: GPU compute latency at 848x480, vs the 33.3 ms (30 fps) / 16.7 ms (60 fps) frame budgets.

Usage: rs-gb10-trt-probe.py [--h 480 --w 848 --channels 16 --layers 5 --fp16/--int8]
"""
import argparse
import os
import re
import subprocess
import sys
import tempfile

import numpy as np


def build_onnx(path, h, w, c, layers):
    import onnx
    from onnx import TensorProto, helper, numpy_helper
    np.random.seed(0)
    nodes, inits = [], []
    cur, cur_c = "input", 1
    for i in range(layers):
        out_c = 1 if i == layers - 1 else c
        wname = f"w{i}"
        wt = (np.random.randn(out_c, cur_c, 3, 3) * 0.05).astype(np.float32)
        inits.append(numpy_helper.from_array(wt, wname))
        conv_out = f"conv{i}"
        nodes.append(helper.make_node("Conv", [cur, wname], [conv_out],
                                      kernel_shape=[3, 3], pads=[1, 1, 1, 1]))
        if i < layers - 1:
            relu_out = f"relu{i}"
            nodes.append(helper.make_node("Relu", [conv_out], [relu_out]))
            cur = relu_out
        else:
            cur = conv_out
        cur_c = out_c
    inp = helper.make_tensor_value_info("input", TensorProto.FLOAT, [1, 1, h, w])
    out = helper.make_tensor_value_info(cur, TensorProto.FLOAT, [1, 1, h, w])
    graph = helper.make_graph(nodes, "depth_filter_probe", [inp], [out], inits)
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 17)])
    onnx.save(model, path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--h", type=int, default=480)
    ap.add_argument("--w", type=int, default=848)
    ap.add_argument("--channels", type=int, default=16)
    ap.add_argument("--layers", type=int, default=5)
    ap.add_argument("--int8", action="store_true", help="probe INT8 instead of FP16")
    args = ap.parse_args()

    trtexec = os.environ.get("LRS_TRTEXEC", "trtexec")
    onnx_path = os.path.join(tempfile.gettempdir(), "gb10_depth_filter_probe.onnx")
    print(f"Synthesizing depth-filter CNN: {args.layers} convs x {args.channels}ch @ {args.w}x{args.h} ...")
    build_onnx(onnx_path, args.h, args.w, args.channels, args.layers)

    # TensorRT 11 imports ONNX as STRONGLY-TYPED by default, which rejects --fp16/--int8 (precision is
    # taken from the ONNX dtypes). So we run the engine as-built (FP32) and report that as a CONSERVATIVE
    # upper bound; FP16/INT8 would be ~2x/~4x faster. This keeps the probe robust and honest.
    cmd = [trtexec, f"--onnx={onnx_path}", "--iterations=100", "--warmUp=200", "--avgRuns=100"]
    prec_label = "FP32 (strongly-typed ONNX; FP16/INT8 would be faster)"
    print("Running:", " ".join(cmd))
    out = subprocess.run(cmd, capture_output=True, text=True)
    txt = out.stdout + out.stderr
    # parse "GPU Compute Time: ... median = X ms ... mean = Y ms"
    med = re.search(r"GPU Compute Time:.*?median\s*=\s*([\d.]+)\s*ms", txt, re.S)
    mean = re.search(r"GPU Compute Time:.*?mean\s*=\s*([\d.]+)\s*ms", txt, re.S)
    thr = re.search(r"Throughput:\s*([\d.]+)\s*qps", txt)
    if not med:
        print("Could not parse trtexec output. Tail:")
        print("\n".join(txt.splitlines()[-25:]))
        return 1
    m = float(med.group(1))
    print("\n================  TensorRT capability probe (GB10)  ================")
    print(f"  model      : {args.layers}-conv depth filter, {args.channels}ch, {args.w}x{args.h}, {prec_label}")
    print(f"  GPU compute: median {m:.3f} ms  (mean {mean.group(1) if mean else '?'} ms)"
          f"  throughput {thr.group(1) if thr else '?'} qps")
    print(f"  fits 30 fps (33.3 ms)? {'YES' if m < 33.3 else 'NO'}   "
          f"fits 60 fps (16.7 ms)? {'YES' if m < 16.7 else 'NO'}")
    print(f"  headroom @30fps: {33.3 / m:.1f}x   (how many such filters fit one 30 fps frame)")
    print("  NOTE: capability probe only — measures GB10 NN headroom for a FUTURE learned depth-filter")
    print("        stage; nothing is integrated into librealsense.")
    print("===================================================================")
    return 0


if __name__ == "__main__":
    sys.exit(main())
