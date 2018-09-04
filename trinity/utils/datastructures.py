from asyncio import (
    AbstractEventLoop,
    Lock,
    PriorityQueue,
    Queue,
    QueueFull,
)
from collections import defaultdict
from enum import Enum
from functools import total_ordering
from itertools import count
from typing import (
    Any,
    Callable,
    Dict,
    Generator,
    Generic,
    Iterable,
    Set,
    Tuple,
    Type,
    TypeVar,
)

from eth_utils import (
    ValidationError,
    to_tuple,
)
from eth_utils.toolz import (
    concat,
    identity,
)

from trinity.utils.queues import (
    queue_get_batch,
    queue_get_nowait,
)

TFunc = TypeVar('TFunc')
TPrerequisite = TypeVar('TPrerequisite', bound=Enum)
TTask = TypeVar('TTask')
TTaskID = TypeVar('TTaskID')


class StaticMethod(Generic[TFunc]):
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
    _order_fn: StaticMethod[Callable[[TTask], Any]] = None

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
            ranked_tasks = queue_get_nowait(self._open_queue, max_results)

            # strip out the wrapper used internally for sorting
            pending_tasks = tuple(task.original for task in ranked_tasks)

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
        ranked_tasks = await queue_get_batch(self._open_queue, max_results)
        pending_tasks = tuple(task.original for task in ranked_tasks)

        # Generate a pending batch of tasks, so uncompleted tasks can be inferred
        next_id = next(self._id_generator)
        self._in_progress[next_id] = pending_tasks

        return (next_id, pending_tasks)

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

    def num_in_progress(self) -> int:
        """How many tasks are retrieved, but not completed"""
        return len(self._tasks) - self._open_queue.qsize()

    def __len__(self) -> int:
        """How many tasks are queued for completion"""
        return len(self._tasks)

    def __contains__(self, task: TTask) -> bool:
        """Determine if a task has been added and not yet completed"""
        return task in self._tasks


class BaseTaskPrerequisites(Generic[TTask, TPrerequisite]):
    """
    Keep track of which prerequisites on a task are complete. It is used internally by
    :class:`OrderedTaskPreparation`
    """
    _prereqs: Iterable[TPrerequisite]
    _completed_prereqs: Set[TPrerequisite]
    _task: TTask

    @classmethod
    def from_enum(cls, prereqs: Type[TPrerequisite]) -> 'Type[BaseTaskPrerequisites[Any, Any]]':

        if len(prereqs) < 1:
            raise ValidationError("There must be at least one prerequisite to track completions")

        return type('CompletionFor' + prereqs.__name__, (cls, ), dict(_prereqs=prereqs))

    def __init__(self, task: TTask) -> None:
        self._task = task
        self._completed_prereqs = set()

    @property
    def task(self) -> TTask:
        return self._task

    @property
    def is_complete(self) -> bool:
        return len(self.remaining_prereqs) == 0

    def set_complete(self) -> None:
        for prereq in self.remaining_prereqs:
            self.finish(prereq)

    @property
    def remaining_prereqs(self) -> Set[TPrerequisite]:
        return set(self._prereqs).difference(self._completed_prereqs)

    def finish(self, prereq: TPrerequisite) -> None:
        if prereq not in self._prereqs:
            raise ValidationError(
                "Prerequisite %r is not recognized by task %r" % (prereq, self._task)
            )
        elif prereq in self._completed_prereqs:
            raise ValidationError(
                "Prerequisite %r is already complete in task %r" % (prereq, self._task)
            )
        else:
            self._completed_prereqs.add(prereq)

    def __repr__(self) -> str:
        return (
            f'<{type(self).__name__}({self._task!r}, done={self._completed_prereqs!r}, '
            f'remaining={self.remaining_prereqs!r})>'
        )


