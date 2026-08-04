// License: Apache 2.0. See LICENSE file in root directory.

#include <librealsense2/rs.hpp>

#include "example.hpp"
#include "example-utils.hpp"

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cmath>
#include <condition_variable>
#include <cstdlib>
#include <cstring>
#include <ctime>
#include <cerrno>
#include <exception>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <map>
#include <memory>
#include <mutex>
#include <fcntl.h>
#include <sstream>
#include <stdexcept>
#include <string>
#include <sys/file.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/resource.h>
#include <thread>
#include <unistd.h>
#include <vector>

using clock_type = std::chrono::steady_clock;

struct options
{
    std::string serial;
    std::string profile = "vga30";
    int cycles = 1;
    double duration_sec = 30.0;
    int timeout_ms = 250;
    int pre_stop_drain_ms = 1200;
    int pre_stop_settle_ms = 250;
    int cooldown_ms = 1000;
    int stop_warn_ms = 5000;
    int hard_stop_ms = 30000;
    std::string output_dir = "/tmp/rs-gb10-profiler";
    bool render = true;
    bool require_usb3 = true;
    bool stress = false;
    bool pointcloud = false;
    bool filters = false;
    bool align_to_color = true;
    bool capture_evidence = true;
    bool list_profiles = false;
};

struct stream_request
{
    rs2_stream stream;
    int index;
    int width;
    int height;
    rs2_format format;
    int fps;
};

struct test_profile
{
    std::string name;
    std::string description;
    std::vector<stream_request> streams;
    bool align_to_color;
    bool pointcloud;
    bool filters;
};

struct running_stats
{
    double total = 0.0;
    double min = 1e30;
    double max = 0.0;
    size_t count = 0;

    void add(double value)
    {
        total += value;
        min = std::min(min, value);
        max = std::max(max, value);
        ++count;
    }

    double mean() const
    {
        return count ? total / static_cast<double>(count) : 0.0;
    }
};

struct stream_counter
{
    uint64_t frames = 0;
    uint64_t gaps = 0;
    unsigned long long last_number = 0;
    bool has_last = false;
};

struct cycle_result
{
    std::string profile;
    int cycle = 0;
    bool started = false;
    bool stopped_cleanly = false;
    bool window_closed = false;
    bool skipped = false;
    bool stop_slow = false;
    bool usb3 = false;
    std::string skip_reason;
    std::string evidence_path;
    uint64_t framesets = 0;
    uint64_t timeouts = 0;
    uint64_t drain_framesets = 0;
    uint64_t drain_timeouts = 0;
    uint64_t exceptions = 0;
    double start_ms = 0.0;
    double pre_stop_ms = 0.0;
    double stop_ms = 0.0;
    double wall_sec = 0.0;
    running_stats wait_ms;
    running_stats process_ms;
    running_stats render_ms;
    std::map<std::string, stream_counter> streams;
    long rss_kb = 0;
};

class process_lock
{
public:
    explicit process_lock(const char* path)
    {
        _fd = open(path, O_CREAT | O_RDWR, 0660);
        if (_fd < 0)
            throw std::runtime_error("Failed to open profiler lock file");
        if (flock(_fd, LOCK_EX | LOCK_NB) != 0)
            throw std::runtime_error("Another RealSense profiler instance is active; refusing concurrent camera ownership");
    }

    ~process_lock()
    {
        if (_fd >= 0)
        {
            flock(_fd, LOCK_UN);
            close(_fd);
        }
    }

    process_lock(const process_lock&) = delete;
    process_lock& operator=(const process_lock&) = delete;

private:
    int _fd = -1;
};

