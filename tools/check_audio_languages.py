#!/usr/bin/env python3
"""Manually audit declared MKV audio languages using speech recognition."""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import subprocess
import sys
import tempfile
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pycountry
from faster_whisper import WhisperModel


LOGGER = logging.getLogger("audio-language-audit")
UNKNOWN_CODES = {"", "und", "unk", "unknown", "mul", "zxx"}
BIBLIOGRAPHIC_CODES = {
    "alb": "sq", "arm": "hy", "baq": "eu", "bur": "my", "chi": "zh",
    "cze": "cs", "dut": "nl", "fre": "fr", "geo": "ka", "ger": "de",
    "gre": "el", "ice": "is", "mac": "mk", "mao": "mi", "may": "ms",
    "per": "fa", "rum": "ro", "slo": "sk", "tib": "bo", "wel": "cy",
}


@dataclass
class SampleResult:
    offset: float
    language: str | None
    probability: float
    speech_seconds: float
    error: str | None = None


@dataclass
class StreamResult:
    ordinal: int
    codec: str
    title: str
    declared_raw: str
    declared: str | None
    detected: str | None
    confidence: float
    agreement: float
    status: str
    reason: str
    samples: list[SampleResult]


def parse_arguments() -> argparse.Namespace:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    parser = argparse.ArgumentParser(
        description="Recursively compare MKV audio language tags with detected speech.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "media_glob",
        help="Path plus wildcard, such as '/media/Movies/*.mkv' or '/media/**/*.mkv'.",
    )
    parser.add_argument("--model", default="small", help="faster-whisper model name or local model path")
    parser.add_argument("--device", default="cpu", choices=("cpu", "cuda", "auto"))
    parser.add_argument("--compute-type", default="int8", help="CTranslate2 compute type")
    parser.add_argument("--samples", type=int, default=3, help="Samples taken across each audio stream")
    parser.add_argument("--sample-seconds", type=float, default=30.0, help="Length of each sample")
    parser.add_argument("--confidence", type=float, default=0.75, help="Minimum accepted model probability")
    parser.add_argument("--agreement", type=float, default=0.60, help="Minimum share of sample weight agreeing")
    parser.add_argument("--model-cache", type=Path, default=Path("models"), help="Persistent model download directory")
    parser.add_argument("--log", type=Path, default=Path(f"audio-language-audit-{timestamp}.log"))
    parser.add_argument("--list", dest="finding_list", type=Path, default=Path(f"audio-language-findings-{timestamp}.txt"))
    arguments = parser.parse_args()
    if arguments.samples < 1:
        parser.error("--samples must be at least 1")
    if arguments.sample_seconds <= 0:
        parser.error("--sample-seconds must be greater than zero")
    if not 0 <= arguments.confidence <= 1 or not 0 <= arguments.agreement <= 1:
        parser.error("--confidence and --agreement must be between 0 and 1")
    return arguments


