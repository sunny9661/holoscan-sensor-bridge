# SPDX-FileCopyrightText: Copyright (c) 2023 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# See README.md for detailed information.

import logging

import hololink as hololink_module

# Camera I2C address.
I2C_ADDRESS = 0b00110100

# Camera I2C registers
VERSION = 100
RESET = 101
WIDTH = 102
HEIGHT = 103
RUN = 104
WATCHDOG = 105
FRAMES_PER_MINUTE = 106
PIXEL_FORMAT = 107
BAYER_FORMAT = 108
INITIALIZE = 199


class UdpCam:
    def __init__(self, hololink_channel, i2c_address=hololink_module.CAM_I2C_CTRL):
        self._hololink_channel = hololink_channel
        self._hololink = hololink_channel.hololink()
        self._i2c = self._hololink.get_i2c(i2c_address)

    def configure(self, height, width, bayer_format, pixel_format, frame_rate_s):
        # Make sure this is a version we know about.
        version = self.get_version()
        logging.info("version=%s" % (version,))
        assert version == 12344312
        # Set up the camera.
        self.configure_camera(height, width, bayer_format, pixel_format, frame_rate_s)
        self._pixel_format = pixel_format
        self._bayer_format = bayer_format
        self._height = height
        self._width = width

    def pixel_format(self):
        return self._pixel_format

    def bayer_format(self):
        return self._bayer_format

    def start(self):
        """Tell the camera to start publishing frame data."""
        self._running = True
        # Setting this register is time-consuming.
        self.set_register(RUN, 1)

    def stop(self):
        self._running = False
        self.set_register(RUN, 0)

    def reset(self):
        self.set_register(RESET, 1)

    def get_version(self):
        return self.get_register(VERSION)

    def get_register(self, register):
        logging.debug("get_register(register=%d)" % (register,))
        write_bytes = bytearray(100)
        serializer = hololink_module.Serializer(write_bytes)
        serializer.append_uint16_be(register)
        read_byte_count = 4
        reply = self._i2c.i2c_transaction(
            I2C_ADDRESS, write_bytes[: serializer.length()], read_byte_count
        )
        deserializer = hololink_module.Deserializer(reply)
        r = deserializer.next_uint32_be()
        return r

    def set_register(self, register, value, timeout=None):
        logging.debug("set_register(register=%d, value=0x%X)" % (register, value))
        write_bytes = bytearray(100)
        serializer = hololink_module.Serializer(write_bytes)
        serializer.append_uint16_be(register)
        serializer.append_uint32_be(value)
        read_byte_count = 0
        self._i2c.i2c_transaction(
            I2C_ADDRESS,
            write_bytes[: serializer.length()],
            read_byte_count,
            timeout=timeout,
        )

    def configure_camera(self, height, width, bayer_format, pixel_format, frame_rate_s):
        self.set_register(WIDTH, width)
        self.set_register(HEIGHT, height)
        self.set_register(BAYER_FORMAT, bayer_format.value)
        self.set_register(PIXEL_FORMAT, pixel_format.value)
        self.set_register(WATCHDOG, 20)
        frames_per_minute = int(60.0 / frame_rate_s)
        self.set_register(FRAMES_PER_MINUTE, frames_per_minute)
        self.set_register(
            INITIALIZE, 1, hololink_module.Timeout(timeout_s=30, retry_s=2)
        )

    def tap_watchdog(self, watchdog_timeout_s=20):
        self.set_register(WATCHDOG, watchdog_timeout_s)

    def configure_converter(self, converter):
        (
            frame_start_size,
            frame_end_size,
            line_start_size,
            line_end_size,
        ) = self._hololink.csi_size()
        converter.configure(
            self._width,
            self._height,
            self._pixel_format,
            frame_start_size,
            frame_end_size,
            line_start_size,
            line_end_size,
        )
