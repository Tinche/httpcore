import ssl
import sys
from contextlib import suppress
from types import TracebackType
from typing import (
    AsyncIterable,
    AsyncIterator,
    Callable,
    Coroutine,
    List,
    Optional,
    Type,
)

from anyio import create_task_group, get_cancelled_exc_class
from anyio.abc import TaskGroup

from .._exceptions import ConnectionNotAvailable, UnsupportedProtocol
from .._models import Origin, Request, Response
from .._ssl import default_ssl_context
from .._synchronization import AsyncEvent
from ..backends.auto import AutoBackend
from ..backends.base import AsyncNetworkBackend
from .connection import AsyncHTTPConnection
from .interfaces import AsyncConnectionInterface, AsyncRequestInterface


class RequestStatus:
    def __init__(self, request: Request):
        self.request = request
        self.connection: Optional[AsyncConnectionInterface] = None
        self._connection_acquired = AsyncEvent()

    def set_connection(self, connection: AsyncConnectionInterface) -> None:
        assert self.connection is None
        self.connection = connection
        self._connection_acquired.set()

    def unset_connection(self) -> None:
        assert self.connection is not None
        self.connection = None
        self._connection_acquired = AsyncEvent()

    async def wait_for_connection(
        self, timeout: float = None
    ) -> AsyncConnectionInterface:
        await self._connection_acquired.wait(timeout=timeout)
        assert self.connection is not None
        return self.connection


async def await_suppressing(awaitable: Callable[..., Coroutine]) -> None:
    """
    A task group will propagate errors, which is what we don't want when closing connections.
    Hence, this small helper to suppress them.
    """
    with suppress(Exception):
        await awaitable()


