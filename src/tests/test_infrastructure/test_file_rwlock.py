"""Unit tests for per-file read-write lock in tool executor helpers."""

import asyncio
import sys
from pathlib import Path


import pytest

from voidx.agent.infrastructure.langgraph.runtime.tool_executor.helpers import (
    _FileRWLock,
    _acquire_file_locks,
    _extract_file_paths,
)


# ---------------------------------------------------------------------------
# _extract_file_paths
# ---------------------------------------------------------------------------
class TestExtractFilePaths:
    def test_read_tool(self):
        paths = _extract_file_paths({"name": "read", "args": {"file_path": "/foo/bar.py"}})
        assert paths == ["/foo/bar.py"]

    def test_write_tool(self):
        paths = _extract_file_paths({"name": "write", "args": {"file_path": "src/x.py"}})
        assert paths == ["src/x.py"]

    def test_replace_tool(self):
        paths = _extract_file_paths({"name": "replace", "args": {"file_path": "lib/util.py", "start_no": 1}})
        assert paths == ["lib/util.py"]


    def test_bash_no_path(self):
        paths = _extract_file_paths({"name": "bash", "args": {"command": "ls"}})
        assert paths == []

    def test_agent_no_path(self):
        paths = _extract_file_paths({"name": "agent", "args": {"mode": "review", "goal": "Review files", "detail": "Check paths.", "scope": "src"}})
        assert paths == []

    def test_normalizes_relative_dots(self):
        paths = _extract_file_paths({"name": "read", "args": {"file_path": "src/../lib/x.py"}})
        assert paths == ["lib/x.py"]

    def test_empty_file_path_skipped(self):
        paths = _extract_file_paths({"name": "write", "args": {"file_path": ""}})
        assert paths == []

    def test_manage_create_single_path_string(self):
        paths = _extract_file_paths({"name": "manage", "args": {"op": "create", "paths": "new.txt"}})
        assert paths == ["new.txt"]

    def test_manage_create_multiple_paths(self):
        paths = _extract_file_paths(
            {"name": "manage", "args": {"op": "create", "paths": ["a.py", "b.py"]}}
        )
        assert paths == ["a.py", "b.py"]

    def test_manage_delete_paths(self):
        paths = _extract_file_paths(
            {"name": "manage", "args": {"op": "delete", "paths": ["x.py", "y.py"]}}
        )
        assert paths == ["x.py", "y.py"]

    def test_manage_move_both_src_and_dest(self):
        paths = _extract_file_paths(
            {"name": "manage", "args": {"op": "move", "moves": [{"src": "a.py", "dest": "b.py"}]}}
        )
        assert sorted(paths) == ["a.py", "b.py"]

    def test_manage_move_multiple_items(self):
        paths = _extract_file_paths(
            {"name": "manage", "args": {"op": "move", "moves": [
                {"src": "a.py", "dest": "c.py"},
                {"src": "b.py", "dest": "d.py"},
            ]}}
        )
        assert sorted(paths) == ["a.py", "b.py", "c.py", "d.py"]

    def test_manage_no_op_returns_empty(self):
        paths = _extract_file_paths({"name": "manage", "args": {"op": "create"}})
        assert paths == []