static void usage()
{
    std::cout
        << "Usage: rs-gb10-profiler [options]\n\n"
        << "Visible RealSense GB10 render, lifecycle, and profiling test.\n\n"
        << "Options:\n"
        << "  --serial <sn>          Bind to a specific camera serial\n"
        << "  --profile <name|all>   vga30, vga60, depth90-ir, hd15, or all\n"
        << "  --cycles <n>           Start/stop cycles per profile\n"
        << "  --duration-sec <n>     Seconds per cycle\n"
        << "  --timeout-ms <n>       try_wait_for_frames timeout\n"
        << "  --pre-stop-drain-ms <n> Drain and drop frames before stop\n"
        << "  --pre-stop-settle-ms <n> Sleep after releasing processing state\n"
        << "  --cooldown-ms <n>      Sleep after stop before next start\n"
        << "  --stop-warn-ms <n>     Mark stop dirty if it takes longer than this\n"
        << "  --hard-stop-ms <n>     Terminate if pipeline.stop() wedges past this\n"
        << "  --output-dir <path>    Write compact logs/evidence under this directory\n"
        << "  --no-evidence          Do not write framebuffer evidence images\n"
        << "  --no-render            Disable visible rendering for stress runs\n"
        << "  --allow-usb2           Do not fail when the device is on USB2\n"
        << "  --pointcloud           Force pointcloud processing when depth exists\n"
        << "  --filters              Force depth post-processing filters\n"
        << "  --no-align             Disable align-to-color processing\n"
        << "  --stress               all profiles, 3 cycles, 5 seconds each\n"
        << "  --list-profiles        Print available built-in test profiles\n"
        << "  -h, --help             Show this help\n";
}

static bool read_arg(int argc, char** argv, int& i, std::string* value)
{
    if (i + 1 >= argc)
        return false;
    *value = argv[++i];
    return true;
}

static options parse_options(int argc, char** argv)
{
    options opt;
    for (int i = 1; i < argc; ++i)
    {
        std::string arg(argv[i]);
        std::string value;
        if (arg == "--serial" && read_arg(argc, argv, i, &value))
            opt.serial = value;
        else if (arg == "--profile" && read_arg(argc, argv, i, &value))
            opt.profile = value;
        else if (arg == "--cycles" && read_arg(argc, argv, i, &value))
            opt.cycles = std::max(1, std::atoi(value.c_str()));
        else if (arg == "--duration-sec" && read_arg(argc, argv, i, &value))
            opt.duration_sec = std::max(0.1, std::atof(value.c_str()));
        else if (arg == "--timeout-ms" && read_arg(argc, argv, i, &value))
            opt.timeout_ms = std::max(1, std::atoi(value.c_str()));
        else if (arg == "--pre-stop-drain-ms" && read_arg(argc, argv, i, &value))
            opt.pre_stop_drain_ms = std::max(0, std::atoi(value.c_str()));
        else if (arg == "--pre-stop-settle-ms" && read_arg(argc, argv, i, &value))
            opt.pre_stop_settle_ms = std::max(0, std::atoi(value.c_str()));
        else if (arg == "--cooldown-ms" && read_arg(argc, argv, i, &value))
            opt.cooldown_ms = std::max(0, std::atoi(value.c_str()));
        else if (arg == "--stop-warn-ms" && read_arg(argc, argv, i, &value))
            opt.stop_warn_ms = std::max(1, std::atoi(value.c_str()));
        else if (arg == "--hard-stop-ms" && read_arg(argc, argv, i, &value))
            opt.hard_stop_ms = std::max(1, std::atoi(value.c_str()));
        else if (arg == "--output-dir" && read_arg(argc, argv, i, &value))
            opt.output_dir = value;
        else if (arg == "--no-evidence")
            opt.capture_evidence = false;
        else if (arg == "--no-render")
            opt.render = false;
        else if (arg == "--allow-usb2")
            opt.require_usb3 = false;
        else if (arg == "--stress")
            opt.stress = true;
        else if (arg == "--pointcloud")
            opt.pointcloud = true;
        else if (arg == "--filters")
            opt.filters = true;
        else if (arg == "--no-align")
            opt.align_to_color = false;
        else if (arg == "--list-profiles")
            opt.list_profiles = true;
        else if (arg == "-h" || arg == "--help")
        {
            usage();
            std::exit(EXIT_SUCCESS);
        }
        else
        {
            std::ostringstream ss;
            ss << "Unknown or incomplete option: " << arg;
            throw std::invalid_argument(ss.str());
        }
    }

    if (opt.stress)
    {
        opt.profile = "all";
        opt.cycles = std::max(opt.cycles, 3);
        opt.duration_sec = std::min(opt.duration_sec, 5.0);
        opt.pointcloud = true;
        opt.filters = true;
    }

    return opt;
}

