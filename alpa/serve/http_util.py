# pylint: skip-file
"""
Adopted from
https://github.com/ray-project/ray/blob/master/python/ray/serve/_private/http_util.py
https://github.com/ray-project/ray/blob/master/python/ray/serve/_private/utils.py
"""
import asyncio
from dataclasses import dataclass
import inspect
import json
import random
import socket
import traceback
from typing import Any, Dict, Type

from fastapi.encoders import jsonable_encoder
import numpy as np
import starlette.responses
import starlette.requests
from starlette.types import Send, ASGIApp

try:
    import pandas as pd
except ImportError:
    pd = None


@dataclass
class HTTPRequestWrapper:
    scope: Dict[Any, Any]
    body: bytes


def build_starlette_request(request_wrapper):
    """Build and return a Starlette Request from ASGI payload.

    This function is intended to be used immediately before task invocation
    happens.
    """
    scope, serialized_body = request_wrapper.scope, request_wrapper.body

    # Simulates receiving HTTP body from TCP socket.  In reality, the body has
    # already been streamed in chunks and stored in serialized_body.
    received = False

    async def mock_receive():
        nonlocal received

        # If the request has already been received, starlette will keep polling
        # for HTTP disconnect. We will pause forever. The coroutine should be
        # cancelled by starlette after the response has been sent.
        if received:
            block_forever = asyncio.Event()
            await block_forever.wait()

        received = True
        return {
            "body": serialized_body,
            "type": "http.request",
            "more_body": False
        }

    return starlette.requests.Request(scope, mock_receive)


class Response:
    """ASGI compliant response class.

    It is expected to be called in async context and pass along
    `scope, receive, send` as in ASGI spec.

    >>> from ray.serve.http_util import Response
    >>> scope, receive = ... # doctest: +SKIP
    >>> await Response({"k": "v"}).send(scope, receive, send) # doctest: +SKIP
    """

    def __init__(self, content=None, status_code=200):
        """Construct a HTTP Response based on input type.

        Args:
            content: Any JSON serializable object.
            status_code (int, optional): Default status code is 200.
        """
        self.status_code = status_code
        self.raw_headers = []

        if content is None:
            self.body = b""
            self.set_content_type("text")
        elif isinstance(content, bytes):
            self.body = content
            self.set_content_type("text")
        elif isinstance(content, str):
            self.body = content.encode("utf-8")
            self.set_content_type("text-utf8")
        else:
            # Delayed import since utils depends on http_util
            self.body = json.dumps(
                jsonable_encoder(content,
                                 custom_encoder=serve_encoders)).encode()
            self.set_content_type("json")

    def set_content_type(self, content_type):
        if content_type == "text":
            self.raw_headers.append([b"content-type", b"text/plain"])
        elif content_type == "text-utf8":
            self.raw_headers.append(
                [b"content-type", b"text/plain; charset=utf-8"])
        elif content_type == "json":
            self.raw_headers.append([b"content-type", b"application/json"])
        else:
            raise ValueError("Invalid content type {}".format(content_type))

    async def send(self, scope, receive, send):
        await send({
            "type": "http.response.start",
            "status": self.status_code,
            "headers": self.raw_headers,
        })
        await send({"type": "http.response.body", "body": self.body})


async def receive_http_body(scope, receive, send):
    body_buffer = []
    more_body = True
    while more_body:
        message = await receive()
        assert message["type"] == "http.request"

        more_body = message["more_body"]
        body_buffer.append(message["body"])

    return b"".join(body_buffer)


class RawASGIResponse(ASGIApp):
    """Implement a raw ASGI response interface.

    We have to build this because starlette's base response class is
    still too smart and perform header inference.
    """

    def __init__(self, messages):
        self.messages = messages

    async def __call__(self, _scope, _receive, send):
        for message in self.messages:
            await send(message)

    @property
    def status_code(self):
        return self.messages[0]["status"]


