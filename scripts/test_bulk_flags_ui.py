"""Run the production draft projection and tri-state handlers in Chromium."""
from pathlib import Path
from playwright.sync_api import sync_playwright

root=Path(__file__).resolve().parents[1]
tv=(root/'app/static/v79-season-filters.js').read_text()
projection=tv[tv.index('  function projectDraft(paths,'):tv.index('  window.projectTvEpisodeEdit=')]
with sync_playwright() as p:
    browser=p.chromium.launch(headless=True)
    page=browser.new_page()
    result=page.evaluate('''source=>{
      let records=[{path:'a',stream_type:'subtitle',type_index:0,language:'pt',region:'',track_name:'',is_forced:1},
                   {path:'a',stream_type:'subtitle',type_index:1,language:'en',region:'',track_name:'',is_forced:1}];
      const draftBaseline=structuredClone(records);
      const streamMatchesFilters=(item,filters)=>item.language===filters.language;
      const renderEpisodes=()=>{},refreshVirtualFilters=()=>{};
      eval(source+`;window.projectFixture=projectDraft`);
      const paths=[...window.projectFixture(['a'],{language:'pt'},{language:'',region:'',track_name:''},false,{forced_action:'clear'})];
      return {paths,records};
    }''',projection)
    assert result['paths']==['a']
    assert result['records'][0]['is_forced'] is False
    assert result['records'][1]['is_forced']==1
    for field_class in ('season-bulk-value', 'movie-header-bulk-value'):
        page.set_content(f'<input class="{field_class}" name="track_name" data-saved-field="title_subtitle">')
        source=(root/'app/static/v36-inline-combobox.js').read_text().split('const inlineComboboxObserver')[0]
        # Load the production combobox with no saved names: clear still exists.
        page.evaluate('''source=>{
          window.v8Saved={}; window.openSavedValueMenu=null;
          window.$=selector=>document.querySelector(selector);
          eval(source);
          const input=document.querySelector('input');
          installSavedValuePopups(document);input.click();
        }''',source)
        assert page.locator('[role=option]').first.inner_text()=='Clear track name (blank)'
        page.locator('[role=option]').first.click()
        assert page.locator('input').input_value()==''
        assert page.locator('input').get_attribute('data-dirty')=='true'
    result=page.evaluate('''source=>{
      let records=[{path:'a',stream_type:'subtitle',type_index:0,language:'pt',region:'',track_name:'Old'},
                   {path:'a',stream_type:'subtitle',type_index:1,language:'en',region:'',track_name:'Keep'}];
      const draftBaseline=structuredClone(records);
      const streamMatchesFilters=(item,filters)=>item.language===filters.language;
      const renderEpisodes=()=>{},refreshVirtualFilters=()=>{};
      eval(source+`;window.projectFixture=projectDraft`);
      window.projectFixture(['a'],{language:'pt'},{language:'',region:'',track_name:''},false,{changed_fields:['track_name']});
      return records;
    }''',projection)
    assert [row['track_name'] for row in result]==['','Keep']
    for file in ('v79-season-filters.js','v82-movie-streams.js'):
        source=(root/'app/static'/file).read_text()
        handler=next(line.strip() for line in source.splitlines() if 'for(const tri of editContent.querySelectorAll' in line)
        page.set_content('<div id="editor"><label><span>Forced <em data-tri-state>—</em></span><input type="checkbox" name="forced" data-action="forced"></label><button>Apply</button></div>')
        page.evaluate('''handler=>{const editContent=document.querySelector('#editor'),apply=editContent.querySelector('button'),integrate=null,remove=null;eval(handler)}''',handler)
        for state,checked,indeterminate,disabled in [('set',True,False,False),('clear',False,True,False),('unchanged',False,False,True)]:
            page.locator('input').click()
            actual=page.locator('input').evaluate('(e)=>({state:e.dataset.state,checked:e.checked,indeterminate:e.indeterminate})')
            assert actual==dict(state=state,checked=checked,indeterminate=indeterminate),(file,actual)
            assert page.locator('button').is_disabled()==disabled
    browser.close()
print('PASS: flag-only draft change, unmatched stream preserved, three-state movie/TV controls and no-change button state')
