/* License: Apache 2.0. See LICENSE file in root directory.
Copyright(c) 2017 RealSense, Inc. All Rights Reserved. */

// Python bindings for the GPU (OpenGL/GLSL) processing module rs2::gl
// (librealsense2-gl). Only compiled/linked when the realsense2-gl library is
// built — gated by BUILD_GLSL_EXTENSIONS in wrappers/python/CMakeLists.txt and
// the PYRS_WITH_GL compile definition; pyrealsense2 in a non-GLSL build simply
// lacks the `gl` submodule.
//
// Keep-on-GPU usage from Python (the whole point is NO device->host copy):
//   1. Make a GL context current (e.g. glfw.make_context_current(window)).
//   2. rs.gl.init_processing(use_glsl=True)        # AFTER a context is current
//   3. col = rs.gl.colorizer()
//   4. out = col.process(depth_frame)              # stays in a GL texture
//   5. gf  = rs.gl.gpu_frame(out)                  # falsy if it fell back to CPU
//      assert gf and gf.get_texture_id() != 0      # <-- the GPU discriminator
//   6. ... draw the texture in your GL window ...
//   7. rs.gl.shutdown_processing()                 # WHILE the context is current
//                                                  # (before destroying it; else SIGSEGV)
//
// WITHOUT a current GL context, init_processing()/process() SILENTLY fall back to
// CPU: process() still returns a frame, but gpu_frame(out) is falsy and
// get_texture_id() is 0. Always assert on the gpu_frame, never on "did it return".

#include "pyrealsense2.h"
#include <librealsense2-gl/rs_processing_gl.hpp>

void init_gl(py::module &m) {
    auto gl = m.def_submodule(
        "gl",
        "GPU-accelerated (OpenGL/GLSL) processing — rs2::gl. A GL context must be CURRENT before "
        "init_processing(); output frames stay resident in GL textures (no device->host readback).");

    gl.def("init_processing",
           [](bool use_glsl) { rs2::gl::init_processing(use_glsl); },
           "use_glsl"_a = true,
           "Initialize GL processing on the CURRENTLY-current GL context (make a context current "
           "first, e.g. glfw.make_context_current(win)). Without a current context the GL blocks "
           "silently fall back to CPU.");

    gl.def("shutdown_processing",
           []() { rs2::gl::shutdown_processing(); },
           "Shut down GL processing. Call WHILE the GL context is still current, before destroying "
           "it (otherwise static GL teardown can SIGSEGV at exit).");

    // gpu_frame: recover the GL texture id from a processed frame, and tell whether the frame is
    // actually GPU-resident. Construct from a processed frame; it evaluates False (__bool__,
    // inherited from frame) when the frame is NOT GPU-backed (CPU fallback).
    py::class_<rs2::gl::gpu_frame, rs2::frame>(gl, "gpu_frame",
        "A frame whose pixel data lives in a GL texture. Build it from a processed frame; it is "
        "falsy (bool(gf) == False) when the frame is not GPU-resident (i.e. CPU fallback).")
        .def(py::init<rs2::frame>(), "f"_a)
        .def("get_texture_id", &rs2::gl::gpu_frame::get_texture_id, "id"_a = 0u,
             "OpenGL texture name for plane `id` (0 = first / only). Nonzero only for a real GPU frame.");

    // gl::colorizer — Depth->RGB on the GPU; output stays in a GL texture (wrap in gpu_frame).
    py::class_<rs2::gl::colorizer, rs2::colorizer>(gl, "colorizer",
        "GPU colorizer (Depth->RGB, histogram-equalized) — the GPU counterpart of rs.colorizer. "
        "process()/colorize() output stays in a GL texture; wrap it in gl.gpu_frame to get the texture id.")
        .def(py::init<>())
        .def("process", [](rs2::gl::colorizer &c, rs2::frame f) { return c.process(f); }, "frame"_a,
             "Colorize a depth frame on the GPU; the result stays in a GL texture (wrap in gl.gpu_frame).");

    // gl::pointcloud — GPU pointcloud; calculate() output stays GPU-resident.
    py::class_<rs2::gl::pointcloud, rs2::pointcloud>(gl, "pointcloud",
        "GPU pointcloud — the GPU counterpart of rs.pointcloud. calculate() output stays GPU-resident.")
        .def(py::init<>());

    // Convenience discriminator: is this frame backed by a GL texture?
    gl.def("is_gpu_frame",
           [](rs2::frame f) { return static_cast<bool>(rs2::gl::gpu_frame(f)); }, "frame"_a,
           "True iff the frame is backed by a GL texture — the keep-on-GPU discriminator "
           "(False on CPU fallback, e.g. when no GL context was current at init_processing()).");
}
