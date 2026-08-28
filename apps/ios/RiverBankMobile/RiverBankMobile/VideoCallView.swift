import AVFoundation
import SwiftUI
import WebKit

struct VideoCallView: View {
    @EnvironmentObject private var store: AppStore

    var body: some View {
        NavigationStack {
            Group {
                if store.token.isEmpty {
                    ContentUnavailableView {
                        Label("尚未配置通话", systemImage: "video.slash")
                    } description: {
                        Text("请先在“设置”中保存树莓派地址和配对令牌。")
                    }
                } else {
                    WebRTCContainer(server: store.server, token: store.token)
                        .ignoresSafeArea(edges: .bottom)
                }
            }
            .background(Color.black)
            .navigationTitle("视频通话")
            .navigationBarTitleDisplayMode(.inline)
        }
    }
}

private struct WebRTCContainer: UIViewRepresentable {
    let server: String
    let token: String

    func makeCoordinator() -> Coordinator {
        Coordinator()
    }

    func makeUIView(context: Context) -> WKWebView {
        let configuration = WKWebViewConfiguration()
        configuration.allowsInlineMediaPlayback = true
        configuration.mediaTypesRequiringUserActionForPlayback = []
        configuration.defaultWebpagePreferences.allowsContentJavaScript = true

        let webView = WKWebView(frame: .zero, configuration: configuration)
        webView.navigationDelegate = context.coordinator
        webView.uiDelegate = context.coordinator
        webView.isOpaque = false
        webView.backgroundColor = .black
        webView.scrollView.backgroundColor = .black
        webView.scrollView.contentInsetAdjustmentBehavior = .never
        context.coordinator.load(webView: webView, server: server, token: token)
        return webView
    }

    func updateUIView(_ webView: WKWebView, context: Context) {
        context.coordinator.loadIfNeeded(webView: webView, server: server, token: token)
    }

    static func dismantleUIView(_ webView: WKWebView, coordinator: Coordinator) {
        webView.evaluateJavaScript("window.riverbankHangup && window.riverbankHangup(false)")
        coordinator.deactivateAudioSession()
        webView.navigationDelegate = nil
        webView.uiDelegate = nil
    }

    @MainActor
    final class Coordinator: NSObject, WKNavigationDelegate, WKUIDelegate {
        private var configurationFingerprint = ""

        func loadIfNeeded(webView: WKWebView, server: String, token: String) {
            let fingerprint = "\(server)\u{0}\(token)"
            guard fingerprint != configurationFingerprint else { return }
            load(webView: webView, server: server, token: token)
        }

        func load(webView: WKWebView, server: String, token: String) {
            configurationFingerprint = "\(server)\u{0}\(token)"
            let normalizedServer = server.trimmingCharacters(in: CharacterSet(charactersIn: "/"))
            let payload = ["server": normalizedServer, "token": token]
            guard let json = try? JSONSerialization.data(withJSONObject: payload),
                  let baseURL = URL(string: normalizedServer) else { return }
            let encoded = json.base64EncodedString()
            let page = VideoCallPage.html.replacingOccurrences(of: "__RIVERBANK_CONFIG__", with: encoded)
            webView.loadHTMLString(page, baseURL: baseURL)
        }

        @available(iOS 15.0, *)
        func webView(
            _ webView: WKWebView,
            requestMediaCapturePermissionFor origin: WKSecurityOrigin,
            initiatedByFrame frame: WKFrameInfo,
            type: WKMediaCaptureType,
            decisionHandler: @escaping @MainActor @Sendable (WKPermissionDecision) -> Void
        ) {
            activateAudioSession()
            decisionHandler(.grant)
        }

        func activateAudioSession() {
            let session = AVAudioSession.sharedInstance()
            do {
                try session.setCategory(
                    .playAndRecord,
                    mode: .videoChat,
                    options: [.defaultToSpeaker, .allowBluetoothHFP]
                )
                try session.setActive(true)
            } catch {
                // WebRTC owns the media request; a route setup failure must not abort it.
            }
        }

