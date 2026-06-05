// License: Apache 2.0. See LICENSE file in root directory.
// Copyright(c) 2015 RealSense, Inc. All Rights Reserved.

#include "uvc-streamer.h"
#include "../usb-tuning.h"
// GB10 Stop-Endpoint mitigation default (P4a watchdog rate-limit) comes from
// usb-tuning.h. With RS2_GB10_USB_TUNING undefined the watchdog path below is the
// original unconditional reset_endpoint — byte-identical to upstream.

const int UVC_PAYLOAD_MAX_HEADER_LENGTH         = 1024;
const int DEQUEUE_MILLISECONDS_TIMEOUT          = 50;
const int ENDPOINT_RESET_MILLISECONDS_TIMEOUT   = 100;

void cleanup_frame(backend_frame *ptr) {
    if (ptr) ptr->owner->deallocate(ptr);
}

namespace librealsense
{
    namespace platform
    {
        uvc_streamer::uvc_streamer(uvc_streamer_context context) :
            _context(context), _action_dispatcher(10)
        {
            auto inf = context.usb_device->get_interface(context.control->bInterfaceNumber);
            if (inf == nullptr)
                throw std::runtime_error("can't find UVC streaming interface of device: " + context.usb_device->get_info().id);
            _read_endpoint = inf->first_endpoint(platform::RS2_USB_ENDPOINT_DIRECTION_READ);

            _read_buff_length = UVC_PAYLOAD_MAX_HEADER_LENGTH + _context.control->dwMaxVideoFrameSize;
            LOG_INFO("endpoint " << (int)_read_endpoint->get_address() << " read buffer size: " << std::dec <<_read_buff_length);

            _action_dispatcher.start();

            _watchdog_timeout = static_cast<int64_t>(((1000.0 / _context.profile.fps) * 10));

            init();
        }

        uvc_streamer::~uvc_streamer()
        {
            flush();
        }

        void uvc_process_bulk_payload(backend_frame_ptr fp, size_t payload_len, backend_frames_queue& queue) {

            /* ignore empty payload transfers */
            if (!fp || payload_len < 2)
                return;

            uint8_t header_len = fp->pixels[0];
            uint8_t header_info = fp->pixels[1];

            size_t data_len = payload_len - header_len;

            if (header_info & 0x40)
            {
                LOG_ERROR("bad packet: error bit set");
                return;
            }
            if (header_len > payload_len)
            {
                LOG_ERROR("bogus packet: actual_len=" << payload_len << ", header_len=" << header_len);
                return;
            }


            LOG_DEBUG("Passing packet to user CB with size " << (data_len + header_len));
            librealsense::platform::frame_object fo{ data_len, header_len,
                                                     fp->pixels.data() + header_len , fp->pixels.data() };
            fp->fo = fo;

            queue.enqueue(std::move(fp));
        }

        void uvc_streamer::init()
        {
            _frames_archive = std::make_shared<backend_frames_archive>();
            // Get all pointers from archive and initialize their content
            std::vector<backend_frame *> frames;
            for (auto i = 0; i < _frames_archive->CAPACITY; i++) {
                auto ptr = _frames_archive->allocate();
                ptr->pixels.resize(_read_buff_length, 0);
                ptr->owner = _frames_archive.get();
                frames.push_back(ptr);
            }

            for (auto ptr : frames) {
                _frames_archive->deallocate(ptr);
            }

            _publish_frame_thread = std::make_shared<active_object<>>([this](dispatcher::cancellable_timer cancellable_timer)
            {
                backend_frame_ptr fp(nullptr, [](backend_frame *) {});
                if (_queue.dequeue(&fp, DEQUEUE_MILLISECONDS_TIMEOUT))
                {
                    if(_publish_frames && running())
                        _context.user_cb(_context.profile, fp->fo, []() mutable {});
                }
            });

            _watchdog = std::make_shared<watchdog>([this]()
             {
                 _action_dispatcher.invoke([this](dispatcher::cancellable_timer c)
                   {
                       if(!_running || !_frame_arrived)
                           return;

#if defined(RS2_GB10_USB_TUNING) && RS2_GB10_USB_TUNING
                       // P4a: rate-limit the watchdog's Stop-Endpoint command so a transient
                       // stall under 3-stream saturating load does not pile onto the GB10 xHCI
                       // control-path (the command the controller dies on). Suppress resets that
                       // arrive faster than WATCHDOG_MIN_RESET_INTERVAL.
                       uint64_t now_ms = static_cast<uint64_t>(
                           std::chrono::duration_cast<std::chrono::milliseconds>(
                               std::chrono::steady_clock::now().time_since_epoch()).count());
                       if(librealsense::usb_tuning::watchdog_should_reset(
                               now_ms, _last_reset_ms, librealsense::usb_tuning::WATCHDOG_MIN_RESET_INTERVAL))
                       {
                           LOG_ERROR("uvc streamer watchdog triggered on endpoint: " << (int)_read_endpoint->get_address());
                           _context.messenger->reset_endpoint(_read_endpoint, ENDPOINT_RESET_MILLISECONDS_TIMEOUT);
                           _last_reset_ms = now_ms;
                           _frame_arrived = false;
                       }
                       else
                       {
                           LOG_DEBUG("watchdog reset suppressed (rate-limited)");
                       }
#else
                       // Upstream behavior: reset the endpoint on every watchdog trigger.
                       LOG_ERROR("uvc streamer watchdog triggered on endpoint: " << (int)_read_endpoint->get_address());
                       _context.messenger->reset_endpoint(_read_endpoint, ENDPOINT_RESET_MILLISECONDS_TIMEOUT);
                       _frame_arrived = false;
#endif
                   });
             }, _watchdog_timeout);

            _watchdog->start();

            _request_callback = std::make_shared<usb_request_callback>([this](platform::rs_usb_request r)
            {
                _action_dispatcher.invoke([this, r](dispatcher::cancellable_timer)
                {
                    if(!_running)
                      return;

                    auto al = r->get_actual_length();
                    // Relax the frame size constrain for compressed streams
                    bool is_compressed = val_in_range(_context.profile.format, { 0x4d4a5047U , 0x5a313648U}); // MJPEG, Z16H
                    if(al > 0L && ((al == r->get_buffer().data()[0] + _context.control->dwMaxVideoFrameSize) || is_compressed ))
                    {
                        auto f = backend_frame_ptr(_frames_archive->allocate(), &cleanup_frame);
                        if(f)
                        {
                            _frame_arrived = true;
                            _watchdog->kick();
                            memcpy(f->pixels.data(), r->get_buffer().data(), r->get_buffer().size());
                            uvc_process_bulk_payload(std::move(f), r->get_actual_length(), _queue);
                        }
                    }

                    auto sts = _context.messenger->submit_request(r);
                    if(sts != platform::RS2_USB_STATUS_SUCCESS)
                        LOG_ERROR("failed to submit UVC request, error: " << sts);
                });
            });

            _requests = std::vector<rs_usb_request>(_context.request_count);
            for(auto&& r : _requests)
            {
                r = _context.messenger->create_request(_read_endpoint);
                r->set_buffer(std::vector<uint8_t>(_read_buff_length));
                r->set_callback(_request_callback);
            }
        }

