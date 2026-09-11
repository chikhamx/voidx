"""Terminal frame rendering helpers."""

from __future__ import annotations

import io
import asyncio
import re
import shutil
import sys
import time
from dataclasses import dataclass, replace

from rich.cells import cell_len
from rich.console import Console, Group
from rich.text import Text

from voidx.presentation.output.dock import dock
from voidx.presentation.output.dock.formatting import text_from_line
from .commit_output import scrolled_frame_payload
from .helpers import (
    _BEGIN_SYNCHRONIZED_OUTPUT,
    _END_SYNCHRONIZED_OUTPUT,
    _rendered_row_count,
)
from .layout import (
    BottomViewportPlan,
    LayoutSnapshot,
    LogicalRenderPlan,
    BottomGeometry,
    PhysicalViewportPlan,
    RegionGeometry,
    RenderedRows,
    SourceSlice,
    normalize_rendered_rows,
    project_bottom_viewport,
    diff_layout,
    project_physical_viewport,
)
from .state import RenderStats
from .terminal_writer import FrameBatch, FrameResult


@dataclass(frozen=True)
class _RenderPlan:
    width: int
    height: int
    status_lines: tuple[object, ...]
    panel_lines: tuple[str, ...]
    busy_activity_elements: tuple[Text, ...]
    thinking_stream_elements: tuple[Text, ...]
    base_bottom_rows: int
    panel_rows: int
    panel_ansi: str | None
    bottom_elements: tuple[object, ...]
    panel_elements: tuple[Text, ...]
    input_rows: tuple[int, ...]
    logical_plan: LogicalRenderPlan | None






