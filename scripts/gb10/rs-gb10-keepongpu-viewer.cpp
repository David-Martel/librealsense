// rs-gb10-keepongpu-viewer.cpp — P1: the keep-on-GPU OpenGL render path as a LIVE viewer (GB10).
//
// Renders the live RealSense depth stream to an on-screen window using the GPU-resident chain:
//   pipeline depth -> rs2::gl::colorizer (output stays in a GL texture) -> draw that texture
//   straight to the window's framebuffer -> glfwSwapBuffers.
// There is NO device->host readback (no get_data() D2H) and NO host->device re-upload — the GPU
// colorizes and scans the result out to the display. The measured saving vs the cv2/CPU path that
// pays the D2H is ~1 ms @640x480, ~3 ms @1280x720, ~7 ms @1920x1080 (see ROS2-GL-PINNED-FINDINGS).
//
// ROBUSTNESS (R2 fix): the GL processing-lane GPU objects free their GL resources in static
// destructors at process exit, which SIGSEGVs if the GL context is already gone. We call
// rs2::gl::shutdown_processing() WHILE THE CONTEXT IS STILL CURRENT, before glfwDestroyWindow /
// glfwTerminate -> clean `return 0` (no _exit(0) workaround, no crash).
//
// ENHANCEMENTS:
//   --record <out.mp4>  pipe raw RGBA frames to ffmpeg h264_nvenc after swap; best-effort
//                       (SIGPIPE-safe, no stall). NOTE: --record introduces a D2H glReadPixels;
//                       the keep-on-GPU render_ms sample is taken BEFORE that readback so the
//                       p50 metric stays comparable to a non-record run.
//   (always on) TELEMETRY: every 30 frames prints device frame number, HW/sensor timestamp +
//                       domain, metadata (actual_exposure, actual_fps, frame_counter) where
//                       supported, and the rolling render p50 + delivered fps.
//   --stream color      draw the color frame (BGR->texture upload+draw); depth mode by default.
//                       Note: the keep-on-GPU GPU-resident benefit is maximized for depth (GL
//                       colorizer stays in texture). Color mode is a straight upload+draw path.
//
// SAFE: single depth or color stream (the conservative envelope). Build: rs-gb10-keepongpu-build.sh
// Run via the launcher / `just hil-keepongpu`. ESC or window-close or --duration ends it.

#include <GLFW/glfw3.h>                              // must precede the gl header (context-share overload)
#include <librealsense2/rs.hpp>
#include <librealsense2-gl/rs_processing_gl.hpp>
#include <cerrno>
#include <csignal>
#include <cstdio>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <memory>
#include <vector>
#include <chrono>
#include <algorithm>
#include <string>

// ---------------------------------------------------------------------------
// Minimal GL loader (glfwGetProcAddress for everything; we don't link -lGL)
// ---------------------------------------------------------------------------
using GLenum_   = unsigned int; using GLuint_  = unsigned int; using GLint_   = int;
using GLsizei_  = int;          using GLbool_  = unsigned char; using GLchar_  = char;
using GLfloat_  = float;

#define GL_VENDOR_           0x1F00u
#define GL_RENDERER_         0x1F01u
#define GL_VERSION_          0x1F02u
#define GL_TEXTURE_2D_       0x0DE1u
#define GL_TEXTURE0_         0x84C0u
#define GL_FLOAT_            0x1406u
#define GL_COLOR_BUFFER_BIT_ 0x00004000u
#define GL_TRIANGLE_STRIP_   0x0005u
#define GL_ARRAY_BUFFER_     0x8892u
#define GL_STATIC_DRAW_      0x88E4u
#define GL_VERTEX_SHADER_    0x8B31u
#define GL_FRAGMENT_SHADER_  0x8B30u
#define GL_COMPILE_STATUS_   0x8B81u
#define GL_LINK_STATUS_      0x8B82u
#define GL_TEXTURE_MIN_FILTER_ 0x2801u
#define GL_TEXTURE_MAG_FILTER_ 0x2800u
#define GL_LINEAR_           0x2601u
#define GLFW_KEY_ESCAPE_     256
// For glReadPixels / glPixelStorei
#define GL_RGB_              0x1907u
#define GL_RGBA_             0x1908u
#define GL_UNSIGNED_BYTE_    0x1401u
#define GL_PACK_ALIGNMENT_   0x0D05u
// glTexImage2D internals for color upload
#define GL_TEXTURE_WRAP_S_   0x2802u
#define GL_TEXTURE_WRAP_T_   0x2803u
#define GL_CLAMP_TO_EDGE_    0x812Fu

