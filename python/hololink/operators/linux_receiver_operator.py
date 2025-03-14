# SPDX-FileCopyrightText: Copyright (c) 2023-2024 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
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
import os
import socket
import threading

from cuda import cuda

import hololink as hololink_module

MS_PER_SEC = 1000
US_PER_SEC = 1000 * MS_PER_SEC
NS_PER_SEC = 1000 * US_PER_SEC
SEC_PER_NS = 1.0 / NS_PER_SEC


class LinuxReceiverOperator(hololink_module.operators.BaseReceiverOp):
    def __init__(self, *args, receiver_affinity=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._receiver_affinity = receiver_affinity
        if self._receiver_affinity is None:
            # By default, run us on the third core in the system;
            # run with HOLOLINK_AFFINITY=<n> to use a different core or
            # set HOLOLINK_AFFINITY="" to avoid affinity configuration.
            affinity = os.getenv("HOLOLINK_AFFINITY", "2")
            # The len(affinity) supports this command
            #   HOLOLINK_AFFINITY= python3 ...
            # to avoid affinity settings.
            if (affinity is not None) and (len(affinity) > 0):
                self._receiver_affinity = {int(affinity)}

    def _start_receiver(self):
        self._check_buffer_size(self._frame_size)
        self._hololink_channel.configure_socket(self._data_socket.fileno())
        self._receiver = hololink_module.operators.LinuxReceiver(
            self._frame_memory,
            self._frame_size,
            self._data_socket.fileno(),
            self.received_address_offset(),
        )

        def _ready(receiver):
            self.frame_ready()

        self._receiver.set_frame_ready(_ready)
        self._receiver_thread = threading.Thread(
            daemon=True, name=self.name, target=self._run
        )
        self._receiver_thread.start()
        self._hololink_channel.authenticate(
            self._receiver.get_qp_number(), self._receiver.get_rkey()
        )

    def _run(self):
        cuda.cuCtxSetCurrent(self._frame_context)
        if self._receiver_affinity:
            os.sched_setaffinity(0, self._receiver_affinity)
        self._receiver.run()

    def _stop(self):
        self._receiver.close()
        self._receiver_thread.join()
        # close the socket after the receiver thread stopped
        self._data_socket.close()

    def _get_next_frame(self, timeout_ms):
        ok, receiver_metadata = self._receiver.get_next_frame(timeout_ms)
        if not ok:
            return None
        application_metadata = {
            "frame_number": receiver_metadata.frame_number,
            "frame_packets_received": receiver_metadata.frame_packets_received,
            "frame_bytes_received": receiver_metadata.frame_bytes_received,
            "received_s": receiver_metadata.received_s,
            "received_ns": receiver_metadata.received_ns,
            "timestamp_s": receiver_metadata.timestamp_s,
            "timestamp_ns": receiver_metadata.timestamp_ns,
            "metadata_s": receiver_metadata.metadata_s,
            "metadata_ns": receiver_metadata.metadata_ns,
            "packets_dropped": receiver_metadata.packets_dropped,
            "crc": receiver_metadata.crc,
            "psn": receiver_metadata.psn,
        }
        return application_metadata

    def _check_buffer_size(self, data_memory_size):
        receiver_buffer_size = self._data_socket.getsockopt(
            socket.SOL_SOCKET, socket.SO_RCVBUF
        )
        if receiver_buffer_size < data_memory_size:
            # round it up to a 64k boundary
            boundary = 0x10000 - 1
            request_size = (data_memory_size + boundary) & ~boundary
            self._data_socket.setsockopt(
                socket.SOL_SOCKET, socket.SO_RCVBUF, request_size
            )
            receiver_buffer_size = self._data_socket.getsockopt(
                socket.SOL_SOCKET, socket.SO_RCVBUF
            )
            logging.debug("receiver buffer size=%s" % (receiver_buffer_size,))
            if receiver_buffer_size < data_memory_size:
                logging.warning(
                    "Kernel receiver buffer size is too small; "
                    + "performance will be unreliable."
                )
                logging.warning(
                    'Resolve this with "echo %d | sudo tee /proc/sys/net/core/rmem_max"'
                    % (request_size,)
                )
