from datetime import date, timedelta

TASKS = {}
_next_id = 1


def create_task(title: str, due_in_days: int, urgent: bool) -> dict:
    global _next_id
    task = {
        "id": _next_id,
        "title": title,
        "due": (date.today() + timedelta(days=due_in_days)).isoformat(),
        "urgent": urgent,
    }
    TASKS[_next_id] = task
    _next_id += 1
    return task


def calculate_priority(task: dict) -> int:
    days_left = (date.fromisoformat(task["due"]) - date.today()).days
    score = max(0, 10 - days_left)
    if task["urgent"]:
        score += 5
    return score
