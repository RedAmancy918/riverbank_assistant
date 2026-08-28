const remoteVideo = document.getElementById('remoteVideo');
const localVideo = document.getElementById('localVideo');
const emptyState = document.getElementById('emptyState');
const connectionPill = document.getElementById('connectionPill');
const connectionLabel = document.getElementById('connectionLabel');
const connectButton = document.getElementById('connectButton');
const muteButton = document.getElementById('muteButton');
const cameraButton = document.getElementById('cameraButton');
const serverUrl = document.getElementById('serverUrl');
const pairingToken = document.getElementById('pairingToken');
const cameraSelect = document.getElementById('cameraSelect');
const microphoneSelect = document.getElementById('microphoneSelect');
const refreshDevices = document.getElementById('refreshDevices');
const message = document.getElementById('message');
const networkStats = document.getElementById('networkStats');
const callTab = document.getElementById('callTab');
const reportsTab = document.getElementById('reportsTab');
const callView = document.getElementById('callView');
const reportsView = document.getElementById('reportsView');
const reportCount = document.getElementById('reportCount');
const refreshReports = document.getElementById('refreshReports');
const reportListStatus = document.getElementById('reportListStatus');
const reportList = document.getElementById('reportList');
const reportPreviewEmpty = document.getElementById('reportPreviewEmpty');
const reportPreview = document.getElementById('reportPreview');
const reportTitle = document.getElementById('reportTitle');
const reportMeta = document.getElementById('reportMeta');
const reportContent = document.getElementById('reportContent');
const downloadReport = document.getElementById('downloadReport');

let peerConnection = null;
let localStream = null;
let remoteStream = null;
let sessionId = null;
let statsTimer = null;
let previousStats = new Map();
let currentView = 'call';
let reports = [];
let selectedReport = null;
let reportsLoading = false;
let reportRequestId = 0;

function endpoint(path) {
  return `${serverUrl.value.trim().replace(/\/$/, '')}${path}`;
}

function headers() {
  return {
    'Authorization': `Bearer ${pairingToken.value.trim()}`,
    'Content-Type': 'application/json'
  };
}

function setState(state, label) {
  connectionPill.dataset.state = state;
  connectionLabel.textContent = label;
  const active = state === 'connected' || state === 'connecting';
  connectButton.textContent = active ? '挂断' : '开始通话';
  connectButton.classList.toggle('hangup', active);
  muteButton.disabled = !active;
  cameraButton.disabled = !active;
  serverUrl.disabled = active;
  pairingToken.disabled = active;
  cameraSelect.disabled = active;
  microphoneSelect.disabled = active;
}

function saveSettings() {
  localStorage.setItem('riverbank.serverUrl', serverUrl.value.trim());
  localStorage.setItem('riverbank.pairingToken', pairingToken.value.trim());
  localStorage.setItem('riverbank.cameraId', cameraSelect.value);
  localStorage.setItem('riverbank.microphoneId', microphoneSelect.value);
}

function restoreSettings() {
  serverUrl.value = localStorage.getItem('riverbank.serverUrl') || serverUrl.value;
  pairingToken.value = localStorage.getItem('riverbank.pairingToken') || '';
}

async function enumerateDevices() {
  try {
    const permissionProbe = await navigator.mediaDevices.getUserMedia({ audio: true, video: true });
    permissionProbe.getTracks().forEach((track) => track.stop());
    const devices = await navigator.mediaDevices.enumerateDevices();
    const previousCamera = localStorage.getItem('riverbank.cameraId') || cameraSelect.value;
    const previousMicrophone = localStorage.getItem('riverbank.microphoneId') || microphoneSelect.value;
    cameraSelect.replaceChildren();
    microphoneSelect.replaceChildren();
    devices.filter((item) => item.kind === 'videoinput').forEach((device, index) => {
      const option = new Option(device.label || `摄像头 ${index + 1}`, device.deviceId);
      cameraSelect.add(option);
    });
    devices.filter((item) => item.kind === 'audioinput').forEach((device, index) => {
      const option = new Option(device.label || `麦克风 ${index + 1}`, device.deviceId);
      microphoneSelect.add(option);
    });
    if ([...cameraSelect.options].some((item) => item.value === previousCamera)) cameraSelect.value = previousCamera;
    if ([...microphoneSelect.options].some((item) => item.value === previousMicrophone)) microphoneSelect.value = previousMicrophone;
    message.textContent = '音视频设备已就绪';
  } catch (error) {
    message.textContent = `设备访问失败：${error.message}`;
  }
}

