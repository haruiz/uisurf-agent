from __future__ import annotations

"""A2A request handler that supports callback-based confirmation pauses."""

import asyncio
import logging
from collections.abc import AsyncGenerator, Awaitable, Callable
from typing import Protocol, cast

from a2a.server.context import ServerCallContext
from a2a.server.events import Event, EventConsumer
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import ResultAggregator, TaskManager, TaskUpdater
from a2a.types import (
    InternalError,
    Message,
    MessageSendParams,
    Task,
    TaskState,
    TaskStatusUpdateEvent,
)
from a2a.utils import get_message_text, new_agent_text_message
from a2a.utils.errors import ServerError
from a2a.utils.task import apply_history_length


logger = logging.getLogger(__name__)

INTERRUPTIBLE_TASK_STATES = {
    TaskState.auth_required,
    TaskState.input_required,
}


class ConfirmationCapableExecutor(Protocol):
    """Executor interface needed for callback-driven confirmation resumes."""

    def has_pending_prompt(self, task_id: str) -> bool:
        ...

    def resolve_pending_prompt(self, task_id: str, user_input: str) -> bool:
        ...

    def build_pending_prompt_message(self, task_id: str) -> str:
        ...


class ConfirmationRequestHandler(DefaultRequestHandler):
    """Request handler that pauses on input-required and resumes pending prompts."""

    agent_executor: ConfirmationCapableExecutor

    async def _continue_consuming(
        self,
        result_aggregator: ResultAggregator,
        consumer: EventConsumer,
        event_callback: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        """Continue processing a queue after the current request has returned."""
        async for event in consumer.consume_all():
            if isinstance(event, Message):
                result_aggregator._message = event  # type: ignore[attr-defined]
                return

            await result_aggregator.task_manager.process(event)
            if event_callback:
                await event_callback()

    @staticmethod
    def _event_state(event: Event) -> TaskState | None:
        """Extract the task state from a task-bearing event."""
        if isinstance(event, Task):
            return event.status.state
        if isinstance(event, TaskStatusUpdateEvent):
            return event.status.state
        return None

    async def _consume_and_break_on_interruptible_state(
        self,
        result_aggregator: ResultAggregator,
        consumer: EventConsumer,
        *,
        blocking: bool,
        event_callback: Callable[[], Awaitable[None]] | None = None,
        continue_in_background: bool,
    ) -> tuple[Task | Message | None, bool, TaskState | None, asyncio.Task | None]:
        """Consume until completion or until a resumable pause/non-blocking break."""
        interrupted = False
        interrupted_state: TaskState | None = None
        bg_task: asyncio.Task | None = None

        async for event in consumer.consume_all():
            if isinstance(event, Message):
                result_aggregator._message = event  # type: ignore[attr-defined]
                return event, False, None, None

            await result_aggregator.task_manager.process(event)
            state = self._event_state(event)

            if state in INTERRUPTIBLE_TASK_STATES or not blocking:
                interrupted_state = state
                should_continue_in_background = (
                    continue_in_background and state != TaskState.input_required
                )
                if should_continue_in_background:
                    bg_task = asyncio.create_task(
                        self._continue_consuming(
                            result_aggregator,
                            consumer,
                            event_callback,
                        )
                    )
                interrupted = True
                break

        return (
            await result_aggregator.current_result,
            interrupted,
            interrupted_state,
            bg_task,
        )

    async def _resume_pending_prompt_non_stream(
        self,
        params: MessageSendParams,
        task: Task,
        context: ServerCallContext | None = None,
    ) -> Message | Task:
        """Resolve a pending confirmation and return the next task breakpoint."""
        queue = await self._queue_manager.get(task.id)
        if not queue:
            raise ServerError(
                error=InternalError(
                    message=f"No active event queue exists for task {task.id}."
                )
            )

        task_manager = TaskManager(
            task_id=task.id,
            context_id=task.context_id,
            task_store=self.task_store,
            initial_message=params.message,
            context=context,
        )
        task = task_manager.update_with_message(params.message, task)
        result_aggregator = ResultAggregator(task_manager)
        updater = TaskUpdater(queue, task.id, task.context_id)
        user_input = get_message_text(params.message)

        if self.agent_executor.resolve_pending_prompt(task.id, user_input):
            await updater.update_status(
                TaskState.working,
                new_agent_text_message(
                    "Confirmation received. Resuming agent execution...",
                    task.context_id,
                    task.id,
                ),
            )
        else:
            await updater.update_status(
                TaskState.input_required,
                new_agent_text_message(
                    self.agent_executor.build_pending_prompt_message(task.id),
                    task.context_id,
                    task.id,
                ),
            )

        consumer = EventConsumer(queue)
        blocking = True
        if params.configuration and params.configuration.blocking is False:
            blocking = False

        result, _, _, _ = await self._consume_and_break_on_interruptible_state(
            result_aggregator,
            consumer,
            blocking=blocking,
            continue_in_background=False,
        )

        if not result:
            raise ServerError(error=InternalError())

        if isinstance(result, Task) and params.configuration:
            result = apply_history_length(result, params.configuration.history_length)

        await self._send_push_notification_if_needed(task.id, result_aggregator)
        return result

    async def _resume_pending_prompt_stream(
        self,
        params: MessageSendParams,
        task: Task,
        context: ServerCallContext | None = None,
    ) -> AsyncGenerator[Event]:
        """Resolve a pending confirmation and stream resumed task events."""
        queue = await self._queue_manager.get(task.id)
        if not queue:
            raise ServerError(
                error=InternalError(
                    message=f"No active event queue exists for task {task.id}."
                )
            )

        task_manager = TaskManager(
            task_id=task.id,
            context_id=task.context_id,
            task_store=self.task_store,
            initial_message=params.message,
            context=context,
        )
        task = task_manager.update_with_message(params.message, task)
        result_aggregator = ResultAggregator(task_manager)
        updater = TaskUpdater(queue, task.id, task.context_id)
        user_input = get_message_text(params.message)

        if self.agent_executor.resolve_pending_prompt(task.id, user_input):
            await updater.update_status(
                TaskState.working,
                new_agent_text_message(
                    "Confirmation received. Resuming agent execution...",
                    task.context_id,
                    task.id,
                ),
            )
        else:
            await updater.update_status(
                TaskState.input_required,
                new_agent_text_message(
                    self.agent_executor.build_pending_prompt_message(task.id),
                    task.context_id,
                    task.id,
                ),
            )

        consumer = EventConsumer(queue)
        async for event in result_aggregator.consume_and_emit(consumer):
            if isinstance(event, Task):
                self._validate_task_id_match(task.id, event.id)

            await self._send_push_notification_if_needed(
                task.id,
                result_aggregator,
            )
            yield event

            if self._event_state(event) in INTERRUPTIBLE_TASK_STATES:
                break

    async def on_message_send(
        self,
        params: MessageSendParams,
        context: ServerCallContext | None = None,
    ) -> Message | Task:
        """Pause on input-required and resume pending confirmations."""
        existing_task: Task | None = None
        if params.message.task_id:
            existing_task = await self.task_store.get(params.message.task_id, context)

        if (
            existing_task
            and existing_task.status.state == TaskState.input_required
            and self.agent_executor.has_pending_prompt(existing_task.id)
        ):
            return await self._resume_pending_prompt_non_stream(
                params,
                existing_task,
                context,
            )

        (
            _task_manager,
            task_id,
            _queue,
            result_aggregator,
            producer_task,
        ) = await self._setup_message_execution(params, context)
        consumer = EventConsumer(_queue)
        producer_task.add_done_callback(consumer.agent_task_callback)

        blocking = True
        if params.configuration and params.configuration.blocking is False:
            blocking = False

        interrupted_or_non_blocking = False
        try:
            async def push_notification_callback() -> None:
                await self._send_push_notification_if_needed(
                    task_id,
                    result_aggregator,
                )

            (
                result,
                interrupted_or_non_blocking,
                _interrupted_state,
                bg_consume_task,
            ) = await self._consume_and_break_on_interruptible_state(
                result_aggregator,
                consumer,
                blocking=blocking,
                event_callback=push_notification_callback,
                continue_in_background=True,
            )

            if bg_consume_task is not None:
                bg_consume_task.set_name(f"continue_consuming:{task_id}")
                self._track_background_task(bg_consume_task)

        except Exception:
            logger.exception("Agent execution failed")
            producer_task.cancel()
            raise
        finally:
            if interrupted_or_non_blocking:
                cleanup_task = asyncio.create_task(
                    self._cleanup_producer(producer_task, task_id)
                )
                cleanup_task.set_name(f"cleanup_producer:{task_id}")
                self._track_background_task(cleanup_task)
            else:
                await self._cleanup_producer(producer_task, task_id)

        if not result:
            raise ServerError(error=InternalError())

        if isinstance(result, Task):
            self._validate_task_id_match(task_id, result.id)
            if params.configuration:
                result = apply_history_length(result, params.configuration.history_length)

        await self._send_push_notification_if_needed(task_id, result_aggregator)
        return result

    async def on_message_send_stream(
        self,
        params: MessageSendParams,
        context: ServerCallContext | None = None,
    ) -> AsyncGenerator[Event]:
        """Stream events until completion or an input/auth-required pause."""
        existing_task: Task | None = None
        if params.message.task_id:
            existing_task = await self.task_store.get(params.message.task_id, context)

        if (
            existing_task
            and existing_task.status.state == TaskState.input_required
            and self.agent_executor.has_pending_prompt(existing_task.id)
        ):
            async for event in self._resume_pending_prompt_stream(
                params,
                existing_task,
                context,
            ):
                yield event
            return

        (
            _task_manager,
            task_id,
            queue,
            result_aggregator,
            producer_task,
        ) = await self._setup_message_execution(params, context)
        consumer = EventConsumer(queue)
        producer_task.add_done_callback(consumer.agent_task_callback)

        try:
            async for event in result_aggregator.consume_and_emit(consumer):
                if isinstance(event, Task):
                    self._validate_task_id_match(task_id, event.id)

                await self._send_push_notification_if_needed(task_id, result_aggregator)
                yield event

                event_state = self._event_state(event)
                if event_state in INTERRUPTIBLE_TASK_STATES:
                    if event_state == TaskState.auth_required:
                        bg_task = asyncio.create_task(
                            self._continue_consuming(result_aggregator, consumer)
                        )
                        bg_task.set_name(f"background_consume:{task_id}")
                        self._track_background_task(bg_task)
                    break
        except (asyncio.CancelledError, GeneratorExit):
            bg_task = asyncio.create_task(
                self._continue_consuming(result_aggregator, consumer)
            )
            bg_task.set_name(f"background_consume:{task_id}")
            self._track_background_task(bg_task)
            raise
        finally:
            cleanup_task = asyncio.create_task(
                self._cleanup_producer(producer_task, task_id)
            )
            cleanup_task.set_name(f"cleanup_producer:{task_id}")
            self._track_background_task(cleanup_task)
