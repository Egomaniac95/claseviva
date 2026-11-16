/* Selector accesible con iconos locales; el select conserva el valor y sus eventos. */
(() => {
 'use strict';
 const pickers = new Map();
 const iconURL = /^\/api\/iconos-app\/[a-f0-9]{64}\.png$/;
 const sourceFor = value => AUDIO_SOURCES.find(source => source.id === value) ||
  (['chrome','msedge'].includes(value) ? AUDIO_SOURCES.find(source => source.icono === value) : null);

 function loadIcon(image, url, fallback) {
  url = iconURL.test(url || '') ? url : '';
  if (image.dataset.iconUrl === url) return;
  image.dataset.iconUrl = url;image.hidden = true;
  fallback.style.visibility = '';
  if (!url) {image.removeAttribute('src');return;}
  let attempts = 0;
  image.onload = () => {
   if (image.dataset.iconUrl !== url) return;
   image.hidden = false;fallback.style.visibility = 'hidden';
  };
  image.onerror = () => {
   if (image.dataset.iconUrl !== url) return;
   image.hidden = true;fallback.style.visibility = '';
   if (attempts++ < 2) setTimeout(() => {
    if (image.dataset.iconUrl === url) image.src = url + '?retry=' + attempts;
   }, 800);
  };
  image.src = url;
 }

 function iconNode(source) {
  const holder = document.createElement('span');holder.className = 'source-option-icon';
  const key = ['chrome','msedge','sistema'].includes(source?.icono) ? source.icono : 'app';
  holder.innerHTML = '<svg aria-hidden="true" viewBox="0 0 24 24"><use href="#source-' + key + '"/></svg>';
  if (source?.icono_url) {
   const image = document.createElement('img');image.alt = '';image.width = image.height = 32;
   image.decoding = 'async';holder.appendChild(image);
   loadIcon(image, source.icono_url, holder.firstElementChild);
  }
  return holder;
 }

 function close(picker, focus = false) {
  picker.list.hidden = true;picker.button.setAttribute('aria-expanded','false');
  picker.root.classList.remove('is-open');picker.section?.classList.remove('source-popup-open');
  if (focus) picker.button.focus();
 }

 function open(picker, last = false) {
  if (picker.select.disabled) return;
  for (const other of pickers.values()) if (other !== picker) close(other);
  picker.list.hidden = false;picker.button.setAttribute('aria-expanded','true');
  picker.root.classList.add('is-open');picker.section?.classList.add('source-popup-open');
  const options = [...picker.list.querySelectorAll('[role="option"]:not(:disabled)')];
  const selected = options.find(option => option.getAttribute('aria-selected') === 'true');
  (selected || (last ? options.at(-1) : options[0]))?.focus();
 }

 function create(select) {
  const root = select.closest('.source-select');
  if (!root) return null;
  const id = select.id;
  const button = document.createElement('button');button.type = 'button';button.id = id + 'Picker';
  button.className = 'source-picker-toggle';button.setAttribute('role','combobox');
  button.setAttribute('aria-haspopup','listbox');button.setAttribute('aria-expanded','false');
  button.setAttribute('aria-controls',id + 'Options');
  button.setAttribute('aria-label',id === 'ifuente' ? 'Audio' : 'Fuente de audio');
  const text = document.createElement('span');text.className = 'source-picker-text';button.appendChild(text);
  const image = document.createElement('img');image.className = 'source-native-icon';image.id = id + 'NativeIcon';
  image.width = image.height = 32;image.alt = '';image.hidden = true;image.decoding = 'async';
  const list = document.createElement('div');list.id = id + 'Options';
  list.className = 'source-picker-list';list.setAttribute('role','listbox');
  list.setAttribute('aria-label','Aplicaciones de audio');list.hidden = true;
  root.append(button,image,list);root.classList.add('has-system-icons');
  select.tabIndex = -1;select.setAttribute('aria-hidden','true');
  const label = document.querySelector('label[for="' + id + '"]');
  if (label) label.htmlFor = button.id;
  const picker = {select,root,button,text,image,list,section:root.closest('section'),signature:''};
  button.onclick = () => list.hidden ? open(picker) : close(picker,true);
  button.onkeydown = event => {
   if (['ArrowDown','ArrowUp'].includes(event.key)) {
    event.preventDefault();open(picker,event.key === 'ArrowUp');
   } else if (event.key === 'Escape') {event.preventDefault();close(picker,true);}
  };
  let search = '',searchTime = 0;
  list.onkeydown = event => {
   const options = [...list.querySelectorAll('[role="option"]:not(:disabled)')];
   const current = options.indexOf(document.activeElement);
   let next;
   if (event.key === 'ArrowDown') next = (current + 1) % options.length;
   else if (event.key === 'ArrowUp') next = (current - 1 + options.length) % options.length;
   else if (event.key === 'Home') next = 0;
   else if (event.key === 'End') next = options.length - 1;
   else if (event.key === 'Escape') {event.preventDefault();close(picker,true);return;}
   else if (event.key === 'Tab') {close(picker,true);return;}
   else if (event.key.length === 1 && event.key !== ' ' && !event.ctrlKey && !event.altKey) {
    search = performance.now() - searchTime > 700 ? event.key : search + event.key;
    searchTime = performance.now();
    next = options.findIndex(option => option.textContent.trim().toLocaleLowerCase().startsWith(search.toLocaleLowerCase()));
   }
   if (next !== undefined && options[next]) {event.preventDefault();options[next].focus();}
  };
  pickers.set(id,picker);
  return picker;
 }

 globalThis.syncSourcePicker = id => {
  const select = document.getElementById(id);
  if (!select) return;
  const picker = pickers.get(id) || create(select);
  if (!picker) return;
  const selected = sourceFor(select.value);
  picker.text.textContent = select.selectedOptions[0]?.textContent || 'Selecciona una aplicación';
  picker.button.disabled = select.disabled;
  const fallback = document.getElementById(id + 'Icon')?.closest('svg');
  if (fallback) loadIcon(picker.image,selected?.icono_url,fallback);
  const signature = JSON.stringify([...select.options].map(option =>
   [option.value,option.textContent,sourceFor(option.value)?.icono_url || '',option.disabled]));
  if (picker.signature !== signature) {
   close(picker);picker.list.replaceChildren();
   for (const option of select.options) {
    const source = sourceFor(option.value);
    const row = document.createElement('button');row.type = 'button';row.className = 'source-picker-option';
    row.setAttribute('role','option');row.tabIndex = -1;row.dataset.value = option.value;
    row.disabled = option.disabled || (!source && option.value !== 'sistema');
    row.title = source?.detalle || '';
    row.appendChild(iconNode(source || {icono:option.value === 'sistema' ? 'sistema' : 'app'}));
    const label = document.createElement('span');label.textContent = option.textContent;row.appendChild(label);
    row.onclick = () => {
     select.value = option.value;close(picker,true);
     select.dispatchEvent(new Event('change',{bubbles:true}));
    };
    picker.list.appendChild(row);
   }
   picker.signature = signature;
  }
  for (const option of picker.list.children) option.setAttribute('aria-selected',String(option.dataset.value === select.value));
 };

 document.addEventListener('pointerdown',event => {
  for (const picker of pickers.values()) if (!picker.root.contains(event.target)) close(picker);
 });
})();