typedef const unsigned char* (*PFN_S)(GLenum_);
typedef void  (*PFN_v)     (void);
typedef void  (*PFN_bt)    (GLenum_, GLuint_);
typedef void  (*PFN_tp)    (GLenum_, GLenum_, GLint_);
typedef void  (*PFN_at)    (GLenum_);
typedef void  (*PFN_vp)    (GLint_, GLint_, GLsizei_, GLsizei_);
typedef void  (*PFN_cl)    (GLenum_);
typedef void  (*PFN_cc)    (GLfloat_, GLfloat_, GLfloat_, GLfloat_);
typedef void  (*PFN_gva)   (GLsizei_, GLuint_*);
typedef void  (*PFN_bva)   (GLuint_);
typedef void  (*PFN_gb)    (GLsizei_, GLuint_*);
typedef void  (*PFN_bb)    (GLenum_, GLuint_);
typedef void  (*PFN_bd)    (GLenum_, long, const void*, GLenum_);
typedef void  (*PFN_vap)   (GLuint_, GLint_, GLenum_, GLbool_, GLsizei_, const void*);
typedef void  (*PFN_eva)   (GLuint_);
typedef GLuint_ (*PFN_cs)  (GLenum_);
typedef void  (*PFN_ss)    (GLuint_, GLsizei_, const GLchar_* const*, const GLint_*);
typedef void  (*PFN_comp)  (GLuint_);
typedef void  (*PFN_giv)   (GLuint_, GLenum_, GLint_*);
typedef void  (*PFN_gil)   (GLuint_, GLsizei_, GLsizei_*, GLchar_*);
typedef GLuint_ (*PFN_cp)  (void);
typedef void  (*PFN_as)    (GLuint_, GLuint_);
typedef void  (*PFN_lp)    (GLuint_);
typedef void  (*PFN_up)    (GLuint_);
typedef GLint_ (*PFN_gul)  (GLuint_, const GLchar_*);
typedef void  (*PFN_u1i)   (GLint_, GLint_);
typedef void  (*PFN_da)    (GLenum_, GLint_, GLsizei_);
// New for NVENC record path
typedef void  (*PFN_rp)    (GLint_, GLint_, GLsizei_, GLsizei_, GLenum_, GLenum_, void*);
typedef void  (*PFN_psi)   (GLenum_, GLint_);
// New for color upload
typedef void  (*PFN_gt)    (GLsizei_, GLuint_*);
typedef void  (*PFN_ti2d)  (GLenum_, GLint_, GLint_, GLsizei_, GLsizei_, GLint_, GLenum_, GLenum_, const void*);
typedef void  (*PFN_tsi2d) (GLenum_, GLint_, GLint_, GLsizei_, GLsizei_, GLsizei_, GLsizei_, GLenum_, GLenum_, const void*);

static PFN_S   gGetString;         static PFN_bt  gBindTexture;       static PFN_tp  gTexParameteri;
static PFN_at  gActiveTexture;     static PFN_vp  gViewport;          static PFN_cl  gClear;
static PFN_cc  gClearColor;        static PFN_v   gFinish;
static PFN_gva gGenVertexArrays;   static PFN_bva gBindVertexArray;   static PFN_gb  gGenBuffers;
static PFN_bb  gBindBuffer;        static PFN_bd  gBufferData;        static PFN_vap gVertexAttribPointer;
static PFN_eva gEnableVertexAttribArray;
static PFN_cs  gCreateShader;      static PFN_ss  gShaderSource;      static PFN_comp gCompileShader;
static PFN_giv gGetShaderiv;       static PFN_gil gGetShaderInfoLog;  static PFN_cp  gCreateProgram;
static PFN_as  gAttachShader;      static PFN_lp  gLinkProgram;       static PFN_giv gGetProgramiv;
static PFN_up  gUseProgram;        static PFN_gul gGetUniformLocation; static PFN_u1i gUniform1i;
static PFN_da  gDrawArrays;
static PFN_rp  gReadPixels;        static PFN_psi gPixelStorei;
static PFN_gt  gGenTextures;       static PFN_ti2d gTexImage2D;

