"""User-maintained subtitle replacement rules; configuration and preview only.

No route in this module reads or changes media, cached subtitles, or job queues.
Keep each rule in durable application settings so normal system backups include
it. Updates use compare-and-swap to avoid losing edits from another browser.
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Literal

from fastapi import HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator

from app.v2 import app, connection
from app.v5 import ISO_639_TO_1, canonical_language

logger = logging.getLogger("uvicorn.error")
PREFIX = "subtitle-autofix-rule/"
MAX_REPLACEMENTS = 500
MAX_PREVIEW_LENGTH = 32_768
MAX_PREVIEW_OUTPUT = 1_048_576
LANGUAGE_TAG = re.compile(r"([a-z]{2,3})(?:[-_]([a-z]{2}|\d{3}))?", re.I)
RULE_ID = re.compile(r"[0-9a-f]{32}")
DEFAULT_LANGUAGES = ("pt", "pt-BR", "pt-PT", "en", "en-US", "en-GB", "es", "es-ES", "es-MX",
                     "fr", "fr-FR", "fr-CA", *sorted(set(ISO_639_TO_1.values())), "und")


def normalized_language(value: str, region: str = "") -> str:
    """Use the same language aliases/regions as the editors, without a PT default."""
    source_tag = str(value).strip()
    if "|" in source_tag:
        if source_tag.count("|") != 1:
            raise ValueError("Choose a single language/region value.")
        base, area = (part.strip() for part in source_tag.split("|"))
        source_tag = base + ("-" + area if area else "")
    match = LANGUAGE_TAG.fullmatch(source_tag)
    if not match:
        raise ValueError("Choose a language or language/region value, such as pt, pt-BR, or en.")
    source = match[1].lower()
    language = canonical_language(source)
    area = (region.strip() or match[2] or ("BR" if source == "pob" else "")).upper()
    if area and not re.fullmatch(r"[A-Z]{2}|\d{3}", area):
        raise ValueError("Use a two-letter region or a three-digit region code.")
    if language == "und" and area:
        raise ValueError("Undetermined language does not have a region.")
    return f"{language}-{area}" if area else language


class Replacement(BaseModel):
    model_config = ConfigDict(extra="forbid")
    from_text: str = Field(alias="from", min_length=1, max_length=1_000)
    to_text: str = Field(alias="to", max_length=1_000)
    match: Literal["literal", "word"] = "literal"

    @field_validator("from_text", "to_text")
    @classmethod
    def single_line(cls, value: str) -> str:
        # Do not strip or normalize Unicode: whitespace and broken encodings
        # are often exactly what the user wants to replace.
        if "\r" in value or "\n" in value:
            raise ValueError("Use single-line replacements; subtitle line breaks are preserved.")
        return value

    @model_validator(mode="after")
    def useful_replacement(self) -> Replacement:
        if self.from_text == self.to_text:
            raise ValueError("From and To must differ; leave To empty to remove a value.")
        return self


class RuleFields(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=1_000)
    enabled: StrictBool = True
    languages: list[str] = Field(min_length=1, max_length=3)
    replacements: list[Replacement] = Field(min_length=1, max_length=MAX_REPLACEMENTS)

    @field_validator("name", "description")
    @classmethod
    def clean_label(cls, value: str, info) -> str:
        value = value.strip()
        if info.field_name == "name" and not value:
            raise ValueError("Give this rule a name.")
        if "\x00" in value:
            raise ValueError("Rule labels cannot contain null characters.")
        return value

    @field_validator("languages")
    @classmethod
    def clean_languages(cls, values: list[str]) -> list[str]:
        result = [normalized_language(value) for value in values]
        if len(set(result)) != len(result):
            raise ValueError("Select each language/region only once in a rule.")
        return result

    @model_validator(mode="after")
    def unique_replacements(self) -> RuleFields:
        keys = [(item.from_text, item.match) for item in self.replacements]
        if len(set(keys)) != len(keys):
            raise ValueError("Do not repeat the same From value and matching mode in one rule.")
        return self


class RuleUpdate(RuleFields):
    revision: int = Field(ge=1)


class SavedRule(RuleFields):
    schema_version: Literal[1] = 1
    id: str = Field(pattern=r"^[0-9a-f]{32}$")
    revision: int = Field(ge=1)
    created_at: str
    updated_at: str


class PreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rule: RuleFields
    text: str = Field(max_length=MAX_PREVIEW_LENGTH)


def language_matches(rule: RuleFields, language: str, region: str = "") -> bool:
    """Metadata matching for future executors; never infer a language from text."""
    if not rule.enabled:
        return False
    try:
        value = normalized_language(language or "und", region)
    except ValueError:
        return False
    base = value.split("-", 1)[0]
    return any(target == value or ("-" not in target and target == base) for target in rule.languages)


def preview_replacements(rule: RuleFields, text: str) -> dict:
    """Apply an ordered literal rule to a bounded sample, not a subtitle file."""
    if len(text) > MAX_PREVIEW_LENGTH:
        raise HTTPException(422, "Sample text is too long; use at most 32,768 characters.")
    current, counts = text, []
    for item in rule.replacements:
        if item.match == "word":
            pattern = re.compile(r"(?<!\w)" + re.escape(item.from_text) + r"(?!\w)")
            count = sum(1 for _ in pattern.finditer(current))
        else:
            pattern = None
            count = current.count(item.from_text)
        size = len(current) + count * (len(item.to_text) - len(item.from_text))
        if size > MAX_PREVIEW_OUTPUT:
            raise HTTPException(422, "This rule expands the sample too much. Review the replacement order.")
        if count:
            # A function replacement keeps backslashes and \1 literal too.
            current = pattern.sub(lambda _match: item.to_text, current) if pattern else current.replace(item.from_text, item.to_text)
        counts.append(count)
    return {"text": current, "changed": current != text, "replacement_count": sum(counts), "counts": counts}


def _key(rule_id: str) -> str:
    if not RULE_ID.fullmatch(rule_id):
        raise HTTPException(404, "Subtitle autofix rule not found.")
    return PREFIX + rule_id


def _decode(raw: str, key: str) -> SavedRule:
    try:
        rule = SavedRule.model_validate_json(raw)
        if key != PREFIX + rule.id:
            raise ValueError("Rule identifier differs from its saved key")
        return rule
    except ValueError as exc:
        logger.error("subtitle_autofix event=invalid_saved_rule key=%s", key)
        raise HTTPException(500, "A saved autofix rule could not be read. Its data was preserved; check the system log.") from exc


def _json(rule: SavedRule) -> str:
    return json.dumps(rule.model_dump(by_alias=True), ensure_ascii=False, separators=(",", ":"))


def _stored(db, rule_id: str) -> tuple[SavedRule, str]:
    key = _key(rule_id)
    row = db.execute("SELECT value FROM application_settings WHERE key=?", (key,)).fetchone()
    if not row:
        raise HTTPException(404, "Subtitle autofix rule not found. Refresh the list.")
    return _decode(row["value"], key), row["value"]


def _conflict() -> HTTPException:
    return HTTPException(409, "This rule changed in another screen. Your draft is still here; refresh before saving again.")


@app.get("/api/settings/subtitle-autofix/rules")
def list_rules() -> dict:
    with connection() as db:
        rows = db.execute("SELECT key,value FROM application_settings WHERE key LIKE ? ORDER BY key", (PREFIX + "%",)).fetchall()
        settings = db.execute("SELECT value FROM application_settings WHERE key IN ('language_region_order','language_region_custom') ORDER BY key DESC").fetchall()
    rules = [_decode(row["value"], row["key"]).model_dump(by_alias=True) for row in rows]
    rules.sort(key=lambda item: (item["name"].casefold(), item["id"]))
    # Read only small configuration rows, never scan the media catalog to build
    # this selector. Saved order comes first, followed by offline defaults.
    values = []
    for row in settings:
        try:
            configured = json.loads(row["value"])
            if isinstance(configured, list):
                values.extend(value for value in configured if isinstance(value, str))
        except (ValueError, TypeError):
            pass
    values.extend(DEFAULT_LANGUAGES)
    values.extend(value for rule in rules for value in rule["languages"])
    languages = []
    for value in values:
        try:
            value = normalized_language(value)
        except ValueError:
            continue
        if value not in languages:
            languages.append(value)
    return {"rules": rules, "languages": languages, "limits": {"languages": 3, "replacements": MAX_REPLACEMENTS, "preview_characters": MAX_PREVIEW_LENGTH}}


@app.post("/api/settings/subtitle-autofix/rules", status_code=201)
def create_rule(request: RuleFields) -> dict:
    stamp = datetime.now(timezone.utc).isoformat()
    rule = SavedRule(**request.model_dump(by_alias=True), id=uuid.uuid4().hex, revision=1, created_at=stamp, updated_at=stamp)
    with connection() as db:
        db.execute("INSERT INTO application_settings(key,value) VALUES(?,?)", (_key(rule.id), _json(rule)))
    logger.info("subtitle_autofix event=rule_created id=%s languages=%s replacements=%s", rule.id, len(rule.languages), len(rule.replacements))
    return rule.model_dump(by_alias=True)


@app.put("/api/settings/subtitle-autofix/rules/{rule_id}")
def update_rule(rule_id: str, request: RuleUpdate) -> dict:
    with connection() as db:
        previous, raw = _stored(db, rule_id)
        if previous.revision != request.revision:
            raise _conflict()
        rule = SavedRule(**request.model_dump(by_alias=True, exclude={"revision"}), id=previous.id,
                         revision=previous.revision + 1, created_at=previous.created_at,
                         updated_at=datetime.now(timezone.utc).isoformat())
        updated = db.execute("UPDATE application_settings SET value=? WHERE key=? AND value=?", (_json(rule), _key(rule_id), raw))
        if updated.rowcount != 1:
            raise _conflict()
    logger.info("subtitle_autofix event=rule_updated id=%s revision=%s", rule.id, rule.revision)
    return rule.model_dump(by_alias=True)


@app.delete("/api/settings/subtitle-autofix/rules/{rule_id}")
def delete_rule(rule_id: str, revision: int = Query(ge=1)) -> dict:
    with connection() as db:
        previous, raw = _stored(db, rule_id)
        if previous.revision != revision:
            raise _conflict()
        deleted = db.execute("DELETE FROM application_settings WHERE key=? AND value=?", (_key(rule_id), raw))
        if deleted.rowcount != 1:
            raise _conflict()
    logger.info("subtitle_autofix event=rule_deleted id=%s", rule_id)
    return {"deleted": rule_id}


@app.post("/api/settings/subtitle-autofix/preview")
def preview_rule(request: PreviewRequest) -> dict:
    return preview_replacements(request.rule, request.text)
