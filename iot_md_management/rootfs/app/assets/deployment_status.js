// Deployment reads are independent of catalog/log reads. Revisions protect
// same-second updates; the epoch rejects reads started before a local mutation.
let deploymentRefreshPromise=null, deploymentMutationEpoch=0, trackedDeploymentId='';
function applyDeploymentSnapshot(incoming) {
  const index=state.deployments.findIndex(item=>item.id===incoming.id);
  if(index<0)return;
  if(Number(incoming.revision||0)<Number(state.deployments[index].revision||0))return;
  state.deployments[index]=incoming;
}
function renderDeploymentStatus() {
  reconcileUpdateCancellations();
  renderMetrics();renderDeployments();renderActionHistory();
  if(trackedDeploymentId){
    const tracked=state.deployments.find(item=>item.id===trackedDeploymentId);
    if(tracked&&['complete','failed','partial','staged','cancelled'].includes(tracked.status)){
      const status=document.getElementById('deployment-form-status');
      if(status){status.className='status';status.textContent='';}
      trackedDeploymentId='';
    }
  }
}
async function refreshDeploymentProgress() {
  if(document.hidden)return {deployments:state.deployments};
  if(deploymentRefreshPromise)return deploymentRefreshPromise;
  deploymentRefreshPromise=(async()=>{
    try{
      // One catch-up read if a cancellation invalidated the in-flight read.
      for(let attempt=0;attempt<2;attempt++){
        const epoch=deploymentMutationEpoch, result=await api('api/deployments');
        if(!Array.isArray(result?.deployments))throw Error('Invalid deployment status response');
        if(epoch!==deploymentMutationEpoch)continue;
        const known=new Map(state.deployments.map(item=>[item.id,item]));
        state.deployments=result.deployments.map(item=>Number(item.revision||0)<Number(known.get(item.id)?.revision||0)?known.get(item.id):item);
        renderDeploymentStatus();clearWorkspaceError('Update status could not be refreshed');
        return result;
      }
      return {deployments:state.deployments};
    }catch(error){showWorkspaceError('Update status could not be refreshed',error);throw error;}
  })();
  try{return await deploymentRefreshPromise;}finally{deploymentRefreshPromise=null;}
}
function refreshVisibleDeploymentStatus(){if(!document.hidden)refreshDeploymentProgress().catch(()=>{});}
document.addEventListener('visibilitychange',refreshVisibleDeploymentStatus);
window.addEventListener('pageshow',refreshVisibleDeploymentStatus);
window.addEventListener('focus',refreshVisibleDeploymentStatus);
