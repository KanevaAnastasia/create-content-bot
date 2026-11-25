"""One project-specific menu shared by workspace and text-first entry points."""
from .discipline_modes import ARCHIVE, BOSS, TWO_LIVES, LEARNING, EXPERIMENT, FLOW, FORK, LABELS, REFRAME, VASYA

PENDING_MODE_KEY = "pending_project_generation_mode"


def project_mode_rows(project_key=None):
    project = str(project_key or "grandma_kitchen").strip()
    if project == "discipline":
        return [["История"], [LABELS[VASYA], LABELS[REFRAME]],
                [LABELS[FLOW], LABELS[BOSS]], [LABELS[TWO_LIVES], LABELS[ARCHIVE]], [LABELS[LEARNING]],
                [LABELS[FORK], LABELS[EXPERIMENT]], ["Очередь Дисциплины"],
                ["Визуализация"], ["Копия"], ["Реклама"]]
    rows = [["Обычный", "Совет"], ["По референсу"]] if project == "grandma_kitchen" else [["Обычный"]]
    return rows + [["Silence"], ["Длинный"], ["Копия"], ["История"], ["Визуализация"], ["Реклама"]]


def project_mode_labels(project_key=None):
    return [label for row in project_mode_rows(project_key) for label in row]
