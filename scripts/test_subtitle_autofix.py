"""Autofix rules/configuration tests; no media, production DB, or jobs touched."""
import sqlite3
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi import HTTPException
from pydantic import ValidationError
from app import subtitle_autofix as autofix


def rule(**overrides):
    data = {"name": "Portuguese repairs", "languages": ["pt-BR", "por|PT"], "replacements": [
        {"from": "Ã£", "to": "ã"}, {"from": "â€™", "to": "'"},
        {"from": "bat", "to": "cat", "match": "word"}, {"from": "[ad]", "to": ""},
    ]}
    data.update(overrides)
    return autofix.RuleFields.model_validate(data)


def invalid(data):
    try:
        autofix.RuleFields.model_validate(data)
    except ValidationError:
        return
    raise AssertionError(f"Invalid rule accepted: {data}")


base = rule()
assert base.languages == ["pt-BR", "pt-PT"]
assert autofix.normalized_language("POR") == "pt"
assert autofix.normalized_language("pt|") == "pt"
assert autofix.normalized_language("pt-br") == "pt-BR"
assert autofix.normalized_language("pob") == "pt-BR"
assert autofix.normalized_language("eng_US") == "en-US"
assert autofix.normalized_language("es|419") == "es-419"
for bad in ("", "pt--", "pt-BR-extra", "Portuguese", "pt|BR|PT", "pt/BR", "en-", "und-BR"):
    try:
        autofix.normalized_language(bad)
    except ValueError:
        pass
    else:
        raise AssertionError(f"Malformed language accepted: {bad}")
assert autofix.language_matches(base, "por", "BR")
assert autofix.language_matches(base, "pt_PT")
assert autofix.language_matches(base, "pob")
assert not autofix.language_matches(base, "pt")
assert not autofix.language_matches(base, "eng")
assert not autofix.language_matches(rule(enabled=False), "pt-BR")
assert autofix.language_matches(rule(languages=["por"]), "pt-BR")
assert autofix.language_matches(rule(languages=["und"]), "")

for overrides in (
    {"name": "   "}, {"languages": []}, {"languages": ["pt", "en", "es", "fr"]},
    {"languages": ["por", "pt"]}, {"languages": ["pt-BR", "pob"]}, {"languages": [""]},
    {"replacements": []}, {"replacements": [{"from": "", "to": "ok"}]},
    {"replacements": [{"from": "foo", "to": "foo"}]},
    {"replacements": [{"from": "a\nb", "to": "a"}]},
    {"replacements": [{"from": "x", "to": "y", "match": "regex"}]},
    {"replacements": [{"from": "a", "to": "b"}, {"from": "a", "to": "c"}]},
    {"replacements": [{"from": "x", "to": "y"}] * (autofix.MAX_REPLACEMENTS + 1)},
    {"enabled": "false"}, {"_report_verified": True},
):
    invalid(base.model_dump(by_alias=True) | overrides)

sample = "NÃ£o â€™bat! combat battalion [ad] BAT. AÇÃO, avó, à, João.\nSecond line."
result = autofix.preview_replacements(base, sample)
assert result["text"] == "Não 'cat! combat battalion  BAT. AÇÃO, avó, à, João.\nSecond line."
assert result["counts"] == [1, 1, 1, 1]
assert result["replacement_count"] == 4 and result["changed"]
assert autofix.preview_replacements(rule(enabled=False), sample) == result, "Disabled drafts must be previewable"
assert autofix.preview_replacements(base, "Nothing to repair.")["changed"] is False
assert autofix.preview_replacements(base, "")["replacement_count"] == 0
assert autofix.preview_replacements(rule(replacements=[{"from": "  ", "to": " "}]), " a  b ")["text"] == " a b "
assert autofix.preview_replacements(rule(replacements=[{"from": ".*", "to": r"\1$"}]), "one.*two.*")["text"] == r"one\1$two\1$"
assert autofix.preview_replacements(rule(replacements=[{"from": "ação", "to": "gesto", "match": "word"}]), "ação reação AÇÃO pré-ação")["text"] == "gesto reação AÇÃO pré-gesto"
ordered = rule(replacements=[{"from": "a", "to": "b"}, {"from": "b", "to": "c"}])
assert autofix.preview_replacements(ordered, "a b")["text"] == "c c"
ordered.replacements.reverse()
assert autofix.preview_replacements(ordered, "a b")["text"] == "b c"
try:
    autofix.preview_replacements(rule(replacements=[{"from": "a", "to": "b" * 1000}]), "a" * 2000)