static std::vector<test_profile> built_in_profiles()
{
    return {
        { "vga30", "RGB + depth 640x480@30, align-to-color by default",
            {
                { RS2_STREAM_DEPTH, -1, 640, 480, RS2_FORMAT_Z16, 30 },
                { RS2_STREAM_COLOR, -1, 640, 480, RS2_FORMAT_RGB8, 30 },
            },
            true, true, false },
        { "vga60", "Depth 640x480@60 plus RGB 640x480@30",
            {
                { RS2_STREAM_DEPTH, -1, 640, 480, RS2_FORMAT_Z16, 60 },
                { RS2_STREAM_COLOR, -1, 640, 480, RS2_FORMAT_RGB8, 30 },
            },
            true, true, false },
        { "depth90-ir", "Depth + infrared 848x480@90",
            {
                { RS2_STREAM_DEPTH, -1, 848, 480, RS2_FORMAT_Z16, 90 },
                { RS2_STREAM_INFRARED, 1, 848, 480, RS2_FORMAT_Y8, 90 },
            },
            false, true, false },
        { "hd15", "RGB + depth 1280x720@15",
            {
                { RS2_STREAM_DEPTH, -1, 1280, 720, RS2_FORMAT_Z16, 15 },
                { RS2_STREAM_COLOR, -1, 1280, 720, RS2_FORMAT_RGB8, 15 },
            },
            true, true, false },
    };
}

static long rss_kb()
{
    rusage usage{};
    if (getrusage(RUSAGE_SELF, &usage) == 0)
        return usage.ru_maxrss;
    return 0;
}

static double elapsed_ms(clock_type::time_point a, clock_type::time_point b)
{
    return std::chrono::duration_cast<std::chrono::duration<double, std::milli>>(b - a).count();
}

static double elapsed_sec(clock_type::time_point a, clock_type::time_point b)
{
    return std::chrono::duration_cast<std::chrono::duration<double>>(b - a).count();
}

static std::string timestamp_label()
{
    auto now = std::time(nullptr);
    std::tm tm{};
    localtime_r(&now, &tm);
    char buf[32] = {};
    std::strftime(buf, sizeof(buf), "%Y%m%d-%H%M%S", &tm);
    return buf;
}

static void mkdir_if_needed(const std::string& path)
{
    if (path.empty())
        return;
    if (mkdir(path.c_str(), 0775) != 0 && errno != EEXIST)
    {
        std::ostringstream ss;
        ss << "Failed to create output directory: " << path;
        throw std::runtime_error(ss.str());
    }
}

static std::string create_run_dir(const std::string& base)
{
    if (base.empty())
        return "";
    mkdir_if_needed(base);
    std::ostringstream ss;
    ss << base << "/" << timestamp_label() << "-pid" << getpid();
    auto run_dir = ss.str();
    mkdir_if_needed(run_dir);
    return run_dir;
}

static std::string artifact_path(const options& opt,
                                 const std::string& profile,
                                 int cycle,
                                 const char* suffix)
{
    if (opt.output_dir.empty())
        return "";
    std::ostringstream ss;
    ss << opt.output_dir << "/" << profile << "-cycle" << cycle << suffix;
    return ss.str();
}

static std::string run_log_path(const options& opt)
{
    if (opt.output_dir.empty())
        return "";
    return opt.output_dir + "/run.log";
}

static void append_log_line(const options& opt, const std::string& line)
{
    auto path = run_log_path(opt);
    if (path.empty())
        return;
    std::ofstream out(path.c_str(), std::ios::app);
    if (out)
        out << line << "\n";
}

static void save_frontbuffer_ppm(const std::string& path, int width, int height)
{
    if (path.empty() || width <= 0 || height <= 0)
        return;

    std::vector<unsigned char> pixels(static_cast<size_t>(width) * static_cast<size_t>(height) * 3);
    glPixelStorei(GL_PACK_ALIGNMENT, 1);
    glReadBuffer(GL_FRONT);
    glReadPixels(0, 0, width, height, GL_RGB, GL_UNSIGNED_BYTE, pixels.data());

    std::ofstream out(path.c_str(), std::ios::binary);
    if (!out)
        throw std::runtime_error("Failed to write framebuffer evidence image");

    out << "P6\n" << width << " " << height << "\n255\n";
    const size_t stride = static_cast<size_t>(width) * 3;
    for (int y = height - 1; y >= 0; --y)
        out.write(reinterpret_cast<const char*>(pixels.data() + static_cast<size_t>(y) * stride), stride);
}

