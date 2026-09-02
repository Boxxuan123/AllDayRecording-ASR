"""Compatibility facade for the split transfer interface."""

from .composition import TransferHTTPServer, create_transfer_server
from .http_handler import TransferRequestHandler
from .network import _prioritize_interface_addresses
from .protocol import (
    DEFAULT_PASSKEY_ORIGIN,
    DEFAULT_PASSKEY_RP_ID,
    DEFAULT_TRANSFER_PORT,
    PROTOCOL_NAME,
    PROTOCOL_VERSION,
)
from .runtime import _automatic_workflow_startup_message, serve_transfer

__all__ = [
    "DEFAULT_PASSKEY_ORIGIN",
    "DEFAULT_PASSKEY_RP_ID",
    "DEFAULT_TRANSFER_PORT",
    "PROTOCOL_NAME",
    "PROTOCOL_VERSION",
    "TransferHTTPServer",
    "TransferRequestHandler",
    "_automatic_workflow_startup_message",
    "_prioritize_interface_addresses",
    "create_transfer_server",
    "serve_transfer",
]
