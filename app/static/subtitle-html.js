/* Keep preview cleanup offers consistent with app/subtitle_html.py. */
(function () {
  const names=new Set('i b u s em strong font span br div p ruby rt rb c q small big sub sup a nobr strike tt v lang center marquee blink mark del ins code pre blockquote h1 h2 h3 h4 h5 h6 ul ol li table thead tbody tr td th caption'.split(' '));
  const decoder=document.createElement('textarea');
  const colorAttribute=/(?:^|\s)color\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s/>]+))/i;
  const styleAttribute=/(?:^|\s)style\s*=\s*(?:"([^"]*)"|'([^']*)')/i;
  const colorDeclaration=/(?:^|;)\s*color\s*:\s*([^;]+)/i;
  const safeColor=/^(?:#[0-9a-f]{3,8}|[a-z]{1,32}|rgba?\([\d.,%\s]+\)|hsla?\([\d.,%\s]+\))$/i;
  window.hasRemovableSubtitleHtml=function(text){
    const stack=[];
    for(const match of String(text||'').matchAll(/<\s*(\/?)\s*([a-z][a-z0-9]*)\b([^<>]*?)>/gi)){
      const name=match[2].toLowerCase(),attributes=match[3];if(!names.has(name))continue;
      // Void breaks (including subtitle </br> variants) do not own styling.
      if(name==='br'){if(attributes.replace(/[\/\s]/g,''))return true;continue;}
      if(match[1]){
        const index=stack.findLastIndex(item=>item.name===name);
        if(index<0||!stack[index].retained)return true;
        stack.splice(index);continue;
      }
      let retained=name==='i'||name==='em'||name==='u',remainder=attributes;
      if(name==='font'||name==='span'){
        const attribute=attributes.match(name==='font'?colorAttribute:styleAttribute);
        const value=attribute?.slice(1).find(v=>v!==undefined)||'';
        const declaration=name==='span'?value.match(colorDeclaration):null;
        decoder.innerHTML=name==='font'?value:declaration?.[1]||'';
        retained=safeColor.test(decoder.value.trim());
        remainder=attributes.replace(name==='font'?colorAttribute:styleAttribute,'');
        if(name==='span'&&value.replace(colorDeclaration,'').replace(/[;\s]/g,''))return true;
      }
      if(!retained||remainder.replace(/[\/\s]/g,''))return true;
      if(!attributes.trimEnd().endsWith('/')&&name!=='br')stack.push({name,retained});
    }
    return false;
  };
})();