static std::string stream_key(const rs2::frame& frame)
{
    auto profile = frame.get_profile();
    std::ostringstream ss;
    ss << profile.stream_name() << "#" << profile.stream_index();
    return ss.str();
}

static void update_stream_counter(cycle_result& result, const rs2::frame& frame)
{
    auto key = stream_key(frame);
    auto& counter = result.streams[key];
    counter.frames++;
    auto number = frame.get_frame_number();
    if (counter.has_last && number > counter.last_number + 1)
        counter.gaps += number - counter.last_number - 1;
    counter.last_number = number;
    counter.has_last = true;
}

static std::string get_info_or(const rs2::device& dev, rs2_camera_info info, const std::string& fallback)
{
    if (dev.supports(info))
        return dev.get_info(info);
    return fallback;
}

static rs2::device select_device(rs2::context& ctx, const std::string& requested_serial)
{
    auto devices = ctx.query_devices();
    if (devices.size() == 0)
        throw std::runtime_error("No RealSense devices were found");

    for (size_t index = 0; index < devices.size(); ++index)
    {
        try
        {
            auto dev = devices[index];
            auto serial = get_info_or(dev, RS2_CAMERA_INFO_SERIAL_NUMBER, "");
            if (requested_serial.empty() || serial == requested_serial)
                return dev;
        }
        catch (const rs2::error& error)
        {
            std::cerr << "device.warning index=" << index << " " << error.what() << "\n";
        }
    }

    std::ostringstream ss;
    ss << "Requested RealSense serial was not found: " << requested_serial;
    throw std::runtime_error(ss.str());
}

static void print_device(const rs2::device& dev)
{
    std::cout << "device.name=" << get_info_or(dev, RS2_CAMERA_INFO_NAME, "unknown") << "\n"
              << "device.serial=" << get_info_or(dev, RS2_CAMERA_INFO_SERIAL_NUMBER, "unknown") << "\n"
              << "device.firmware=" << get_info_or(dev, RS2_CAMERA_INFO_FIRMWARE_VERSION, "unknown") << "\n"
              << "device.usb=" << get_info_or(dev, RS2_CAMERA_INFO_USB_TYPE_DESCRIPTOR, "unknown") << "\n";
}

static bool is_usb3(const rs2::device& dev)
{
    auto usb = get_info_or(dev, RS2_CAMERA_INFO_USB_TYPE_DESCRIPTOR, "");
    return usb.find("3.") != std::string::npos || usb == "3";
}

class stop_watchdog
{
public:
    explicit stop_watchdog(int hard_stop_ms)
        : _timeout(std::chrono::milliseconds(hard_stop_ms))
        , _thread([this](std::stop_token token) { watch(token); })
    {
    }

    ~stop_watchdog()
    {
        complete();
    }

    void complete()
    {
        {
            std::lock_guard<std::mutex> lock(_mutex);
            _done = true;
        }
        _cv.notify_all();
        if (_thread.joinable())
        {
            _thread.request_stop();
            _thread.join();
        }
    }

    stop_watchdog(const stop_watchdog&) = delete;
    stop_watchdog& operator=(const stop_watchdog&) = delete;

private:
    void watch(std::stop_token token)
    {
        std::unique_lock<std::mutex> lock(_mutex);
        const auto completed = _cv.wait_for(lock, _timeout, [&]() {
            return _done || token.stop_requested();
        });
        if (!completed)
        {
            std::cerr << "stop.fatal=pipeline.stop exceeded hard timeout "
                      << _timeout.count() << "ms; terminating to release camera ownership\n";
            std::_Exit(EXIT_FAILURE);
        }
    }

    std::chrono::milliseconds _timeout;
    std::mutex _mutex;
    std::condition_variable_any _cv;
    bool _done = false;
    std::jthread _thread;
};

class pipeline_session
{
public:
    explicit pipeline_session(rs2::context& ctx)
        : _pipe(ctx)
    {
    }

    ~pipeline_session()
    {
        stop_noexcept(15000);
    }

