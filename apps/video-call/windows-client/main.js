const { app, BrowserWindow, dialog, ipcMain, net, session, shell } = require('electron');
const fs = require('fs/promises');
const path = require('path');

app.commandLine.appendSwitch(
  'disable-features',
  'WebRtcHideLocalIpsWithMdns'
);

function createWindow() {
  const window = new BrowserWindow({
    width: 1280,
    height: 820,
    minWidth: 920,
    minHeight: 620,
    backgroundColor: '#05090d',
    title: 'RiverBank Call',
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true
    }
  });
  window.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
  window.webContents.on('will-navigate', (event) => event.preventDefault());
  window.loadFile('index.html');
}

function safeDownloadName(value) {
  const cleaned = path.basename(String(value || 'RiverBank-report.md'))
    .replace(/[<>:"/\\|?*\x00-\x1f]/g, '_')
    .trim();
  return cleaned || 'RiverBank-report.md';
}

ipcMain.handle('download-report', async (_event, request) => {
  const target = new URL(String(request?.url || ''));
  if (!['http:', 'https:'].includes(target.protocol)) throw new Error('不支持的下载地址');
  const token = String(request?.token || '').trim();
  if (token.length < 16) throw new Error('配对令牌无效');
  const filename = safeDownloadName(request?.filename);
  const response = await net.fetch(target.toString(), {
    headers: { Authorization: `Bearer ${token}` }
  });
  if (!response.ok) throw new Error((await response.text()) || `下载失败：${response.status}`);
  const bytes = Buffer.from(await response.arrayBuffer());
  if (bytes.length > 64 * 1024 * 1024) throw new Error('报告文件超过 64 MB 限制');
  const result = await dialog.showSaveDialog({
    title: '保存 RiverBank 报告',
    defaultPath: path.join(app.getPath('downloads'), filename),
    buttonLabel: '保存'
  });
  if (result.canceled || !result.filePath) return { canceled: true };
  await fs.writeFile(result.filePath, bytes, { flag: 'w' });
  return { canceled: false, path: result.filePath, size: bytes.length };
});

ipcMain.handle('open-external', async (_event, value) => {
  const target = new URL(String(value || ''));
  if (!['http:', 'https:'].includes(target.protocol)) throw new Error('不支持的链接地址');
  await shell.openExternal(target.toString());
  return { ok: true };
});

app.whenReady().then(() => {
  session.defaultSession.setPermissionCheckHandler((_webContents, permission) => {
    return permission === 'media';
  });
  session.defaultSession.setPermissionRequestHandler(
    (_webContents, permission, callback) => callback(permission === 'media')
  );
  createWindow();
  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on('window-all-closed', () => app.quit());
