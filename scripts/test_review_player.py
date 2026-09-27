"""Exercise the disposable review_test_server (not the production application)."""
import json
import time
import urllib.request
import urllib.error
import urllib.parse
from playwright.sync_api import sync_playwright

BASE='http://127.0.0.1:18383'
def api(path,body=None,method=None):
    req=urllib.request.Request(BASE+path,data=json.dumps(body).encode() if body is not None else None,
        headers={'Content-Type':'application/json'},method=method)
    with urllib.request.urlopen(req,timeout=120) as response:return json.load(response)

fixture=api('/test/fixture')
with sync_playwright() as p:
    browser=p.chromium.launch(headless=True)
    page=browser.new_page(viewport={'width':1280,'height':900})
    errors=[]
    page.on('pageerror',lambda error:errors.append(str(error)))
    page.goto(BASE+'/test/player')
    page.locator('#review-media').click()
    page.wait_for_function('document.querySelector("video")?.currentTime>2',timeout=60000)
    first=page.locator('video').evaluate('(v)=>v.currentTime')
    page.locator('[data-subtitle]').select_option('embedded:subtitle:0')
    page.wait_for_function('document.querySelector("video track")?.track?.cues?.length>0')
    assert page.locator('video').evaluate('(v)=>v.currentTime')>=first
    page.locator('[data-audio]').select_option('embedded:audio:1')
    page.wait_for_function('document.querySelector("[data-plan]").textContent.includes("AAC")')
    page.wait_for_function('document.querySelector("video")?.currentTime>2',timeout=60000)
    page.locator('[data-timeline]').evaluate('(el)=>{el.value=45;el.dispatchEvent(new Event("change"));}')
    page.wait_for_function('document.querySelector("[data-time]").textContent.startsWith("0:4")',timeout=60000)
    page.wait_for_function('document.querySelector("video")?.currentTime>1',timeout=60000)
    page.locator('[data-output=audio]').click()  # Video + subtitle; audio is not required.
    page.wait_for_function('document.querySelector("video")?.currentTime>1',timeout=60000)
    bounds=page.locator('video').bounding_box()
    assert bounds['y']+bounds['height']<=900, bounds
    page.locator('[data-close]').click()
    assert not errors, errors
    browser.close()

# Prepare and add a real AAC track using only the synthetic file.
prepared=api('/api/review/audio',{'path':fixture['path'],'audio_index':1})
api('/test/run/'+str(prepared['task_id']),{},'POST')
items=api('/api/review/audio?path='+urllib.parse.quote(fixture['path']))['items']
assert items[0]['status']=='ready',items
before=api('/test/fixture')
assert len(before['streams'])==4
approved=api('/api/review/audio/'+prepared['id']+'/approve',{'action':'add'})
api('/test/run/'+str(approved['task_id']),{},'POST')
after=api('/test/fixture')
audios=[s for s in after['streams'] if s['codec_type']=='audio']
assert [s['codec_name'] for s in audios]==['aac','ac3','aac']
assert audios[-1]['channels']==2
assert audios[-1]['disposition']['default']==0
assert not api('/api/review/audio?path='+urllib.parse.quote(fixture['path']))['items']
prepared=api('/api/review/audio',{'path':fixture['path'],'audio_index':1})
api('/test/run/'+str(prepared['task_id']),{},'POST')
approved=api('/api/review/audio/'+prepared['id']+'/approve',{'action':'replace'})
api('/test/run/'+str(approved['task_id']),{},'POST')
replaced=api('/test/fixture')
assert [s['codec_name'] for s in replaced['streams'] if s['codec_type']=='audio']==['aac','aac','aac']
prepared=api('/api/review/audio',{'path':fixture['path'],'audio_index':0})
api('/test/run/'+str(prepared['task_id']),{},'POST')
api('/test/touch',{},'POST')
try:
    api('/api/review/audio/'+prepared['id']+'/approve',{'action':'replace'})
    raise AssertionError('Stale approval should fail')
except urllib.error.HTTPError as error:
    assert error.code==409
api('/api/review/audio/'+prepared['id'],method='DELETE')
assert api('/test/resources')['staged_directories']==0
for action in ('discard','commit'):
    prepared=api('/api/review/audio',{'path':fixture['path'],'audio_index':0})
    api('/test/run/'+str(prepared['task_id']),{},'POST')
    before_count=len(api('/test/fixture')['streams'])
    approval=api('/api/review/audio/'+prepared['id']+'/approve',{'action':'add','draft_session':'fixture-draft'})
    assert approval['draft'] is True
    assert len(api('/test/fixture')['streams'])==before_count
    api('/test/draft/'+action,{},'POST')
    assert len(api('/test/fixture')['streams'])==before_count+(action=='commit')
    if action=='discard':
        assert api('/api/review/audio?path='+urllib.parse.quote(fixture['path']))['items'][0]['status']=='ready'
        api('/api/review/audio/'+prepared['id'],method='DELETE')
print('PASS: real HLS playback, subtitle hot switch, audio switch, full timeline seek, video-only, height fit, AAC staging, approved add/replace, stale-source refusal, rejection cleanup; no production media touched')
print('PASS: draft audio approval does not mutate media; Discard releases approval and Save consolidates integration')
