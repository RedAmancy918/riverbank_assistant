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
                        Text("请先登录 RiverBank 账号。")
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
        :root { color-scheme: dark; --cyan:#56d8ff; --green:#45e98f; --red:#ff5f69; --text:#eaf8fb; --muted:#86a6b0; --glass:rgba(7,20,27,.72); }
        * { box-sizing:border-box; -webkit-tap-highlight-color:transparent; }
        html,body { width:100%; height:100%; margin:0; overflow:hidden; background:#000; color:var(--text); font-family:-apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif; }
        button { font:inherit; color:inherit; }
        .app { position:relative; width:100%; height:100%; overflow:hidden; background:#000; }
        #remote { width:100%; height:100%; object-fit:cover; background:#000; }
        .shade { position:absolute; inset:0; pointer-events:none; background:linear-gradient(180deg,rgba(0,7,10,.66),transparent 25%,transparent 61%,rgba(0,5,8,.88)); }
        .top { position:absolute; z-index:5; top:max(15px,env(safe-area-inset-top)); left:18px; right:18px; display:flex; align-items:center; justify-content:flex-end; }
        .pill { display:flex; align-items:center; gap:7px; min-height:32px; padding:7px 11px; border:1px solid rgba(255,255,255,.12); border-radius:999px; color:var(--muted); font-size:11px; background:rgba(28,28,30,.72); backdrop-filter:blur(18px); transition:opacity .2s,transform .2s; }
        .app[data-state="idle"] .pill { opacity:.82; }
        .dot { width:7px; height:7px; border-radius:50%; background:#53666e; }
        .pill[data-state="connecting"] .dot { background:var(--cyan); box-shadow:0 0 15px var(--cyan); animation:pulse 1.1s infinite; }
        .pill[data-state="connected"] { color:#b9f8d6; }
        .pill[data-state="connected"] .dot { background:var(--green); box-shadow:0 0 14px rgba(69,233,143,.8); }
        .pill[data-state="failed"] .dot { background:var(--red); }
        .empty { position:absolute; inset:58px 0 122px; display:grid; place-content:center; justify-items:center; text-align:center; padding:28px; transition:opacity .25s,transform .25s; }
        .empty.hidden { opacity:0; pointer-events:none; }
        .product-mark { position:relative; display:grid; place-items:center; width:86px; height:86px; margin-bottom:22px; border-radius:22px; background:rgba(28,28,30,.94); box-shadow:inset 0 0 0 1px rgba(255,255,255,.035); }
        .product-grid { display:grid; grid-template-columns:repeat(3,15px); grid-auto-rows:15px; gap:4px; }
        .product-grid i { width:15px; height:15px; border-radius:4px; transform:translateY(0) scale(1); transform-origin:center; will-change:transform,filter,opacity; }
        .product-grid i:nth-child(1) { background:#e5f3f7; }
        .product-grid i:nth-child(2) { background:#cad9de; }
        .product-grid i:nth-child(3) { background:#9fadb2; }
        .product-grid i:nth-child(4) { background:#bfd0d5; }
        .product-grid i:nth-child(5) { background:#a3b2b7; }
        .product-grid i:nth-child(6) { background:#7f8d92; }
        .product-grid i:nth-child(7) { background:#96a6ab; }
        .product-grid i:nth-child(8) { background:#79878c; }
        .product-grid i:nth-child(9) { background:var(--cyan); }
        .app[data-state="connecting"] .product-grid i { animation:gridBounce 1.08s cubic-bezier(.34,1.45,.52,1) infinite; }
        .app[data-state="connecting"] .product-grid i:nth-child(1) { animation-delay:0s; }
        .app[data-state="connecting"] .product-grid i:nth-child(2) { animation-delay:.07s; }
        .app[data-state="connecting"] .product-grid i:nth-child(3) { animation-delay:.14s; }
        .app[data-state="connecting"] .product-grid i:nth-child(4) { animation-delay:.21s; }
        .app[data-state="connecting"] .product-grid i:nth-child(5) { animation-delay:.28s; }
        .app[data-state="connecting"] .product-grid i:nth-child(6) { animation-delay:.35s; }
        .app[data-state="connecting"] .product-grid i:nth-child(7) { animation-delay:.42s; }
        .app[data-state="connecting"] .product-grid i:nth-child(8) { animation-delay:.49s; }
        .app[data-state="connecting"] .product-grid i:nth-child(9) { animation-delay:.56s; }
        .empty h2 { margin:0 0 9px; font-size:22px; font-weight:650; letter-spacing:0; }
        .empty .description { margin:0; max-width:300px; color:#85858b; font-size:15px; line-height:1.65; }
        .local { position:absolute; z-index:4; right:14px; top:calc(max(12px,env(safe-area-inset-top)) + 58px); width:29vw; max-width:138px; aspect-ratio:3/4; overflow:hidden; border-radius:20px; border:1px solid rgba(194,241,255,.28); background:#081116; box-shadow:0 12px 36px rgba(0,0,0,.46); opacity:0; transform:scale(.94); transition:opacity .2s,transform .2s; }
        .local.visible { opacity:1; transform:scale(1); }
        #local { width:100%; height:100%; object-fit:cover; transform:scaleX(-1); }
        .local.back #local { transform:none; }
        .stats { position:absolute; z-index:5; top:max(15px,env(safe-area-inset-top)); left:18px; max-width:calc(100% - 150px); padding:7px 10px; border:1px solid rgba(255,255,255,.09); border-radius:999px; background:rgba(28,28,30,.72); color:var(--muted); font-size:10px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; backdrop-filter:blur(18px); }
        .stats:empty { display:none; }
        .message { position:absolute; z-index:4; left:50%; bottom:calc(max(119px,env(safe-area-inset-bottom) + 107px)); transform:translateX(-50%); width:max-content; max-width:82%; padding:7px 12px; border-radius:999px; color:#9f9fa5; background:rgba(28,28,30,.62); font-size:11px; text-align:center; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; backdrop-filter:blur(15px); }
        .message:empty { display:none; }
        .controls { position:absolute; z-index:6; left:50%; bottom:max(30px,env(safe-area-inset-bottom) + 13px); transform:translateX(-50%); display:flex; align-items:center; justify-content:center; gap:8px; width:240px; height:72px; padding:7px 8px; border:1px solid rgba(255,255,255,.12); border-radius:999px; background:rgba(28,28,30,.94); box-shadow:0 14px 42px rgba(0,0,0,.3); backdrop-filter:blur(22px); transition:width .24s cubic-bezier(.2,.8,.2,1),gap .22s; }
        .control { flex:0 0 auto; min-width:0; width:44px; height:44px; padding:0; border:1px solid rgba(255,255,255,.09); border-radius:50%; background:rgba(255,255,255,.055); display:grid; place-items:center; overflow:hidden; transition:width .22s,transform .12s,background .18s,opacity .18s,border-width .22s; }
        .control:active { transform:scale(.92); }
        .control:disabled { opacity:.38; }
        .control.active { background:rgba(203,63,76,.82); }
        .control svg { width:21px; height:21px; fill:none; stroke:currentColor; stroke-width:2; stroke-linecap:round; stroke-linejoin:round; }
        #connect { flex-basis:56px; width:56px; min-width:56px; height:56px; border:0; border-radius:50%; background:linear-gradient(145deg,#1598bf,#58dcff); color:#03151b; box-shadow:0 9px 27px rgba(47,194,233,.28); }
        #connect.hangup { color:white; background:linear-gradient(145deg,#c83945,#ff6972); box-shadow:0 10px 30px rgba(255,73,88,.25); }
        #connect svg { width:24px; height:24px; }
        .app[data-state="idle"] .controls,.app[data-state="failed"] .controls { bottom:max(30px,env(safe-area-inset-bottom) + 13px); width:72px; height:72px; padding:7px; gap:0; overflow:hidden; }
        .app[data-state="idle"] #connect,.app[data-state="failed"] #connect { flex-basis:56px; width:56px; min-width:56px; height:56px; }
        .app[data-state="idle"] #connect svg,.app[data-state="failed"] #connect svg { width:24px; height:24px; }
        .app[data-state="idle"] .control:not(#connect),.app[data-state="failed"] .control:not(#connect) { position:absolute; width:0; height:0; padding:0; border-width:0; opacity:0; visibility:hidden; pointer-events:none; transform:scale(.72); }
        @keyframes gridBounce { 0%,42%,100% { transform:translateY(0) scale(1); filter:brightness(1); opacity:.82; } 18% { transform:translateY(-5px) scale(1.07); filter:brightness(1.32); opacity:1; } 28% { transform:translateY(1px) scale(.98); filter:brightness(1.08); opacity:.94; } }
        @keyframes pulse { 50% { opacity:.45; transform:scale(.8); } }
        @media (prefers-reduced-motion:reduce) { .app[data-state="connecting"] .product-grid i { animation:gridFade 1.08s ease-in-out infinite; } }
        @keyframes gridFade { 0%,55%,100% { opacity:.48; } 24% { opacity:1; } }
      </style>
    </head>
    <body>
      <main class="app" id="app" data-state="idle">
        <video id="remote" autoplay playsinline></video>
        <div class="shade"></div>
        <header class="top">
          <div class="pill" id="pill" data-state="idle"><i class="dot"></i><span id="state">待连接</span></div>
        </header>
        <section class="empty" id="empty"><div class="product-mark"><b class="product-grid"><i></i><i></i><i></i><i></i><i></i><i></i><i></i><i></i><i></i></b></div><h2>与小灰视频通话</h2><p class="description">使用 iPhone 与小灰进行<br>实时双向音视频通话</p></section>
        <aside class="local" id="localBox"><video id="local" autoplay muted playsinline></video></aside>
        <div class="stats" id="stats"></div>
        <div class="message" id="message" role="status" aria-live="polite"></div>
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
        const app=$('app'),remote=$('remote'),local=$('local'),localBox=$('localBox'),empty=$('empty'),pill=$('pill'),stateLabel=$('state'),message=$('message'),stats=$('stats');
        const connect=$('connect'),mute=$('mute'),camera=$('camera'),flip=$('flip'),connectIcon=$('connectIcon');
        let pc=null,localStream=null,remoteStream=null,sessionId=null,statsTimer=null,facing='user',busy=false,previousStats=null;
        const endpoint=path=>cfg.server.replace(/\/$/,'')+path;
        const headers=()=>({'Authorization':`Bearer ${cfg.token}`,'Content-Type':'application/json'});
        function setState(value,label){app.dataset.state=value;pill.dataset.state=value;stateLabel.textContent=label;const active=value==='connecting'||value==='connected';connect.classList.toggle('hangup',active);connect.setAttribute('aria-label',active?'结束通话':'开始通话');connectIcon.innerHTML=active?'<path d="M5 5l14 14M19 5 5 19"/>':'<path d="M15 10l4-3v10l-4-3z"/><rect x="3" y="6" width="12" height="12" rx="3"/>';mute.disabled=!active;camera.disabled=!active;flip.disabled=!active;}
        function waitIce(target,timeout=8000){if(target.iceGatheringState==='complete')return Promise.resolve();return new Promise(resolve=>{const timer=setTimeout(resolve,timeout);const listener=()=>{if(target.iceGatheringState==='complete'){clearTimeout(timer);target.removeEventListener('icegatheringstatechange',listener);resolve();}};target.addEventListener('icegatheringstatechange',listener);});}
        async function start(){if(busy)return;if(!cfg.token||!cfg.token.startsWith('rbs_'))throw new Error('请先登录 RiverBank 账号');busy=true;setState('connecting','正在连接');message.textContent='正在请求摄像头与麦克风权限';try{localStream=await navigator.mediaDevices.getUserMedia({video:{facingMode:{ideal:facing},width:{ideal:1280},height:{ideal:720},frameRate:{ideal:24,max:30}},audio:{echoCancellation:true,noiseSuppression:true,autoGainControl:true,channelCount:1}});local.srcObject=localStream;localBox.classList.add('visible');remoteStream=new MediaStream();remote.srcObject=remoteStream;pc=new RTCPeerConnection({iceServers:[]});localStream.getTracks().forEach(track=>pc.addTrack(track,localStream));pc.addEventListener('track',event=>{const source=event.streams[0];(source?source.getTracks():[event.track]).forEach(track=>{if(!remoteStream.getTracks().includes(track))remoteStream.addTrack(track);});empty.classList.add('hidden');remote.play().catch(()=>{});});pc.addEventListener('connectionstatechange',()=>{const value=pc?.connectionState||'closed';if(value==='connected'){setState('connected','通话中');message.textContent='已与小灰建立实时通话';beginStats();}else if(value==='failed'){setState('failed','连接失败');message.textContent='连接失败，请检查网络后重试';}else if(value==='disconnected'){message.textContent='连接中断，正在等待恢复';}});const offer=await pc.createOffer({offerToReceiveAudio:true,offerToReceiveVideo:true});await pc.setLocalDescription(offer);await waitIce(pc);message.textContent='正在建立加密音视频通道';const response=await fetch(endpoint('/api/v1/offer'),{method:'POST',headers:headers(),body:JSON.stringify({sdp:pc.localDescription.sdp,type:pc.localDescription.type,device_name:'RiverBank on iPhone'})});if(!response.ok)throw new Error((await response.text())||`服务器返回 ${response.status}`);const answer=await response.json();sessionId=answer.session_id;await pc.setRemoteDescription({sdp:answer.sdp,type:answer.type});}catch(error){await hangup(false);setState('failed','连接失败');message.textContent=error?.name==='NotAllowedError'?'请允许访问摄像头和麦克风':'无法开始通话，请检查网络后重试';}finally{busy=false;}}
        async function hangup(notify=true){clearInterval(statsTimer);statsTimer=null;previousStats=null;if(notify&&cfg.token)fetch(endpoint('/api/v1/hangup'),{method:'POST',headers:headers(),body:JSON.stringify({session_id:sessionId})}).catch(()=>{});if(pc){pc.close();pc=null;}if(localStream)localStream.getTracks().forEach(track=>track.stop());localStream=null;remoteStream=null;local.srcObject=null;remote.srcObject=null;sessionId=null;localBox.classList.remove('visible');empty.classList.remove('hidden');stats.textContent='';mute.classList.remove('active');camera.classList.remove('active');setState('idle','待连接');if(notify)message.textContent='通话已结束，可再次发起';}
        async function switchCamera(){if(!pc||!localStream||busy)return;busy=true;try{facing=facing==='user'?'environment':'user';const replacement=await navigator.mediaDevices.getUserMedia({video:{facingMode:{exact:facing},width:{ideal:1280},height:{ideal:720},frameRate:{ideal:24,max:30}}});const newTrack=replacement.getVideoTracks()[0];const sender=pc.getSenders().find(item=>item.track?.kind==='video');await sender.replaceTrack(newTrack);localStream.getVideoTracks().forEach(track=>{localStream.removeTrack(track);track.stop();});localStream.addTrack(newTrack);local.srcObject=null;local.srcObject=localStream;localBox.classList.toggle('back',facing==='environment');message.textContent=facing==='user'?'已切换到前置摄像头':'已切换到后置摄像头';}catch(error){facing=facing==='user'?'environment':'user';message.textContent='摄像头切换失败';}finally{busy=false;}}
        function toggle(kind,button){const tracks=localStream?.getTracks().filter(track=>track.kind===kind)||[];if(!tracks.length)return;const next=!tracks[0].enabled;tracks.forEach(track=>track.enabled=next);button.classList.toggle('active',!next);message.textContent=kind==='audio'?(next?'麦克风已打开':'麦克风已静音'):(next?'摄像头已打开':'摄像头已关闭');}
        async function updateStats(){if(!pc||pc.connectionState!=='connected')return;const reports=await pc.getStats();let inbound=0,outbound=0,rtt=null;reports.forEach(report=>{if(report.type==='candidate-pair'&&report.state==='succeeded'&&report.nominated)rtt=report.currentRoundTripTime;if(report.type==='inbound-rtp'&&!report.isRemote)inbound+=report.bytesReceived||0;if(report.type==='outbound-rtp'&&!report.isRemote)outbound+=report.bytesSent||0;});const now=performance.now();if(previousStats){const seconds=Math.max((now-previousStats.now)/1000,.1);const down=((inbound-previousStats.inbound)*8/seconds/1e6).toFixed(1),up=((outbound-previousStats.outbound)*8/seconds/1e6).toFixed(1);stats.textContent=`↓ ${down}  ↑ ${up} Mbps${rtt==null?'':` · ${Math.round(rtt*1000)} ms`}`;}else stats.textContent='正在统计网络…';previousStats={now,inbound,outbound};}
        function beginStats(){clearInterval(statsTimer);statsTimer=setInterval(()=>updateStats().catch(()=>{}),1500);}
        connect.addEventListener('click',()=>{if(pc)hangup(true);else start();});mute.addEventListener('click',()=>toggle('audio',mute));camera.addEventListener('click',()=>toggle('video',camera));flip.addEventListener('click',switchCamera);
        window.riverbankHangup=hangup;
        window.addEventListener('pagehide',()=>hangup(true));
        setState('idle','待连接');
      </script>
    </body>
    </html>
    """#
}