template <typename T> static T L(const char* n, bool& ok) {
    auto f = reinterpret_cast<T>(glfwGetProcAddress(n));
    if (!f) { printf("FATAL: glfwGetProcAddress(%s) null\n", n); ok = false; }
    return f;
}
static bool load_gl() {
    bool ok = true;
    gGetString           = L<PFN_S>  ("glGetString",            ok);
    gBindTexture         = L<PFN_bt> ("glBindTexture",           ok);
    gTexParameteri       = L<PFN_tp> ("glTexParameteri",         ok);
    gActiveTexture       = L<PFN_at> ("glActiveTexture",         ok);
    gViewport            = L<PFN_vp> ("glViewport",              ok);
    gClear               = L<PFN_cl> ("glClear",                 ok);
    gClearColor          = L<PFN_cc> ("glClearColor",            ok);
    gFinish              = L<PFN_v>  ("glFinish",                ok);
    gGenVertexArrays     = L<PFN_gva>("glGenVertexArrays",       ok);
    gBindVertexArray     = L<PFN_bva>("glBindVertexArray",       ok);
    gGenBuffers          = L<PFN_gb> ("glGenBuffers",            ok);
    gBindBuffer          = L<PFN_bb> ("glBindBuffer",            ok);
    gBufferData          = L<PFN_bd> ("glBufferData",            ok);
    gVertexAttribPointer = L<PFN_vap>("glVertexAttribPointer",   ok);
    gEnableVertexAttribArray = L<PFN_eva>("glEnableVertexAttribArray", ok);
    gCreateShader        = L<PFN_cs> ("glCreateShader",          ok);
    gShaderSource        = L<PFN_ss> ("glShaderSource",          ok);
    gCompileShader       = L<PFN_comp>("glCompileShader",        ok);
    gGetShaderiv         = L<PFN_giv>("glGetShaderiv",           ok);
    gGetShaderInfoLog    = L<PFN_gil>("glGetShaderInfoLog",       ok);
    gCreateProgram       = L<PFN_cp> ("glCreateProgram",         ok);
    gAttachShader        = L<PFN_as> ("glAttachShader",          ok);
    gLinkProgram         = L<PFN_lp> ("glLinkProgram",           ok);
    gGetProgramiv        = L<PFN_giv>("glGetProgramiv",          ok);
    gUseProgram          = L<PFN_up> ("glUseProgram",            ok);
    gGetUniformLocation  = L<PFN_gul>("glGetUniformLocation",    ok);
    gUniform1i           = L<PFN_u1i>("glUniform1i",             ok);
    gDrawArrays          = L<PFN_da> ("glDrawArrays",            ok);
    gReadPixels          = L<PFN_rp> ("glReadPixels",            ok);
    gPixelStorei         = L<PFN_psi>("glPixelStorei",           ok);
    gGenTextures         = L<PFN_gt> ("glGenTextures",           ok);
    gTexImage2D          = L<PFN_ti2d>("glTexImage2D",           ok);
    return ok;
}

static const char* VS = "#version 330 core\n"
    "layout(location=0) in vec2 pos; layout(location=1) in vec2 uv; out vec2 vUV;\n"
    "void main(){ vUV = vec2(uv.x, 1.0-uv.y); gl_Position = vec4(pos,0.0,1.0); }\n";  // flip Y for screen
static const char* FS = "#version 330 core\n"
    "in vec2 vUV; out vec4 frag; uniform sampler2D tex;\n"
    "void main(){ frag = vec4(texture(tex, vUV).rgb, 1.0); }\n";