    static rs2::config make_config(const test_profile& profile, const std::string& serial)
    {
        rs2::config cfg;
        if (!serial.empty())
            cfg.enable_device(serial);
        for (auto&& s : profile.streams)
            cfg.enable_stream(s.stream, s.index, s.width, s.height, s.format, s.fps);
        return cfg;
    }

    bool can_resolve(const test_profile& profile, const std::string& serial)
    {
        auto cfg = make_config(profile, serial);
        return cfg.can_resolve(_pipe);
    }

    rs2::pipeline_profile start(const test_profile& profile, const std::string& serial)
    {
        auto cfg = make_config(profile, serial);

        auto active = _pipe.start(cfg);
        _started = true;
        return active;
    }

    bool try_wait(rs2::frameset* frames, int timeout_ms)
    {
        return _pipe.try_wait_for_frames(frames, static_cast<unsigned int>(timeout_ms));
    }

    bool stop_noexcept(int hard_stop_ms)
    {
        if (!_started)
            return true;

        stop_watchdog watchdog(hard_stop_ms);
        try
        {
            _pipe.stop();
            _started = false;
            watchdog.complete();
            return true;
        }
        catch (const std::exception& e)
        {
            std::cerr << "stop.warning=" << e.what() << "\n";
            _started = false;
            watchdog.complete();
            return false;
        }
        catch (...)
        {
            std::cerr << "stop.warning=unknown exception\n";
            _started = false;
            watchdog.complete();
            return false;
        }
    }

private:
    rs2::pipeline _pipe;
    bool _started = false;
};

static std::map<int, rs2::frame> build_render_frames(const rs2::frameset& frames, rs2::colorizer& colorizer)
{
    std::map<int, rs2::frame> out;
    int index = 0;
    if (auto color = frames.get_color_frame())
        out[index++] = color;
    if (auto depth = frames.get_depth_frame())
        out[index++] = depth.apply_filter(colorizer);
    if (auto ir = frames.first_or_default(RS2_STREAM_INFRARED))
        out[index++] = ir;
    return out;
}

static void apply_processing(const test_profile& profile,
                             const options& opt,
                             rs2::frameset& frames,
                             rs2::align& align_to_color,
                             rs2::pointcloud& pc,
                             rs2::decimation_filter& decimation,
                             rs2::spatial_filter& spatial,
                             rs2::temporal_filter& temporal)
{
    const bool do_align = opt.align_to_color && profile.align_to_color;
    if (do_align && frames.get_color_frame() && frames.get_depth_frame())
        frames = align_to_color.process(frames);

    auto depth = frames.get_depth_frame();
    if (!depth)
        return;

    if (opt.pointcloud || profile.pointcloud)
    {
        if (auto color = frames.get_color_frame())
            pc.map_to(color);
        pc.calculate(depth);
    }

    if (opt.filters || profile.filters)
    {
        rs2::frame filtered = depth;
        filtered = decimation.process(filtered);
        filtered = spatial.process(filtered);
        filtered = temporal.process(filtered);
    }
}

static void drain_before_stop(pipeline_session& session,
                              const options& opt,
                              cycle_result& result)
{
    if (opt.pre_stop_drain_ms <= 0)
        return;

    auto drain_begin = clock_type::now();
    auto deadline = drain_begin + std::chrono::milliseconds(opt.pre_stop_drain_ms);
    const auto wait_ms = std::max(1, std::min(opt.timeout_ms, 100));

    while (clock_type::now() < deadline)
    {
        rs2::frameset dropped;
        try
        {
            if (session.try_wait(&dropped, wait_ms))
                ++result.drain_framesets;
            else
                ++result.drain_timeouts;
        }
        catch (const std::exception& e)
        {
            std::cerr << "pre_stop.warning=" << e.what() << "\n";
            ++result.exceptions;
            break;
        }
    }

    result.pre_stop_ms += elapsed_ms(drain_begin, clock_type::now());
}

