#ifdef RS2_USE_CUDA

#include "cuda-align.cuh"
#include "../../../include/librealsense2/rsutil.h"
#include "../../cuda/rscuda_utils.cuh"
#include <rsutils/easylogging/easyloggingpp.h>

// CUDA headers
#include <cuda_runtime.h>
#include <stdexcept>
#include <string>

#ifdef _MSC_VER 
// Add library dependencies if using VS
#pragma comment(lib, "cudart_static")
#endif

using namespace librealsense;
using namespace rscuda;

namespace
{
    constexpr int ALIGN_BLOCK_X = THREADS_IN_WARP; // warp size so a warp's lanes hit consecutive image-x pixels (coalesced reads).
    constexpr int ALIGN_BLOCK_Y = 4;               // 4 chosen empirically, best run times on tested platforms (~12 blocks/SM at 33 regs/thread)
}

template<int N> struct bytes { unsigned char b[N]; };

namespace
{
    void cuda_or_throw(cudaError_t result, const char* what)
    {
        if (result != cudaSuccess)
        {
            std::string message = std::string("CUDA align failure (") + what + "): "
                + cudaGetErrorString(result);
            LOG_ERROR(message);
            throw std::runtime_error(message);
        }
    }

    template<typename T>
    void ensure_dev_buffer(std::shared_ptr<T>& buffer, size_t& capacity, size_t elements)
    {
        if (!buffer || capacity < elements)
        {
            buffer = alloc_dev<T>(static_cast<int>(elements));
            capacity = elements;
        }
    }

    template<typename T>
    void refresh_device_copy(std::shared_ptr<T>& buffer, const T& value, const char* what)
    {
        if (!buffer)
            buffer = alloc_dev<T>(1);
        cuda_or_throw(cudaMemcpy(buffer.get(), &value, sizeof(T), cudaMemcpyHostToDevice), what);
    }
}

int calc_block_size(int pixel_count, int thread_count)
{
    return ((pixel_count % thread_count) == 0) ? (pixel_count / thread_count) : (pixel_count / thread_count + 1);
}

__device__ void kernel_transfer_pixels(int2* mapped_pixels, const rs2_intrinsics* depth_intrin,
    const rs2_intrinsics* other_intrin, const rs2_extrinsics* depth_to_other, float depth_val, int depth_x, int depth_y, int block_index)
{
    float shift = block_index ? 0.5 : -0.5;
    auto depth_size = depth_intrin->width * depth_intrin->height;
    auto mapped_index = block_index * depth_size + (depth_y * depth_intrin->width + depth_x);

    if (mapped_index >= depth_size * 2)
        return;

    // Skip over depth pixels with the value of zero, we have no depth data so we will not write anything into our aligned images
    if (depth_val == 0)
    {
        mapped_pixels[mapped_index] = { -1, -1 };
        return;
    }

    //// Map the top-left corner of the depth pixel onto the other image
    float depth_pixel[2] = { depth_x + shift, depth_y + shift }, depth_point[3], other_point[3], other_pixel[2];
    rscuda::rs2_deproject_pixel_to_point(depth_point, depth_intrin, depth_pixel, depth_val);
    rscuda::rs2_transform_point_to_point(other_point, depth_to_other, depth_point);
    rscuda::rs2_project_point_to_pixel(other_pixel, other_intrin, other_point);
    mapped_pixels[mapped_index].x = static_cast<int>(other_pixel[0] + 0.5f);
    mapped_pixels[mapped_index].y = static_cast<int>(other_pixel[1] + 0.5f);
}

__device__ void atomic_min_uint16(uint16_t* address, uint16_t value)
{
    auto base_address = reinterpret_cast<unsigned int*>(reinterpret_cast<size_t>(address) & ~size_t(2));
    auto high_word = (reinterpret_cast<size_t>(address) & size_t(2)) != 0;
    unsigned int old_value = *base_address;
    unsigned int assumed_value;
    do
    {
        assumed_value = old_value;
        uint16_t current = high_word ?
            static_cast<uint16_t>(assumed_value >> 16) :
            static_cast<uint16_t>(assumed_value & 0xffff);
        if (current <= value)
            return;
        unsigned int replacement = high_word ?
            ((assumed_value & 0x0000ffff) | (static_cast<unsigned int>(value) << 16)) :
            ((assumed_value & 0xffff0000) | static_cast<unsigned int>(value));
        old_value = atomicCAS(base_address, assumed_value, replacement);
    } while (old_value != assumed_value);
}

