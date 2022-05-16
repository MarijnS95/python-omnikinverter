"""Data model and conversions for tcp-based communication with the Omnik Inverter."""
from collections.abc import Generator
import asyncio
from ctypes import BigEndianStructure, c_char, c_ubyte, c_uint, c_ushort
from typing import Any, Optional

from .const import LOGGER
from .exceptions import OmnikInverterPacketInvalidError

MESSAGE_START = 0x68
MESSAGE_END = 0x16
MESSAGE_SEND_SEP = 0x40
MESSAGE_RECV_SEP = 0x41
MESSAGE_TYPE_INFORMATION_REQUEST = 0x30
MESSAGE_TYPE_INFORMATION_REPLY = 0xB0
MESSAGE_TYPE_ERROR_STRING = 0xB1
MESSAGE_TYPE_STRING = 0xF0  # Message seems to consist of pure text
UINT16_MAX = 65535
# Message length field does not include the "header":
# - Length (1 byte)
# - Separator (1 byte)
# - Message type (1 byte)
# - Repeated integer serial number (2 * 4 bytes)
# - CRC in suffix (1 byte)
MESSAGE_HEADER_SIZE = 3 + 2 * 4 + 1


class _AcOutput(BigEndianStructure):
    _fields_ = [
        ("frequency", c_ushort),
        ("power", c_ushort),
    ]

    def get_power(self) -> Optional[int]:
        """Retrieve AC power.

        Returns:
            The power field, or None if it is unset.
        """
        return None if self.power == UINT16_MAX else self.power

    def get_frequency(self) -> Optional[float]:
        """Retrieve AC frequency.

        Returns:
            The frequency field in Hertz, or None if it is unset.
        """
        return None if self.frequency == UINT16_MAX else self.frequency * 0.01


class _TcpData(BigEndianStructure):
    _pack_ = 1
    _fields_ = [
        ("padding0", c_char * 3),
        ("serial_number", c_char * 16),
        ("temperature", c_ushort),
        ("dc_input_voltage", c_ushort * 3),
        ("dc_input_current", c_ushort * 3),
        ("ac_output_current", c_ushort * 3),
        ("ac_output_voltage", c_ushort * 3),
        ("ac_output", _AcOutput * 3),
        ("solar_energy_today", c_ushort),
        ("solar_energy_total", c_uint),
        ("solar_hours_total", c_uint),
        ("inverter_active", c_ushort),
        ("padding1", c_ubyte * 4),
        ("unknown0", c_ushort),
        ("padding2", c_ubyte * 10),
        ("firmware", c_char * 16),
        ("padding3", c_ubyte * 4),
        ("firmware_slave", c_char * 16),
        ("padding4", c_ubyte * 4),
    ]


def _pack_message(
    message_type: int, serial_number: int, message: bytearray
) -> bytearray:
    # Prepend message "header"
    request_data = bytearray([len(message), MESSAGE_SEND_SEP, message_type])
    serial_bytes = serial_number.to_bytes(4, "little")
    request_data.extend(serial_bytes)
    request_data.extend(serial_bytes)

    request_data.extend(message)

    checksum = sum(request_data) & 0xFF

    # Finally, prepend message with a start marker, and append the CRC
    # and end marker.
    request_data.insert(0, MESSAGE_START)
    request_data.append(checksum)
    request_data.append(MESSAGE_END)

    return request_data


def _unpack_message(message: bytearray) -> tuple[int, int, bytearray]:
    LOGGER.debug("Handling message `%s`", message)

    message_checksum = message.pop()
    checksum = sum(message) & 0xFF
    if message_checksum != checksum:
        raise OmnikInverterPacketInvalidError(
            f"Checksum mismatch (calculated `{checksum}` got `{message_checksum}`)"
        )

    # Now that the checksum has been computed remove the length,
    # separator, message type and repeated serial number

    length = message.pop(0)  # Length
    if message.pop(0) != MESSAGE_RECV_SEP:
        raise OmnikInverterPacketInvalidError("Invalid receiver separator")

    message_type = message.pop(0)
    LOGGER.debug(
        "Message type %02x, length %s, checksum %02x", message_type, length, checksum
    )

    serial0 = int.from_bytes(message[:4], "little")
    serial1 = int.from_bytes(message[4:8], "little")
    if serial0 != serial1:
        raise OmnikInverterPacketInvalidError(
            f"Serial number mismatch in reply {serial0} != {serial1}"
        )

    return (message_type, serial0, message[8:])


async def _unpack_messages(
    reader: asyncio.StreamReader,
) -> Generator[tuple[int, int, bytearray], None, None]:
    async def readbyte():
        return (await reader.readexactly(1))[0]

    while True:
        message_start = await readbyte()
        if message_start == 0xFF:  # pragma: no cover
            # The stream contains a bunch more 0xFF to "end" the current sequence of
            # messages.  After some time more information will be sent, presuming this
            # TCP stream is kept open.
            # XXX: Replace this with `continue` to keep indefinitely yielding valid messages
            return

        if message_start != MESSAGE_START:
            raise OmnikInverterPacketInvalidError("Invalid start byte")

        original_length = await readbyte()
        length = original_length + MESSAGE_HEADER_SIZE

        # Read message and reconstruct with length byte for CRC check
        message = bytearray(
            [original_length] + list(await reader.readexactly(length - 1))
        )
        if len(message) != length:
            raise OmnikInverterPacketInvalidError(
                f"Could only read {len(message)} out of {length} "
                "expected bytes from TCP stream",
            )

        # Prepare for the next message by consuming the end byte
        if await readbyte() != MESSAGE_END:
            raise OmnikInverterPacketInvalidError("Invalid end byte")

        yield _unpack_message(message)