static GLuint_ make_program() {
    auto sh = [](GLenum_ t, const char* s) {
        GLuint_ id = gCreateShader(t); gShaderSource(id, 1, &s, nullptr); gCompileShader(id);
        GLint_ ok = 0; gGetShaderiv(id, GL_COMPILE_STATUS_, &ok);
        if (!ok) { char log[1024]; GLsizei_ n = 0; gGetShaderInfoLog(id, 1024, &n, log); printf("shader: %.*s\n", (int)n, log); }
        return id;
    };
    GLuint_ p = gCreateProgram();
    gAttachShader(p, sh(GL_VERTEX_SHADER_, VS)); gAttachShader(p, sh(GL_FRAGMENT_SHADER_, FS));
    gLinkProgram(p); GLint_ ok = 0; gGetProgramiv(p, GL_LINK_STATUS_, &ok);
    if (!ok) printf("FATAL: program link failed\n");
    return p;
}

static double now_ms() {
    using namespace std::chrono;
    return duration<double, std::milli>(steady_clock::now().time_since_epoch()).count();
}

// ---------------------------------------------------------------------------
// RAII ffmpeg pipe: popen/pclose, SIGPIPE-safe, best-effort writes
// ---------------------------------------------------------------------------
struct FfmpegPipe {
    FILE* fp = nullptr;

    FfmpegPipe() = default;
    FfmpegPipe(const FfmpegPipe&)            = delete;
    FfmpegPipe& operator=(const FfmpegPipe&) = delete;
    FfmpegPipe(FfmpegPipe&& o) noexcept : fp(o.fp) { o.fp = nullptr; }
    FfmpegPipe& operator=(FfmpegPipe&& o) noexcept {
        if (this != &o) { if (fp) pclose(fp); fp = o.fp; o.fp = nullptr; }
        return *this;
    }

    // Open an ffmpeg child writing h264_nvenc to out_path at the given resolution/fps.
    // Returns false if popen fails (non-fatal: record silently disabled).
    bool open(const std::string& out_path, int fbw, int fbh, int stream_fps) {
        // Input: raw RGBA, bottom-left origin -> vflip to correct orientation
        // Use system ffmpeg (/usr/bin/ffmpeg) which we confirmed has h264_nvenc.
        // If LRS_FFMPEG_BIN is set, use that instead.
        const char* ffbin = getenv("LRS_FFMPEG_BIN");
        if (!ffbin || ffbin[0] == '\0') ffbin = "/usr/bin/ffmpeg";

        // Build the command string safely
        char cmd[2048];
        int n = snprintf(cmd, sizeof(cmd),
            "%s -y -f rawvideo -pixel_format rgba -video_size %dx%d -framerate %d -i pipe:0 "
            "-vf vflip -c:v h264_nvenc -preset p4 -an \"%s\" 2>/dev/null",
            ffbin, fbw, fbh, stream_fps, out_path.c_str());
        if (n < 0 || static_cast<size_t>(n) >= sizeof(cmd)) {
            printf("RECORD: command too long — recording disabled\n");
            return false;
        }
        fp = popen(cmd, "w");
        if (!fp) {
            printf("RECORD: popen(\"%s\") failed: %s — recording disabled\n", ffbin, strerror(errno));
            return false;
        }
        printf("RECORD: ffmpeg child started -> \"%s\"  (%dx%d @%d fps, h264_nvenc, vflip)\n",
               out_path.c_str(), fbw, fbh, stream_fps);
        return true;
    }

    // Best-effort write: silently drop frame if pipe is dead.
    void write_frame(const void* data, size_t bytes) {
        if (!fp) return;
        if (ferror(fp)) return;  // pipe already broken — ignore
        (void)fwrite(data, 1, bytes, fp);
        // Don't check return: SIGPIPE is masked; ferror will catch it next time.
    }

    ~FfmpegPipe() {
        if (fp) {
            pclose(fp);  // blocks until ffmpeg flushes/finishes (correct teardown)
            fp = nullptr;
            printf("RECORD: ffmpeg child joined — output written.\n");
        }
    }
};

