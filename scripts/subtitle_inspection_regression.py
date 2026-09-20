#!/usr/bin/env python3
"""Read-only regression checks for targeted subtitle/voice invalidation.

This script intentionally does not inspect or enqueue media. It validates the
pure decision and fingerprint helpers used before scheduling inspection work.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.subtitle_detector_config import SUBTITLE_DETECTOR_VERSION
from app.v51 import damage_kind
from app.v79 import (
    _subtitle_analysis_signature,
    analyze_sdh,
    calibrate_subtitle_confidence,
    detect_common_variant,
    normalized_evidence_sample,
    subtitle_quality_issue,
)
from app.v80 import detection_scope_for_edit, detection_scope_for_operation


@dataclass(frozen=True)
class Stat:
    st_size: int = 1234
    st_mtime_ns: int = 5678


def check(name: str, actual: object, expected: object) -> None:
    if actual != expected:
        raise AssertionError(f"{name}: expected {expected!r}, got {actual!r}")


def main() -> int:
    # Metadata-only edits do not change spoken/subtitle text.
    check("track-name-only", detection_scope_for_edit({"tracks": [{"codec_type": "subtitle", "type_index": 2, "track_name": "Signs"}]}), {})
    check("default-forced-only", detection_scope_for_edit({"tracks": [{"codec_type": "audio", "type_index": 1, "default": True, "forced": False}]}), {})

    # Language/region changes target only the changed streams.
    check("subtitle-language", detection_scope_for_edit({"tracks": [{"codec_type": "subtitle", "type_index": 3, "language": "pt", "region": "BR"}]}), {"subtitle_indices": [3]})
    check("audio-language", detection_scope_for_edit({"tracks": [{"codec_type": "audio", "type_index": 1, "language": "en"}]}), {"audio_indices": [1]})
    check("subtitle-removal", detection_scope_for_edit({"remove": ["embedded:subtitle:4"]}), {"subtitle_indices": "all"})
    check("external-integration", detection_scope_for_edit({"external_subtitles": [{"embed": True}]}), {"subtitle_indices": "all"})

    check("subtitle-operation", detection_scope_for_operation("subtitle_content"), {"subtitle_indices": "all"})
    check("audio-operation", detection_scope_for_operation("audio_content"), {"audio_indices": "all"})
    check("metadata-only-operation", detection_scope_for_operation("track_name"), {})

    stream = {"source": "embedded", "type_index": 2, "codec": "SubRip/SRT", "language": "pt", "region": "BR"}
    first = _subtitle_analysis_signature("/media/example.mkv", stream, ["pt", "pt-BR", "en"], Stat())
    same = _subtitle_analysis_signature("/media/example.mkv", stream, ["pt", "pt-BR", "en"], Stat())
    changed_version = _subtitle_analysis_signature("/media/example.mkv", stream, ["pt", "pt-BR", "en"], Stat(), SUBTITLE_DETECTOR_VERSION + 1)
    changed_metadata = _subtitle_analysis_signature("/media/example.mkv", {**stream, "region": "PT"}, ["pt", "pt-BR", "en"], Stat())
    check("stable-fingerprint", same, first)
    if first == changed_version:
        raise AssertionError("detector version must invalidate the stream fingerprint")
    if first == changed_metadata:
        raise AssertionError("stream metadata must invalidate the stream fingerprint")

    if subtitle_quality_issue("visit https://example.invalid now", "") != "Subtitle contains advertising or link-like text":
        raise AssertionError("link-like subtitle content must be actionable")
    mixed = " ".join(["tu comboio"] * 8 + ["the is here"] * 8)
    detected, confidence, evidence = detect_common_variant(mixed, {"pt", "en"})
    if detected or confidence != 0.0 or "Mixed Portuguese/English" not in evidence:
        raise AssertionError("balanced bilingual evidence must remain explicitly inconclusive")
    repetitive = " ".join(["você está chegando agora"] * 80)
    _, repetitive_confidence, _ = detect_common_variant(repetitive, {"pt", "en"})
    if repetitive_confidence > 0.80:
        raise AssertionError("repeated marker words must not manufacture high confidence")
    sample = normalized_evidence_sample("1\n00:00:01,000 --> 00:00:02,000\n<i>Olá, mundo!</i>")
    if sample != "Olá, mundo!" or len(sample) > 280:
        raise AssertionError("diagnostic excerpt must normalize markup/timing and remain bounded")

    if calibrate_subtitle_confidence(0.95, 1, 80, 1.0) > 0.60:
        raise AssertionError("sparse subtitles must not retain high confidence")
    if calibrate_subtitle_confidence(0.95, 20, 1200, 0.8) < 0.90:
        raise AssertionError("well-covered subtitles should retain strong confidence")

    br_detected, _, _ = detect_common_variant("ação ótimo direção receção", {"pt"})
    pt_detected, _, _ = detect_common_variant("acção óptimo direcção recepção", {"pt"})
    if br_detected != "pt-BR" or pt_detected != "pt-PT":
        raise AssertionError("Portuguese spelling evidence must distinguish regional variants")
    gibberish = "1\n00:00:01,000 --> 00:00:02,000\na b c d e f g h i j k l"
    if "OCR gibberish" not in damage_kind(gibberish):
        raise AssertionError("isolated OCR characters must be classified as damage")

    label, confidence, evidence = analyze_sdh("1\n00:00:01,000 --> 00:00:02,000\n[MUSIC]\n")
    if not label or confidence <= 0 or not evidence:
        raise AssertionError("SDH analysis should provide a label, confidence and evidence")

    print(f"subtitle inspection regression passed (detector v{SUBTITLE_DETECTOR_VERSION})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
