const remoteVideo = document.getElementById('remoteVideo');
const localVideo = document.getElementById('localVideo');
const launchView = document.getElementById('launchView');
const loginView = document.getElementById('loginView');
const loginForm = document.getElementById('loginForm');
const loginButton = document.getElementById('loginButton');
const loginMessage = document.getElementById('loginMessage');
const appShell = document.getElementById('appShell');
const logoutButton = document.getElementById('logoutButton');
const emptyState = document.getElementById('emptyState');
const connectionPill = document.getElementById('connectionPill');
const connectionLabel = document.getElementById('connectionLabel');
const connectButton = document.getElementById('connectButton');
const muteButton = document.getElementById('muteButton');
const cameraButton = document.getElementById('cameraButton');
const serverUrl = document.getElementById('serverUrl');
const accountUsername = document.getElementById('accountUsername');
const accountPassword = document.getElementById('accountPassword');
const loginModeButton = document.getElementById('loginModeButton');
const registerModeButton = document.getElementById('registerModeButton');
const registerFields = document.getElementById('registerFields');
const registerPasswordConfirmation = document.getElementById('registerPasswordConfirmation');
const registerDisplayName = document.getElementById('registerDisplayName');
const registrationPasscode = document.getElementById('registrationPasscode');
const registerGuidance = document.getElementById('registerGuidance');
const accountDisplayName = document.getElementById('accountDisplayName');
const setupFields = document.getElementById('setupFields');
const setupCredential = document.getElementById('setupCredential');
const toggleHostSettings = document.getElementById('toggleHostSettings');
const hostSettings = document.getElementById('hostSettings');
const accountButton = document.getElementById('accountButton');
const accountDialog = document.getElementById('accountDialog');
const closeAccountDialog = document.getElementById('closeAccountDialog');
const currentAccountName = document.getElementById('currentAccountName');
const currentAccountMeta = document.getElementById('currentAccountMeta');
const desktopVersion = document.getElementById('desktopVersion');
const changePasswordForm = document.getElementById('changePasswordForm');
const currentPassword = document.getElementById('currentPassword');
const newPassword = document.getElementById('newPassword');
const adminUsersSection = document.getElementById('adminUsersSection');
const accountUserList = document.getElementById('accountUserList');
const createUserForm = document.getElementById('createUserForm');
const newUserUsername = document.getElementById('newUserUsername');
const newUserDisplayName = document.getElementById('newUserDisplayName');
const newUserPassword = document.getElementById('newUserPassword');
const newUserRole = document.getElementById('newUserRole');
const accountMessage = document.getElementById('accountMessage');
const cameraSelect = document.getElementById('cameraSelect');
const microphoneSelect = document.getElementById('microphoneSelect');
const refreshDevices = document.getElementById('refreshDevices');
const message = document.getElementById('message');
const networkStats = document.getElementById('networkStats');
const chatTab = document.getElementById('chatTab');
const callTab = document.getElementById('callTab');
const reportsTab = document.getElementById('reportsTab');
const chatView = document.getElementById('chatView');
const callView = document.getElementById('callView');
const reportsView = document.getElementById('reportsView');
const newChatButton = document.getElementById('newChatButton');
const chatHistory = document.getElementById('chatHistory');
const chatTitle = document.getElementById('chatTitle');
const chatMessages = document.getElementById('chatMessages');
const chatComposer = document.getElementById('chatComposer');
const chatInput = document.getElementById('chatInput');
const chatSend = document.getElementById('chatSend');
const chatAttach = document.getElementById('chatAttach');
const chatAttachmentInput = document.getElementById('chatAttachmentInput');
const chatAttachmentTray = document.getElementById('chatAttachmentTray');
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
let currentView = 'chat';
let reports = [];
let selectedReport = null;
let reportsLoading = false;
let reportRequestId = 0;
let chats = [];
let activeChatId = '';
let currentChatMessages = [];
let chatLoading = false;
let chatPollTimer = null;
let authenticated = false;
let devicesInitialized = false;
let pendingChatAttachments = [];
let authToken = '';
let currentUser = null;
let authenticationMode = 'login';
const attachmentObjectUrls = new Map();
const DEFAULT_EDGE_HOST = 'https://riverbank-tech.tail0acdab.ts.net/assistant';
const CHAT_ATTACHMENT_EXTENSIONS = new Set([
  'jpg', 'jpeg', 'png', 'webp', 'pdf', 'md', 'markdown', 'txt'
]);
const CHAT_IMAGE_EXTENSIONS = new Set(['jpg', 'jpeg', 'png', 'webp']);
const MAX_CHAT_ATTACHMENT_BYTES = 15 * 1024 * 1024;
const MAX_CHAT_ATTACHMENT_TOTAL_BYTES = 30 * 1024 * 1024;
const MAX_CHAT_ATTACHMENTS = 4;

function endpoint(path) {
  return `${serverUrl.value.trim().replace(/\/$/, '')}${path}`;
}

function headers() {
  return {
    'Authorization': `Bearer ${authToken}`,
    'Content-Type': 'application/json'
  };
}

function setState(state, label) {
  connectionPill.dataset.state = state;
  connectionLabel.textContent = label;
  const active = state === 'connected' || state === 'connecting';
  connectButton.textContent = active ? '结束通话' : '开始通话';
  connectButton.classList.toggle('hangup', active);
  muteButton.disabled = !active;
  cameraButton.disabled = !active;
  cameraSelect.disabled = active;
  microphoneSelect.disabled = active;
}