class OrderedTaskPreparation(Generic[TTask, TTaskID, TPrerequisite]):
    """
    This class is useful when a series of tasks with prerequisites must be run
    sequentially. The prerequisites may be finished in any order, but the
    tasks may only be run when all prerequisites are complete, and the
    dependent task is also complete. Tasks may only depend on one other task.

    For example, you might want to download block bodies and receipts at
    random, but need to import them sequentially. Importing blocks is the ``task``,
    downloading the parts is the ``prerequisite``, and a block's parent is its
    ``dependency``.

    Below is a sketch of how to do that:

        # The complete list of prerequisites to complete
        class BlockDownloads(Enum):
            receipts = auto()
            bodies = auto()

        block_import_tasks = OrderedTaskPreparation(
            BlockDownloads,

            # we use this method to extract an ID from the header:
            lambda header: header.hash,

            # we use this method to extract the ID of the dependency,
            # so that we can guarantee that the parent block gets imported first
            lambda header: header.parent_hash,
        )

        # We mark the genesis block as already imported, so header1 is ready
        # as soon as its prerequisites are complete.
        block_import_tasks.set_finished_dependency(header0)

        # We register the tasks before completing any prerequisites
        block_import_tasks.register_tasks((header1, header2, header3))

        # Start download of bodies & receipts...

        # They complete in random order

        # we notify this class which prerequisites are complete:
        block_import_tasks.finish_prereq(BlockDownloads.receipts, (header2, header3))
        block_import_tasks.finish_prereq(BlockDownloads.bodies, (header1, header2))

        # this await would hang, waiting on the receipt from header1:
        # await block_import_tasks.ready_tasks()

        block_import_tasks.finish_prereq(BlockDownloads.receipts, (header1, ))

        # now we have all the necessary info to import blocks 1 and 2
        headers_ready_to_import = await block_import_tasks.ready_tasks()

        # these will always return in sequential order:
        assert headers_ready_to_import == (header1, header2)

    In a real implementation, you would have a loop waiting on
    :meth:`ready_tasks` and import them, rather than interleaving them like
    the above example.

    Note that this class does *not* track when the main tasks are
    complete. It is assumed that the caller will complete the tasks in the
    order they are returned by ready_tasks().

    The memory needs of this class would naively be unbounded. Any newly
    registered task might depend on any other task in history. To prevent
    unbounded memory usage, old tasks are pruned after a configurable depth.

    Vocab:

    - prerequisites: all these must be completed for a task to be ready
        (a necessary but not sufficient condition)
    - ready: a task is ready after all its prereqs are completed, and the task it depends on is
        also ready
    """
    # methods to extract the id and dependency IDs out of a task
    _id_of: StaticMethod[Callable[[TTask], TTaskID]]
    _dependency_of: StaticMethod[Callable[[TTask], TTaskID]]

    # by default, how long should the integrator wait before pruning?
    _default_max_depth = 10000  # not sure how to pick a good default here

    _prereq_tracker: Type[BaseTaskPrerequisites[TTask, TPrerequisite]]

    def __init__(
            self,
            prerequisites: Type[TPrerequisite],
            id_extractor: Callable[[TTask], TTaskID],
            dependency_extractor: Callable[[TTask], TTaskID],
            max_depth: int = None) -> None:

        self._prereq_tracker = BaseTaskPrerequisites.from_enum(prerequisites)
        self._id_of = id_extractor
        self._dependency_of = dependency_extractor
        self._oldest_depth = 0

        # how long to wait before pruning
        if max_depth is None:
            self._max_depth = self._default_max_depth
        else:
            self._max_depth = min([self._default_max_depth, max_depth])

        # all of the tasks that have been completed, and not pruned
        self._tasks: Dict[TTaskID, BaseTaskPrerequisites[TTask, TPrerequisite]] = {}

        # In self._dependencies, when the key becomes ready, the task ids in the
        # value set *might* also become ready
        # (they only become ready if their prerequisites are complete)
        self._dependencies: Dict[TTaskID, Set[TTaskID]] = defaultdict(set)

        # task ids are in this set if either:
        # - one of their prerequisites is incomplete OR
        # - their dependent task is not ready
        self._unready: Set[TTaskID] = set()

        # This is a queue of tasks that have become ready, in order.
        # They wait in this Queue until being returned by ready_tasks().
        self._ready_tasks: 'Queue[TTask]' = Queue()

        # Track the depth from the original task at 0 to n dependencies away
        # This is used exclusively for pruning
        self._depths: Dict[TTaskID, int] = {}

    def set_finished_dependency(self, finished_task: TTask) -> None:
        """
        Mark this task as already finished. Crucially, the first task that is registered in
        :meth:`register_tasks` must have finished_task as its dependency.
        """
        if len(self._tasks) > 0 or self._oldest_depth != 0:
            raise ValidationError(f"Tasks already added, cannot set {finished_task!r} as finished")

        completed = self._prereq_tracker(finished_task)
        completed.set_complete()
        task_id = self._id_of(finished_task)
        self._tasks[task_id] = completed
        self._depths[task_id] = 0
        # note that this task is intentionally *not* added to self._unready

    def register_tasks(self, tasks: Tuple[TTask, ...]) -> None:
        """
        Initiate a task into tracking. Each task must be registered *after* its dependency has
        been registered.

        :param tasks: the tasks to register, in iteration order
        """
        task_meta_info = (
            (self._prereq_tracker(task), self._id_of(task), self._dependency_of(task))
            for task in tasks
        )

        for prereq_tracker, task_id, dependency_id in task_meta_info:
            if dependency_id not in self._tasks:
                raise ValidationError(
                    f"Cannot prepare task {prereq_tracker!r} with id {task_id} and "
                    f"dependency {dependency_id} before preparing its dependency"
                )
            else:
                self._tasks[task_id] = prereq_tracker
                self._unready.add(task_id)
                self._dependencies[dependency_id].add(task_id)
                depth = self._depths[dependency_id] + 1
                self._depths[task_id] = depth

    def finish_prereq(self, prereq: TPrerequisite, tasks: Tuple[TTask, ...]) -> None:
        """For every task in tasks, mark the given prerequisite as completed"""
        if len(self._tasks) == 0:
            raise ValidationError("Cannot finish a task until set_last_completion() is called")

        for task in tasks:
            task_id = self._id_of(task)
            if task_id not in self._tasks:
                raise ValidationError(f"Cannot finish task {task_id!r} before preparing it")

            task_completion = self._tasks[task_id]
            task_completion.finish(prereq)
            if task_completion.is_complete and self._dependency_of(task) not in self._unready:
                self._mark_complete(task_id)

    async def ready_tasks(self) -> Tuple[TTask, ...]:
        """
        Return the next batch of tasks that are ready to process. If none are ready,
        hang until at least one task becomes ready.
        """
        return await queue_get_batch(self._ready_tasks)

    def _mark_complete(self, task_id: TTaskID) -> None:
        qualified_tasks = tuple([task_id])
        while qualified_tasks:
            qualified_tasks = tuple(concat(
                self._mark_one_task_complete(task_id)
                for task_id in qualified_tasks
            ))

    @to_tuple
    def _mark_one_task_complete(self, task_id: TTaskID) -> Generator[TTaskID, None, None]:
        """
        Called when this task is completed and its dependency is complete, for the first time

        :return: any task IDs that can now also be marked as complete
        """
        task_completion = self._tasks[task_id]

        # put this task in the completed queue
        self._ready_tasks.put_nowait(task_completion.task)

        # note that this task has been made ready
        self._unready.remove(task_id)

        # prune any completed tasks that are too old
        self._prune(task_id)

        # resolve tasks that depend on this task
        for depending_task_id in self._dependencies[task_id]:
            # we already know that this task is ready, so we only need to check completion
            if self._tasks[depending_task_id].is_complete:
                yield depending_task_id

    def _prune(self, task_id: TTaskID) -> None:
        """
        This prunes any data starting at the task completed at task_completion, and older.
        It is called when the task becomes ready.
        """
        # determine how far back to prune
        finished_depth = self._depths[task_id]

        prune_depth = finished_depth - self._max_depth
        if prune_depth > self._oldest_depth:

            for depth in range(self._oldest_depth, prune_depth):
                prune_tasks = tuple(
                    task_id for task_id in self._tasks.keys()
                    if self._depths[task_id] == depth
                )

                for prune_task_id in prune_tasks:
                    del self._tasks[prune_task_id]
                    del self._dependencies[prune_task_id]
                    del self._depths[prune_task_id]

            self._oldest_depth = prune_depth
