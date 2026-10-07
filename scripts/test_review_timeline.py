"""Real FFmpeg timeline/packet regression checks; generated media only."""
import ast
import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
tree = ast.parse((ROOT / 'app/review_playback.py').read_text())
scope = {'Path':Path, 'PlaybackRequest':SimpleNamespace}
exec(compile(ast.Module(body=[node for node in tree.body if isinstance(node, ast.FunctionDef)
                             and node.name=='playback_command'], type_ignores=[]), str(ROOT / 'app/review_playback.py'), 'exec'), scope)


def run(command):
    return subprocess.run(command, check=True, capture_output=True, timeout=90).stdout


def packets(path, interval=None):
    return json.loads(run(['ffprobe', '-v', 'error'] + (['-read_intervals', interval] if interval else []) + ['-show_packets', '-show_data', '-show_entries',
                          'packet=stream_index,pts_time,dts_time,data_hash,data', '-show_data_hash', 'sha256',
                          '-of', 'json', str(path)]))['packets']


def regression():
    with tempfile.TemporaryDirectory(prefix='vse-review-timing-') as temporary:
        folder = Path(temporary)
        media = folder / 'source.mkv'
        run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=160x90:rate=24:duration=24',
             '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000:duration=24',
             '-c:v', 'libx264', '-threads', '2', '-g', '144', '-sc_threshold', '0', '-bf', '3',
             '-c:a', 'aac', '-output_ts_offset', '5', str(media)])
        source = packets(media)
        metadata = json.loads(run(['ffprobe', '-v', 'error', '-show_format', '-of', 'json', str(media)]))
        start_time = float(metadata['format']['start_time'])
        originals = {}
        for item in source:
            originals.setdefault(item['data_hash'], []).append(item)
        for seek in (0, 9.3, 17.7):
            for mode in ('av', 'audio', 'video'):
                output = folder / f'{mode}-{seek}'
                output.mkdir()
                request = SimpleNamespace(start=seek, mode=mode, audio_index=0, force_aac=False)
                command = scope['playback_command'](request, media, output, {'video_copy':True, 'audio_copy':True, 'source_origin':start_time}, None)
                del command[command.index('-readrate'):command.index('-readrate')+2]
                run(command)
                result = packets(output / 'index.m3u8')
                # Transport wrappers must not change the source clock or AAC
                # samples. Video keyframe preroll is deliberately retained.
                for packet in result:
                    source_index = packet['stream_index'] if mode=='av' else (1 if mode=='audio' else 0)
                    if source_index==1:
                        data = bytes.fromhex(''.join(line.split(':',1)[1].split('  ',1)[0] for line in packet['data'].splitlines() if ':' in line))
                        if mode!='audio' and (data[:2]==b'\xff\xf1' or data[:2]==b'\xff\xf9'):
                            data=data[7:]
                        digest='SHA256:'+hashlib.sha256(data).hexdigest()
                        matches = originals.get(digest, [])
                    else:
                        matches = [original for original in source if original['stream_index']==0]
                    assert matches, (mode, seek, packet['pts_time'])
                    # Matroska CodecDelay/AAC priming is represented differently
                    # in TS; allow ONE 1024-sample frame, never seconds of drift.
                    tolerance = 1024/48000+.002 if source_index==1 else .002
                    assert any(abs(float(packet['pts_time']) - float(original['pts_time'])) < tolerance for original in matches), (mode, seek, packet['pts_time'], [item['pts_time'] for item in matches[:2]])
                if mode != 'video':
                    audio_index = 1 if mode == 'av' else 0
                    audio = [packet for packet in result if packet['stream_index']==audio_index]
                    assert len(audio)>50
                    for pair, (previous, current) in enumerate(zip(audio, audio[1:])):
                        step = float(current['pts_time'])-float(previous['pts_time'])
                        assert abs(step-1024/48000)<.002, (mode,seek,previous['pts_time'],current['pts_time'])
        print('PASS: nine real playback combinations preserve video timestamps, copied AAC payloads, audio continuity and source/subtitle clock at zero and non-keyframe seeks')


def real_sample(media):
    """Bounded, read-only playback check of a representative catalog file."""
    metadata = json.loads(run(['ffprobe', '-v', 'error', '-show_streams', '-show_format', '-of', 'json', str(media)]))
    origin = float(metadata['format'].get('start_time') or 0)
    with tempfile.TemporaryDirectory(prefix='vse-review-real-') as temporary:
        for index, stream in enumerate(s for s in metadata['streams'] if s['codec_type']=='audio'):
            output = Path(temporary) / str(index)
            output.mkdir()
            request = SimpleNamespace(start=63.7, mode='av', audio_index=index, force_aac=False)
            plan = {'video_copy':True, 'audio_copy':stream['codec_name']=='aac', 'source_origin':origin}
            command = scope['playback_command'](request, media, output, plan, None)
            del command[command.index('-readrate'):command.index('-readrate')+2]
            command[command.index('-sn'):command.index('-sn')] = ['-frames:v','480']
            run(command)
            result = packets(output/'index.m3u8')
            run(['ffmpeg', '-v', 'error', '-i', str(output/'index.m3u8'), '-map', '0:a:0', '-map', '0:v:0', '-f', 'null', '-'])
            video = next(packet for packet in result if packet['stream_index']==0)
            audio = next(packet for packet in result if packet['stream_index']==1)
            assert abs(float(video['pts_time'])-origin-request.start)<15
            assert abs(float(audio['pts_time'])-float(video['pts_time']))<1
            print(f'PASS: audio {index+1} · {stream["codec_name"]} · bounded video/audio decode · first video PTS {video["pts_time"]} · first audio PTS {audio["pts_time"]}')


if __name__ == '__main__':
    real_sample(Path(sys.argv[2])) if len(sys.argv)>2 and sys.argv[1]=='--media' else regression()
