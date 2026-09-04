const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('riverbankDesktop', {
  platform: process.platform,
  getAppVersion: () => ipcRenderer.invoke('app-version'),
  loadAuthSession: () => ipcRenderer.invoke('auth-session-load'),
  saveAuthSession: (record) => ipcRenderer.invoke('auth-session-save', record),
  clearAuthSession: () => ipcRenderer.invoke('auth-session-clear'),
  downloadReport: (request) => ipcRenderer.invoke('download-report', request),
  downloadAttachment: (request) => ipcRenderer.invoke('download-attachment', request),
  openExternal: (url) => ipcRenderer.invoke('open-external', url)
});
