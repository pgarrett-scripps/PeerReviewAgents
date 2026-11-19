"""The run's own tool server: one object, two doors into the same state.

A review is a panel of agents working on one manuscript through one set of
tools. Those tools are :mod:`peerreviewagents.plugin.server`, and the state
they read and write is that module's, so everything in a run has to reach the
*same* server — the orchestrator, which does the opening and closing itself,
and every agent, which is its own process and needs a wire.

:class:`Service` is that one server, with both doors:

* :meth:`Service.call` is the near door. The orchestrator calls a tool
  directly, in this process, and gets the tool's own return value — no
  schema, no relay, no agent asked to read a dict back to us. Everything the
  recorder agent used to do for us happens here.
* :meth:`Service.listen` opens the far door: a streamable-HTTP MCP endpoint on
  the loopback interface, on a port the kernel picks, served by a thread that
  lives exactly as long as the run. An agent is handed that address and sees
  the same state.

Two properties are worth stating plainly, because the run's integrity rests on
them.

**Who gets in.** The endpoint is bound to ``127.0.0.1``, so nothing off this
machine can reach it, and its port is new for every run and never written
anywhere but into the agents we start. Past that the admission control is the
one the server already had and still has: a tool refuses to do anything for a
slot until that slot has claimed itself with the claim code this run minted for
it, and a code is good for one slot in one run. An agent of another run cannot
present a code this run would take.

**One thing at a time.** The tools are ordinary synchronous functions over
shared state, written for a server that ran them one at a time on a single
event loop. Agents here are separate processes and their calls can arrive at
once, so every synchronous call — through the wire or through
:meth:`Service.call` — is taken under one lock. The server's one asynchronous
tool, ``resolve_citations``, is run on the serving loop rather than under the
lock, because it waits on the network and holding a lock across that wait would
stop the whole panel; that is also exactly how it behaved inside the session.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import socket
import threading
import time

from . import server as srv

#: What a tool is called on the wire: the MCP server's name is ``peerreview``,
#: and a client prefixes every tool with it. The plugin's own prefix carried
#: the plugin's name as well (``mcp__plugin_pra_peerreview__``); nothing is
#: loaded as a plugin any more, so the name is just the server's.
TOOL_PREFIX = 'mcp__peerreview__'
#: How long a caller waits for the serving thread to come up, in seconds.
READY = 30.0
#: How long an in-process call to the one asynchronous tool may take. It walks
#: the reference list over the network in batches and is given the same ceiling
#: a stage gets, not a request's.
ASYNC_TIMEOUT = 1800.0

TOOLS = {tool.__name__: tool for tool in srv.TOOLS}


def free_port():
    """A port nothing is listening on, chosen by the kernel.

    There is a gap between this closing the probe socket and the server
    binding, which is why the port is handed straight to the thread that binds
    it and never reserved in advance.
    """
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


class Service:
    """One run's tool server, reachable in process and over loopback HTTP."""

    def __init__(self, plugin_root=None, host='127.0.0.1'):
        self.host = host
        self.lock = threading.RLock()
        self.port = None
        self.loop = None
        self.thread = None
        self.failure = None
        self._ready = threading.Event()
        # No tools yet: every one of them is registered by :meth:`_wrap`, as a
        # wrapper that holds this run's lock. A name registered twice keeps the
        # first registration, so letting the server register the bare functions
        # would leave the wrappers dropped and the lock doing nothing.
        self._server = srv.create_server(str(plugin_root) if plugin_root else None, tools=())
        self._wrap()

    # -- the near door ----------------------------------------------------

    def call(self, name, *args, **keywords):
        """Call one tool in this process and return what it returns.

        This is the orchestrator's own hand on the tools: ``ingest`` at the
        start, ``write_review`` at the end, the ledger in between. A tool that
        refuses returns its refusal — ``{'request_rejected': ...}``, the same
        dict an agent would be handed — rather than raising, so the caller
        reads it the way :func:`orchestrator.refusal` does. The difference from
        an agent is not the shape but the trust: the dict arrives here intact
        instead of as a sentence somebody has to be relied on to pass on.
        """
        tool = TOOLS.get(name)
        if tool is None:
            raise KeyError(f'the server has no tool called {name!r}')
        if inspect.iscoroutinefunction(tool):
            return self.call_async(tool(*args, **keywords))
        with self.lock:
            return tool(*args, **keywords)

    def call_async(self, coroutine, timeout=ASYNC_TIMEOUT):
        """Run one coroutine tool, on the serving loop when there is one.

        On the serving loop, because that is where an agent's call to the same
        tool would run: one loop means the awaits interleave the way they did
        inside the session instead of running truly beside the panel.
        """
        if self.loop is not None and self.loop.is_running():
            return asyncio.run_coroutine_threadsafe(coroutine, self.loop).result(timeout)
        return asyncio.run(coroutine)

    # -- the far door -----------------------------------------------------

    @property
    def address(self):
        """The MCP endpoint an agent is pointed at, or ``None``."""
        return None if self.port is None else f'http://{self.host}:{self.port}/mcp'

    def configuration(self):
        """The MCP client configuration for this run's one server."""
        return {'mcpServers': {'peerreview': {'type': 'http', 'url': self.address}}}

    def listen(self, timeout=READY):
        """Start serving, and return the address once it accepts connections."""
        if self.port is not None:
            return self.address
        self.port = free_port()
        self.thread = threading.Thread(target=self._serve, name='peerreview-service',
                                       daemon=True)
        self.thread.start()
        if not self._ready.wait(timeout) or not self._accepts(timeout):
            raise RuntimeError(f'the run\'s tool server never came up on port {self.port}'
                               + (f': {self.failure}' if self.failure else ''))
        return self.address

    def stop(self):
        """Stop serving. The thread is a daemon, so this is best effort."""
        loop, self.loop = self.loop, None
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(loop.stop)
        self.port = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.stop()
        return False

    # -- the plumbing -----------------------------------------------------

    def _wrap(self):
        """Register every tool, synchronous ones under the lock."""
        for tool in srv.TOOLS:
            self._server.tool()(tool if inspect.iscoroutinefunction(tool) else self._guard(tool))

    def _guard(self, tool):
        lock = self.lock

        @functools.wraps(tool)
        def guarded(*args, **keywords):
            with lock:
                return tool(*args, **keywords)

        return guarded

    def _serve(self):
        try:
            self._server.settings.host = self.host
            self._server.settings.port = self.port
            self._server.settings.log_level = 'WARNING'
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            self._ready.set()
            self.loop.run_until_complete(self._server.run_streamable_http_async())
        except BaseException as problem:  # noqa: BLE001 - reported, not raised, off-thread
            self.failure = f'{type(problem).__name__}: {problem}'
            self._ready.set()

    def _accepts(self, timeout):
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            if self.failure:
                return False
            try:
                with socket.create_connection((self.host, self.port), timeout=1):
                    return True
            except OSError:
                time.sleep(0.1)
        return False
