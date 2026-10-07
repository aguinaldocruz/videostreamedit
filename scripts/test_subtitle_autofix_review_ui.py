"""Browser fixture: full text/approval flow; no production media/jobs used."""
import os
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT=Path(__file__).resolve().parents[1]
fixture='''<!doctype html><meta charset="utf-8"><style>
body{font:15px Arial;background:#111820;color:#e7edf5}button,select{font:inherit;padding:7px;border:1px solid #485365;background:#202b3a;color:inherit;border-radius:6px}button{cursor:pointer}button:disabled{opacity:.4}[hidden]{display:none!important}dialog{background:#151b22;color:inherit;border:1px solid #485365;border-radius:10px}.dialog-title,.dialog-actions{background:#202833}h3{margin-top:0}.muted{color:#9aa6b2}.error{color:#f7aaaa}.autofix-badge{padding:5px;border:1px solid #485365;border-radius:5px}
</style><button id="open">Autofix</button><button id="open-tv">TV episode Autofix</button><button id="open-encoding">UTF-8 quickfix</button>'''

with sync_playwright() as p:
    browser=p.chromium.launch(headless=True,executable_path=os.getenv('PLAYWRIGHT_CHROMIUM_EXECUTABLE'))
    page=browser.new_page(viewport={'width':1300,'height':900})
    errors=[]
    page.on('pageerror',lambda value:errors.append(str(value)))
    page.route('http://review.test/**',lambda route:route.fulfill(status=200,content_type='text/html',body=fixture))
    page.goto('http://review.test/')
    page.evaluate('''()=>{
      window.calls=[];window.applied=[];window.rulesAvailable=true;window.stale=false;window.failedTask=false;window.polls=0;window.sequence=0;
      window.toast=()=>{};window.events=[];['media-properties-queued','media-properties-applied'].forEach(name=>document.addEventListener(name,event=>events.push({name,...event.detail})));
      const source='1\\n00:00:00,000 --> 00:00:01,000\\nFez cafÃ©? <i>Sim</i>\\n';
      window.fullText=source.replace('cafÃ©','café')+'<script>window.xssExecuted=true</script>\\n'+'Line content\\n'.repeat(4000)+'END OF FULL SUBTITLE';
      window.review=null;
      const copy=value=>JSON.parse(JSON.stringify(value));
      window.api=async(url,options={})=>{
        const method=options.method||'GET',payload=options.body?JSON.parse(options.body):null;calls.push({url,method,payload});
        if(url.endsWith('/options'))return{rules:rulesAvailable?[{id:'a'.repeat(32),name:'Portuguese repair',languages:['pt-BR','pt-PT'],matching_streams:2},{id:'b'.repeat(32),name:'English rule',languages:['en'],matching_streams:0}]:[],note:'SRT/SubRip text only.'};
        if(url.endsWith('/prepare')){const encoding=url.includes('/encoding/');review={review_id:payload.operation_id,repair_kind:encoding?'encoding':'rule',rule:encoding?'Normalize encoding to UTF-8':'Portuguese repair',streams:[{id:'first',label:'Subtitle 1',source:'embedded',language:'pt-BR',replacements:encoding?0:1,cached:true,decision:null,source_encoding:'Windows-1252 (inferred)'},{id:'second',label:'Subtitle 2',source:'embedded',language:'pt-PT',replacements:encoding?0:2,cached:true,decision:null,source_encoding:'Windows-1252 (inferred)'}],unchanged:1,warnings:['Subtitle 3: image format is unsupported']};return copy(review)}
        if(url.endsWith('/progress'))return{step:2,total:5,message:'Applying replacements to Subtitle 1'};
        if(url.includes('/streams/')){const stream=review.streams.find(item=>url.endsWith('/'+item.id));return{...copy(stream),original:review.repair_kind==='encoding'?fullText:source,text:fullText}}
        if(url.endsWith('/decision')){review.streams.find(item=>item.id===payload.stream_id).decision=payload.approve;return copy(review)}
        if(url.endsWith('/apply')){if(stale)throw Error('Media changed since preview. Nothing was applied; prepare a new review.');if(review.streams.some(item=>item.decision===null))throw Error('Unreviewed stream');applied.push({mode:payload.mode,streams:copy(review.streams.filter(item=>item.decision))});return{task_id:77,mode:payload.mode,accepted:true}}
        if(url==='/api/v65/queue/status'){polls++;return{items:[{id:77,status:failedTask?'failed':polls>1?'succeeded':'running',progress_current:polls>1?8:3,progress_total:8,progress_message:'Remuxing once to replace approved subtitles',error:failedTask?'Verification refused; original retained':null}]}}
        if(method==='DELETE')return{discarded:true};
        throw Error('Unexpected API call '+url);
      };
      document.querySelector('#open').onclick=()=>openSubtitleAutofixReview('/media/Movie.mkv','Fixture movie');
      document.querySelector('#open-tv').onclick=()=>openSubtitleAutofixReview('/media/Show.S01E01.mkv','Fixture show · S01E01');
      document.querySelector('#open-encoding').onclick=()=>openSubtitleEncodingReview('/media/Movie.mkv','Legacy movie');
    }''')
    page.add_style_tag(content=(ROOT/'app/static/subtitle-autofix-review.css').read_text())
    page.add_script_tag(content=(ROOT/'app/static/v33-global-busy.js').read_text())
    page.add_script_tag(content=(ROOT/'app/static/subtitle-autofix-review.js').read_text())
    def released():page.wait_for_function("!document.body.classList.contains('app-busy')")
    def open_review(tv=False):
        page.locator('#open-tv' if tv else '#open').click();released()
        assert page.locator('[data-af-rule] option').count()==1
        assert not page.locator('[data-af-apply]').count(), 'Execution available before approval'
        page.locator('[data-af-prepare]').click()
        page.wait_for_function("document.querySelector('[data-af-fixed]')?.textContent.endsWith('END OF FULL SUBTITLE')")
        released()
        assert page.locator('[data-af-fixed]').inner_text().endswith('END OF FULL SUBTITLE')
        assert 'café?' in page.locator('[data-af-fixed]').inner_text()
        assert page.evaluate('!window.xssExecuted'), 'Subtitle text executed as HTML'
        assert not page.evaluate('applied.length'), 'Preview changed media'
    open_review()
    assert 'Review 1 of 2' in page.locator('[data-af-stage]').inner_text()
    page.locator('[data-af-compare]').check()
    assert page.locator('[data-af-original]').is_visible()
    assert 'cafÃ©' in page.locator('[data-af-original]').inner_text()
    page.screenshot(path='/tmp/vse-autofix-review-wide.png')
    for width in (850,360):
        page.set_viewport_size({'width':width,'height':760})
        assert page.locator('[data-af-approve]').is_visible()
        assert page.locator('[data-af-reject]').is_visible()
        assert page.evaluate("document.querySelector('#subtitle-autofix-review-dialog').getBoundingClientRect().bottom<=innerHeight"), 'Review controls overflow viewport'
        assert page.evaluate("document.querySelector('#subtitle-autofix-review-dialog').scrollWidth<=document.querySelector('#subtitle-autofix-review-dialog').clientWidth+1"), 'Review horizontally overflowed'
    page.set_viewport_size({'width':1300,'height':900})
    page.locator('[data-af-approve]').click();released()
    assert 'Review 2 of 2' in page.locator('[data-af-stage]').inner_text()
    assert not page.locator('[data-af-apply]').count()
    page.locator('[data-af-reject]').click();released()
    assert '1 approved · 1 rejected' in page.locator('[data-af-body]').inner_text()
    assert not page.evaluate('applied.length')
    page.evaluate('stale=true')
    page.locator('[data-af-queue]').click();released()
    assert 'Media changed' in page.locator('[data-af-error]').inner_text()
    assert not page.evaluate('applied.length')
    assert page.locator('[data-af-queue]').is_enabled(), 'Submission failure lost approvals'
    page.evaluate('stale=false')
    page.locator('[data-af-queue]').click();released()
    assert page.locator('#subtitle-autofix-review-dialog').is_hidden()
    assert page.evaluate('applied.length')==1
    assert page.evaluate('applied[0].streams.map(item=>item.id)')==['first']
    assert page.evaluate('events[0].path')=='/media/Movie.mkv'

    page.evaluate('applied=[];events=[];polls=0')
    open_review(tv=True)
    assert 'S01E01' in page.locator('[data-af-media]').inner_text()
    page.locator('[data-af-approve]').click();released()
    page.locator('[data-af-approve]').click();released()
    page.locator('[data-af-apply]').click()
    page.wait_for_function("document.querySelector('[data-af-stage]').textContent==='Completed'");released()
    assert page.evaluate('applied.length')==1 and len(page.evaluate('applied[0].streams'))==2
    assert page.evaluate('events.map(item=>item.name)')==['media-properties-queued','media-properties-applied']
    assert not page.locator('[data-af-apply]').count(), 'Completed operation was offered twice'
    page.locator('[data-af-close]').click();released()
    # No matching rules, cancel before submission, all rejected and failed task.
    page.evaluate('rulesAvailable=false;applied=[]')
    page.locator('#open').click();released()
    assert 'No enabled rule matches' in page.locator('[data-af-body]').inner_text()
    assert not page.locator('[data-af-prepare]').count()
    page.locator('[data-af-close]').click();released()
    page.evaluate('rulesAvailable=true')
    open_review()
    page.locator('[data-af-reject]').click();released()
    page.locator('[data-af-reject]').click();released()
    assert not page.locator('[data-af-apply]').count()
    assert not page.locator('[data-af-queue]').count()
    page.locator('[data-af-close]').click();released()
    assert not page.evaluate('applied.length')
    open_review()
    page.locator('[data-af-close]').click();released()
    assert not page.evaluate('applied.length')
    open_review()
    page.locator('[data-af-approve]').click();released()
    page.locator('[data-af-reject]').click();released()
    page.evaluate('failedTask=true;polls=0')
    page.locator('[data-af-apply]').click();released()
    assert 'Verification refused' in page.locator('[data-af-error]').inner_text()
    assert not page.locator('[data-af-apply]').count(), 'Failed committed task resubmission was offered without review'
    page.locator('[data-af-close]').click();released()
    # Encoding-only consent shares the dialog, with no rule dependency or
    # misleading replacement count. Byte change, not a fabricated text fix.
    page.evaluate('failedTask=false;applied=[];rulesAvailable=false;polls=0')
    page.locator('#open-encoding').click();released()
    assert 'Subtitle UTF-8 quickfix' in page.locator('#autofix-review-title').inner_text()
    assert not page.locator('[data-af-rule]').count()
    assert 'No media is changed' in page.locator('[data-af-body]').inner_text()
    page.locator('[data-af-prepare]').click();released()
    assert 'Windows-1252 (inferred) → UTF-8 · text unchanged' in page.locator('[data-af-body]').inner_text()
    page.locator('[data-af-compare]').check()
    assert page.locator('[data-af-fixed]').inner_text()==page.locator('[data-af-original]').inner_text()
    page.locator('[data-af-approve]').click();released()
    page.locator('[data-af-reject]').click();released()
    assert 'Normalize to UTF-8' in page.locator('[data-af-body]').inner_text()
    assert not page.evaluate('applied.length')
    page.locator('[data-af-queue]').click();released()
    assert page.evaluate('applied.length')==1
    assert page.evaluate('calls.some(c=>c.url.endsWith("/encoding/prepare") && !c.payload.rule_id)')
    assert not errors, errors
    browser.close()
print('PASS: rule selection, full escaped text, per-stream approval/rejection, final-only queue/apply, stale and failed errors retained, source events, no duplicate execution, cancellation, no-rule state, busy progress and mobile/desktop fit')
