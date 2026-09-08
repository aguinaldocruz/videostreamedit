from pydantic import BaseModel, Field

from app.v11 import connection
from app.v85 import app


class LanguageRegionUse(BaseModel):
    value: str = Field(min_length=1, max_length=32)


@app.on_event("startup")
def initialize_language_region_usage() -> None:
    with connection() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS language_region_selection_usage (
                value TEXT PRIMARY KEY,
                use_count INTEGER NOT NULL DEFAULT 0
            )
        """)


@app.post("/api/v86/language-region-use")
def record_language_region_use(request: LanguageRegionUse) -> dict:
    value = request.value.strip()
    with connection() as db:
        db.execute(
            """INSERT INTO language_region_selection_usage(value, use_count)
               VALUES (?, 1)
               ON CONFLICT(value) DO UPDATE SET use_count = use_count + 1""",
            (value,),
        )
        count = db.execute(
            "SELECT use_count FROM language_region_selection_usage WHERE value=?",
            (value,),
        ).fetchone()["use_count"]
    return {"value": value, "use_count": count}
