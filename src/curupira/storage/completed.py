"""SQLite persistence for completed dispatch snapshots and next actions."""

from curupira.models import CompletedTaskState, Task
from curupira.storage.key_value import _SQLiteJsonRepository


class CompletedTaskRepository(_SQLiteJsonRepository[CompletedTaskState]):
    """Remember work already completed for the current source snapshot."""

    _ENTITY_NAME = "completed_tasks"
    _MODEL = CompletedTaskState

    async def get(self, task: Task) -> CompletedTaskState | None:
        """Return the prior result for one logical source item."""
        return await self._get_value(task.state_key)

    async def save(self, task: Task, next_action: str) -> None:
        """Record a successful run and its explicit follow-up action."""
        await self._save_value(
            task.state_key,
            CompletedTaskState(fingerprint=task.state_fingerprint, next_action=next_action),
        )

    async def delete(self, task: Task) -> None:
        """Forget completion when its task leaves GitHub's open workflow."""
        await self._delete_value(task.state_key)