except HTTPException as exc:
    assert exc.status_code == 422
else:
    raise AssertionError("Excessive preview expansion accepted")

db = sqlite3.connect(":memory:")
db.row_factory = sqlite3.Row
db.execute("CREATE TABLE application_settings(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
db.execute("INSERT INTO application_settings VALUES('subtitle_color','#FFFF00')")
db.execute('INSERT INTO application_settings VALUES(?,?)', ('language_region_order', '["pt|BR","fr|CA","por","pt|PT"]'))
db.commit()


@contextmanager
def isolated():
    with db:
        yield db


with patch.object(autofix, "connection", isolated):
    listed = autofix.list_rules()
    assert not listed["rules"]
    assert listed["languages"][:4] == ["pt-BR", "fr-CA", "pt", "pt-PT"]
    first = autofix.create_rule(base)
    second = autofix.create_rule(rule(name="English spelling", languages=["eng"]))
    assert first["revision"] == 1 and first["id"] != second["id"]
    assert autofix.list_rules()["rules"][0]["name"] == "English spelling"
    replacement_body = {key: first[key] for key in ("name", "description", "enabled", "languages", "replacements", "revision")}
    replacement_body["enabled"] = False
    updated = autofix.update_rule(first["id"], autofix.RuleUpdate.model_validate(replacement_body))
    assert updated["revision"] == 2 and not updated["enabled"]
    assert updated["created_at"] == first["created_at"]
    assert updated["replacements"][0] == {"from": "Ã£", "to": "ã", "match": "literal"}
    for operation in (
        lambda: autofix.update_rule(first["id"], autofix.RuleUpdate.model_validate(replacement_body)),
        lambda: autofix.delete_rule(first["id"], revision=1),
    ):
        try:
            operation()
        except HTTPException as exc:
            assert exc.status_code == 409
        else:
            raise AssertionError("Stale edit overwrote a newer rule")
    assert autofix.delete_rule(first["id"], revision=2) == {"deleted": first["id"]}
    assert len(autofix.list_rules()["rules"]) == 1
    try:
        autofix.delete_rule("../../subtitle_color", revision=1)
    except HTTPException as exc:
        assert exc.status_code == 404
    assert db.execute("SELECT value FROM application_settings WHERE key='subtitle_color'").fetchone()[0] == "#FFFF00"
    # A corrupt record must be reported and retained, not silently erased by
    # a read, rewrite of the whole library, or another rule's successful edit.
    key = autofix.PREFIX + "0" * 32
    db.execute("INSERT INTO application_settings VALUES(?,?)", (key, '{"schema_version":2}'))
    db.commit()
    try:
        autofix.list_rules()
    except HTTPException as exc:
        assert exc.status_code == 500
    else:
        raise AssertionError("Unreadable durable rule silently ignored")
    assert db.execute("SELECT value FROM application_settings WHERE key=?", (key,)).fetchone()[0] == '{"schema_version":2}'

# Bounded pure preview performance smoke check, not a catalog scan.
many = rule(replacements=[{"from": f"unique-{i}", "to": f"corrected-{i}"} for i in range(500)])
started = time.perf_counter()
assert not autofix.preview_replacements(many, "Ação correta. " * 2000)["changed"]
elapsed = time.perf_counter() - started
assert elapsed < 3, f"Bounded sample preview was unexpectedly slow: {elapsed:.2f}s"
print(f"PASS: autofix validation, language/region matching, literal/word/Unicode replacements, row order, limits, CRUD, stale-edit protection, corrupt-record retention; 500-row preview {elapsed:.3f}s")
