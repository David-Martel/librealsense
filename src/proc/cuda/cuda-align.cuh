#pragma once
#ifdef RS2_USE_CUDA

#include "../../../include/librealsense2/rs.h"
#include <memory>
#include <stdint.h>
#include <stddef.h>

namespace librealsense
{
    class align_cuda_helper
    {
    public:
        align_cuda_helper() :
            _d_depth_in(nullptr),
            _d_other_in(nullptr),
            _d_aligned_out(nullptr),
            _depth_capacity(0),
            _other_capacity(0),
            _aligned_capacity(0),
            _pixel_map_capacity(0) {}

        void align_other_to_depth(unsigned char* h_aligned_out, const uint16_t* h_depth_in,
            float depth_scale, const rs2_intrinsics& h_depth_intrin, const rs2_extrinsics& h_depth_to_other,
            const rs2_intrinsics& h_other_intrin, const unsigned char* h_other_in, rs2_format other_format, int other_bytes_per_pixel);

        void align_depth_to_other(unsigned char* h_aligned_out, const uint16_t* h_depth_in,
            float depth_scale, const rs2_intrinsics& h_depth_intrin, const rs2_extrinsics& h_depth_to_other,
            const rs2_intrinsics& h_other_intrin);

    private:
        std::shared_ptr<uint16_t>       _d_depth_in;
        std::shared_ptr<unsigned char>  _d_other_in;
        std::shared_ptr<unsigned char>  _d_aligned_out;
        std::shared_ptr<int2>           _d_pixel_map;
        size_t                          _depth_capacity;
        size_t                          _other_capacity;
        size_t                          _aligned_capacity;
        size_t                          _pixel_map_capacity;

        std::shared_ptr<rs2_intrinsics> _d_other_intrinsics;
        std::shared_ptr<rs2_intrinsics> _d_depth_intrinsics;
        std::shared_ptr<rs2_extrinsics> _d_depth_other_extrinsics;
    };
}
#endif // RS2_USE_CUDA
