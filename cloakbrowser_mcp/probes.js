async (opt) => {
  // Stealth audit probes. Runs inside the page. opt = {network: bool}.
  // Verdicts: FAIL = a site can see the automation; WARN = unusual compared with a stock Chrome.
  const raw = {};
  const findings = [];
  const add = (id, verdict, note) => findings.push({ id, verdict, note });
  const t = (f) => { try { return f(); } catch (e) { return 'ERR:' + String(e).slice(0, 80); } };
  const network = !opt || opt.network !== false;
  const nav = navigator;

  raw.ua = nav.userAgent;
  raw.langs = nav.languages;
  raw.tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
  raw.locale = Intl.DateTimeFormat().resolvedOptions().locale;
  raw.screen = { s: [screen.width, screen.height], avail: [screen.availWidth, screen.availHeight], inner: [innerWidth, innerHeight], outer: [outerWidth, outerHeight], dpr: devicePixelRatio };

  // 1. automation flags and leftovers
  if (nav.webdriver !== false) add('webdriver', 'FAIL', 'navigator.webdriver is ' + nav.webdriver);
  if (/Headless/i.test(nav.userAgent)) add('headless-ua', 'FAIL', 'HeadlessChrome in the user agent');
  const bad = t(() => Object.getOwnPropertyNames(window).filter(k => /playwright|__pw|cdc_|\$cdc|nightmare|phantom|selenium|puppeteer|pptr|wdio|domAutomation|__cm_|__nightwatch/i.test(k)));
  if (bad.length) add('automation-globals', 'FAIL', 'window has ' + bad.join(','));
  if (t(() => document.querySelector('[data-cm-ref],#__cm_overlay'))) add('legacy-markers', 'FAIL', 'the fixed marker names data-cm-ref / __cm_overlay are in the page');
  const markerAttrs = t(() => { const s = new Set(); for (const el of document.querySelectorAll('*')) for (const a of el.getAttributeNames()) if (/^data-k[0-9a-f]{10}$/.test(a)) s.add(a); return [...s]; });
  raw.markerAttrs = markerAttrs;
  if (Array.isArray(markerAttrs) && markerAttrs.length) add('page-markers', 'WARN', 'the page carries snapshot ref attributes: ' + markerAttrs.join(',') + ' (random per run, but present until reload)');

  // 2. CDP / Runtime.enable detection
  const cdp = t(() => { let hit = false; const e = new Error(); Object.defineProperty(e, 'stack', { get() { hit = true; return ''; } }); console.debug(e); return hit; });
  if (cdp === true) add('cdp-runtime-enable', 'FAIL', 'console.debug touched Error.stack: Runtime.enable is on');

  // 3. native-code integrity
  const fns = {
    'Navigator.webdriver getter': () => { const d = Object.getOwnPropertyDescriptor(Navigator.prototype, 'webdriver'); return d && d.get; },
    'permissions.query': () => nav.permissions.query,
    'toDataURL': () => HTMLCanvasElement.prototype.toDataURL,
    'getParameter': () => WebGLRenderingContext.prototype.getParameter,
    'console.log': () => console.log,
    'console.debug': () => console.debug,
    'Function.toString': () => Function.prototype.toString,
    'fetch': () => window.fetch,
    'Date.getTimezoneOffset': () => Date.prototype.getTimezoneOffset,
  };
  raw.nativeFns = {};
  for (const [name, get] of Object.entries(fns)) {
    const f = t(get);
    if (typeof f !== 'function') { raw.nativeFns[name] = 'missing'; continue; }
    const s = Function.prototype.toString.call(f);
    const native = /\{\s*\[native code\]\s*\}\s*$/.test(s);
    const shape = /^function (get |set )?[\w$.]*\(\) \{ \[native code\] \}$/.test(s) && (s.includes(' ' + f.name + '(') || (name === 'Navigator.webdriver getter'));
    raw.nativeFns[name] = !native ? 'SOURCE' : shape ? 'native' : 'native-but-anonymous';
    if (!native) add('non-native-' + name, 'FAIL', name + ' shows source: ' + s.slice(0, 60));
    else if (!shape) add('anonymous-native-' + name, 'WARN', name + ' prints "' + s + '" (a Proxy wrapper; the capture_console hook does this)');
  }

  // 4. cross-realm consistency
  const fr = t(() => { const f = document.createElement('iframe'); f.srcdoc = 'x'; document.body.appendChild(f); const w = f.contentWindow; const o = { wd: w.navigator.webdriver, ua: w.navigator.userAgent === nav.userAgent, langs: w.navigator.languages.join() === nav.languages.join(), plat: w.navigator.platform === nav.platform, cores: w.navigator.hardwareConcurrency === nav.hardwareConcurrency }; f.remove(); return o; });
  raw.iframe = fr;
  if (fr && (fr.wd !== false || !fr.ua || !fr.langs || !fr.plat || !fr.cores)) add('iframe-mismatch', 'FAIL', JSON.stringify(fr));
  raw.worker = await (async () => {
    try {
      const src = 'const c=new OffscreenCanvas(1,1).getContext("webgl");let r=null;if(c){const e=c.getExtension("WEBGL_debug_renderer_info");r=e&&c.getParameter(e.UNMASKED_RENDERER_WEBGL)}postMessage({ua:navigator.userAgent,cores:navigator.hardwareConcurrency,langs:navigator.languages,plat:navigator.platform,gl:r,wd:navigator.webdriver,tz:Intl.DateTimeFormat().resolvedOptions().timeZone})';
      const w = new Worker(URL.createObjectURL(new Blob([src])));
      return await new Promise((res, rej) => { w.onmessage = e => res(e.data); w.onerror = () => rej('worker error'); setTimeout(() => rej('timeout'), 3000); });
    } catch (e) { return 'ERR ' + e; }
  })();
  const gl = t(() => { const c = document.createElement('canvas'); const g = c.getContext('webgl'); const e = g.getExtension('WEBGL_debug_renderer_info'); return { v: g.getParameter(e.UNMASKED_VENDOR_WEBGL), r: g.getParameter(e.UNMASKED_RENDERER_WEBGL) }; });
  raw.gl = gl;
  if (raw.worker && typeof raw.worker === 'object') {
    const w = raw.worker, diffs = [];
    if (w.ua !== nav.userAgent) diffs.push('ua');
    if (w.cores !== nav.hardwareConcurrency) diffs.push('cores');
    if (w.langs.join() !== nav.languages.join()) diffs.push('languages');
    if (w.plat !== nav.platform) diffs.push('platform');
    if (gl && gl.r && w.gl !== gl.r) diffs.push('webgl renderer');
    if (w.tz !== raw.tz) diffs.push('timezone');
    if (w.wd === true) diffs.push('webdriver');  // workers have no webdriver property at all in real Chrome
    if (diffs.length) add('worker-mismatch', 'FAIL', 'worker differs from page on ' + diffs.join(','));
  } else add('worker-probe', 'WARN', String(raw.worker));

  // 5. software rendering
  if (gl && /SwiftShader|llvmpipe|Software|Mesa OffScreen/i.test(gl.r || '')) add('software-gl', 'FAIL', 'software WebGL renderer: ' + gl.r);

  // 6. UA vs client hints
  raw.uach = await (async () => { try { const u = nav.userAgentData; const h = await u.getHighEntropyValues(['platformVersion', 'architecture', 'bitness', 'fullVersionList']); return { brands: u.brands, platform: u.platform, mobile: u.mobile, h }; } catch (e) { return 'ERR ' + e; } })();
  if (raw.uach && raw.uach.platform) {
    const uaPlat = /Windows/.test(nav.userAgent) ? 'Windows' : /Mac OS/.test(nav.userAgent) ? 'macOS' : /Linux/.test(nav.userAgent) ? 'Linux' : '?';
    if (raw.uach.platform !== uaPlat) add('uach-platform', 'FAIL', 'UA says ' + uaPlat + ', client hints say ' + raw.uach.platform);
    const major = (nav.userAgent.match(/Chrome\/(\d+)/) || [])[1];
    const hb = (raw.uach.brands || []).find(b => /Google Chrome|Chromium/.test(b.brand));
    if (hb && major && hb.version !== major) add('uach-version', 'FAIL', 'UA major ' + major + ' vs brand ' + hb.version);
    if (raw.uach.brands && !raw.uach.brands.some(b => /Not.?A.?Brand/i.test(b.brand))) add('uach-grease', 'WARN', 'no GREASE brand in userAgentData.brands: ' + raw.uach.brands.map(b => b.brand).join());
  } else add('uach-missing', 'WARN', 'navigator.userAgentData is unavailable (insecure page?)');
  if (/Windows/.test(nav.userAgent) && nav.platform !== 'Win32') add('platform', 'FAIL', 'navigator.platform ' + nav.platform);

  // 7. locale / language coherence
  const lang0 = (nav.languages && nav.languages[0]) || nav.language;
  if (raw.locale && lang0 && raw.locale.split('-')[0] !== lang0.split('-')[0]) add('locale-vs-language', 'FAIL', raw.locale + ' vs ' + lang0);
  if (nav.languages.length === 1) add('single-language', 'WARN', 'navigator.languages has one entry (' + lang0 + '); a stock Chrome lists at least two');

  // 8. window geometry
  const s = raw.screen;
  if (s.outer[0] < 300 || s.outer[1] < 200) add('window-minimized', 'WARN', 'outer window ' + s.outer.join('x') + ' (a minimised window reports 160x28)');
  if (s.outer[0] === s.inner[0] && s.outer[1] === s.inner[1]) add('no-window-chrome', 'WARN', 'outer equals inner size: no browser UI');
  if (s.s[0] === 800 && s.s[1] === 600) add('default-screen', 'FAIL', '800x600 default');

  // 9. permissions
  raw.perm = await (async () => { try { const q = await nav.permissions.query({ name: 'notifications' }); return { notif: Notification.permission, query: q.state }; } catch (e) { return 'ERR ' + e; } })();
  if (raw.perm && raw.perm.notif === 'denied' && raw.perm.query === 'prompt') add('notification-permission', 'FAIL', 'Notification.permission denied but query says prompt (headless tell)');

  // 10. devices
  raw.media = await (async () => { try { return (await nav.mediaDevices.enumerateDevices()).map(x => x.kind); } catch (e) { return 'ERR ' + e; } })();
  if (Array.isArray(raw.media)) {
    if (!raw.media.length) add('no-media-devices', 'WARN', 'enumerateDevices is empty');
    else if (!raw.media.includes('audioinput')) add('no-microphone', 'WARN', 'no audioinput listed: ' + raw.media.join(',') + ' (fine if this machine has no microphone)');
  }
  raw.voices = t(() => speechSynthesis.getVoices().length);
  if (raw.voices === 0) add('no-voices', 'WARN', 'speechSynthesis lists no voices (voices load asynchronously; re-run once)');
  raw.plugins = t(() => nav.plugins.length);
  if (raw.plugins === 0) add('no-plugins', 'FAIL', 'navigator.plugins is empty');

  // 11. WebRTC: a stock Chrome always yields at least an mDNS host candidate
  raw.rtc = await (async () => {
    try {
      const pc = new RTCPeerConnection({});
      const c = [];
      pc.onicecandidate = e => { if (e.candidate) c.push(e.candidate.type + ' ' + e.candidate.address); };
      pc.createDataChannel('x');
      await pc.setLocalDescription(await pc.createOffer());
      await new Promise(r => setTimeout(r, 2500));
      pc.close();
      return c;
    } catch (e) { return 'ERR ' + e; }
  })();
  if (Array.isArray(raw.rtc)) {
    if (!raw.rtc.length) add('webrtc-no-candidates', 'WARN', 'RTCPeerConnection produced no ICE candidates; stock Chrome on the same machine produces mDNS host candidates (measured). Comes from the browser build, not launch flags');
    const pub = raw.rtc.filter(x => !/\.local$/.test(x.split(' ')[1] || '') && !/^(10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.)/.test(x.split(' ')[1] || ''));
    if (pub.length) add('webrtc-ip-leak', 'WARN', 'ICE reveals ' + pub.join('; '));
  }

  // 12. canvas stability, timers, stack, connection
  raw.canvasStable = t(() => { const mk = () => { const c = document.createElement('canvas'); c.width = 200; c.height = 40; const x = c.getContext('2d'); x.font = '16px Arial'; x.fillText('cloak test 123', 4, 20); return c.toDataURL(); }; return mk() === mk(); });
  if (raw.canvasStable === false) add('canvas-unstable', 'FAIL', 'two identical canvas reads differ: per-read noise is detectable');
  raw.conn = t(() => nav.connection && { rtt: nav.connection.rtt, dl: nav.connection.downlink, et: nav.connection.effectiveType });
  if (raw.conn && raw.conn.rtt === 0) add('connection-rtt-zero', 'FAIL', 'navigator.connection.rtt is 0 (headless tell)');

  // 13. what a server sees, and whether the IP agrees with the browser
  if (network) {
    raw.headers = await (async () => { try { return (await (await fetch('https://httpbin.org/headers')).json()).headers; } catch (e) { return 'ERR ' + e; } })();
    if (raw.headers && typeof raw.headers === 'object') {
      const al = raw.headers['Accept-Language'] || '';
      if (al && !al.startsWith(nav.languages[0])) add('accept-language', 'FAIL', 'header ' + al + ' vs navigator ' + nav.languages.join());
      if (!raw.headers['Sec-Ch-Ua']) add('sec-ch-ua-missing', 'FAIL', 'requests carry no Sec-CH-UA header although the page reports client hints');
      const expect = nav.languages.join(',');
      const got = al.split(',').map(x => x.split(';')[0]).join(',');
      if (al && got !== expect) add('accept-language-list', 'WARN', 'header ' + got + ' differs from navigator.languages ' + expect);
    } else add('headers-probe', 'WARN', 'could not read request headers: ' + raw.headers);
    raw.geo = await (async () => {
      for (const u of ['https://ipinfo.io/json', 'https://ipapi.co/json/', 'https://api.country.is/']) {
        try { const j = await (await fetch(u)).json(); if (j && (j.ip || j.country)) return { via: u, ip: j.ip, country: j.country || j.country_code, tz: j.timezone || null, city: j.city || null, org: j.org || j.asn || null }; } catch (e) { /* next */ }
      }
      return null;
    })();
    if (raw.geo && raw.geo.tz && raw.geo.tz !== raw.tz) add('timezone-vs-ip', 'FAIL', 'browser timezone ' + raw.tz + ' but the exit IP is in ' + raw.geo.tz + ' (' + (raw.geo.city || raw.geo.country) + '): use configure(timezone="auto") without a proxy, or a proxy (geoip is then automatic)');
    if (!raw.geo) add('geo-probe', 'WARN', 'no IP geolocation service answered; timezone/IP coherence not checked');
  }

  return { findings, raw };
}
