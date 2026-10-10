"""Local persistence repositories."""

from curupira.storage.completed import CompletedTaskRepository
from curupira.storage.cron import CronScheduleRepository
from curupira.storage.sessions import RunningSessionRepository

__all__ = ["CompletedTaskRepository", "CronScheduleRepository", "RunningSessionRepository"]