class _FrameRendererMixin:
    def _frame_width(self) -> int:
        return max((self._console.width or 80) - 1, 20)

    def _terminal_writer_worker_mode(self) -> bool:
        return bool(
            getattr(self, "_terminal_writer_required", False)
            or getattr(self._terminal_writer, "worker_mode", False)
        )

    def _layout_snapshot_for_frame(
        self,
        *,
        generation: int,
        width: int,
        term_height: int,
        start_row: int,
        lines: list[str],
        frame_rows: int,
        bottom_rows: int,
        lines_up: int,
        cursor_ansi: str,
    ) -> LayoutSnapshot | None:
        if frame_rows < 1 or frame_rows > term_height:
            return None
        if start_row < 1 or start_row + frame_rows - 1 > term_height:
            return None
        if bottom_rows < 1 or bottom_rows > frame_rows:
            return None

        cursor_row = start_row + max(frame_rows - lines_up - 1, 0)
        cursor_row = max(start_row, min(cursor_row, start_row + frame_rows - 1))
        cursor_col = 1
        match = re.search(r"\x1b\[(?:\d+)?A\x1b\[(\d+)G", cursor_ansi)
        if match is not None:
            cursor_col = max(1, min(int(match.group(1)), width))

        frame_rendered = normalize_rendered_rows("\n".join(lines), width=width)
        frame_region = RegionGeometry(
            key="frame",
            start_row=start_row,
            visual_rows=frame_rendered.visual_rows,
            width=width,
            content_signature=frame_rendered.signature,
            patch_safe=frame_rendered.patch_safe,
        )
        bottom_start = start_row + frame_rows - bottom_rows
        bottom_rendered = normalize_rendered_rows(
            "\n".join(lines[-bottom_rows:]),
            width=width,
        )
        bottom_region = RegionGeometry(
            key="bottom",
            start_row=bottom_start,
            visual_rows=bottom_rendered.visual_rows,
            width=width,
            content_signature=bottom_rendered.signature,
            patch_safe=bottom_rendered.patch_safe,
        )
        empty = RegionGeometry(
            key="bottom.empty",
            start_row=bottom_start,
            visual_rows=0,
            width=width,
            content_signature=(),
            patch_safe=True,
        )
        bottom = BottomGeometry(
            rendered=bottom_rendered,
            region=bottom_region,
            top_separator=empty,
            input=RegionGeometry(
                key="bottom.input",
                start_row=bottom_start,
                visual_rows=bottom_rows,
                width=width,
                content_signature=bottom_rendered.signature,
                patch_safe=bottom_rendered.patch_safe,
            ),
            middle_separator=empty,
            panel=empty,
            panel_status_separator=empty,
            status=empty,
            cursor_row=cursor_row,
            cursor_col=cursor_col,
        )
        return LayoutSnapshot(
            terminal_width=width,
            terminal_height=term_height,
            frame_start_row=start_row,
            frame_rows=frame_rows,
            regions=(frame_region,),
            source_slices=(),
            bottom=bottom,
            cursor_row=cursor_row,
            cursor_col=cursor_col,
            scroll_epoch=self._scroll_epoch,
            generation=generation,
            restore_epoch=getattr(self, "_restore_epoch", 0),
        )
    def _invalidate_layout(
        self,
        reason: str,
        *,
        advances_scroll_epoch: bool = True,
    ) -> None:
        """Invalidate physical layout snapshots at an absolute terminal boundary."""
        if reason in {"clear", "resize", "terminal_submission_failure", "overflow"}:
            self._invalidate_bottom_anchor()
            self._render_state.applied_temporary_panel = False
        self._applied_layout_snapshot = None
        self._pending_layout_snapshots.clear()
        self._pending_layout_force_full.clear()
        pending_frame_states = getattr(self, "_pending_frame_states", None)
        if pending_frame_states is not None:
            pending_frame_states.clear()
        self._full_layout_invalidated = True
        if advances_scroll_epoch:
            self._scroll_epoch += 1

    def _invalidate_layout_snapshots_for_commit(self) -> None:
        """Drop pre-commit layout snapshots without forcing a full repaint.

        A pending commit shifts the frame start row on the worker, so
        snapshots captured before the commit would patch stale absolute
        rows. The worker keeps its own baseline intact (preserve_baseline),
        therefore the next frame may still diff.
        """
        self._applied_layout_snapshot = None
        self._pending_layout_snapshots.clear()
        self._pending_layout_force_full.clear()
        pending_frame_states = getattr(self, "_pending_frame_states", None)
        if pending_frame_states is not None:
            pending_frame_states.clear()
        self._scroll_epoch += 1

    def _handle_terminal_submission_failure(
        self,
        operation: str,
        error: BaseException,
        *,
        layout_already_invalidated: bool = False,
    ) -> None:
        """Invalidate terminal state once after a submission or token failure."""
        del operation, error
        if self._terminal_submission_failed:
            self._running = False
            return
        self._terminal_submission_failed = True
        self._render_state.applied_temporary_panel = False
        self._invalidate_bottom_anchor()
        if layout_already_invalidated:
            self._applied_layout_snapshot = None
            self._pending_layout_snapshots.clear()
            self._pending_layout_force_full.clear()
            pending_frame_states = getattr(self, "_pending_frame_states", None)
            if pending_frame_states is not None:
                pending_frame_states.clear()
            self._full_layout_invalidated = True
        else:
            self._invalidate_layout("terminal_submission_failure")
        self._running = False

    def _handle_sync_terminal_failure(self, error: BaseException) -> None:
        recover = getattr(self._terminal_writer, "_recover_sync_failure", None)
        if callable(recover):
            try:
                recover(error)
            except BaseException:
                pass
        self._invalidate_frame_cache()
        self._handle_terminal_submission_failure("sync_frame", error)

    def _invalidate_pending_layout_after_submit_failure(
        self,
        error: BaseException,
    ) -> None:
        self._handle_terminal_submission_failure("frame_enqueue", error)

    def _pending_worker_frame_states(self) -> dict[int, dict[str, object]]:
        states = getattr(self, "_pending_frame_states", None)
        if states is None:
            states = {}
            self._pending_frame_states = states
        return states

    def _apply_worker_frame_state(self, state: dict[str, object]) -> None:
        self._visible_committed_rows = int(state["visible_rows"])
        frame_rows = int(state["frame_rows"])
        start_row = int(state["start_row"])
        bottom_rows = int(state["bottom_rows"])
        self._last_frame_rows = frame_rows
        self._last_frame_start_row = start_row
        self._last_bottom_rows = bottom_rows
        self._last_bottom_start_row = start_row + frame_rows - bottom_rows

        busy_rows = int(state["busy_activity_rows"])
        if busy_rows > 0:
            self._record_busy_activity_layout(
                start_row=int(state["busy_activity_start_row"]),
                rows=busy_rows,
                width=int(state["width"]),
                term_height=state["term_height"],
                bottom_rows=bottom_rows,
                thinking_rows=int(state["thinking_stream_rows"]),
            )
        else:
            self._invalidate_busy_activity_layout()
        self._record_input_cursor_geometry(frame_rows, int(state["lines_up"]))
        self._has_rendered_frame = True
        self._prev_frame_lines = list(state["target_lines"])
        self._prev_frame_start_row = start_row
        self._prev_frame_width = int(state["width"])
        self._prev_frame_term_height = state["term_height"]
        self._bottom_region_dirty = False
        self._last_render_plan = state["render_plan"]

    def _track_pending_terminal_operation(
        self,
        token,
        *,
        kind: str,
        apply_state=None,
    ) -> None:
        wait = getattr(self._terminal_writer, "wait", None)
        if not callable(wait):
            if apply_state is not None:
                apply_state()
            return
        if not (hasattr(token, "_future") or hasattr(token, "future")):
            if apply_state is not None:
                apply_state()
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        token_key = id(token)
        operation = {
            "kind": kind,
            "token": token,
            "scroll_epoch": self._scroll_epoch,
            "after_generation": self._submitted_generation,
            "apply_state": apply_state,
        }
        self._pending_terminal_operations[token_key] = operation
        operation["task"] = loop.create_task(
            self._wait_for_pending_terminal_operation(token)
        )

    async def _wait_for_pending_terminal_operation(self, token) -> None:
        token_key = id(token)
        operation = self._pending_terminal_operations.get(token_key)
        if operation is None:
            return
        try:
            await self._terminal_writer.wait(token)
        except asyncio.CancelledError:
            self._pending_terminal_operations.pop(token_key, None)
        except BaseException as exc:
            self._pending_terminal_operations.pop(token_key, None)
            self._handle_terminal_submission_failure(operation["kind"], exc)
        else:
            self._apply_pending_terminal_operation(token)

    def _apply_pending_terminal_operation(self, token) -> None:
        operation = self._pending_terminal_operations.pop(id(token), None)
        if operation is not None:
            apply_state = operation.get("apply_state")
            if apply_state is not None:
                apply_state()

    def _apply_completed_barriers_before_frame(self, generation: int) -> None:
        # Future completion is queued before FrameResult, but its waiter may run later.
        for operation in tuple(self._pending_terminal_operations.values()):
            if (
                operation["kind"] != "barrier"
                or operation["after_generation"] >= generation
            ):
                continue
            token = operation["token"]
            future = getattr(token, "_future", None)
            if future is None:
                future = getattr(token, "future", None)
            if (
                isinstance(future, asyncio.Future)
                and future.done()
                and not future.cancelled()
                and future.exception() is None
            ):
                self._apply_pending_terminal_operation(token)

    def _submit_terminal_barrier(
        self,
        *,
        kind: str,
        ansi: str = "",
        invalidate_frame: bool = False,
        apply_state=None,
    ):
        invalidates = invalidate_frame or kind in {"clear", "scroll", "resize"}
        if invalidates:
            self._invalidate_layout(kind)
        kwargs = {"kind": kind}
        if ansi:
            kwargs["ansi"] = ansi
        if invalidate_frame:
            kwargs["invalidate_frame"] = True
        try:
            token = self._terminal_writer.submit_barrier(**kwargs)
        except Exception as exc:
            self._handle_terminal_submission_failure(
                kind,
                exc,
                layout_already_invalidated=invalidates,
            )
            raise
        self._track_pending_terminal_operation(
            token,
            kind="barrier",
            apply_state=apply_state,
        )
        operation = self._pending_terminal_operations.get(id(token))
        if operation is not None:
            operation["barrier_kind"] = kind
        return token

    def _handle_terminal_frame_result(self, result: FrameResult) -> None:
        snapshot = self._pending_layout_snapshots.get(result.generation)
        if snapshot is None:
            return
        if not result.applied:
            return
        frame_states = self._pending_worker_frame_states()
        if (
            snapshot.scroll_epoch != self._scroll_epoch
            or getattr(snapshot, "restore_epoch", 0) != getattr(self, "_restore_epoch", 0)
        ):
            self._pending_layout_snapshots.pop(result.generation, None)
            self._pending_layout_force_full.pop(result.generation, None)
            frame_states.pop(result.generation, None)
            return
        self._apply_completed_barriers_before_frame(result.generation)
        force_full = self._pending_layout_force_full.get(result.generation, False)
        frame_state = frame_states.get(result.generation)
        if frame_state is not None:
            self._apply_worker_frame_state(frame_state)
        self._record_applied_layout(snapshot)
        for generation in tuple(self._pending_layout_snapshots):
            if generation <= result.generation:
                self._pending_layout_snapshots.pop(generation, None)
                self._pending_layout_force_full.pop(generation, None)
        for generation in tuple(frame_states):
            if generation <= result.generation:
                frame_states.pop(generation, None)
        if self._full_layout_invalidated and force_full:
            self._full_layout_invalidated = False
        self._render_stats = RenderStats(
            total_lines=result.total_lines,
            changed_lines=result.changed_lines,
            render_ms=result.render_ms,
            strategy=result.strategy,
        )

    def _render_frame(self) -> None:
        """Render to terminal: capture Rich output, write with cursor control."""
        started_at = time.perf_counter()
        width = self._frame_width()
        term_height = shutil.get_terminal_size().lines if self._tty else None
        render_failed = False
        worker_mode = self._tty and self._terminal_writer_worker_mode()
        if worker_mode and self._render_state.pending_commit_tokens:
            return
        if worker_mode and any(
            operation.get("barrier_kind") == "scroll"
            for operation in self._pending_terminal_operations.values()
        ):
            self.invalidate()
            return
        resize_frame = False
        clear_screen = False
        clear_submitted = False
        committed_before_clear = None
        force_full = False
        full_frame_repaint = False
        if self._tty:
            term_height = term_height or shutil.get_terminal_size().lines
            resize_frame = self._prev_frame_width != 0 and (
                self._prev_frame_width != width
                or self._prev_frame_term_height != term_height
            )
            clear_screen = dock.consume_clear_screen_request()
            if worker_mode and clear_screen:
                committed_before_clear = (
                    self._committed_line_count,
                    self._committed_projection,
                    self._visible_committed_rows,
                )
                self._committed_line_count = 0
                self._committed_projection = None
                self._visible_committed_rows = 0
            full_frame_repaint = self._full_frame_repaint_pending
            self._full_frame_repaint_pending = False
            force_full = (
                self._full_layout_invalidated
                or resize_frame
                or clear_screen
                or full_frame_repaint
            )
            if not worker_mode:
                if resize_frame:
                    self._invalidate_frame_cache()
                    self._invalidate_layout("resize")
                if clear_screen:
                    self._committed_line_count = 0
                    self._committed_projection = None
                    self._visible_committed_rows = 0
                    self._invalidate_frame_cache()
                    self._invalidate_layout("clear")

        self._render_plan = None
        try:
            try:
                renderable = self._render_impl(height=term_height, capture_plan=True)
                render_plan = self._render_plan
            except Exception as exc:
                import traceback

                render_plan = None
                render_failed = True
                if self._tty:
                    force_full = True
                self._pending_tb = traceback.format_exc()
                self._last_error = f"Render error: {exc}"
                renderable = Group(Text(f"Render error: {exc}", style="red"))

            if worker_mode and clear_screen and committed_before_clear is not None:
                (
                    self._committed_line_count,
                    self._committed_projection,
                    self._visible_committed_rows,
                ) = committed_before_clear

            ansi = self._capture_renderable(renderable, width)
            if not self._tty:
                return

            frame_rows = _rendered_row_count(ansi)
            if render_failed:
                bottom_rows = 0
                busy_activity_rows = 0
                thinking_stream_rows = 0
            else:
                if render_plan is None:
                    bottom_renderable = self._render_bottom_impl()
                else:
                    bottom_renderable = Group(*render_plan.bottom_elements)
                bottom_ansi = self._capture_renderable(bottom_renderable, width)
                bottom_rows = _rendered_row_count(bottom_ansi)
                busy_activity_rows = (
                    len(render_plan.busy_activity_elements)
                    if render_plan is not None
                    else self._busy_activity_row_count(width)
                )
                thinking_stream_rows = (
                    len(render_plan.thinking_stream_elements)
                    if render_plan is not None
                    else len(self._active_thinking_stream_elements(width))
                )
            lines = ansi.splitlines()

            if worker_mode:
                physical: PhysicalViewportPlan | None = None
                target_lines = lines
                fixed_bottom_rows = 0
                bottom_dock_anchored = self._frame_bottom_is_anchored(term_height, render_plan)
                scroll_bottom: int | None = None
                if (
                    not render_failed
                    and render_plan is not None
                    and render_plan.logical_plan is not None
                ):
                    provisional = self._physical_viewport_for_frame(
                        render_plan.logical_plan,
                        width=width,
                        term_height=term_height,
                        frame_start_row=1,
                        anchor_bottom=bottom_dock_anchored,
                    )
                    scroll_frame_rows = provisional.frame_rows
                    if bottom_dock_anchored:
                        fixed_bottom_rows = self._last_bottom_rows
                        scroll_bottom = term_height - fixed_bottom_rows
                else:
                    scroll_frame_rows = frame_rows
                    if bottom_dock_anchored:
                        fixed_bottom_rows = self._last_bottom_rows
                        scroll_bottom = term_height - fixed_bottom_rows
                visible_before = 0 if clear_screen else self._visible_committed_rows
                visible_after, scroll_ansi = self._frame_scroll_plan(
                    scroll_frame_rows,
                    term_height,
                    visible_rows=visible_before,
                    fixed_bottom_rows=fixed_bottom_rows,
                    scroll_bottom=scroll_bottom,
                )
                force_full = force_full or bool(scroll_ansi and not bottom_dock_anchored)
                start_row = max(visible_after + 1, 1)
                if (
                    not render_failed
                    and render_plan is not None
                    and render_plan.logical_plan is not None
                ):
                    physical = self._physical_viewport_for_frame(
                        render_plan.logical_plan,
                        width=width,
                        term_height=term_height,
                        frame_start_row=start_row,
                        anchor_bottom=bool(bottom_dock_anchored or scroll_ansi),
                    )
                    target_lines = self._physical_target_lines(physical)
                    frame_rows = physical.frame_rows
                    bottom_rows = physical.bottom.rendered.visual_rows
                    busy_activity_rows = physical.projected_regions[1].visual_rows
                    thinking_stream_rows = physical.projected_regions[2].visual_rows
                    start_row = physical.frame_start_row
                    cursor_ansi = (
                        f"\x1b[{physical.cursor_row};{physical.cursor_col}H"
                    )
                    lines_up = max(
                        frame_rows - (physical.cursor_row - start_row) - 1,
                        0,
                    )
                elif render_failed:
                    cursor_ansi, lines_up = "", 0
                else:
                    cursor_ansi, lines_up = self._input_cursor_target(plan=render_plan)
                generation = self._terminal_frame_generation + 1
                render_ms = (time.perf_counter() - started_at) * 1000
                batch = FrameBatch(
                    generation=generation,
                    start_row=start_row,
                    target_lines=tuple(target_lines),
                    cursor_ansi=cursor_ansi,
                    render_ms=render_ms,
                    force_full=force_full,
                    scroll_ansi=scroll_ansi if bottom_dock_anchored else "",
                    scroll_rows=visible_before - visible_after,
                    scroll_bottom=scroll_bottom or 0,
                )

                if resize_frame:
                    self._submit_terminal_barrier(
                        kind="resize",
                        apply_state=self._apply_resize_state,
                    )
                if clear_screen:
                    self._submit_terminal_barrier(
                        kind="clear",
                        ansi="\x1b[2J\x1b[H",
                        apply_state=self._apply_clear_state,
                    )
                    clear_submitted = True
                if scroll_ansi and not bottom_dock_anchored:
                    self._submit_terminal_barrier(
                        kind="scroll",
                        ansi=scroll_ansi,
                        apply_state=lambda: self._apply_scroll_state(visible_after),
                    )

                if physical is not None:
                    snapshot = self._layout_snapshot_for_physical(
                        physical=physical,
                        generation=generation,
                        width=width,
                        term_height=term_height,
                        frame_start_row=start_row,
                        scroll_epoch=self._scroll_epoch,
                        restore_epoch=self._restore_epoch,
                    )
                else:
                    snapshot = self._layout_snapshot_for_frame(
                        generation=generation,
                        width=width,
                        term_height=term_height,
                        start_row=start_row,
                        lines=target_lines,
                        frame_rows=frame_rows,
                        bottom_rows=bottom_rows,
                        lines_up=lines_up,
                        cursor_ansi=cursor_ansi,
                    )
                self._layout_generation = generation
                if snapshot is not None:
                    self._pending_layout_snapshots[generation] = snapshot
                    self._pending_layout_force_full[generation] = force_full
                try:
                    self._terminal_writer.submit_frame(batch)
                except Exception as exc:
                    self._pending_layout_snapshots.pop(generation, None)
                    self._pending_layout_force_full.pop(generation, None)
                    self._invalidate_pending_layout_after_submit_failure(exc)
                    raise
                self._has_rendered_frame = True
                self._last_frame_start_row = start_row
                self._submitted_generation = generation
                self._terminal_frame_generation = generation
                self._pending_worker_frame_states()[generation] = {
                    "visible_rows": visible_after,
                    "frame_rows": frame_rows,
                    "start_row": start_row,
                    "bottom_rows": bottom_rows,
                    "busy_activity_rows": busy_activity_rows,
                    "busy_activity_start_row": next(
                        (
                            region.start_row
                            for region in (snapshot.regions if snapshot is not None else ())
                            if region.key == "vibe"
                        ),
                        start_row
                        + frame_rows
                        - bottom_rows
                        - thinking_stream_rows
                        - busy_activity_rows,
                    ),
                    "thinking_stream_rows": thinking_stream_rows,
                    "width": width,
                    "term_height": term_height,
                    "lines_up": lines_up,
                    "target_lines": tuple(target_lines),
                    "render_plan": render_plan,
                }
            else:
                physical: PhysicalViewportPlan | None = None
                target_lines = lines
                logical = (
                    render_plan.logical_plan
                    if not render_failed and render_plan is not None
                    else None
                )
                fixed_bottom_rows = 0
                bottom_dock_anchored = self._frame_bottom_is_anchored(term_height, render_plan)
                scroll_bottom: int | None = None
                if logical is not None:
                    provisional = self._physical_viewport_for_frame(
                        logical,
                        width=width,
                        term_height=term_height,
                        frame_start_row=1,
                        anchor_bottom=bottom_dock_anchored,
                    )
                    scroll_frame_rows = provisional.frame_rows
                    if bottom_dock_anchored:
                        fixed_bottom_rows = self._last_bottom_rows
                        scroll_bottom = term_height - fixed_bottom_rows
                else:
                    scroll_frame_rows = frame_rows
                    if bottom_dock_anchored:
                        fixed_bottom_rows = self._last_bottom_rows
                        scroll_bottom = term_height - fixed_bottom_rows
                visible_before = 0 if clear_screen else self._visible_committed_rows
                visible_after, scroll_ansi = self._frame_scroll_plan(
                    scroll_frame_rows,
                    term_height,
                    visible_rows=visible_before,
                    fixed_bottom_rows=fixed_bottom_rows,
                    scroll_bottom=scroll_bottom,
                )
                force_full = force_full or bool(scroll_ansi and not bottom_dock_anchored)
                start_row = max(visible_after + 1, 1)
                try:
                    payload: list[str] = []
                    if clear_screen:
                        payload.append("\x1b[2J\x1b[H")
                    if scroll_ansi:
                        payload.append(scroll_ansi)
                        if not bottom_dock_anchored:
                            self._invalidate_frame_cache()
                            self._invalidate_layout("scroll")

                    if logical is not None:
                        physical = self._physical_viewport_for_frame(
                            logical,
                            width=width,
                            term_height=term_height,
                            frame_start_row=start_row,
                            anchor_bottom=bool(bottom_dock_anchored or scroll_ansi),
                        )
                        target_lines = self._physical_target_lines(physical)
                        frame_rows = physical.frame_rows
                        bottom_rows = physical.bottom.rendered.visual_rows
                        busy_activity_rows = physical.projected_regions[1].visual_rows
                        thinking_stream_rows = physical.projected_regions[2].visual_rows
                        start_row = physical.frame_start_row
                        cursor_ansi = f"\x1b[{physical.cursor_row};{physical.cursor_col}H"
                        lines_up = max(
                            frame_rows - (physical.cursor_row - start_row) - 1,
                            0,
                        )
                    elif render_failed:
                        cursor_ansi, lines_up = "", 0
                    else:
                        cursor_ansi, lines_up = self._input_cursor_target(plan=render_plan)

                    generation = self._terminal_frame_generation + 1
                    snapshot = None
                    if physical is not None:
                        snapshot = self._layout_snapshot_for_physical(
                            physical=physical,
                            generation=generation,
                            width=width,
                            term_height=term_height,
                            frame_start_row=start_row,
                            scroll_epoch=self._scroll_epoch,
                            restore_epoch=self._restore_epoch,
                        )
                    else:
                        snapshot = self._layout_snapshot_for_frame(
                            generation=generation,
                            width=width,
                            term_height=term_height,
                            start_row=start_row,
                            lines=target_lines,
                            frame_rows=frame_rows,
                            bottom_rows=bottom_rows,
                            lines_up=lines_up,
                            cursor_ansi=cursor_ansi,
                        )
                    frame_ansi, changed_lines, strategy = self._sync_layout_payload(
                        start_row=start_row,
                        previous_lines=self._prev_frame_lines,
                        previous_start_row=self._prev_frame_start_row,
                        scroll_rows=visible_before - visible_after if scroll_ansi else 0,
                        scroll_bottom=scroll_bottom or 0,
                        new_lines=target_lines,
                        previous_snapshot=self._applied_layout_snapshot,
                        snapshot=snapshot,
                        force_full=force_full,
                    )
                    if scroll_ansi and bottom_dock_anchored and not force_full and self._prev_frame_lines is not None:
                        frame_ansi, changed_lines = scrolled_frame_payload(
                            previous=self._prev_frame_lines, previous_start=self._prev_frame_start_row,
                            current=target_lines, start=start_row,
                            scroll_rows=visible_before - visible_after, scroll_bottom=scroll_bottom or 0,
                        )
                        strategy = "diff-scroll"
                    if not scroll_ansi and not force_full and self._prev_frame_lines is not None and self._applied_layout_snapshot is None:
                        frame_ansi, changed_lines = scrolled_frame_payload(
                            previous=self._prev_frame_lines, previous_start=self._prev_frame_start_row,
                            current=target_lines, start=start_row, scroll_rows=0, scroll_bottom=0,
                        )
                        strategy = "diff"
                    payload.append(frame_ansi)
                    if physical is not None or not render_failed:
                        payload.append(cursor_ansi)
                    self._write_sync_payload("".join(payload))
                except BaseException as exc:
                    self._handle_sync_terminal_failure(exc)
                    raise

                self._visible_committed_rows = visible_after
                self._last_frame_rows = frame_rows
                self._last_frame_start_row = start_row
                self._last_bottom_rows = bottom_rows
                self._last_bottom_start_row = start_row + frame_rows - bottom_rows
                if busy_activity_rows > 0:
                    busy_activity_start_row = (
                        next(
                            (
                                region.start_row
                                for region in (snapshot.regions if snapshot is not None else ())
                                if region.key == "vibe"
                            ),
                            start_row
                            + frame_rows
                            - bottom_rows
                            - thinking_stream_rows
                            - busy_activity_rows,
                        )
                    )
                    self._record_busy_activity_layout(
                        start_row=busy_activity_start_row,
                        rows=busy_activity_rows,
                        width=width,
                        term_height=term_height,
                        bottom_rows=bottom_rows,
                        thinking_rows=thinking_stream_rows,
                    )
                else:
                    self._invalidate_busy_activity_layout()
                self._record_input_cursor_geometry(frame_rows, lines_up)
                self._has_rendered_frame = True
                self._prev_frame_lines = list(target_lines)
                self._prev_frame_start_row = start_row
                self._prev_frame_width = width
                self._prev_frame_term_height = term_height
                self._bottom_region_dirty = False
                self._terminal_frame_generation = generation
                self._layout_generation = generation
                if snapshot is not None:
                    self._record_applied_layout(snapshot)
                    self._full_layout_invalidated = False
                self._render_stats = RenderStats(
                    total_lines=len(target_lines),
                    changed_lines=changed_lines,
                    render_ms=(time.perf_counter() - started_at) * 1000,
                    strategy=strategy,
                )
                self._last_render_plan = render_plan

            if self._pending_tb:
                sys.stderr.write(self._pending_tb)
                sys.stderr.flush()
                self._pending_tb = ""
        finally:
            self._render_plan = None
            if worker_mode and clear_screen and not clear_submitted:
                if committed_before_clear is not None:
                    (
                        self._committed_line_count,
                        self._committed_projection,
                        self._visible_committed_rows,
                    ) = committed_before_clear
                dock.request_clear_screen()

    def _write_sync_payload(self, ansi: str) -> None:
        self._terminal_writer.write(
            _BEGIN_SYNCHRONIZED_OUTPUT + ansi + _END_SYNCHRONIZED_OUTPUT
        )
        self._terminal_writer.flush()

    def _render_diff(
        self,
        start_row: int,
        prev_lines: list[str],
        new_lines: list[str],
    ) -> tuple[int, str]:
        total = max(len(prev_lines), len(new_lines))
        changed = [
            index
            for index in range(total)
            if index >= len(prev_lines)
            or index >= len(new_lines)
            or prev_lines[index] != new_lines[index]
        ]

        wrote_tail_clear = False
        for index in changed:
            row = start_row + index
            self._terminal_writer.write(f"\x1b[{row};1H")
            if index >= len(new_lines):
                self._terminal_writer.write("\x1b[K")
                wrote_tail_clear = True
                continue
            self._terminal_writer.write(new_lines[index])
            self._terminal_writer.write("\x1b[K")
        return len(changed), "diff-tail-clear" if wrote_tail_clear else "diff"

    @staticmethod
    def _sync_layout_payload(
        *,
        start_row: int,
        previous_lines: list[str] | None,
        new_lines: list[str],
        previous_snapshot: LayoutSnapshot | None,
        snapshot: LayoutSnapshot | None,
        force_full: bool,
        previous_start_row: int | None = None,
        scroll_rows: int = 0,
        scroll_bottom: int = 0,
    ) -> tuple[str, int, str]:
        def full() -> tuple[str, int, str]:
            old_start = previous_start_row
            if old_start is None and previous_snapshot is not None:
                old_start = previous_snapshot.frame_start_row
            prefix = []
            if old_start is not None and previous_lines is not None:
                for index in range(len(previous_lines)):
                    row = old_start + index
                    if row <= scroll_bottom:
                        row -= scroll_rows
                    if 1 <= row < start_row:
                        prefix.append(f"\x1b[{row};1H\x1b[K")
            ansi = "".join(prefix) + f"\x1b[{start_row};1H\x1b[J" + "\n".join(new_lines)
            return ansi, len(new_lines), "full"

        if snapshot is None or force_full or previous_lines is None:
            return full()
        if previous_snapshot is None:
            return full()

        layout_diff = diff_layout(previous_snapshot, snapshot)
        if layout_diff.kind == "full":
            return full()
        if layout_diff.kind in {"unchanged", "cursor"}:
            return "", 0, layout_diff.kind

        payload: list[str] = []

        def write_row(row: int, line: str) -> None:
            payload.append(f"\x1b[{row};1H{line}\x1b[K")

        if layout_diff.kind == "regions":
            rows = {
                row
                for row in layout_diff.changed_rows
                if snapshot.frame_start_row <= row
                < snapshot.frame_start_row + snapshot.frame_rows
            }
            changed_rows = []
            for row in sorted(rows):
                new_index = row - snapshot.frame_start_row
                old_index = row - previous_snapshot.frame_start_row
                if (
                    0 <= old_index < len(previous_lines)
                    and previous_lines[old_index] == new_lines[new_index]
                ):
                    continue
                write_row(row, new_lines[new_index])
                changed_rows.append(row)
            return "".join(payload), len(changed_rows), "diff"

        first_row = max(
            snapshot.frame_start_row,
            layout_diff.first_absolute_row or snapshot.frame_start_row,
        )
        end_row = snapshot.frame_start_row + snapshot.frame_rows
        for row in range(first_row, end_row):
            write_row(row, new_lines[row - snapshot.frame_start_row])
        old_end_row = previous_snapshot.frame_start_row + previous_snapshot.frame_rows
        tail_rows = tuple(range(end_row, max(end_row, old_end_row)))
        for row in tail_rows:
            write_row(row, "")
        return (
            "".join(payload),
            max(0, end_row - first_row) + len(tail_rows),
            "diff-suffix" if not tail_rows else "diff-tail-clear",
        )

    def _invalidate_bottom_anchor(self) -> None:
        self._last_bottom_rows = 0
        self._last_bottom_start_row = 1

    def _invalidate_frame_cache(self) -> None:
        self._last_render_plan = None
        self._prev_frame_lines = None
        self._prev_frame_start_row = 1
        self._prev_frame_width = 0
        self._prev_frame_term_height = None
        self._invalidate_busy_activity_layout()

    def _record_applied_layout(self, snapshot: LayoutSnapshot) -> None:
        self._applied_layout_snapshot = snapshot
        # Identity survives commits; absolute geometry must not.
        self._render_state.applied_temporary_panel = snapshot.bottom.panel.visual_rows > 0

    def _frame_bottom_is_anchored(
        self, term_height: int | None, render_plan: _RenderPlan | None,
    ) -> bool:
        if not self._bottom_dock_is_anchored(term_height):
            return False
        logical = render_plan.logical_plan if render_plan is not None else None
        has_panel = logical is not None and any(
            key == "panel" and rows.visual_rows > 0
            for key, rows in logical.bottom_source.source_children
        )
        # Temporary panels borrow trailing space; touching the bottom is not a
        # persistent anchor. Commit output still uses the physical dock anchor.
        return not (
            has_panel
            or self._render_state.applied_temporary_panel
        )

    def _bottom_dock_is_anchored(self, term_height: int | None) -> bool:
        return bool(
            self._has_rendered_frame
            and self._last_bottom_rows > 0
            and term_height is not None
            and self._last_bottom_start_row + self._last_bottom_rows - 1 == term_height
        )

    def _record_busy_activity_layout(
        self,
        *,
        start_row: int,
        rows: int,
        width: int,
        term_height: int | None,
        bottom_rows: int,
        thinking_rows: int,
    ) -> None:
        self._last_busy_activity_start_row = start_row
        self._last_busy_activity_rows = rows
        self._last_busy_activity_width = width
        self._last_busy_activity_term_height = term_height
        self._last_busy_activity_bottom_rows = bottom_rows
        self._last_busy_activity_thinking_rows = thinking_rows

    def _invalidate_busy_activity_layout(self) -> None:
        self._last_busy_activity_rows = 0
        self._last_busy_activity_start_row = 0
        self._last_busy_activity_width = 0
        self._last_busy_activity_term_height = None
        self._last_busy_activity_bottom_rows = 0
        self._last_busy_activity_thinking_rows = 0

    def _busy_activity_layout_matches(
        self,
        *,
        plan: _RenderPlan,
        width: int,
        term_height: int | None,
        rows: int,
    ) -> bool:
        expected_height = max(term_height or self._console.height or 24, 1)
        return (
            self._last_busy_activity_start_row > 0
            and self._last_busy_activity_rows == rows
            and plan.width == width
            and plan.height == expected_height
            and self._last_busy_activity_width == width
            and self._last_busy_activity_term_height == term_height
            and self._last_busy_activity_bottom_rows == self._last_bottom_rows
            and self._last_busy_activity_thinking_rows == len(plan.thinking_stream_elements)
        )

    def _frame_geometry_changed(self) -> bool:
        if not self._tty or self._prev_frame_width == 0:
            return False
        return (
            self._prev_frame_width != self._frame_width()
            or self._prev_frame_term_height != shutil.get_terminal_size().lines
        )

    def _frame_scroll_plan(
        self,
        frame_rows: int,
        term_height: int,
        *,
        visible_rows: int | None = None,
        fixed_bottom_rows: int = 0,
        scroll_bottom: int | None = None,
    ) -> tuple[int, str]:
        visible = self._visible_committed_rows if visible_rows is None else visible_rows
        visible = max(0, min(visible, term_height))
        overlap = visible + frame_rows - term_height
        if overlap <= 0:
            return visible, ""
        scroll_rows = min(overlap, visible)
        if scroll_rows <= 0:
            return 0, ""
        effective_scroll_bottom = (
            scroll_bottom
            if scroll_bottom is not None
            else term_height - fixed_bottom_rows
        )
        if fixed_bottom_rows > 0 or scroll_bottom is not None:
            scroll_bottom = effective_scroll_bottom
            if scroll_bottom >= 2:
                return (
                    visible - scroll_rows,
                    f"\x1b[1;{scroll_bottom}r\x1b[{scroll_bottom};1H"
                    + ("\n" * scroll_rows)
                    + "\x1b[r",
                )
            return 0, ""
        return (
            visible - scroll_rows,
            f"\x1b[{term_height};1H" + "\n" * scroll_rows,
        )

    def _make_room_for_frame(
        self,
        frame_rows: int,
        term_height: int,
        *,
        fixed_bottom_rows: int = 0,
    ) -> bool:
        visible_after, scroll_ansi = self._frame_scroll_plan(
            frame_rows,
            term_height,
            fixed_bottom_rows=fixed_bottom_rows,
        )
        if scroll_ansi:
            if self._terminal_writer_worker_mode():
                self._submit_terminal_barrier(
                    kind="scroll",
                    ansi=scroll_ansi,
                    apply_state=lambda: self._apply_scroll_state(visible_after),
                )
            else:
                self._terminal_writer.write(scroll_ansi)
                self._apply_scroll_state(visible_after)
        return bool(scroll_ansi)

    def _apply_resize_state(self) -> None:
        self._invalidate_frame_cache()

    def _apply_clear_state(self) -> None:
        self._committed_line_count = 0
        self._committed_projection = None
        self._visible_committed_rows = 0
        self._invalidate_frame_cache()

    def _apply_scroll_state(self, visible_rows: int) -> None:
        self._visible_committed_rows = visible_rows
        self._invalidate_frame_cache()

    def _sync_snapshot_is_usable(
        self,
        snapshot: LayoutSnapshot | None,
        *,
        width: int,
        term_height: int,
    ) -> bool:
        previous_lines = self._prev_frame_lines
        return bool(
            isinstance(snapshot, LayoutSnapshot)
            and not self._full_layout_invalidated
            and snapshot.scroll_epoch == self._scroll_epoch
            and snapshot.terminal_width == width
            and snapshot.terminal_height == term_height
            and snapshot.frame_start_row == self._prev_frame_start_row
            and snapshot.frame_rows == len(previous_lines or ())
            and self._prev_frame_width == width
            and self._prev_frame_term_height == term_height
            and self._has_rendered_frame
        )

    @staticmethod
    def _snapshot_geometry_matches(
        previous: LayoutSnapshot,
        current: LayoutSnapshot,
    ) -> bool:
        if (
            previous.frame_start_row != current.frame_start_row
            or previous.frame_rows != current.frame_rows
            or len(previous.regions) != len(current.regions)
        ):
            return False
        return all(
            old.key == new.key
            and old.start_row == new.start_row
            and old.visual_rows == new.visual_rows
            for old, new in zip(previous.regions, current.regions)
        )

    @staticmethod
    def _snapshot_region(
        snapshot: LayoutSnapshot,
        key: str,
    ) -> RegionGeometry | None:
        for region in snapshot.regions:
            if region.key == key:
                return region
        if key.startswith("bottom."):
            attribute = key.removeprefix("bottom.")
            region = getattr(snapshot.bottom, attribute, None)
            if isinstance(region, RegionGeometry):
                return region
        return None

    @classmethod
    def _snapshot_with_region_content(
        cls,
        snapshot: LayoutSnapshot,
        *,
        key: str,
        rendered: RenderedRows,
        new_lines: list[str],
    ) -> LayoutSnapshot:
        if not key.startswith("bottom."):
            return replace(
                snapshot,
                regions=tuple(
                    replace(
                        region,
                        content_signature=rendered.signature,
                        patch_safe=rendered.patch_safe,
                    )
                    if region.key == key
                    else region
                    for region in snapshot.regions
                ),
            )

        attribute = key.removeprefix("bottom.")
        previous_child = getattr(snapshot.bottom, attribute, None)
        if not isinstance(previous_child, RegionGeometry):
            raise ValueError(f"unknown bottom region: {key}")
        bottom_start = snapshot.bottom.region.start_row - snapshot.frame_start_row
        bottom_end = bottom_start + snapshot.bottom.region.visual_rows
        if bottom_start < 0 or bottom_end > len(new_lines):
            raise ValueError("bottom region is outside frame lines")
        bottom_rendered = normalize_rendered_rows(
            "\n".join(new_lines[bottom_start:bottom_end]),
            width=snapshot.bottom.region.width,
        )
        bottom_child = replace(
            previous_child,
            content_signature=rendered.signature,
            patch_safe=rendered.patch_safe,
        )
        bottom = replace(
            snapshot.bottom,
            rendered=bottom_rendered,
            region=replace(
                snapshot.bottom.region,
                content_signature=bottom_rendered.signature,
                patch_safe=all(
                    getattr(snapshot.bottom, child).patch_safe
                    for child in (
                        "top_separator",
                        "input",
                        "middle_separator",
                        "panel",
                        "panel_status_separator",
                        "status",
                    )
                ),
            ),
            **{attribute: bottom_child},
        )
        return replace(
            snapshot,
            bottom=bottom,
            regions=tuple(
                bottom.region if region.key == "bottom" else region
                for region in snapshot.regions
            ),
        )

    def _apply_sync_snapshot_patch(
        self,
        *,
        previous: LayoutSnapshot,
        snapshot: LayoutSnapshot,
        new_lines: list[str],
        started_at: float,
        lines_up: int,
        render_plan: _RenderPlan | None = None,
        force_regions: tuple[str, ...] = (),
        patch_rows: set[int] | None = None,
        bottom_region_dirty: bool | None = None,
    ) -> bool:
        if not self._snapshot_geometry_matches(previous, snapshot):
            return False
        if len(new_lines) != snapshot.frame_rows:
            return False
        layout_diff = diff_layout(previous, snapshot)
        if layout_diff.kind in {"full", "suffix"}:
            return False

        previous_lines = self._prev_frame_lines
        if previous_lines is None or len(previous_lines) != previous.frame_rows:
            return False

        forced_rows: set[int] = set()
        for key in force_regions:
            region = self._snapshot_region(snapshot, key)
            if region is None:
                return False
            forced_rows.update(
                range(region.start_row, region.start_row + region.visual_rows)
            )
        if patch_rows is not None:
            forced_rows.intersection_update(patch_rows)

        changed_rows = set(layout_diff.changed_rows)
        if patch_rows is not None:
            changed_rows.intersection_update(patch_rows)
        if layout_diff.kind not in {"regions", "unchanged", "cursor"}:
            return False
        rows = changed_rows | forced_rows
        written_rows = 0
        payload: list[str] = []
        for row in sorted(rows):
            new_index = row - snapshot.frame_start_row
            old_index = row - previous.frame_start_row
            if not 0 <= new_index < len(new_lines):
                return False
            forced = row in forced_rows
            if (
                not forced
                and 0 <= old_index < len(previous_lines)
                and previous_lines[old_index] == new_lines[new_index]
            ):
                continue
            payload.append(f"\x1b[{row};1H{new_lines[new_index]}\x1b[K")
            written_rows += 1

        worker_mode = self._tty and self._terminal_writer_worker_mode()
        if worker_mode:
            generation = snapshot.generation
            cursor_ansi = f"\x1b[{snapshot.cursor_row};{snapshot.cursor_col}H"
            render_ms = (time.perf_counter() - started_at) * 1000
            batch = FrameBatch(
                generation=generation,
                start_row=snapshot.frame_start_row,
                target_lines=tuple(new_lines),
                cursor_ansi=cursor_ansi,
                render_ms=render_ms,
                force_full=False,
            )
            self._layout_generation = generation
            self._pending_layout_snapshots[generation] = snapshot
            self._pending_layout_force_full[generation] = False
            try:
                self._terminal_writer.submit_frame(batch)
            except Exception as exc:
                self._pending_layout_snapshots.pop(generation, None)
                self._pending_layout_force_full.pop(generation, None)
                self._invalidate_pending_layout_after_submit_failure(exc)
                raise
            self._has_rendered_frame = True
            self._last_frame_start_row = snapshot.frame_start_row
            self._submitted_generation = generation
            self._terminal_frame_generation = generation

            vibe = next((region for region in snapshot.regions if region.key == "vibe"), None)
            thinking = next((region for region in snapshot.regions if region.key == "thinking"), None)
            busy_activity_rows = vibe.visual_rows if vibe is not None else 0
            thinking_stream_rows = thinking.visual_rows if thinking is not None else 0
            bottom_rows = snapshot.bottom.region.visual_rows
            self._pending_worker_frame_states()[generation] = {
                "visible_rows": self._visible_committed_rows,
                "frame_rows": snapshot.frame_rows,
                "start_row": snapshot.frame_start_row,
                "bottom_rows": bottom_rows,
                "busy_activity_rows": busy_activity_rows,
                "busy_activity_start_row": (
                    vibe.start_row
                    if vibe is not None
                    else (
                        snapshot.frame_start_row
                        + snapshot.frame_rows
                        - bottom_rows
                        - thinking_stream_rows
                        - busy_activity_rows
                    )
                ),
                "thinking_stream_rows": thinking_stream_rows,
                "width": snapshot.terminal_width,
                "term_height": snapshot.terminal_height,
                "lines_up": lines_up,
                "target_lines": tuple(new_lines),
                "render_plan": render_plan,
            }
            if bottom_region_dirty is not None:
                self._bottom_region_dirty = bottom_region_dirty
            self._render_stats = RenderStats(
                total_lines=len(new_lines),
                changed_lines=written_rows,
                render_ms=render_ms,
                strategy="diff" if written_rows else "cursor",
            )
            return True

        payload.append(f"\x1b[{snapshot.cursor_row};{snapshot.cursor_col}H")
        try:
            self._write_sync_payload("".join(payload))
        except BaseException as exc:
            self._handle_sync_terminal_failure(exc)
            raise

        self._record_applied_layout(snapshot)
        self._prev_frame_lines = list(new_lines)
        self._prev_frame_start_row = snapshot.frame_start_row
        self._prev_frame_width = snapshot.terminal_width
        self._prev_frame_term_height = snapshot.terminal_height
        self._terminal_frame_generation = snapshot.generation
        self._layout_generation = snapshot.generation
        self._last_frame_rows = snapshot.frame_rows
        self._last_frame_start_row = snapshot.frame_start_row
        self._last_bottom_rows = snapshot.bottom.region.visual_rows
        self._last_bottom_start_row = snapshot.bottom.region.start_row
        self._record_input_cursor_geometry(snapshot.frame_rows, lines_up)
        self._has_rendered_frame = True
        if bottom_region_dirty is not None:
            self._bottom_region_dirty = bottom_region_dirty
        self._full_layout_invalidated = False
        if render_plan is not None:
            self._last_render_plan = render_plan

            vibe = next(
                (region for region in snapshot.regions if region.key == "vibe"),
                None,
            )
            thinking = next(
                (region for region in snapshot.regions if region.key == "thinking"),
                None,
            )
            if vibe is not None and vibe.visual_rows > 0:
                self._record_busy_activity_layout(
                    start_row=vibe.start_row,
                    rows=vibe.visual_rows,
                    width=snapshot.terminal_width,
                    term_height=snapshot.terminal_height,
                    bottom_rows=snapshot.bottom.region.visual_rows,
                    thinking_rows=thinking.visual_rows if thinking is not None else 0,
                )
            else:
                self._invalidate_busy_activity_layout()
        self._render_stats = RenderStats(
            total_lines=len(new_lines),
            changed_lines=written_rows,
            render_ms=(time.perf_counter() - started_at) * 1000,
            strategy="diff" if written_rows else "cursor",
        )
        return True

    def _render_sync_local_frame(
        self,
        *,
        bottom_region_dirty: bool | None = None,
    ) -> bool:
        width = self._frame_width()
        term_height = shutil.get_terminal_size().lines
        previous = self._applied_layout_snapshot
        if not self._sync_snapshot_is_usable(
            previous,
            width=width,
            term_height=term_height,
        ):
            self._render_frame()
            return True

        started_at = time.perf_counter()
        self._render_plan = None
        try:
            self._render_impl(height=term_height, capture_plan=True)
            render_plan = self._render_plan
            if render_plan is None or render_plan.logical_plan is None:
                raise ValueError("local repaint did not produce a logical render plan")
            physical = self._physical_viewport_for_frame(
                render_plan.logical_plan,
                width=width,
                term_height=term_height,
                frame_start_row=previous.frame_start_row,
            )
            new_lines = self._physical_target_lines(physical)
            generation = self._terminal_frame_generation + 1
            snapshot = self._layout_snapshot_for_physical(
                physical=physical,
                generation=generation,
                width=width,
                term_height=term_height,
                frame_start_row=previous.frame_start_row,
                scroll_epoch=self._scroll_epoch,
                restore_epoch=self._restore_epoch,
            )
            if not self._snapshot_geometry_matches(previous, snapshot):
                self._render_plan = None
                self._render_frame()
                return True
            lines_up = max(
                snapshot.frame_rows
                - (snapshot.cursor_row - snapshot.frame_start_row)
                - 1,
                0,
            )
            if not self._apply_sync_snapshot_patch(
                previous=previous,
                snapshot=snapshot,
                new_lines=new_lines,
                started_at=started_at,
                lines_up=lines_up,
                render_plan=render_plan,
                bottom_region_dirty=bottom_region_dirty,
            ):
                self._render_plan = None
                self._render_frame()
                return True
            return True
        except OSError as exc:
            self._handle_sync_terminal_failure(exc)
            return False
        except Exception:
            self._render_plan = None
            self._render_frame()
            return True
        finally:
            self._render_plan = None

    def _render_sync_local_region(
        self,
        *,
        key: str,
        rendered: RenderedRows,
        render_plan: _RenderPlan | None = None,
        force_regions: tuple[str, ...] = (),
        patch_rows: set[int] | None = None,
        bottom_region_dirty: bool | None = None,
        cursor_pos: tuple[int, int] | None = None,
    ) -> bool:
        width = self._frame_width()
        term_height = shutil.get_terminal_size().lines
        previous = self._applied_layout_snapshot
        if not self._sync_snapshot_is_usable(
            previous,
            width=width,
            term_height=term_height,
        ):
            return False
        previous_region = self._snapshot_region(previous, key)
        previous_lines = self._prev_frame_lines
        if (
            previous_region is None
            or previous_region.visual_rows != rendered.visual_rows
            or previous_lines is None
            or not rendered.patch_safe
        ):
            return False

        start = previous_region.start_row - previous.frame_start_row
        end = start + previous_region.visual_rows
        if start < 0 or end > len(previous_lines):
            return False
        new_lines = list(previous_lines)
        new_lines[start:end] = rendered.rows
        try:
            snapshot = self._snapshot_with_region_content(
                previous,
                key=key,
                rendered=rendered,
                new_lines=new_lines,
            )
        except (TypeError, ValueError):
            return False
        snapshot = replace(
            snapshot,
            generation=self._terminal_frame_generation + 1,
        )
        if cursor_pos is not None:
            snapshot = replace(
                snapshot,
                cursor_row=cursor_pos[0],
                cursor_col=cursor_pos[1],
                bottom=replace(
                    snapshot.bottom,
                    cursor_row=cursor_pos[0],
                    cursor_col=cursor_pos[1],
                ),
            )
        lines_up = max(
            snapshot.frame_rows
            - (snapshot.cursor_row - snapshot.frame_start_row)
            - 1,
            0,
        )
        if not self._apply_sync_snapshot_patch(
            previous=previous,
            snapshot=snapshot,
            new_lines=new_lines,
            started_at=time.perf_counter(),
            lines_up=lines_up,
            render_plan=render_plan,
            force_regions=force_regions,
            patch_rows=patch_rows,
            bottom_region_dirty=bottom_region_dirty,
        ):
            return False
        return True

    def _input_region_panel_content_changed(
        self,
        previous: LayoutSnapshot,
        width: int,
    ) -> bool:
        panel_lines = self._render_panel_lines(width)
        previous_panel = self._snapshot_region(previous, "bottom.panel")
        if not panel_lines:
            return previous_panel is not None and previous_panel.visual_rows > 0
        _, panel_ansi = self._panel_row_count_and_ansi(panel_lines, width)
        panel_elements = self._render_panel_elements(
            panel_lines,
            width,
            panel_ansi=panel_ansi,
        )
        rendered = self._capture_region_rows(
            panel_elements,
            width,
            signature_context=("bottom", "panel"),
        )
        if previous_panel is None:
            return True
        return (
            rendered.visual_rows != previous_panel.visual_rows
            or rendered.signature != previous_panel.content_signature
        )

    def _render_input_region(self) -> None:
        if self._full_frame_repaint_pending:
            self._render_frame()
            return
        if not self._tty or not self._has_rendered_frame or self._last_bottom_rows <= 0:
            self._render_frame()
            return
        if self._frame_geometry_changed():
            self._render_frame()
            return

        width = self._frame_width()
        term_height = shutil.get_terminal_size().lines
        previous = self._applied_layout_snapshot
        if self._sync_snapshot_is_usable(
            previous,
            width=width,
            term_height=term_height,
        ):
            input_region = self._snapshot_region(previous, "bottom.input")
            if input_region is not None:
                try:
                    if self._input_region_panel_content_changed(previous, width):
                        self._render_sync_local_frame(bottom_region_dirty=True)
                        self._last_render_plan = None
                        return
                    input_elements = self._render_input_elements(width)
                    rendered = self._capture_region_rows(
                        input_elements,
                        width,
                        signature_context=("bottom", "input"),
                    )
                    if rendered.visual_rows == input_region.visual_rows:
                        patch_rows = set(
                            range(
                                input_region.start_row,
                                input_region.start_row + input_region.visual_rows,
                            )
                        )
                        input_rows = self._input_display_rows(width)
                        cursor_row_idx = min(self._cursor_row, max(len(input_rows) - 1, 0))
                        current_line = self._current_line()
                        display_line = self._input_display_text(current_line)
                        cursor = min(self._cursor_col, len(current_line))
                        render_width = self._render_line_width(width)
                        if self._active_text_secret:
                            before_cursor = "*" * cell_len(current_line[:cursor])
                        else:
                            before_cursor = display_line[:cursor]
                        cursor_cells = self._input_line_prefix_width(cursor_row_idx) + cell_len(before_cursor)
                        cursor_visual_row = min(cursor_cells // render_width, input_rows[cursor_row_idx] - 1)
                        cursor_row = input_region.start_row + cursor_visual_row
                        cursor_col = cursor_cells % render_width + 1

                        if self._render_sync_local_region(
                            key="bottom.input",
                            rendered=rendered,
                            force_regions=("bottom.input",),
                            patch_rows=patch_rows,
                            bottom_region_dirty=True,
                            cursor_pos=(cursor_row, cursor_col),
                        ):
                            self._last_render_plan = None
                            return
                except Exception:
                    pass

        self._render_sync_local_frame(bottom_region_dirty=True)
        self._last_render_plan = None

    def _render_choice_selection_region(self) -> bool:
        if not self._tty or self._active_choice is None:
            return False
        if not self._has_rendered_frame or self._last_bottom_rows <= 0:
            return False
        if self._frame_geometry_changed():
            self._render_frame()
            return True

        width = self._frame_width()
        term_height = shutil.get_terminal_size().lines
        previous = self._applied_layout_snapshot
        if self._sync_snapshot_is_usable(
            previous,
            width=width,
            term_height=term_height,
        ):
            try:
                panel_lines = self._render_panel_lines(width)
                _, panel_ansi = self._panel_row_count_and_ansi(panel_lines, width)
                panel_elements = self._render_panel_elements(
                    panel_lines,
                    width,
                    panel_ansi=panel_ansi,
                )
                panel = self._capture_region_rows(
                    panel_elements,
                    width,
                    signature_context=("bottom", "panel"),
                )
                panel_region = self._snapshot_region(previous, "bottom.panel")
                if panel_region is not None:
                    patch_rows = set(
                        range(
                            panel_region.start_row,
                            panel_region.start_row + panel_region.visual_rows,
                        )
                    )
                    if self._render_sync_local_region(
                        key="bottom.panel",
                        rendered=panel,
                        force_regions=("bottom.panel",),
                        patch_rows=patch_rows,
                    ):
                        self._last_render_plan = None
                        return True
            except Exception:
                pass

        if self._terminal_writer_worker_mode():
            self._render_frame()
            return True

        try:
            ansi = self._capture_renderable(self._render_bottom_impl(), width)
        except Exception:
            return False

        bottom_rows = _rendered_row_count(ansi)
        if bottom_rows != self._last_bottom_rows:
            return False

        start_row = self._last_bottom_start_row
        if start_row <= 0:
            return False

        lines = ansi.splitlines()
        if len(lines) != bottom_rows:
            return False

        for offset, line in enumerate(lines):
            self._terminal_writer.write(f"\x1b[{start_row + offset};1H")
            self._terminal_writer.write(line)
            self._terminal_writer.write("\x1b[K")
        self._position_input_cursor(self._last_frame_rows)
        self._has_rendered_frame = True
        self._last_render_plan = None
        self._terminal_writer.flush()
        return True

    def _render_busy_activity_tick(self) -> bool:
        if self._full_frame_repaint_pending:
            return False
        if (
            not self._tty
            or not self._busy_activity_tick_active()
            or not self._has_rendered_frame
            or self._render_scheduled
        ):
            return False
        if self._frame_geometry_changed():
            return False

        plan = self._last_render_plan
        if plan is None:
            if self._terminal_writer_worker_mode():
                self._render_frame()
                return True
            return False
        width = self._frame_width()
        term_height = shutil.get_terminal_size().lines
        previous = self._applied_layout_snapshot
        if not self._sync_snapshot_is_usable(
            previous,
            width=width,
            term_height=term_height,
        ):
            if self._terminal_writer_worker_mode():
                self._render_frame()
                return True
            return False

        try:
            elements = self._render_busy_activity_elements(width)
            rendered = self._capture_region_rows(
                elements,
                width,
                signature_context=("vibe",),
            )
        except Exception:
            if self._terminal_writer_worker_mode():
                self._render_frame()
                return True
            return False
        if rendered.visual_rows <= 0:
            if self._terminal_writer_worker_mode():
                self._render_frame()
                return True
            return False
        vibe = self._snapshot_region(previous, "vibe")
        if vibe is None or vibe.visual_rows != rendered.visual_rows:
            if self._terminal_writer_worker_mode():
                self._render_frame()
                return True
            return False
        patch_rows = set(range(vibe.start_row, vibe.start_row + vibe.visual_rows))
        next_plan = replace(plan, busy_activity_elements=tuple(elements))
        if self._render_sync_local_region(
            key="vibe",
            rendered=rendered,
            render_plan=next_plan,
            force_regions=("vibe",),
            patch_rows=patch_rows,
        ):
            return True
        if self._terminal_writer_worker_mode():
            self._render_frame()
            return True
        return False

    def _capture_renderable(self, renderable: object, width: int) -> str:
        capture_width = max(width, 1)
        key = (capture_width, self._console.height)
        if self._capture_console is None or self._capture_console_key != key:
            self._capture_buffer = io.StringIO()
            self._capture_console = Console(
                file=self._capture_buffer,
                force_terminal=True,
                color_system="truecolor",
                width=capture_width,
                height=self._console.height,
            )
            self._capture_console_key = key
        else:
            # Reuse the capture console but clear the backing buffer first.
            self._capture_buffer.seek(0)
            self._capture_buffer.truncate(0)
        self._capture_console.print(renderable)
        ansi = self._capture_buffer.getvalue()
        # Strip only the newline added by Console.print. Meaningful trailing
        # blank rows are part of the transcript and must still count.
        return ansi[:-1] if ansi.endswith("\n") else ansi


    def _move_to_frame_end_sequence(self) -> str:
        if not self._has_rendered_frame:
            return ""
        if self._cursor_to_frame_end_lines <= 0:
            return "\r"
        return f"\r\x1b[{self._cursor_to_frame_end_lines}B\r"

    def _input_cursor_target(
        self,
        *,
        plan: _RenderPlan | None = None,
    ) -> tuple[str, int]:
        if plan is None:
            width = self._frame_width()
            status_lines = self._render_hint_lines()
            panel_rows = self._visible_panel_row_count(width)
            input_rows = self._input_display_rows(width)
        else:
            width = plan.width
            status_lines = plan.status_lines
            panel_rows = plan.panel_rows
            input_rows = plan.input_rows
        snapshot = self._applied_layout_snapshot
        if snapshot is not None:
            source_row, source_col = self._input_source_cursor(width, tuple(input_rows))
            for source_slice in snapshot.source_slices:
                if (
                    source_slice.key == "bottom.input"
                    and source_slice.source_start <= source_row < source_slice.source_end
                ):
                    row = (
                        snapshot.bottom.region.start_row
                        + source_slice.projected_start
                        + source_row - source_slice.source_start
                    )
                    lines_up = snapshot.frame_start_row + snapshot.frame_rows - row
                    return f"\x1b[{row};{source_col}H", lines_up
        cursor_row = min(self._cursor_row, max(len(input_rows) - 1, 0))
        current_line = self._current_line()
        display_line = self._input_display_text(current_line)
        cursor = min(self._cursor_col, len(current_line))
        render_width = self._render_line_width(width)
        if self._active_text_secret:
            before_cursor = "*" * cell_len(current_line[:cursor])
        else:
            before_cursor = display_line[:cursor]
        cursor_cells = self._input_line_prefix_width(cursor_row) + cell_len(before_cursor)
        cursor_visual_row = min(cursor_cells // render_width, input_rows[cursor_row] - 1)
        rows_after_cursor = (
            input_rows[cursor_row]
            - cursor_visual_row
            - 1
            + sum(input_rows[cursor_row + 1 :])
        )
        lines_up = (
            rows_after_cursor
            + 1
            + panel_rows
            + (1 if panel_rows else 0)
            + len(status_lines)
        )
        col = cursor_cells % render_width
        frame_end = self._last_frame_start_row + self._last_frame_rows
        if self._last_frame_rows <= 0:
            frame_end = self._console.height or 24
        row = max(1, min(frame_end - lines_up, self._console.height or 24))
        return f"\x1b[{row};{col + 1}H", lines_up

    def _input_cursor_sequence(
        self,
        *,
        plan: _RenderPlan | None = None,
    ) -> str:
        sequence, _ = self._input_cursor_target(plan=plan)
        return sequence

    def _record_input_cursor_geometry(self, frame_rows: int, lines_up: int) -> None:
        self._cursor_to_frame_top_lines = max(frame_rows - lines_up, 0)
        self._cursor_to_frame_end_lines = lines_up
        self._last_frame_rows = frame_rows

    def _position_input_cursor(
        self,
        frame_rows: int | None = None,
        *,
        plan: _RenderPlan | None = None,
    ) -> None:
        """Move terminal cursor to the current input cursor position."""
        sequence, lines_up = self._input_cursor_target(plan=plan)
        self._terminal_writer.write(sequence)
        self._terminal_writer.flush()
        if frame_rows is not None:
            self._record_input_cursor_geometry(frame_rows, lines_up)
    def _capture_region_rows(
        self,
        renderables: list[object] | tuple[object, ...],
        width: int,
        *,
        signature_context: tuple[object, ...] = (),
    ) -> RenderedRows:
        if not renderables:
            return normalize_rendered_rows(
                "",
                width=width,
                signature_context=signature_context,
            )
        ansi = self._capture_renderable(Group(*renderables), width)
        return normalize_rendered_rows(
            ansi,
            width=width,
            signature_context=signature_context,
        )

    def _input_source_cursor(
        self,
        width: int,
        input_rows: tuple[int, ...],
    ) -> tuple[int, int]:
        if not input_rows:
            return 0, 1
        cursor_row = min(self._cursor_row, len(input_rows) - 1)
        current_line = self._current_line()
        display_line = self._input_display_text(current_line)
        cursor = min(self._cursor_col, len(current_line))
        render_width = self._render_line_width(width)
        if self._active_text_secret:
            before_cursor = "*" * cell_len(current_line[:cursor])
        else:
            before_cursor = display_line[:cursor]
        cursor_cells = self._input_line_prefix_width(cursor_row) + cell_len(before_cursor)
        cursor_visual_row = min(
            cursor_cells // render_width,
            input_rows[cursor_row] - 1,
        )
        prompt_rows = 1 if self._active_text_prompt is not None else 0
        source_row = prompt_rows + sum(input_rows[:cursor_row]) + cursor_visual_row
        return source_row, (cursor_cells % render_width) + 1

    def _build_logical_render_plan(
        self,
        *,
        width: int,
        height: int,
        transcript_elements: list[object],
        todo_elements: list[object],
        busy_activity_elements: list[Text],
        thinking_stream_elements: list[Text],
        status_lines: list[object],
        panel_elements: list[Text],
        input_elements: list[Text],
        input_rows: tuple[int, ...],
    ) -> LogicalRenderPlan:
        top_regions = (
            self._capture_region_rows(
                transcript_elements,
                width,
                signature_context=("transcript",),
            ),
            self._capture_region_rows(
                busy_activity_elements,
                width,
                signature_context=("vibe",),
            ),
            self._capture_region_rows(
                thinking_stream_elements,
                width,
                signature_context=("thinking",),
            ),
            self._capture_region_rows(
                todo_elements,
                width,
                signature_context=("todo",),
            ),
        )
        separator = Text("─" * width, style="dim")
        bottom_children: list[tuple[str, RenderedRows]] = [
            (
                "top_separator",
                self._capture_region_rows([separator], width, signature_context=("bottom", "top_separator")),
            ),
            (
                "input",
                self._capture_region_rows(
                    input_elements,
                    width,
                    signature_context=("bottom", "input"),
                ),
            ),
            (
                "middle_separator",
                self._capture_region_rows([separator], width, signature_context=("bottom", "middle_separator")),
            ),
        ]
        if panel_elements:
            bottom_children.append(
                (
                    "panel",
                    self._capture_region_rows(
                        panel_elements,
                        width,
                        signature_context=("bottom", "panel"),
                    ),
                )
            )
            bottom_children.append(
                (
                    "panel_status_separator",
                    self._capture_region_rows(
                        [separator],
                        width,
                        signature_context=("bottom", "panel_status_separator"),
                    ),
                )
            )
        if status_lines:
            bottom_children.append(
                (
                    "status",
                    self._capture_region_rows(
                        status_lines,
                        width,
                        signature_context=("bottom", "status"),
                    )
                )
            )

        source_cursor_row, cursor_col = self._input_source_cursor(width, input_rows)
        bottom_row_count = sum(rendered.visual_rows for _, rendered in bottom_children)
        bottom_source = project_bottom_viewport(
            bottom_children,
            terminal_height=max(height, bottom_row_count, 1),
            source_cursor_key="input",
            source_cursor_row=source_cursor_row,
            cursor_col=cursor_col,
            start_row=1,
            width=width,
        )
        source_keys = ("transcript", "vibe", "thinking", "todo")
        source_signature = tuple(
            (key, rendered.signature)
            for key, rendered in zip(source_keys, top_regions)
        ) + (
            ("bottom", bottom_source.source_signature),
            ("cursor", source_cursor_row, cursor_col),
        )
        return LogicalRenderPlan(
            source_regions=top_regions,
            bottom_source=bottom_source,
            source_cursor=("input", source_cursor_row),
            source_signature=source_signature,
        )

    @staticmethod
    def _physical_target_lines(physical: PhysicalViewportPlan) -> list[str]:
        lines: list[str] = []
        for region in physical.projected_regions:
            lines.extend(region.rows)
        lines.extend(physical.bottom.rendered.rows)
        if len(lines) != physical.frame_rows:
            raise ValueError("physical viewport rows do not match frame geometry")
        return lines

    @staticmethod
    def _layout_snapshot_for_physical(
        *,
        physical: PhysicalViewportPlan,
        generation: int,
        width: int,
        term_height: int,
        frame_start_row: int | None = None,
        scroll_epoch: int,
        restore_epoch: int,
    ) -> LayoutSnapshot:
        effective_start = physical.frame_start_row if frame_start_row is None else frame_start_row
        region_keys = ("transcript", "vibe", "thinking", "todo")
        regions: list[RegionGeometry] = []
        next_row = effective_start
        for key, rendered in zip(region_keys, physical.projected_regions):
            regions.append(
                RegionGeometry(
                    key=key,
                    start_row=next_row,
                    visual_rows=rendered.visual_rows,
                    width=width,
                    content_signature=rendered.signature,
                    patch_safe=rendered.patch_safe,
                )
            )
            next_row += rendered.visual_rows
        regions.append(physical.bottom.region)
        return LayoutSnapshot(
            terminal_width=width,
            terminal_height=term_height,
            frame_start_row=effective_start,
            frame_rows=physical.frame_rows,
            regions=tuple(regions),
            source_slices=physical.source_slices,
            bottom=physical.bottom,
            cursor_row=physical.cursor_row,
            cursor_col=physical.cursor_col,
            scroll_epoch=scroll_epoch,
            generation=generation,
            restore_epoch=restore_epoch,
        )

    def _physical_viewport_for_frame(
        self,
        logical: LogicalRenderPlan,
        *,
        width: int,
        term_height: int,
        frame_start_row: int,
        anchor_bottom: bool = False,
    ) -> PhysicalViewportPlan:
        return project_physical_viewport(
            logical,
            terminal_width=width,
            terminal_height=term_height,
            frame_start_row=frame_start_row,
            anchor_bottom=anchor_bottom,
        )


    def _restored_active_line_indexes(
        self,
        lines: list[str],
        line_map: dict[int, str],
        committed: int,
    ) -> list[int]:
        bounded_committed = min(max(committed, 0), len(lines))
        committed_indexes = set(
            self._scrollback_line_indexes(
                lines,
                line_map,
                0,
                bounded_committed,
            )
        )
        return [
            index
            for index in range(len(lines))
            if index >= bounded_committed or index not in committed_indexes
        ]

    def _render_impl(
        self,
        *,
        height: int | None = None,
        capture_plan: bool = False,
    ) -> Group:
        width = self._frame_width()
        render_height = max(height or self._console.height or 24, 1)

        # Cross-mixin render hooks: status, panel, busy activity, thinking, and input.
        status_lines = self._render_hint_lines()
        panel_lines = self._render_panel_lines(width)
        busy_activity_elements = self._render_busy_activity_elements(width)
        thinking_stream_elements = self._active_thinking_stream_elements(width)
        input_rows = self._input_display_rows(width)
        input_elements = self._render_input_elements(width)

        base_bottom_rows = self._base_bottom_row_count(
            width,
            status_lines,
            input_elements=input_elements,
        )
        if panel_lines:
            panel_row_limit = max(
                render_height
                - base_bottom_rows
                - len(busy_activity_elements)
                - len(thinking_stream_elements)
                - 1,
                0,
            )
            self._panel_row_limit = panel_row_limit
            panel_rows, panel_ansi = self._panel_row_count_and_ansi(panel_lines, width)
        else:
            self._panel_row_limit = None
            panel_rows, panel_ansi = 0, None
        bottom_fixed_lines = (
            base_bottom_rows
            + panel_rows
            + (1 if panel_rows else 0)
        )
        todo_budget = max(
            render_height
            - bottom_fixed_lines
            - len(busy_activity_elements)
            - len(thinking_stream_elements),
            0,
        )
        todo_max_rows = min(
            self._pinned_todo_max_rows(
                render_height,
                bottom_fixed_lines
                + len(busy_activity_elements)
                + len(thinking_stream_elements),
            ),
            todo_budget,
        )
        pinned_todo_elements = self._render_pinned_todo_elements(width, max_rows=todo_max_rows)
        fixed_lines = (
            bottom_fixed_lines
            + len(pinned_todo_elements)
            + len(busy_activity_elements)
            + len(thinking_stream_elements)
        )
        body_limit = max(render_height - fixed_lines, 0)

        # Transcript — only render uncommitted (active) lines.
        # Restored history stays in the viewport; only new, uncommitted root
        # blocks participate in the active frame after the restore boundary.
        restored_range = self._sync_restored_render_state()
        if restored_range is not None:
            restored_start, restored_end = restored_range
            history_start = restored_start if self._restored_startup_flushed else 0
            current_end = len(dock.tree.root.children)
            if self._restored_history_retired:
                history_lines, history_line_map = [], {}
            else:
                history_lines, history_line_map = dock.tree.render_root_slice_with_line_map(
                    width,
                    history_start,
                    restored_end,
                )
            added_lines, added_line_map = dock.tree.render_root_slice_with_line_map(
                width,
                restored_end,
                current_end,
            )
            committed_added = min(
                self._restored_committed_line_count,
                len(added_lines),
            )
            added_active_indexes = self._restored_active_line_indexes(
                added_lines,
                added_line_map,
                committed_added,
            )
            thinking_node_id = dock.active_thinking_stream_node_id()
            active_lines = [
                line
                for index, line in enumerate(history_lines)
                if history_line_map.get(index) != thinking_node_id
            ]
            active_lines.extend(
                added_lines[index]
                for index in added_active_indexes
                if added_line_map.get(index) != thinking_node_id
            )
        else:
            tree_lines, line_map = dock.tree.render_with_line_map(width)
            thinking_line_ids = dock.active_thinking_stream_line_ids(width)
            active_indexes = self._active_identity_line_indexes(tree_lines, line_map)
            active_lines = [
                tree_lines[index]
                for index in active_indexes
                if index not in thinking_line_ids
            ]

        if dock.has_active_thinking_stream():
            while active_lines and not active_lines[-1].strip():
                active_lines.pop()

        elements: list = self._transcript_elements_for_rows(
            active_lines,
            width,
            body_limit,
        )
        full_transcript_elements = [
            self._safe_text_from_line(line) for line in active_lines
        ]
        full_todo_elements = pinned_todo_elements

        elements.extend(busy_activity_elements)
        elements.extend(thinking_stream_elements)
        elements.extend(pinned_todo_elements)
        panel_elements = self._render_panel_elements(
            panel_lines,
            width,
            panel_ansi=panel_ansi,
        )
        bottom_elements = self._render_bottom_elements(
            width,
            panel_lines,
            status_lines,
            panel_ansi=panel_ansi,
            input_elements=input_elements,
            panel_elements=panel_elements,
        )
        elements.extend(bottom_elements)

        logical_plan = None
        if capture_plan:
            logical_plan = self._build_logical_render_plan(
                width=width,
                height=render_height,
                transcript_elements=full_transcript_elements,
                todo_elements=full_todo_elements,
                busy_activity_elements=busy_activity_elements,
                thinking_stream_elements=thinking_stream_elements,
                status_lines=status_lines,
                panel_elements=panel_elements,
                input_elements=input_elements,
                input_rows=tuple(input_rows),
            )
            self._render_plan = _RenderPlan(
                width=width,
                height=render_height,
                status_lines=tuple(status_lines),
                panel_lines=tuple(panel_lines),
                busy_activity_elements=tuple(busy_activity_elements),
                thinking_stream_elements=tuple(thinking_stream_elements),
                base_bottom_rows=base_bottom_rows,
                panel_rows=panel_rows,
                panel_ansi=panel_ansi,
                bottom_elements=tuple(bottom_elements),
                panel_elements=tuple(panel_elements),
                input_rows=tuple(input_rows),
                logical_plan=logical_plan,
            )

        return Group(*elements)

    def _active_thinking_stream_elements(self, width: int) -> list[Text]:
        lines = dock.active_thinking_stream_lines(width)
        if not lines and hasattr(dock, "active_integration_startup_lines"):
            lines = dock.active_integration_startup_lines(width)
        if not lines:
            return []
        return self._transcript_elements_for_rows(lines, width, len(lines))

    def _safe_text_from_line(self, line: str) -> Text:
        try:
            return text_from_line(line)
        except Exception:
            return Text(line)

    def _visual_row_count(self, rendered: Text, width: int) -> int:
        return max(len(rendered.wrap(self._console, max(width, 1), overflow="fold")), 1)

    def _bounded_text_candidates_for_rows(
        self,
        lines: list[str],
        width: int,
        row_limit: int,
        *,
        overscan_rows: int | None = None,
    ) -> tuple[list[Text], str, bool]:
        if not lines or row_limit <= 0:
            return [], "", False

        overscan = (
            min(max(row_limit, 8), 32)
            if overscan_rows is None
            else max(overscan_rows, 0)
        )
        target_rows = row_limit + overscan
        renderables: list[Text] = []
        visual_rows = 0
        for line in reversed(lines):
            rendered = self._safe_text_from_line(line)
            renderables.insert(0, rendered)
            visual_rows += self._visual_row_count(rendered, width)
            if visual_rows >= target_rows:
                break

        ansi = self._capture_renderable(Group(*renderables), width)
        return renderables, ansi, len(renderables) < len(lines)

    def _text_elements_from_bounded_ansi(
        self,
        ansi: str,
        row_limit: int,
        *,
        truncated: bool,
        prepend_ellipsis: bool,
    ) -> list[Text]:
        if not ansi or row_limit <= 0:
            return []
        rows = ansi.splitlines()
        truncated = truncated or len(rows) > row_limit
        if prepend_ellipsis and truncated:
            if row_limit == 1:
                return [Text("…", style="dim")]
            rows = rows[-(row_limit - 1):]
            return [Text("…", style="dim")] + [Text.from_ansi(row) for row in rows]
        return [Text.from_ansi(row) for row in rows[-row_limit:]]

    def _bounded_text_elements_for_rows(
        self,
        lines: list[str],
        width: int,
        row_limit: int,
        *,
        overscan_rows: int | None = None,
        prepend_ellipsis: bool = False,
    ) -> list[Text]:
        renderables, ansi, truncated = self._bounded_text_candidates_for_rows(
            lines,
            width,
            row_limit,
            overscan_rows=overscan_rows,
        )
        del renderables
        return self._text_elements_from_bounded_ansi(
            ansi,
            row_limit,
            truncated=truncated,
            prepend_ellipsis=prepend_ellipsis,
        )

    def _transcript_elements_for_rows(
        self,
        lines: list[str],
        width: int,
        row_limit: int,
    ) -> list[Text]:
        return self._bounded_text_elements_for_rows(lines, width, row_limit)

    def _render_bottom_impl(self) -> Group:
        width = self._frame_width()
        return Group(
            *self._render_bottom_elements(
                width,
                self._render_panel_lines(width),
                self._render_hint_lines(),
            )
        )

    def _render_input_elements(self, width: int) -> list[Text]:
        elements: list[Text] = []
        prompt = "❯ "

        if self._active_text_prompt is not None:
            elements.append(Text(f"{self._active_text_prompt} ", style="bold"))
            prompt = ""

        prompt_width = 2
        for row, line in enumerate(self._input_lines):
            prefix = prompt if row == 0 else " " * prompt_width
            elements.extend(self._render_input_line(row, line, prefix, width))

        return elements

    def _render_bottom_elements(
        self,
        width: int,
        panel_lines: list[str],
        status_lines: list,
        *,
        panel_ansi: str | None = None,
        input_elements: list[Text] | None = None,
        panel_elements: list[Text] | None = None,
    ) -> list:
        elements: list = [Text("─" * width, style="dim")]
        if input_elements is None:
            input_elements = self._render_input_elements(width)
        elements.extend(input_elements)
        elements.append(Text("─" * width, style="dim"))

        # Panels (attachment, command palette, choice)
        if panel_elements is None:
            panel_elements = self._render_panel_elements(
                panel_lines,
                width,
                panel_ansi=panel_ansi,
            )
        elements.extend(panel_elements)

        if panel_elements:
            elements.append(Text("─" * width, style="dim"))

        # Status bar (always at the very bottom)
        for line in status_lines:
            elements.append(line)

        return elements

    def _render_hint_lines(self) -> list:
        lines: list = []
        status = self._status_summary_text(self._frame_width())
        if status.plain:
            lines.append(status)
        if self._notice:
            lines.append(Text("  " + self._notice, style="#8F9BA8"))
        if self._last_error:
            lines.append(Text("  ⚠ " + self._last_error, style="red"))
        return lines

    def _base_bottom_elements(
        self,
        width: int,
        status_lines: list,
        *,
        input_elements: list[Text] | None = None,
    ) -> list:
        if input_elements is None:
            input_elements = self._render_input_elements(width)
        return [
            Text("─" * width, style="dim"),
            *input_elements,
            Text("─" * width, style="dim"),
            *status_lines,
        ]

    def _base_bottom_row_count(
        self,
        width: int,
        status_lines: list,
        *,
        input_elements: list[Text] | None = None,
    ) -> int:
        elements = self._base_bottom_elements(
            width,
            status_lines,
            input_elements=input_elements,
        )
        key = (
            width,
            self._console.height,
            tuple(self._renderable_cache_key(element) for element in elements),
        )
        if key == self._base_bottom_rows_cache_key:
            return self._base_bottom_rows_cache_count
        count = _rendered_row_count(self._capture_renderable(Group(*elements), width))
        self._base_bottom_rows_cache_key = key
        self._base_bottom_rows_cache_count = count
        return count

    @staticmethod
    def _renderable_cache_key(renderable: object) -> tuple:
        if isinstance(renderable, Text):
            return (
                "text",
                renderable.plain,
                str(renderable.style),
                tuple(
                    (span.start, span.end, str(span.style))
                    for span in renderable.spans
                ),
            )
        return ("repr", repr(renderable))

    def _render_panel_elements(
        self,
        panel_lines: list[str],
        width: int,
        *,
        panel_ansi: str | None = None,
    ) -> list[Text]:
        if not panel_lines:
            return []
        row_limit = self._panel_row_limit
        if row_limit is None:
            return [self._safe_text_from_line(line) for line in panel_lines]
        if row_limit <= 0:
            return []
        if panel_ansi is None:
            return self._bounded_text_elements_for_rows(
                panel_lines,
                width,
                row_limit,
                prepend_ellipsis=True,
            )
        return [Text.from_ansi(row) for row in panel_ansi.splitlines()]

    def _panel_row_count_and_ansi(
        self,
        panel_lines: list[str],
        width: int,
    ) -> tuple[int, str | None]:
        if not panel_lines:
            return 0, None
        row_limit = self._panel_row_limit
        if row_limit is None:
            elements = [self._safe_text_from_line(line) for line in panel_lines]
            if not elements:
                return 0, ""
            ansi = self._capture_renderable(Group(*elements), width)
            return _rendered_row_count(ansi), ansi
        if row_limit <= 0:
            return 0, ""

        _, ansi, truncated = self._bounded_text_candidates_for_rows(
            panel_lines,
            width,
            row_limit,
        )
        if not ansi:
            return 0, ""

        rows = ansi.splitlines()
        truncated = truncated or len(rows) > row_limit
        if truncated:
            if row_limit == 1:
                ansi = "\x1b[2m…\x1b[0m"
            else:
                visible_rows = rows[-(row_limit - 1):]
                ansi = "\x1b[2m…\x1b[0m\n" + "\n".join(visible_rows)
        return _rendered_row_count(ansi), ansi


    def _panel_row_count(self, panel_lines: list[str], width: int) -> int:
        return self._panel_row_count_and_ansi(panel_lines, width)[0]

    def _visible_panel_row_count(
        self,
        width: int,
        *,
        panel_lines: list[str] | None = None,
    ) -> int:
        lines = self._render_panel_lines(width) if panel_lines is None else panel_lines
        rows = self._panel_row_count(lines, width)
        row_limit = self._panel_row_limit
        if row_limit is None:
            return rows
        return min(rows, max(row_limit, 0))