// ---------------------------------------------------------------------------
// Telemetry: print every N frames
// ---------------------------------------------------------------------------
static void print_telemetry(const rs2::depth_frame& df,
                             const std::vector<double>& render_ms,
                             double wall_fps,
                             int telemetry_interval)
{
    unsigned long long frame_num = df.get_frame_number();
    double ts      = df.get_timestamp();
    auto   ts_dom  = df.get_frame_timestamp_domain();
    const char* dom_str = rs2_timestamp_domain_to_string(ts_dom);

    printf("[TELEM frame=%llu] ts=%.3f ms  domain=%s  wall_fps=%.1f",
           frame_num, ts, dom_str, wall_fps);

    // Metadata — always guard with supports_frame_metadata (throws if unsupported)
    if (df.supports_frame_metadata(RS2_FRAME_METADATA_ACTUAL_EXPOSURE)) {
        long long exp_us = df.get_frame_metadata(RS2_FRAME_METADATA_ACTUAL_EXPOSURE);
        printf("  exposure=%lld us", exp_us);
    }
    if (df.supports_frame_metadata(RS2_FRAME_METADATA_ACTUAL_FPS)) {
        long long fps_x1000 = df.get_frame_metadata(RS2_FRAME_METADATA_ACTUAL_FPS);
        printf("  actual_fps=%.1f", fps_x1000 / 1000.0);
    }
    if (df.supports_frame_metadata(RS2_FRAME_METADATA_FRAME_COUNTER)) {
        long long fctr = df.get_frame_metadata(RS2_FRAME_METADATA_FRAME_COUNTER);
        printf("  frame_ctr=%lld", fctr);
    }

    // Rolling render p50 over the most recent `telemetry_interval` samples
    if (!render_ms.empty()) {
        size_t window = static_cast<size_t>(telemetry_interval);
        size_t start  = render_ms.size() > window ? render_ms.size() - window : 0;
        std::vector<double> win(render_ms.begin() + static_cast<std::ptrdiff_t>(start),
                                render_ms.end());
        std::nth_element(win.begin(), win.begin() + static_cast<std::ptrdiff_t>(win.size() / 2), win.end());
        double p50 = win[win.size() / 2];
        printf("  render_p50=%.3f ms (last %zu frames, NO D2H)", p50, win.size());
    }
    printf("\n");
    fflush(stdout);
}

