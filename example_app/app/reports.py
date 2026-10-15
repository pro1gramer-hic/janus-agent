from app.services import TASKS, calculate_priority


def top_tasks(limit: int = 3) -> list:
    ranked = sorted(TASKS.values(), key=calculate_priority, reverse=True)
    return [task["title"] for task in ranked[:limit]]