/* Compatibilidad para el panel que ya está grabando: se activa al recargar la
   página, sin reiniciar Python, capturadores ni transcriptor. El panel nuevo
   anuncia su capacidad y usa directamente su propia ruta de consolidación. */
document.addEventListener('DOMContentLoaded',async()=>{
 const bridge='http://127.0.0.1:8757';
 async function recoveryRequest(path,body){
  const control=new AbortController();const timer=setTimeout(()=>control.abort(),10000);
  try{
   const response=await fetch(bridge+path,{method:body?'POST':'GET',signal:control.signal,
    cache:'no-store',headers:body?{'Content-Type':'application/json'}:{},body:body?JSON.stringify(body):null});
   const data=await response.json();
   if(!response.ok)throw new Error(data.msg||'No se pudo completar la recuperación');
   return data;
  }finally{clearTimeout(timer)}
 }
 try{
  const parent=await fetch('/api/healthz',{cache:'no-store'}).then(response=>response.json());
  if(parent.consolidation_recovery)return;
  const helper=await recoveryRequest('/health');
  if(helper.app!=='ClaseVivaRecovery'||helper.data_root_id!==parent.data_root_id)return;
  const previous=globalThis.api;
  if(typeof previous!=='function')return;
  globalThis.api=async function(url,body,timeout){
   if(url==='/api/consolidar')return recoveryRequest(url,body||{});
   const state=await previous(url,body,timeout);
   if(url!=='/api/estado')return state;
   let recovery;
   try{recovery=await recoveryRequest('/status')}catch(_error){return state}
   if(recovery.data_root_id!==parent.data_root_id||recovery.clase_id!==state.clase_id||recovery.curso!==state.curso)return state;
   const notice=document.getElementById('recoveryNotice');
   if(notice)notice.textContent=state.estado==='fin'
    ?'Grabación finalizada. Pulsa Consolidar para recuperar el audio pendiente. Si una API falla, los audios se conservan y puedes reintentar.'
    :'Recuperación preparada. Al terminar, pulsa Fin y después Consolidar; si falla la red, puedes volver a intentarlo.';
   if(state.estado!=='fin')return state;
   if(state.estado==='fin'&&recovery.ready){
    state.shutdown_blocking=false;state.consolidation_ready=true;
    state.consolidation_status='ready_to_recover_stt';
    if(!recovery.running&&recovery.status!=='error'&&recovery.status!=='complete'){
     state.post_estado='pendiente';state.post_titulo='Lista para consolidar';
     state.post_mensaje='Los audios pendientes se procesarán al pulsar Consolidar.';
    }
   }
   if(recovery.running){
    state.post_corriendo=true;state.post_estado='consolidando';state.post_titulo='Recuperando y consolidando…';
    state.post_mensaje='Transcribiendo el audio conservado y generando la lección completa.';
   }else if(recovery.status==='error'){
    state.post_corriendo=false;state.post_error=recovery.error;state.post_estado='error';
    state.post_titulo='Error al consolidar';state.post_mensaje=recovery.error;
   }else if(recovery.status==='complete'){
    state.post_corriendo=false;state.post_error='';state.post_estado='consolidada';
    state.post_titulo='Lección consolidada';state.post_mensaje='Audio y lección procesados correctamente.';
   }
   if(recovery.log)state.post_log=recovery.log;
   return state;
  };
  const section=document.getElementById('secpost');
  if(section&&!document.getElementById('recoveryNotice')){
   const notice=document.createElement('div');notice.id='recoveryNotice';notice.className='post-state';
   notice.textContent='Recuperación de audio activada. Consolidar conserva los audios si una API falla y permite reintentar.';
   section.prepend(notice);
  }
  if(typeof refrescar==='function')refrescar(true);
 }catch(_error){/* Sin puente: el panel conserva su funcionamiento habitual. */}
});