class AsyncConnectionPool(AsyncRequestInterface):
    """
    A connection pool for making HTTP requests.
    """

    def __init__(
        self,
        ssl_context: ssl.SSLContext = None,
        max_connections: Optional[int] = 10,
        max_keepalive_connections: int = None,
        keepalive_expiry: float = None,
        http1: bool = True,
        http2: bool = False,
        retries: int = 0,
        local_address: str = None,
        uds: str = None,
        network_backend: AsyncNetworkBackend = None,
    ) -> None:
        """
        A connection pool for making HTTP requests.

        Parameters:
            ssl_context: An SSL context to use for verifying connections.
                If not specified, the default `httpcore.default_ssl_context()`
                will be used.
            max_connections: The maximum number of concurrent HTTP connections that
                the pool should allow. Any attempt to send a request on a pool that
                would exceed this amount will block until a connection is available.
            max_keepalive_connections: The maximum number of idle HTTP connections
                that will be maintained in the pool.
            keepalive_expiry: The duration in seconds that an idle HTTP connection
                may be maintained for before being expired from the pool.
            http1: A boolean indicating if HTTP/1.1 requests should be supported
                by the connection pool. Defaults to True.
            http2: A boolean indicating if HTTP/2 requests should be supported by
                the connection pool. Defaults to False.
            retries: The maximum number of retries when trying to establish a
                connection.
            local_address: Local address to connect from. Can also be used to connect
                using a particular address family. Using `local_address="0.0.0.0"`
                will connect using an `AF_INET` address (IPv4), while using
                `local_address="::"` will connect using an `AF_INET6` address (IPv6).
            uds: Path to a Unix Domain Socket to use instead of TCP sockets.
            network_backend: A backend instance to use for handling network I/O.
        """
        if ssl_context is None:
            ssl_context = default_ssl_context()

        self._ssl_context = ssl_context

        self._max_connections = (
            sys.maxsize if max_connections is None else max_connections
        )
        self._max_keepalive_connections = (
            sys.maxsize
            if max_keepalive_connections is None
            else max_keepalive_connections
        )
        self._max_keepalive_connections = min(
            self._max_connections, self._max_keepalive_connections
        )

        self._keepalive_expiry = keepalive_expiry
        self._http1 = http1
        self._http2 = http2
        self._retries = retries
        self._local_address = local_address
        self._uds = uds

        self._pool: List[AsyncConnectionInterface] = []
        self._requests: List[RequestStatus] = []
        self._network_backend = (
            AutoBackend() if network_backend is None else network_backend
        )
        self._closer: TaskGroup = create_task_group()

    def create_connection(self, origin: Origin) -> AsyncConnectionInterface:
        return AsyncHTTPConnection(
            origin=origin,
            ssl_context=self._ssl_context,
            keepalive_expiry=self._keepalive_expiry,
            http1=self._http1,
            http2=self._http2,
            retries=self._retries,
            local_address=self._local_address,
            uds=self._uds,
            network_backend=self._network_backend,
        )

    @property
    def connections(self) -> List[AsyncConnectionInterface]:
        """
        Return a list of the connections currently in the pool.

        For example:

        ```python
        >>> pool.connections
        [
            <AsyncHTTPConnection ['https://example.com:443', HTTP/1.1, ACTIVE, Request Count: 6]>,
            <AsyncHTTPConnection ['https://example.com:443', HTTP/1.1, IDLE, Request Count: 9]> ,
            <AsyncHTTPConnection ['http://example.com:80', HTTP/1.1, IDLE, Request Count: 1]>,
        ]
        ```
        """
        return list(self._pool)

    def _attempt_to_acquire_connection(self, status: RequestStatus) -> bool:
        """
        Attempt to provide a connection that can handle the given origin.
        """
        origin = status.request.url.origin

        # If there are queued requests in front of us, then don't acquire a
        # connection. We handle requests strictly in order.
        waiting = [s for s in self._requests if s.connection is None]
        if waiting and waiting[0] is not status:
            return False

        # Reuse an existing connection if one is currently available.
        for idx, connection in enumerate(self._pool):
            if connection.can_handle_request(origin) and connection.is_available():
                self._pool.pop(idx)
                self._pool.insert(0, connection)
                status.set_connection(connection)
                return True

        # If the pool is currently full, attempt to close one idle connection.
        if len(self._pool) >= self._max_connections:
            for idx, connection in reversed(list(enumerate(self._pool))):
                if connection.is_idle():
                    self._closer.start_soon(await_suppressing, connection.aclose)
                    self._pool.pop(idx)
                    break

        # If the pool is still full, then we cannot acquire a connection.
        if len(self._pool) >= self._max_connections:
            return False

        # Otherwise create a new connection.
        connection = self.create_connection(origin)
        self._pool.insert(0, connection)
        status.set_connection(connection)
        return True

    def _close_expired_connections(self) -> None:
        """
        Clean up the connection pool by closing off any connections that have expired.
        """
        # Close any connections that have expired their keep-alive time.
        for idx, connection in reversed(list(enumerate(self._pool))):
            if connection.has_expired():
                self._closer.start_soon(await_suppressing, connection.aclose)
                self._pool.pop(idx)

        # If the pool size exceeds the maximum number of allowed keep-alive connections,
        # then close off idle connections as required.
        pool_size = len(self._pool)
        for idx, connection in reversed(list(enumerate(self._pool))):
            if connection.is_idle() and pool_size > self._max_keepalive_connections:
                self._closer.start_soon(await_suppressing, connection.aclose)
                self._pool.pop(idx)
                pool_size -= 1

    def _attempt_starting_queued(self) -> None:
        for status in self._requests:
            if status.connection is None:
                acquired = self._attempt_to_acquire_connection(status)
                # If we could not acquire a connection for a queued request
                # then we don't need to check anymore requests that are
                # queued later behind it.
                if not acquired:
                    break

    async def handle_async_request(self, request: Request) -> Response:
        """
        Send an HTTP request, and return an HTTP response.

        This is the core implementation that is called into by `.request()` or `.stream()`.
        """
        scheme = request.url.scheme.decode()
        if scheme == "":
            raise UnsupportedProtocol(
                "Request URL is missing an 'http://' or 'https://' protocol."
            )
        if scheme not in ("http", "https"):
            raise UnsupportedProtocol(
                f"Request URL has an unsupported protocol '{scheme}://'."
            )

        status = RequestStatus(request)

        self._requests.append(status)
        self._close_expired_connections()
        self._attempt_to_acquire_connection(status)

        while True:
            timeouts = request.extensions.get("timeout", {})
            timeout = timeouts.get("pool", None)
            try:
                connection = await status.wait_for_connection(timeout=timeout)
            except get_cancelled_exc_class():
                # We were cancelled while waiting for a connection.
                self._requests.remove(status)
                if status.connection is not None:
                    self._attempt_starting_queued()
                raise

            try:
                response = await connection.handle_async_request(request)
            except ConnectionNotAvailable:
                # The ConnectionNotAvailable exception is a special case, that
                # indicates we need to retry the request on a new connection.
                #
                # The most common case where this can occur is when multiple
                # requests are queued waiting for a single connection, which
                # might end up as an HTTP/2 connection, but which actually ends
                # up as HTTP/1.1.
                # Maintain our position in the request queue, but reset the
                # status so that the request becomes queued again.
                status.unset_connection()
                self._attempt_to_acquire_connection(status)
            except get_cancelled_exc_class():
                # The task performing this request has been cancelled.
                # Since we don't know the state of the underlying connection,
                # we remove the connection from the pool and close it.
                self._pool.remove(connection)
                self._closer.start_soon(await_suppressing, connection.aclose)
                self._requests.remove(status)

                # Since a connection has been closed, it's possible a different
                # request may now proceed.
                self._attempt_starting_queued()
                raise
            except Exception as exc:
                await self.response_closed(status)
                raise exc
            else:
                break

        # When we return the response, we wrap the stream in a special class
        # that handles notifying the connection pool once the response
        # has been released.
        assert isinstance(response.stream, AsyncIterable)
        return Response(
            status=response.status,
            headers=response.headers,
            content=ConnectionPoolByteStream(response.stream, self, status),
            extensions=response.extensions,
        )

    async def response_closed(self, status: RequestStatus) -> None:
        """
        This method acts as a callback once the request/response cycle is complete.

        It is called into from the `ConnectionPoolByteStream.aclose()` method.
        """
        assert status.connection is not None
        connection = status.connection

        # Update the state of the connection pool.
        self._requests.remove(status)

        if connection.is_closed() and connection in self._pool:
            self._pool.remove(connection)

        # Since we've had a response closed, it's possible we'll now be able
        # to service one or more requests that are currently pending.
        self._attempt_starting_queued()

        # Housekeeping.
        self._close_expired_connections()

    async def aclose(self) -> None:
        """
        Close any connections in the pool.
        """
        for connection in self._pool:
            # It's ok to propagate potential errors here.
            self._closer.start_soon(connection.aclose)
        self._pool = []
        self._requests = []
        await self._closer.__aexit__(None, None, None)

    async def __aenter__(self) -> "AsyncConnectionPool":
        await self._closer.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: Type[BaseException] = None,
        exc_value: BaseException = None,
        traceback: TracebackType = None,
    ) -> None:
        await self.aclose()


class ConnectionPoolByteStream:
    """
    A wrapper around the response byte stream, that additionally handles
    notifying the connection pool when the response has been closed.
    """

    def __init__(
        self,
        stream: AsyncIterable[bytes],
        pool: AsyncConnectionPool,
        status: RequestStatus,
    ) -> None:
        self._stream = stream
        self._pool = pool
        self._status = status

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for part in self._stream:
            yield part

    async def aclose(self) -> None:
        try:
            if hasattr(self._stream, "aclose"):
                await self._stream.aclose()  # type: ignore
        finally:
            await self._pool.response_closed(self._status)