# ---------------------------------------------------------------------------
# _FileRWLock — basic semantics
# ---------------------------------------------------------------------------
class TestFileRWLockBasic:
    @pytest.mark.asyncio
    async def test_single_read_lock(self):
        lock = _FileRWLock()
        await lock.acquire_read()
        await lock.release_read()

    @pytest.mark.asyncio
    async def test_single_write_lock(self):
        lock = _FileRWLock()
        await lock.acquire_write()
        await lock.release_write()

    @pytest.mark.asyncio
    async def test_concurrent_reads(self):
        """Multiple readers should be able to hold the lock simultaneously."""
        lock = _FileRWLock()
        started = []
        finished = []

        async def reader(i):
            await lock.acquire_read()
            started.append(i)
            await asyncio.sleep(0.05)
            finished.append(i)
            await lock.release_read()

        await asyncio.gather(reader(1), reader(2), reader(3))
        # All three must have started before any finished
        assert len(started) == 3
        # All finished
        assert len(finished) == 3

    @pytest.mark.asyncio
    async def test_writers_are_exclusive(self):
        """Two writers cannot hold the lock at the same time."""
        lock = _FileRWLock()
        concurrent_writers = 0
        max_concurrent = 0

        async def writer(i):
            nonlocal concurrent_writers, max_concurrent
            await lock.acquire_write()
            concurrent_writers += 1
            max_concurrent = max(max_concurrent, concurrent_writers)
            await asyncio.sleep(0.02)
            concurrent_writers -= 1
            await lock.release_write()

        await asyncio.gather(writer(1), writer(2), writer(3))
        assert max_concurrent == 1

    @pytest.mark.asyncio
    async def test_read_blocks_write(self):
        """A writer must wait for all readers to finish."""
        lock = _FileRWLock()
        events = []

        async def reader():
            await lock.acquire_read()
            events.append("r-start")
            await asyncio.sleep(0.05)
            events.append("r-end")
            await lock.release_read()

        async def writer():
            await asyncio.sleep(0.01)  # Ensure reader acquires first
            await lock.acquire_write()
            events.append("w-start")
            await lock.release_write()
            events.append("w-end")

        await asyncio.gather(reader(), writer())
        assert events.index("r-end") < events.index("w-start"), f"Writer started before reader finished: {events}"

    @pytest.mark.asyncio
    async def test_write_blocks_read(self):
        """A reader must wait for a writer to finish."""
        lock = _FileRWLock()
        events = []

        async def writer():
            await lock.acquire_write()
            events.append("w-start")
            await asyncio.sleep(0.05)
            events.append("w-end")
            await lock.release_write()

        async def reader():
            await asyncio.sleep(0.01)  # Ensure writer acquires first
            await lock.acquire_read()
            events.append("r-start")
            await lock.release_read()
            events.append("r-end")

        await asyncio.gather(writer(), reader())
        assert events.index("w-end") < events.index("r-start"), f"Reader started before writer finished: {events}"


# ---------------------------------------------------------------------------
# _FileRWLock — concurrent read + write ordering
# ---------------------------------------------------------------------------
class TestFileRWLockOrdering:
    @pytest.mark.asyncio
    async def test_reader_after_writer_starts_later(self):
        """Reader arriving during writer's wait should not overtake."""
        lock = _FileRWLock()
        events = []

        async def writer():
            await lock.acquire_write()
            events.append("w")
            await asyncio.sleep(0.03)
            await lock.release_write()

        async def reader():
            await asyncio.sleep(0.01)
            await lock.acquire_read()
            events.append("r")
            await lock.release_read()

        await asyncio.gather(writer(), reader())
        assert events == ["w", "r"]

    @pytest.mark.asyncio
    async def test_different_files_independent(self):
        """Locks on different file paths do not block each other."""
        lock_a = _FileRWLock()
        lock_b = _FileRWLock()
        concurrent = 0
        max_concurrent = 0

        async def writer_a():
            nonlocal concurrent, max_concurrent
            await lock_a.acquire_write()
            concurrent += 1
            max_concurrent = max(max_concurrent, concurrent)
            await asyncio.sleep(0.03)
            concurrent -= 1
            await lock_a.release_write()

        async def writer_b():
            nonlocal concurrent, max_concurrent
            await lock_b.acquire_write()
            concurrent += 1
            max_concurrent = max(max_concurrent, concurrent)
            await asyncio.sleep(0.03)
            concurrent -= 1
            await lock_b.release_write()

        await asyncio.gather(writer_a(), writer_b())
        assert max_concurrent == 2, "Different files should allow concurrent writes"