function saveSettings() {
  localStorage.setItem('riverbank.serverUrl', serverUrl.value.trim());
  localStorage.setItem('riverbank.username', accountUsername.value.trim());
  if (cameraSelect.value) localStorage.setItem('riverbank.cameraId', cameraSelect.value);
  if (microphoneSelect.value) localStorage.setItem('riverbank.microphoneId', microphoneSelect.value);
}

function migrateEdgeHost(value) {
  const candidate = String(value || '').trim().replace(/\/$/, '');
  if (!candidate) return DEFAULT_EDGE_HOST;
  if (candidate === 'https://riverbank-tech.tail0acdab.ts.net') return DEFAULT_EDGE_HOST;
  if (candidate === 'http://riverbank-tech.tail0acdab.ts.net/assistant') return DEFAULT_EDGE_HOST;
  return candidate;
}

function restoreSettings() {
  const savedHost = localStorage.getItem('riverbank.serverUrl');
  serverUrl.value = migrateEdgeHost(savedHost || serverUrl.value);
  if (savedHost !== serverUrl.value) {
    localStorage.setItem('riverbank.serverUrl', serverUrl.value);
  }
  accountUsername.value = localStorage.getItem('riverbank.username') || '';
}

function setHostSettingsVisible(visible) {
  hostSettings.classList.toggle('hidden', !visible);
  toggleHostSettings.setAttribute('aria-expanded', String(visible));
  toggleHostSettings.textContent = visible ? '收起连接设置' : '连接设置';
}

async function setAuthenticationMode(mode) {
  authenticationMode = mode === 'register' ? 'register' : 'login';
  const registering = authenticationMode === 'register';
  loginModeButton.classList.toggle('active', !registering);
  registerModeButton.classList.toggle('active', registering);
  loginModeButton.setAttribute('aria-selected', String(!registering));
  registerModeButton.setAttribute('aria-selected', String(registering));
  registerFields.classList.toggle('hidden', !registering);
  setupFields.classList.add('hidden');
  accountPassword.autocomplete = registering ? 'new-password' : 'current-password';
  loginButton.textContent = registering ? '注册并进入' : '登录';
  accountPassword.value = '';
  registerPasswordConfirmation.value = '';
  registerDisplayName.value = '';
  registrationPasscode.value = '';
  registerPasswordConfirmation.value = '';
  registrationPasscode.value = '';
  loginMessage.classList.remove('error');
  loginMessage.textContent = registering ? '使用注册通行码创建独立账号' : '等待连接';
  registerGuidance.textContent = '';
  if (!registering) return;
  try {
    const config = await fetchAuthConfig();
    if (!config.registration_enabled) {
      registerGuidance.textContent = '此设备当前未开放账号注册';
    } else if (config.initial_admin_username) {
      registerGuidance.textContent = `请先注册 ${config.initial_admin_username} 管理员账号。`;
      accountUsername.value = config.initial_admin_username;
    }
  } catch (_error) {
    // Submission provides the complete connection error.
  }
}

function activeChatStorageKey() {
  return currentUser?.id ? `riverbank.activeChatId.${currentUser.id}` : '';
}

function saveActiveChatId() {
  const key = activeChatStorageKey();
  if (!key) return;
  if (activeChatId) localStorage.setItem(key, activeChatId);
  else localStorage.removeItem(key);
}

async function fetchAuthConfig() {
  const response = await fetch(endpoint('/api/v1/auth/config'), { cache: 'no-store' });
  if (!response.ok) throw new Error((await response.text()) || `设备返回 ${response.status}`);
  return response.json();
}

async function completeLogin(payload, { persist = true } = {}) {
  authToken = String(payload.token || '');
  currentUser = payload.user || null;
  if (!authToken.startsWith('rbs_') || !currentUser?.id) throw new Error('设备返回了无效的账号会话');
  accountPassword.value = '';
  setupCredential.value = '';
  setupFields.classList.add('hidden');
  accountUsername.value = currentUser.username || accountUsername.value;
  saveSettings();
  if (persist) {
    await window.riverbankDesktop?.saveAuthSession({
      server: serverUrl.value.trim(),
      username: currentUser.username,
      token: authToken
    });
  }
  activeChatId = localStorage.getItem(activeChatStorageKey()) || '';
  authenticated = true;
  loginView.classList.add('hidden');
  appShell.classList.remove('hidden');
  accountButton.textContent = currentUser.display_name || currentUser.username;
  loginMessage.textContent = '连接成功';
  message.textContent = `已登录为 ${currentUser.display_name || currentUser.username}`;
  setState('idle', '未通话');
  setView('chat');
}