def configure_logging(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    file_handler = logging.FileHandler(path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    LOGGER.setLevel(logging.INFO)
    LOGGER.addHandler(file_handler)
    LOGGER.addHandler(console_handler)


def wildcard_root(expression: str) -> tuple[Path, str]:
    expanded = str(Path(expression).expanduser())
    wildcard_positions = [expanded.find(character) for character in "*[?" if character in expanded]
    if not wildcard_positions:
        path = Path(expanded)
        return (path if path.is_dir() else path.parent, "*.mkv" if path.is_dir() else path.name)
    position = min(wildcard_positions)
    slash = expanded.rfind("/", 0, position)
    root_text = expanded[:slash] if slash > 0 else ("/" if expanded.startswith("/") else ".")
    pattern = expanded[slash + 1:]
    return Path(root_text).resolve(), pattern


def discover_files(expression: str) -> list[Path]:
    root, pattern = wildcard_root(expression)
    if not root.is_dir():
        raise FileNotFoundError(f"Search root does not exist: {root}")
    simplified = pattern[3:] if pattern.startswith("**/") else pattern
    files = {
        path.resolve()
        for path in root.rglob("*")
        if path.is_file()
        and path.suffix.lower() == ".mkv"
        and (path.relative_to(root).match(pattern) or path.relative_to(root).match(simplified))
    }
    return sorted(files, key=lambda path: str(path).casefold())


def run_json(command: list[str]) -> dict:
    completed = subprocess.run(command, capture_output=True, text=True, check=True)
    return json.loads(completed.stdout)


def probe_media(path: Path) -> tuple[float, list[dict]]:
    data = run_json([
        "ffprobe", "-v", "error", "-show_entries",
        "format=duration:stream=index,codec_type,codec_name,duration:stream_tags=language,title",
        "-of", "json", str(path),
    ])
    duration = float((data.get("format") or {}).get("duration") or 0)
    audio = [stream for stream in data.get("streams", []) if stream.get("codec_type") == "audio"]
    return duration, audio


def normalize_language(value: str | None) -> str | None:
    raw = (value or "").strip().lower().replace("_", "-")
    base = raw.split("-", 1)[0]
    if base in UNKNOWN_CODES:
        return None
    if len(base) == 2:
        return base
    if base in BIBLIOGRAPHIC_CODES:
        return BIBLIOGRAPHIC_CODES[base]
    try:
        language = pycountry.languages.lookup(base)
    except LookupError:
        return base
    return getattr(language, "alpha_2", None) or base


def sample_offsets(duration: float, sample_seconds: float, count: int) -> list[float]:
    if duration <= sample_seconds:
        return [0.0]
    usable = max(0.0, duration - sample_seconds)
    fractions = [0.5] if count == 1 else [(index + 1) / (count + 1) for index in range(count)]
    return [round(usable * fraction, 3) for fraction in fractions]


def extract_sample(media: Path, audio_ordinal: int, offset: float, seconds: float, output: Path) -> None:
    subprocess.run([
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
        "-ss", str(offset), "-i", str(media), "-map", f"0:a:{audio_ordinal}",
        "-t", str(seconds), "-vn", "-ac", "1", "-ar", "16000",
        "-c:a", "pcm_s16le", "-y", str(output),
    ], capture_output=True, text=True, check=True)


def detect_sample(model: WhisperModel, sample: Path, offset: float) -> SampleResult:
    try:
        segments, info = model.transcribe(
            str(sample), beam_size=1, vad_filter=True,
            condition_on_previous_text=False, word_timestamps=False,
        )
        materialized = list(segments)
        speech_seconds = sum(max(0.0, segment.end - segment.start) for segment in materialized)
        if speech_seconds <= 0:
            return SampleResult(offset, None, 0.0, 0.0, "no speech detected")
        return SampleResult(offset, normalize_language(info.language), float(info.language_probability), speech_seconds)
    except Exception as error:  # Continue auditing other streams and files.
        return SampleResult(offset, None, 0.0, 0.0, str(error))


def combine_samples(samples: list[SampleResult]) -> tuple[str | None, float, float]:
    usable = [sample for sample in samples if sample.language and not sample.error]
    if not usable:
        return None, 0.0, 0.0
    weights: dict[str, float] = defaultdict(float)
    probabilities: dict[str, list[float]] = defaultdict(list)
    for sample in usable:
        weight = max(sample.probability, 0.01)
        weights[sample.language] += weight
        probabilities[sample.language].append(sample.probability)
    detected = max(weights, key=weights.get)
    confidence = sum(probabilities[detected]) / len(probabilities[detected])
    agreement = weights[detected] / sum(weights.values())
    return detected, confidence, agreement


def audit_stream(
    model: WhisperModel,
    media: Path,
    duration: float,
    stream: dict,
    ordinal: int,
    arguments: argparse.Namespace,
    temporary: Path,
) -> StreamResult:
    tags = stream.get("tags") or {}
    declared_raw = str(tags.get("language") or "")
    declared = normalize_language(declared_raw)
    samples = []
    for sample_number, offset in enumerate(sample_offsets(duration, arguments.sample_seconds, arguments.samples), 1):
        output = temporary / f"audio-{ordinal}-sample-{sample_number}.wav"
        try:
            extract_sample(media, ordinal, offset, arguments.sample_seconds, output)
            sample = detect_sample(model, output, offset)
        except subprocess.CalledProcessError as error:
            message = (error.stderr or str(error))[-1000:].replace("\n", " ")
            sample = SampleResult(offset, None, 0.0, 0.0, message)
        samples.append(sample)
        LOGGER.info(
            "sample file=%s audio=%d offset=%.3f detected=%s probability=%.4f speech_seconds=%.2f error=%s",
            media, ordinal + 1, offset, sample.language or "none", sample.probability,
            sample.speech_seconds, sample.error or "none",
        )
    detected, confidence, agreement = combine_samples(samples)
    if detected is None:
        status, reason = "CHECK", "no usable spoken-language result"
    elif declared is None:
        status, reason = "MISMATCH", f"missing or unsupported declared language; detected {detected}"
    elif confidence < arguments.confidence:
        status, reason = "CHECK", f"confidence {confidence:.3f} is below {arguments.confidence:.3f}"
    elif agreement < arguments.agreement:
        status, reason = "CHECK", f"sample agreement {agreement:.3f} is below {arguments.agreement:.3f}"
    elif declared != detected:
        status, reason = "MISMATCH", f"declared {declared}, detected {detected}"
    else:
        status, reason = "OK", f"declared and detected language are {declared}"
    result = StreamResult(
        ordinal=ordinal, codec=str(stream.get("codec_name") or "unknown"),
        title=str(tags.get("title") or ""), declared_raw=declared_raw,
        declared=declared, detected=detected, confidence=confidence,
        agreement=agreement, status=status, reason=reason, samples=samples,
    )
    LOGGER.info(
        "result file=%s audio=%d codec=%s title=%r declared_raw=%r declared=%s detected=%s confidence=%.4f agreement=%.4f status=%s reason=%s",
        media, ordinal + 1, result.codec, result.title, result.declared_raw,
        result.declared or "none", result.detected or "none", result.confidence,
        result.agreement, result.status, result.reason,
    )
    return result


def main() -> int:
    arguments = parse_arguments()
    configure_logging(arguments.log)
    for executable in ("ffmpeg", "ffprobe"):
        if not shutil.which(executable):
            LOGGER.error("Required executable is unavailable: %s", executable)
            return 2
    try:
        files = discover_files(arguments.media_glob)
    except Exception as error:
        LOGGER.error("Could not discover media: %s", error)
        return 2
    if not files:
        LOGGER.error("No matching MKV files: %s", arguments.media_glob)
        return 2
    arguments.model_cache.mkdir(parents=True, exist_ok=True)
    LOGGER.info(
        "audit_start expression=%r files=%d model=%s samples=%d sample_seconds=%.1f confidence=%.3f agreement=%.3f",
        arguments.media_glob, len(files), arguments.model, arguments.samples,
        arguments.sample_seconds, arguments.confidence, arguments.agreement,
    )
    LOGGER.info("Loading model; the first run may download model files into %s", arguments.model_cache)
    try:
        model = WhisperModel(
            arguments.model, device=arguments.device, compute_type=arguments.compute_type,
            download_root=str(arguments.model_cache),
        )
    except Exception as error:
        LOGGER.exception("Could not load speech-language model: %s", error)
        return 2
    findings: list[tuple[Path, list[StreamResult]]] = []
    tested_streams = 0
    for file_number, media in enumerate(files, 1):
        LOGGER.info("file_start number=%d total=%d path=%s", file_number, len(files), media)
        try:
            duration, streams = probe_media(media)
            if not streams:
                LOGGER.warning("file_result path=%s status=CHECK reason=no audio streams", media)
                findings.append((media, []))
                continue
            with tempfile.TemporaryDirectory(prefix="vse-language-") as directory:
                results = [
                    audit_stream(model, media, duration, stream, ordinal, arguments, Path(directory))
                    for ordinal, stream in enumerate(streams)
                ]
            tested_streams += len(results)
            if any(result.status != "OK" for result in results):
                findings.append((media, results))
            LOGGER.info("file_result path=%s status=%s audio_streams=%d", media, "CHECK" if media in {item[0] for item in findings} else "OK", len(results))
        except Exception as error:
            LOGGER.exception("file_result path=%s status=ERROR reason=%s", media, error)
            findings.append((media, []))
    arguments.finding_list.parent.mkdir(parents=True, exist_ok=True)
    arguments.finding_list.write_text(
        "".join(f"{media}\n" for media, _ in findings), encoding="utf-8"
    )
    LOGGER.info(
        "audit_complete files=%d audio_streams=%d findings=%d log=%s list=%s",
        len(files), tested_streams, len(findings), arguments.log, arguments.finding_list,
    )
    print(f"\nComplete: {len(files)} files, {tested_streams} audio streams, {len(findings)} files need review")
    print(f"Full log: {arguments.log}")
    print(f"Finding list: {arguments.finding_list}")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
