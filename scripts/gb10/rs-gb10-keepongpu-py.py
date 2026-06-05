#!/usr/bin/env python3
"""GB10 keep-on-GPU depth viewer/validator — the PYTHON counterpart of the C++ rs-gb10-keepongpu-viewer,
built on the new `rs.gl` pybind binding (pyrealsense2 built with realsense2-gl / BUILD_GLSL_EXTENSIONS).

The whole point is NO device->host copy: depth -> rs.gl.colorizer (output stays in a GL texture) ->
the texture is drawn straight to the window. The moment you np.asanyarray(frame.get_data()) or route
through cv2, the texture round-trips host memory and the keep-on-GPU win is gone — so this is a separate
GL render path, not a drop-in to the cv2-based test-bed loop.

Modes (camera path requires an explicit flag so it never opens the device by accident):
  --validate            Headless (hidden GL window) proof that the GPU path is REAL, not a CPU fallback:
                        colorize one depth frame on the GPU and assert is_gpu_frame + texture_id != 0.
                        Single depth stream = safe envelope. Exit 0 = PASS.
  --view [--duration N] Visible window: live colorized depth drawn from the GL texture (no D2H), ~N s.

Requires a build with rs.gl (currently the py3.14 tree build-gb10-py314; the 3.12 HIL build has examples
off so it lacks realsense2-gl). Run under the matching venv with PYTHONPATH/LD_LIBRARY_PATH at that
build's Release dir (see `just hil-keepongpu-py`). NO numpy needed.

KEY LESSONS carried from the C++ viewer:
  * make a GL context CURRENT before rs.gl.init_processing() (else the GL blocks silently fall back to CPU);
  * call rs.gl.shutdown_processing() WHILE the context is still current, before destroying it (else SIGSEGV).
"""
import argparse
import sys
import time

import glfw
import pyrealsense2 as rs


def _require_gl():
    if not hasattr(rs, "gl"):
        sys.exit("ERROR: this pyrealsense2 has no rs.gl — build with BUILD_GLSL_EXTENSIONS + examples "
                 "(realsense2-gl), e.g. the py3.14 tree build-gb10-py314.")


def _make_context(width, height, visible, title):
    if not glfw.init():
        sys.exit("ERROR: glfw.init() failed")
    glfw.window_hint(glfw.VISIBLE, glfw.TRUE if visible else glfw.FALSE)
    glfw.window_hint(glfw.RESIZABLE, glfw.FALSE)
    win = glfw.create_window(width, height, title, None, None)
    if not win:
        glfw.terminate()
        sys.exit("ERROR: could not create a GL window/context (is DISPLAY set / reachable?)")
    glfw.make_context_current(win)          # MUST precede rs.gl.init_processing()
    glfw.swap_interval(0)
    return win


def _start_depth(w, h, fps):
    pipe = rs.pipeline()
    cfg = rs.config()
    cfg.enable_stream(rs.stream.depth, w, h, rs.format.z16, fps)
    pipe.start(cfg)
    return pipe


def cmd_validate(args):
    """Headless: prove the colorized frame is a real GL texture (not CPU fallback)."""
    _require_gl()
    win = _make_context(640, 480, visible=False, title="rs-gl-validate")
    rc, gl_inited, out = 1, False, None
    try:
        rs.gl.init_processing(True)
        gl_inited = True
        col = rs.gl.colorizer()
        pipe = _start_depth(args.width, args.height, args.fps)
        gf = None
        try:
            for _ in range(args.warmup + 5):
                fs = pipe.wait_for_frames(2000)
                d = fs.get_depth_frame()
                if not d:
                    continue
                out = col.process(d)               # GPU colorize -> GL texture
                gf = rs.gl.gpu_frame(out)
                if gf:
                    break
        finally:
            pipe.stop()
        is_gpu = bool(gf)
        tex = gf.get_texture_id(0) if is_gpu else 0
        print(f"[keepongpu-py] is_gpu_frame={is_gpu} texture_id={tex}")
        rc = 0 if (is_gpu and tex != 0) else 1
        print("[keepongpu-py] VALIDATE", "PASS (real GPU frame, GL texture)" if rc == 0
              else "FAIL (CPU fallback — no current GL context?)")
    finally:
        if gl_inited:
            rs.gl.shutdown_processing()            # context still current
        glfw.destroy_window(win)
        glfw.terminate()
    return rc