function waitForIceGathering(pc, timeoutMs = 8000) {
  if (pc.iceGatheringState === 'complete') return Promise.resolve();
  return new Promise((resolve) => {
    const timer = setTimeout(resolve, timeoutMs);
    const listener = () => {
      if (pc.iceGatheringState === 'complete') {
        clearTimeout(timer);
        pc.removeEventListener('icegatheringstatechange', listener);
        resolve();
      }
    };
    pc.addEventListener('icegatheringstatechange', listener);
  });
}

async function startCall() {
  const token = pairingToken.value.trim();
  if (token.length < 16) throw new Error('请输入至少 16 字符的配对令牌');
  saveSettings();
  setState('connecting', '连接中');
  message.textContent = '正在打开本机摄像头与麦克风…';
  localStream = await navigator.mediaDevices.getUserMedia({
    video: {
      deviceId: cameraSelect.value ? { exact: cameraSelect.value } : undefined,
      width: { ideal: 1280 },
      height: { ideal: 720 },
      frameRate: { ideal: 24, max: 30 }
    },
    audio: {
      deviceId: microphoneSelect.value ? { exact: microphoneSelect.value } : undefined,
      echoCancellation: true,
      noiseSuppression: true,
      autoGainControl: true,
      channelCount: 1
    }
  });
  localVideo.srcObject = localStream;
  remoteStream = new MediaStream();
  remoteVideo.srcObject = remoteStream;
  peerConnection = new RTCPeerConnection({ iceServers: [] });
  localStream.getTracks().forEach((track) => peerConnection.addTrack(track, localStream));
  peerConnection.addEventListener('track', (event) => {
    if (event.streams.length) {
      event.streams[0].getTracks().forEach((track) => remoteStream.addTrack(track));
    } else {
      remoteStream.addTrack(event.track);
    }
    emptyState.classList.add('hidden');
    remoteVideo.play().catch(() => {});
  });
  peerConnection.addEventListener('connectionstatechange', () => {
    const state = peerConnection?.connectionState || 'closed';
    if (state === 'connected') {
      setState('connected', '通话中');
      message.textContent = '音视频通道已连接';
      beginStats();
    } else if (['failed', 'disconnected', 'closed'].includes(state)) {
      if (state !== 'closed') message.textContent = `连接状态：${state}`;
      if (state === 'failed') setState('failed', '连接失败');
    }
  });
  const offer = await peerConnection.createOffer({ offerToReceiveAudio: true, offerToReceiveVideo: true });
  await peerConnection.setLocalDescription(offer);
  await waitForIceGathering(peerConnection);
  message.textContent = '正在与 RiverBank 协商媒体通道…';
  const response = await fetch(endpoint('/api/v1/offer'), {
    method: 'POST',
    headers: headers(),
    body: JSON.stringify({
      sdp: peerConnection.localDescription.sdp,
      type: peerConnection.localDescription.type,
      device_name: `RiverBank Call on ${navigator.userAgentData?.platform || 'Windows'}`
    })
  });
  if (!response.ok) throw new Error((await response.text()) || `服务器返回 ${response.status}`);
  const answer = await response.json();
  sessionId = answer.session_id;
  await peerConnection.setRemoteDescription({ sdp: answer.sdp, type: answer.type });
}