# ---------------------------------------------------------------------------
# Batch-level ordering: file ops complete before non-file ops
# ---------------------------------------------------------------------------
class TestBatchOrdering:
    @pytest.mark.asyncio
    async def test_non_file_ops_wait_for_file_ops(self):
        """Calls without file paths (bash, agent, etc.) must not start
        before all file-path calls (read/write/replace/file) complete."""
        calls = [
            {"name": "write", "args": {"file_path": "a.py"}, "id": "w1"},
            {"name": "bash", "args": {"command": "pytest"}, "id": "b1"},
            {"name": "read", "args": {"file_path": "b.py"}, "id": "r1"},
            {"name": "bash", "args": {"command": "ls"}, "id": "b2"},
        ]

        file_calls = [tc for tc in calls if _extract_file_paths(tc)]
        other_calls = [tc for tc in calls if not _extract_file_paths(tc)]

        assert [tc["name"] for tc in file_calls] == ["write", "read"]
        assert [tc["name"] for tc in other_calls] == ["bash", "bash"]

        execution_order: list[tuple[str, str]] = []
        file_lock_manager: dict[str, _FileRWLock] = {}

        def _get_rwlock(path):
            if path not in file_lock_manager:
                file_lock_manager[path] = _FileRWLock()
            return file_lock_manager[path]

        async def fake_execute(tc):
            execution_order.append(("start", tc["name"]))
            await asyncio.sleep(0.01)
            execution_order.append(("end", tc["name"]))
            return tc["id"]

        async def execute_one_file_locked(tc):
            paths = sorted(set(_extract_file_paths(tc)))
            is_write = tc.get("name") in ("write", "replace", "manage")
            rw_locks = []
            try:
                for p in paths:
                    lk = _get_rwlock(p)
                    rw_locks.append(lk)
                    if is_write:
                        await lk.acquire_write()
                    else:
                        await lk.acquire_read()
                return await fake_execute(tc)
            finally:
                for lk, p in zip(rw_locks, paths):
                    if is_write:
                        await lk.release_write()
                    else:
                        await lk.release_read()

        async def execute_one_no_file_lock(tc):
            return await fake_execute(tc)

        call_index = {tc["id"]: i for i, tc in enumerate(calls)}
        results: list = [None] * len(calls)

        async def _run_and_place(file_group, executor_fn):
            if not file_group:
                return
            group_results = await asyncio.gather(
                *[executor_fn(tc) for tc in file_group]
            )
            for tc, result in zip(file_group, group_results):
                results[call_index[tc["id"]]] = result

        await _run_and_place(file_calls, execute_one_file_locked)
        await _run_and_place(other_calls, execute_one_no_file_lock)

        # All file ops must have finished before any non-file op started
        file_end_positions = [
            i for i, (ev, name) in enumerate(execution_order)
            if ev == "end" and name in ("write", "read")
        ]
        other_start_positions = [
            i for i, (ev, name) in enumerate(execution_order)
            if ev == "start" and name == "bash"
        ]
        assert max(file_end_positions) < min(other_start_positions), (
            f"bash started before file ops finished: {execution_order}"
        )

        assert len(results) == 4
        assert all(r is not None for r in results)