class ASGIHTTPSender(Send):
    """Implement the interface for ASGI sender to save data from varisous
    asgi response type (fastapi, starlette, etc.)
    """

    def __init__(self) -> None:
        self.messages = []

    async def __call__(self, message):
        assert message["type"] in ("http.response.start", "http.response.body")
        self.messages.append(message)

    def build_asgi_response(self) -> RawASGIResponse:
        return RawASGIResponse(self.messages)


def make_fastapi_class_based_view(fastapi_app, cls: Type) -> None:
    """Transform the `cls`'s methods and class annotations to FastAPI routes.

    Modified from
    https://github.com/dmontagu/fastapi-utils/blob/master/fastapi_utils/cbv.py

    Usage:
    >>> from fastapi import FastAPI
    >>> app = FastAPI() # doctest: +SKIP
    >>> class A: # doctest: +SKIP
    ...     @app.route("/{i}") # doctest: +SKIP
    ...     def func(self, i: int) -> str: # doctest: +SKIP
    ...         return self.dep + i # doctest: +SKIP
    >>> # just running the app won't work, here.
    >>> make_fastapi_class_based_view(app, A) # doctest: +SKIP
    >>> # now app can be run properly
    """
    # Delayed import to prevent ciruclar imports in workers.
    from fastapi import Depends, APIRouter
    from fastapi.routing import APIRoute

    def get_current_servable_instance():
        from ray import serve

        return serve.get_replica_context().servable_object

    # Find all the class method routes
    class_method_routes = [
        route for route in fastapi_app.routes if
        # User defined routes must all be APIRoute.
        isinstance(route, APIRoute)
        # We want to find the route that's bound to the `cls`.
        # NOTE(simon): we can't use `route.endpoint in inspect.getmembers(cls)`
        # because the FastAPI supports different routes for the methods with
        # same name. See #17559.
        and (cls.__qualname__ in route.endpoint.__qualname__)
    ]

    # Modify these routes and mount it to a new APIRouter.
    # We need to to this (instead of modifying in place) because we want to use
    # the laster fastapi_app.include_router to re-run the dependency analysis
    # for each routes.
    new_router = APIRouter()
    for route in class_method_routes:
        fastapi_app.routes.remove(route)

        # This block just adds a default values to the self parameters so that
        # FastAPI knows to inject the object when calling the route.
        # Before: def method(self, i): ...
        # After: def method(self=Depends(...), *, i):...
        old_endpoint = route.endpoint
        old_signature = inspect.signature(old_endpoint)
        old_parameters = list(old_signature.parameters.values())
        if len(old_parameters) == 0:
            # TODO(simon): make it more flexible to support no arguments.
            raise RayServeException(
                "Methods in FastAPI class-based view must have ``self`` as "
                "their first argument.")
        old_self_parameter = old_parameters[0]
        new_self_parameter = old_self_parameter.replace(
            default=Depends(get_current_servable_instance))
        new_parameters = [new_self_parameter] + [
            # Make the rest of the parameters keyword only because
            # the first argument is no longer positional.
            parameter.replace(kind=inspect.Parameter.KEYWORD_ONLY)
            for parameter in old_parameters[1:]
        ]
        new_signature = old_signature.replace(parameters=new_parameters)
        setattr(route.endpoint, "__signature__", new_signature)
        setattr(route.endpoint, "_serve_cls", cls)
        new_router.routes.append(route)
    fastapi_app.include_router(new_router)

    routes_to_remove = list()
    for route in fastapi_app.routes:
        if not isinstance(route, APIRoute):
            continue

        # If there is a response model, FastAPI creates a copy of the fields.
        # But FastAPI creates the field incorrectly by missing the outer_type_.
        if route.response_model:
            original_resp_fields = route.response_field.outer_type_.__fields__
            cloned_resp_fields = (
                route.secure_cloned_response_field.outer_type_.__fields__)
            for key, field in cloned_resp_fields.items():
                field.outer_type_ = original_resp_fields[key].outer_type_

        # Remove endpoints that belong to other class based views.
        serve_cls = getattr(route.endpoint, "_serve_cls", None)
        if serve_cls is not None and serve_cls != cls:
            routes_to_remove.append(route)
    fastapi_app.routes[:] = [
        r for r in fastapi_app.routes if r not in routes_to_remove
    ]


