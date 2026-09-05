#!/usr/bin/env python3
"""Find embedded text subtitles that already contain Unicode U+FFFD."""

from __future__ import annotations

import argparse
import fnmatch
import json
import logging
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path


LOGGER = logging.getLogger("subtitle-corruption-audit")
TEXT_CODECS = {
    "ass", "eia_608", "jacosub", "microdvd", "mov_text", "mpl2", "pjs",
    "realtext", "sami", "srt", "ssa", "stl", "subrip", "subviewer",
    "subviewer1", "text", "ttml", "vplayer", "webvtt",
}
HEX_LINE = re.compile(r"^[0-9a-fA-F]+:\s+(.+?)(?:\s{2,}.*)?$")


def arguments() -> argparse.Namespace:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    parser = argparse.ArgumentParser(
        description="Recursively find embedded text subtitles containing the Unicode replacement character (�).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("media_glob", help="Quoted path/wildcard, for example '/media/**/*.mkv'")
    parser.add_argument("--list", dest="finding_list", type=Path, default=Path("subtitle-corruption-findings.tsv"))
    parser.add_argument("--log", type=Path, default=Path(f"subtitle-corruption-audit-{stamp}.log"))
    parser.add_argument("--state", type=Path, default=Path("subtitle-corruption-audit.state.jsonl"))
    parser.add_argument("--timeout", type=int, default=1800, help="Maximum FFprobe seconds per media file")
    parser.add_argument("--fresh", action="store_true", help="Discard the checkpoint and scan every matching file again")
    parser.add_argument("--progress-every", type=int, default=25, help="Print progress after this many checked files")
    args = parser.parse_args()
    if args.timeout < 1 or args.progress_every < 1:
        parser.error("--timeout and --progress-every must be positive")
    return args


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
    positions = [expanded.find(char) for char in "*[?" if char in expanded]
    if not positions:
        path = Path(expanded)
        return (path if path.is_dir() else path.parent, "*.mkv" if path.is_dir() else path.name)
    position = min(positions)
    slash = expanded.rfind("/", 0, position)
    root = expanded[:slash] if slash > 0 else ("/" if expanded.startswith("/") else ".")
    return Path(root).resolve(), expanded[slash + 1:]


def discover(expression: str) -> list[Path]:
    root, pattern = wildcard_root(expression)
    if not root.is_dir():
        raise FileNotFoundError(f"Search root does not exist: {root}")
    simplified = pattern[3:] if pattern.startswith("**/") else pattern
    return sorted({
        path.resolve() for path in root.rglob("*")
        if path.is_file()
        and (fnmatch.fnmatch(str(path.relative_to(root)), pattern)
             or fnmatch.fnmatch(str(path.relative_to(root)), simplified))
    }, key=lambda path: str(path).casefold())


def packet_bytes(dump: str) -> bytes:
    result = bytearray()
    for line in dump.splitlines():
        match = HEX_LINE.match(line.strip())
        if not match:
            continue
        # FFprobe groups the hexadecimal column into four-character words.
        words = re.findall(r"\b[0-9a-fA-F]{4}\b|\b[0-9a-fA-F]{2}\b", match.group(1))
        if words:
            result.extend(bytes.fromhex("".join(words)))
    return bytes(result)


def scan(path: Path, timeout: int) -> list[dict]:
    command = [
        "ffprobe", "-v", "error", "-select_streams", "s", "-show_streams",
        "-show_packets", "-show_data", "-show_entries",
        "stream=index,codec_name:packet=stream_index,data", "-of", "json", str(path),
    ]
    completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=True)
    data = json.loads(completed.stdout)
    streams = data.get("streams") or []
    text_streams = {
        int(stream["index"]): (number + 1, str(stream.get("codec_name") or "unknown"))
        for number, stream in enumerate(streams)
        if str(stream.get("codec_name") or "").lower() in TEXT_CODECS
    }
    contents: dict[int, bytearray] = {index: bytearray() for index in text_streams}
    for packet in data.get("packets") or []:
        index = int(packet.get("stream_index", -1))
        if index in contents:
            contents[index].extend(packet_bytes(str(packet.get("data") or "")))
            contents[index].extend(b"\n")
    findings = []
    marker = "\ufffd".encode()
    for index, raw in contents.items():
        count = raw.count(marker)
        if not count:
            continue
        subtitle_number, codec = text_streams[index]
        text = bytes(raw).decode("utf-8", errors="replace")
        sample = next((line.strip() for line in text.splitlines() if "\ufffd" in line), "")
        findings.append({"subtitle": subtitle_number, "stream": index, "codec": codec, "count": count, "sample": sample[:180]})
    return findings