# ---------------------------------------------------------------------------
# Same-file write ordering: line number descending
# ---------------------------------------------------------------------------
class TestSameFileWriteOrdering:
    """Multiple writes to the same file must execute by line number descending
    so that earlier edits don't shift line numbers for later edits."""

    @pytest.mark.asyncio
    async def test_insert_order_descending(self):
        """Two inserts on the same file: higher lineno executes first."""
        from voidx.agent.infrastructure.langgraph.runtime.tool_executor.helpers import _sort_file_calls_by_line_descending

        calls = [
            {"name": "write", "args": {"file_path": "a.py", "op": "insert", "lineno": 5, "new_string": "x"}, "id": "w1"},
            {"name": "write", "args": {"file_path": "a.py", "op": "insert", "lineno": 10, "new_string": "y"}, "id": "w2"},
        ]
        sorted_calls = _sort_file_calls_by_line_descending(calls)
        assert sorted_calls[0]["id"] == "w2"  # lineno 10 first
        assert sorted_calls[1]["id"] == "w1"  # lineno 5 second

    @pytest.mark.asyncio
    async def test_replace_order_descending(self):
        """Two replaces on the same file: higher start line_no executes first."""
        from voidx.agent.infrastructure.langgraph.runtime.tool_executor.helpers import _sort_file_calls_by_line_descending

        calls = [
            {"name": "replace", "args": {"file_path": "a.py", "bounds": [{"line_no": 3, "anchor": "x"}], "new_string": "X"}, "id": "r1"},
            {"name": "replace", "args": {"file_path": "a.py", "bounds": [{"line_no": 8, "anchor": "y"}], "new_string": "Y"}, "id": "r2"},
        ]
        sorted_calls = _sort_file_calls_by_line_descending(calls)
        assert sorted_calls[0]["id"] == "r2"  # line_no 8 first
        assert sorted_calls[1]["id"] == "r1"  # line_no 3 second

    @pytest.mark.asyncio
    async def test_mixed_insert_replace_descending(self):
        """Mixed write+replace on same file: sorted by line number descending."""
        from voidx.agent.infrastructure.langgraph.runtime.tool_executor.helpers import _sort_file_calls_by_line_descending

        calls = [
            {"name": "write", "args": {"file_path": "a.py", "op": "insert", "lineno": 2, "new_string": "x"}, "id": "w1"},
            {"name": "replace", "args": {"file_path": "a.py", "bounds": [{"line_no": 7, "anchor": "y"}], "new_string": "Y"}, "id": "r1"},
            {"name": "write", "args": {"file_path": "a.py", "op": "insert", "lineno": 12, "new_string": "z"}, "id": "w2"},
        ]
        sorted_calls = _sort_file_calls_by_line_descending(calls)
        assert [tc["id"] for tc in sorted_calls] == ["w2", "r1", "w1"]

    @pytest.mark.asyncio
    async def test_different_files_keep_original_order(self):
        """Writes to different files should not be reordered."""
        from voidx.agent.infrastructure.langgraph.runtime.tool_executor.helpers import _sort_file_calls_by_line_descending

        calls = [
            {"name": "write", "args": {"file_path": "a.py", "op": "insert", "lineno": 5, "new_string": "x"}, "id": "w1"},
            {"name": "write", "args": {"file_path": "b.py", "op": "insert", "lineno": 1, "new_string": "y"}, "id": "w2"},
        ]
        sorted_calls = _sort_file_calls_by_line_descending(calls)
        assert [tc["id"] for tc in sorted_calls] == ["w1", "w2"]

    @pytest.mark.asyncio
    async def test_append_treated_as_lowest_priority(self):
        """op=append has no line number; should execute last (lowest priority)."""
        from voidx.agent.infrastructure.langgraph.runtime.tool_executor.helpers import _sort_file_calls_by_line_descending

        calls = [
            {"name": "write", "args": {"file_path": "a.py", "op": "append", "new_string": "tail"}, "id": "w1"},
            {"name": "write", "args": {"file_path": "a.py", "op": "insert", "lineno": 3, "new_string": "x"}, "id": "w2"},
        ]
        sorted_calls = _sort_file_calls_by_line_descending(calls)
        assert sorted_calls[-1]["id"] == "w1"  # append goes last
        assert sorted_calls[0]["id"] == "w2"

    @pytest.mark.asyncio
    async def test_write_full_overwrite_treated_as_lowest(self):
        """op=write (full overwrite) has no line number; executes last."""
        from voidx.agent.infrastructure.langgraph.runtime.tool_executor.helpers import _sort_file_calls_by_line_descending

        calls = [
            {"name": "write", "args": {"file_path": "a.py", "op": "write", "new_string": "full"}, "id": "w1"},
            {"name": "replace", "args": {"file_path": "a.py", "bounds": [{"line_no": 5, "anchor": "x"}], "new_string": "X"}, "id": "r1"},
        ]
        sorted_calls = _sort_file_calls_by_line_descending(calls)
        assert sorted_calls[-1]["id"] == "w1"  # full overwrite goes last
        assert sorted_calls[0]["id"] == "r1"

    @pytest.mark.asyncio
    async def test_replace_two_bounds_uses_min_line_no(self):
        """Two-bound replace uses the smaller line_no for sorting."""
        from voidx.agent.infrastructure.langgraph.runtime.tool_executor.helpers import _sort_file_calls_by_line_descending

        calls = [
            {"name": "replace", "args": {"file_path": "a.py", "bounds": [{"line_no": 3, "anchor": "x"}, {"line_no": 6, "anchor": "y"}], "new_string": "Z"}, "id": "r1"},
            {"name": "replace", "args": {"file_path": "a.py", "bounds": [{"line_no": 8, "anchor": "a"}], "new_string": "A"}, "id": "r2"},
        ]
        sorted_calls = _sort_file_calls_by_line_descending(calls)
        assert sorted_calls[0]["id"] == "r2"  # line_no 8 first
        assert sorted_calls[1]["id"] == "r1"  # min(3,6)=3 second

    @pytest.mark.asyncio
    async def test_reads_not_reordered(self):
        """Reads should not be reordered — they use shared read lock."""
        from voidx.agent.infrastructure.langgraph.runtime.tool_executor.helpers import _sort_file_calls_by_line_descending

        calls = [
            {"name": "read", "args": {"file_path": "a.py", "offset": 10, "limit": 5}, "id": "r1"},
            {"name": "read", "args": {"file_path": "a.py", "offset": 1, "limit": 5}, "id": "r2"},
        ]
        sorted_calls = _sort_file_calls_by_line_descending(calls)
        assert [tc["id"] for tc in sorted_calls] == ["r1", "r2"]

    @pytest.mark.asyncio
    async def test_execution_order_descending_integration(self):
        """Integration: same-file writes actually execute in descending line order."""
        calls = [
            {"name": "write", "args": {"file_path": "a.py", "op": "insert", "lineno": 5, "new_string": "x"}, "id": "w1"},
            {"name": "write", "args": {"file_path": "a.py", "op": "insert", "lineno": 10, "new_string": "y"}, "id": "w2"},
            {"name": "write", "args": {"file_path": "a.py", "op": "insert", "lineno": 2, "new_string": "z"}, "id": "w3"},
        ]

        execution_order: list[str] = []

        async def fake_execute(tc):
            execution_order.append(tc["id"])
            await asyncio.sleep(0.001)
            return tc["id"]

        file_lock_manager: dict[str, _FileRWLock] = {}

        def _get_rwlock(path):
            if path not in file_lock_manager:
                file_lock_manager[path] = _FileRWLock()
            return file_lock_manager[path]

        async def execute_one_file_locked(tc):
            paths = sorted(set(_extract_file_paths(tc)))
            is_write = tc.get("name") in ("write", "replace", "manage")
            rw_locks = []
            try:
                for p in paths:
                    lk = _get_rwlock(p)
                    rw_locks.append(lk)
                    if is_write:
                        await lk.acquire_write()
                    else:
                        await lk.acquire_read()
                return await fake_execute(tc)
            finally:
                for lk, p in zip(rw_locks, paths):
                    if is_write:
                        await lk.release_write()
                    else:
                        await lk.release_read()

        from voidx.agent.infrastructure.langgraph.runtime.tool_executor.helpers import _sort_file_calls_by_line_descending
        file_calls = [tc for tc in calls if _extract_file_paths(tc)]
        sorted_file_calls = _sort_file_calls_by_line_descending(file_calls)

        call_index = {tc["id"]: i for i, tc in enumerate(calls)}
        results: list = [None] * len(calls)

        async def _run_and_place(file_group, executor_fn):
            if not file_group:
                return
            group_results = await asyncio.gather(
                *[executor_fn(tc) for tc in file_group]
            )
            for tc, result in zip(file_group, group_results):
                results[call_index[tc["id"]]] = result

        await _run_and_place(sorted_file_calls, execute_one_file_locked)

        # Execution order must be descending by line number: w2(10), w1(5), w3(2)
        assert execution_order == ["w2", "w1", "w3"], f"Expected descending line order, got {execution_order}"
        assert all(r is not None for r in results)