__global__  void kernel_map_depth_to_other(int2* mapped_pixels, const uint16_t* depth_in, const rs2_intrinsics* depth_intrin, const rs2_intrinsics* other_intrin,
    const rs2_extrinsics* depth_to_other, float depth_scale)
{
    int depth_x = blockIdx.x * blockDim.x + threadIdx.x;
    int depth_y = blockIdx.y * blockDim.y + threadIdx.y;

    if (depth_x >= depth_intrin->width || depth_y >= depth_intrin->height)
        return;

    int depth_pixel_index = depth_y * depth_intrin->width + depth_x;
    float depth_val = depth_in[depth_pixel_index] * depth_scale;
    kernel_transfer_pixels(mapped_pixels, depth_intrin, other_intrin, depth_to_other, depth_val, depth_x, depth_y, blockIdx.z);
}

template<int BPP>
__global__  void kernel_other_to_depth(unsigned char* aligned, const unsigned char* other, const int2* mapped_pixels, const rs2_intrinsics* depth_intrin, const rs2_intrinsics* other_intrin)
{
    // Cache intrinsic dimensions in registers; the kernel uses them many times (loop bounds, indexing)
    // reading via global pointer each time is inefficient, caching them in registers speeds up the kernel significantly.
    const int depth_w = depth_intrin->width;
    const int depth_h = depth_intrin->height;
    const int other_w = other_intrin->width;
    const int other_h = other_intrin->height;
    const int depth_size = depth_w * depth_h;

    const int depth_x = blockIdx.x * blockDim.x + threadIdx.x;
    const int depth_y = blockIdx.y * blockDim.y + threadIdx.y;
    if (depth_x >= depth_w || depth_y >= depth_h)
        return;

    const int depth_pixel_index = depth_y * depth_w + depth_x;

    const int2 p0 = mapped_pixels[depth_pixel_index];
    const int2 p1 = mapped_pixels[depth_size + depth_pixel_index];

    if (p0.x < 0 || p0.y < 0 || p1.x >= other_w || p1.y >= other_h)
        return;

    // Copy the pixel value from the other image to the aligned output at depth_pixel_index.
    // Originally looped over mapped rectangle but only the last iteration's value (bottom-right corner, p1) survived.
    // Skip the loop and do a single write, guarded by p1 >= p0 to preserve the "no iterations -> no write" edge case.
    if (p1.x >= p0.x && p1.y >= p0.y)
    {
        auto in_other = (const bytes<BPP> *)(other);
        auto out_other = (bytes<BPP> *)(aligned);
        out_other[depth_pixel_index] = in_other[p1.y * other_w + p1.x];
    }
}

__global__  void kernel_depth_to_other(uint16_t* aligned_out, const uint16_t* depth_in, const int2* mapped_pixels, const rs2_intrinsics* depth_intrin, const rs2_intrinsics* other_intrin)
{
    // Cache intrinsic dimensions in registers (see kernel_other_to_depth for rationale).
    const int depth_w = depth_intrin->width;
    const int depth_h = depth_intrin->height;
    const int other_w = other_intrin->width;
    const int other_h = other_intrin->height;
    const int depth_size = depth_w * depth_h;

    const int depth_x = blockIdx.x * blockDim.x + threadIdx.x;
    const int depth_y = blockIdx.y * blockDim.y + threadIdx.y;
    if (depth_x >= depth_w || depth_y >= depth_h)
        return;

    const int depth_pixel_index = depth_y * depth_w + depth_x;

    const int2 p0 = mapped_pixels[depth_pixel_index];
    const int2 p1 = mapped_pixels[depth_size + depth_pixel_index];

    uint16_t new_val = depth_in[depth_pixel_index];
    if (!new_val)
        return;

    if (p0.x < 0 && p1.x < 0)
        return;
    if (p0.y < 0 && p1.y < 0)
        return;
    if (p0.x >= other_w && p1.x >= other_w)
        return;
    if (p0.y >= other_h && p1.y >= other_h)
        return;

    // Transfer between the depth pixels and the pixels inside the rectangle on the other image
    int x0 = max(0, min(p0.x, p1.x));
    int y0 = max(0, min(p0.y, p1.y));
    int x1 = min(other_w - 1, max(p0.x, p1.x));
    int y1 = min(other_h - 1, max(p0.y, p1.y));
    for (int y = y0; y <= y1; ++y)
    {
        for (int x = x0; x <= x1; ++x)
        {
            auto other_pixel_index = y * other_w + x;
            atomic_min_uint16(&aligned_out[other_pixel_index], new_val);
        }
    }
}

