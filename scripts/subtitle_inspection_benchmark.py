#!/usr/bin/env python3
"""Bounded, read-only subtitle inspection benchmark.

Selects at most one representative embedded subtitle stream per codec and at
most 16 media from the current catalog. It only reads media and reports timing;
it never writes detector rows or enqueues work.
"""
from __future__ import annotations

import json
import statistics
import subprocess
import time
from pathlib import Path

from app.v11 import connection
from app.v79 import analyze_sdh, detect_common_variant, normalized_evidence_sample
from app.v51 import damage_kind

MAX_MEDIA = 6
TIMEOUT_SECONDS = 20

def main() -> int:
    with connection() as db:
        rows = db.execute("""
            SELECT m.path, msi.type_index, COALESCE(msi.codec,'') AS codec
            FROM media_stream_index msi JOIN plex_media m ON m.path=msi.path
            WHERE msi.stream_type='subtitle' AND m.path IS NOT NULL
            ORDER BY m.path, msi.type_index
        """).fetchall()
    selected=[]; codecs=set(); paths=set()
    for row in rows:
        path=str(row['path']); codec=str(row['codec'] or 'unknown').lower()
        if path in paths:
            continue
        # First pass gives codec diversity; second pass fills the bounded set.
        if codec in codecs and len(selected) < MAX_MEDIA//2:
            continue
        if Path(path).is_file():
            selected.append(dict(path=path,type_index=int(row['type_index']),codec=codec)); paths.add(path); codecs.add(codec)
        if len(selected)>=MAX_MEDIA: break
    if len(selected)<MAX_MEDIA:
        for row in rows:
            path=str(row['path'])
            if path in paths or not Path(path).is_file(): continue
            selected.append(dict(path=path,type_index=int(row['type_index']),codec=str(row['codec'] or 'unknown').lower())); paths.add(path)
            if len(selected)>=MAX_MEDIA: break
    timings=[]; results=[]
    for item in selected:
        started=time.perf_counter(); status='ok'; chars=0; cues=0
        try:
            out=subprocess.run(['ffmpeg','-nostdin','-hide_banner','-loglevel','error','-i',item['path'],'-map',f"0:s:{item['type_index']}",'-t','120','-f','srt','pipe:1'],capture_output=True,timeout=TIMEOUT_SECONDS,check=False)
            text=out.stdout.decode('utf-8','replace')
            if out.returncode and not text:
                status=f'ffmpeg_exit_{out.returncode}'
            chars=len(normalized_evidence_sample(text, 100000)); cues=len(__import__('re').findall(r'-->[^\n]*',text))
            detected,confidence,evidence=detect_common_variant(text, {'pt','en'})
            sdh,sdh_conf,_=analyze_sdh(text)
            damage=damage_kind(text)
        except subprocess.TimeoutExpired:
            status='timeout'; detected=''; confidence=0; evidence=''; sdh=''; sdh_conf=0; damage=''; text=''
        elapsed=time.perf_counter()-started; timings.append(elapsed)
        print(json.dumps({'sample':len(results)+1,'codec':item['codec'],'seconds':round(elapsed,3),'status':status},ensure_ascii=False),flush=True)
        results.append({'codec':item['codec'],'seconds':round(elapsed,3),'status':status,'cues':cues,'text_chars':chars,'detected':detected,'confidence':round(float(confidence),3),'sdh':sdh,'sdh_confidence':round(float(sdh_conf),3),'damage':damage})
    summary={'sample_limit':MAX_MEDIA,'sampled':len(results),'read_only':True,'codecs':sorted(codecs),'seconds_total':round(sum(timings),3),'seconds_mean':round(statistics.mean(timings),3) if timings else 0,'seconds_median':round(statistics.median(timings),3) if timings else 0,'media_per_second':round(len(results)/sum(timings),3) if timings and sum(timings)>0 else 0,'results':results}
    print(json.dumps(summary,ensure_ascii=False))
    return 0

if __name__=='__main__': raise SystemExit(main())