static cycle_result run_cycle(rs2::context& ctx,
                              const options& opt,
                              const test_profile& profile,
                              int cycle,
                              window* app)
{
    cycle_result result;
    result.profile = profile.name;
    result.cycle = cycle;

    auto serial = opt.serial;
    if (serial.empty())
    {
        auto dev = select_device(ctx, "");
        serial = get_info_or(dev, RS2_CAMERA_INFO_SERIAL_NUMBER, "");
    }

    pipeline_session session(ctx);

    try
    {
        if (!session.can_resolve(profile, serial))
        {
            result.skipped = true;
            result.skip_reason = "profile-not-supported-by-current-device-or-usb-mode";
            return result;
        }

        auto start_begin = clock_type::now();
        auto active = session.start(profile, serial);
        auto start_end = clock_type::now();
        result.start_ms = elapsed_ms(start_begin, start_end);
        result.started = true;
        auto dev = active.get_device();
        result.usb3 = is_usb3(dev);
        if (opt.require_usb3 && !result.usb3)
            throw std::runtime_error("Device is not connected over USB3; use --allow-usb2 only for degraded testing");
    }
    catch (const std::exception& e)
    {
        std::cerr << "start.error profile=" << profile.name << " cycle=" << cycle << " " << e.what() << "\n";
        result.exceptions++;
        return result;
    }

    auto run_begin = clock_type::now();
    {
        rs2::colorizer colorizer;
        rs2::align align_to_color(RS2_STREAM_COLOR);
        rs2::pointcloud pc;
        rs2::decimation_filter decimation;
        rs2::spatial_filter spatial;
        rs2::temporal_filter temporal;

        auto deadline = run_begin + std::chrono::duration_cast<clock_type::duration>(
            std::chrono::duration<double>(opt.duration_sec));

        while (clock_type::now() < deadline)
        {
            try
            {
                rs2::frameset frames;
                auto wait_begin = clock_type::now();
                if (!session.try_wait(&frames, opt.timeout_ms))
                {
                    result.wait_ms.add(elapsed_ms(wait_begin, clock_type::now()));
                    result.timeouts++;
                    continue;
                }
                auto wait_end = clock_type::now();
                result.wait_ms.add(elapsed_ms(wait_begin, wait_end));
                result.framesets++;

                for (auto&& frame : frames)
                    update_stream_counter(result, frame);

                auto process_begin = clock_type::now();
                apply_processing(profile, opt, frames, align_to_color, pc, decimation, spatial, temporal);
                auto process_end = clock_type::now();
                result.process_ms.add(elapsed_ms(process_begin, process_end));

                if (app)
                {
                    auto render_begin = clock_type::now();
                    auto render_frames = build_render_frames(frames, colorizer);
                    app->show(render_frames);
                    bool keep_open = static_cast<bool>(*app);
                    if (opt.capture_evidence && result.evidence_path.empty())
                    {
                        result.evidence_path = artifact_path(opt, profile.name, cycle, ".ppm");
                        save_frontbuffer_ppm(result.evidence_path, static_cast<int>(app->width()), static_cast<int>(app->height()));
                    }
                    auto render_end = clock_type::now();
                    result.render_ms.add(elapsed_ms(render_begin, render_end));
                    if (!keep_open)
                    {
                        result.window_closed = true;
                        break;
                    }
                }
            }
            catch (const std::exception& e)
            {
                std::cerr << "cycle.warning profile=" << profile.name << " cycle=" << cycle << " " << e.what() << "\n";
                result.exceptions++;
                break;
            }
        }

        drain_before_stop(session, opt, result);
    }

    result.wall_sec = elapsed_sec(run_begin, clock_type::now());
    if (opt.pre_stop_settle_ms > 0)
    {
        auto settle_begin = clock_type::now();
        std::this_thread::sleep_for(std::chrono::milliseconds(opt.pre_stop_settle_ms));
        result.pre_stop_ms += elapsed_ms(settle_begin, clock_type::now());
    }

    auto stop_begin = clock_type::now();
    result.stopped_cleanly = session.stop_noexcept(opt.hard_stop_ms);
    auto stop_end = clock_type::now();
    result.stop_ms = elapsed_ms(stop_begin, stop_end);
    if (result.stop_ms > opt.stop_warn_ms)
    {
        result.stop_slow = true;
        result.stopped_cleanly = false;
        result.exceptions++;
    }
    result.rss_kb = rss_kb();

    std::this_thread::sleep_for(std::chrono::milliseconds(opt.cooldown_ms));
    return result;
}

