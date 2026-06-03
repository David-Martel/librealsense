// License: Apache 2.0. See LICENSE file in root directory.
// Copyright(c) 2015 RealSense, Inc. All Rights Reserved.

#include "device-libusb.h"
#include "types.h"
#include "../usb-tuning.h"

#include <fstream>
#include <mutex>   // Required for std::once_flag and std::call_once.
#include <unordered_map>   // P7 re-acquire guard: per-process device acquire counter.
#include <stdexcept>       // P7 re-acquire guard: std::runtime_error on REFUSE.
#include <cstdlib>         // P7 re-acquire guard: std::getenv for the refuse opt-in.

namespace
{
    // P3 advisory: warn once per process if /sys/module/usbcore/parameters/usbfs_memory_mb
    // is below the threshold recommended for D400 high-bandwidth multistream on the RSUSB
    // (libusb) backend.  On NVIDIA DGX Spark / GB10 the default 16 MB budget causes
    // control-path starvation under three concurrent saturating bulk streams.  We only
    // read the sysfs value — never write it (config must stay inspectable / versioned).
    //
    // Gated behind RS2_GB10_USB_TUNING so it is fully opt-in: a stock upstream build is
    // unchanged (no sysfs read, no log line) on machines that have not opted into the
    // GB10 tuning profile.
    void check_usbfs_memory_mb_once()
    {
#if defined(__linux__) && defined(RS2_GB10_USB_TUNING) && RS2_GB10_USB_TUNING
        static std::once_flag s_checked;
        std::call_once( s_checked, []()
        {
            std::ifstream f( "/sys/module/usbcore/parameters/usbfs_memory_mb" );
            long current_mb = 0;
            if( f >> current_mb )
            {
                auto msg = librealsense::usb_tuning::usbfs_memory_advice( current_mb, 256 );
                if( !msg.empty() )
                    LOG_WARNING( msg );
            }
            // If the file is missing or unreadable (e.g. usbcore built-in, non-Linux VM),
            // extraction fails and we silently skip — no warning, no throw.
        } );
#endif  // __linux__ && RS2_GB10_USB_TUNING
    }

    // P7 re-acquire guard: detect the controller-death #2 "churn" — an app that destroys
    // and recreates its rs2::context/pipeline between streams fully releases the device
    // back to kernel uvcvideo, which re-probes the UVC control endpoints and can wedge the
    // GB10 xHCI. usb_device_libusb is built once PER SENSOR (depth, color, ...) within one
    // legitimate session, all keyed by the device's USB bus-port unique_id and all alive
    // at once — so we must NOT fire on "constructed before". We track LIVE instances per
    // device id: a dangerous re-acquire is constructing one when the live count is 0 (every
    // prior handle destroyed) AND the device was acquired before — the destroy-all-then-
    // reacquire churn. Then warn (default) or, with RS2_GB10_REFUSE_REACQUIRE, throw. We
    // never touch the kernel binding (the contraindicated P1/P5). RSUSB-only path.
    //
    // Gated behind RS2_GB10_USB_TUNING so a stock upstream build is byte-identical.
#if defined(RS2_GB10_USB_TUNING) && RS2_GB10_USB_TUNING
    std::mutex& reacquire_mutex() { static std::mutex m; return m; }
    std::unordered_map< std::string, int >& reacquire_live()  { static std::unordered_map< std::string, int > m; return m; }
    std::unordered_map< std::string, int >& reacquire_total() { static std::unordered_map< std::string, int > m; return m; }

    // Read-only decision at construction start. May throw (REFUSE) — which aborts the
    // construction before any commit, keeping the live/total counters balanced.
    void check_device_reacquire( const std::string& device_id )
    {
        int live_before = 0, total_before = 0;
        {
            std::lock_guard< std::mutex > lock( reacquire_mutex() );
            live_before  = reacquire_live()[device_id];
            total_before = reacquire_total()[device_id];
        }

        const bool dangerous = librealsense::usb_tuning::is_dangerous_reacquire( live_before, total_before );

        const char* refuse_env = std::getenv( "RS2_GB10_REFUSE_REACQUIRE" );
        const bool refuse_opt_in = refuse_env && *refuse_env && std::string( refuse_env ) != "0";

        const auto action = librealsense::usb_tuning::resolve_reacquire_action(
            dangerous, librealsense::usb_tuning::GB10_TUNING_ENABLED, refuse_opt_in );

        if( action == librealsense::usb_tuning::reacquire_action::allow )
            return;

        const auto advice = librealsense::usb_tuning::reacquire_advice( device_id, total_before );
        if( action == librealsense::usb_tuning::reacquire_action::refuse )
            throw std::runtime_error( advice );
        LOG_WARNING( advice );
    }

    // Register a live instance — called at the END of a successful construction so it
    // pairs 1:1 with the destructor's release (a ctor that throws never commits).
    void commit_device_acquire( const std::string& device_id )
    {
        std::lock_guard< std::mutex > lock( reacquire_mutex() );
        reacquire_live()[device_id]++;
        reacquire_total()[device_id]++;
    }

    // Drop a live instance — called from the destructor.
    void release_device_acquire( const std::string& device_id )
    {
        std::lock_guard< std::mutex > lock( reacquire_mutex() );
        auto& live = reacquire_live()[device_id];
        if( live > 0 )
            --live;
    }