__global__  void kernel_replace_to_zero(uint16_t* aligned_out, const rs2_intrinsics* other_intrin)
{
    int x = blockIdx.x * blockDim.x + threadIdx.x;
    int y = blockIdx.y * blockDim.y + threadIdx.y;

    if (x >= other_intrin->width || y >= other_intrin->height)
        return;

    auto other_pixel_index = y * other_intrin->width + x;
    if (aligned_out[other_pixel_index] == 0xffff)
        aligned_out[other_pixel_index] = 0;
}

void align_cuda_helper::align_other_to_depth(unsigned char* h_aligned_out, const uint16_t* h_depth_in,
    float depth_scale, const rs2_intrinsics& h_depth_intrin, const rs2_extrinsics& h_depth_to_other,
    const rs2_intrinsics& h_other_intrin, const unsigned char* h_other_in, rs2_format other_format, int other_bytes_per_pixel)
{
    int depth_pixel_count = h_depth_intrin.width * h_depth_intrin.height;
    int other_pixel_count = h_other_intrin.width * h_other_intrin.height;
    int depth_size = depth_pixel_count * 2;
    int other_size = other_pixel_count * other_bytes_per_pixel;
    int aligned_pixel_count = depth_pixel_count;
    int aligned_size = aligned_pixel_count * other_bytes_per_pixel;

    // allocate and copy objects to cuda device memory
    refresh_device_copy(_d_depth_intrinsics, h_depth_intrin, "H2D depth intrinsics");
    refresh_device_copy(_d_other_intrinsics, h_other_intrin, "H2D other intrinsics");
    refresh_device_copy(_d_depth_other_extrinsics, h_depth_to_other, "H2D depth-to-other extrinsics");

    ensure_dev_buffer(_d_depth_in, _depth_capacity, static_cast<size_t>(aligned_pixel_count));
    cuda_or_throw(cudaMemcpy(_d_depth_in.get(), h_depth_in, depth_size, cudaMemcpyHostToDevice), "H2D depth");

    ensure_dev_buffer(_d_other_in, _other_capacity, static_cast<size_t>(other_size));
    cuda_or_throw(cudaMemcpy(_d_other_in.get(), h_other_in, other_size, cudaMemcpyHostToDevice), "H2D other");

    ensure_dev_buffer(_d_aligned_out, _aligned_capacity, static_cast<size_t>(aligned_size));
    cuda_or_throw(cudaMemset(_d_aligned_out.get(), 0, aligned_size), "clear aligned other-to-depth");

    ensure_dev_buffer(_d_pixel_map, _pixel_map_capacity, static_cast<size_t>(depth_pixel_count * 2));

    dim3 block(ALIGN_BLOCK_X, ALIGN_BLOCK_Y);
    dim3 depth_blocks(calc_block_size(h_depth_intrin.width, block.x), calc_block_size(h_depth_intrin.height, block.y));
    dim3 mapping_blocks(depth_blocks.x, depth_blocks.y, 2);

    kernel_map_depth_to_other <<<mapping_blocks,block>>> (_d_pixel_map.get(), _d_depth_in.get(), _d_depth_intrinsics.get(), _d_other_intrinsics.get(),
        _d_depth_other_extrinsics.get(), depth_scale);
    cuda_or_throw(cudaGetLastError(), "map depth to other launch");

    switch (other_bytes_per_pixel)
    {
    case 1: kernel_other_to_depth<1> <<<depth_blocks,block>>> (_d_aligned_out.get(), _d_other_in.get(), _d_pixel_map.get(), _d_depth_intrinsics.get(), _d_other_intrinsics.get()); break;
    case 2: kernel_other_to_depth<2> <<<depth_blocks,block>>> (_d_aligned_out.get(), _d_other_in.get(), _d_pixel_map.get(), _d_depth_intrinsics.get(), _d_other_intrinsics.get()); break;
    case 3: kernel_other_to_depth<3> <<<depth_blocks,block>>> (_d_aligned_out.get(), _d_other_in.get(), _d_pixel_map.get(), _d_depth_intrinsics.get(), _d_other_intrinsics.get()); break;
    case 4: kernel_other_to_depth<4> <<<depth_blocks,block>>> (_d_aligned_out.get(), _d_other_in.get(), _d_pixel_map.get(), _d_depth_intrinsics.get(), _d_other_intrinsics.get()); break;
    }
    cuda_or_throw(cudaGetLastError(), "other to depth launch");

    cuda_or_throw(cudaStreamSynchronize(0), "other to depth sync");

    cuda_or_throw(cudaMemcpy(h_aligned_out, _d_aligned_out.get(), aligned_size, cudaMemcpyDeviceToHost), "D2H aligned other-to-depth");
}