static void print_cycle_result(const cycle_result& r)
{
    const double fps = r.wall_sec > 0.0 ? static_cast<double>(r.framesets) / r.wall_sec : 0.0;
    uint64_t total_gaps = 0;
    for (auto&& kv : r.streams)
        total_gaps += kv.second.gaps;

    std::cout << std::fixed << std::setprecision(2)
              << "RESULT profile=" << r.profile
              << " cycle=" << r.cycle
              << " started=" << (r.started ? "yes" : "no")
              << " skipped=" << (r.skipped ? "yes" : "no")
              << " stopped=" << (r.skipped ? "skipped" : (r.stopped_cleanly ? "clean" : "dirty"))
              << " stop_slow=" << (r.stop_slow ? "yes" : "no")
              << " window_closed=" << (r.window_closed ? "yes" : "no")
              << " usb3=" << (r.usb3 ? "yes" : "no")
              << " framesets=" << r.framesets
              << " fps=" << fps
              << " timeouts=" << r.timeouts
              << " drain_framesets=" << r.drain_framesets
              << " drain_timeouts=" << r.drain_timeouts
              << " gaps=" << total_gaps
              << " exceptions=" << r.exceptions
              << " evidence=" << (r.evidence_path.empty() ? "none" : r.evidence_path)
              << " start_ms=" << r.start_ms
              << " pre_stop_ms=" << r.pre_stop_ms
              << " stop_ms=" << r.stop_ms
              << " wait_ms_mean=" << r.wait_ms.mean()
              << " process_ms_mean=" << r.process_ms.mean()
              << " render_ms_mean=" << r.render_ms.mean()
              << " rss_kb=" << r.rss_kb
              << "\n";
    if (r.skipped)
        std::cout << "SKIP profile=" << r.profile << " cycle=" << r.cycle << " reason=" << r.skip_reason << "\n";

    for (auto&& kv : r.streams)
    {
        std::cout << "STREAM profile=" << r.profile
                  << " cycle=" << r.cycle
                  << " name=" << kv.first
                  << " frames=" << kv.second.frames
                  << " gaps=" << kv.second.gaps
                  << "\n";
    }
}

static std::vector<test_profile> selected_profiles(const options& opt)
{
    auto profiles = built_in_profiles();
    if (opt.profile == "all")
        return profiles;

    std::vector<test_profile> selected;
    for (auto&& p : profiles)
        if (p.name == opt.profile)
            selected.push_back(p);

    if (selected.empty())
    {
        std::ostringstream ss;
        ss << "Unknown profile: " << opt.profile;
        throw std::invalid_argument(ss.str());
    }
    return selected;
}

static std::string result_line(const cycle_result& r)
{
    const double fps = r.wall_sec > 0.0 ? static_cast<double>(r.framesets) / r.wall_sec : 0.0;
    uint64_t total_gaps = 0;
    for (auto&& kv : r.streams)
        total_gaps += kv.second.gaps;

    std::ostringstream ss;
    ss << std::fixed << std::setprecision(2)
       << "RESULT profile=" << r.profile
       << " cycle=" << r.cycle
       << " started=" << (r.started ? "yes" : "no")
       << " skipped=" << (r.skipped ? "yes" : "no")
       << " stopped=" << (r.skipped ? "skipped" : (r.stopped_cleanly ? "clean" : "dirty"))
       << " stop_slow=" << (r.stop_slow ? "yes" : "no")
       << " window_closed=" << (r.window_closed ? "yes" : "no")
       << " usb3=" << (r.usb3 ? "yes" : "no")
       << " framesets=" << r.framesets
       << " fps=" << fps
       << " timeouts=" << r.timeouts
       << " drain_framesets=" << r.drain_framesets
       << " drain_timeouts=" << r.drain_timeouts
       << " gaps=" << total_gaps
       << " exceptions=" << r.exceptions
       << " evidence=" << (r.evidence_path.empty() ? "none" : r.evidence_path)
       << " start_ms=" << r.start_ms
       << " pre_stop_ms=" << r.pre_stop_ms
       << " stop_ms=" << r.stop_ms
       << " wait_ms_mean=" << r.wait_ms.mean()
       << " process_ms_mean=" << r.process_ms.mean()
       << " render_ms_mean=" << r.render_ms.mean()
       << " rss_kb=" << r.rss_kb;
    return ss.str();
}

