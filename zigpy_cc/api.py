import asyncio
import logging
from typing import List

from zigpy_cc.exception import CommandError
from . import uart
from .definition import Definition
from .types import Repr, Subsystem, CommandType, Timeouts
from .zpi_object import ZpiObject

LOGGER = logging.getLogger(__name__)

COMMAND_TIMEOUT = 3
CC_BAUDRATE = 115200


class Matcher(Repr):
    def __init__(self, type, subsystem, command, payload):
        self.type = type
        self.subsystem = subsystem
        self.command = command
        self.payload = payload


class Waiter(Repr):
    def __init__(self, type: int, subsystem: int, command: str, payload, timeout: int, sequence):
        self.matcher = Matcher(type, subsystem, command, payload)
        self.future = asyncio.get_event_loop().create_future()
        self.timeout = timeout
        self.sequence = sequence

    async def wait(self):
        return await asyncio.wait_for(self.future, self.timeout / 1000)

    def set_result(self, result) -> None:
        self.future.set_result(result)

    def match(self, obj: ZpiObject):
        matcher = self.matcher
        if matcher.type != obj.type or matcher.subsystem != obj.subsystem or matcher.command != obj.command:
            return False

        if matcher.payload:
            for f, v in matcher.payload.items():
                if v != obj.payload[f]:
                    LOGGER.warn("payload missmatch\n-%s\n+%s", matcher.payload, obj.payload)
                    return False

        return True


class API:
    def __init__(self):
        self._uart = None
        self._seq = 1
        self._waiters: List[Waiter] = []
        self._app = None
        self._proto_ver = None

    @property
    def protocol_version(self):
        """Protocol Version."""
        return self._proto_ver

    def set_application(self, app):
        self._app = app

    async def connect(self, device, baudrate=CC_BAUDRATE):
        assert self._uart is None
        self._uart = await uart.connect(device, baudrate, self)

    def close(self):
        return self._uart.close()

    async def _command(self, subsystem, command, payload) -> ZpiObject:
        return await self.request(subsystem, command, payload)

    async def request(self, subsystem, command, payload, expectedStatus=None):
        obj = self._create_obj(subsystem, command, payload)
        return await self.request_raw(obj, expectedStatus)

    async def request_raw(self, obj, expectedStatus=None):
        if expectedStatus is None:
            expectedStatus = [0]
        """
        TODO add queue
        """
        LOGGER.debug("<-- %s", obj)
        frame = obj.to_unpi_frame()

        if obj.type == CommandType.SREQ:
            timeout = Timeouts.SREQ
            waiter = self.wait_for(CommandType.SRSP, obj.subsystem, obj.command, {}, timeout)
            self._uart.send(frame)
            result = await waiter.wait()
            if result and 'status' in result.payload and result.payload['status'] not in expectedStatus:
                raise CommandError(
                    result.payload['status'],
                    "SREQ '{}' failed with status '{}' (expected '{}')".format(obj.command, result.payload['status'],
                                                                               expectedStatus),
                )
            else:
                return result
        elif obj.type == CommandType.AREQ and obj.is_reset_command():
            waiter = self.wait_for(CommandType.AREQ, Subsystem.SYS, 'resetInd', {}, Timeouts.reset)
            # TODO clear queue
            self._uart.send(frame)
            return await waiter.wait()
        else:
            if obj.type == CommandType.AREQ:
                self._uart.send(frame)
                return None
            else:
                raise Exception("Unknown type '{}'".format(obj.type))

        # self._uart.send(frame)
        # fut = asyncio.Future()
        # seq = "{}_{}".format(subsystem, 'resetInd' if command == "resetReq" else command)
        #
        # self._awaiting[seq] = fut
        # try:
        #     return await asyncio.wait_for(fut, timeout=COMMAND_TIMEOUT)
        # except asyncio.TimeoutError:
        #     LOGGER.warning("No response to '%s' command", obj)
        #     self._awaiting.pop(seq)
        #     raise

    def create_response_waiter(self, obj: ZpiObject, sequence=None):
        waiter = self.get_response_waiter(obj, sequence)
        if waiter:
            LOGGER.debug("waiting for %d %s", sequence, obj.command)

    def get_response_waiter(self, obj: ZpiObject, sequence=None):
        if obj.type == CommandType.SREQ and obj.command == 'dataRequest':
            return None

        if obj.type == CommandType.SREQ and obj.command.endswith('Req'):
            rsp = obj.command.replace('Req', 'Rsp')
            for cmd in Definition[obj.subsystem]:
                if rsp == cmd['name']:
                    payload = {'srcaddr': obj.payload['dstaddr']}
                    return self.wait_for(CommandType.AREQ, Subsystem.ZDO, rsp, payload, sequence=sequence)

        LOGGER.warn("no response cmd configured for %s", obj.command)
        return None

    def wait_for(self, type, subsystem, command, payload=None, timeout=Timeouts.default, sequence=None):
        waiter = Waiter(type, subsystem, command, payload, timeout, sequence)
        self._waiters.append(waiter)

        return waiter

    async def aps_data_request(self, req_id, dst_addr_ep, profile, cluster, src_ep, data):
        raise Exception('Not implemented')

    def data_received(self, frame):
        obj = ZpiObject.from_unpi_frame(frame)
        LOGGER.debug('--> %s', obj)

        to_remove = []
        for waiter in self._waiters:
            if waiter.future.done():
                LOGGER.warning('waiter already done %s', waiter)
                to_remove.append(waiter)
            elif waiter.match(obj):
                to_remove.append(waiter)
                waiter.set_result(obj)
                obj.sequence = waiter.sequence
        # else:
        #     LOGGER.debug('NOT A RESPONSE %s', obj)

        for waiter in to_remove:
            self._waiters.remove(waiter)

        if self._app is not None:
            self._app.handle_znp(obj)

        try:
            getattr(self, "_handle_%s" % (obj.command,))(obj)
        except AttributeError as e:
            # LOGGER.debug(e)
            pass

    async def version(self):
        version = await self._command(Subsystem.SYS, "version", {})
        # todo check version
        self._proto_ver = version.payload
        return version.payload

    def _handle_version(self, data):
        LOGGER.debug("Version response: %s", data.payload)

    def _handle_getDeviceInfo(self, data):
        LOGGER.debug("Device info: %s", data.payload)

    def _handle_srcRtgInd(self, data):
        pass

    def _create_obj(self, subsystem, command, payload):
        cmd = next(c for c in Definition[subsystem] if c["name"] == command)

        return ZpiObject(
            cmd["type"], subsystem, command, cmd["ID"], payload, cmd["request"]
        )
