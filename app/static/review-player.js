/* Media Review: current-position switching, bounded server sessions and explicit approval. */
(function () {
  const old = document.querySelector('#media-review-dialog');
  if (!old) return;
  const dialog = document.createElement('dialog');
  dialog.id = 'media-review-dialog';
  dialog.className = 'review-player';
  dialog.innerHTML = `<header class="rp-header"><div><h2>Review media</h2><p data-title></p><div class="rp-badges" aria-live="polite"><span data-plan>Ready</span><span data-status></span></div></div><button type="button" data-close>Close</button></header>
    <section class="rp-toolbar"><label>Audio<select data-audio aria-label="Audio stream"></select></label><label>Subtitles<select data-subtitle aria-label="Subtitle stream"></select></label><div class="rp-outputs" aria-label="Review outputs">${['audio','video','subtitle'].map(kind=>`<button type="button" data-output="${kind}" aria-pressed="true" class="active">${kind[0].toUpperCase()+kind.slice(1)}</button>`).join('')}</div><button type="button" data-play>Play</button></section>
    <div class="rp-stage" data-stage></div>
    <section class="rp-transport"><input data-timeline type="range" min="0" max="1" step="0.1" value="0" aria-label="Media timeline"><div class="rp-transport-row"><div class="rp-navigation"><button type="button" data-start title="Go to beginning">|◀</button><button type="button" data-back title="Back 10 seconds">−10s</button><button type="button" data-toggle>Play</button><button type="button" data-stop>Stop</button><button type="button" data-next title="Forward 10 seconds">+10s</button><button type="button" data-end title="Go to ending">▶|</button></div><label class="rp-volume">Volume<input data-volume type="range" min="0" max="1" step="0.05" value="1" aria-label="Volume"></label><select data-speed aria-label="Playback speed"><option value="0.75">0.75×</option><option value="1" selected>1×</option><option value="1.25">1.25×</option><option value="1.5">1.5×</option><option value="2">2×</option></select><button type="button" data-fullscreen>Fullscreen</button><time data-time>0:00 / —</time></div></section>
    <details class="rp-compatibility"><summary>Compatibility & staged audio / subtitles <span data-stage-count></span></summary><div class="rp-compat-actions"><button type="button" data-retry>Try AAC stereo playback</button><button type="button" data-convert>Create AAC stereo version</button><small>Playback conversion is temporary. Permanent changes require review and approval; video is never re-encoded by audio approval.</small></div><div data-audio-stages></div><div data-subtitle-stages></div></details>`;
  old.replaceWith(dialog);
  const el = name => dialog.querySelector(`[data-${name}]`);
  let path='', title='', generation=0, session=null, pending=null, hls=null, media=null, total=0, offset=0, desired=0, forceAAC=false, timer=null, pollTimer=null, playing=false, subtitleGeneration=0, blob=null, refreshEditorAfterApproval=false;
  const subtitleCache = new Map();
  let playIntent=true;
  const resumeState=()=>pending?playIntent:media?!media.paused:true;
  const graphical = /^(hdmv_pgs_subtitle|pgs|dvd_subtitle|dvb_subtitle|vobsub|xsub)$/i;
  const active = kind => dialog.querySelector(`[data-output="${kind}"]`).classList.contains('active');
  const mode = () => active('video') ? (active('audio')?'av':'video') : active('audio')?'audio':'subtitle';
  const format = value => {const s=Math.max(0,Math.floor(Number(value)||0));return `${s>=3600?Math.floor(s/3600)+':':''}${String(Math.floor(s/60)%60).padStart(s>=3600?2:1,'0')}:${String(s%60).padStart(2,'0')}`;};
  const position = () => media ? Math.min(total,offset+(Number(media.currentTime)||0)) : desired;
  const status = (text,error=false) => {el('status').textContent=text;el('status').classList.toggle('error',error);};
  async function request(url, options={}) {
    const response=await fetch(url,{signal:AbortSignal.timeout(60000),...options,headers:{'Content-Type':'application/json',...(options.headers||{})}});
    const value=await response.json().catch(()=>({}));
    if(!response.ok)throw new Error(typeof value.detail==='string'?value.detail:`Request failed (${response.status})`);
    return value;
  }
  const removeSession = id => {if(id)fetch(`/api/review/playback/${id}`,{method:'DELETE',keepalive:true}).catch(()=>{});};
  function stopPlayer() {
    clearInterval(timer);timer=null;
    removeSession(session);session=null;
    if(hls){hls.destroy();hls=null;}
    if(media){media.pause();media.removeAttribute('src');media.load();media=null;}
    if(blob){URL.revokeObjectURL(blob);blob=null;}
  }
  function close() {generation++;subtitleGeneration++;removeSession(pending);pending=null;stopPlayer();clearInterval(pollTimer);dialog.close();}
  el('close').onclick=close;
  dialog.addEventListener('close',()=>{
    if(!refreshEditorAfterApproval)return;
    refreshEditorAfterApproval=false;
    if(state.selectedPath!==path||!document.querySelector('#stream-dialog')?.open)return;
    (window.streamEditorRefreshPaths??=new Set()).add(path);
    openEditor(path,title).catch(error=>status(`Could not refresh stream properties: ${error.message}`,true));
  });
  dialog.addEventListener('cancel',e=>{e.preventDefault();close();});
  window.addEventListener('pagehide',()=>{removeSession(session);removeSession(pending);});
  function updateTransport(){
    const time=position();if(!el('timeline').matches(':active'))el('timeline').value=String(time);
    el('timeline').max=String(total||1);el('time').textContent=`${format(time)} / ${total?format(total):'—'}`;
    el('toggle').textContent=media&&!media.paused?'Pause':'Play';
  }
  function streamRows(kind){return [...document.querySelectorAll('#stream-content .stream-row')].filter(row=>row.dataset.codecType===kind&&!row.querySelector('[name=remove]')?.checked);}
  function rowLabel(row){return [row.dataset.external==='true'?'External subtitle':`${row.dataset.codecType==='audio'?'Audio':'Subtitle'} ${Number(row.dataset.typeIndex)+1}`,
    [row.querySelector('[name=language]')?.value,row.querySelector('[name=region]')?.value].filter(Boolean).join('-'),row.querySelector('[name=title]')?.value,row.dataset.codec].filter(Boolean).join(' · ');}
  function appendRow(select,row){const option=new Option(rowLabel(row),row.dataset.key);option.dataset.index=row.dataset.typeIndex;option.dataset.codec=row.dataset.codec||'';option.dataset.source=row.dataset.external==='true'?'external':'embedded';option.dataset.path=row.dataset.path||'';if(graphical.test(option.dataset.codec)){option.textContent+=' — image overlay unsupported';}select.add(option);}
  const chosen = name => el(name).selectedOptions[0];
  const canCreateAacVersion = () => {
    const option=chosen('audio');
    return active('audio') && Boolean(option?.value) && !option.dataset.stage && option.dataset.codec.toLowerCase() !== 'aac';
  };
  const updateAudioConversionOffer = () => {el('convert').hidden=!canCreateAacVersion();};
  function subtitleQuery(){const option=chosen('subtitle');if(!active('subtitle')||!option?.value)return null;return new URLSearchParams({path,source:option.dataset.source,index:option.dataset.index||'0',external_path:option.dataset.path||''});}
  async function subtitleText(){const query=subtitleQuery();if(!query)return '';const key=query.toString();if(subtitleCache.has(key))return subtitleCache.get(key);const response=await fetch('/api/review/subtitle?'+query);if(!response.ok){const error=await response.json().catch(()=>({}));throw new Error(error.detail||'Subtitle could not be loaded');}const text=await response.text();subtitleCache.set(key,text);return text;}
  async function switchSubtitle(){
    const own=++subtitleGeneration, target=media;
    if(!target||target.tagName!=='VIDEO')return;
    target.querySelectorAll('track').forEach(track=>track.remove());
    if(blob){URL.revokeObjectURL(blob);blob=null;}
    if(!subtitleQuery())return;
    status('Loading subtitle overlay…');
    try{
      const text=await subtitleText();if(own!==subtitleGeneration||target!==media)return;
      blob=URL.createObjectURL(new Blob([text],{type:'text/vtt'}));
      const track=document.createElement('track');track.kind='subtitles';track.label=chosen('subtitle').textContent;track.default=true;track.src=blob;
      track.onload=()=>{if(target!==media)return;const cues=[...(track.track.cues||[])];for(const cue of cues){if(cue.endTime<=offset){track.track.removeCue(cue);continue;}cue.startTime=Math.max(0,cue.startTime-offset);cue.endTime=Math.max(0,cue.endTime-offset);}track.track.mode='showing';status('Subtitle changed — playback unchanged');};
      target.append(track);track.track.mode='showing';
    }catch(error){if(own===subtitleGeneration)status(error.message,true);}
  }
  async function showText(){
    const own=generation;
    stopPlayer();status('Loading subtitle text…');
    try{const text=await subtitleText();if(own!==generation||!dialog.open)return;el('stage').innerHTML='<div class="rp-text"><pre></pre><button type="button" data-clean-html hidden>Queue HTML removal</button></div>';el('stage').querySelector('pre').textContent=text||'Select a text subtitle.';
      const option=chosen('subtitle'),button=el('stage').querySelector('button');button.hidden=!/<[^>]+>/.test(text)||!option?.value||option.dataset.source==='staged';
      button.onclick=async()=>{button.disabled=true;try{await request('/api/v68/subtitle-html-cleanup',{method:'POST',body:JSON.stringify({path,type_index:option.dataset.source==='embedded'?Number(option.dataset.index):null,external_path:option.dataset.source==='external'?option.dataset.path:null})});toast('HTML cleanup queued');close();}catch(error){status(error.message,true);button.disabled=false;}};status('Subtitle text — original media unchanged');
    }catch(error){status(error.message,true);}
  }
  let hlsPromise;
  function loadHls(){if(window.Hls)return Promise.resolve(window.Hls);return hlsPromise ||= new Promise((resolve,reject)=>{const script=document.createElement('script');script.src='/assets/review-hls.js';script.onload=()=>resolve(window.Hls);script.onerror=()=>{hlsPromise=null;reject(new Error('The local playback library could not be loaded'));};document.head.append(script);});}
  async function load(start=position(),autoplay=true){
    playIntent=autoplay;
    const own=++generation;removeSession(pending);pending=null;
    desired=Math.max(0,Math.min(start,total?total-.1:start));
    if(mode()==='subtitle')return showText();
    const audio=chosen('audio');if(active('audio')&&!audio?.value){status('Select an audio stream or turn Audio off',true);return;}
    if(media)media.pause();
    const began=performance.now();
    const elapsed=()=>`${Math.floor((performance.now()-began)/1000)}s elapsed`;
    status('Step 1 of 4 · Checking file fingerprint and stream metadata…');
    let id,checkTimer;
    try{
      // getRandomValues also works on HTTP LAN origins; randomUUID requires HTTPS.
      id=Array.from(crypto.getRandomValues(new Uint8Array(16)),b=>b.toString(16).padStart(2,'0')).join('');pending=id;
      checkTimer=setInterval(()=>{if(own===generation)status(`Step 1 of 4 · Checking file fingerprint and stream metadata · ${elapsed()}${performance.now()-began>10000?' · Storage or metadata lookup is taking longer than usual':''}`);},1000);
      const result=await request('/api/review/playback',{method:'POST',body:JSON.stringify({path,session_id:id,mode:mode(),audio_index:audio?.dataset.index?Number(audio.dataset.index):0,audio_stage:audio?.dataset.stage||null,start:desired,force_aac:forceAAC})});
      clearInterval(checkTimer);
      if(own!==generation){removeSession(id);return;}
      total=result.duration;const plan=result.plan;
      const badge=plan.video_copy?(plan.audio_copy?'Stream copy':'AAC stereo playback'):'Video compatibility conversion';el('plan').textContent=badge;el('plan').title=plan.video_copy?'Video is not re-encoded. Playback uses a bounded temporary buffer.':'This codec needs video conversion for playback only. The media is unchanged.';
      // Browser hints supplement server checks, never replace runtime error handling.
      if(plan.audio_copy&&active('audio')&&navigator.mediaCapabilities?.decodingInfo){try{const support=await navigator.mediaCapabilities.decodingInfo({type:'media-source',audio:{contentType:'audio/mp4; codecs="mp4a.40.2"',channels:String(plan.audio_channels||2),bitrate:192000,samplerate:Number(plan.audio_sample_rate)||48000}});if(!support.supported&&!forceAAC){removeSession(id);forceAAC=true;return load(desired,autoplay);}}catch(_){} }
      let ready=false;
      for(let count=0;count<240;count++){
        if(own!==generation){removeSession(id);return;}
        const progress=await request(`/api/review/playback/${id}/status`);
        if(progress.error)throw new Error(progress.error);
        if(progress.ready){ready=true;break;}
        status(`Step ${progress.phase==='waiting'?2:3} of 4 · ${progress.message||'Preparing first playable segment'} · ${progress.buffered_seconds||0}s processed · ${elapsed()}`);
        await new Promise(resolve=>setTimeout(resolve,500));
      }
      if(!ready)throw new Error('Playback preparation timed out. Try AAC stereo or choose another stream.');
      if(own!==generation){removeSession(id);return;}
      stopPlayer();session=id;pending=null;offset=result.start;
      el('stage').innerHTML=mode()==='audio'?'<audio preload="auto"></audio>':'<video playsinline preload="auto"></video>';
      media=el('stage').firstElementChild;const target=media;media.volume=Number(el('volume').value);media.playbackRate=Number(el('speed').value);
      const onReady=()=>{if(media!==target)return;status('Step 4 of 4 complete · Playback ready');if(playIntent)target.play().catch(()=>status('Press Play to start'));switchSubtitle();updateTransport();};
      media.addEventListener('loadedmetadata',onReady,{once:true});
      media.addEventListener('timeupdate',updateTransport);media.addEventListener('play',()=>{playing=true;updateTransport();});media.addEventListener('pause',()=>{playing=false;updateTransport();});
      media.addEventListener('waiting',()=>status('Buffering selected streams…'));media.addEventListener('playing',()=>status('Playing'));
      media.addEventListener('error',()=>status('Browser could not decode this stream. Use “Try AAC stereo playback” or choose another audio.',true));
      const Hls=await loadHls();
      if(own!==generation)return;
      if(Hls?.isSupported()){
        hls=new Hls({enableWorker:true,lowLatencyMode:false,startPosition:0,liveSyncDuration:86400,liveMaxLatencyDuration:172800,maxBufferLength:24,backBufferLength:12});
        hls.on(Hls.Events.ERROR,(_,data)=>{if(data.fatal&&own===generation)status(`Playback error (${data.details}). Try AAC stereo or seek to restart the buffer.`,true);});
        hls.loadSource(result.manifest_url);hls.attachMedia(media);
      }else if(media.canPlayType('application/vnd.apple.mpegurl'))media.src=result.manifest_url;
      else throw new Error('This browser does not support HLS playback');
      timer=setInterval(()=>{if(session&&media)request(`/api/review/playback/${session}/heartbeat`,{method:'POST',body:JSON.stringify({position:media.currentTime||0})}).catch(()=>{});},3000);
      status(`Step 4 of 4 · Loading buffered video/audio into browser · ${elapsed()}`);updateTransport();
    }catch(error){if(own===generation){removeSession(id);pending=null;status(error.message,true);}}
    finally{clearInterval(checkTimer);}
  }
  function seek(value){const next=Math.max(0,Math.min(total-.1,Number(value)||0));const relative=next-offset;if(media&&!pending&&relative>=0){for(let i=0;i<media.buffered.length;i++)if(relative>=media.buffered.start(i)&&relative<media.buffered.end(i)){media.currentTime=relative;updateTransport();return;}}load(next,resumeState());}
  el('timeline').onchange=()=>seek(el('timeline').value);
  el('timeline').oninput=()=>{el('time').textContent=`${format(el('timeline').value)} / ${format(total)}`;};
  el('start').onclick=()=>seek(0);el('back').onclick=()=>seek(position()-10);el('next').onclick=()=>seek(position()+10);el('end').onclick=()=>seek(total-2);
  function toggle(){if(pending){playIntent=!playIntent;status(playIntent?'Will play when ready':'Will remain paused when ready');return;}if(!media)return load(desired,true);if(media.paused)media.play().catch(error=>status(error.message,true));else media.pause();}
  el('toggle').onclick=toggle;el('play').onclick=toggle;el('stop').onclick=()=>{desired=0;generation++;removeSession(pending);pending=null;stopPlayer();el('stage').textContent='Stopped — press Play to start again';updateTransport();};
  el('volume').oninput=()=>{if(media)media.volume=Number(el('volume').value);};el('speed').onchange=()=>{if(media)media.playbackRate=Number(el('speed').value);};
  el('fullscreen').onclick=()=>{if(document.fullscreenElement)document.exitFullscreen();else dialog.requestFullscreen?.();};
  el('audio').onchange=()=>{el('audio').title=chosen('audio')?.textContent||'';updateAudioConversionOffer();forceAAC=false;load(pending?desired:position(),resumeState());};
  el('subtitle').onchange=()=>{el('subtitle').title=chosen('subtitle')?.textContent||'';mode()==='subtitle'?showText():switchSubtitle();};
  dialog.querySelectorAll('[data-output]').forEach(button=>button.onclick=()=>{const count=dialog.querySelectorAll('[data-output].active').length;if(button.classList.contains('active')&&count===1)return;const before=mode(),time=pending?desired:position(),wasPlaying=resumeState();button.classList.toggle('active');button.setAttribute('aria-pressed',String(button.classList.contains('active')));updateAudioConversionOffer();if(mode()===before&&button.dataset.output==='subtitle')switchSubtitle();else load(time,wasPlaying);});
  el('retry').onclick=()=>{forceAAC=true;load(position(),true);};
  el('convert').onclick=async()=>{const option=chosen('audio');if(!canCreateAacVersion()){status('Select an original non-AAC audio stream to convert',true);return;}const button=el('convert');button.disabled=true;status('Queueing AAC preparation…');try{const result=await request('/api/review/audio',{method:'POST',body:JSON.stringify({path,audio_index:Number(option.dataset.index)})});status(`AAC preparation queued${result.task_id?' · job #'+result.task_id:''}; original media is unchanged`);await refreshStages();}catch(error){status(error.message,true);}finally{button.disabled=false;}};
  function textPopup(text,heading){const popup=document.createElement('dialog');popup.className='rp-text-popup';popup.innerHTML='<h3></h3><pre></pre><button type="button">Close</button>';popup.querySelector('h3').textContent=heading;popup.querySelector('pre').textContent=text;popup.querySelector('button').onclick=()=>popup.close();popup.onclose=()=>popup.remove();document.body.append(popup);popup.showModal();}
  function actionButton(label,action){const button=document.createElement('button');button.type='button';button.textContent=label;button.onclick=async()=>{button.disabled=true;try{await action();}catch(error){status(error.message,true);}finally{button.disabled=false;}};return button;}
  async function refreshStages(){
    if(dialog.querySelector('.rp-stage-card input:focus'))return;
    const ownPath=path;
    try{
      const [audioData,subtitleData]=await Promise.all([request('/api/review/audio?path='+encodeURIComponent(path)),request('/api/v83/media-review/staged-subtitles?path='+encodeURIComponent(path))]);
      if(path!==ownPath||!dialog.open)return;
      const previousAudio=el('audio').value,previousSubtitle=el('subtitle').value;
      el('audio').querySelectorAll('[data-stage]').forEach(option=>option.remove());el('subtitle').querySelectorAll('[data-source=staged]').forEach(option=>option.remove());
      el('audio-stages').replaceChildren();el('subtitle-stages').replaceChildren();
      for(const item of audioData.items){
        if(['ready','draft','queued'].includes(item.status)){const option=new Option(`Audio ${item.audio_index+1} · AAC stereo · ${item.status==='ready'?'awaiting approval':item.status}`,'stage:'+item.id);option.dataset.stage=item.id;option.dataset.index=item.audio_index;el('audio').add(option);}
        const card=document.createElement('article');card.className='rp-stage-card';const name=document.createElement('span');name.textContent=`Audio ${item.audio_index+1} · AAC stereo · ${item.status}${item.task_id?' · job #'+item.task_id:''}${item.error?' · '+item.error:''}`;card.append(name);
        if(item.status==='ready'){
          for(const [label,action] of [['Approve: add track','add'],['Approve: replace original','replace']])card.append(actionButton(label,async()=>{
            if(action==='replace'&&!confirm('Replace the original audio with AAC stereo? This loses the original codec and surround channels. Add a track instead to retain them.'))return;
            const draft=window.tvShowEditSession?.status==='open'?window.tvShowEditSession.session_id:null;
            const result=await request(`/api/review/audio/${item.id}/approve`,{method:'POST',body:JSON.stringify({action,draft_session:draft})});
            if(result.draft){window.markTvEditDirty?.();status('Audio approval saved in the TV draft; files remain unchanged until Save.');}else status(`Audio integration queued · job #${result.task_id}`);
            await refreshStages();
          }));
        }
        if(['ready','failed'].includes(item.status)||(item.status==='queued'&&['failed','cancelled'].includes(item.task_status)))card.append(actionButton('Reject',async()=>{await request(`/api/review/audio/${item.id}`,{method:'DELETE'});await refreshStages();}));
        el('audio-stages').append(card);
      }
      for(const item of subtitleData.items||[]){
        const stagedPath=item.staged_path||item.path;const option=new Option(`Downloaded subtitle · ${item.language||'und'} · awaiting approval`,'staged:'+stagedPath);option.dataset.source='staged';option.dataset.path=stagedPath;el('subtitle').add(option);
        const card=document.createElement('article');card.className='rp-stage-card';const label=document.createElement('span');label.textContent=item.name||stagedPath.split('/').pop();card.append(label);
        card.append(actionButton('View text',async()=>{const response=await fetch('/api/v83/media-review/staged-subtitles/file?'+new URLSearchParams({path,staged_path:stagedPath}));if(!response.ok)throw new Error('Could not read staged subtitle');textPopup(await response.text(),label.textContent);}));
        const language=document.createElement('input');language.value=item.language||'';language.placeholder='Language';language.setAttribute('aria-label','Downloaded subtitle language');const region=document.createElement('input');region.value=item.region||'';region.placeholder='Region';region.setAttribute('aria-label','Downloaded subtitle region');card.append(language,region);
        card.append(actionButton('Approve subtitle',async()=>{if(window.tvShowEditSession?.status==='open')throw new Error('Finish the TV draft before placing downloaded subtitles; playback and text review remain available.');if(typeof queuedChangeCount==='function'&&queuedChangeCount()>0)throw new Error('Apply or discard pending stream-property changes before approving this subtitle.');await request('/api/v83/media-review/staged-subtitles/approve',{method:'POST',body:JSON.stringify({path,staged_path:stagedPath,language:language.value,region:region.value})});refreshEditorAfterApproval=true;status('Subtitle approved. Stream properties will refresh when you close Media Review.');await refreshStages();}));
        card.append(actionButton('Reject',async()=>{await request('/api/v83/media-review/staged-subtitles/reject',{method:'POST',body:JSON.stringify({path,staged_path:stagedPath})});await refreshStages();}));el('subtitle-stages').append(card);
      }
      if([...el('audio').options].some(o=>o.value===previousAudio))el('audio').value=previousAudio;
      if([...el('subtitle').options].some(o=>o.value===previousSubtitle))el('subtitle').value=previousSubtitle;
      updateAudioConversionOffer();
      el('stage-count').textContent=`(${audioData.items.length+(subtitleData.items||[]).length})`;
    }catch(error){if(path===ownPath)status(error.message,true);}
  }
  window.reviewPlayerOpen=async function(preferredRow=null){
    generation++;stopPlayer();removeSession(pending);pending=null;clearInterval(pollTimer);subtitleCache.clear();refreshEditorAfterApproval=false;
    path=state.selectedPath;title=document.querySelector('#selected-file').textContent;total=0;offset=0;desired=0;forceAAC=false;
    el('title').textContent=title;el('audio').replaceChildren();el('subtitle').replaceChildren(new Option('No subtitles',''));
    for(const row of streamRows('audio'))appendRow(el('audio'),row);for(const row of streamRows('subtitle'))appendRow(el('subtitle'),row);
    dialog.querySelectorAll('[data-output]').forEach(button=>{const enabled=!preferredRow||button.dataset.output===preferredRow.dataset.codecType;button.classList.toggle('active',enabled);button.setAttribute('aria-pressed',String(enabled));});
    const selectDefault=(kind,first,second)=>{const key=document.querySelector(`[name=${first}-${kind}]:checked`)?.value||document.querySelector(`[name=${second}-${kind}]:checked`)?.value;if([...el(kind).options].some(option=>option.value===key))el(kind).value=key;};
    selectDefault('audio','default','forced');selectDefault('subtitle','forced','default');if(preferredRow&&['audio','subtitle'].includes(preferredRow.dataset.codecType))el(preferredRow.dataset.codecType).value=preferredRow.dataset.key;
    if(!el('audio').options.length){const button=dialog.querySelector('[data-output=audio]');button.classList.remove('active');button.setAttribute('aria-pressed','false');}
    updateAudioConversionOffer();
    el('stage').textContent='Preparing review…';dialog.showModal();updateTransport();refreshStages();pollTimer=setInterval(refreshStages,6000);await load(0,true);
  };
  document.querySelector('#review-media').onclick=()=>window.reviewPlayerOpen();
  dialog.addEventListener('keydown',event=>{if(event.target.matches('input,select,textarea,button'))return;if(event.key===' '){event.preventDefault();toggle();}else if(event.key==='ArrowRight'){event.preventDefault();seek(position()+10);}else if(event.key==='ArrowLeft'){event.preventDefault();seek(position()-10);}});
})();
