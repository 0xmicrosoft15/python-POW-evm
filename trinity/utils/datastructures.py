from asyncio import (
    AbstractEventLoop,
    Lock,
    PriorityQueue,
    QueueFull,
)
from functools import total_ordering
from itertools import count
from typing import (
    Any,
    Callable,
    Dict,
    Generic,
    Set,
    Tuple,
    Type,
    TypeVar,
)

from eth_utils import (
    ValidationError,
)
from eth_utils.toolz import identity

TTask = TypeVar('TTask')
TFunc = TypeVar('TFunc')


class FunctionProperty(Generic[TFunc]):
    """
    A property class purely to convince mypy to let us assign a function to an
    instance variable. See more at: https://github.com/python/mypy/issues/708#issuecomment-405812141
    """
    def __get__(self, oself: Any, owner: Any) -> TFunc:
        return self._func

    def __set__(self, oself: Any, value: TFunc) -> None:
        self._func = value


@total_ordering
class SortableTask(Generic[TTask]):
    _order_fn: FunctionProperty[Callable[[TTask], Any]] = None

    @classmethod
    def orderable_by_func(cls, order_fn: Callable[[TTask], Any]) -> 'Type[SortableTask[TTask]]':
        return type('PredefinedSortableTask', (cls, ), dict(_order_fn=staticmethod(order_fn)))

    def __init__(self, task: TTask) -> None:
        if self._order_fn is None:
            raise ValidationError("Must create this class with orderable_by_func before init")
        self._task = task
        _comparable_val = self._order_fn(task)

        # validate that _order_fn produces a valid comparable
        try:
            self_equal = _comparable_val == _comparable_val
            self_lt = _comparable_val < _comparable_val
            self_gt = _comparable_val > _comparable_val
            if not self_equal or self_lt or self_gt:
                raise ValidationError(
                    "The orderable function provided a comparable value that does not compare"
                    f"validly to itself: equal to self? {self_equal}, less than self? {self_lt}, "
                    f"greater than self? {self_gt}"
                )
        except TypeError as exc:
            raise ValidationError(
                f"The provided order_fn {self._order_fn!r} did not return a sortable "
                f"value from {task!r}"
            ) from exc

        self._comparable_val = _comparable_val

    @property
    def original(self) -> TTask:
        return self._task

    def __eq__(self, other: Any) -> bool:
        if not isinstance(other, SortableTask):
            return False
        else:
            return self._comparable_val == other._comparable_val

    def __lt__(self, other: Any) -> bool:
        if not isinstance(other, SortableTask):
            return False
        else:
            return self._comparable_val < other._comparable_val