def cmd_view(args):
    """Visible window: draw the colorized-depth GL texture straight to screen (no D2H)."""
    _require_gl()
    from OpenGL.GL import (glViewport, glClear, glClearColor, glEnable, glDisable, glBindTexture,
                           glBegin, glEnd, glVertex2f, glTexCoord2f,
                           GL_COLOR_BUFFER_BIT, GL_TEXTURE_2D, GL_QUADS)
    win = _make_context(args.width, args.height, visible=True, title="GB10 keep-on-GPU (Python rs.gl)")
    rc, gl_inited = 1, False
    try:
        rs.gl.init_processing(True)
        gl_inited = True
        col = rs.gl.colorizer()
        pipe = _start_depth(args.width, args.height, args.fps)
        glClearColor(0.0, 0.0, 0.0, 1.0)
        frames, t0, last = 0, time.time(), time.time()
        try:
            while not glfw.window_should_close(win) and (time.time() - t0) < args.duration:
                fs = pipe.wait_for_frames(2000)
                d = fs.get_depth_frame()
                if not d:
                    continue
                gf = rs.gl.gpu_frame(col.process(d))     # GPU colorize, stays in texture
                tex = gf.get_texture_id(0) if gf else 0
                fbw, fbh = glfw.get_framebuffer_size(win)
                glViewport(0, 0, fbw, fbh)
                glClear(GL_COLOR_BUFFER_BIT)
                if tex:
                    glEnable(GL_TEXTURE_2D)
                    glBindTexture(GL_TEXTURE_2D, tex)
                    glBegin(GL_QUADS)                    # fullscreen quad; flip V (texture origin bottom-left)
                    glTexCoord2f(0.0, 1.0); glVertex2f(-1.0, -1.0)
                    glTexCoord2f(1.0, 1.0); glVertex2f(1.0, -1.0)
                    glTexCoord2f(1.0, 0.0); glVertex2f(1.0, 1.0)
                    glTexCoord2f(0.0, 0.0); glVertex2f(-1.0, 1.0)
                    glEnd()
                    glDisable(GL_TEXTURE_2D)
                glfw.swap_buffers(win)
                glfw.poll_events()
                frames += 1
                if time.time() - last >= 1.0:
                    print(f"[keepongpu-py] {frames/(time.time()-t0):.1f} fps  (keep-on-GPU, no D2H)")
                    last = time.time()
        finally:
            pipe.stop()
        print(f"[keepongpu-py] VIEW done: {frames} frames in {time.time()-t0:.1f}s")
        rc = 0 if frames > 0 else 1
    finally:
        if gl_inited:
            rs.gl.shutdown_processing()
        glfw.destroy_window(win)
        glfw.terminate()
    return rc


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = p.add_mutually_exclusive_group()
    g.add_argument("--validate", action="store_true", help="Headless GPU-frame proof (no on-screen window).")
    g.add_argument("--view", action="store_true", help="Visible keep-on-GPU viewer (OPERATOR; camera + display).")
    p.add_argument("--width", type=int, default=848)
    p.add_argument("--height", type=int, default=480)
    p.add_argument("--fps", type=int, default=30)
    p.add_argument("--duration", type=float, default=15.0, help="Seconds for --view (default 15).")
    p.add_argument("--warmup", type=int, default=15, help="Warmup frames before the GPU check (--validate).")
    if len(sys.argv) == 1:
        p.print_help()
        return 0
    args = p.parse_args()
    if args.validate:
        return cmd_validate(args)
    if args.view:
        return cmd_view(args)
    p.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