def load_state(path: Path) -> dict[str, dict]:
    result: dict[str, dict] = {}
    if not path.exists():
        return result
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            item = json.loads(line)
            result[item["path"]] = item
        except (json.JSONDecodeError, KeyError, TypeError):
            continue
    return result


def write_findings(path: Path, state: dict[str, dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as output:
        output.write("media\tsubtitle\tstream_index\tcodec\treplacement_count\tsample\n")
        for media in sorted(state, key=str.casefold):
            for finding in state[media].get("findings", []):
                sample = str(finding["sample"]).replace("\t", " ").replace("\r", " ").replace("\n", " ")
                output.write(f"{media}\tSubtitle {finding['subtitle']}\t{finding['stream']}\t{finding['codec']}\t{finding['count']}\t{sample}\n")
    temporary.replace(path)


def main() -> int:
    args = arguments()
    configure_logging(args.log)
    if shutil.which("ffprobe") is None:
        LOGGER.error("ffprobe is not installed or not in PATH")
        return 2
    try:
        files = discover(args.media_glob)
    except OSError as exc:
        LOGGER.error("discovery_failed error=%s", exc)
        return 2
    if args.fresh:
        args.state.unlink(missing_ok=True)
    state = load_state(args.state)
    pending = []
    for path in files:
        stat = path.stat()
        old = state.get(str(path))
        if old and old.get("size") == stat.st_size and old.get("mtime_ns") == stat.st_mtime_ns and old.get("status") in {"clean", "corrupt"}:
            continue
        pending.append((path, stat))
    LOGGER.info("scan_started discovered=%d cached=%d pending=%d", len(files), len(files) - len(pending), len(pending))
    args.state.parent.mkdir(parents=True, exist_ok=True)
    errors = 0
    with args.state.open("a", encoding="utf-8") as checkpoint:
        for number, (path, stat) in enumerate(pending, 1):
            try:
                findings = scan(path, args.timeout)
                status = "corrupt" if findings else "clean"
                item = {"path": str(path), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns, "status": status, "findings": findings}
                state[str(path)] = item
                checkpoint.write(json.dumps(item, ensure_ascii=False) + "\n")
                checkpoint.flush()
                if findings:
                    LOGGER.warning("corrupt file=%s tracks=%s replacements=%d", path, ",".join(str(x["subtitle"]) for x in findings), sum(x["count"] for x in findings))
                    write_findings(args.finding_list, state)
            except (subprocess.SubprocessError, json.JSONDecodeError, OSError, ValueError) as exc:
                errors += 1
                LOGGER.error("media_failed file=%s error=%s", path, str(exc).replace("\n", " "))
            if number % args.progress_every == 0 or number == len(pending):
                corrupt = sum(bool(item.get("findings")) for item in state.values())
                LOGGER.info("progress completed=%d pending_total=%d corrupt_media=%d errors=%d", number, len(pending), corrupt, errors)
    write_findings(args.finding_list, state)
    corrupt = sum(bool(item.get("findings")) for item in state.values())
    LOGGER.info("scan_finished files=%d corrupt_media=%d errors=%d list=%s state=%s", len(files), corrupt, errors, args.finding_list, args.state)
    return 1 if corrupt or errors else 0


if __name__ == "__main__":
    sys.exit(main())
