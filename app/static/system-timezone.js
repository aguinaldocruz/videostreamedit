(function(){
  let zone=window.VSE_TIMEZONE||'America/Sao_Paulo';
  function parsed(value){
    if(value instanceof Date)return value;
    if(typeof value==='number')return new Date(value);
    let text=String(value||'').trim();
    if(!text)return null;
    // PostgreSQL text timestamps without an offset are stored as UTC. Never
    // let the browser reinterpret them in its own local timezone.
    text=text.replace(' ','T');
    if(/^\d{4}-\d\d-\d\dT/.test(text)&&!/(?:Z|[+-]\d\d(?::?\d\d)?)$/i.test(text))text+='Z';
    const date=new Date(text);
    return Number.isNaN(date.getTime())?null:date;
  }
  function format(value,mode){
    const date=parsed(value);if(!date)return value?String(value):'—';
    try{return new Intl.DateTimeFormat(undefined,mode==='time'?{timeStyle:'medium',timeZone:zone}:{dateStyle:'short',timeStyle:'medium',timeZone:zone}).format(date)}
    catch(_error){return date.toISOString()}
  }
  window.formatAppDate=value=>format(value,'date');
  window.formatAppTime=value=>format(value,'time');
  window.appTimezone=()=>zone;
  window.setAppTimezone=value=>{zone=value||'America/Sao_Paulo';window.VSE_TIMEZONE=zone};
})();