class TaskQueue(Generic[TTask]):
    """
    TaskQueue keeps priority-order track of pending tasks, with a limit on number pending.

    A producer of tasks will insert pending tasks with await add(), which will not return until
    all tasks have been added to the queue.

    A task consumer calls await get() to retrieve tasks for processing. Tasks will be returned in
    priority order. If no tasks are pending, get()
    will pause until at least one is available. Only one consumer will have a task "checked out"
    from get() at a time.

    After tasks are successfully completed, the consumer will call complete() to remove them from
    the queue. The consumer doesn't need to complete all tasks, but any uncompleted tasks will be
    considered abandoned. Another consumer can pick it up at the next get() call.
    """

    # a class to wrap the task and make it sortable
    _task_wrapper: Type[SortableTask[TTask]]

    # batches of tasks that have been started but not completed
    _in_progress: Dict[int, Tuple[TTask, ...]]

    # all tasks that have been placed in the queue and have not been started
    _open_queue: 'PriorityQueue[SortableTask[TTask]]'

    # all tasks that have been placed in the queue and have not been completed
    _tasks: Set[TTask]

    def __init__(
            self,
            maxsize: int = 0,
            order_fn: Callable[[TTask], Any] = identity,
            *,
            loop: AbstractEventLoop = None) -> None:
        self._maxsize = maxsize
        self._full_lock = Lock(loop=loop)
        self._open_queue = PriorityQueue(maxsize, loop=loop)
        self._task_wrapper = SortableTask.orderable_by_func(order_fn)
        self._id_generator = count()
        self._tasks = set()
        self._in_progress = {}

    async def add(self, tasks: Tuple[TTask, ...]) -> None:
        """
        add() will insert as many tasks as can be inserted until the queue fills up.
        Then it will pause until the queue is no longer full, and continue adding tasks.
        It will finally return when all tasks have been inserted.
        """
        if not isinstance(tasks, tuple):
            raise ValidationError(f"must pass a tuple of tasks to add(), but got {tasks!r}")

        already_pending = self._tasks.intersection(tasks)
        if already_pending:
            raise ValidationError(
                f"Duplicate tasks detected: {already_pending!r} are already present in the queue"
            )

        # make sure to insert the highest-priority items first, in case queue fills up
        remaining = tuple(sorted(map(self._task_wrapper, tasks)))

        while remaining:
            num_tasks = len(self._tasks)

            if self._maxsize <= 0:
                # no cap at all, immediately insert all tasks
                open_slots = len(remaining)
            elif num_tasks < self._maxsize:
                # there is room to add at least one more task
                open_slots = self._maxsize - num_tasks
            else:
                # wait until there is room in the queue
                await self._full_lock.acquire()

                # the current number of tasks has changed, can't reuse num_tasks
                num_tasks = len(self._tasks)
                open_slots = self._maxsize - num_tasks

            queueing, remaining = remaining[:open_slots], remaining[open_slots:]

            for task in queueing:
                # There will always be room in _open_queue until _maxsize is reached
                try:
                    self._open_queue.put_nowait(task)
                except QueueFull as exc:
                    task_idx = queueing.index(task)
                    qsize = self._open_queue.qsize()
                    raise QueueFull(
                        f'TaskQueue unsuccessful in adding task {task.original!r} ',
                        f'because qsize={qsize}, '
                        f'num_tasks={num_tasks}, maxsize={self._maxsize}, open_slots={open_slots}, '
                        f'num queueing={len(queueing)}, len(_tasks)={len(self._tasks)}, task_idx='
                        f'{task_idx}, queuing={queueing}, original msg: {exc}',
                    )

            original_queued = tuple(task.original for task in queueing)
            self._tasks.update(original_queued)

            if self._full_lock.locked() and len(self._tasks) < self._maxsize:
                self._full_lock.release()

    def get_nowait(self, max_results: int = None) -> Tuple[int, Tuple[TTask, ...]]:
        """
        Get pending tasks. If no tasks are pending, raise an exception.

        :param max_results: return up to this many pending tasks. If None, return all pending tasks.
        :return: (batch_id, tasks to attempt)
        :raise ~asyncio.QueueFull: if no tasks are available
        """
        if self._open_queue.empty():
            raise QueueFull("No tasks are available to get")
        else:
            pending_tasks = self._get_nowait(max_results)

            # Generate a pending batch of tasks, so uncompleted tasks can be inferred
            next_id = next(self._id_generator)
            self._in_progress[next_id] = pending_tasks

            return (next_id, pending_tasks)

    async def get(self, max_results: int = None) -> Tuple[int, Tuple[TTask, ...]]:
        """
        Get pending tasks. If no tasks are pending, wait until a task is added.

        :param max_results: return up to this many pending tasks. If None, return all pending tasks.
        :return: (batch_id, tasks to attempt)
        """
        if max_results is not None and max_results < 1:
            raise ValidationError("Must request at least one task to process, not {max_results!r}")

        # if the queue is empty, wait until at least one item is available
        queue = self._open_queue
        if queue.empty():
            wrapped_first_task = await queue.get()
        else:
            wrapped_first_task = queue.get_nowait()
        first_task = wrapped_first_task.original

        # In order to return from get() as soon as possible, never await again.
        # Instead, take only the tasks that are already available.
        if max_results is None:
            remaining_count = None
        else:
            remaining_count = max_results - 1
        remaining_tasks = self._get_nowait(remaining_count)

        # Combine the first and remaining tasks
        all_tasks = (first_task, ) + remaining_tasks

        # Generate a pending batch of tasks, so uncompleted tasks can be inferred
        next_id = next(self._id_generator)
        self._in_progress[next_id] = all_tasks

        return (next_id, all_tasks)

    def _get_nowait(self, max_results: int = None) -> Tuple[TTask, ...]:
        queue = self._open_queue

        # How many results do we want?
        available = queue.qsize()
        if max_results is None:
            num_tasks = available
        else:
            num_tasks = min((available, max_results))

        # Combine the remaining tasks with the first task we already pulled.
        ranked_tasks = tuple(queue.get_nowait() for _ in range(num_tasks))

        # strip out the wrapper used internally for sorting
        return tuple(task.original for task in ranked_tasks)

    def complete(self, batch_id: int, completed: Tuple[TTask, ...]) -> None:
        if batch_id not in self._in_progress:
            raise ValidationError(f"batch id {batch_id} not recognized, with tasks {completed!r}")

        attempted = self._in_progress.pop(batch_id)

        unrecognized_tasks = set(completed).difference(attempted)
        if unrecognized_tasks:
            self._in_progress[batch_id] = attempted
            raise ValidationError(
                f"cannot complete tasks {unrecognized_tasks!r} in this batch, only {attempted!r}"
            )

        incomplete = set(attempted).difference(completed)

        for task in incomplete:
            # These tasks are already counted in the total task count, so there will be room
            self._open_queue.put_nowait(self._task_wrapper(task))

        self._tasks.difference_update(completed)

        if self._full_lock.locked() and len(self._tasks) < self._maxsize:
            self._full_lock.release()

    def __contains__(self, task: TTask) -> bool:
        """Determine if a task has been added and not yet completed"""
        return task in self._tasks