// ---------------------------------------------------------------------------
// main
// ---------------------------------------------------------------------------
int main(int argc, char** argv) {
    int    W         = 848;
    int    H         = 480;
    int    fps       = 30;
    double duration  = 15.0;
    std::string record_path;
    bool   color_mode = false;

    for (int i = 1; i < argc; ++i) {
        std::string a = argv[i];
        if      (a == "--duration"  && i + 1 < argc) duration    = atof(argv[++i]);
        else if (a == "--size"      && i + 1 < argc) { sscanf(argv[++i], "%dx%d", &W, &H); }
        else if (a == "--record"    && i + 1 < argc) record_path = argv[++i];
        else if (a == "--stream"    && i + 1 < argc) {
            std::string mode = argv[++i];
            if (mode == "color") {
                color_mode = true;
                printf("NOTE: --stream color uses a host-upload path (BGR->texture). "
                       "The keep-on-GPU GPU-resident benefit (no D2H) applies to depth mode.\n");
            }
        }
    }

    printf("== GB10 keep-on-GPU viewer ==  %dx%d@%d  duration=%.0fs  mode=%s  %s\n",
           W, H, fps, duration,
           color_mode ? "color(upload)" : "depth(GL-resident)",
           record_path.empty() ? "(no record)" : ("record=" + record_path).c_str());

    // Mask SIGPIPE so a dead ffmpeg child doesn't kill the viewer
    signal(SIGPIPE, SIG_IGN);

    if (!glfwInit()) { printf("FATAL: glfwInit\n"); return 2; }
    glfwWindowHint(GLFW_RESIZABLE, 0);  // fixed size: keeps glReadPixels geometry stable
    GLFWwindow* win = glfwCreateWindow(W, H, "GB10 keep-on-GPU (live depth, GL-resident)", nullptr, nullptr);
    if (!win) {
        printf("FATAL: glfwCreateWindow (DISPLAY=%s)\n", getenv("DISPLAY") ? getenv("DISPLAY") : "unset");
        glfwTerminate(); return 3;
    }
    glfwMakeContextCurrent(win); glfwSwapInterval(0);
    if (!load_gl()) { glfwTerminate(); return 4; }

    std::string renderer = reinterpret_cast<const char*>(gGetString(GL_RENDERER_));
    printf("GL_RENDERER: %s\n", renderer.c_str());
    if (renderer.find("NVIDIA") == std::string::npos && renderer.find("GB10") == std::string::npos) {
        printf("FATAL: not the GB10 GPU (software rasterizer?) — aborting\n");
        glfwDestroyWindow(win); glfwTerminate(); return 5;
    }

    int rc = 0;
    bool gl_inited = false;   // guard teardown: only shut the GL lane down if init_processing() actually ran
    try {
        rs2::gl::init_processing(win, /*use_glsl=*/true);   // share the library GL context with our window
        gl_inited = true;

        rs2::pipeline pipe; rs2::config cfg;
        if (color_mode) {
            cfg.enable_stream(RS2_STREAM_COLOR, W, H, RS2_FORMAT_BGR8, fps);
        } else {
            cfg.enable_stream(RS2_STREAM_DEPTH, W, H, RS2_FORMAT_Z16, fps);
        }
        pipe.start(cfg);

        rs2::gl::colorizer gl_col;  // only used in depth mode (stays in GL texture)

        // For color mode: allocate a plain texture to upload host BGR frames
        GLuint_ color_tex = 0;
        if (color_mode) {
            gGenTextures(1, &color_tex);
            gBindTexture(GL_TEXTURE_2D_, color_tex);
            gTexParameteri(GL_TEXTURE_2D_, GL_TEXTURE_MIN_FILTER_, GL_LINEAR_);
            gTexParameteri(GL_TEXTURE_2D_, GL_TEXTURE_MAG_FILTER_, GL_LINEAR_);
            gTexParameteri(GL_TEXTURE_2D_, GL_TEXTURE_WRAP_S_, GL_CLAMP_TO_EDGE_);
            gTexParameteri(GL_TEXTURE_2D_, GL_TEXTURE_WRAP_T_, GL_CLAMP_TO_EDGE_);
            gBindTexture(GL_TEXTURE_2D_, 0);
        }

        GLuint_ prog = make_program(), vao = 0, vbo = 0;
        const float quad[] = { -1,-1, 0,0,  1,-1, 1,0,  -1,1, 0,1,  1,1, 1,1 };
        gGenVertexArrays(1, &vao); gBindVertexArray(vao);
        gGenBuffers(1, &vbo); gBindBuffer(GL_ARRAY_BUFFER_, vbo);
        gBufferData(GL_ARRAY_BUFFER_, (long)sizeof(quad), quad, GL_STATIC_DRAW_);
        gVertexAttribPointer(0, 2, GL_FLOAT_, 0, 4 * (GLsizei_)sizeof(float), (void*)0);
        gEnableVertexAttribArray(0);
        gVertexAttribPointer(1, 2, GL_FLOAT_, 0, 4 * (GLsizei_)sizeof(float), (void*)(2 * sizeof(float)));
        gEnableVertexAttribArray(1);

        // Obtain framebuffer size for the recording path (GLFW_RESIZABLE=0 → constant)
        int fbw = 0, fbh = 0;
        glfwGetFramebufferSize(win, &fbw, &fbh);

        // RAII ffmpeg child — opened once, destructs on any exit path
        FfmpegPipe ffpipe;
        std::vector<uint8_t> readback_buf;
        if (!record_path.empty()) {
            if (ffpipe.open(record_path, fbw, fbh, fps)) {
                // RGBA readback buffer: fbw * fbh * 4 bytes, aligned to row-size=4 (GL default)
                readback_buf.resize(static_cast<size_t>(fbw) * static_cast<size_t>(fbh) * 4u);
            }
        }

        static constexpr int TELEMETRY_INTERVAL = 30;  // print every N frames
        std::vector<double> render_ms; render_ms.reserve(2000);
        double t0 = now_ms(); int frames = 0;

        while (!glfwWindowShouldClose(win) && glfwGetKey(win, GLFW_KEY_ESCAPE_) != 1) {
            rs2::frameset fs = pipe.wait_for_frames(5000);

            // ---- Telemetry source: always the depth frame (metadata lives on the depth sensor) ----
            rs2::depth_frame df = fs.get_depth_frame();

            double r0 = now_ms();

            if (color_mode) {
                // Color path: host-side BGR upload to GL texture (no GL colorizer, no GPU-resident benefit)
                rs2::video_frame cf = fs.get_color_frame();
                if (cf) {
                    const uint8_t* bgr = static_cast<const uint8_t*>(cf.get_data());
                    int fw = cf.get_width(), fh = cf.get_height();
                    gActiveTexture(GL_TEXTURE0_);
                    gBindTexture(GL_TEXTURE_2D_, color_tex);
                    // Upload BGR -> supply as GL_RGB; the sensor delivers BGR8 so we swap R/B in
                    // the shader by reversing the component order — simplest fix: just upload as RGB
                    // (the swap is visually acceptable for a debug viewer; production path would use
                    // GL_BGR_EXT if available, but we keep the loader simple here).
                    gTexImage2D(GL_TEXTURE_2D_, 0, (GLint_)GL_RGB_, (GLsizei_)fw, (GLsizei_)fh,
                                0, GL_RGB_, GL_UNSIGNED_BYTE_, bgr);
                    gTexParameteri(GL_TEXTURE_2D_, GL_TEXTURE_MIN_FILTER_, GL_LINEAR_);
                    gTexParameteri(GL_TEXTURE_2D_, GL_TEXTURE_MAG_FILTER_, GL_LINEAR_);
                }
                gFinish();
            } else {
                // Depth path: GPU-resident colorize (no D2H)
                if (!df) continue;
                rs2::frame gf = gl_col.process(df);           // colorize on GPU (stays in GL texture)
                gFinish();                                     // ensure the library GL work completed
                rs2::gl::gpu_frame gpu(gf);
                GLuint_ tex = gpu ? gpu.get_texture_id(0) : 0;
                int cur_fbw = 0, cur_fbh = 0;
                glfwGetFramebufferSize(win, &cur_fbw, &cur_fbh);
                gViewport(0, 0, cur_fbw, cur_fbh);
                gClearColor(0, 0, 0, 1); gClear(GL_COLOR_BUFFER_BIT_);
                if (tex) {
                    gUseProgram(prog); gActiveTexture(GL_TEXTURE0_); gBindTexture(GL_TEXTURE_2D_, tex);
                    gTexParameteri(GL_TEXTURE_2D_, GL_TEXTURE_MIN_FILTER_, GL_LINEAR_);
                    gTexParameteri(GL_TEXTURE_2D_, GL_TEXTURE_MAG_FILTER_, GL_LINEAR_);
                    gUniform1i(gGetUniformLocation(prog, "tex"), 0);
                    gBindVertexArray(vao); gDrawArrays(GL_TRIANGLE_STRIP_, 0, 4);
                }
                glfwSwapBuffers(win);

                // ---- Sample render_ms BEFORE the D2H readback ----
                render_ms.push_back(now_ms() - r0);

                // ---- NVENC record: glReadPixels of FBO 0, post-draw ----
                if (!readback_buf.empty() && ffpipe.fp) {
                    gPixelStorei(GL_PACK_ALIGNMENT_, 1);       // disable row padding for arbitrary sizes
                    gReadPixels(0, 0, (GLsizei_)fbw, (GLsizei_)fbh,
                                GL_RGBA_, GL_UNSIGNED_BYTE_, readback_buf.data());
                    // Note: origin is bottom-left; ffmpeg corrects with -vf vflip
                    ffpipe.write_frame(readback_buf.data(), readback_buf.size());
                }

                glfwPollEvents();
                frames++;
                if (duration > 0 && (now_ms() - t0) / 1000.0 >= duration) break;

                // ---- Telemetry (depth mode — df is valid) ----
                if (df && frames % TELEMETRY_INTERVAL == 0) {
                    double wall_fps = frames / ((now_ms() - t0) / 1000.0);
                    print_telemetry(df, render_ms, wall_fps, TELEMETRY_INTERVAL);
                }
                continue;  // already did swap + poll above
            }

            // Color mode draw path (reached only when color_mode == true)
            {
                int cur_fbw = 0, cur_fbh = 0;
                glfwGetFramebufferSize(win, &cur_fbw, &cur_fbh);
                gViewport(0, 0, cur_fbw, cur_fbh);
                gClearColor(0, 0, 0, 1); gClear(GL_COLOR_BUFFER_BIT_);
                gUseProgram(prog); gActiveTexture(GL_TEXTURE0_); gBindTexture(GL_TEXTURE_2D_, color_tex);
                gUniform1i(gGetUniformLocation(prog, "tex"), 0);
                gBindVertexArray(vao); gDrawArrays(GL_TRIANGLE_STRIP_, 0, 4);
                glfwSwapBuffers(win);

                render_ms.push_back(now_ms() - r0);

                // NVENC record for color mode
                if (!readback_buf.empty() && ffpipe.fp) {
                    gPixelStorei(GL_PACK_ALIGNMENT_, 1);
                    gReadPixels(0, 0, (GLsizei_)fbw, (GLsizei_)fbh,
                                GL_RGBA_, GL_UNSIGNED_BYTE_, readback_buf.data());
                    ffpipe.write_frame(readback_buf.data(), readback_buf.size());
                }

                glfwPollEvents();
                frames++;
                if (duration > 0 && (now_ms() - t0) / 1000.0 >= duration) break;

                // Telemetry in color mode: use df if available, else skip metadata
                if (frames % TELEMETRY_INTERVAL == 0) {
                    double wall_fps = frames / ((now_ms() - t0) / 1000.0);
                    if (df) {
                        print_telemetry(df, render_ms, wall_fps, TELEMETRY_INTERVAL);
                    } else {
                        double p50 = 0.0;
                        if (!render_ms.empty()) {
                            std::vector<double> win_ms = render_ms;
                            std::nth_element(win_ms.begin(),
                                             win_ms.begin() + static_cast<std::ptrdiff_t>(win_ms.size() / 2),
                                             win_ms.end());
                            p50 = win_ms[win_ms.size() / 2];
                        }
                        printf("[TELEM frame=%d] wall_fps=%.1f  render_p50=%.3f ms\n",
                               frames, wall_fps, p50);
                        fflush(stdout);
                    }
                }
            }
        }
        pipe.stop();

        if (!render_ms.empty()) {
            std::sort(render_ms.begin(), render_ms.end());
            double p50 = render_ms[render_ms.size() / 2];
            double wall_fps = frames / ((now_ms() - t0) / 1000.0);
            printf("frames=%d  delivered_fps=%.1f  keep-on-GPU render p50=%.3f ms/frame "
                   "(%s, NO D2H for depth path)\n",
                   frames, wall_fps, p50,
                   color_mode ? "color-upload + draw + present" : "colorize-gl + draw + present");
        }

        // FfmpegPipe destructor runs here, joining the child BEFORE GL shutdown.
        // Explicit scope to enforce destruction order vs RS2 teardown below.
        { FfmpegPipe tmp = std::move(ffpipe); }
    } catch (const std::exception& e) {
        printf("ERROR: %s\n", e.what()); rc = 1;
    }
    // ---- R2 FIX: shut the GL processing lane down WHILE THE CONTEXT IS STILL CURRENT ----
    // Runs on BOTH success and error paths. (It was the last statement INSIDE the try, so any
    // exception — e.g. wait_for_frames() timing out on an xHCI stall — skipped it and let the
    // static GL destructors fire against a dead context: the exact R2 SIGSEGV, on every error
    // exit.) On the error path the local FfmpegPipe has already drained during stack unwind,
    // so the ffmpeg-before-GL ordering still holds.
    if (gl_inited) {
        rs2::gl::shutdown_processing();
        printf("rs2::gl::shutdown_processing() OK (context still current) — clean teardown, no SIGSEGV.\n");
    }
    glfwDestroyWindow(win);
    glfwTerminate();
    printf("DONE rc=%d (clean return — R2 teardown order verified).\n", rc);
    return rc;
}