async function enumerateDevices({ reportStatus = true } = {}) {
  const previousCamera = localStorage.getItem('riverbank.cameraId') || cameraSelect.value;
  const previousMicrophone = localStorage.getItem('riverbank.microphoneId') || microphoneSelect.value;
  const permissionErrors = [];
  const probeTracks = [];
  for (const kind of ['audio', 'video']) {
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: kind === 'audio',
        video: kind === 'video'
      });
      probeTracks.push(...stream.getTracks());
    } catch (error) {
      permissionErrors.push({ kind, error });
    }
  }
  probeTracks.forEach((track) => track.stop());

  let devices = [];
  try {
    devices = await navigator.mediaDevices.enumerateDevices();
  } catch (error) {
    permissionErrors.push({ kind: 'all', error });
  }
  const cameras = devices.filter((item) => item.kind === 'videoinput');
  const microphones = devices.filter((item) => item.kind === 'audioinput');
  cameraSelect.replaceChildren();
  microphoneSelect.replaceChildren();
  cameras.forEach((device, index) => {
    cameraSelect.add(new Option(device.label || `摄像头 ${index + 1}`, device.deviceId));
  });
  microphones.forEach((device, index) => {
    microphoneSelect.add(new Option(device.label || `麦克风 ${index + 1}`, device.deviceId));
  });
  if ([...cameraSelect.options].some((item) => item.value === previousCamera)) cameraSelect.value = previousCamera;
  if ([...microphoneSelect.options].some((item) => item.value === previousMicrophone)) microphoneSelect.value = previousMicrophone;
  devicesInitialized = true;

  if (!reportStatus || currentView !== 'call') return { cameras, microphones };
  const missing = [];
  if (!cameras.length) missing.push('摄像头');
  if (!microphones.length) missing.push('麦克风');
  if (missing.length) {
    message.textContent = `本机未找到${missing.join('和')}；Chat 功能不受影响`;
  } else if (permissionErrors.length) {
    message.textContent = '音视频设备已找到，但部分权限未开放';
  } else {
    message.textContent = '音视频设备已就绪';
  }
  return { cameras, microphones };
}

async function login() {
  const address = migrateEdgeHost(serverUrl.value);
  const username = accountUsername.value.trim();
  const password = accountPassword.value;
  let target;
  try {
    target = new URL(address);
  } catch (_error) {
    throw new Error('请输入正确的 RiverBank Edge Host');
  }
  if (target.protocol !== 'https:') throw new Error('账号登录必须使用 HTTPS Edge Host');
  if (username.length < 3) throw new Error('请输入用户名');
  if (password.length < 10) throw new Error('密码至少需要 10 个字符');

  serverUrl.value = address;
  loginButton.disabled = true;
  loginMessage.classList.remove('error');
  loginMessage.textContent = authenticationMode === 'register' ? '正在安全注册…' : '正在安全登录…';
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 9000);
  try {
    const config = await fetchAuthConfig();
    if (authenticationMode === 'register') {
      if (!config.registration_enabled) throw new Error('此设备当前未开放账号注册');
      if (password !== registerPasswordConfirmation.value) throw new Error('两次输入的密码不一致');
      const passcode = registrationPasscode.value;
      if (!passcode) throw new Error('请输入注册通行码');
      if (config.initial_admin_username && username.toLowerCase() !== String(config.initial_admin_username).toLowerCase()) {
        throw new Error(`请先注册管理员账号 ${config.initial_admin_username}`);
      }
      const response = await fetch(endpoint('/api/v1/auth/register'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          username,
          password,
          display_name: registerDisplayName.value.trim(),
          registration_passcode: passcode,
          device_name: `RiverBank Call on ${navigator.platform || 'Desktop'}`
        }),
        signal: controller.signal
      });
      if (!response.ok) {
        const detail = await response.text();
        if (response.status === 401) throw new Error('注册通行码不正确');
        if (response.status === 409 && /administrator/i.test(detail)) {
          throw new Error(`请先注册管理员账号 ${config.initial_admin_username || 'Geo'}`);
        }
        if (response.status === 400 && /already in use/i.test(detail)) throw new Error('这个用户名已经被使用');
        if (response.status === 426) throw new Error('账号注册必须通过 HTTPS 地址');
        throw new Error(detail || `设备返回 ${response.status}`);
      }
      await completeLogin(await response.json());
      return;
    }
    const bootstrap = Boolean(config.bootstrap_required);
    setupFields.classList.toggle('hidden', !bootstrap);
    const credential = setupCredential.value.trim();
    if (bootstrap && credential.length < 16) {
      throw new Error('首次设置请再输入一次性设备凭据');
    }
    const response = await fetch(endpoint(bootstrap ? '/api/v1/auth/bootstrap' : '/api/v1/auth/login'), {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...(bootstrap ? { Authorization: `Bearer ${credential}` } : {})
      },
      body: JSON.stringify({
        username,
        password,
        display_name: accountDisplayName.value.trim(),
        device_name: `RiverBank Call on ${navigator.platform || 'Desktop'}`
      }),
      signal: controller.signal
    });
    if (!response.ok) {
      if (response.status === 401) throw new Error(bootstrap ? '一次性设备凭据不正确' : '用户名或密码不正确');
      if (response.status === 426) throw new Error('账号密码登录必须通过 HTTPS 地址');
      throw new Error((await response.text()) || `设备返回 ${response.status}`);
    }
    await completeLogin(await response.json());
  } catch (error) {
    const fetchFailed = /failed to fetch|load failed|networkerror/i.test(String(error?.message || ''));
    const detail = error.name === 'AbortError'
      ? '连接超时，请检查 Edge Host 和网络'
      : fetchFailed
        ? '无法连接 RiverBank Edge Host，请检查网络和连接设置中的 HTTPS 地址'
        : error.message;
    if (error.name === 'AbortError' || /Host|HTTPS|连接|网络|地址/.test(detail)) {
      setHostSettingsVisible(true);
    }
    loginMessage.classList.add('error');
    loginMessage.textContent = detail;
    throw new Error(detail);
  } finally {
    clearTimeout(timeout);
    loginButton.disabled = false;
  }
}

async function logout() {
  if (peerConnection) await hangup();
  if (authToken) {
    await fetch(endpoint('/api/v1/auth/logout'), {
      method: 'POST', headers: headers(), body: '{}'
    }).catch(() => {});
  }
  authenticated = false;
  clearTimeout(chatPollTimer);
  chatPollTimer = null;
  clearPendingChatAttachments();
  clearAttachmentObjectUrls();
  chats = [];
  currentChatMessages = [];
  reports = [];
  selectedReport = null;
  authToken = '';
  currentUser = null;
  activeChatId = '';
  await window.riverbankDesktop?.clearAuthSession();
  appShell.classList.add('hidden');
  loginView.classList.remove('hidden');
  loginMessage.classList.remove('error');
  loginMessage.textContent = '已安全登出';
  await setAuthenticationMode('login');
  accountPassword.focus();
}

