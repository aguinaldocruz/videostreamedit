"""Browser regression: source clock, subtitle cues, seeks and switching races.

Uses only review_test_server's synthetic fixture, not the live catalog.
"""
import json
import os
import time
import urllib.request
from playwright.sync_api import sync_playwright

BASE = 'http://127.0.0.1:18383'


def resources():
    return json.load(urllib.request.urlopen(BASE + '/test/resources'))


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True, executable_path=os.getenv('PLAYWRIGHT_CHROMIUM_EXECUTABLE'))
    page = browser.new_page(viewport={'width':1280, 'height':900})
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.goto(BASE + '/test/player')
    page.locator('#review-media').click()
    page.wait_for_function('document.querySelector("video")?.currentTime>1', timeout=60000)
    def tone(selector, expected):
        # Measure the browser's decoded signal, not just container codec tags.
        measured = page.locator(selector).evaluate('''async media=>{
          const context=new AudioContext(),source=context.createMediaElementSource(media),analyser=context.createAnalyser();
          analyser.fftSize=8192;source.connect(analyser);analyser.connect(context.destination);await context.resume();
          await new Promise(resolve=>setTimeout(resolve,700));
          const data=new Float32Array(analyser.fftSize);analyser.getFloatTimeDomainData(data);
          let crossings=0,energy=0;for(let i=1;i<data.length;i++){if(data[i]>=0&&data[i-1]<0)crossings++;energy+=data[i]*data[i];}
          const result={hz:crossings*context.sampleRate/data.length,rms:Math.sqrt(energy/data.length)};
          source.disconnect();analyser.disconnect();await context.close();return result;
        }''')
        assert abs(measured['hz']-expected)<12 and measured['rms']>.02, measured
    tone('video', 440)
    page.locator('[data-subtitle]').select_option('embedded:subtitle:0')
    page.wait_for_function('document.querySelector("video track")?.track.cues?.length>=4')
    print('Initial playback and subtitle loaded', flush=True)

    def seek(seconds):
        page.locator('[data-timeline]').evaluate('(e,value)=>{e.value=value;e.dispatchEvent(new Event("change"));}', seconds)
        page.wait_for_function('value=>{const v=document.querySelector("video");const t=v?.currentTime+Number(v?.dataset.timelineOffset);return !v?.paused&&Math.abs(t-value)<1;}', arg=seconds, timeout=60000)

    seek(43.7)  # Outside the initial buffer and between long-GOP keyframes.
    print('Seek 43.7', page.locator('video').evaluate('v=>({t:v.currentTime,offset:v.dataset.timelineOffset})'), flush=True)
    page.wait_for_function('Array.from(document.querySelector("video track")?.track.activeCues||[]).some(c=>c.text==="At forty-three seconds")')
    print('Audio switch synchronized', flush=True)
    timing = page.locator('video').evaluate('v=>({time:v.currentTime,offset:Number(v.dataset.timelineOffset),cues:Array.from(v.querySelector("track").track.cues).map(c=>({text:c.text,start:c.startTime,end:c.endTime}))})')
    cue = next(cue for cue in timing['cues'] if cue['text']=='At forty-three seconds')
    assert abs(cue['start'] + timing['offset'] - 43.021) < .025, timing

    # A new producer starts at a preceding keyframe, but playback must resume
    # at the SAME source time, with the same active subtitle, for either codec.
    before = timing['time'] + timing['offset']
    page.locator('[data-audio]').select_option('embedded:audio:1')
    page.wait_for_function('document.querySelector("[data-plan]").textContent.includes("AAC")')
    page.wait_for_function('value=>{const v=document.querySelector("video");return !v?.paused&&Math.abs(v.currentTime+Number(v.dataset.timelineOffset)-value)<2;}', arg=before, timeout=60000)
    page.wait_for_function('Array.from(document.querySelector("video track")?.track.activeCues||[]).some(c=>c.text==="At forty-three seconds")')
    tone('video', 660)

    seek(9.3)
    print('Seek 9.3', flush=True)
    page.wait_for_function('Array.from(document.querySelector("video track")?.track.activeCues||[]).some(c=>c.text==="At nine seconds")')
    # Rapid seeking, switching and cancelling subtitle fetch/load handlers.
    for seconds in (60, 20, 71, 43.7):
        page.locator('[data-timeline]').evaluate('(e,value)=>{e.value=value;e.dispatchEvent(new Event("change"));}', seconds)
    page.locator('[data-subtitle]').select_option('')
    page.locator('[data-subtitle]').select_option('embedded:subtitle:0')
    page.locator('[data-subtitle]').select_option('')
    page.wait_for_function('()=>{const v=document.querySelector("video");return !v?.paused&&Math.abs(v.currentTime+Number(v.dataset.timelineOffset)-43.7)<2;}', timeout=60000)
    assert page.locator('video').evaluate('v=>Array.from(v.textTracks).every(t=>t.mode==="disabled")')
    page.locator('[data-subtitle]').select_option('embedded:subtitle:0')
    page.wait_for_function('document.querySelector("video track")?.track.activeCues?.length>0')
    assert page.locator('video').evaluate('v=>Array.from(v.textTracks).filter(t=>t.mode==="showing").length') == 1
    time.sleep(3)
    running = [item for item in resources()['sessions'] if not item['done']]
    assert len(running)==1 and not running[0]['error'], running
    assert running[0]['produced'] - running[0]['position'] < 60, running
    before=page.locator('video').evaluate('v=>v.currentTime+Number(v.dataset.timelineOffset)')
    page.locator('[data-output=video]').click()
    page.wait_for_function('value=>{const a=document.querySelector("audio");return !!a&&!a.paused&&Math.abs(a.currentTime+Number(a.dataset.timelineOffset)-value)<2;}',arg=before,timeout=60000)
    tone('audio', 660)
    page.locator('[data-close]').click()
    page.wait_for_timeout(1000)
    assert all(item['done'] for item in resources()['sessions']), resources()
    assert not errors, errors
    browser.close()
print('PASS: synchronized source/subtitle clock, keyframe seeks, copied/transcoded audio switches, measured browser tones, audio-only, rapid changes, disabled stale tracks and session cleanup')
