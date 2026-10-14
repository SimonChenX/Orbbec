// 轮询 /api/status 更新页脚与标签；并自动救回卡住的 MJPEG 流。
//
// 为什么需要这套东西：Chromium 对 multipart/x-mixed-replace 断流**通常不触发 img.onerror**，
// 画面会静默定在最后一帧 —— 症状是页面其它元素（时钟、数字）还在动、两个画面却完全静止。
// 三层判据，任一层命中就重建连接：
//   1) 画面活性探针：把 <img> 画进 64x36 小 canvas 取哈希。这是唯一直接反映「看到的东西
//      是否还在变」的判据。（不能只靠服务器的 seq：服务重启后 seq 会从 0 重新计数，
//      浏览器看到「帧号在变」会误判为正常。）相机噪声保证静止画面每帧哈希也不同，实测不误报。
//   2) 服务启动标识 boot：服务重启 -> 立即重连。
//   3) 帧号看门狗 seq：服务活着但不再出帧时兜底。
(function () {
  const $ = (id) => document.getElementById(id);
  const set = (id, v) => { const e = $(id); if (e && v !== undefined && v !== null) e.textContent = v; };
  const num = (v, s) => (v === undefined || v === null) ? '—' : (Math.round(v) + (s || ''));

  const WATCH = [['colorImg', 'color'], ['depthImg', 'depth']];
  const STALL_LIMIT = 3;     // 帧号连续 N 秒不动 -> 重连
  const STILL_LIMIT = 6;     // 画面哈希连续 N 秒不变 -> 重连

  const lastSeq = {}, stall = {}, lastHash = {}, still = {};
  let lastBoot = null;

  function kick(img) {
    if (!img) return;
    const base = (img.getAttribute('src') || '').split('?')[0];
    img.setAttribute('src', base + '?t=' + Date.now());
  }
  function kickAll() { WATCH.forEach(([id]) => kick($(id))); }

  // img 自己报错时也重连（能触发就更快）
  WATCH.forEach(([id]) => {
    const img = $(id);
    if (img) img.addEventListener('error', () => setTimeout(() => kick(img), 1500));
  });

  // ---- 判据 1：画面活性探针 ----
  const PW = 64, PH = 36;
  const probeCv = document.createElement('canvas');
  probeCv.width = PW; probeCv.height = PH;
  const probeCtx = probeCv.getContext('2d', { willReadFrequently: true });

  function frameHash(img) {
    try {
      probeCtx.drawImage(img, 0, 0, PW, PH);
      const d = probeCtx.getImageData(0, 0, PW, PH).data;
      let h = 0;
      for (let i = 0; i < d.length; i += 4) {
        h = (h * 31 + d[i] + d[i + 1] * 7 + d[i + 2] * 13) | 0;
      }
      return h;
    } catch (e) {
      return null;
    }
  }

  function checkAlive() {
    WATCH.forEach(([id]) => {
      const img = $(id);
      if (!img) return;
      const h = frameHash(img);
      if (h === null) return;
      if (h === lastHash[id]) {
        still[id] = (still[id] || 0) + 1;
        if (still[id] >= STILL_LIMIT) {   // 画面真的不动了 -> 重建连接
          kick(img);
          still[id] = 0;
          lastHash[id] = null;
        }
      } else {
        lastHash[id] = h;
        still[id] = 0;
      }
    });
  }

  // ---- 判据 3：帧号看门狗 ----
  function checkWatch(st) {
    WATCH.forEach(([id, key]) => {
      const img = $(id);
      if (!img || !st || !st[key]) return;
      const seq = st[key].seq;
      if (seq === undefined || seq === null) return;
      if (seq === lastSeq[id]) {
        stall[id] = (stall[id] || 0) + 1;
        if (stall[id] >= STALL_LIMIT) {
          kick(img);
          stall[id] = 0;
          lastSeq[id] = -1;
        }
      } else {
        lastSeq[id] = seq;
        stall[id] = 0;
      }
    });
  }

  function tickClock() {
    const d = new Date();
    const p = (n) => String(n).padStart(2, '0');
    set('clock', p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds()));
  }
  tickClock(); setInterval(tickClock, 500);

  async function poll() {
    try {
      const r = await fetch('/api/status', { cache: 'no-store' });
      const s = await r.json();
      const c = s.color || {}, d = s.depth || {};
      const dot = $('dot');
      if (dot) dot.className = 'dot ' + (s.ok ? 'ok' : 'bad');

      const cm = c.meta || {};
      set('colorMeta', (cm.size || '—') + (c.fps ? '  ' + c.fps + ' fps' : '') +
        (c.state === 'ok' ? '' : '  ⚠ ' + (c.error || c.state)));

      const dm = d.meta || {};
      set('depthMeta', (dm.size || '—') + (d.fps ? '  ' + d.fps + ' fps' : '') +
        (d.state === 'ok' ? '' : '  ⚠ ' + (d.error || d.state)));

      set('dCenter', num(dm.center_mm, ' mm'));
      set('dMin', num(dm.min_mm, ' mm'));
      set('dMax', num(dm.max_mm, ' mm'));
      set('dValid', num(dm.valid_pct, '%'));
      set('dRange', dm.range_mm ? (Math.round(dm.range_mm[0]) + '–' + Math.round(dm.range_mm[1]) + ' mm') : '—');
      set('cFps', num(c.fps, ' fps'));
      set('dFps', num(d.fps, ' fps'));

      // ---- 判据 2：服务重启 -> 立即重连 ----
      if (s.boot) {
        if (lastBoot !== null && s.boot !== lastBoot) {
          lastSeq.color = -1; lastSeq.depth = -1;
          stall.color = 0; stall.depth = 0;
          kickAll();
        }
        lastBoot = s.boot;
      }

      checkWatch(s);
      checkAlive();
      stall.http = 0;
    } catch (e) {
      const dot = $('dot'); if (dot) dot.className = 'dot bad';
      stall.http = (stall.http || 0) + 1;
      if (stall.http >= STALL_LIMIT) { stall.http = 0; kickAll(); }
    }
  }
  poll(); setInterval(poll, 1000);
})();