function setAccountMessage(text, error = false) {
  accountMessage.textContent = text;
  accountMessage.classList.toggle('error', error);
}

function renderAccountUsers(users) {
  accountUserList.replaceChildren();
  users.forEach((user) => {
    const row = document.createElement('div');
    row.className = 'account-user-row';
    const info = document.createElement('div');
    const name = document.createElement('strong');
    name.textContent = user.display_name || user.username;
    const meta = document.createElement('span');
    meta.textContent = `@${user.username} · ${user.role === 'admin' ? '管理员' : '普通用户'}${user.disabled ? ' · 已停用' : ''}`;
    info.append(name, meta);
    row.append(info);
    if (user.id !== currentUser?.id) {
      const toggle = document.createElement('button');
      toggle.type = 'button';
      toggle.textContent = user.disabled ? '启用' : '停用';
      toggle.classList.toggle('enable', user.disabled);
      toggle.addEventListener('click', async () => {
        toggle.disabled = true;
        try {
          const response = await fetch(endpoint(`/api/v1/auth/users/${encodeURIComponent(user.id)}`), {
            method: 'PATCH', headers: headers(), body: JSON.stringify({ disabled: !user.disabled })
          });
          if (!response.ok) throw new Error((await response.text()) || `设备返回 ${response.status}`);
          await loadAccountUsers();
          setAccountMessage(user.disabled ? '账号已启用' : '账号已停用，现有会话已经撤销');
        } catch (error) {
          setAccountMessage(error.message, true);
        } finally {
          toggle.disabled = false;
        }
      });
      row.append(toggle);
    }
    accountUserList.append(row);
  });
}

async function loadAccountUsers() {
  if (currentUser?.role !== 'admin') return;
  const response = await fetch(endpoint('/api/v1/auth/users'), { headers: reportHeaders() });
  if (!response.ok) throw new Error((await response.text()) || `设备返回 ${response.status}`);
  const payload = await response.json();
  renderAccountUsers(Array.isArray(payload.users) ? payload.users : []);
}

async function openAccount() {
  if (!currentUser) return;
  currentAccountName.textContent = currentUser.display_name || currentUser.username;
  currentAccountMeta.textContent = `@${currentUser.username} · ${currentUser.role === 'admin' ? '管理员' : '普通用户'}`;
  adminUsersSection.classList.toggle('hidden', currentUser.role !== 'admin');
  setAccountMessage('');
  accountDialog.showModal();
  if (currentUser.role === 'admin') {
    try {
      await loadAccountUsers();
    } catch (error) {
      setAccountMessage(`用户列表读取失败：${error.message}`, true);
    }
  }
}

