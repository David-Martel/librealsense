// License: Apache 2.0. See LICENSE file in root directory.
// Copyright(c) 2019 RealSense, Inc. All Rights Reserved.

#pragma once

#include "libusb.h"
#include "../usb/usb-request.h"
#include "../usb/usb-device.h"

#include <atomic>


namespace librealsense
{
    namespace platform
    {
        class usb_request_libusb : public usb_request_base
        {
        public:
            usb_request_libusb(libusb_device_handle *dev_handle, rs_usb_endpoint endpoint);
            virtual ~usb_request_libusb();
            
            virtual int get_actual_length() const override;
            virtual void* get_native_request() const override;

            std::shared_ptr<usb_request> get_shared() const;
            void set_shared(const std::shared_ptr<usb_request>& shared);
            void set_active(bool state);

        protected:
            virtual void set_native_buffer_length(int length) override;
            virtual int get_native_buffer_length() override;
            virtual void set_native_buffer(uint8_t* buffer) override;
            virtual uint8_t* get_native_buffer() const override;

        private:
            // F5: written by the libusb event thread (set_active(false) in the completion callback) and
            // polled by the cancel/wait path (while(_active && attempts--)) on another thread — a non-atomic
            // bool here is a data race (UB). atomic<bool> makes every access well-defined; the poll logic is
            // unchanged. (Pure correctness fix, not GB10-specific.)
            std::atomic<bool> _active{ false };
            std::weak_ptr<usb_request> _shared;
            std::shared_ptr<libusb_transfer> _transfer;
        };
    }
}
