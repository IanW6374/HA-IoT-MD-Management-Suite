// One hierarchical selection control; editor values remain intact on deselection.
function profileSelectionState(names,selected){
  const count=names.filter(name=>selected.has(name)).length;
  return {count,total:names.length,checked:!!names.length&&count===names.length,indeterminate:count>0&&count<names.length};
}
function enhanceProfileDesigner(){
  const form=document.getElementById('profile-editor');
  if(!form||form.dataset.designer==='true')return;
  form.dataset.designer='true';
  const first=form.querySelector('.profile-group'),advanced=form.querySelector('details');
  const entries=[...form.querySelectorAll('.profile-entry')],groups=new Map(),expanded=new Set();
  const picker=document.createElement('section');picker.className='profile-picker';
  picker.innerHTML=`<h3 id="profile-settings-label">Choose settings</h3><details id="profile-settings-picker" class="profile-dropdown"><summary aria-labelledby="profile-settings-label profile-selection-summary" aria-controls="profile-settings-options"><span id="profile-selection-summary">Baseline</span><span aria-hidden="true">▾</span></summary><div id="profile-settings-options" class="profile-options"><label class="profile-option-search">Search settings<input id="profile-settings-search" type="search" placeholder="Group or setting"></label><div class="profile-selection-tools"><label class="profile-option"><input id="profile-select-all" type="checkbox"><strong>Select all settings</strong><span id="profile-all-count" class="muted"></span></label><button id="profile-reset-baseline" class="secondary compact" type="button">Reset to baseline</button></div><div id="profile-selection-groups"></div><p id="profile-settings-empty" class="muted hidden">No settings match your search.</p></div></details><div id="profile-selection-chips" class="profile-selection-chips" aria-label="Selected setting groups"></div><p class="muted">Baseline is selected by default. Select all includes device name, secrets and certificates; blank bulk-selected values are left unchanged on the device.</p><span id="profile-selected-count" class="visually-hidden" role="status" aria-live="polite"></span>`;
  first.after(picker);
  for(const entry of entries){
    const control=entry.querySelector('[name]'),include=entry.querySelector('[data-profile-include]'),fieldLabel=control.closest('label');
    const section=entry.closest('.profile-group')?.querySelector('legend')?.textContent||'Settings';
    entry.dataset.settingName=control.name;include.closest('label').classList.add('hidden');
    const heading=document.createElement('div');heading.className='profile-entry-heading';
    heading.innerHTML=`<strong>${esc(entry.dataset.label)}</strong>`;
    const remove=document.createElement('button');remove.type='button';remove.className='badge profile-remove';remove.textContent='Remove';remove.setAttribute('aria-label',`Remove ${entry.dataset.label}`);
    remove.onclick=()=>setSelection([control.name],false);
    heading.append(remove);entry.prepend(heading);
    for(const node of [...fieldLabel.childNodes])if(node.nodeType===3)node.remove();
    if(!groups.has(section))groups.set(section,{names:[],entries:[],advanced:!!advanced?.contains(entry)});
    groups.get(section).names.push(control.name);groups.get(section).entries.push(entry);
  }
  const dropdown=picker.querySelector('details'),summary=dropdown.querySelector('summary'),groupBox=picker.querySelector('#profile-selection-groups');
  const baseline=new Set(baselineProfileFields(entries));
  function selectedNames(){return new Set(entries.filter(entry=>entry.querySelector('[data-profile-include]').checked).map(entry=>entry.dataset.settingName))}
  function setSelection(names,checked,bulk=false){
    for(const entry of entries.filter(item=>names.includes(item.dataset.settingName))){
      const include=entry.querySelector('[data-profile-include]');
      if(checked&&!include.checked)entry.dataset.baselineOptional=String(bulk);
      include.checked=checked;
    }
    if(checked&&advanced&&entries.some(entry=>names.includes(entry.dataset.settingName)&&advanced.contains(entry)))advanced.open=true;
    updateProfileDesigner();
  }
  let index=0;
  for(const [section,group] of groups){
    const id='profile-setting-group-'+index++,row=document.createElement('section');row.className='profile-selection-group';
    row.innerHTML=`<div class="profile-group-choice"><label class="profile-option"><input type="checkbox" data-selection-group><strong>${esc(section)}</strong><small class="profile-group-count muted"></small></label><button type="button" class="secondary profile-group-expand" aria-label="Expand ${esc(section)} settings" aria-controls="${id}" aria-expanded="false"><span aria-hidden="true">▾</span></button></div><div id="${id}" class="profile-group-settings hidden">${group.entries.map(entry=>`<label class="profile-option profile-setting-choice"><input type="checkbox" data-selection-setting="${esc(entry.dataset.settingName)}"><span>${esc(entry.dataset.label)}</span>${['secret','file'].includes(entry.dataset.profileKind)?'<small class="muted">Secret</small>':''}</label>`).join('')}</div>`;
    groupBox.append(row);group.row=row;group.check=row.querySelector('[data-selection-group]');group.toggle=row.querySelector('button');group.children=row.querySelector('.profile-group-settings');
    group.check.onchange=()=>setSelection(group.names,group.check.checked,true);
    group.toggle.onclick=()=>{if(expanded.has(section))expanded.delete(section);else expanded.add(section);filterSettings()};
    for(const checkbox of row.querySelectorAll('[data-selection-setting]'))checkbox.onchange=()=>setSelection([checkbox.dataset.selectionSetting],checkbox.checked);
  }
  function syncCheck(control,names,selected){const state=profileSelectionState(names,selected);control.checked=state.checked;control.indeterminate=state.indeterminate;return state}
  function updateProfileDesigner(){
    const selected=selectedNames(),count=selected.size;
    const optionalText=new Set(['device_description','wifi_ip_address','wifi_subnet_mask','wifi_gateway','wifi_dns_server','mqtt_server','mqtt_username','syslog_host','acme_directory_url','certificate_hostname','portal_certificate_hostname']);
    for(const entry of entries){const control=entry.querySelector('[name]'),included=selected.has(control.name);entry.classList.toggle('hidden',!included);control.disabled=!included;control.required=included&&control.type!=='checkbox'&&!optionalText.has(control.name)&&entry.dataset.baselineOptional!=='true'}
    picker.querySelector('#profile-selected-count').textContent=`${count} settings selected`;
    const isBaseline=count===baseline.size&&[...baseline].every(name=>selected.has(name));
    picker.querySelector('#profile-selection-summary').textContent=`${isBaseline?'Baseline · ':''}${count} setting${count===1?'':'s'} selected`;
    const all=syncCheck(picker.querySelector('#profile-select-all'),entries.map(entry=>entry.dataset.settingName),selected);
    picker.querySelector('#profile-all-count').textContent=`${all.count}/${all.total}`;
    const chips=picker.querySelector('#profile-selection-chips');chips.replaceChildren();
    for(const [section,group] of groups){
      const state=syncCheck(group.check,group.names,selected);
      group.row.dataset.selectionState=state.checked?'all':state.indeterminate?'some':'none';
      group.row.querySelector('.profile-group-count').textContent=`${group.advanced?'Advanced · ':''}${state.count}/${state.total}`;
      for(const checkbox of group.row.querySelectorAll('[data-selection-setting]'))checkbox.checked=selected.has(checkbox.dataset.selectionSetting);
      if(state.count){
        const chip=document.createElement('span');chip.className='badge profile-selection-chip';
        chip.append(document.createTextNode(`${section} ${state.count}/${state.total}`));
        const remove=document.createElement('button');remove.type='button';remove.textContent='×';remove.setAttribute('aria-label',`Remove ${section} settings`);remove.onclick=()=>{setSelection(group.names,false);summary.focus()};chip.append(remove);chips.append(chip);
      }
    }
    if(count){const clear=document.createElement('button');clear.type='button';clear.className='secondary compact';clear.textContent='Clear all';clear.onclick=()=>{setSelection(entries.map(entry=>entry.dataset.settingName),false);summary.focus()};chips.append(clear)}
    for(const group of form.querySelectorAll('.profile-group'))if(group!==first)group.classList.toggle('hidden',![...group.querySelectorAll('.profile-entry')].some(entry=>selected.has(entry.dataset.settingName)));
    const showAdvanced=advanced&&entries.some(entry=>selected.has(entry.dataset.settingName)&&advanced.contains(entry));
    advanced?.classList.toggle('hidden',!showAdvanced);if(advanced&&!showAdvanced)advanced.removeAttribute('open');
  }
  function filterSettings(){
    const query=picker.querySelector('#profile-settings-search').value.trim().toLocaleLowerCase();let matches=0;
    for(const [section,group] of groups){
      const sectionMatch=section.toLocaleLowerCase().includes(query);let visible=0;
      for(const label of group.children.querySelectorAll('label')){const match=!query||sectionMatch||label.textContent.toLocaleLowerCase().includes(query);label.classList.toggle('hidden',!match);if(match)visible++}
      group.row.classList.toggle('hidden',!visible);if(visible)matches++;
      const open=expanded.has(section)||!!query;group.children.classList.toggle('hidden',!open);group.toggle.setAttribute('aria-expanded',String(open));group.toggle.setAttribute('aria-label',`${open?'Collapse':'Expand'} ${section} settings`);group.toggle.querySelector('span').textContent=open?'▴':'▾';
    }
    picker.querySelector('#profile-settings-empty').classList.toggle('hidden',!!matches);
  }
  picker.querySelector('#profile-settings-search').oninput=filterSettings;
  picker.querySelector('#profile-select-all').onchange=event=>setSelection(entries.map(entry=>entry.dataset.settingName),event.target.checked,true);
  picker.querySelector('#profile-reset-baseline').onclick=()=>{for(const entry of entries){entry.querySelector('[data-profile-include]').checked=baseline.has(entry.dataset.settingName);entry.dataset.baselineOptional='true'}updateProfileDesigner()};
  dropdown.addEventListener('toggle',()=>summary.setAttribute('aria-expanded',String(dropdown.open)));
  dropdown.addEventListener('keydown',event=>{if(event.key==='Escape'){event.preventDefault();dropdown.open=false;summary.focus()}});
  document.addEventListener('click',event=>{if(!dropdown.contains(event.target))dropdown.open=false});
  window.updateProfileDesigner=updateProfileDesigner;
  picker.querySelector('#profile-reset-baseline').click();filterSettings();
}
