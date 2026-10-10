const updateCancelPending = new Set(), updateCancelErrors = new Map();
function updateCancelControls(deployment) {
  if (!deployment.update) return '';
  return `<div class="actions">${deployment.targets.map(id => {
    const result = deployment.results[id] || {status:'queued'}, key=deployment.id+':'+id;
    if (['complete','failed','cancelled'].includes(result.status)) return '';
    const busy=updateCancelPending.has(key)||result.status==='cancelling', installing=result.status==='installing';
    const device=state.devices.find(item=>item.id===id), label=device?.host||id;
    return `<div>${updateCancelErrors.has(key)?`<p class="status error" role="alert">${esc(updateCancelErrors.get(key))}</p>`:''}<button class="danger compact" type="button" data-deployment="${esc(deployment.id)}" data-device="${esc(id)}" onclick="cancelDeploymentUpdate(this)" ${busy||installing?'disabled':''} title="${installing?'Installation has begun; wait for confirmation before rollback':'Stops staging safely; does not roll back an installed release'}">${busy?'Cancelling…':'Cancel update'} · ${esc(label)}</button></div>`;
  }).join('')}</div>`;
}
async function cancelDeploymentUpdate(button) {
  if (!confirm('Cancel this device update? Staged data will be discarded. Installation cannot be cancelled after it starts.')) return;
  const deployment_id=button.dataset.deployment, device_id=button.dataset.device, key=deployment_id+':'+device_id;
  updateCancelPending.add(key);updateCancelErrors.delete(key);renderDeployments();
  try {
    const result=await api('api/deployments/cancel',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({deployment_id,device_id})});
    const index=state.deployments.findIndex(item=>item.id===result.id);
    if(index>=0)state.deployments[index]=result;
  } catch(error) { updateCancelErrors.set(key,error.message); }
  finally {updateCancelPending.delete(key);renderDeployments();}
  refreshDeploymentProgress();
}