    // Stable per-device key: USB bus-port path, falling back to the descriptor serial.
    std::string reacquire_device_id( const librealsense::platform::usb_device_info& info )
    {
        return info.unique_id.empty() ? info.serial : info.unique_id;
    }
#endif  // RS2_GB10_USB_TUNING
} // anonymous namespace

namespace librealsense
{
    namespace platform
    {
        usb_device_libusb::usb_device_libusb(libusb_device* device, const libusb_device_descriptor& desc, const usb_device_info& info, std::shared_ptr<usb_context> context) :
                _device(device), _usb_device_descriptor(desc), _info(info), _context(context)
        {
            // Advisory preflight: emit a one-time LOG_WARNING if usbfs_memory_mb is too low.
            check_usbfs_memory_mb_once();

            // P7: detect a dangerous mid-session device re-acquire (controller-death #2
            // churn) before doing any work. May throw under RS2_GB10_REFUSE_REACQUIRE.
#if defined(RS2_GB10_USB_TUNING) && RS2_GB10_USB_TUNING
            check_device_reacquire( reacquire_device_id( info ) );
#endif

            usb_descriptor ud = {desc.bLength, desc.bDescriptorType, std::vector<uint8_t>(desc.bLength)};
            memcpy(ud.data.data(), &desc, desc.bLength);
            _descriptors.push_back(ud);

            for (uint8_t c = 0; c < desc.bNumConfigurations; ++c)
            {
                libusb_config_descriptor *config{};
                auto ret = libusb_get_config_descriptor(device, c, &config);
                if (LIBUSB_SUCCESS != ret)
                {
                    LOG_WARNING("failed to read USB config descriptor: error = " << std::dec << ret);
                    continue;
                }

                std::shared_ptr<usb_interface_libusb> curr_ctrl_intf;
                for (uint8_t i = 0; i < config->bNumInterfaces; ++i)
                {
                    auto inf = config->interface[i];
                    auto curr_inf = std::make_shared<usb_interface_libusb>(inf);
                    _interfaces.push_back(curr_inf);
                    switch (inf.altsetting->bInterfaceClass)
                    {
                        case RS2_USB_CLASS_VIDEO:
                        {
                            if(inf.altsetting->bInterfaceSubClass == RS2_USB_SUBCLASS_VIDEO_CONTROL)
                                curr_ctrl_intf = curr_inf;
                            if(inf.altsetting->bInterfaceSubClass == RS2_USB_SUBCLASS_VIDEO_STREAMING && curr_ctrl_intf)
                                curr_ctrl_intf->add_associated_interface(curr_inf);
                            break;
                        }
                        default:
                            break;
                    }
                    for(int j = 0; j < inf.num_altsetting; j++)
                    {
                        auto d = inf.altsetting[j];
                        usb_descriptor ud = {d.bLength, d.bDescriptorType, std::vector<uint8_t>(d.bLength)};
                        memcpy(ud.data.data(), &d, d.bLength);
                        _descriptors.push_back(ud);
                        for(int k = 0; k < d.extra_length; )
                        {
                            auto l = d.extra[k];
                            auto dt = d.extra[k+1];
                            usb_descriptor ud = {l, dt, std::vector<uint8_t>(l)};
                            memcpy(ud.data.data(), &d.extra[k], l);
                            _descriptors.push_back(ud);
                            k += l;
                        }
                    }
                }

                libusb_free_config_descriptor(config);
            }
            libusb_ref_device(_device);

            // P7: register this live instance only after a fully successful construction,
            // so it pairs 1:1 with the destructor's release (a throwing ctor never commits).
#if defined(RS2_GB10_USB_TUNING) && RS2_GB10_USB_TUNING
            commit_device_acquire( reacquire_device_id( info ) );
#endif
        }

        usb_device_libusb::~usb_device_libusb()
        {
#if defined(RS2_GB10_USB_TUNING) && RS2_GB10_USB_TUNING
            release_device_acquire( reacquire_device_id( _info ) );
#endif
            libusb_unref_device(_device);
        }

        const rs_usb_interface usb_device_libusb::get_interface(uint8_t interface_number) const
        {
            auto it = std::find_if(_interfaces.begin(), _interfaces.end(),
                                   [interface_number](const rs_usb_interface& i) { return interface_number == i->get_number(); });
            if (it == _interfaces.end())
                return nullptr;
            return *it;
        }

        std::shared_ptr<handle_libusb> usb_device_libusb::get_handle(uint8_t interface_number)
        {
            try
            {
                auto i = get_interface(interface_number);
                if (!i)
                    return nullptr;
                auto intf = std::dynamic_pointer_cast<usb_interface_libusb>(i);
                return std::make_shared<handle_libusb>(_context, _device, intf);
            }
            catch(const std::exception& e)
            {
                return nullptr;
            }
        }

        const std::shared_ptr<usb_messenger> usb_device_libusb::open(uint8_t interface_number)
        {
            auto h = get_handle(interface_number);
            if(!h)
                return nullptr;
            return std::make_shared<usb_messenger_libusb>(shared_from_this(), h);
        }
    }
}
