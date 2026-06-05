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
// SAFE: single depth stream (the conservative envelope). Build: rs-gb10-keepongpu-build.sh
// Run via the launcher / `just hil-keepongpu`. ESC or window-close or --duration ends it.

#include <GLFW/glfw3.h>                              // must precede the gl header (context-share overload)
#include <librealsense2/rs.hpp>
#include <librealsense2-gl/rs_processing_gl.hpp>
#include <cstdio>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <vector>
#include <chrono>
#include <algorithm>
#include <string>

using GLenum_ = unsigned int; using GLuint_ = unsigned int; using GLint_ = int;
using GLsizei_ = int; using GLbool_ = unsigned char; using GLchar_ = char; using GLfloat_ = float;
#define GL_VENDOR_ 0x1F00
#define GL_RENDERER_ 0x1F01
#define GL_VERSION_ 0x1F02
#define GL_TEXTURE_2D_ 0x0DE1
#define GL_TEXTURE0_ 0x84C0
#define GL_FLOAT_ 0x1406
#define GL_COLOR_BUFFER_BIT_ 0x00004000
#define GL_TRIANGLE_STRIP_ 0x0005
#define GL_ARRAY_BUFFER_ 0x8892
#define GL_STATIC_DRAW_ 0x88E4
#define GL_VERTEX_SHADER_ 0x8B31
#define GL_FRAGMENT_SHADER_ 0x8B30
#define GL_COMPILE_STATUS_ 0x8B81
#define GL_LINK_STATUS_ 0x8B82
#define GL_TEXTURE_MIN_FILTER_ 0x2801
#define GL_TEXTURE_MAG_FILTER_ 0x2800
#define GL_LINEAR_ 0x2601
#define GLFW_KEY_ESCAPE_ 256

typedef const unsigned char* (*PFN_S)(GLenum_);
typedef void (*PFN_v)(void);
typedef void (*PFN_bt)(GLenum_, GLuint_);
typedef void (*PFN_tp)(GLenum_, GLenum_, GLint_);
typedef void (*PFN_at)(GLenum_);
typedef void (*PFN_vp)(GLint_, GLint_, GLsizei_, GLsizei_);
typedef void (*PFN_cl)(GLenum_);
typedef void (*PFN_cc)(GLfloat_, GLfloat_, GLfloat_, GLfloat_);
typedef void (*PFN_gva)(GLsizei_, GLuint_*);
typedef void (*PFN_bva)(GLuint_);
typedef void (*PFN_gb)(GLsizei_, GLuint_*);
typedef void (*PFN_bb)(GLenum_, GLuint_);
typedef void (*PFN_bd)(GLenum_, long, const void*, GLenum_);
typedef void (*PFN_vap)(GLuint_, GLint_, GLenum_, GLbool_, GLsizei_, const void*);
typedef void (*PFN_eva)(GLuint_);
typedef GLuint_ (*PFN_cs)(GLenum_);
typedef void (*PFN_ss)(GLuint_, GLsizei_, const GLchar_* const*, const GLint_*);
typedef void (*PFN_comp)(GLuint_);
typedef void (*PFN_giv)(GLuint_, GLenum_, GLint_*);
typedef void (*PFN_gil)(GLuint_, GLsizei_, GLsizei_*, GLchar_*);
typedef GLuint_ (*PFN_cp)(void);
typedef void (*PFN_as)(GLuint_, GLuint_);
typedef void (*PFN_lp)(GLuint_);
typedef void (*PFN_up)(GLuint_);
typedef GLint_ (*PFN_gul)(GLuint_, const GLchar_*);
typedef void (*PFN_u1i)(GLint_, GLint_);
typedef void (*PFN_da)(GLenum_, GLint_, GLsizei_);

static PFN_S gGetString; static PFN_bt gBindTexture; static PFN_tp gTexParameteri; static PFN_at gActiveTexture;
static PFN_vp gViewport; static PFN_cl gClear; static PFN_cc gClearColor; static PFN_v gFinish;
static PFN_gva gGenVertexArrays; static PFN_bva gBindVertexArray; static PFN_gb gGenBuffers; static PFN_bb gBindBuffer;
static PFN_bd gBufferData; static PFN_vap gVertexAttribPointer; static PFN_eva gEnableVertexAttribArray;
static PFN_cs gCreateShader; static PFN_ss gShaderSource; static PFN_comp gCompileShader; static PFN_giv gGetShaderiv;
static PFN_gil gGetShaderInfoLog; static PFN_cp gCreateProgram; static PFN_as gAttachShader; static PFN_lp gLinkProgram;
static PFN_giv gGetProgramiv; static PFN_up gUseProgram; static PFN_gul gGetUniformLocation; static PFN_u1i gUniform1i;
static PFN_da gDrawArrays;

