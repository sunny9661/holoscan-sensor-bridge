/*
 * SPDX-FileCopyrightText: Copyright (c) 2024 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
 * SPDX-License-Identifier: Apache-2.0
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 * http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

#include "base_receiver_op.hpp"

#include <arpa/inet.h>
#include <netinet/in.h>
#include <sys/socket.h>

#include <yaml-cpp/parser.h>

#include <holoscan/holoscan.hpp>

#include <hololink/data_channel.hpp>

/**
 * @brief This macro defining a YAML converter which throws for unsupported types.
 *
 * Background: Holoscan supports setting parameters through YAML files. But for some parameters
 * accepted by the receiver operators like `DataChannel` class of functions it makes no sense
 * to specify them in YAML files. Therefore use a converter which throws for these types.
 *
 * @tparam TYPE
 */
#define YAML_CONVERTER(TYPE)                                                                \
    template <>                                                                             \
    struct YAML::convert<TYPE> {                                                            \
        static Node encode(TYPE&) { throw std::runtime_error("Unsupported"); }              \
                                                                                            \
        static bool decode(const Node&, TYPE&) { throw std::runtime_error("Unsupported"); } \
    };

YAML_CONVERTER(hololink::DataChannel*);
YAML_CONVERTER(std::function<void()>);
YAML_CONVERTER(CUcontext);

namespace hololink::operators {

void BaseReceiverOp::setup(holoscan::OperatorSpec& spec)
{
    spec.output<holoscan::gxf::Entity>("output");

    /// Register converters for arguments not defined by Holoscan
    register_converter<hololink::DataChannel*>();
    register_converter<std::function<void()>>();
    register_converter<CUcontext>();
    register_converter<size_t>();
    register_converter<CUdeviceptr>();

    spec.param(hololink_channel_, "hololink_channel", "HololinkChannel",
        "Pointer to Hololink Datachannel object");
    spec.param(
        device_start_, "device_start", "DeviceStart", "Function to be called to start the device");
    spec.param(
        device_stop_, "device_stop", "DeviceStop", "Function to be called to stop the device");
    spec.param(frame_context_, "frame_context", "FrameContext", "CUDA context");
    spec.param(frame_size_, "frame_size", "FrameSize", "Size of one frame in bytes");
    spec.param(user_frame_memory_, "frame_memory", "FrameMemory", "Frame memory (optional)", 0ull,
        holoscan::ParameterFlag::kOptional);
}

void BaseReceiverOp::start()
{
    // We'll allocate this for you if you like.
    if (!user_frame_memory_.has_value() || (user_frame_memory_.get() == 0ull)) {
        frame_memory_ = allocate(frame_size_);
    } else {
        frame_memory_ = user_frame_memory_.get();
    }

    HOLOSCAN_LOG_INFO("frame_size={} frame={}", frame_size_.get(), frame_memory_);

    //
    data_socket_.reset(socket(AF_INET, SOCK_DGRAM, 0));
    if (!data_socket_) {
        throw std::runtime_error("Failed to create socket");
    }

    start_receiver();

    auto [local_ip, local_port] = local_ip_and_port();
    HOLOSCAN_LOG_INFO("local_ip={} local_port={}", local_ip, local_port);

    hololink_channel_->configure(frame_memory_, frame_size_, local_port);
    device_start_.get()();
}

void BaseReceiverOp::stop()
{
    device_stop_.get()();
    stop_();

    if (!user_frame_memory_.has_value()) {
        // if we allocated the memory, free it
        deviceptr_.release();
        host_deviceptr_.release();
    }
}

void BaseReceiverOp::compute(holoscan::InputContext& input, holoscan::OutputContext& output,
    holoscan::ExecutionContext& context)
{
    const double timeout_ms = 1000.f;
    metadata_ = get_next_frame(timeout_ms);
    if (!metadata_) {
        if (ok_) {
            ok_ = false;
            HOLOSCAN_LOG_ERROR("Ingress frame timeout; ignoring.");
        }
    } else {
        ok_ = true;
    }

    // Create an Entity and use GXF tensor to wrap the CUDA memory.
    nvidia::gxf::Expected<nvidia::gxf::Entity> out_message
        = nvidia::gxf::Entity::New(context.context());
    if (!out_message) {
        throw std::runtime_error("Failed to create GXF entity");
    }
    nvidia::gxf::Expected<nvidia::gxf::Handle<nvidia::gxf::Tensor>> gxf_tensor
        = out_message.value().add<nvidia::gxf::Tensor>("");
    if (!out_message) {
        throw std::runtime_error("Failed to add GXF tensor");
    }
    const nvidia::gxf::Shape shape { static_cast<int>(frame_size_.get()) };
    const nvidia::gxf::PrimitiveType element_type = nvidia::gxf::PrimitiveType::kUnsigned8;
    const uint64_t element_size = nvidia::gxf::PrimitiveTypeSize(element_type);
    if (!gxf_tensor.value()->wrapMemory(shape, element_type, element_size,
            nvidia::gxf::ComputeTrivialStrides(shape, element_size),
            nvidia::gxf::MemoryStorageType::kDevice, reinterpret_cast<void*>(frame_memory_),
            [](void*) {
                // release function, nothing to do
                return nvidia::gxf::Success;
            })) {
        throw std::runtime_error("Failed to add wrap memory");
    }
    // Emit the tensor.
    output.emit(out_message.value(), "output");
}

std::shared_ptr<hololink::Metadata> BaseReceiverOp::metadata() const { return metadata_; }

std::tuple<std::string, uint32_t> BaseReceiverOp::local_ip_and_port()
{
    sockaddr_in ip {};
    ip.sin_family = AF_UNSPEC;
    socklen_t ip_len = sizeof(ip);
    if (getsockname(data_socket_.get(), (sockaddr*)&ip, &ip_len) < 0) {
        throw std::runtime_error(
            fmt::format("getsockname failed with errno={}: \"{}\"", errno, strerror(errno)));
    }

    const std::string local_ip = inet_ntoa(ip.sin_addr);
    const in_port_t local_port = ip.sin_port;

    return { local_ip, local_port };
}

CUdeviceptr BaseReceiverOp::allocate(size_t size, uint32_t flags)
{
    CudaCheck(cuInit(0));
    CudaCheck(cuCtxSetCurrent(frame_context_));
    CUdevice device;
    CudaCheck(cuCtxGetDevice(&device));
    int integrated = 0;
    CudaCheck(cuDeviceGetAttribute(&integrated, CU_DEVICE_ATTRIBUTE_INTEGRATED, device));

    HOLOSCAN_LOG_TRACE("integrated={}", integrated);
    if (integrated == 0) {
        // We're a discrete GPU device; so allocate using cuMemAlloc/cuMemFree
        deviceptr_.reset([size] {
            CUdeviceptr device_deviceptr;
            CudaCheck(cuMemAlloc(&device_deviceptr, size));
            return device_deviceptr;
        }());
        return deviceptr_.get();
    }

    // We're an integrated device (e.g. Tegra) so we must allocate
    // using cuMemHostAlloc/cuMemFreeHost
    host_deviceptr_.reset([size, flags] {
        void* host_deviceptr;
        CudaCheck(cuMemHostAlloc(&host_deviceptr, size, flags));
        return host_deviceptr;
    }());

    CUdeviceptr device_deviceptr;
    CudaCheck(cuMemHostGetDevicePointer(&device_deviceptr, host_deviceptr_.get(), 0));
    return device_deviceptr;
}

} // namespace hololink::operators