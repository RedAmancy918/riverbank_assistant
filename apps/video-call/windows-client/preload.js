const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('riverbankDesktop', {
  platform: process.platform,
  downloadReport: (request) => ipcRenderer.invoke('download-report', request),
  openExternal: (url) => ipcRenderer.invoke('open-external', url)
});