void align_cuda_helper::align_depth_to_other(unsigned char* h_aligned_out, const uint16_t* h_depth_in,
    float depth_scale, const rs2_intrinsics& h_depth_intrin, const rs2_extrinsics& h_depth_to_other,
    const rs2_intrinsics& h_other_intrin)
{
    int depth_pixel_count = h_depth_intrin.width * h_depth_intrin.height;
    int other_pixel_count = h_other_intrin.width * h_other_intrin.height;
    int aligned_pixel_count = other_pixel_count;

    int depth_byte_size = depth_pixel_count * 2;
    int aligned_byte_size = aligned_pixel_count * 2;

    // allocate and copy objects to cuda device memory
    refresh_device_copy(_d_depth_intrinsics, h_depth_intrin, "H2D depth intrinsics");
    refresh_device_copy(_d_other_intrinsics, h_other_intrin, "H2D other intrinsics");
    refresh_device_copy(_d_depth_other_extrinsics, h_depth_to_other, "H2D depth-to-other extrinsics");

    ensure_dev_buffer(_d_depth_in, _depth_capacity, static_cast<size_t>(depth_pixel_count));
    cuda_or_throw(cudaMemcpy(_d_depth_in.get(), h_depth_in, depth_byte_size, cudaMemcpyHostToDevice), "H2D depth");

    ensure_dev_buffer(_d_aligned_out, _aligned_capacity, static_cast<size_t>(aligned_byte_size));
    cuda_or_throw(cudaMemset(_d_aligned_out.get(), 0xff, aligned_byte_size), "clear aligned depth-to-other");

    ensure_dev_buffer(_d_pixel_map, _pixel_map_capacity, static_cast<size_t>(depth_pixel_count * 2));

    dim3 block(ALIGN_BLOCK_X, ALIGN_BLOCK_Y);
    dim3 depth_blocks(calc_block_size(h_depth_intrin.width, block.x), calc_block_size(h_depth_intrin.height, block.y));
    dim3 other_blocks(calc_block_size(h_other_intrin.width, block.x), calc_block_size(h_other_intrin.height, block.y));
    dim3 mapping_blocks(depth_blocks.x, depth_blocks.y, 2);

    kernel_map_depth_to_other <<<mapping_blocks,block>>> (_d_pixel_map.get(), _d_depth_in.get(), _d_depth_intrinsics.get(),
        _d_other_intrinsics.get(), _d_depth_other_extrinsics.get(), depth_scale);
    cuda_or_throw(cudaGetLastError(), "map depth to other launch");

    kernel_depth_to_other <<<depth_blocks,block>>> ((uint16_t*)_d_aligned_out.get(), _d_depth_in.get(), _d_pixel_map.get(),
        _d_depth_intrinsics.get(), _d_other_intrinsics.get());
    cuda_or_throw(cudaGetLastError(), "depth to other launch");

    kernel_replace_to_zero <<<other_blocks,block>>> ((uint16_t*)_d_aligned_out.get(), _d_other_intrinsics.get());
    cuda_or_throw(cudaGetLastError(), "replace invalid depth launch");

    cuda_or_throw(cudaStreamSynchronize(0), "depth to other sync");

    cuda_or_throw(cudaMemcpy(h_aligned_out, _d_aligned_out.get(), aligned_pixel_count * 2, cudaMemcpyDeviceToHost), "D2H aligned depth-to-other");
}

#endif //RS2_USE_CUDA