async function hangup({ notify = true } = {}) {
  clearInterval(statsTimer);
  statsTimer = null;
  previousStats.clear();
  if (notify && pairingToken.value.trim()) {
    fetch(endpoint('/api/v1/hangup'), {
      method: 'POST',
      headers: headers(),
      body: JSON.stringify({ session_id: sessionId }),
      keepalive: true
    }).catch(() => {});
  }
  if (peerConnection) {
    peerConnection.close();
    peerConnection = null;
  }
  if (localStream) localStream.getTracks().forEach((track) => track.stop());
  localStream = null;
  remoteStream = null;
  localVideo.srcObject = null;
  remoteVideo.srcObject = null;
  sessionId = null;
  emptyState.classList.remove('hidden');
  networkStats.textContent = '—';
  muteButton.classList.remove('active');
  cameraButton.classList.remove('active');
  muteButton.querySelector('span:last-child').textContent = '静音';
  cameraButton.querySelector('span:last-child').textContent = '关闭画面';
  setState('idle', '未连接');
  message.textContent = '通话已结束';
}

function toggleTrack(kind, button, enabledLabel, disabledLabel) {
  const tracks = localStream?.getTracks().filter((track) => track.kind === kind) || [];
  if (!tracks.length) return;
  const enabled = !tracks[0].enabled;
  tracks.forEach((track) => { track.enabled = enabled; });
  button.classList.toggle('active', !enabled);
  button.querySelector('span:last-child').textContent = enabled ? enabledLabel : disabledLabel;
}

async function updateStats() {
  if (!peerConnection || peerConnection.connectionState !== 'connected') return;
  const stats = await peerConnection.getStats();
  let rtt = null;
  let inbound = 0;
  let outbound = 0;
  stats.forEach((report) => {
    if (report.type === 'candidate-pair' && report.state === 'succeeded' && report.nominated) rtt = report.currentRoundTripTime;
    if (report.type === 'inbound-rtp' && !report.isRemote) inbound += report.bytesReceived || 0;
    if (report.type === 'outbound-rtp' && !report.isRemote) outbound += report.bytesSent || 0;
  });
  const now = performance.now();
  const previous = previousStats.get('totals');
  let bitrateText = '';
  if (previous) {
    const seconds = Math.max((now - previous.now) / 1000, 0.1);
    const down = ((inbound - previous.inbound) * 8 / seconds / 1_000_000).toFixed(1);
    const up = ((outbound - previous.outbound) * 8 / seconds / 1_000_000).toFixed(1);
    bitrateText = `↓ ${down}  ↑ ${up} Mbps`;
  }
  previousStats.set('totals', { now, inbound, outbound });
  const rttText = rtt == null ? '' : `  ·  ${Math.round(rtt * 1000)} ms`;
  networkStats.textContent = `${bitrateText}${rttText}`.trim() || '正在统计网络…';
}

function beginStats() {
  clearInterval(statsTimer);
  statsTimer = setInterval(() => updateStats().catch(() => {}), 1500);
}

function escapeHtml(value) {
  return String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#039;');
}

function renderInlineMarkdown(value) {
  let text = escapeHtml(value);
  text = text.replace(/`([^`]+)`/g, '<code>$1</code>');
  text = text.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  text = text.replace(/\*([^*]+)\*/g, '<em>$1</em>');
  text = text.replace(
    /\[([^\]]+)]\((https?:\/\/[^\s)]+)\)/g,
    '<a href="$2" data-external-url="$2">$1</a>'
  );
  return text;
}

function renderMarkdown(source) {
  const lines = String(source || '').replace(/\r\n/g, '\n').split('\n');
  const output = [];
  let listType = null;
  let codeLines = null;

  const closeList = () => {
    if (listType) output.push(`</${listType}>`);
    listType = null;
  };

  for (const line of lines) {
    if (line.trim().startsWith('```')) {
      closeList();
      if (codeLines === null) {
        codeLines = [];
      } else {
        output.push(`<pre><code>${escapeHtml(codeLines.join('\n'))}</code></pre>`);
        codeLines = null;
      }
      continue;
    }
    if (codeLines !== null) {
      codeLines.push(line);
      continue;
    }
    const trimmed = line.trim();
    if (!trimmed) {
      closeList();
      continue;
    }
    const heading = /^(#{1,3})\s+(.+)$/.exec(trimmed);
    if (heading) {
      closeList();
      const level = heading[1].length;
      output.push(`<h${level}>${renderInlineMarkdown(heading[2])}</h${level}>`);
      continue;
    }
    if (/^([-*_])\1{2,}$/.test(trimmed)) {
      closeList();
      output.push('<hr>');
      continue;
    }
    const unordered = /^[-*+]\s+(.+)$/.exec(trimmed);
    const ordered = /^\d+[.)]\s+(.+)$/.exec(trimmed);
    if (unordered || ordered) {
      const type = unordered ? 'ul' : 'ol';
      if (listType !== type) {
        closeList();
        output.push(`<${type}>`);
        listType = type;
      }
      output.push(`<li>${renderInlineMarkdown((unordered || ordered)[1])}</li>`);
      continue;
    }
    if (trimmed.startsWith('> ')) {
      closeList();
      output.push(`<blockquote>${renderInlineMarkdown(trimmed.slice(2))}</blockquote>`);
      continue;
    }
    closeList();
    output.push(`<p>${renderInlineMarkdown(trimmed)}</p>`);
  }
  closeList();
  if (codeLines !== null) output.push(`<pre><code>${escapeHtml(codeLines.join('\n'))}</code></pre>`);
  return output.join('\n');
}

