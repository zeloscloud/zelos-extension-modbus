"""Shared constants for the Modbus extension."""

from enum import StrEnum


class Transport(StrEnum):
    TCP = "tcp"
    RTU = "rtu"


class RegisterType(StrEnum):
    HOLDING = "holding"
    INPUT = "input"
    COIL = "coil"
    DISCRETE_INPUT = "discrete_input"


class ByteOrder(StrEnum):
    BIG = "big"
    LITTLE = "little"
    BIG_SWAP = "big_swap"
    LITTLE_SWAP = "little_swap"


class WriteMode(StrEnum):
    AUTO = "auto"
    FC16 = "fc16"
