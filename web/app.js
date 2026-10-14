// 监控页前端：状态轮询 + MJPEG 流自愈 + 参数设置 + 深度取点 + 骨骼叠加
//
// 自愈为什么要三层：Chromium 对 multipart/x-mixed-replace 断流**通常不触发 img.onerror**，
// 画面会静默定在最后一帧（页面其它元素照常动）。判据：
//   1) 画面活性探针：把 <img> 画进 64x36 小 canvas 取哈希 —— 直接测「看到的有没有在变」，不会漏判
//   2) 服务启动标识 boot：服务重启 -> 立即重连
//   3) 帧号看门狗 seq：服务活着但不再出帧时兜底
(function () {
  const $ = (id) => document.getElementById(id);
  const set = (id, v) => { const e = $(id); if (e && v !== undefined && v !== null) e.textContent = v; };
  const num = (v, s) => (v === undefined || v === null || v === '') ? '—' : (Math.round(v) + (s || ''));

  const IMGS = Array.from(document.querySelectorAll('img'));
  const STALL_LIMIT = 3, STILL_LIMIT = 6;
  const lastSeq = {}, stall = {}, lastHash = {}, still = {};
  let lastBoot = null;

  // ?snap=1 静态模式：把 MJPEG 流换成「每秒拉一次快照」。
  // 用途：弱网/跨网段、要看单帧截图、嵌入仪表盘、以及无头浏览器截图
  //（MJPEG 是长连接，headless 的"加载完成"永远不触发，截出来是空白页）。
  const SNAP_MODE = /(^|[?&])snap=1(&|$)/.test(location.search);
  if (SNAP_MODE) {
    IMGS.forEach((img) => {
      const name = (img.getAttribute('src') || '').replace(/^\//, '').replace('.mjpg', '');
      const file = name === 'color' ? '/snapshot/color.jpg'
        : name === 'depth' ? '/snapshot/depth.jpg' : '/snapshot/ir.jpg';
      img.dataset.snap = file;
      img.src = file + '?t=' + Date.now();
    });
    setInterval(() => {
      IMGS.forEach((img) => {
        if (img.hidden || !img.getClientRects().length) return;
        const f = img.dataset.snap;
        if (f) img.src = f + '?t=' + Date.now();
      });
    }, 1000);
  }

  function kick(img) {
    if (!img) return;
    const base = img.dataset.snap || (img.getAttribute('src') || '').split('?')[0];
    img.setAttribute('src', base + '?t=' + Date.now());
  }
  function kickAll() { IMGS.forEach(kick); }
  IMGS.forEach((img) => img.addEventListener('error', () => setTimeout(() => kick(img), 1500)));

  // ---- 判据 1：画面活性探针 ----
  const PW = 64, PH = 36;
  const cv = document.createElement('canvas');
  cv.width = PW; cv.height = PH;
  const ctx = cv.getContext('2d', { willReadFrequently: true });
  function frameHash(img) {
    try {
      ctx.drawImage(img, 0, 0, PW, PH);
      const d = ctx.getImageData(0, 0, PW, PH).data;
      let h = 0;
      for (let i = 0; i < d.length; i += 4) h = (h * 31 + d[i] + d[i + 1] * 7 + d[i + 2] * 13) | 0;
      return h;
    } catch (e) { return null; }
  }
  function checkAlive() {
    IMGS.forEach((img, i) => {
      if (img.hidden || !img.getClientRects().length) return;   // 隐藏的面板不判活性
      const k = 'i' + i;
      const h = frameHash(img);
      if (h === null) return;
      if (h === lastHash[k]) {
        still[k] = (still[k] || 0) + 1;
        if (still[k] >= STILL_LIMIT) { kick(img); still[k] = 0; lastHash[k] = null; }
      } else { lastHash[k] = h; still[k] = 0; }
    });
  }

  function tickClock() {
    const d = new Date(), p = (n) => String(n).padStart(2, '0');
    set('clock', p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds()));
  }
  tickClock(); setInterval(tickClock, 500);

  // ================= 画面框自适应（骨骼 canvas / 十字要对齐） =================
  // <img> 用 width:100%;height:auto 撑满 .imgwrap，所以只要把 .imgwrap 按原图宽高比
  // 缩放成「在 stage 内尽量大」的尺寸，canvas 和十字的定位就天然对齐。
  function fitWrap(stageId, wrapId) {
    const stage = $(stageId), wrap = $(wrapId);
    if (!stage || !wrap) return;
    const img = wrap.querySelector('img');
    const nw = img && img.naturalWidth, nh = img && img.naturalHeight;
    if (!nw || !nh) return;
    const pad = 12; // stage 内边距
    const r = stage.getBoundingClientRect();
    const availW = Math.max(80, r.width - pad), availH = Math.max(60, r.height - pad);
    let w = availW, h = w * nh / nw;
    if (h > availH) { h = availH; w = h * nw / nh; }
    wrap.style.width = w + 'px';
    wrap.style.height = h + 'px';
  }
  const PANELS = [['stageColor', 'wrapColor'], ['stageDepth', 'wrapDepth'], ['stageIr', 'wrapIr']];
  function fitAll() { PANELS.forEach(([s, w]) => fitWrap(s, w)); }
  window.addEventListener('resize', fitAll);
  IMGS.forEach((img) => img.addEventListener('load', fitAll));

  // ================= 深度取点 =================
  const dImg = $('depthImg'), dWrap = $('wrapDepth'), dCross = $('depthCross'), dReadout = $('depthReadout');
  let askBusy = false, askTimer = null;

  function moveCross(e) {
    if (!dWrap || !dCross) return;
    const r = dWrap.getBoundingClientRect();
    dCross.hidden = false;
    dCross.style.left = (e.clientX - r.left) + 'px';
    dCross.style.top = (e.clientY - r.top) + 'px';
  }

  async function askDepth(e) {
    if (!dWrap || !dImg) return;
    const r = dWrap.getBoundingClientRect();
    const nw = dImg.naturalWidth || 320, nh = dImg.naturalHeight || 240;
    const x = Math.max(0, Math.min(nw - 1, Math.floor((e.clientX - r.left) / r.width * nw)));
    const y = Math.max(0, Math.min(nh - 1, Math.floor((e.clientY - r.top) / r.height * nh)));
    if (askBusy) return;
    askBusy = true;
    try {
      const res = await fetch('/api/depth_at?x=' + x + '&y=' + y, { cache: 'no-store' });
      const j = await res.json();
      if (j.ok && j.valid) {
        set('depthReadout', '(' + x + ',' + y + ')  ' + Math.round(j.mm) + ' mm');
        dCross.className = 'cross ok';
      } else if (j.ok) {
        set('depthReadout', '(' + x + ',' + y + ')  该点无有效深度');
        dCross.className = 'cross bad';
      } else {
        set('depthReadout', j.error || '取点失败');
      }
    } catch (err) {
      set('depthReadout', '取点失败：' + err);
    } finally {
      askBusy = false;
    }
  }

  if (dWrap) {
    dWrap.addEventListener('mousemove', (e) => {
      moveCross(e);
      if (askTimer) return;                    // 约 70ms 节流，避免刷爆
      askTimer = setTimeout(() => { askTimer = null; askDepth(e); }, 70);
    });
    dWrap.addEventListener('mouseleave', () => {
      if (dCross) dCross.hidden = true;
      set('depthReadout', '鼠标移到画面上可取该点深度');
    });
  }

  // ================= 骨骼叠加 =================
  // 33 个关键点的标准骨架连接（MediaPipe POSE_CONNECTIONS）
  const EDGES = [
    [0, 1], [1, 2], [2, 3], [3, 7], [0, 4], [4, 5], [5, 6], [6, 8], [9, 10],
    [11, 12], [11, 13], [13, 15], [15, 17], [15, 19], [15, 21], [17, 19],
    [12, 14], [14, 16], [16, 18], [16, 20], [16, 22], [18, 20],
    [11, 23], [12, 24], [23, 24], [23, 25], [24, 26], [25, 27], [26, 28],
    [27, 29], [28, 30], [29, 31], [30, 32], [27, 31], [28, 32]
  ];
  const VIS_MIN = 0.35;   // 低于这个可见度的点不画（腿在桌子下面时模型会瞎猜）
  const poseCv = $('poseCanvas'), poseToggle = $('poseToggle');
  let poseOn = true, poseData = null;

  function fitPoseCanvas() {
    const cImg = $('colorImg');
    if (!poseCv || !cImg) return;
    const nw = cImg.naturalWidth || 1280, nh = cImg.naturalHeight || 720;
    if (poseCv.width !== nw || poseCv.height !== nh) { poseCv.width = nw; poseCv.height = nh; }
  }

  function drawPose() {
    if (!poseCv) return;
    const g = poseCv.getContext('2d');
    g.clearRect(0, 0, poseCv.width, poseCv.height);
    if (!poseOn || !poseData || !poseData.landmarks || !poseData.landmarks.length) return;
    const W = poseCv.width, H = poseCv.height, L = poseData.landmarks;
    const ok = (i) => L[i] && (L[i][3] === undefined || L[i][3] >= VIS_MIN);

    // 先描深色粗边再画亮线，避免背景浅色时看不清
    g.lineCap = 'round'; g.lineJoin = 'round';
    for (const pass of [{ w: 6, c: 'rgba(0,0,0,.55)' }, { w: 3, c: '#38bdf8' }]) {
      g.lineWidth = pass.w; g.strokeStyle = pass.c;
      g.beginPath();
      for (const [a, b] of EDGES) {
        if (!ok(a) || !ok(b)) continue;
        g.moveTo(L[a][0] * W, L[a][1] * H);
        g.lineTo(L[b][0] * W, L[b][1] * H);
      }
      g.stroke();
    }
    for (let i = 0; i < L.length; i++) {
      if (!ok(i)) continue;
      const x = L[i][0] * W, y = L[i][1] * H;
      g.beginPath(); g.arc(x, y, 4.5, 0, 6.2832);
      g.fillStyle = i <= 10 ? '#fbbf24' : '#38bdf8';   // 头面部用暖色
      g.fill();
      g.lineWidth = 1.5; g.strokeStyle = 'rgba(0,0,0,.6)'; g.stroke();
    }
  }

  async function pollPose() {
    if (!poseOn) return;
    try {
      const r = await fetch('/api/pose', { cache: 'no-store' });
      const j = await r.json();
      poseData = j.present ? (j.data || null) : null;
      drawPose();
    } catch (e) { poseData = null; drawPose(); }
  }

  if (poseToggle) {
    poseToggle.checked = poseOn;
    poseToggle.addEventListener('change', () => {
      poseOn = poseToggle.checked;
      if (!poseOn) { poseData = null; drawPose(); }
      else pollPose();
    });
  }
  setInterval(pollPose, 120);   // 侧车约 8fps，前端 8fps 取一次足够跟手

  // ================= 状态 =================
  function applyStatus(s) {
    const c = s.color || {}, d = s.depth || {}, r = s.relay || {}, ctrl = s.control || {};
    const ir = s.ir || {};
    const dot = $('dot');
    if (dot) dot.className = 'dot ' + (s.ok ? 'ok' : 'bad');
    // 深度/红外各跑独立 pipeline，可以同时在线；只有采集端关掉的那一路才隐藏
    const dOn = s.depth_enabled !== false, iOn = s.ir_enabled !== false;
    const pD = $('panelDepth'), pI = $('panelIr'), gD = $('grpDepth'), gI = $('grpIr');
    if (pD) pD.hidden = !dOn;
    if (pI) pI.hidden = !iOn;
    if (gD) gD.hidden = !dOn;
    if (gI) gI.hidden = !iOn;
    if (!dOn && dCross) dCross.hidden = true;

    const cm = c.meta || {}, dm = d.meta || {}, im = ir.meta || {};
    set('colorMeta', (cm.size || '—') + (c.fps ? '  ' + c.fps + ' fps' : '') +
      (c.state === 'ok' ? '' : '  ⚠ ' + (c.error || c.state)));
    set('depthMeta', (dm.size || '—') + (d.fps ? '  ' + d.fps + ' fps' : '') +
      (d.state === 'ok' ? '' : '  ⚠ ' + (d.error || d.state)));
    set('irMeta', (im.size || '—') + (ir.fps ? '  ' + ir.fps + ' fps' : '') +
      (ir.state === 'ok' ? '' : '  ⚠ ' + (ir.error || ir.state)));

    set('dCenter', num(dm.center_mm, ' mm'));
    set('dMin', num(dm.min_mm, ' mm'));
    set('dMax', num(dm.max_mm, ' mm'));
    set('dValid', num(dm.valid_pct, '%'));
    set('dRange', dm.range_mm ? (Math.round(dm.range_mm[0]) + '–' + Math.round(dm.range_mm[1]) + ' mm') : '—');
    set('irP50', num(im.p50));
    set('irP95', num(im.p95));
    set('irBright', num(im.bright_pct, '%'));

    set('dRatio', (dOn && dm.zlib_ratio) ? (dm.zlib_ratio + '×') : (iOn && im.zlib_ratio ? im.zlib_ratio + '×' : '—'));
    set('cFps', num(c.fps, ' fps'));
    set('dFps', num(d.fps, ' fps'));
    set('iFps', num(ir.fps, ' fps'));
    set('ingress', r.ingress_mbps ? (r.ingress_mbps + ' Mbps') : '—');
    set('temp', r.cpu_temp_c ? (r.cpu_temp_c + ' °C') : '—');
    set('host', location.host);

    const ps = s.pose || {};
    set('poseState', poseOn ? (ps.online ? '运行中' : '未运行') : '已关闭');

    if (s.boot) {
      if (lastBoot !== null && s.boot !== lastBoot) {
        Object.keys(lastSeq).forEach((k) => { lastSeq[k] = -1; });
        IMGS.forEach((_, i) => { stall['i' + i] = 0; });
        kickAll();
      }
      lastBoot = s.boot;
    }
    [{ img: $('colorImg'), f: c }, { img: $('depthImg'), f: d }, { img: $('irImg'), f: ir }].forEach(({ img, f }) => {
      if (!img || !f || f.seq === undefined) return;
      const k = img.id || Math.random();
      if (f.seq === lastSeq[k]) {
        stall[k] = (stall[k] || 0) + 1;
        if (stall[k] >= STALL_LIMIT) { kick(img); stall[k] = 0; lastSeq[k] = -1; }
      } else { lastSeq[k] = f.seq; stall[k] = 0; }
    });
    stall.http = 0;
    fitPoseCanvas();
    fitAll();
  }

  async function poll() {
    try {
      const r = await fetch('/api/status', { cache: 'no-store' });
      applyStatus(await r.json());
      checkAlive();
    } catch (e) {
      const dot = $('dot'); if (dot) dot.className = 'dot bad';
      stall.http = (stall.http || 0) + 1;
      if (stall.http >= STALL_LIMIT) { stall.http = 0; kickAll(); }
    }
  }
  poll(); setInterval(poll, 1000);

  // ================= 参数设置 =================
  let cfgLoaded = false, spec = null, curCam = {}, streamSpec = null;

  function el(tag, attrs, text) {
    const e = document.createElement(tag);
    if (attrs) for (const k in attrs) e.setAttribute(k, attrs[k]);
    if (text !== undefined) e.textContent = text;
    return e;
  }

  function buildCameraGrid(specList, values) {
    const box = $('cameraGrid');
    if (!box) return;
    box.innerHTML = '';
    (specList || []).forEach((s) => {
      const wrap = el('div', { class: 'ctl' });
      const isBool = s.min === undefined;
      wrap.appendChild(el('label', { for: 'cam_' + s.key }, s.label));
      if (isBool) {
        const cb = el('input', { type: 'checkbox', id: 'cam_' + s.key });
        cb.checked = String(values[s.key]) === '1' || values[s.key] === 1 || values[s.key] === true;
        wrap.appendChild(cb);
      } else {
        const r = el('input', { type: 'range', id: 'cam_' + s.key, min: s.min, max: s.max, step: s.step || 1 });
        r.value = values[s.key] !== undefined ? values[s.key] : s.default;
        const out = el('span', { class: 'val' }, r.value);
        r.addEventListener('input', () => { out.textContent = r.value; });
        wrap.appendChild(r);
        wrap.appendChild(out);
      }
      box.appendChild(wrap);
    });
  }

  function buildStreamGrid(specList, values) {
    const box = $('streamGrid');
    if (!box) return;
    box.innerHTML = '';
    (specList || []).forEach((s) => {
      const wrap = el('div', { class: 'ctl' });
      wrap.appendChild(el('label', { for: 'st_' + s.key }, s.label));
      const sel = el('select', { id: 'st_' + s.key });
      (s.options || []).forEach((o) => {
        const lab = (s.option_labels && s.option_labels[o]) ? (o + '  ' + s.option_labels[o]) : o;
        const op = el('option', { value: o }, lab);
        if (String(values[s.key]) === String(o)) op.selected = true;
        sel.appendChild(op);
      });
      wrap.appendChild(sel);
      if (s.hint) wrap.appendChild(el('span', { class: 'dim hint' }, s.hint));
      box.appendChild(wrap);
    });
  }

  function collectCamera() {
    const out = {};
    (spec || []).forEach((s) => {
      const e = $('cam_' + s.key);
      if (!e) return;
      out[s.key] = (s.min === undefined) ? (e.checked ? 1 : 0) : parseInt(e.value, 10);
    });
    return out;
  }

  function collectStream() {
    const out = {};
    (streamSpec || []).forEach((s) => {
      const e = $('st_' + s.key);
      if (e) out[s.key] = e.value;
    });
    return out;
  }

  function msg(id, text, ok) {
    const e = $(id);
    if (!e) return;
    e.textContent = text || '';
    e.className = 'msg ' + (ok === undefined ? '' : (ok ? 'ok' : 'bad'));
    if (text) setTimeout(() => { if (e.textContent === text) e.textContent = ''; }, 9000);
  }

  async function loadConfig() {
    try {
      const r = await fetch('/api/config', { cache: 'no-store' });
      const j = await r.json();
      if (!j.ok || !j.relay) {
        msg('camMsg', '读取失败：' + (j.error || '采集端未连接'), false);
        msg('streamMsg', '读取失败：' + (j.error || '采集端未连接'), false);
        const g1 = $('cameraGrid'); if (g1) g1.innerHTML = '<span class="dim">采集端未连接，无法读取</span>';
        const g2 = $('streamGrid'); if (g2) g2.innerHTML = '<span class="dim">采集端未连接，无法读取</span>';
        return;
      }
      const rel = j.relay;
      spec = rel.camera_spec || [];
      streamSpec = rel.stream_spec || [];
      curCam = rel.camera || {};
      buildCameraGrid(spec, curCam);
      buildStreamGrid(streamSpec, rel.stream || {});
      cfgLoaded = true;
    } catch (e) {
      msg('camMsg', '读取失败：' + e, false);
    }
  }

  async function post(obj, msgId) {
    try {
      const r = await fetch('/api/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(obj)
      });
      const j = await r.json();
      if (j.ok) {
        msg(msgId, j.msg || '已应用', true);
        if (j.camera) { curCam = j.camera; buildCameraGrid(spec, curCam); }
        if (j.restart) {
          msg(msgId, (j.msg || '') + '（采集端重启中，画面几秒后恢复）', true);
          setTimeout(loadConfig, 9000);
        }
        return true;
      }
      msg(msgId, j.msg || j.error || '失败', false);
    } catch (e) {
      msg(msgId, '请求失败：' + e, false);
    }
    return false;
  }

  const box = $('settingsBox');
  if (box) {
    // 支持 #settings 深链接（便于直接分享"参数设置"给同事，也便于无头截图）
    if (location.hash === '#settings') box.open = true;
    box.addEventListener('toggle', () => { if (box.open && !cfgLoaded) loadConfig(); });
    $('camApply') && $('camApply').addEventListener('click', () =>
      post({ action: 'set_camera', controls: collectCamera() }, 'camMsg'));
    $('camReset') && $('camReset').addEventListener('click', async () => {
      await post({ action: 'reset_camera' }, 'camMsg');
      await loadConfig();
    });
    $('streamApply') && $('streamApply').addEventListener('click', () => {
      if (!streamSpec) return;
      post({ action: 'set_stream', values: collectStream() }, 'streamMsg');
    });
  }

  fitAll();
})();