int main(int argc, char** argv) try
{
    rs2::log_to_console(RS2_LOG_SEVERITY_WARN);

    auto opt = parse_options(argc, argv);
    auto profiles = selected_profiles(opt);

    if (opt.list_profiles)
    {
        for (auto&& profile : built_in_profiles())
            std::cout << profile.name << " - " << profile.description << "\n";
        return EXIT_SUCCESS;
    }

    process_lock lock("/tmp/rs-gb10-profiler.lock");
    opt.output_dir = create_run_dir(opt.output_dir);
    if (!opt.output_dir.empty())
    {
        std::cout << "artifact.dir=" << opt.output_dir << "\n"
                  << "artifact.log=" << run_log_path(opt) << "\n";
        append_log_line(opt, "run.started=" + timestamp_label());
    }

    rs2::context ctx;
    auto device = select_device(ctx, opt.serial);
    auto serial = get_info_or(device, RS2_CAMERA_INFO_SERIAL_NUMBER, "");
    if (opt.serial.empty())
        opt.serial = serial;

    print_device(device);
    if (opt.require_usb3 && !is_usb3(device))
        throw std::runtime_error("Refusing degraded USB2 mode; reconnect to USB3 or pass --allow-usb2");

    std::cout << "test.serial=" << opt.serial << "\n"
              << "test.render=" << (opt.render ? "visible" : "off") << "\n"
              << "test.cycles=" << opt.cycles << "\n"
              << "test.duration_sec=" << opt.duration_sec << "\n";
    append_log_line(opt, "device.name=" + get_info_or(device, RS2_CAMERA_INFO_NAME, "unknown"));
    append_log_line(opt, "device.serial=" + get_info_or(device, RS2_CAMERA_INFO_SERIAL_NUMBER, "unknown"));
    append_log_line(opt, "device.usb=" + get_info_or(device, RS2_CAMERA_INFO_USB_TYPE_DESCRIPTOR, "unknown"));

    std::unique_ptr<window> app;
    if (opt.render)
    {
        app.reset(new window(1280, 720, "RealSense GB10 Profiler"));
        std::cout << "gl.renderer=" << reinterpret_cast<const char*>(glGetString(GL_RENDERER)) << "\n"
                  << "gl.version=" << reinterpret_cast<const char*>(glGetString(GL_VERSION)) << "\n";
    }

    int failures = 0;
    uint64_t total_timeouts = 0;
    uint64_t total_framesets = 0;

    for (auto&& profile : profiles)
    {
        for (int cycle = 1; cycle <= opt.cycles; ++cycle)
        {
            auto result = run_cycle(ctx, opt, profile, cycle, app.get());
            print_cycle_result(result);
            append_log_line(opt, result_line(result));
            for (auto&& kv : result.streams)
            {
                std::ostringstream ss;
                ss << "STREAM profile=" << result.profile
                   << " cycle=" << result.cycle
                   << " name=" << kv.first
                   << " frames=" << kv.second.frames
                   << " gaps=" << kv.second.gaps;
                append_log_line(opt, ss.str());
            }

            total_timeouts += result.timeouts;
            total_framesets += result.framesets;
            if (!result.skipped && (!result.started || !result.stopped_cleanly || result.exceptions != 0 || result.framesets == 0))
                failures++;
            if (result.window_closed)
                break;
        }
        if (app && glfwWindowShouldClose(*app))
            break;
    }

    std::cout << "SUMMARY framesets=" << total_framesets
              << " timeouts=" << total_timeouts
              << " failures=" << failures
              << "\n";
    {
        std::ostringstream ss;
        ss << "SUMMARY framesets=" << total_framesets
           << " timeouts=" << total_timeouts
           << " failures=" << failures;
        append_log_line(opt, ss.str());
    }

    return failures == 0 ? EXIT_SUCCESS : EXIT_FAILURE;
}
catch (const rs2::error& e)
{
    std::cerr << "RealSense error calling " << e.get_failed_function()
              << "(" << e.get_failed_args() << "): " << e.what() << "\n";
    return EXIT_FAILURE;
}
catch (const std::exception& e)
{
    std::cerr << e.what() << "\n";
    return EXIT_FAILURE;
}
