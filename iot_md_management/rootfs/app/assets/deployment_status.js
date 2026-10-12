// Deployment reads are independent of catalog/log reads. Revisions protect
// same-second updates; the epoch rejects reads started before a local mutation.
let deploymentRefreshPromise=null, deploymentMutationEpoch=0, trackedDeploymentId='';
let workspacePeriodicPromise=null;
async function refreshPeriodicWorkspace(){
  if(document.hidden)return;
  if(workspacePeriodicPromise)return workspacePeriodicPromise;
  workspacePeriodicPromise=refreshAll(true);
  try{return await workspacePeriodicPromise;}finally{workspacePeriodicPromise=null;}
}
function visibleWorkspaceData(name){
  const page=document.body.dataset.page||'overview';
  const pages={overview:['devices','deployments','backups','attention','releases','seed'],
    actions:['devices','deployments','backups','seed'],devices:['devices','groups'],
    releases:['releases'],profiles:['profiles'],logs:['devices','deployments','backups','audit','events','attention'],
    activity:['devices','deployments','backups','audit','events','attention']};
  return (pages[page]||[]).includes(name);
}
function applyDeploymentSnapshot(incoming) {
  const index=state.deployments.findIndex(item=>item.id===incoming.id);
  if(index<0)return;
  if(Number(incoming.revision||0)<Number(state.deployments[index].revision||0))return;
  state.deployments[index]=incoming;
}
function renderDeploymentStatus(historyChanged=true,liveChanged=true) {
  reconcileUpdateCancellations();
  if(liveChanged||historyChanged)renderMetrics();
  if(liveChanged)renderDeployments();else if(typeof refreshProgressFreshness==='function')refreshProgressFreshness();
  if(historyChanged)renderActionHistory();
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
        const terminal=item=>['complete','failed','partial','staged','cancelled'].includes(item.status),
          scoped=document.body?.dataset?.page==='actions',
          include=[...new Set([trackedDeploymentId,...state.deployments.filter(item=>!terminal(item)).map(item=>item.id)].filter(Boolean))].slice(0,64),
          endpoint=scoped?'api/deployments?view=active'+include.map(id=>'&include='+encodeURIComponent(id)).join(''):'api/deployments';
        const epoch=deploymentMutationEpoch, result=await api(endpoint);
        if(!Array.isArray(result?.deployments))throw Error('Invalid deployment status response');
        if(epoch!==deploymentMutationEpoch)continue;
        const known=new Map(state.deployments.map(item=>[item.id,item]));
        const incoming=result.deployments.map(item=>Number(item.revision||0)<Number(known.get(item.id)?.revision||0)?known.get(item.id):item);
        if(scoped){
          const ids=new Set(incoming.map(item=>item.id));
          incoming.push(...state.deployments.filter(item=>terminal(item)&&!ids.has(item.id)).slice(0,20));
        }
        const historyKey=items=>JSON.stringify(items.filter(terminal).map(item=>[item.id,item.revision||0,item.status]));
        const liveKey=items=>JSON.stringify(items.filter(item=>!terminal(item)));
        const historyChanged=historyKey(incoming)!==historyKey(state.deployments),liveChanged=liveKey(incoming)!==liveKey(state.deployments);
        state.deployments=incoming;
        renderDeploymentStatus(historyChanged,liveChanged);clearWorkspaceError('Update status could not be refreshed');
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