function formatReportDate(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '时间未知';
  return new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit'
  }).format(date);
}

function formatFileSize(bytes) {
  const value = Number(bytes || 0);
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / 1024 / 1024).toFixed(1)} MB`;
}

function setView(view) {
  currentView = view === 'reports' ? 'reports' : 'call';
  document.body.dataset.view = currentView;
  callTab.classList.toggle('active', currentView === 'call');
  reportsTab.classList.toggle('active', currentView === 'reports');
  callView.classList.toggle('hidden', currentView !== 'call');
  reportsView.classList.toggle('hidden', currentView !== 'reports');
  if (currentView === 'reports') loadReports().catch(() => {});
}

function reportHeaders() {
  return { Authorization: `Bearer ${pairingToken.value.trim()}` };
}

function renderReportList() {
  reportList.replaceChildren();
  reportCount.textContent = String(reports.length);
  if (!reports.length) {
    const empty = document.createElement('div');
    empty.className = 'report-list-empty';
    empty.textContent = '还没有任务报告。通过树莓派语音助手生成后，会自动出现在这里。';
    reportList.append(empty);
    return;
  }
  reports.forEach((report) => {
    const card = document.createElement('button');
    card.type = 'button';
    card.className = 'report-card';
    card.classList.toggle('active', selectedReport?.id === report.id);
    const title = document.createElement('span');
    title.className = 'report-card-title';
    title.textContent = report.title || report.filename;
    const summary = document.createElement('span');
    summary.className = 'report-card-summary';
    summary.textContent = report.summary || 'Markdown 任务报告';
    const meta = document.createElement('span');
    meta.className = 'report-card-meta';
    meta.textContent = `${formatReportDate(report.modified_at)}  ·  ${formatFileSize(report.size_bytes)}`;
    card.append(title, summary, meta);
    card.addEventListener('click', () => openReport(report));
    reportList.append(card);
  });
}

async function loadReports({ silent = false } = {}) {
  if (reportsLoading) return;
  const token = pairingToken.value.trim();
  if (token.length < 16) {
    reportListStatus.textContent = '请先填写配对令牌';
    if (!silent) message.textContent = '报告库需要树莓派地址和配对令牌';
    return;
  }
  saveSettings();
  reportsLoading = true;
  refreshReports.disabled = true;
  if (!silent) reportListStatus.textContent = '正在读取树莓派报告…';
  try {
    const response = await fetch(endpoint('/api/v1/reports?limit=300'), {
      headers: reportHeaders()
    });
    if (!response.ok) throw new Error((await response.text()) || `服务器返回 ${response.status}`);
    const payload = await response.json();
    reports = Array.isArray(payload.reports) ? payload.reports : [];
    if (selectedReport) {
      selectedReport = reports.find((item) => item.id === selectedReport.id) || null;
    }
    reportListStatus.textContent = `共 ${reports.length} 份报告 · 只读同步`;
    renderReportList();
    if (!selectedReport && reports.length) await openReport(reports[0]);
    if (!reports.length) {
      reportPreview.classList.add('hidden');
      reportPreviewEmpty.classList.remove('hidden');
    }
    if (!silent) message.textContent = `已同步 ${reports.length} 份任务报告`;
  } catch (error) {
    reportListStatus.textContent = `读取失败：${error.message}`;
    if (!silent) message.textContent = `报告同步失败：${error.message}`;
  } finally {
    reportsLoading = false;
    refreshReports.disabled = false;
  }
}

async function openReport(report) {
  selectedReport = report;
  renderReportList();
  reportPreviewEmpty.classList.add('hidden');
  reportPreview.classList.remove('hidden');
  reportTitle.textContent = report.title || report.filename;
  reportMeta.textContent = '正在读取正文…';
  reportContent.innerHTML = '<p>正在从树莓派读取报告…</p>';
  downloadReport.disabled = true;
  const requestId = ++reportRequestId;
  try {
    const response = await fetch(endpoint(`/api/v1/reports/${encodeURIComponent(report.id)}`), {
      headers: reportHeaders()
    });
    if (!response.ok) throw new Error((await response.text()) || `服务器返回 ${response.status}`);
    const payload = await response.json();
    if (requestId !== reportRequestId) return;
    reportMeta.textContent = `${formatReportDate(payload.modified_at)}  ·  ${formatFileSize(payload.size_bytes)}  ·  ${payload.filename}`;
    reportContent.innerHTML = renderMarkdown(payload.content);
    downloadReport.disabled = false;
  } catch (error) {
    if (requestId !== reportRequestId) return;
    reportMeta.textContent = '正文读取失败';
    reportContent.textContent = error.message;
    message.textContent = `报告读取失败：${error.message}`;
  }
}

async function downloadSelectedReport() {
  if (!selectedReport || !window.riverbankDesktop?.downloadReport) return;
  downloadReport.disabled = true;
  try {
    const result = await window.riverbankDesktop.downloadReport({
      url: endpoint(`/api/v1/reports/${encodeURIComponent(selectedReport.id)}/download`),
      token: pairingToken.value.trim(),
      filename: selectedReport.filename
    });
    if (!result.canceled) message.textContent = `报告已保存：${result.path}`;
  } catch (error) {
    message.textContent = `下载失败：${error.message}`;
  } finally {
    downloadReport.disabled = false;
  }
}

callTab.addEventListener('click', () => setView('call'));
reportsTab.addEventListener('click', () => setView('reports'));
refreshReports.addEventListener('click', () => loadReports());
downloadReport.addEventListener('click', downloadSelectedReport);
reportContent.addEventListener('click', (event) => {
  const link = event.target.closest('a[data-external-url]');
  if (!link) return;
  event.preventDefault();
  window.riverbankDesktop?.openExternal(link.dataset.externalUrl).catch((error) => {
    message.textContent = `无法打开来源链接：${error.message}`;
  });
});
serverUrl.addEventListener('change', () => {
  saveSettings();
  reports = [];
  selectedReport = null;
  renderReportList();
  if (currentView === 'reports') loadReports();
});
pairingToken.addEventListener('change', () => {
  saveSettings();
  if (currentView === 'reports') loadReports();
});

connectButton.addEventListener('click', async () => {
  if (peerConnection) {
    await hangup();
    return;
  }
  try {
    await startCall();
  } catch (error) {
    message.textContent = `连接失败：${error.message}`;
    await hangup({ notify: false });
    setState('failed', '连接失败');
  }
});
muteButton.addEventListener('click', () => toggleTrack('audio', muteButton, '静音', '取消静音'));
cameraButton.addEventListener('click', () => toggleTrack('video', cameraButton, '关闭画面', '打开画面'));
refreshDevices.addEventListener('click', enumerateDevices);
window.addEventListener('beforeunload', () => hangup());

restoreSettings();
setState('idle', '未连接');
setView('call');
enumerateDevices();
setInterval(() => {
  if (currentView === 'reports') loadReports({ silent: true });
}, 15000);
