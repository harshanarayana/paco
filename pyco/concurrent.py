# -*- coding: utf-8 -*-
"""Coroutines concurrent pool executor with built-in
concurrency limit based on a semaphore free slots algorithm.

Usage::

    async def fetch(url):
        r = await aiohttp.get(url)
        return await r.read()
    # limit the concurrent coroutines to 3
    pool = concurrent(3)
    for _ in range(10):
        p.submit(fetch, 'http://www.baidu.com')
    await p.join()
"""
import asyncio
from collections import deque, namedtuple
from .observer import Observer
from .utils import isiter

# Task represents an immutable tuple storing the index order
# and coroutine object.
Task = namedtuple('Task', ['index', 'coro'])


@asyncio.coroutine
def safe_run(coro, return_exceptions=False):
    try:
        result = yield from coro
    except Exception as err:
        if return_exceptions:
            result = err
        else:
            raise err
    return result


@asyncio.coroutine
def collect(coro, index, results,
            preserve_order=False,
            return_exceptions=False):
    result = yield from safe_run(coro, return_exceptions=return_exceptions)

    if preserve_order:
        results[index] = result
    else:
        results.append(result)


class ConcurrentExecutor(object):
    """
    Concurrent executes a set of asynchronous coroutines
    with a simple throttle concurrency configurable concurrency limit.

    Implements an observer pub/sub interface, allowing API consumers to
    subscribe functions or coroutines to events.

    ConcurrentExecutor is a low-level implementation that powers .
    For most cases you won't need to rely on it, instead you can
    use the utility functions that provides a higher and simpler abstraction.

    This class is not thread safe.

    Events:
        - start (executor): triggered before executor cycle starts.
        - finish (executor): triggered when all the coroutine finished.
        - task.start (task): triggered before coroutine starts.
        - task.finish (task, result): triggered when the coroutine finished.

    Usage::

        pool = ConcurrentExecutor(3)
        pool.add(coroutine, 'foo', 1)
        pool.add(coroutine, 'foo', 1)
        pool.add(coroutine, 'foo', 1)
        await pool.run(return_exceptions=True)
    """
    def __init__(self, limit=10, loop=None, coros=None):
        """
        Creates a new ConcurrentExecutor instance.

        Arguments:
            limit (int): concurrency limit. Defaults to 10.
            loop (asyncio.BaseEventLoop): optional loop to run.
                Defaults to asyncio.get_event_loop().

        Returns:
            ConcurrentExecutor
        """
        self.running = False
        self.return_exceptions = False
        self.limit = max(int(limit), 0)
        self.pool = deque()
        self.observer = Observer()
        self.loop = loop or asyncio.get_event_loop()
        self.throttler = asyncio.Semaphore(self.limit, loop=self.loop)

        # Register coroutines in the pool
        if isiter(coros):
            self.extend(*coros)

    def reset(self):
        """
        Resets the executer scheduler internal state.

        Raises:
            RuntimeError: is the executor is still running.
        """
        if self.running:
            raise RuntimeError('executor is still running')

        self.pool.clear()
        self.observer.clear()
        self.throttler = asyncio.Semaphore(self.limit, loop=self.loop)

    def cancel(self):
        """
        Tries to gracefully cancel the pending coroutine scheduled
        coroutine tasks.
        """
        self.pool.clear()
        self.running = False

    def on(self, event, fn):
        """
        Subscribes to a specific event.

        Arguments:
            event (str): event name to subcribe.
            fn (function): function to trigger.
        """
        return self.observer.on(event, fn)

    def off(self, event):
        """
        Removes event subscribers.

        Arguments:
            event (str): event name to remove observers.
        """
        return self.observer.off(event)

    def extend(self, *coros):
        """
        Add multiple coroutines to the executor pool.

        Raises:
            TypeError: if the coro object is not a valid coroutine
        """
        for coro in coros:
            self.add(coro)

    def add(self, coro, *args, **kw):
        """
        Adds a new coroutine function with optional variadic argumetns.

        Arguments:
            coro (coroutine function): coroutine to execute.
            *args (mixed): optional variadic arguments

        Raises:
            TypeError: if the coro object is not a valid coroutine

        Returns:
            future: coroutine wrapped future
        """
        # Create coroutine object if a function is provided
        if asyncio.iscoroutinefunction(coro):
            coro = coro(*args, **kw)

        # Verify coroutine
        if not asyncio.iscoroutine(coro):
            raise TypeError('coro must be a coroutine object')

        # Store coroutine with arguments for deferred execution
        index = max(len(self.pool), 0)
        task = Task(index, coro)

        # Append the coroutine data to the pool
        self.pool.append(task)

        return coro

    # Alias to add()
    submit = add

    @asyncio.coroutine
    def _run_sequentially(self):
        # Store futures in two queues
        done, pending = [], []

        # Run until the pool is empty
        while len(self.pool):
            future = asyncio.Future(loop=self.loop)
            pending.append(future)

            # Run coroutine
            result = yield from self._run_coro((self.pool.popleft()))

            # Assign result to future
            if isinstance(result, Exception):
                if not self.return_exceptions:
                    raise result
                future.set_exception(result)
            else:
                future.set_result(result)

            # Swap future between queues
            future = pending.pop()
            done.append(future)

        # Build futures tuple to be compatible with asyncio.wait() interface
        return set(done), set(pending)

    @asyncio.coroutine
    def _run_concurrently(self, timeout=None, return_when=None):
        coros = []
        limit = self.limit

        while len(self.pool):
            task = self.pool.popleft()

            # Run without concurrency limit
            if limit <= 0:
                coros.append(self._run_coro(task))
            # Otherwise, schedule for concurrent based flow
            else:
                coros.append(self._schedule_coro(task))

        # Wait until all the coroutines finish
        return (yield from asyncio.wait(coros,
                                        timeout=timeout,
                                        return_when=return_when))

    @asyncio.coroutine
    def _run_coro(self, task):
        # Executor must be running
        if not self.running:
            return None

        # Trigger task pre-execution event
        yield from self.observer.trigger('task.start', task)

        # Trigger coroutine task
        index, coro = task

        # Safe coroutine execution
        result = yield from safe_run(coro,
                                     return_exceptions=self.return_exceptions)

        # Trigger task post-execution event
        yield from self.observer.trigger('task.finish', task, result)

        # Return result to future binding
        return result

    @asyncio.coroutine
    def _schedule_coro(self, task):
        """
        Executes a given coroutine in the next available slot.

        Slots are available based on a simple free slots
        scheduling semaphore-based algorithm.
        """
        # Run when a slot is available
        with (yield from self.throttler):
            return (yield from self._run_coro(task))

    @asyncio.coroutine
    def run(self, timeout=None,
            return_exceptions=None,
            return_when='ALL_COMPLETED'):
        """
        Executes the registered coroutines in the executor queue.

        Arguments:
            timeout (int/float): max execution timeout. No limit by default.

        Returns:
            asyncio.Future (tuple): two sets of Futures: (done, pending)

        Raises:
            ValueError: if there is no coroutines to schedule.
            RuntimeError: if executor is still running.
            TimeoutError: if execution takes more than expected.
        """
        # Only allow 1 concurrent execution
        if self.running:
            raise RuntimeError('executor is already running')

        # Check we have coroutines to schedule
        if len(self.pool) == 0:
            raise ValueError('no coroutines to schedule')

        # Set executor state to running
        self.running = True

        # Configure return exceptions
        if return_exceptions is not None:
            self.return_exceptions = return_exceptions

        # Trigger pre-execution event
        self.observer.trigger('start', self)

        # Sequential coroutines execution
        if self.limit == 1:
            done, pending = yield from self._run_sequentially()

        # Concurrent execution based on configured limit
        if self.limit != 1:
            done, pending = yield from self._run_concurrently(
                timeout=timeout,
                return_when=return_when)

        # Reset internal state and queue
        self.running = False

        # Trigger pre-execution event
        self.observer.trigger('finish', self)

        # Reset executor state to defaults after each execution
        self.reset()

        # Return resultant futures in two tuples
        return done, pending

    # Idiomatic method alias to run()
    wait = run

    def is_running(self):
        """
        Checks the executor running state.

        Returns:
            bool: True if the executur is running, otherwise False.
        """
        return self.running

# Semantic shortcut to ConcurrentExecutor()
concurrent = ConcurrentExecutor