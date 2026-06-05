#ifdef RS2_USE_CUDA

#include "cuda-pointcloud.cuh"
#include <iostream>
#include <chrono>

// --- GB10 shared-memory zero-copy experiment (opt-in; default OFF = byte-identical upstream) -------
// Attribution ladder for "what does DGX Spark unified memory buy for the pointcloud CUDA path?".
// Selected at runtime via RS2_PC_MODE (read once): 0=baseline (per-frame cudaMalloc+cudaMemcpy+free),
// 1=cached device buffers (alloc once, reuse; still cudaMemcpy), 2=cached MANAGED buffers
// (cudaMallocManaged once; host writes/reads the coherent buffer with plain memcpy, no cudaMemcpy).
// Default when the define is compiled in but RS2_PC_MODE is unset = 0, so other tools on this build
// are unaffected. Buffers are process-static, mutex-guarded, grow-only, sized to the current frame.
#if defined(RS2_GB10_PC_ZEROCOPY)
#include <mutex>
#include <cstdlib>
#include <cstring>
namespace {
    int rs2_pc_mode() {
        static int m = -1;
        if (m < 0) { const char* e = std::getenv("RS2_PC_MODE"); m = e ? std::atoi(e) : 1; }  // default 1 = cached-device (promoted)
        return m;
    }
    struct pc_zc_buffers {
        std::mutex mtx;
        int cap = 0; bool managed = false;
        float* d_points = nullptr; uint16_t* d_depth = nullptr; rs2_intrinsics* d_intrin = nullptr;
        void free_all() {
            if (d_points) cudaFree(d_points); if (d_depth) cudaFree(d_depth); if (d_intrin) cudaFree(d_intrin);
            d_points = nullptr; d_depth = nullptr; d_intrin = nullptr; cap = 0;
        }
        void ensure(int count, bool want_managed) {
            if (d_points && cap >= count && managed == want_managed) return;
            free_all(); managed = want_managed;
            auto alloc = [&](void** p, size_t bytes) {
                return want_managed ? cudaMallocManaged(p, bytes) : cudaMalloc(p, bytes); };
            cudaError_t r = alloc((void**)&d_points, (size_t)count * sizeof(float) * 3);
            r = alloc((void**)&d_depth, (size_t)count * sizeof(uint16_t));
            r = alloc((void**)&d_intrin, sizeof(rs2_intrinsics));
            assert(r == cudaSuccess); (void)r; cap = count;
        }
    };
    pc_zc_buffers& pc_zc() { static pc_zc_buffers b; return b; }
}
#endif
// ---------------------------------------------------------------------------------------------------


__device__
float map_depth (float depth_scale, uint16_t z) {
    return depth_scale * z;
}

__device__
void deproject_pixel_to_point_cuda(float points[3], const struct rs2_intrinsics * intrin, const float pixel[2], float depth) {
    assert(intrin->model != RS2_DISTORTION_MODIFIED_BROWN_CONRADY); // Cannot deproject from a forward-distorted image
    assert(intrin->model != RS2_DISTORTION_FTHETA); // Cannot deproject to an ftheta image
    //assert(intrin->model != RS2_DISTORTION_BROWN_CONRADY); // Cannot deproject to an brown conrady model
    float x = (pixel[0] - intrin->ppx) / intrin->fx;
    float y = (pixel[1] - intrin->ppy) / intrin->fy;    

    float xo = x;
    float yo = y;

    if (intrin->model == RS2_DISTORTION_INVERSE_BROWN_CONRADY)
    {
        // need to loop until convergence 
        // 10 iterations determined empirically
        for (int i = 0; i < 10; i++)
        {
            float r2 = x * x + y * y;
            float icdist = (float)1 / (float)(1 + ((intrin->coeffs[4] * r2 + intrin->coeffs[1])*r2 + intrin->coeffs[0])*r2);
            float xq = x / icdist;
            float yq = y / icdist;
            float delta_x = 2 * intrin->coeffs[2] * xq*yq + intrin->coeffs[3] * (r2 + 2 * xq*xq);
            float delta_y = 2 * intrin->coeffs[3] * xq*yq + intrin->coeffs[2] * (r2 + 2 * yq*yq);
            x = (xo - delta_x)*icdist;
            y = (yo - delta_y)*icdist;
        }
    }
    else if (intrin->model == RS2_DISTORTION_BROWN_CONRADY)
    {
        // need to loop until convergence 
        // 10 iterations determined empirically
        for (int i = 0; i < 10; i++)
        {
            float r2 = x * x + y * y;
            float icdist = (float)1 / (float)(1 + ((intrin->coeffs[4] * r2 + intrin->coeffs[1])*r2 + intrin->coeffs[0])*r2);
            float delta_x = 2 * intrin->coeffs[2] * x*y + intrin->coeffs[3] * (r2 + 2 * x*x);
            float delta_y = 2 * intrin->coeffs[3] * x*y + intrin->coeffs[2] * (r2 + 2 * y*y);
            x = (xo - delta_x)*icdist;
            y = (yo - delta_y)*icdist;
        }
    }
    points[0] = depth * x;
    points[1] = depth * y;
    points[2] = depth;
    
}