@pytest.mark.asyncio
async def test_cancelled_waiting_reader_does_not_release_writer_lock():
    lock = _FileRWLock()
    await lock.acquire_write()

    task = asyncio.create_task(
        _acquire_file_locks(["file.py"], is_write=False, get_lock=lambda _path: lock)
    )
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert lock._writer_active is True
    assert lock._readers == 0
    await lock.release_write()


@pytest.mark.asyncio
async def test_cancelled_waiting_writer_does_not_release_reader_lock():
    lock = _FileRWLock()
    await lock.acquire_read()

    task = asyncio.create_task(
        _acquire_file_locks(["file.py"], is_write=True, get_lock=lambda _path: lock)
    )
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert lock._writer_active is False
    assert lock._readers == 1
    await lock.release_read()


@pytest.mark.asyncio
async def test_partial_multi_path_cancellation_releases_only_acquired_locks_in_reverse_order():
    first = _FileRWLock()
    second = _FileRWLock()
    release_order: list[str] = []
    await second.acquire_write()

    original_release = first.release_read

    async def release_first():
        release_order.append("first")
        await original_release()

    first.release_read = release_first
    locks = {"a.py": first, "b.py": second}
    task = asyncio.create_task(
        _acquire_file_locks(
            ["a.py", "b.py"],
            is_write=False,
            get_lock=locks.__getitem__,
        )
    )
    await asyncio.sleep(0)
    assert first._readers == 1

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert release_order == ["first"]
    assert first._readers == 0
    assert second._writer_active is True
    await second.release_write()

    await first.acquire_write()
    await first.release_write()
    await second.acquire_read()
    await second.release_read()
