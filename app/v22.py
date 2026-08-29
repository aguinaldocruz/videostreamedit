from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from pydantic import BaseModel, Field

from app.v13 import media_details_with_ietf
from app.v19 import app


class CloneCandidateRequest(BaseModel):
    paths: list[str] = Field(min_length=1, max_length=250)
    expected: dict


def normalized_clone_state(path: str) -> dict:
    details = media_details_with_ietf(path)
    counters: dict[str, int] = {}
    rows = []
    keys = {}
    for stream in [*details["streams"], *details["external_subtitles"]]:
        source = "external" if stream.get("external") else "embedded"
        codec_type = stream["codec_type"]
        counter_key = f"{source}:{codec_type}"
        position = counters.get(counter_key, 0)
        counters[counter_key] = position + 1
        identifier = f"{counter_key}:{position}"
        actual_key = f'external:{stream.get("path")}' if source == "external" else f'embedded:{codec_type}:{stream["type_index"]}'
        keys[actual_key] = identifier
        rows.append({
            "id": identifier,
            "source": source,
            "type": codec_type,
            "codec": stream.get("codec") or "unknown",
            "language": stream.get("language") or "",
            "region": stream.get("region") or "",
            "title": stream.get("title") or "",
            "embed": False,
            "removed": False,
        })

    def selected(codec_type: str, flag: str) -> str | None:
        for stream in [*details["streams"], *details["external_subtitles"]]:
            if stream["codec_type"] == codec_type and stream.get(flag):
                key = f'external:{stream.get("path")}' if stream.get("external") else f'embedded:{codec_type}:{stream["type_index"]}'
                return keys.get(key)
        return None

    return {
        "rows": rows,
        "order": {kind: [row["id"] for row in rows if row["type"] == kind] for kind in ("audio", "subtitle")},
        "defaultAudio": selected("audio", "default"),
        "forcedAudio": selected("audio", "forced"),
        "defaultSubtitle": selected("subtitle", "default"),
        "forcedSubtitle": selected("subtitle", "forced"),
    }


def inspect_clone_candidate(path: str, expected: dict) -> dict:
    try:
        return {"path": path, "compatible": normalized_clone_state(path) == expected}
    except Exception as exc:
        return {"path": path, "compatible": False, "error": str(exc)}


@app.post("/api/v22/tv/clone/inspect")
def inspect_clone_candidates(request: CloneCandidateRequest) -> dict:
    with ThreadPoolExecutor(max_workers=min(4, len(request.paths))) as executor:
        results = list(executor.map(lambda path: inspect_clone_candidate(path, request.expected), request.paths))
    candidates = [item["path"] for item in results if item["compatible"]]
    return {"count": len(candidates), "candidates": candidates, "checked": len(results)}