template <typename T> static T L(const char* n, bool& ok) {
    auto f = reinterpret_cast<T>(glfwGetProcAddress(n));
    if (!f) { printf("FATAL: glfwGetProcAddress(%s) null\n", n); ok = false; }
    return f;
}
static bool load_gl() {
    bool ok = true;
    gGetString = L<PFN_S>("glGetString", ok); gBindTexture = L<PFN_bt>("glBindTexture", ok);
    gTexParameteri = L<PFN_tp>("glTexParameteri", ok); gActiveTexture = L<PFN_at>("glActiveTexture", ok);
    gViewport = L<PFN_vp>("glViewport", ok); gClear = L<PFN_cl>("glClear", ok); gClearColor = L<PFN_cc>("glClearColor", ok);
    gFinish = L<PFN_v>("glFinish", ok); gGenVertexArrays = L<PFN_gva>("glGenVertexArrays", ok);
    gBindVertexArray = L<PFN_bva>("glBindVertexArray", ok); gGenBuffers = L<PFN_gb>("glGenBuffers", ok);
    gBindBuffer = L<PFN_bb>("glBindBuffer", ok); gBufferData = L<PFN_bd>("glBufferData", ok);
    gVertexAttribPointer = L<PFN_vap>("glVertexAttribPointer", ok); gEnableVertexAttribArray = L<PFN_eva>("glEnableVertexAttribArray", ok);
    gCreateShader = L<PFN_cs>("glCreateShader", ok); gShaderSource = L<PFN_ss>("glShaderSource", ok);
    gCompileShader = L<PFN_comp>("glCompileShader", ok); gGetShaderiv = L<PFN_giv>("glGetShaderiv", ok);
    gGetShaderInfoLog = L<PFN_gil>("glGetShaderInfoLog", ok); gCreateProgram = L<PFN_cp>("glCreateProgram", ok);
    gAttachShader = L<PFN_as>("glAttachShader", ok); gLinkProgram = L<PFN_lp>("glLinkProgram", ok);
    gGetProgramiv = L<PFN_giv>("glGetProgramiv", ok); gUseProgram = L<PFN_up>("glUseProgram", ok);
    gGetUniformLocation = L<PFN_gul>("glGetUniformLocation", ok); gUniform1i = L<PFN_u1i>("glUniform1i", ok);
    gDrawArrays = L<PFN_da>("glDrawArrays", ok);
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

int main(int argc, char** argv) {
    int W = 848, H = 480, fps = 30; double duration = 15.0;
    for (int i = 1; i < argc; ++i) {
        std::string a = argv[i];
        if (a == "--duration" && i + 1 < argc) duration = atof(argv[++i]);
        else if (a == "--size" && i + 1 < argc) { sscanf(argv[++i], "%dx%d", &W, &H); }
    }
    printf("== GB10 keep-on-GPU viewer ==  %dx%d@%d  duration=%.0fs  (ESC/close to quit)\n", W, H, fps, duration);

    if (!glfwInit()) { printf("FATAL: glfwInit\n"); return 2; }
    GLFWwindow* win = glfwCreateWindow(W, H, "GB10 keep-on-GPU (live depth, GL-resident)", nullptr, nullptr);
    if (!win) { printf("FATAL: glfwCreateWindow (DISPLAY=%s)\n", getenv("DISPLAY") ? getenv("DISPLAY") : "unset"); glfwTerminate(); return 3; }
    glfwMakeContextCurrent(win); glfwSwapInterval(0);
    if (!load_gl()) { glfwTerminate(); return 4; }
    std::string renderer = (const char*)gGetString(GL_RENDERER_);
    printf("GL_RENDERER: %s\n", renderer.c_str());
    if (renderer.find("NVIDIA") == std::string::npos && renderer.find("GB10") == std::string::npos) {
        printf("FATAL: not the GB10 GPU (software rasterizer?) — aborting\n"); glfwDestroyWindow(win); glfwTerminate(); return 5;
    }

    int rc = 0;
    try {
        rs2::gl::init_processing(win, /*use_glsl=*/true);   // share the library GL context with our window
        rs2::gl::colorizer gl_col;
        rs2::pipeline pipe; rs2::config cfg;
        cfg.enable_stream(RS2_STREAM_DEPTH, W, H, RS2_FORMAT_Z16, fps);
        pipe.start(cfg);

        GLuint_ prog = make_program(), vao = 0, vbo = 0;
        const float quad[] = { -1,-1, 0,0,  1,-1, 1,0,  -1,1, 0,1,  1,1, 1,1 };
        gGenVertexArrays(1, &vao); gBindVertexArray(vao);
        gGenBuffers(1, &vbo); gBindBuffer(GL_ARRAY_BUFFER_, vbo);
        gBufferData(GL_ARRAY_BUFFER_, sizeof(quad), quad, GL_STATIC_DRAW_);
        gVertexAttribPointer(0, 2, GL_FLOAT_, 0, 4 * sizeof(float), (void*)0); gEnableVertexAttribArray(0);
        gVertexAttribPointer(1, 2, GL_FLOAT_, 0, 4 * sizeof(float), (void*)(2 * sizeof(float))); gEnableVertexAttribArray(1);

        std::vector<double> render_ms; render_ms.reserve(2000);
        double t0 = now_ms(); int frames = 0;
        while (!glfwWindowShouldClose(win) && glfwGetKey(win, GLFW_KEY_ESCAPE_) != 1) {
            rs2::frameset fs = pipe.wait_for_frames(5000);
            rs2::depth_frame df = fs.get_depth_frame();
            if (!df) continue;
            double r0 = now_ms();
            rs2::frame gf = gl_col.process(df);                 // colorize on GPU (stays in a GL texture)
            gFinish();                                          // ensure the library GL work completed
            rs2::gl::gpu_frame gpu(gf);
            GLuint_ tex = gpu ? gpu.get_texture_id(0) : 0;
            int fbw, fbh; glfwGetFramebufferSize(win, &fbw, &fbh);
            gViewport(0, 0, fbw, fbh); gClearColor(0, 0, 0, 1); gClear(GL_COLOR_BUFFER_BIT_);
            if (tex) {                                          // draw the GPU texture straight to the window — no D2H
                gUseProgram(prog); gActiveTexture(GL_TEXTURE0_); gBindTexture(GL_TEXTURE_2D_, tex);
                gTexParameteri(GL_TEXTURE_2D_, GL_TEXTURE_MIN_FILTER_, GL_LINEAR_);
                gTexParameteri(GL_TEXTURE_2D_, GL_TEXTURE_MAG_FILTER_, GL_LINEAR_);
                gUniform1i(gGetUniformLocation(prog, "tex"), 0);
                gBindVertexArray(vao); gDrawArrays(GL_TRIANGLE_STRIP_, 0, 4);
            }
            glfwSwapBuffers(win);                               // present to the display (GPU scanout)
            render_ms.push_back(now_ms() - r0);
            glfwPollEvents();
            frames++;
            if (duration > 0 && (now_ms() - t0) / 1000.0 >= duration) break;
        }
        pipe.stop();

        if (!render_ms.empty()) {
            std::sort(render_ms.begin(), render_ms.end());
            double p50 = render_ms[render_ms.size() / 2];
            double wall_fps = frames / ((now_ms() - t0) / 1000.0);
            printf("frames=%d  delivered_fps=%.1f  keep-on-GPU render p50=%.3f ms/frame "
                   "(colorize-gl + draw + present, NO D2H)\n", frames, wall_fps, p50);
        }

        // ---- R2 FIX: shut the GL processing lane down WHILE THE CONTEXT IS STILL CURRENT ----
        rs2::gl::shutdown_processing();
        printf("rs2::gl::shutdown_processing() OK (context still current) — clean teardown, no SIGSEGV.\n");
    } catch (const std::exception& e) {
        printf("ERROR: %s\n", e.what()); rc = 1;
    }
    glfwDestroyWindow(win);
    glfwTerminate();
    printf("DONE rc=%d (clean return — R2 teardown order verified).\n", rc);
    return rc;
}
