"""Autofix setup navigation/editing regression, with in-browser fake storage."""
import json
import os
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
fixture = '''<!doctype html><meta charset="utf-8"><style>
  body{margin:12px;background:#111820;color:#e7edf5;font-family:Arial,sans-serif}
  .hidden{display:none!important}button,input,select,textarea{font:inherit;border:1px solid #485365;border-radius:6px;padding:7px;background:#202b3a;color:inherit}
  button{cursor:pointer}button:disabled{opacity:.5}button.primary{background:#22517a}button.active{border-color:#62b0ff}
  .setup-tabs,.tasks-tabs{display:none}article{margin:0}nav{margin-bottom:15px}
  </style><nav><button type="button" data-page="setup" onclick="document.querySelector('#setup').classList.remove('hidden')">Setup</button></nav>
  <section id="setup" class="hidden"><div class="page-title"><h2>Setup</h2><p>Settings</p></div>
  <div class="setup-tabs"><button data-setup-tab="plex">Plex</button><button data-setup-tab="import">Import</button><button data-setup-tab="queue">Tasks</button></div>
  <div class="setup-tab-panels"><section data-setup-panel="plex">Plex settings</section><section data-setup-panel="import" class="hidden">Import settings</section>
  <section data-setup-panel="queue" class="hidden"><div class="tasks-shell"><div class="tasks-tabs"><button data-tasks-tab="queue">Task queue</button><button data-tasks-tab="indexes">Indexes</button></div><section data-tasks-panel="queue">Queue fixture</section><section data-tasks-panel="indexes" class="hidden">Index fixture</section></div></section></div></section>'''

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True, executable_path=os.getenv('PLAYWRIGHT_CHROMIUM_EXECUTABLE'))
    page = browser.new_page(viewport={'width': 1400, 'height': 1000})
    errors, dialogs = [], []
    page.on('pageerror', lambda error: errors.append(str(error)))

    def accept_dialog(dialog):
        dialogs.append(dialog.message)
        dialog.accept()

    page.on('dialog', accept_dialog)
    page.route('http://autofix.test/**', lambda route: route.fulfill(status=200, content_type='text/html', body=fixture))
    page.goto('http://autofix.test/')
    page.evaluate('''() => {
      window.requests=[];window.savedRules=[];window.nextId=1;window.failSave=false;
      const copy=value=>JSON.parse(JSON.stringify(value));
      window.api=async(url,options={})=>{
        const method=options.method||'GET',payload=options.body?JSON.parse(options.body):null;
        window.requests.push({url,method,payload});
        if(url==='/api/settings/subtitle-autofix/rules'&&method==='GET')
          return {rules:copy(window.savedRules),languages:['pt-BR','pt-PT','pt','en','en-US','fr'],limits:{languages:3,replacements:500,preview_characters:32768}};
        if(url==='/api/settings/subtitle-autofix/preview'){
          let text=payload.text;const counts=[];
          payload.rule.replacements.forEach(row=>{const count=text.split(row.from).length-1;counts.push(count);text=text.split(row.from).join(row.to)});
          return {text,changed:text!==payload.text,replacement_count:counts.reduce((a,b)=>a+b,0),counts};
        }
        if(url.startsWith('/api/settings/subtitle-autofix/rules')){
          if(window.failSave)throw Error('This rule changed in another screen. Your draft is still here; refresh before saving again.');
          if(method==='POST'){const rule={...payload,id:String(window.nextId++).padStart(32,'0'),revision:1,created_at:'2026-10-06T15:00:00Z',updated_at:'2026-10-06T15:00:00Z'};window.savedRules.push(rule);return copy(rule)}
          const id=url.split('/').pop().split('?')[0],position=window.savedRules.findIndex(rule=>rule.id===id);
          if(method==='PUT'){const rule={...window.savedRules[position],...payload,revision:window.savedRules[position].revision+1};window.savedRules[position]=rule;return copy(rule)}
          if(method==='DELETE'){window.savedRules.splice(position,1);return {deleted:id}}
        }
        return {};
      };
      document.querySelectorAll('[data-setup-tab]').forEach(button=>button.onclick=()=>document.querySelectorAll('[data-setup-panel]').forEach(panel=>panel.classList.toggle('hidden',panel.dataset.setupPanel!==button.dataset.setupTab)));
    }''')
    page.add_style_tag(content=(ROOT/'app/static/setup-workspace.css').read_text())
    page.add_style_tag(content=(ROOT/'app/static/subtitle-autofix.css').read_text())
    page.add_script_tag(content=(ROOT/'app/static/v102-setup.js').read_text())
    page.add_script_tag(content=(ROOT/'app/static/subtitle-autofix.js').read_text())
    assert not page.evaluate("requests.some(item=>item.url.includes('subtitle-autofix'))"), 'Hidden setup eagerly loaded rules'
    page.get_by_role('button', name='Setup', exact=True).click()
    page.get_by_role('tab', name='Editing', exact=True).click()
    page.get_by_role('tab', name='Subtitle autofix', exact=True).click()
    page.wait_for_function("requests.filter(item=>item.url==='/api/settings/subtitle-autofix/rules').length===1")
    assert page.locator('[data-setup-panel="subtitle-autofix"]').is_visible()
    assert page.locator('[data-setup-panel="import"]').is_hidden(), 'Import screen leaked into autofix'
    assert page.get_by_text('No autofix rules yet', exact=True).is_visible()
    page.locator('[data-autofix-add]').click()
    page.locator('[data-autofix-name]').fill('Portuguese corrections')
    page.locator('[data-autofix-description]').fill('Only reviewed substitutions')
    page.get_by_label('Language 1', exact=True).select_option('pt-BR')
    page.locator('[data-autofix-add-language]').click()
    page.get_by_label('Language 2', exact=True).select_option('pt-PT')
    page.locator('[data-autofix-add-language]').click()
    page.get_by_label('Language 3', exact=True).select_option('en')
    assert page.locator('[data-autofix-add-language]').is_disabled()
    assert page.locator('[data-autofix-language]').count() == 3
    page.get_by_label('From 1', exact=True).fill('Ã£')
    page.get_by_label('To 1', exact=True).fill('ã')
    page.locator('[data-autofix-add-replacement]').click()
    page.get_by_label('From 2', exact=True).fill('[ad]')
    page.locator('[data-autofix-add-replacement]').click()
    page.get_by_label('From 3', exact=True).fill('teh')
    page.get_by_label('To 3', exact=True).fill('the')
    page.get_by_label('Match 3', exact=True).select_option('word')
    page.locator('[data-autofix-move-up="2"]').click()
    assert page.get_by_label('From 2', exact=True).input_value() == 'teh'
    assert page.get_by_label('To 3', exact=True).input_value() == ''
    page.locator('[data-autofix-sample]').fill('NÃ£o [ad] teh')
    page.locator('[data-autofix-preview-button]').click()
    page.wait_for_function("document.querySelector('[data-autofix-result]').value==='Não  the'")
    assert '3 replacements' in page.locator('[data-autofix-preview-status]').inner_text()
    assert not page.evaluate('savedRules.length'), 'Sample preview saved a rule'
    page.locator('[data-autofix-save]').click()
    page.wait_for_function('savedRules.length===1')
    saved = page.evaluate('savedRules[0]')
    assert saved['languages'] == ['pt-BR', 'pt-PT', 'en']
    assert saved['replacements'] == [
        {'from': 'Ã£', 'to': 'ã', 'match': 'literal'},
        {'from': 'teh', 'to': 'the', 'match': 'word'},
        {'from': '[ad]', 'to': '', 'match': 'literal'},
    ]
    assert page.locator('[data-autofix-save]').is_disabled(), 'Saved fields remained pending'
    assert 'no pending changes' in page.locator('[data-autofix-draft-state]').inner_text()
    dialogs.clear()
    page.locator('[data-autofix-cancel]').click()
    assert not dialogs, 'Closing a saved rule incorrectly asked to discard'
    page.locator('[data-autofix-edit]').click()
    page.locator('[data-autofix-name]').fill('Portuguese reviewed corrections')
    page.evaluate('window.failSave=true')
    page.locator('[data-autofix-save]').click()
    page.wait_for_function("document.querySelector('[data-autofix-editor-status]').textContent.includes('another screen')")
    assert page.locator('[data-autofix-name]').input_value() == 'Portuguese reviewed corrections'
    assert page.locator('[data-autofix-save]').is_enabled()
    # Navigating away/returning keeps the unsaved draft; do not reload settings
    # or rewrite it after a hidden panel becomes visible again.
    page.get_by_role('tab', name='Subtitle color', exact=True).click()
    page.get_by_role('tab', name='Subtitle autofix', exact=True).click()
    assert page.locator('[data-autofix-name]').input_value() == 'Portuguese reviewed corrections'
    assert page.evaluate("requests.filter(item=>item.url==='/api/settings/subtitle-autofix/rules'&&item.method==='GET').length") == 1
    page.evaluate('window.failSave=false')
    page.locator('[data-autofix-save]').click()
    page.wait_for_function('savedRules[0].revision===2')
    assert page.locator('[data-autofix-save]').is_disabled()
    page.screenshot(path='/tmp/vse-subtitle-autofix-wide.png', full_page=True)
    for width in (900, 360):
        page.set_viewport_size({'width': width, 'height': 900})
        assert page.evaluate("document.querySelector('[data-autofix-workspace]').scrollWidth<=document.querySelector('[data-autofix-workspace]').clientWidth+1"), f'Autofix controls overflow at {width}px'
        assert page.locator('[data-autofix-cancel]').is_visible()
    page.screenshot(path='/tmp/vse-subtitle-autofix-mobile.png', full_page=True)
    page.locator('[data-autofix-remove-language="2"]').click()
    page.locator('[data-autofix-remove-replacement="2"]').click()
    page.locator('[data-autofix-save]').click()
    page.wait_for_function('savedRules[0].languages.length===2&&savedRules[0].replacements.length===2')
    page.locator('[data-autofix-cancel]').click()
    page.locator('[data-autofix-toggle]').click()
    page.wait_for_function('!savedRules[0].enabled')
    assert 'Disabled' in page.locator('[data-autofix-rules]').inner_text()
    page.locator('[data-autofix-toggle]').click()
    page.wait_for_function('savedRules[0].enabled')
    page.locator('[data-autofix-delete]').click()
    page.wait_for_function('savedRules.length===0')
    assert page.get_by_text('No autofix rules yet', exact=True).is_visible()
    writes = page.evaluate("requests.filter(item=>item.method!=='GET')")
    assert all(item['url'].startswith('/api/settings/subtitle-autofix/') for item in writes), json.dumps(writes)
    assert not errors, errors
    browser.close()

print('PASS: actual Setup navigation, lazy loading, 3-language limit, replacement ordering/removal/preview, explicit saves, no-pending reset, stale draft preservation, enable/delete, 900/360px layout; no media/job writes')
