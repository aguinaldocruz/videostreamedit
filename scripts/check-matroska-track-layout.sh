#!/usr/bin/env bash
# Offline, read-only check of one Matroska file's physical Tracks/Cluster order.
# Exit 0: Tracks precede first Cluster; 1: they do not; 2: cannot determine.
set -u

if (( $# != 1 )); then
    printf 'Usage: %s FILE.mkv\n' "${0##*/}" >&2
    exit 2
fi

if [[ ! -f "$1" || ! -r "$1" ]]; then
    printf 'Cannot read file: %s\n' "$1" >&2
    exit 2
fi

if ! command -v python3 >/dev/null 2>&1; then
    printf 'Python 3 is required (standard library only).\n' >&2
    exit 2
fi

python3 - "$1" <<'PY'
import os
import sys

path = sys.argv[1]
EBML, SEGMENT, TRACKS, CLUSTER = 0x1A45DFA3, 0x18538067, 0x1654AE6B, 0x1F43B675


def vint(source, element_id=False):
    first = source.read(1)
    if not first or first == b"\x00":
        raise ValueError("invalid or missing EBML element")
    marker, length = 0x80, 1
    while not first[0] & marker:
        marker >>= 1
        length += 1
    if length > (4 if element_id else 8):
        raise ValueError("invalid EBML integer length")
    tail = source.read(length - 1)
    if len(tail) != length - 1:
        raise ValueError("truncated EBML element")
    number = int.from_bytes(first + tail, "big")
    if not element_id:
        number &= (1 << (7 * length)) - 1
        if number == (1 << (7 * length)) - 1:
            return None  # Unknown-sized Segment is permitted.
    return number


def element(source):
    identifier = vint(source, True)
    size = vint(source)
    return identifier, size, source.tell()


try:
    file_size = os.path.getsize(path)
    with open(path, "rb") as source:
        identifier, size, start = element(source)
        if identifier != EBML or size is None or start + size > file_size:
            raise ValueError("not a valid EBML header")
        source.seek(start + size)
        identifier, size, start = element(source)
        if identifier != SEGMENT:
            raise ValueError("Matroska Segment was not found")
        end = file_size if size is None else start + size
        if end > file_size:
            raise ValueError("Matroska Segment extends beyond the file")
        tracks_seen = False
        header_bytes = 0
        for _ in range(4096):
            if source.tell() >= end or header_bytes > 8 * 1024 * 1024:
                break
            before = source.tell()
            identifier, size, start = element(source)
            header_bytes += start - before
            if identifier == TRACKS:
                tracks_seen = True
            if identifier == CLUSTER:
                if tracks_seen:
                    print("OK: Matroska track headers are before the first media Cluster.")
                    sys.exit(0)
                print("WARNING: Matroska track headers are NOT before the first media Cluster.")
                print("This can be spec-valid with an early SeekHead, but may affect player compatibility.")
                print("A stream-copy remux may help; no file was changed.")
                sys.exit(1)
            if size is None or start + size > end:
                raise ValueError("cannot skip an earlier element with unknown or invalid size")
            source.seek(start + size)
        raise ValueError("first media Cluster was not found within the bounded header check")
except (OSError, ValueError) as exc:
    print(f"UNKNOWN: Cannot determine track-header layout: {exc}", file=sys.stderr)
    sys.exit(2)
PY
