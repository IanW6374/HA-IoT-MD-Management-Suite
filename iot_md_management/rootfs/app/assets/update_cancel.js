const updateCancelPending = new Set(), updateCancelErrors = new Map(), updateCancelAllPending = new Set();
function updateCancelDeviceBadge(deployment, id) {
  if (!deployment.update) return '';
  const result=deployment.results[id]||{status:'queued'}, key=deployment.id+':'+id;
  if (['complete','failed','cancelled'].includes(result.status)) return '';
  const busy=updateCancelPending.has(key)||result.status==='cancelling', installing=result.status==='installing';
  const device=state.devices.find(item=>item.id===id), label=device?.host||id;
  return `<button class="badge cancel-badge ${busy?'warn':'danger'}" type="button" data-deployment="${esc(deployment.id)}" data-device="${esc(id)}" onclick="cancelDeploymentUpdate(this)" ${busy||installing||updateCancelAllPending.has(deployment.id)?'disabled':''} aria-label="Cancel update for ${esc(label)}" title="${installing?'Installation has begun; wait for confirmation before rollback':'Cancel staging for '+esc(label)+'; does not roll back an installed release'}">${busy?'Cancelling…':'Cancel'}</button>`;
}
function updateCancelError(deployment,id) {
  const error=updateCancelErrors.get(deployment.id+':'+id);
  return error?`<p class="status error" role="alert">${esc(error)}</p>`:'';
}
function updateCancelEligible(deployment,id) {
  const status=deployment.results[id]?.status||'queued';
  return !!deployment.update&&deployment.targets.includes(id)&&!['complete','failed','cancelled','cancelling','installing'].includes(status)&&!updateCancelPending.has(deployment.id+':'+id);
}
function updateCancelAllBadge(deployment) {
  if (!deployment.update||deployment.targets.every(id=>['complete','failed','cancelled'].includes(deployment.results[id]?.status)))return '';
  const busy=updateCancelAllPending.has(deployment.id), eligible=deployment.targets.some(id=>updateCancelEligible(deployment,id));
  return `<button class="badge cancel-badge ${busy?'warn':'danger'}" type="button" data-deployment="${esc(deployment.id)}" onclick="cancelAllDeploymentUpdates(this)" ${busy||!eligible?'disabled':''} title="Cancel queued and staging updates; installation already in progress is left running">${busy?'Cancelling…':'Cancel all'}</button>`;
}
async function requestDeploymentCancellation(deployment_id,device_id) {
  const key=deployment_id+':'+device_id;
  updateCancelPending.add(key);updateCancelErrors.delete(key);renderDeployments();
  try {
    const result=await api('api/deployments/cancel',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({deployment_id,device_id})});
    const index=state.deployments.findIndex(item=>item.id===result.id);
    if(index>=0)state.deployments[index]=result;
  } catch(error) { updateCancelErrors.set(key,error.message); }
  finally {updateCancelPending.delete(key);renderDeployments();}
}
async function cancelDeploymentUpdate(button) {
  const deployment_id=button.dataset.deployment,device_id=button.dataset.device, deployment=state.deployments.find(item=>item.id===deployment_id);
  if(!deployment||!updateCancelEligible(deployment,device_id)||updateCancelAllPending.has(deployment_id))return;
  if (!confirm('Cancel this device update? Staged data will be discarded. Installation cannot be cancelled after it starts.')) return;
  await requestDeploymentCancellation(deployment_id,device_id);
  refreshDeploymentProgress().catch(error=>showWorkspaceError('Update status could not be refreshed',error));
}
async function cancelAllDeploymentUpdates(button) {
  const deployment_id=button.dataset.deployment,deployment=state.deployments.find(item=>item.id===deployment_id);
  if(!deployment?.update||updateCancelAllPending.has(deployment_id))return;
  const targets=deployment.targets.filter(id=>updateCancelEligible(deployment,id));
  if(!targets.length||!confirm('Cancel all queued and staging updates in this job? Devices already installing will be left running.'))return;
  updateCancelAllPending.add(deployment_id);renderDeployments();
  try {
    for(const id of targets) {
      const current=state.deployments.find(item=>item.id===deployment_id);
      if(current&&updateCancelEligible(current,id))await requestDeploymentCancellation(deployment_id,id);
    }
  } finally {updateCancelAllPending.delete(deployment_id);renderDeployments();}
  refreshDeploymentProgress().catch(error=>showWorkspaceError('Update status could not be refreshed',error));
}