def create_information_request(serial_number: int) -> bytearray:
    """Compute a magic message to which the Omnik will reply with raw statistics.

    Args:
        serial_number: Integer with the serial number of your Omnik device.

    Returns:
        A bytearray with the raw message data, to be sent over a TCP socket.
    """
    return _pack_message(
        MESSAGE_TYPE_INFORMATION_REQUEST, serial_number, bytearray([0x01, 0x00])
    )


async def parse_messages(
    serial_number: int, reader: asyncio.StreamReader
) -> dict[str, Any]:
    """Receive and process replies from the Omnik inverter over TCP.

    Args:
        serial_number: Serial number passed to
            `clk.create_information_request()`, used to validate the reply.
        reader: Raw socket from which to read Omnik inverter replies.

    Returns:
        A Python dictionary (text) with the response from
        the Omnik Inverter.

    Raises:
        OmnikInverterPacketInvalidError: Received data fails basic validity checks.
    """

    info = None

    async for (message_type, reply_serial_number, message) in _unpack_messages(reader):
        if reply_serial_number != serial_number:
            LOGGER.debug(
                "Replied serial number %s not equal to request %s",
                reply_serial_number,
                serial_number,
            )

        if message_type == MESSAGE_TYPE_INFORMATION_REPLY:
            if info is not None:  # pragma: no cover
                LOGGER.warning("Omnik sent multiple INFORMATION_REPLY messages")
            info = _parse_information_reply(message)
        elif message_type == MESSAGE_TYPE_STRING:  # pragma: no cover
            LOGGER.warning(
                "Omnik sent text message `%s`", message.decode("utf8").strip()
            )
        elif message_type == MESSAGE_TYPE_ERROR_STRING:  # pragma: no cover
            LOGGER.warning("Omnik sent error message `%s`", message)
        else:
            raise OmnikInverterPacketInvalidError(
                f"Unknown Omnik message type {message_type:02x} "
                f"with contents `{message}`",
            )

    if info is None:
        raise OmnikInverterPacketInvalidError(
            "None of the messages contained an information reply!"
        )

    return info


def _parse_information_reply(data: bytes) -> dict[str, Any]:
    tcp_data = _TcpData.from_buffer_copy(data)

    if tcp_data.unknown0 not in [0, UINT16_MAX]:  # pragma: no cover
        LOGGER.warning("Unexpected unknown0 `%s`", tcp_data.unknown0)

    if tcp_data.padding0 != b"\x81\x02\x01":  # pragma: no cover
        LOGGER.warning("Unexpected padding0 `%s`", tcp_data.padding0)

    # For all data that's expected to be zero, print it if it's not. Perhaps
    # there are more interesting fields on different inverters waiting to be
    # uncovered.
    for idx in range(1, 5):  # pragma: no cover
        name = f"padding{idx}"
        padding = getattr(tcp_data, name)
        if sum(padding):
            LOGGER.warning("Unexpected `%s`: `%s`", name, padding)

    def list_divide_10(integers: list[int]) -> list[Optional[float]]:
        return [None if v == UINT16_MAX else v * 0.1 for v in integers]

    def int_to_bool(num: int) -> bool:
        return {
            0: False,
            1: True,
        }[num]

    # Set temperature to None if it matches 65326, this is returned
    # when the inverter is "offline".
    def temperature_to_float(temp: int) -> Optional[float]:
        return None if temp == 65326 else temp * 0.1

    # Only these fields will be extracted from the structure
    field_extractors = {
        "serial_number": None,
        "temperature": temperature_to_float,
        "dc_input_voltage": list_divide_10,
        "dc_input_current": list_divide_10,
        "ac_output_current": list_divide_10,
        "ac_output_voltage": list_divide_10,
        "ac_output": None,
        "solar_energy_today": 0.01,
        "solar_energy_total": 0.1,
        "solar_hours_total": None,
        "inverter_active": int_to_bool,
        "firmware": None,
        "firmware_slave": None,
    }

    result: dict[str, Any] = {}

    for (name, extractor) in field_extractors.items():
        value = getattr(tcp_data, name)

        if name == "ac_output":
            # Flatten the list of frequency+power AC objects

            result["ac_output_frequency"] = [o.get_frequency() for o in value]
            result["ac_output_power"] = [o.get_power() for o in value]
            continue

        if isinstance(extractor, float):
            value *= extractor
        elif extractor is not None:
            value = extractor(value)
        elif isinstance(value, bytes):
            value = value.decode(encoding="utf-8")

        result[name] = value

    return result
