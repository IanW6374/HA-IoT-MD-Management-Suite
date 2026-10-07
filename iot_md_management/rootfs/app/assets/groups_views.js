// Saved empty groups plus groups already assigned to devices.
function availableGroups(){return [...new Set(['default',...(state.groups||[]).map(group=>group.name),...state.devices.map(device=>device.cohort)])].filter(Boolean).sort((a,b)=>a.localeCompare(b))}
function groupOptions(selected='default'){return [...new Set([...availableGroups(),selected])].filter(Boolean).map(name=>`<option value="${esc(name)}" ${name===selected?'selected':''}>${esc(name)}</option>`).join('')}
function renderGroups(){
  const box=document.getElementById('group-list');
  if(box)box.innerHTML=`<table class="catalog-table"><caption class="visually-hidden">Saved device groups</caption><thead><tr><th scope="col">Group</th><th scope="col">Devices</th></tr></thead><tbody>${availableGroups().map(name=>`<tr><td>${esc(name)}</td><td>${state.devices.filter(device=>device.cohort===name).length}</td></tr>`).join('')}</tbody></table>`;
  for(const select of document.querySelectorAll('[data-group-select]')){
    const selected=select.value||'default',options=groupOptions(selected);
    if(select.innerHTML!==options)select.innerHTML=options;
  }
}
document.getElementById('group-create').onsubmit=async event=>{
  event.preventDefault();const form=event.target,status=document.getElementById('group-status'),button=form.querySelector('button');button.disabled=true;
  try{await api('api/groups',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:form.elements.name.value})});form.reset();await refreshAll();status.className='status success';status.textContent='Group created.'}
  catch(error){status.className='status error';status.textContent=error.message}
  finally{button.disabled=false}
};