__global__
//void kernel_deproject_depth_cuda(float * points, const rs2_intrinsics & intrin, const uint16_t * depth, std::function<uint16_t(float)> map_depth)

void kernel_deproject_depth_cuda(float * points, const rs2_intrinsics* intrin, const uint16_t * depth, float depth_scale)
{
    int i = blockDim.x * blockIdx.x + threadIdx.x;
    
    if (i >= (*intrin).height * (*intrin).width) {
        return;
    }
    int stride = blockDim.x * gridDim.x;
    int a, b;
    
    for (int j = i; j < (*intrin).height * (*intrin).width; j += stride) {
        b = j / (*intrin).width;
        a = j - b * (*intrin).width;
        const float pixel[] = { (float)a, (float)b };
        deproject_pixel_to_point_cuda(points + j * 3, intrin, pixel, depth_scale * depth[j]);               
   }
}


void rscuda::deproject_depth_cuda(float * points, const rs2_intrinsics & intrin, const uint16_t * depth, float depth_scale)
{
    int count = intrin.height * intrin.width;

#if defined(RS2_GB10_PC_ZEROCOPY)
    const int _mode = rs2_pc_mode();
    if (_mode == 1 || _mode == 2) {
        const bool managed = (_mode == 2);
        auto& b = pc_zc();
        std::lock_guard<std::mutex> lk(b.mtx);
        b.ensure(count, managed);
        const int nb = count / RS2_CUDA_THREADS_PER_BLOCK;
        if (managed) {                                  // host write into coherent managed memory
            std::memcpy(b.d_depth, depth, (size_t)count * sizeof(uint16_t));
            std::memcpy(b.d_intrin, &intrin, sizeof(rs2_intrinsics));
        } else {                                        // cached device buffers, still cudaMemcpy
            cudaMemcpy(b.d_depth, depth, (size_t)count * sizeof(uint16_t), cudaMemcpyHostToDevice);
            cudaMemcpy(b.d_intrin, &intrin, sizeof(rs2_intrinsics), cudaMemcpyHostToDevice);
        }
        kernel_deproject_depth_cuda<<<nb, RS2_CUDA_THREADS_PER_BLOCK>>>(b.d_points, b.d_intrin, b.d_depth, depth_scale);
        cudaDeviceSynchronize();                        // explicit sync (D2H memcpy used to provide it)
        if (managed) std::memcpy(points, b.d_points, (size_t)count * sizeof(float) * 3);
        else cudaMemcpy(points, b.d_points, (size_t)count * sizeof(float) * 3, cudaMemcpyDeviceToHost);
        return;
    }
    // _mode == 0 falls through to the unmodified baseline below
#endif
    int numBlocks = count / RS2_CUDA_THREADS_PER_BLOCK;
    
    float *dev_points = 0;	
    uint16_t *dev_depth = 0;
    rs2_intrinsics* dev_intrin = 0;
    cudaError_t result;

    result = cudaMalloc(&dev_points, count * sizeof(float) * 3);
    assert(result == cudaSuccess);
    result = cudaMalloc(&dev_depth, count * sizeof(uint16_t));
    assert(result == cudaSuccess);
    result = cudaMalloc(&dev_intrin, sizeof(rs2_intrinsics));
    assert(result == cudaSuccess);
       
    result = cudaMemcpy(dev_depth, depth, count * sizeof(uint16_t), cudaMemcpyHostToDevice);
    assert(result == cudaSuccess); 
    result = cudaMemcpy(dev_intrin, &intrin, sizeof(rs2_intrinsics), cudaMemcpyHostToDevice);
    assert(result == cudaSuccess); 
     
    kernel_deproject_depth_cuda<<<numBlocks, RS2_CUDA_THREADS_PER_BLOCK>>>(dev_points, dev_intrin, dev_depth, depth_scale); 

     result = cudaMemcpy(points, dev_points, count * sizeof(float) * 3, cudaMemcpyDeviceToHost);
     assert(result == cudaSuccess);

     if (result); // suppress warning about "variable "result" was set but never used"

    cudaFree(dev_points);
    cudaFree(dev_depth);
    cudaFree(dev_intrin);
}

#endif