        func deactivateAudioSession() {
            try? AVAudioSession.sharedInstance().setActive(
                false,
                options: .notifyOthersOnDeactivation
            )
        }
    }
}

private enum VideoCallPage {
    static let html = #"""
    <!doctype html>
    <html lang="zh-CN">
    <head>
      <meta charset="utf-8">
      <meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover,user-scalable=no">
      <style>
        :root { color-scheme: dark; --cyan:#56d8ff; --green:#45e98f; --red:#ff5f69; --text:#eaf8fb; --muted:#86a6b0; }
        * { box-sizing:border-box; -webkit-tap-highlight-color:transparent; }
        html,body { width:100%; height:100%; margin:0; overflow:hidden; background:#020609; color:var(--text); font-family:-apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif; }
        button { font:inherit; color:inherit; }
        .app { position:relative; width:100%; height:100%; overflow:hidden; background:#020609; }
        #remote { width:100%; height:100%; object-fit:cover; background:#020609; }
        .shade { position:absolute; inset:0; pointer-events:none; background:linear-gradient(180deg,rgba(0,7,10,.72),transparent 24%,transparent 61%,rgba(0,5,8,.84)); }
        .top { position:absolute; z-index:5; top:max(12px,env(safe-area-inset-top)); left:14px; right:14px; display:flex; align-items:center; justify-content:space-between; }
        .brand { display:flex; align-items:center; gap:9px; padding:8px 12px; border:1px solid rgba(86,216,255,.18); border-radius:999px; background:rgba(3,13,18,.62); backdrop-filter:blur(18px); }
        .mark { display:grid; grid-template-columns:repeat(3,5px); gap:2px; }
        .mark i { width:5px; height:5px; border-radius:1.5px; background:var(--cyan); opacity:calc(.35 + var(--n) * .08); }
        .brand span { font-size:12px; font-weight:700; letter-spacing:.08em; }
        .pill { display:flex; align-items:center; gap:7px; padding:9px 12px; border:1px solid rgba(86,216,255,.17); border-radius:999px; color:var(--muted); font-size:12px; background:rgba(3,13,18,.65); backdrop-filter:blur(18px); }
        .dot { width:8px; height:8px; border-radius:50%; background:#53666e; }
        .pill[data-state="connecting"] .dot { background:var(--cyan); box-shadow:0 0 15px var(--cyan); animation:pulse 1.1s infinite; }
        .pill[data-state="connected"] { color:#b9f8d6; }
        .pill[data-state="connected"] .dot { background:var(--green); box-shadow:0 0 14px rgba(69,233,143,.8); }
        .pill[data-state="failed"] .dot { background:var(--red); }
        .empty { position:absolute; inset:0; display:grid; place-content:center; text-align:center; padding:32px; transition:opacity .25s; }
        .empty.hidden { opacity:0; pointer-events:none; }
        .orbit { width:74px; height:74px; margin:auto; border:1px solid rgba(86,216,255,.26); border-radius:50%; position:relative; }
        .orbit:after { content:""; position:absolute; width:10px; height:10px; left:32px; top:-5px; border-radius:50%; background:var(--cyan); box-shadow:0 0 18px rgba(86,216,255,.9); transform-origin:5px 42px; animation:orbit 2.5s linear infinite; }
        .empty h2 { margin:19px 0 7px; font-size:22px; }
        .empty p { margin:0; color:var(--muted); font-size:14px; line-height:1.55; }
        .local { position:absolute; z-index:4; right:14px; top:calc(max(12px,env(safe-area-inset-top)) + 58px); width:29vw; max-width:138px; aspect-ratio:3/4; overflow:hidden; border-radius:20px; border:1px solid rgba(194,241,255,.28); background:#081116; box-shadow:0 12px 36px rgba(0,0,0,.46); opacity:0; transform:scale(.94); transition:opacity .2s,transform .2s; }
        .local.visible { opacity:1; transform:scale(1); }
        #local { width:100%; height:100%; object-fit:cover; transform:scaleX(-1); }
        .local.back #local { transform:none; }
        .stats { position:absolute; z-index:4; left:15px; bottom:calc(max(106px,env(safe-area-inset-bottom) + 94px)); padding:7px 10px; border-radius:999px; background:rgba(2,9,12,.58); color:var(--muted); font-size:11px; backdrop-filter:blur(12px); }
        .message { position:absolute; z-index:4; left:50%; bottom:calc(max(163px,env(safe-area-inset-bottom) + 151px)); transform:translateX(-50%); max-width:84%; padding:8px 13px; border-radius:999px; color:#c9e7ee; background:rgba(3,13,18,.62); font-size:12px; text-align:center; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; backdrop-filter:blur(15px); }
        .controls { position:absolute; z-index:6; left:0; right:0; bottom:max(18px,env(safe-area-inset-bottom)); display:flex; align-items:center; justify-content:center; gap:12px; padding:0 12px; }
        .control { width:54px; height:54px; border:1px solid rgba(114,205,229,.2); border-radius:50%; background:rgba(12,29,38,.86); backdrop-filter:blur(18px); display:grid; place-items:center; transition:transform .12s,background .18s,opacity .18s; }
        .control:active { transform:scale(.92); }
        .control:disabled { opacity:.38; }
        .control.active { background:rgba(203,63,76,.82); }
        .control svg { width:23px; height:23px; fill:none; stroke:currentColor; stroke-width:2; stroke-linecap:round; stroke-linejoin:round; }
        #connect { width:72px; height:72px; border:0; border-radius:50%; background:linear-gradient(145deg,#1598bf,#58dcff); color:#03151b; box-shadow:0 10px 30px rgba(47,194,233,.3); }
        #connect.hangup { color:white; background:linear-gradient(145deg,#c83945,#ff6972); box-shadow:0 10px 30px rgba(255,73,88,.25); }
        #connect svg { width:29px; height:29px; }
        @keyframes orbit { to { transform:rotate(360deg); } }
        @keyframes pulse { 50% { opacity:.45; transform:scale(.8); } }
      </style>
    </head>
    <body>
      <main class="app">
        <video id="remote" autoplay playsinline></video>
        <div class="shade"></div>
        <header class="top">
          <div class="brand"><b class="mark"><i style="--n:1"></i><i style="--n:2"></i><i style="--n:3"></i><i style="--n:2"></i><i style="--n:3"></i><i style="--n:4"></i><i style="--n:3"></i><i style="--n:4"></i><i style="--n:5"></i></b><span>RIVERBANK</span></div>
          <div class="pill" id="pill" data-state="idle"><i class="dot"></i><span id="state">未连接</span></div>
        </header>
        <section class="empty" id="empty"><div class="orbit"></div><h2>连接 RiverBank</h2><p>使用 iPhone 摄像头和麦克风<br>与树莓派实时双向通话</p></section>
        <aside class="local" id="localBox"><video id="local" autoplay muted playsinline></video></aside>
        <div class="stats" id="stats">—</div>
        <div class="message" id="message">点击中间按钮开始通话</div>
        <nav class="controls">
          <button class="control" id="mute" aria-label="静音" disabled><svg viewBox="0 0 24 24"><path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2M12 19v3M8 22h8"/></svg></button>
          <button class="control" id="camera" aria-label="关闭画面" disabled><svg viewBox="0 0 24 24"><path d="M14 8h5l3-3v14l-3-3h-5z"/><rect x="2" y="6" width="12" height="12" rx="3"/></svg></button>
          <button class="control" id="connect" aria-label="开始通话"><svg id="connectIcon" viewBox="0 0 24 24"><path d="M15 10l4-3v10l-4-3z"/><rect x="3" y="6" width="12" height="12" rx="3"/></svg></button>
          <button class="control" id="flip" aria-label="切换摄像头" disabled><svg viewBox="0 0 24 24"><path d="M20 7h-9a5 5 0 0 0-5 5v1"/><path d="m17 4 3 3-3 3M4 17h9a5 5 0 0 0 5-5v-1"/><path d="m7 20-3-3 3-3"/></svg></button>
        </nav>
      </main>
      <script>
        const cfg=JSON.parse(atob("__RIVERBANK_CONFIG__"));
        const $=id=>document.getElementById(id);
        const remote=$('remote'),local=$('local'),localBox=$('localBox'),empty=$('empty'),pill=$('pill'),stateLabel=$('state'),message=$('message'),stats=$('stats');
        const connect=$('connect'),mute=$('mute'),camera=$('camera'),flip=$('flip'),connectIcon=$('connectIcon');
        let pc=null,localStream=null,remoteStream=null,sessionId=null,statsTimer=null,facing='user',busy=false,previousStats=null;
        const endpoint=path=>cfg.server.replace(/\/$/,'')+path;
        const headers=()=>({'Authorization':`Bearer ${cfg.token}`,'Content-Type':'application/json'});
        function setState(value,label){pill.dataset.state=value;stateLabel.textContent=label;const active=value==='connecting'||value==='connected';connect.classList.toggle('hangup',active);connect.setAttribute('aria-label',active?'挂断':'开始通话');connectIcon.innerHTML=active?'<path d="M5 5l14 14M19 5 5 19"/>':'<path d="M15 10l4-3v10l-4-3z"/><rect x="3" y="6" width="12" height="12" rx="3"/>';mute.disabled=!active;camera.disabled=!active;flip.disabled=!active;}
        function waitIce(target,timeout=8000){if(target.iceGatheringState==='complete')return Promise.resolve();return new Promise(resolve=>{const timer=setTimeout(resolve,timeout);const listener=()=>{if(target.iceGatheringState==='complete'){clearTimeout(timer);target.removeEventListener('icegatheringstatechange',listener);resolve();}};target.addEventListener('icegatheringstatechange',listener);});}
        async function start(){if(busy)return;if(!cfg.token||cfg.token.length<16)throw new Error('请先在设置中保存配对令牌');busy=true;setState('connecting','连接中');message.textContent='正在打开摄像头和麦克风…';try{localStream=await navigator.mediaDevices.getUserMedia({video:{facingMode:{ideal:facing},width:{ideal:1280},height:{ideal:720},frameRate:{ideal:24,max:30}},audio:{echoCancellation:true,noiseSuppression:true,autoGainControl:true,channelCount:1}});local.srcObject=localStream;localBox.classList.add('visible');remoteStream=new MediaStream();remote.srcObject=remoteStream;pc=new RTCPeerConnection({iceServers:[]});localStream.getTracks().forEach(track=>pc.addTrack(track,localStream));pc.addEventListener('track',event=>{const source=event.streams[0];(source?source.getTracks():[event.track]).forEach(track=>{if(!remoteStream.getTracks().includes(track))remoteStream.addTrack(track);});empty.classList.add('hidden');remote.play().catch(()=>{});});pc.addEventListener('connectionstatechange',()=>{const value=pc?.connectionState||'closed';if(value==='connected'){setState('connected','通话中');message.textContent='音视频通道已连接';beginStats();}else if(value==='failed'){setState('failed','连接失败');message.textContent='媒体通道连接失败';}else if(value==='disconnected'){message.textContent='连接暂时中断';}});const offer=await pc.createOffer({offerToReceiveAudio:true,offerToReceiveVideo:true});await pc.setLocalDescription(offer);await waitIce(pc);message.textContent='正在协商媒体通道…';const response=await fetch(endpoint('/api/v1/offer'),{method:'POST',headers:headers(),body:JSON.stringify({sdp:pc.localDescription.sdp,type:pc.localDescription.type,device_name:'RiverBank on iPhone'})});if(!response.ok)throw new Error((await response.text())||`服务器返回 ${response.status}`);const answer=await response.json();sessionId=answer.session_id;await pc.setRemoteDescription({sdp:answer.sdp,type:answer.type});}catch(error){await hangup(false);setState('failed','连接失败');message.textContent=error.message||'无法建立通话';}finally{busy=false;}}
        async function hangup(notify=true){clearInterval(statsTimer);statsTimer=null;previousStats=null;if(notify&&cfg.token)fetch(endpoint('/api/v1/hangup'),{method:'POST',headers:headers(),body:JSON.stringify({session_id:sessionId})}).catch(()=>{});if(pc){pc.close();pc=null;}if(localStream)localStream.getTracks().forEach(track=>track.stop());localStream=null;remoteStream=null;local.srcObject=null;remote.srcObject=null;sessionId=null;localBox.classList.remove('visible');empty.classList.remove('hidden');stats.textContent='—';mute.classList.remove('active');camera.classList.remove('active');setState('idle','未连接');if(notify)message.textContent='通话已结束';}
        async function switchCamera(){if(!pc||!localStream||busy)return;busy=true;try{facing=facing==='user'?'environment':'user';const replacement=await navigator.mediaDevices.getUserMedia({video:{facingMode:{exact:facing},width:{ideal:1280},height:{ideal:720},frameRate:{ideal:24,max:30}}});const newTrack=replacement.getVideoTracks()[0];const sender=pc.getSenders().find(item=>item.track?.kind==='video');await sender.replaceTrack(newTrack);localStream.getVideoTracks().forEach(track=>{localStream.removeTrack(track);track.stop();});localStream.addTrack(newTrack);local.srcObject=null;local.srcObject=localStream;localBox.classList.toggle('back',facing==='environment');message.textContent=facing==='user'?'已切换到前置摄像头':'已切换到后置摄像头';}catch(error){facing=facing==='user'?'environment':'user';message.textContent='摄像头切换失败';}finally{busy=false;}}
        function toggle(kind,button){const tracks=localStream?.getTracks().filter(track=>track.kind===kind)||[];if(!tracks.length)return;const next=!tracks[0].enabled;tracks.forEach(track=>track.enabled=next);button.classList.toggle('active',!next);message.textContent=kind==='audio'?(next?'麦克风已打开':'麦克风已静音'):(next?'摄像头已打开':'摄像头已关闭');}
        async function updateStats(){if(!pc||pc.connectionState!=='connected')return;const reports=await pc.getStats();let inbound=0,outbound=0,rtt=null;reports.forEach(report=>{if(report.type==='candidate-pair'&&report.state==='succeeded'&&report.nominated)rtt=report.currentRoundTripTime;if(report.type==='inbound-rtp'&&!report.isRemote)inbound+=report.bytesReceived||0;if(report.type==='outbound-rtp'&&!report.isRemote)outbound+=report.bytesSent||0;});const now=performance.now();if(previousStats){const seconds=Math.max((now-previousStats.now)/1000,.1);const down=((inbound-previousStats.inbound)*8/seconds/1e6).toFixed(1),up=((outbound-previousStats.outbound)*8/seconds/1e6).toFixed(1);stats.textContent=`↓ ${down}  ↑ ${up} Mbps${rtt==null?'':` · ${Math.round(rtt*1000)} ms`}`;}else stats.textContent='正在统计网络…';previousStats={now,inbound,outbound};}
        function beginStats(){clearInterval(statsTimer);statsTimer=setInterval(()=>updateStats().catch(()=>{}),1500);}
        connect.addEventListener('click',()=>{if(pc)hangup(true);else start();});mute.addEventListener('click',()=>toggle('audio',mute));camera.addEventListener('click',()=>toggle('video',camera));flip.addEventListener('click',switchCamera);
        window.riverbankHangup=hangup;
        window.addEventListener('pagehide',()=>hangup(true));
        setState('idle','未连接');
      </script>
    </body>
    </html>
    """#
}
