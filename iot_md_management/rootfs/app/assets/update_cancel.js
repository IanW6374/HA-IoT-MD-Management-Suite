const updateCancelPending = new Set(), updateCancelErrors = new Map(), updateCancelAllPending = new Set(), updateCancelUncertain = new Set();
function reconcileUpdateCancellations(){
  for(const deployment of state.deployments)for(const id of deployment.targets){
    if(['complete','failed','cancelled'].includes(deployment.results[id]?.status)){
      const key=deployment.id+':'+id;updateCancelUncertain.delete(key);updateCancelErrors.delete(key);
    }
  }
}
function updateCancelDeviceBadge(deployment, id) {
  if (!deployment.update) return '';
  const result=deployment.results[id]||{status:'queued'}, key=deployment.id+':'+id;
  if (['complete','failed','cancelled'].includes(result.status)) return '';
  const busy=updateCancelPending.has(key)||result.status==='cancelling', installing=result.status==='installing', uncertain=updateCancelUncertain.has(key);
  const device=state.devices.find(item=>item.id===id), label=device?.host||id;
  return `<button class="badge cancel-badge ${busy||uncertain?'warn':'danger'}" type="button" data-deployment="${esc(deployment.id)}" data-device="${esc(id)}" onclick="cancelDeploymentUpdate(this)" ${busy||uncertain||installing||updateCancelAllPending.has(deployment.id)?'disabled':''} aria-label="Cancel update for ${esc(label)}" title="${installing?'Installation has begun; wait for confirmation before rollback':uncertain?'Cancellation acknowledgement is uncertain; checking status without repeating the request':'Cancel staging for '+esc(label)+'; does not roll back an installed release'}">${uncertain?'Checking…':busy?'Cancelling…':'Cancel'}</button>`;
}
function updateCancelError(deployment,id) {
  const error=updateCancelErrors.get(deployment.id+':'+id);
  return error?`<p class="status error" role="alert">${esc(error)}</p>`:'';
}
function updateCancelEligible(deployment,id) {
  const status=deployment.results[id]?.status||'queued';
  return !!deployment.update&&deployment.targets.includes(id)&&!['complete','failed','cancelled','cancelling','installing'].includes(status)&&!updateCancelPending.has(deployment.id+':'+id)&&!updateCancelUncertain.has(deployment.id+':'+id);
}
function updateCancelAllBadge(deployment) {
  if (!deployment.update||deployment.targets.every(id=>['complete','failed','cancelled'].includes(deployment.results[id]?.status)))return '';
  const busy=updateCancelAllPending.has(deployment.id), eligible=deployment.targets.some(id=>updateCancelEligible(deployment,id));
  return `<button class="badge cancel-badge ${busy?'warn':'danger'}" type="button" data-deployment="${esc(deployment.id)}" onclick="cancelAllDeploymentUpdates(this)" ${busy||!eligible?'disabled':''} title="Cancel queued and staging updates; installation already in progress is left running">${busy?'Cancelling…':'Cancel all'}</button>`;
}
async function requestDeploymentCancellation(deployment_id,device_id) {
  const key=deployment_id+':'+device_id;
  deploymentMutationEpoch++;
  updateCancelPending.add(key);updateCancelErrors.delete(key);renderDeploymentStatus();
  let timer;
  try {
    const acknowledgement=api('api/deployments/cancel',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({deployment_id,device_id})}).then(result=>{
      if(result?.id!==deployment_id||!result.results?.[device_id])throw Error('Invalid cancellation acknowledgement');
      deploymentMutationEpoch++;applyDeploymentSnapshot(result);
      updateCancelUncertain.delete(key);updateCancelErrors.delete(key);renderDeploymentStatus();
      return result;
    });
    // Do not abort or replay a mutation. Bound only how long the UI waits;
    // a late acknowledgement still updates the view safely using its revision.
    await Promise.race([acknowledgement,new Promise((_,reject)=>{timer=setTimeout(()=>reject(Error('Cancellation acknowledgement timed out; checking status. Do not repeat the request.')),45000);})]);
  } catch(error) { updateCancelUncertain.add(key);updateCancelErrors.set(key,error.message); }
  finally {clearTimeout(timer);updateCancelPending.delete(key);renderDeploymentStatus();refreshDeploymentProgress().catch(()=>{});}
}
async function cancelDeploymentUpdate(button) {
  const deployment_id=button.dataset.deployment,device_id=button.dataset.device, deployment=state.deployments.find(item=>item.id===deployment_id);
  if(!deployment||!updateCancelEligible(deployment,device_id)||updateCancelAllPending.has(deployment_id))return;
  if (!confirm('Cancel this device update? Staged data will be discarded. Installation cannot be cancelled after it starts.')) return;
  await requestDeploymentCancellation(deployment_id,device_id);
  refreshDeploymentProgress().catch(()=>{});
}
async function cancelAllDeploymentUpdates(button) {
  const deployment_id=button.dataset.deployment,deployment=state.deployments.find(item=>item.id===deployment_id);
  if(!deployment?.update||updateCancelAllPending.has(deployment_id))return;
  const targets=deployment.targets.filter(id=>updateCancelEligible(deployment,id));
  if(!targets.length||!confirm('Cancel all queued and staging updates in this job? Devices already installing will be left running.'))return;
  updateCancelAllPending.add(deployment_id);renderDeploymentStatus();
  try {
    // Keep status reads responsive without serializing the entire batch behind
    // one slow device. Refill each worker independently as acknowledgements arrive.
    let next=0;
    const worker=async()=>{
      while(next<targets.length){
        const id=targets[next++],current=state.deployments.find(item=>item.id===deployment_id);
        if(current&&updateCancelEligible(current,id))await requestDeploymentCancellation(deployment_id,id);
      }
    };
    await Promise.all(Array.from({length:Math.min(4,targets.length)},worker));
  } finally {updateCancelAllPending.delete(deployment_id);renderDeploymentStatus();}
  refreshDeploymentProgress().catch(()=>{});
}
