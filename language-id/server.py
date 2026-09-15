from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from speechbrain.inference.classifiers import EncoderClassifier

app = FastAPI(title="VideoStreamEdit Speech Language ID", version="1.0")
MODEL_SOURCE = "speechbrain/lang-id-voxlingua107-ecapa"
MODEL_DIR = os.environ.get("SPEECHBRAIN_CACHE", "/models/speechbrain")
classifier: EncoderClassifier | None = None


def get_classifier() -> EncoderClassifier:
    global classifier
    if classifier is None:
        classifier = EncoderClassifier.from_hparams(
            source=MODEL_SOURCE,
            savedir=MODEL_DIR,
            run_opts={"device": "cpu"},
        )
    return classifier


def normalize_audio(source: Path, target: Path) -> None:
    result = subprocess.run(
        [
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
            "-i", str(source), "-vn", "-ac", "1", "-ar", "16000",
            "-c:a", "pcm_s16le", str(target),
        ],
        capture_output=True,
        text=True,
        timeout=180,
    )
    if result.returncode:
        raise ValueError(result.stderr[-1000:] or "ffmpeg could not decode audio")


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok", "model": MODEL_SOURCE}


@app.post("/detect-language")
async def detect_language(audio_file: UploadFile = File(...)) -> dict[str, object]:
    if not audio_file.filename:
        raise HTTPException(400, "audio_file must have a filename")
    suffix = Path(audio_file.filename).suffix or ".bin"
    try:
        with tempfile.TemporaryDirectory(prefix="vse-lang-") as work:
            source = Path(work) / ("input" + suffix)
            wav = Path(work) / "input.wav"
            source.write_bytes(await audio_file.read())
            normalize_audio(source, wav)
            out_prob, score, index, text_lab = get_classifier().classify_file(str(wav))
            label = str(text_lab[0])
            # VoxLingua labels are usually e.g. "pt", "en", or "por".
            code = label.split(":", 1)[0].strip().lower()
            # SpeechBrain returns log posterior scores, not probabilities.
            confidence = float(out_prob[0][int(index[0])].exp())
            return {
                "language_code": code,
                "detected_language": label,
                "confidence": confidence,
                "model": MODEL_SOURCE,
            }
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(500, f"language identification failed: {exc}") from exc