def set_socket_reuse_port(sock: socket.socket) -> bool:
    """Mutate a socket object to allow multiple process listening on the same port.

    Returns:
        success: whether the setting was successful.
    """
    try:
        # These two socket options will allow multiple process to bind the the
        # same port. Kernel will evenly load balance among the port listeners.
        # Note: this will only work on Linux.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        # In some Python binary distribution (e.g., conda py3.6), this flag
        # was not present at build time but available in runtime. But
        # Python relies on compiler flag to include this in binary.
        # Therefore, in the absence of socket.SO_REUSEPORT, we try
        # to use `15` which is value in linux kernel.
        # https://github.com/torvalds/linux/blob/master/tools/include/uapi/asm-generic/socket.h#L27
        else:
            sock.setsockopt(socket.SOL_SOCKET, 15, 1)
        return True
    except Exception as e:
        logger.debug(
            f"Setting SO_REUSEPORT failed because of {e}. SO_REUSEPORT is disabled."
        )
        return False


def new_port(lower_bound=10000, upper_bound=65535, denylist=None):
    if not denylist:
        denylist = set()
    port = random.randint(lower_bound, upper_bound)
    retry = 0
    while port in denylist:
        if retry > 100:
            break
        port = random.randint(lower_bound, upper_bound)
        retry += 1
    if retry > 100:
        raise ValueError("Failed to find a new port from the range "
                         f"{lower_bound}-{upper_bound}. Denylist: {denylist}")
    return port


class _ServeCustomEncoders:
    """Group of custom encoders for common types that's not handled by FastAPI."""

    @staticmethod
    def encode_np_array(obj):
        assert isinstance(obj, np.ndarray)
        if obj.dtype.kind == "f":  # floats
            obj = obj.astype(float)
        if obj.dtype.kind in {"i", "u"}:  # signed and unsigned integers.
            obj = obj.astype(int)
        return obj.tolist()

    @staticmethod
    def encode_np_scaler(obj):
        assert isinstance(obj, np.generic)
        return obj.item()

    @staticmethod
    def encode_exception(obj):
        assert isinstance(obj, Exception)
        return str(obj)

    @staticmethod
    def encode_pandas_dataframe(obj):
        assert isinstance(obj, pd.DataFrame)
        return obj.to_dict(orient="records")


serve_encoders = {
    np.ndarray: _ServeCustomEncoders.encode_np_array,
    np.generic: _ServeCustomEncoders.encode_np_scaler,
    Exception: _ServeCustomEncoders.encode_exception,
}

if pd is not None:
    serve_encoders[pd.DataFrame] = _ServeCustomEncoders.encode_pandas_dataframe


class ASGIHandler:

    def __init__(self, controller):
        self.controller = controller

    async def __call__(self, scope, receive, send):
        """Implements the ASGI protocol.

        See details at:
            https://asgi.readthedocs.io/en/latest/specs/index.html.
        """
        await self.controller.handle_asgi(scope, receive, send)


class RelayException(Exception):

    def __init__(self, e):
        self.e = e
        self.stacktrace = "".join(traceback.format_tb(e.__traceback__))


def make_error_response(e):
    if isinstance(e, RelayException):
        msg = str(e.e)
        stacktrace = "".join(traceback.format_tb(
            e.__traceback__)) + e.stacktrace
    else:
        msg = str(e)
        stacktrace = "".join(traceback.format_tb(e.__traceback__))

    return {"type": "error", "message": msg, "stacktrace": stacktrace}
