from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from pydantic import BaseModel, Field

from app.v22 import app, normalized_clone_state


class CloneHistoryInspectRequest(BaseModel):
    paths: list[str] = Field(min_length=1, max_length=250)
    templates: list[dict] = Field(min_length=1, max_length=10)


def inspect_history_path(path: str) -> tuple[str, dict | None]:
    try:
        return path, normalized_clone_state(path)
    except Exception:
        return path, None


@app.post("/api/v25/tv/clone/history/inspect")
def inspect_clone_history(request: CloneHistoryInspectRequest) -> dict:
    with ThreadPoolExecutor(max_workers=min(4, len(request.paths))) as executor:
        states = list(executor.map(inspect_history_path, request.paths))
    matches = []
    for index, template in enumerate(request.templates):
        expected = template.get("before")
        candidates = [path for path, state in states if state is not None and state == expected]
        if candidates:
            matches.append({"template_index": index, "count": len(candidates), "candidates": candidates})
    return {"checked": len(states), "matches": matches}
