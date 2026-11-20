"""One project-specific menu shared by workspace and text-first entry points."""
from .discipline_modes import EXPERIMENT, FLOW, FORK, LABELS, REFRAME, VASYA

PENDING_MODE_KEY = "pending_project_generation_mode"


def project_mode_rows(project_key=None):
    project = str(project_key or "grandma_kitchen").strip()
    if project == "discipline":
        return [["История"], [LABELS[VASYA], LABELS[REFRAME]],
                [LABELS[FLOW]], [LABELS[FORK], LABELS[EXPERIMENT]],
                ["Визуализация"], ["Копия"], ["Реклама"]]
    rows = [["Обычный", "Совет"]] if project == "grandma_kitchen" else [["Обычный"]]
    return rows + [["Silence"], ["Длинный"], ["Копия"], ["История"], ["Визуализация"], ["Реклама"]]


def project_mode_labels(project_key=None):
    return [label for row in project_mode_rows(project_key) for label in row]
