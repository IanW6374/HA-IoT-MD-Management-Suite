// Health must not wait for unrelated release, log or backup requests/renderers.
let deviceStatusRefresh = null;
async function refreshDeviceStatus() {
  if (deviceStatusRefresh) return deviceStatusRefresh;
  deviceStatusRefresh = (async () => {
    try {
      const result = await api('api/devices');
      if (!Array.isArray(result?.devices)) throw new Error('Invalid device status response');
      state.devices = result.devices;
      renderDevices();
      renderMetrics();
      clearWorkspaceError('Device status could not be refreshed');
      return result;
    } catch (error) {
      showWorkspaceError('Device status could not be refreshed', error);
      throw error;
    }
  })();
  try { return await deviceStatusRefresh; }
  finally { deviceStatusRefresh = null; }
}
function refreshVisibleDeviceStatus() {
  if (!document.hidden) refreshDeviceStatus().catch(() => {});
}
document.addEventListener('visibilitychange', refreshVisibleDeviceStatus);
window.addEventListener('pageshow', refreshVisibleDeviceStatus);
window.addEventListener('focus', refreshVisibleDeviceStatus);