        void uvc_streamer::start()
        {
            _action_dispatcher.invoke_and_wait([this](dispatcher::cancellable_timer c)
            {
                if(_running)
                    return;

                _context.messenger->reset_endpoint(_read_endpoint, RS2_USB_ENDPOINT_DIRECTION_READ);

                {
                    std::lock_guard<std::mutex> lock(_running_mutex);
                    _running = true;
                }

                for(auto&& r : _requests)
                {
                    auto sts = _context.messenger->submit_request(r);
                    if(sts != platform::RS2_USB_STATUS_SUCCESS)
                        throw std::runtime_error("failed to submit UVC request while start streaming");
                }

                _publish_frame_thread->start();

            }, [this](){ return _running; });
        }

        void uvc_streamer::stop()
        {
            _action_dispatcher.invoke_and_wait([this](dispatcher::cancellable_timer c)
            {
                if(!_running)
                    return;

                _request_callback->cancel();

                _watchdog->stop();

                _frames_archive->stop_allocation();

                _queue.clear();

                for(auto&& r : _requests)
                  _context.messenger->cancel_request(r);

                _requests.clear();

                _frames_archive->wait_until_empty();

#if !(defined(RS2_GB10_USB_TUNING) && RS2_GB10_USB_TUNING)
                // Upstream resets (libusb_clear_halt) the read endpoint on every stop — byte-identical here.
                _context.messenger->reset_endpoint(_read_endpoint, RS2_USB_ENDPOINT_DIRECTION_READ);
#else
                // H1 (GB10 SAFE-STOP): SKIP the read-endpoint reset on stop. clear_halt is a
                // Stop-Endpoint-arming control transfer; issuing it during the immediate post-stop
                // teardown — which on the GB10 xHCI can race a -110 control timeout (the lethal sequence,
                // FINDINGS death #3) — is exactly what vigil's restart_pipeline() stop->start recovery hits
                // every cycle. We RELOCATE the clear_halt to start()'s unconditional reset_endpoint (line
                // 184): that moves it OUT of the post-error teardown window and INTO the settled start
                // window (after the recovery's stop + ~0.5s + fresh-pipeline rebuild), when the controller
                // has calmed. Correctness holds because start() always clears the endpoint before the first
                // transfer, and nothing transfers on _read_endpoint between stop and start (control is on
                // ep0), so a momentarily-halted read endpoint in that window is inert. One fewer armed
                // Stop-Endpoint per stream per teardown, issued only after traffic has settled.
#endif

                _publish_frame_thread->stop();

                {
                    std::lock_guard<std::mutex> lock(_running_mutex);
                    _running = false;
                    _stopped_cv.notify_one();
                }

            }, [this](){ return !_running; });
        }

        void uvc_streamer::flush()
        {
            if(_running)
                stop();

            // synchronized so do not destroy shared pointers while it's still being running
            {
                std::unique_lock<std::mutex> lock(_running_mutex);
                _stopped_cv.wait_for(lock, std::chrono::seconds(1), [&]() { return !_running; });
            }

            _read_endpoint.reset();

            _watchdog.reset();
            _publish_frame_thread.reset();
            _request_callback.reset();

            _frames_archive.reset();

            _action_dispatcher.stop();
        }

        bool uvc_streamer::wait_for_first_frame(uint32_t timeout_ms)
        {
            auto start = std::chrono::system_clock::now();
            while(!_frame_arrived)
            {
                auto end = std::chrono::system_clock::now();
                auto duration = std::chrono::duration_cast<std::chrono::milliseconds>(end - start).count();
                if(duration > timeout_ms)
                    break;
            }
            return _frame_arrived;
        }
    }
}
