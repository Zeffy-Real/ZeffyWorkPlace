"""workflow 包：P1-2 工作流引擎（templates / state_machine / engine）。"""

from app.workflow.engine import WorkflowEngine, engine
from app.workflow.state_machine import WorkflowStateError
from app.workflow.templates import TEMPLATES, get_template

__all__ = [
    "WorkflowEngine",
    "engine",
    "WorkflowStateError",
    "get_template",
    "TEMPLATES",
]