async function resumeSavedSession() {
  const saved = await window.riverbankDesktop?.loadAuthSession();
  if (!saved?.token || !saved?.server) return false;
  const migratedServer = migrateEdgeHost(saved.server);
  serverUrl.value = migratedServer;
  localStorage.setItem('riverbank.serverUrl', migratedServer);
  accountUsername.value = saved.username || '';
  authToken = saved.token;
  try {
    const response = await fetch(endpoint('/api/v1/auth/me'), { headers: reportHeaders() });
    if (!response.ok) throw new Error('session expired');
    const payload = await response.json();
    await completeLogin(
      { token: authToken, user: payload.user },
      { persist: migratedServer !== saved.server }
    );
    return true;
  } catch (_error) {
    authToken = '';
    await window.riverbankDesktop?.clearAuthSession();
    return false;
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
  if (!authToken.startsWith('rbs_')) throw new Error('请先登录 RiverBank 账号');
  saveSettings();
  if (!devicesInitialized) await enumerateDevices({ reportStatus: true });
  if (!cameraSelect.options.length || !microphoneSelect.options.length) {
    throw new Error('本机缺少可用的摄像头或麦克风');
  }
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
  if (notify && authToken) {
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
  muteButton.querySelector('.control-label').textContent = '静音';
  muteButton.setAttribute('aria-label', '静音');
  cameraButton.querySelector('.control-label').textContent = '关闭摄像头';
  cameraButton.setAttribute('aria-label', '关闭摄像头');
  setState('idle', '未连接');
  message.textContent = '通话已结束';
}

function toggleTrack(kind, button, enabledLabel, disabledLabel) {
  const tracks = localStream?.getTracks().filter((track) => track.kind === kind) || [];
  if (!tracks.length) return;
  const enabled = !tracks[0].enabled;
  tracks.forEach((track) => { track.enabled = enabled; });
  button.classList.toggle('active', !enabled);
  const label = enabled ? enabledLabel : disabledLabel;
  button.querySelector('.control-label').textContent = label;
  button.setAttribute('aria-label', label);
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

function activeChat() {
  return chats.find((item) => item.id === activeChatId) || null;
}

function chatIsResponding() {
  return currentChatMessages.some((item) => item.role === 'assistant' && ['queued', 'running'].includes(item.state));
}

function fileExtension(name) {
  const match = /\.([^.]+)$/.exec(String(name || '').toLowerCase());
  return match ? match[1] : '';
}

function clearPendingChatAttachments() {
  pendingChatAttachments.forEach((item) => {
    if (item.previewUrl) URL.revokeObjectURL(item.previewUrl);
  });
  pendingChatAttachments = [];
  chatAttachmentInput.value = '';
  renderPendingChatAttachments();
  updateChatComposer();
}

function clearAttachmentObjectUrls() {
  attachmentObjectUrls.forEach((value) => URL.revokeObjectURL(value));
  attachmentObjectUrls.clear();
}

function renderPendingChatAttachments() {
  chatAttachmentTray.replaceChildren();
  chatAttachmentTray.classList.toggle('hidden', pendingChatAttachments.length === 0);
  pendingChatAttachments.forEach((item, index) => {
    const card = document.createElement('div');
    card.className = 'chat-pending-attachment';
    if (item.previewUrl) {
      const preview = document.createElement('img');
      preview.src = item.previewUrl;
      preview.alt = '';
      card.append(preview);
    } else {
      const mark = document.createElement('span');
      mark.className = 'chat-pending-file-mark';
      mark.textContent = fileExtension(item.file.name).toUpperCase().slice(0, 4) || 'FILE';
      card.append(mark);
    }
    const name = document.createElement('span');
    name.className = 'chat-pending-name';
    name.textContent = item.file.name;
    name.title = `${item.file.name} · ${formatFileSize(item.file.size)}`;
    const remove = document.createElement('button');
    remove.type = 'button';
    remove.className = 'chat-pending-remove';
    remove.textContent = '×';
    remove.setAttribute('aria-label', `移除 ${item.file.name}`);
    remove.addEventListener('click', () => {
      const [removed] = pendingChatAttachments.splice(index, 1);
      if (removed?.previewUrl) URL.revokeObjectURL(removed.previewUrl);
      renderPendingChatAttachments();
      updateChatComposer();
    });
    card.append(name, remove);
    chatAttachmentTray.append(card);
  });
}

function addPendingChatAttachments(files) {
  const candidates = Array.from(files || []);
  for (const file of candidates) {
    if (pendingChatAttachments.length >= MAX_CHAT_ATTACHMENTS) {
      message.textContent = '单条消息最多上传 4 个附件';
      break;
    }
    const extension = fileExtension(file.name);
    if (!CHAT_ATTACHMENT_EXTENSIONS.has(extension)) {
      message.textContent = `不支持 ${file.name}；请选择图片、PDF、Markdown 或 TXT`;
      continue;
    }
    if (!file.size || file.size > MAX_CHAT_ATTACHMENT_BYTES) {
      message.textContent = `${file.name} 为空或超过 15 MB`;
      continue;
    }
    const nextTotal = pendingChatAttachments.reduce((sum, item) => sum + item.file.size, 0) + file.size;
    if (nextTotal > MAX_CHAT_ATTACHMENT_TOTAL_BYTES) {
      message.textContent = '单条消息的附件总计不能超过 30 MB';
      continue;
    }
    if (
      CHAT_IMAGE_EXTENSIONS.has(extension)
      && pendingChatAttachments.some((item) => CHAT_IMAGE_EXTENSIONS.has(fileExtension(item.file.name)))
    ) {
      message.textContent = '单条消息最多上传 1 张图片';
      continue;
    }
    pendingChatAttachments.push({
      file,
      previewUrl: CHAT_IMAGE_EXTENSIONS.has(extension) ? URL.createObjectURL(file) : ''
    });
  }
  chatAttachmentInput.value = '';
  renderPendingChatAttachments();
  updateChatComposer();
}

function updateChatComposer() {
  const responding = chatIsResponding();
  chatSend.textContent = responding ? '■' : '↑';
  chatSend.classList.toggle('stop', responding);
  chatSend.disabled = !responding && !chatInput.value.trim() && !pendingChatAttachments.length;
  chatAttach.disabled = responding;
  chatSend.setAttribute('aria-label', responding ? '停止生成' : '发送');
}

function attachmentEndpoint(attachment) {
  const conversationId = attachment.conversation_id || activeChatId;
  return endpoint(
    `/api/v1/chats/${encodeURIComponent(conversationId)}/attachments/${encodeURIComponent(attachment.id)}`
  );
}

async function hydrateAttachmentImage(image, attachment) {
  try {
    let objectUrl = attachmentObjectUrls.get(attachment.id);
    if (!objectUrl) {
      const response = await fetch(attachmentEndpoint(attachment), { headers: reportHeaders() });
      if (!response.ok) throw new Error(`附件读取失败：${response.status}`);
      objectUrl = URL.createObjectURL(await response.blob());
      attachmentObjectUrls.set(attachment.id, objectUrl);
    }
    image.src = objectUrl;
  } catch (_error) {
    image.alt = '图片加载失败';
  }
}

async function downloadChatAttachment(attachment) {
  if (!window.riverbankDesktop?.downloadAttachment) return;
  try {
    const result = await window.riverbankDesktop.downloadAttachment({
      url: attachmentEndpoint(attachment),
      token: authToken,
      filename: attachment.original_name
    });
    if (!result.canceled) message.textContent = `附件已保存：${result.path}`;
  } catch (error) {
    message.textContent = `附件下载失败：${error.message}`;
  }
}

function renderMessageAttachments(attachments) {
  if (!Array.isArray(attachments) || !attachments.length) return null;
  const list = document.createElement('div');
  list.className = 'chat-message-attachments';
  attachments.forEach((attachment) => {
    const card = document.createElement('button');
    card.type = 'button';
    card.className = 'chat-message-attachment';
    card.title = `保存 ${attachment.original_name}`;
    card.addEventListener('click', () => downloadChatAttachment(attachment));
    if (attachment.kind === 'image') {
      card.classList.add('chat-attachment-thumb');
      const preview = document.createElement('img');
      preview.alt = attachment.original_name || '上传的图片';
      hydrateAttachmentImage(preview, attachment);
      card.append(preview);
    } else {
      const mark = document.createElement('span');
      mark.className = 'chat-file-mark';
      mark.textContent = fileExtension(attachment.original_name).toUpperCase().slice(0, 4) || 'FILE';
      const info = document.createElement('span');
      info.className = 'chat-file-info';
      const name = document.createElement('strong');
      name.textContent = attachment.original_name || '附件';
      const size = document.createElement('span');
      size.textContent = formatFileSize(attachment.size_bytes);
      info.append(name, size);
      card.append(mark, info);
    }
    list.append(card);
  });
  return list;
}

function renderChatHistory() {
  chatHistory.replaceChildren();
  if (!chats.length) {
    const empty = document.createElement('p');
    empty.className = 'chat-history-empty';
    empty.textContent = '还没有对话';
    chatHistory.append(empty);
    return;
  }
  chats.forEach((chat) => {
    const row = document.createElement('div');
    row.className = 'chat-history-row';
    row.classList.toggle('active', chat.id === activeChatId);
    const open = document.createElement('button');
    open.type = 'button';
    open.className = 'chat-history-open';
    const title = document.createElement('strong');
    title.textContent = chat.title || '新对话';
    const preview = document.createElement('span');
    preview.textContent = chat.preview || '开始一段对话';
    open.append(title, preview);
    open.addEventListener('click', () => selectChat(chat.id));
    const remove = document.createElement('button');
    remove.type = 'button';
    remove.className = 'chat-history-delete';
    remove.textContent = '×';
    remove.title = '删除对话';
    remove.addEventListener('click', () => deleteChat(chat.id));
    row.append(open, remove);
    chatHistory.append(row);
  });
}

function renderChatMessages() {
  chatMessages.replaceChildren();
  const chat = activeChat();
  chatTitle.textContent = chat?.title || 'RiverBank';
  if (!currentChatMessages.length) {
    const welcome = document.createElement('div');
    welcome.className = 'chat-welcome';
    welcome.innerHTML = '<img src="assets/riverbank-mark.svg" alt="RiverBank"><h2>有什么可以帮你？</h2><p>直接提问或讨论想法；复杂工作可以交给后台任务继续执行。</p>';
    chatMessages.append(welcome);
  } else {
    currentChatMessages.forEach((item, itemIndex) => {
      const row = document.createElement('article');
      row.className = `chat-message ${item.role === 'user' ? 'user' : 'assistant'}`;
      if (item.role === 'assistant') {
        const mark = document.createElement('img');
        mark.src = 'assets/riverbank-mark.svg';
        mark.alt = 'RiverBank';
        row.append(mark);
      }
      const content = document.createElement('div');
      content.className = 'chat-message-content';
      const attachments = renderMessageAttachments(item.attachments);
      if (attachments) content.append(attachments);
      const messageBody = document.createElement('div');
      messageBody.className = 'chat-message-body';
      let showMessageBody = true;
      if (item.content) {
        messageBody.innerHTML = renderMarkdown(item.content);
      } else if (item.role === 'user') {
        showMessageBody = false;
      } else if (['queued', 'running'].includes(item.state)) {
        messageBody.innerHTML = '<span class="chat-thinking"><i></i><i></i><i></i></span>';
      } else if (item.state === 'cancelled') {
        messageBody.innerHTML = '<span class="chat-muted">已停止生成</span>';
      } else {
        messageBody.innerHTML = `<span class="chat-error">${escapeHtml(item.error || '回答失败')}</span>`;
      }
      if (showMessageBody) content.append(messageBody);
      if (item.role === 'assistant' && ['failed', 'cancelled'].includes(item.state)) {
        const previousUser = currentChatMessages
          .slice(0, itemIndex)
          .reverse()
          .find((candidate) => candidate.role === 'user');
        if (previousUser?.content && !previousUser.attachments?.length) {
          const retry = document.createElement('button');
          retry.type = 'button';
          retry.className = 'chat-retry';
          retry.textContent = '重试';
          retry.addEventListener('click', () => {
            retryChatMessage(previousUser.content).catch((error) => {
              message.textContent = `重试失败：${error.message}`;
            });
          });
          content.append(retry);
        }
      }
      row.append(content);
      chatMessages.append(row);
    });
  }
  chatMessages.scrollTop = chatMessages.scrollHeight;
  updateChatComposer();
}

async function loadChats({ silent = false } = {}) {
  if (chatLoading) return;
  if (!authToken) {
    if (!silent) message.textContent = '请先登录 RiverBank 账号';
    return;
  }
  chatLoading = true;
  try {
    const response = await fetch(endpoint('/api/v1/chats?limit=100'), { headers: reportHeaders() });
    if (!response.ok) throw new Error((await response.text()) || `服务器返回 ${response.status}`);
    const payload = await response.json();
    chats = Array.isArray(payload.conversations) ? payload.conversations : [];
    if (activeChatId && !chats.some((item) => item.id === activeChatId)) activeChatId = '';
    if (!activeChatId && chats.length) activeChatId = chats[0].id;
    saveActiveChatId();
    renderChatHistory();
    if (activeChatId) await loadChatMessages({ silent: true });
    else renderChatMessages();
    if (!silent) message.textContent = 'Chat 已连接';
  } catch (error) {
    if (!silent) message.textContent = `Chat 连接失败：${error.message}`;
  } finally {
    chatLoading = false;
  }
}

async function createChat() {
  const response = await fetch(endpoint('/api/v1/chats'), {
    method: 'POST',
    headers: headers(),
    body: JSON.stringify({ title: '', source: 'desktop', device_name: navigator.platform || 'Desktop' })
  });
  if (!response.ok) throw new Error((await response.text()) || `服务器返回 ${response.status}`);
  const payload = await response.json();
  const chat = payload.conversation;
  chats.unshift(chat);
  activeChatId = chat.id;
  currentChatMessages = [];
  saveActiveChatId();
  renderChatHistory();
  renderChatMessages();
  chatInput.focus();
  return chat;
}

async function selectChat(chatId) {
  if (chatIsResponding() && chatId !== activeChatId) return;
  if (chatId !== activeChatId) clearPendingChatAttachments();
  activeChatId = chatId;
  saveActiveChatId();
  renderChatHistory();
  await loadChatMessages();
}

async function loadChatMessages({ silent = false } = {}) {
  if (!activeChatId) return;
  try {
    const response = await fetch(endpoint(`/api/v1/chats/${encodeURIComponent(activeChatId)}/messages`), {
      headers: reportHeaders()
    });
    if (!response.ok) throw new Error((await response.text()) || `服务器返回 ${response.status}`);
    const payload = await response.json();
    currentChatMessages = Array.isArray(payload.messages) ? payload.messages : [];
    const index = chats.findIndex((item) => item.id === payload.conversation.id);
    if (index >= 0) chats[index] = { ...chats[index], ...payload.conversation };
    renderChatHistory();
    renderChatMessages();
    scheduleChatPoll();
  } catch (error) {
    if (!silent) message.textContent = `读取对话失败：${error.message}`;
  }
}

function scheduleChatPoll() {
  clearTimeout(chatPollTimer);
  chatPollTimer = null;
  if (!chatIsResponding() || currentView !== 'chat') return;
  chatPollTimer = setTimeout(() => loadChatMessages({ silent: true }), 550);
}

async function sendChatMessage() {
  const content = chatInput.value.trim();
  if ((!content && !pendingChatAttachments.length) || chatIsResponding()) return;
  if (!activeChatId) await createChat();
  const form = new FormData();
  form.append('content', content);
  pendingChatAttachments.forEach((item) => form.append('files', item.file, item.file.name));
  const response = await fetch(endpoint(`/api/v1/chats/${encodeURIComponent(activeChatId)}/messages`), {
    method: 'POST',
    headers: reportHeaders(),
    body: form
  });
  if (!response.ok) {
    throw new Error((await response.text()) || `服务器返回 ${response.status}`);
  }
  const payload = await response.json();
  chatInput.value = '';
  clearPendingChatAttachments();
  resizeChatInput();
  currentChatMessages.push(payload.user_message, payload.assistant_message);
  renderChatMessages();
  await loadChats({ silent: true });
  scheduleChatPoll();
}

async function retryChatMessage(content) {
  if (chatIsResponding()) return;
  chatInput.value = content;
  resizeChatInput();
  await sendChatMessage();
}

async function stopChatResponse() {
  const assistant = [...currentChatMessages].reverse().find((item) => item.role === 'assistant' && ['queued', 'running'].includes(item.state));
  if (!assistant || !activeChatId) return;
  const response = await fetch(
    endpoint(`/api/v1/chats/${encodeURIComponent(activeChatId)}/messages/${encodeURIComponent(assistant.id)}/cancel`),
    { method: 'POST', headers: headers(), body: '{}' }
  );
  if (!response.ok) throw new Error((await response.text()) || `服务器返回 ${response.status}`);
  scheduleChatPoll();
}

async function deleteChat(chatId) {
  if (chatId === activeChatId && chatIsResponding()) {
    message.textContent = '请先停止当前回答再删除对话';
    return;
  }
  const response = await fetch(endpoint(`/api/v1/chats/${encodeURIComponent(chatId)}`), {
    method: 'DELETE', headers: reportHeaders()
  });
  if (!response.ok) throw new Error((await response.text()) || `服务器返回 ${response.status}`);
  chats = chats.filter((item) => item.id !== chatId);
  if (activeChatId === chatId) {
    activeChatId = chats[0]?.id || '';
    currentChatMessages = [];
    saveActiveChatId();
  }
  renderChatHistory();
  if (activeChatId) await loadChatMessages(); else renderChatMessages();
}

function resizeChatInput() {
  chatInput.style.height = 'auto';
  chatInput.style.height = `${Math.min(chatInput.scrollHeight, 150)}px`;
  updateChatComposer();
}

function setView(view) {
  if (!authenticated) return;
  currentView = ['chat', 'call', 'reports'].includes(view) ? view : 'chat';
  document.body.dataset.view = currentView;
  chatTab.classList.toggle('active', currentView === 'chat');
  callTab.classList.toggle('active', currentView === 'call');
  reportsTab.classList.toggle('active', currentView === 'reports');
  chatView.classList.toggle('hidden', currentView !== 'chat');
  callView.classList.toggle('hidden', currentView !== 'call');
  reportsView.classList.toggle('hidden', currentView !== 'reports');
  if (currentView === 'reports') loadReports().catch(() => {});
  if (currentView === 'chat') loadChats().catch(() => {});
  if (currentView === 'call' && !devicesInitialized) {
    enumerateDevices({ reportStatus: true }).catch((error) => {
      message.textContent = `设备检测失败：${error.message}`;
    });
  }
}

function reportHeaders() {
  return { Authorization: `Bearer ${authToken}` };
}

function renderReportList() {
  reportList.replaceChildren();
  reportCount.textContent = String(reports.length);
  if (!reports.length) {
    const empty = document.createElement('div');
    empty.className = 'report-list-empty';
    empty.textContent = '还没有任务报告。通过 RiverBank Edge 语音助手生成后，会自动出现在这里。';
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
  if (!authToken) {
    reportListStatus.textContent = '请先登录 RiverBank 账号';
    if (!silent) message.textContent = '报告库需要登录 RiverBank 账号';
    return;
  }
  saveSettings();
  reportsLoading = true;
  refreshReports.disabled = true;
  if (!silent) reportListStatus.textContent = '正在读取 RiverBank Edge 报告…';
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
  reportContent.innerHTML = '<p>正在从 RiverBank Edge 读取报告…</p>';
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
      token: authToken,
      filename: selectedReport.filename
    });
    if (!result.canceled) message.textContent = `报告已保存：${result.path}`;
  } catch (error) {
    message.textContent = `下载失败：${error.message}`;
  } finally {
    downloadReport.disabled = false;
  }
}

chatTab.addEventListener('click', () => setView('chat'));
callTab.addEventListener('click', () => setView('call'));
reportsTab.addEventListener('click', () => setView('reports'));
newChatButton.addEventListener('click', () => {
  clearPendingChatAttachments();
  createChat().catch((error) => {
    message.textContent = `新建对话失败：${error.message}`;
  });
});
chatAttach.addEventListener('click', () => chatAttachmentInput.click());
chatAttachmentInput.addEventListener('change', () => addPendingChatAttachments(chatAttachmentInput.files));
chatComposer.addEventListener('submit', (event) => {
  event.preventDefault();
  const operation = chatIsResponding() ? stopChatResponse() : sendChatMessage();
  operation.catch((error) => { message.textContent = `Chat 操作失败：${error.message}`; });
});
chatInput.addEventListener('input', resizeChatInput);
chatInput.addEventListener('keydown', (event) => {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    chatComposer.requestSubmit();
  }
});
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
  if (!authenticated) return;
  saveSettings();
  clearPendingChatAttachments();
  clearAttachmentObjectUrls();
  reports = [];
  selectedReport = null;
  chats = [];
  activeChatId = '';
  currentChatMessages = [];
  renderReportList();
  renderChatHistory();
  renderChatMessages();
  if (currentView === 'reports') loadReports();
  if (currentView === 'chat') loadChats();
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
cameraButton.addEventListener('click', () => toggleTrack('video', cameraButton, '关闭摄像头', '打开摄像头'));
refreshDevices.addEventListener('click', enumerateDevices);
loginForm.addEventListener('submit', (event) => {
  event.preventDefault();
  login().catch(() => {});
});
loginModeButton.addEventListener('click', () => setAuthenticationMode('login'));
registerModeButton.addEventListener('click', () => setAuthenticationMode('register'));
accountButton.addEventListener('click', () => openAccount());
closeAccountDialog.addEventListener('click', () => accountDialog.close());
accountDialog.addEventListener('click', (event) => {
  if (event.target === accountDialog) accountDialog.close();
});
changePasswordForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  try {
    const response = await fetch(endpoint('/api/v1/auth/change-password'), {
      method: 'POST',
      headers: headers(),
      body: JSON.stringify({
        current_password: currentPassword.value,
        new_password: newPassword.value
      })
    });
    if (!response.ok) throw new Error((await response.text()) || `设备返回 ${response.status}`);
    currentPassword.value = '';
    newPassword.value = '';
    setAccountMessage('密码已更新；其他设备上的登录会话已撤销');
  } catch (error) {
    setAccountMessage(error.message, true);
  }
});
createUserForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  try {
    const response = await fetch(endpoint('/api/v1/auth/users'), {
      method: 'POST',
      headers: headers(),
      body: JSON.stringify({
        username: newUserUsername.value.trim(),
        display_name: newUserDisplayName.value.trim(),
        password: newUserPassword.value,
        role: newUserRole.value
      })
    });
    if (!response.ok) throw new Error((await response.text()) || `设备返回 ${response.status}`);
    newUserUsername.value = '';
    newUserDisplayName.value = '';
    newUserPassword.value = '';
    newUserRole.value = 'user';
    await loadAccountUsers();
    setAccountMessage('新用户已创建，可立即在其他设备登录');
  } catch (error) {
    setAccountMessage(error.message, true);
  }
});
logoutButton.addEventListener('click', () => logout().catch((error) => {
  message.textContent = `登出失败：${error.message}`;
}));
toggleHostSettings.addEventListener('click', () => {
  setHostSettingsVisible(hostSettings.classList.contains('hidden'));
});
window.addEventListener('beforeunload', () => {
  clearPendingChatAttachments();
  clearAttachmentObjectUrls();
  hangup();
});

restoreSettings();
window.riverbankDesktop?.getAppVersion().then((version) => {
  desktopVersion.textContent = `RiverBank Call · ${version.displayVersion}`;
}).catch(() => {
  desktopVersion.textContent = 'RiverBank Call';
});
setState('idle', '未通话');
loginView.classList.remove('hidden');
appShell.classList.add('hidden');
resumeSavedSession().then((resumed) => {
  if (!resumed) (accountUsername.value ? accountPassword : serverUrl).focus();
});
setTimeout(() => {
  launchView.classList.add('completed');
}, 2150);
setInterval(() => {
  if (currentView === 'reports') loadReports({ silent: true });
}, 15000);
