/* =========================================================
   USB 相机控制台 · 前端逻辑
   - 原有: RUN/STOP、拍照、全屏、状态轮询、断流重连
   - 新增: 录像启停/计时、文件管理(预览/下载/删除)
   ========================================================= */
(function () {
  'use strict';

  const $ = (id) => document.getElementById(id);

  const stage = $('stage'), led = $('led'), ledText = $('ledText');
  const ledRec = $('ledRec'), ledRecText = $('ledRecText');
  const btnRun = $('btnRun'), runText = $('runText');
  const btnRec = $('btnRec'), recText = $('recText');
  const streamImg = $('stream');
  const recBadge = $('recBadge'), recTimer = $('recTimer');

  let running = false;
  let recording = false;
  let recordStartedAt = 0;
  let currentFilter = 'all';
  let fileList = [];
  let pendingDelete = null;
  let currentPreview = null;
  let currentRecordFile = '';

  /* ---------------- 通用 UI ---------------- */

  function toast(msg, isErr) {
    const t = $('toast');
    t.textContent = msg;
    t.classList.toggle('err', !!isErr);
    t.classList.add('show');
    clearTimeout(t._h);
    t._h = setTimeout(() => t.classList.remove('show'), 2600);
  }

  function fmtElapsed(sec) {
    sec = Math.max(0, Math.floor(sec));
    const h = Math.floor(sec / 3600);
    const m = Math.floor((sec % 3600) / 60);
    const s = sec % 60;
    const pad = (n) => String(n).padStart(2, '0');
    return h > 0 ? `${pad(h)}:${pad(m)}:${pad(s)}` : `${pad(m)}:${pad(s)}`;
  }

  function fmtSize(n) {
    if (n < 1024) return n + ' B';
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
    if (n < 1024 * 1024 * 1024) return (n / 1048576).toFixed(1) + ' MB';
    return (n / 1073741824).toFixed(2) + ' GB';
  }

  /* ---------------- 相机状态 ---------------- */

  function setRunning(on) {
    running = on;
    if (!on && yoloLiveRunning) stopYoloLive('RGB 已停止，连续检测自动结束');
    stage.classList.toggle('live', on);
    led.classList.toggle('on', on && !recording);
    led.classList.toggle('rec', on && recording);
    $('recBadge').classList.toggle('on', on);
    ledText.textContent = on ? 'LIVE' : 'IDLE';
    btnRun.classList.toggle('running', on);
    runText.textContent = on ? 'STOP 停止' : 'RUN 运行';
  }

  function setRecording(on, elapsed) {
    const wasOn = recording;
    recording = on;
    btnRec.classList.toggle('recording', on);
    recText.textContent = on ? '停止录像' : '开始录像';
    recBadge.classList.toggle('on', on);
    recTimer.classList.toggle('on', on);
    ledRec.style.display = on ? 'inline-flex' : 'none';
    if (on) {
      led.classList.remove('on');
      led.classList.add('rec');
      ledText.textContent = 'REC';
      $('tStatus').textContent = 'RECORDING';
      $('tStatus').className = 'v err';
      // 本地计时起点: 用后端 elapsed 反推, 这样刷新页面也能接着计
      if (!wasOn || elapsed == null) {
        recordStartedAt = Date.now() - (elapsed || 0) * 1000;
      }
    } else {
      recTimer.classList.remove('on');
      recordStartedAt = 0;
      if (running) {
        led.classList.remove('rec');
        led.classList.add('on');
        ledText.textContent = 'LIVE';
      }
    }
    if (on && elapsed != null) {
      recTimer.textContent = 'REC ' + fmtElapsed(elapsed);
    }
  }

  function applyStatus(s) {
    const starting = !!s.starting && !s.running;
    currentRecordFile = (s.recording && s.record_file) ? s.record_file : '';
    setRunning(!!s.running);
    const wasRec = recording;
    setRecording(!!s.recording, s.record_elapsed);

    if (starting) {
      $('tStatus').textContent = 'STARTING';
      $('tStatus').className = 'v warn';
      btnRun.disabled = true;
      runText.textContent = '启动中…';
    } else {
      $('tStatus').textContent = recording ? 'RECORDING' : (running ? 'RUNNING' : 'IDLE');
      $('tStatus').className = 'v' + (recording ? ' err' : (running ? ' ok' : ''));
      btnRun.disabled = false;
    }

    $('tIndex').textContent = (s.index != null) ? s.index : '—';
    $('tRes').textContent = (s.width && s.height) ? (s.width + ' × ' + s.height) : '—';
    $('tFps').textContent = (s.fps != null && s.running) ? (s.fps + ' fps') : '—';
    $('tFrames').textContent = (s.frames != null) ? s.frames : '—';
    $('tUptime').textContent = (s.running && s.uptime != null) ? (s.uptime + ' s') : '—';

    const fm = $('footMsg');
    if (s.last_error) {
      fm.textContent = 'ERROR · ' + s.last_error; fm.className = 'err';
    } else if (s.record_processing) {
      fm.textContent = 'ENCODING · 正在生成浏览器兼容 H.264'; fm.className = 'ok';
    } else if (s.record_error && !s.recording) {
      fm.textContent = 'REC ERROR · ' + s.record_error; fm.className = 'err';
    } else if (s.recording) {
      fm.textContent = 'RECORDING · ' + (s.record_file || '…') + ' · ' +
        fmtElapsed(s.record_elapsed || 0);
      fm.className = 'ok';
    } else if (starting) {
      fm.textContent = 'STARTING · 相机启动中…'; fm.className = '';
    } else if (s.running) {
      fm.textContent = 'LIVE · 画面传输中'; fm.className = 'ok';
    } else {
      fm.textContent = 'READY · 等待启动'; fm.className = '';
    }

    // 录像刚结束时刷新文件列表
    if (wasRec && !s.recording) {
      loadFiles(true);
    }
    if (s.record_error && !s.recording && wasRec) {
      toast('录像异常: ' + s.record_error, true);
    }
  }

  async function fetchStatus() {
    try {
      const r = await fetch('/status', { cache: 'no-store' });
      applyStatus(await r.json());
    } catch (e) {
      $('footMsg').textContent = 'ERROR · 无法连接服务端';
      $('footMsg').className = 'err';
    }
  }

  /* ---------------- 相机控制 ---------------- */

  async function toggleRun() {
    btnRun.disabled = true;
    try {
      const url = running ? '/stop' : '/start';
      const r = await fetch(url, { method: 'POST', cache: 'no-store' });
      const s = await r.json();
      applyStatus(s);
      if (s.last_error) {
        toast(s.last_error, true);
      } else {
        toast(running ? '相机已停止' : '相机已启动');
      }
      if (s.running) {
        streamImg.src = '/stream?t=' + Date.now();
      }
    } catch (e) {
      toast('操作失败: ' + e.message, true);
    } finally {
      btnRun.disabled = false;
      fetchStatus();
    }
  }

  async function toggleRec() {
    btnRec.disabled = true;
    try {
      const url = recording ? '/record/stop' : '/record/start';
      const r = await fetch(url, { method: 'POST', cache: 'no-store' });
      const s = await r.json();
      applyStatus(s);
      if (s.record_error && !s.recording) {
        toast(s.record_error, true);
      } else {
        toast(s.recording ? '开始录像: ' + (s.record_file || '') : '录像已保存');
      }
    } catch (e) {
      toast('录像操作失败: ' + e.message, true);
    } finally {
      btnRec.disabled = false;
      fetchStatus();
    }
  }

  async function snapshot() {
    try {
      const r = await fetch('/snapshot', { cache: 'no-store' });
      if (!r.ok) {
        const e = await r.json().catch(() => ({}));
        toast(e.error || '拍照失败', true);
        return;
      }
      const blob = await r.blob();
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = 'capture_' + Date.now() + '.jpg';
      a.click();
      setTimeout(() => URL.revokeObjectURL(a.href), 4000);
      toast('照片已下载');
      loadFiles(true);
    } catch (e) {
      toast('拍照失败: ' + e.message, true);
    }
  }

  function toggleFull() {
    if (!document.fullscreenElement) stage.requestFullscreen?.();
    else document.exitFullscreen?.();
  }

  /* ---------------- 文件管理 ---------------- */

  async function loadFiles(silent) {
    try {
      const r = await fetch('/files', { cache: 'no-store' });
      const d = await r.json();
      fileList = d.files || [];
      renderFiles();
      if (!silent) toast('文件列表已刷新');
    } catch (e) {
      if (!silent) toast('加载文件列表失败: ' + e.message, true);
    }
  }

  function renderFiles() {
    const grid = $('fileGrid');
    const items = fileList.filter((f) => currentFilter === 'all' || f.kind === currentFilter);
    $('fileCount').textContent = items.length;
    $('fileSync').textContent = '共 ' + fileList.length + ' 个文件';
    $('fileEmpty').style.display = items.length ? 'none' : 'block';

    grid.innerHTML = items.map((f) => {
      const isImg = f.kind === 'image';
      const isRecording = (f.name === currentRecordFile);
      const thumb = isImg
        ? `<img src="/file/${encodeURIComponent(f.name)}" alt="" loading="lazy">`
        : `<div class="play"><svg viewBox="0 0 24 24"><polygon points="8 5 19 12 8 19 8 5"/></svg></div>`;
      return `
        <div class="file-card${isRecording ? ' recording' : ''}" data-name="${encodeURIComponent(f.name)}">
          <div class="file-thumb" data-act="preview">
            ${thumb}
            <div class="kind">${isRecording ? 'REC…' : (isImg ? 'IMG' : 'VIDEO')}</div>
          </div>
          <div class="file-meta">
            <div class="file-name" title="${f.name}">${f.name}</div>
            <div class="file-sub"><span>${fmtSize(f.size)}</span><span>${f.mtime_str}</span></div>
          </div>
          <div class="file-acts">
            <div class="mini" data-act="preview">${isRecording ? '录制中…' : '预览'}</div>
            <div class="mini" data-act="download">下载</div>
            <div class="mini del" data-act="delete">删除</div>
          </div>
        </div>`;
    }).join('');
  }

  function findFile(name) {
    return fileList.find((f) => f.name === name) || null;
  }

  function openPreview(name) {
    const f = findFile(name);
    if (!f) return;
    currentPreview = f;
    $('previewTitle').textContent = f.name;
    const body = $('previewBody');
    if (f.kind === 'image') {
      body.innerHTML = `<img src="/file/${encodeURIComponent(f.name)}" alt="">`;
    } else {
      body.innerHTML =
        `<video controls autoplay playsinline id="previewVideo" ` +
        `src="/file/${encodeURIComponent(f.name)}"></video>` +
        `<div id="videoErr" style="display:none;margin-top:12px;padding:12px;` +
        `border:1px solid rgba(255,107,107,.35);border-radius:8px;` +
        `background:rgba(255,107,107,.08);color:var(--danger);font-family:var(--mono);font-size:11px">` +
        `视频加载失败。可能是浏览器不支持该编码，或文件正在录制中。<br>` +
        `建议点「下载」后用本地播放器打开。</div>`;
      // 加载失败兜底: mp4v 等编码浏览器放不了, 但可以下载
      const v = body.querySelector('#previewVideo');
      const errBox = body.querySelector('#videoErr');
      v.addEventListener('error', () => {
        errBox.style.display = 'block';
        v.style.display = 'none';
      });
      v.addEventListener('stalled', () => {
        if (v.readyState === 0) errBox.style.display = 'block';
      });
    }
    $('previewMask').classList.add('show');
  }

  function closePreview() {
    $('previewMask').classList.remove('show');
    const v = $('previewBody').querySelector('video');
    if (v) { try { v.pause(); } catch (e) {} }
    $('previewBody').innerHTML = '';
    currentPreview = null;
  }

  function downloadFile(name) {
    const a = document.createElement('a');
    a.href = '/file/' + encodeURIComponent(name) + '/download';
    a.download = name;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
  }

  function askDelete(name) {
    pendingDelete = name;
    $('confirmName').textContent = name;
    $('confirmMask').classList.add('show');
  }

  function closeConfirm() {
    $('confirmMask').classList.remove('show');
    pendingDelete = null;
  }

  async function doDelete() {
    const name = pendingDelete;
    if (!name) return;
    try {
      const r = await fetch('/file/' + encodeURIComponent(name), { method: 'DELETE' });
      const d = await r.json();
      if (d.ok) {
        toast('已删除 ' + name);
        closeConfirm();
        loadFiles(true);
      } else {
        toast(d.error || '删除失败', true);
      }
    } catch (e) {
      toast('删除失败: ' + e.message, true);
    }
  }

  /* ---------------- 颜色识别 (点击才识别, 不持续叠加) ---------------- */

  function rgbToCss(bgr) {
    return `rgb(${bgr[2]},${bgr[1]},${bgr[0]})`;
  }

  function nameColor(name) {
    const map = {
      '深红': '#8b1a1a', '红色': '#ff4d4f', '朱红': '#e23c2a', '橙红': '#ff5722',
      '橙色': '#fa8c16', '橘色': '#ff9800', '金黄': '#f5b400', '黄色': '#fadb14',
      '明黄': '#ffe33d', '黄绿': '#bfe04a', '草绿': '#7cb305', '绿色': '#52c41a',
      '翠绿': '#23c16b', '深绿': '#2f7d32', '青绿': '#13c2c2', '青色': '#00bcd4',
      '天蓝': '#16a3e0', '浅蓝': '#4fc0e8', '蓝色': '#1677ff', '宝蓝': '#2456c9',
      '靛蓝': '#123f8c', '深蓝': '#0b2a5b', '紫罗兰': '#8a4fff', '紫色': '#722ed1',
      '品红': '#eb2f96', '紫红': '#c2318c', '玫红': '#e0407a', '桃红': '#ff7eb6',
      '粉红': '#ffa8c5', '黑色': '#2b2f36', '深灰色': '#4a4f57', '灰色': '#8c8c8c',
      '浅灰色': '#c0c4cc', '白色': '#e6edf3'
    };
    return map[name] || '#3ddc84';
  }

  function renderDetectResult(d) {
    const n = d.count || 0;
    $('colorCount').textContent = `(${n} 个)`;
    const dets = d.detections || [];
    const dl = $('detList');
    if (!dets.length) {
      dl.innerHTML = '<div class="det-empty">未检出明显颜色区域</div>';
    } else {
      dl.innerHTML = dets.map((x) => `
        <div class="det-row">
          <span class="sw" style="background:${rgbToCss(x.color_bgr)}"></span>
          <span class="nm">${x.color}</span>
          <span class="ar" title="置信度">${(x.confidence * 100).toFixed(0)}%</span>
          <span class="ps">(${x.cx},${x.cy})</span>
        </div>`).join('');
    }
    const stats = d.stats || {};
    const keys = Object.keys(stats);
    const sl = $('statList');
    if (!keys.length) {
      sl.innerHTML = '<div class="det-empty">暂无统计</div>';
    } else {
      const max = Math.max(...keys.map((k) => stats[k]), 1);
      sl.innerHTML = keys.sort((a, b) => stats[b] - stats[a]).map((k) => {
        const pct = Math.round(stats[k] / max * 100);
        return `<div class="stat-row">
          <span class="nm">${k}</span>
          <span class="bar"><i style="width:${pct}%;background:${nameColor(k)}"></i></span>
          <span class="vv">${stats[k]}</span>
        </div>`;
      }).join('');
    }
    $('colorSummary').textContent =
      `最近识别: ${n} 个 · S≥${(d.thresholds||{}).s_min} V≥${(d.thresholds||{}).v_min}`;
    $('colorBadge').textContent = n > 0 ? 'OK' : '—';
  }

  function showAnnotated(d) {
    const mask = $('annotatedMask');
    const img = $('annotatedImg');
    img.src = '/color/annotated?t=' + Date.now();
    $('annotatedTitle').textContent = `识别结果 · ${d.count} 个区域`;
    const names = [...new Set((d.detections || []).map((x) => x.color))];
    $('annotatedSummary').textContent =
      `颜色种类: ${names.join('、') || '无'}　｜　阈值 S≥${(d.thresholds||{}).s_min} V≥${(d.thresholds||{}).v_min}　｜　灵敏度 ${(d.sensitivity ?? '')}`;
    mask.classList.add('show');
  }

  async function runDetect() {
    if (!running) {
      toast('请先点击 RUN 运行相机', true);
      return;
    }
    const btn = $('btnColorDetect');
    btn.disabled = true;
    $('colorDetectText').textContent = '识别中…';
    try {
      const r = await fetch('/color/detect', {
        method: 'POST', cache: 'no-store'
      });
      const d = await r.json();
      if (!d.ok) {
        toast(d.error || '识别失败', true);
        return;
      }
      renderDetectResult(d);
      showAnnotated(d);
      toast(`识别完成，检出 ${d.count} 个区域`);
    } catch (e) {
      toast('识别失败: ' + e.message, true);
    } finally {
      btn.disabled = false;
      $('colorDetectText').textContent = '识别整幅画面';
    }
  }

  let settingsTimer = null;
  function pushColorSettings() {
    clearTimeout(settingsTimer);
    settingsTimer = setTimeout(async () => {
      try {
        const body = {
          sensitivity: parseInt($('sensSlider').value, 10),
          min_area: parseInt($('areaSlider').value, 10),
        };
        await fetch('/color/settings', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        });
      } catch (e) { /* 静默 */ }
    }, 250);
  }

  function placeMarker(clientX, clientY, colorBgr, label) {
    // 在视频上画一个 DOM 标记, 不改视频本身
    const stage = $('stage');
    const rect = stage.getBoundingClientRect();
    const x = clientX - rect.left, y = clientY - rect.top;
    let el = stage.querySelector('.pick-marker');
    if (!el) {
      el = document.createElement('div');
      el.className = 'pick-marker';
      el.innerHTML = '<span></span>';
      stage.appendChild(el);
    }
    el.style.left = x + 'px';
    el.style.top = y + 'px';
    el.style.borderColor = rgbToCss(colorBgr || [255, 255, 255]);
    const span = el.querySelector('span');
    if (span) span.textContent = label || '';
    el.classList.add('show');
    clearTimeout(el._h);
    el._h = setTimeout(() => el.classList.remove('show'), 2600);
  }

  async function pickColorAt(clientX, clientY) {
    const img = $('stream');
    const rect = img.getBoundingClientRect();
    const iw = img.naturalWidth || 640, ih = img.naturalHeight || 480;
    const scale = Math.min(rect.width / iw, rect.height / ih);
    const offX = (rect.width - iw * scale) / 2;
    const offY = (rect.height - ih * scale) / 2;
    const ix = Math.round((clientX - rect.left - offX) / scale);
    const iy = Math.round((clientY - rect.top - offY) / scale);
    if (ix < 0 || iy < 0 || ix >= iw || iy >= ih) return;
    try {
      const r = await fetch(`/color/pick?x=${ix}&y=${iy}`, { cache: 'no-store' });
      const d = await r.json();
      if (!d.ok) { toast(d.error || '取色失败', true); return; }
      const p = d.pick;
      $('pickBox').innerHTML = `
        <div class="row"><span class="big" style="color:${nameColor(p.color)}">${p.color}</span>
          <span>置信度 ${(p.confidence * 100).toFixed(0)}%</span></div>
        <div class="row"><span class="sw" style="background:${rgbToCss(p.bgr)}"></span>
          <span>RGB (${p.bgr[2]}, ${p.bgr[1]}, ${p.bgr[0]})</span></div>
        <div class="row">HEX ${p.hex}　HSV (${p.hsv[0]}, ${p.hsv[1]}, ${p.hsv[2]})</div>
        <div class="row">坐标 (${p.x}, ${p.y})</div>`;
      placeMarker(clientX, clientY, p.bgr, p.color);
    } catch (e) {
      toast('取色失败: ' + e.message, true);
    }
  }

  function saveAnnotated() {
    const a = document.createElement('a');
    a.href = '/color/annotated?t=' + Date.now();
    a.download = 'color_annotated_' + Date.now() + '.jpg';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    toast('标注图已下载');
  }

  /* ---------------- 文字识别 (点击才识别) ---------------- */

  function renderOcrResult(d) {
    const n = d.count || 0;
    $('ocrCount').textContent = `(${n} 段 · ${d.elapsed_ms || 0} ms)`;
    $('ocrBadge').textContent = n > 0 ? `${n} 段` : '无文字';
    $('ocrBadge').style.color = n > 0 ? 'var(--accent)' : 'var(--muted)';
    $('ocrBadge').style.borderColor = n > 0 ? 'rgba(61,220,132,.45)' : 'var(--line)';
    $('ocrSummary').textContent = n > 0
      ? `最近识别 ${n} 段 · 耗时 ${d.elapsed_ms} ms${d.only_alnum ? ' · 仅字母/数字' : ''}`
      : '字母 / 数字 / 中文 · 点击才识别';

    const items = d.results || [];
    const list = $('ocrList');
    if (!items.length) {
      list.innerHTML = '<div class="det-empty">当前画面未识别到文字</div>';
      return;
    }
    list.innerHTML = items.map((it, i) => `
      <div class="det-row">
        <span class="ar" style="color:var(--muted);min-width:22px">#${i + 1}</span>
        <span class="nm" style="font-family:var(--mono);font-size:12px;color:var(--ink)">${escapeHtml(it.text)}</span>
        <span class="ar" style="color:${it.is_alnum ? 'var(--accent)' : 'var(--muted)'}">${it.is_alnum ? '字母/数字' : '其它'}</span>
        <span class="ps" style="color:var(--accent)">${(it.confidence * 100).toFixed(1)}%</span>
      </div>`).join('');
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    }[c]));
  }

  function showOcrAnnotated(d) {
    $('ocrImg').src = '/ocr/annotated?t=' + Date.now();
    $('ocrModalTitle').textContent = `文字识别结果 · ${d.count} 段`;
    const texts = (d.results || []).map((x) => x.text).join('  ｜  ');
    $('ocrModalSummary').textContent =
      `识别内容: ${texts || '无'}　｜　耗时 ${d.elapsed_ms} ms　｜　${d.only_alnum ? '仅字母/数字' : '全部文本'}`;
    $('ocrMask').classList.add('show');
  }

  async function runOcr() {
    if (!running) {
      toast('请先点击 RUN 运行相机', true);
      return;
    }
    const btn = $('btnOcr');
    btn.disabled = true;
    $('ocrBtnText').textContent = '识别中…';
    try {
      const r = await fetch('/ocr', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ only_alnum: $('ocrAlnumOnly').checked }),
        cache: 'no-store',
      });
      const d = await r.json();
      if (!d.ok) {
        toast(d.error || '识别失败', true);
        if (d.error && d.error.indexOf('加载') >= 0) {
          $('ocrSummary').textContent = d.error;
        }
        return;
      }
      renderOcrResult(d);
      showOcrAnnotated(d);
      toast(d.count > 0 ? `识别到 ${d.count} 段文字` : '未识别到文字');
    } catch (e) {
      toast('识别失败: ' + e.message, true);
    } finally {
      btn.disabled = false;
      $('ocrBtnText').textContent = '识别画面文字';
    }
  }

  function saveOcrAnnotated() {
    const a = document.createElement('a');
    a.href = '/ocr/annotated?t=' + Date.now();
    a.download = 'ocr_annotated_' + Date.now() + '.jpg';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    toast('标注图已下载');
  }

  /* ---------------- 二维码识别 (点击才识别) ---------------- */

  function renderQrResult(d) {
    const n = d.count || 0;
    $('qrCount').textContent = `(${n} 个 · ${d.elapsed_ms || 0} ms)`;
    $('qrBadge').textContent = n > 0 ? `${n} 个` : '未检出';
    $('qrBadge').style.color = n > 0 ? 'var(--accent)' : 'var(--muted)';
    $('qrBadge').style.borderColor = n > 0 ? 'rgba(61,220,132,.45)' : 'var(--line)';
    $('qrSummary').textContent = n > 0
      ? `最近识别 ${n} 个 · 耗时 ${d.elapsed_ms} ms`
      : 'QR 码 · 支持一图多码 · 点击才识别';

    const items = d.results || [];
    const list = $('qrList');
    if (!items.length) {
      list.innerHTML = '<div class="det-empty">当前画面未识别到二维码</div>';
      return;
    }
    list.innerHTML = items.map((it, i) => `
      <div class="det-row" style="align-items:flex-start;padding:10px">
        <span class="ar" style="color:var(--muted);min-width:22px">#${i + 1}</span>
        <span class="nm" style="flex:2;font-family:var(--mono);font-size:12px;color:var(--ink);word-break:break-all">${escapeHtml(it.text)}</span>
        <span class="ar" style="min-width:52px;color:var(--accent)">${it.type}</span>
      </div>`).join('');
  }

  function showQrAnnotated(d) {
    $('qrImg').src = '/qr/annotated?t=' + Date.now();
    $('qrModalTitle').textContent = `二维码识别结果 · ${d.count} 个`;
    const texts = (d.results || []).map((x) => x.text).join('  ｜  ');
    $('qrModalSummary').textContent =
      `识别内容: ${texts || '无'}　｜　耗时 ${d.elapsed_ms} ms`;
    $('qrMask').classList.add('show');
  }

  async function runQr() {
    if (!running) {
      toast('请先点击 RUN 运行相机', true);
      return;
    }
    const btn = $('btnQr');
    btn.disabled = true;
    $('qrBtnText').textContent = '识别中…';
    try {
      const r = await fetch('/qr', { method: 'POST', cache: 'no-store' });
      const d = await r.json();
      if (!d.ok) { toast(d.error || '识别失败', true); return; }
      renderQrResult(d);
      showQrAnnotated(d);
      toast(d.count > 0 ? `识别到 ${d.count} 个二维码` : '未识别到二维码');
    } catch (e) {
      toast('识别失败: ' + e.message, true);
    } finally {
      btn.disabled = false;
      $('qrBtnText').textContent = '识别二维码';
    }
  }

  function saveQrAnnotated() {
    const a = document.createElement('a');
    a.href = '/qr/annotated?t=' + Date.now();
    a.download = 'qr_annotated_' + Date.now() + '.jpg';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    toast('标注图已下载');
  }

  /* ---------------- 目标检测 / 水果识别 (点击才识别) ---------------- */

  let yoloLiveRunning = false;
  let yoloLiveTimer = 0;
  let yoloDetectionBusy = false;

  function renderYoloResult(d) {
    const n = d.count || 0;
    $('yoloCount').textContent = `(${n} 个 · 水果 ${d.fruit_count || 0} 个 · ${d.elapsed_ms || 0} ms)`;
    $('yoloBadge').textContent = n > 0 ? `${n} 个` : '未检出';
    $('yoloBadge').style.color = n > 0 ? 'var(--accent)' : 'var(--muted)';
    $('yoloBadge').style.borderColor = n > 0 ? 'rgba(61,220,132,.45)' : 'var(--line)';
    $('yoloSummary').textContent = n > 0
      ? `最近检测 ${n} 个 · 耗时 ${d.elapsed_ms} ms · 置信度≥${(d.conf ?? 0.35).toFixed(2)}`
      : 'YOLOv8 · 80 类 · 水果含香蕉/苹果/橙子';

    const items = d.results || [];
    const list = $('yoloList');
    if (!items.length) {
      list.innerHTML = `<div class="det-empty">${d.only_fruit ? '未检出水果（可去掉"只看水果"试试）' : '当前画面未检出物体'}</div>`;
    } else {
      list.innerHTML = items.slice(0, 30).map((it) => `
        <div class="det-row">
          <span class="sw" style="background:${it.is_fruit ? 'var(--accent)' : 'var(--warn)'}"></span>
          <span class="nm">${it.label_cn} <span style="color:var(--muted)">${it.label_en}</span>${it.distance_mm ? ` <span style="color:var(--accent)">${it.distance_mm}mm ${it.width_cm}x${it.height_cm}cm</span>` : ''}</span>
          <span class="ar" style="color:var(--accent)">${(it.confidence * 100).toFixed(1)}%</span>
          <span class="ps">(${it.bbox[0]},${it.bbox[1]})</span>
        </div>`).join('');
    }

    const stats = d.stats || {};
    const keys = Object.keys(stats);
    const sl = $('yoloStatList');
    if (!keys.length) {
      sl.innerHTML = '<div class="det-empty">暂无统计</div>';
    } else {
      const max = Math.max(...keys.map((k) => stats[k]), 1);
      sl.innerHTML = keys.sort((a, b) => stats[b] - stats[a]).map((k) => {
        const pct = Math.round(stats[k] / max * 100);
        return `<div class="stat-row">
          <span class="nm" style="min-width:56px">${k}</span>
          <span class="bar"><i style="width:${pct}%;background:var(--accent)"></i></span>
          <span class="vv">${stats[k]} 个</span>
        </div>`;
      }).join('');
    }
  }

  function showYoloAnnotated(d) {
    $('yoloImg').src = '/yolo/annotated?t=' + Date.now();
    $('yoloModalTitle').textContent = `目标检测结果 · ${d.count} 个`;
    const names = [...new Set((d.results || []).map((x) => x.label_cn))];
    $('yoloModalSummary').textContent =
      `检出类别: ${names.join('、') || '无'}　｜　耗时 ${d.elapsed_ms} ms　｜　${d.only_fruit ? '仅水果' : '全部物体'}`;
    $('yoloMask').classList.add('show');
  }

  async function runYolo({ continuous = false, silent = false } = {}) {
    if (!running) {
      if (!silent) toast('请先点击 RUN 运行相机', true);
      return false;
    }
    if (yoloDetectionBusy) return false;
    yoloDetectionBusy = true;
    const btn = $('btnYolo');
    btn.disabled = true;
    $('yoloBtnText').textContent = '检测中…';
    try {
      const r = await fetch('/yolo', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          only_fruit: $('yoloFruitOnly').checked,
          conf: parseInt($('yoloConfSlider').value, 10) / 100,
        }),
        cache: 'no-store',
      });
      const d = await r.json();
      if (!d.ok) {
        if (!silent) toast(d.error || '检测失败', true);
        return false;
      }
      renderYoloResult(d);
      if (continuous) {
        $('yoloLiveFrame').src = '/yolo/annotated?t=' + Date.now();
      } else {
        showYoloAnnotated(d);
      }
      if (!silent) {
        toast(d.count > 0
          ? `检出 ${d.count} 个目标（水果 ${d.fruit_count || 0} 个）`
          : '未检出目标');
      }
      return true;
    } catch (e) {
      if (!silent) toast('检测失败: ' + e.message, true);
      return false;
    } finally {
      yoloDetectionBusy = false;
      btn.disabled = yoloLiveRunning;
      $('yoloBtnText').textContent = yoloLiveRunning ? '连续检测中…' : '识别目标';
    }
  }

  function stopYoloLive(message = '连续检测已停止') {
    yoloLiveRunning = false;
    clearTimeout(yoloLiveTimer);
    yoloLiveTimer = 0;
    $('panelYolo').classList.remove('yolo-live-active');
    $('btnYoloLive').textContent = '开启连续检测';
    $('btnYolo').disabled = false;
    $('yoloBtnText').textContent = '识别目标';
    if (message) toast(message);
  }

  async function yoloLiveTick() {
    if (!yoloLiveRunning) return;
    if (!running) {
      stopYoloLive('RGB 已停止，连续检测自动结束');
      return;
    }
    const ok = await runYolo({ continuous: true, silent: true });
    if (!yoloLiveRunning) return;
    if (!ok) {
      stopYoloLive('本轮 YOLO 检测失败，连续检测已停止');
      return;
    }
    const interval = parseInt($('yoloLiveInterval').value, 10) || 1500;
    yoloLiveTimer = setTimeout(yoloLiveTick, interval);
  }

  function toggleYoloLive() {
    if (yoloLiveRunning) {
      stopYoloLive();
      return;
    }
    if (!running) {
      toast('请先点击 RUN 运行相机，再开启连续检测', true);
      return;
    }
    yoloLiveRunning = true;
    $('panelYolo').classList.add('yolo-live-active');
    $('btnYoloLive').textContent = '停止连续检测';
    $('btnYolo').disabled = true;
    $('yoloBtnText').textContent = '连续检测中…';
    toast('YOLO 连续检测已开启');
    yoloLiveTick();
  }

  function saveYoloAnnotated() {
    const a = document.createElement('a');
    a.href = '/yolo/annotated?t=' + Date.now();
    a.download = 'yolo_annotated_' + Date.now() + '.jpg';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    toast('标注图已下载');
  }

  /* ---------------- 深度图 / 测距 ---------------- */

  let depthActive = false;

  function renderDepthStatus(s) {
    depthActive = !!s.mode;
    $('depthBadge').textContent = depthActive ? '运行中' : '关闭';
    $('depthBadge').style.color = depthActive ? 'var(--accent)' : 'var(--muted)';
    $('depthBadge').style.borderColor = depthActive
      ? 'rgba(61,220,132,.45)' : 'var(--line)';
    $('depthToggleText').textContent = depthActive ? '关闭深度模式' : '开启深度模式';
    $('btnDepthToggle').classList.toggle('running', depthActive);

    const info = $('depthInfo');
    if (s.ok === false) {
      info.innerHTML = `<div class="row" style="color:var(--danger)">${escapeHtml(s.error || '深度模块不可用')}</div>`;
      $('depthSummary').textContent = '深度模块不可用';
      return;
    }
    const cs = (s.color_size || []).join('×') || '—';
    const ds = (s.depth_size || []).join('×') || '—';
    const depthHasValues = s.depth_data_valid === true;
    info.innerHTML = `
      <div class="row">彩色: <span style="color:var(--ink);margin-left:6px">${cs}</span></div>
      <div class="row">深度: <span style="color:var(--ink);margin-left:6px">${ds}</span></div>
      <div class="row">帧率: <span style="color:var(--ink);margin-left:6px">${s.depth_fps || 0} fps</span></div>
      <div class="row">格式: <span style="color:var(--ink);margin-left:6px">${s.depth_format ?? '—'}</span></div>
      <div class="row">比例: <span style="color:var(--ink);margin-left:6px">${s.depth_scale ?? '—'}</span></div>
      <div class="row">帧数: <span style="color:var(--ink);margin-left:6px">${s.depth_frames ?? 0}</span></div>
      <div class="row" style="color:${depthHasValues ? 'var(--accent)' : (depthActive ? 'var(--warn)' : 'var(--muted)')}">${depthHasValues ? '深度值有效，可测距' : (depthActive ? 'SDK有帧，但没有有效深度值；当前不可测距' : '等待启动')}</div>`;
    $('depthSummary').textContent = depthActive
      ? (depthHasValues
        ? `深度运行中 · ${s.depth_fps || 0} fps · 点击画面测距`
        : '深度管线已启动，但暂无有效距离值')
      : 'Orbbec DaBai DCW · 结构光测距';
    if (s.depth_error) {
      info.innerHTML += `<div class="row" style="color:var(--danger)">${escapeHtml(s.depth_error)}</div>`;
    }

    // 实时深度图视图状态
    const wrap = $('depthFrameWrap');
    if (wrap) wrap.classList.toggle('live', depthActive);
    const ratio = $('depthRatio');
    if (ratio) {
      const vr = s.valid_ratio;
      ratio.textContent = (depthActive && vr != null)
        ? `有效像素 ${(vr * 100).toFixed(0)}%` : '有效像素 —';
      ratio.style.color = (vr != null && vr >= 0.35) ? 'var(--accent)' : 'var(--warn)';
    }
  }

  async function toggleDepth() {
    const btn = $('btnDepthToggle');
    btn.disabled = true;
    try {
      const url = depthActive ? '/depth/stop' : '/depth/start';
      const r = await fetch(url, { method: 'POST', cache: 'no-store' });
      const s = await r.json();
      if (s.ok === false) {
        renderDepthStatus(s);
        toast(s.error || '操作失败', true);
        return;
      }
      renderDepthStatus({ ...s, mode: !depthActive });
      const noDepthData = !depthActive && s.depth_running && !s.depth_data_valid;
      toast(!depthActive
        ? (noDepthData ? '深度管线已启动，但无有效测距值' : '深度模式已开启')
        : '深度模式已关闭，RGB 已恢复', noDepthData);
      // /stream 会自动切换深度/RGB 源, 保持实时流不断
      $('stream').src = '/stream?t=' + Date.now();
      refreshDepthImg();
    } catch (e) {
      toast('操作失败: ' + e.message, true);
    } finally {
      btn.disabled = false;
      fetchDepthStatus();
    }
  }

  async function fetchDepthStatus() {
    try {
      const r = await fetch('/depth/status', { cache: 'no-store' });
      renderDepthStatus(await r.json());
    } catch (e) {
      $('depthSummary').textContent = '深度模块未加载';
    }
  }

  // 实时深度图刷新 (深度激活且面板可见时才拉, 避免空转)
  let depthImgTimer = null;
  function refreshDepthImg() {
    if (!depthActive) return;
    const img = $('depthImg');
    const cimg = $('depthColorImg');
    if (img) img.src = '/depth/frame?t=' + Date.now();
    if (cimg) cimg.src = '/depth/color-frame?t=' + Date.now();
  }
  function startDepthImgLoop() {
    if (depthImgTimer) return;
    depthImgTimer = setInterval(() => {
      const panel = $('panelDepth');
      if (depthActive && panel && panel.classList.contains('open')) refreshDepthImg();
    }, 350);
  }

  // 深度图点击测距 (彩色/深度像素坐标一致, 都映射到 depth_size 空间)
  function depthImgMeasure(wrap, img, cross, clientX, clientY) {
    if (!depthActive || !wrap || !img) return;
    const rect = img.getBoundingClientRect();
    const iw = img.naturalWidth || 640, ih = img.naturalHeight || 360;
    const ix = Math.round((clientX - rect.left) * iw / rect.width);
    const iy = Math.round((clientY - rect.top) * ih / rect.height);
    if (ix < 0 || iy < 0 || ix >= iw || iy >= ih) return;
    if (cross) {
      cross.style.display = 'block';
      cross.style.left = (clientX - rect.left) + 'px';
      cross.style.top = (clientY - rect.top) + 'px';
    }
    fetch(`/depth/measure?x=${ix}&y=${iy}`, { cache: 'no-store' })
      .then((r) => r.json())
      .then((d) => {
        if (!d.ok) { toast(d.error || '测距失败', true); return; }
        showDepthReadout(d);
      })
      .catch((e) => toast('测距失败: ' + e.message, true));
  }

  function showDepthReadout(d) {
    const mm = d.distance_mm;
    $('depthReadout').innerHTML = `
      <div class="row"><span class="big" style="color:var(--accent)">${mm != null ? mm.toFixed(1) + ' mm' : '无效(空洞)'}</span>
        <span>点击位置 (${d.x}, ${d.y})</span></div>
      <div class="row">中心距离: <span style="color:var(--ink);margin-left:6px">${d.center_mm != null ? d.center_mm.toFixed(1) + ' mm' : '—'}</span></div>
      <div class="row">深度图尺寸: ${d.depth_size.join('×')}　比例: ${d.depth_scale}</div>
      <div class="row">邻域有效点: ${d.valid_pixels ?? '—'}/${d.sample_pixels ?? '—'} (${((d.local_valid_ratio || 0) * 100).toFixed(0)}%)</div>
      <div class="row">${mm != null ? (mm / 1000).toFixed(3) + ' 米' : '该点无深度数据'}</div>
      ${d.quality_warning ? `<div class="row depth-warning">${d.quality_warning}</div>` : ''}`;
  }

  /* ---------------- 三维点云 ---------------- */
  let pcPoints = [];
  let pcRotX = -0.5, pcRotY = 0.6, pcScale = 1.0;
  function loadPointcloud() {
    const step = +($('pcStep') ? $('pcStep').value : 8);
    fetch(`/depth/pointcloud?step=${step}`, { cache: 'no-store' })
      .then((r) => r.json())
      .then((d) => {
        if (!d.ok) { toast(d.error || '点云加载失败', true); return; }
        pcPoints = d.points || [];
        const badge = $('pcBadge');
        if (badge) badge.textContent = pcPoints.length ? `${pcPoints.length} 点` : '无数据';
        const sum = $('pcSummary');
        if (sum) sum.textContent = `深度 3D · ${pcPoints.length} 点 · 拖拽旋转`;
        drawPointCloud();
      })
      .catch((e) => toast('点云加载失败: ' + e.message, true));
  }
  let pcScreen = [];   // {sx, sy, i} 投影后屏幕坐标(拾取用)
  let pcPick = null;   // 选中物体: {idx[], bbox, center, dims, vol}
  function projPoint(p, W, H) {
    const cx = W / 2, cy = H / 2;
    const cosY = Math.cos(pcRotY), sinY = Math.sin(pcRotY);
    const cosX = Math.cos(pcRotX), sinX = Math.sin(pcRotX);
    const x = p[0], y = p[1], z = p[2];
    const x1 = x * cosY - z * sinY;
    const z1 = x * sinY + z * cosY;
    const y1 = y * cosX - z1 * sinX;
    const z2 = y * sinX + z1 * cosX;
    const persp = 600 / (600 + z2);
    return { sx: cx + x1 * persp * pcScale, sy: cy + y1 * persp * pcScale, z: z2, persp };
  }
  function drawPointCloud() {
    const cv = $('pcCanvas'); if (!cv) return;
    const ctx = cv.getContext('2d');
    const W = cv.width, H = cv.height;
    ctx.fillStyle = '#0a0d12'; ctx.fillRect(0, 0, W, H);
    if (!pcPoints.length) {
      ctx.fillStyle = '#888'; ctx.font = '14px sans-serif'; ctx.textAlign = 'center';
      ctx.fillText('暂无点云 · 请开启深度模式后点「刷新点云」', W / 2, H / 2);
      pcScreen = [];
      return;
    }
    const proj = pcPoints.map((p, i) => ({ ...projPoint(p, W, H), i }));
    proj.sort((a, b) => a.z - b.z);   // 远处先画
    pcScreen = [];
    const picked = new Set(pcPick ? pcPick.idx : []);
    for (const p of proj) {
      const t = Math.max(0, Math.min(1, p.z / 2500));
      const r = Math.round(255 * (1 - t) + 60 * t);
      const g = Math.round(120 * (1 - t) + 160 * t);
      const b = Math.round(60 * (1 - t) + 255 * t);
      const isPick = picked.has(p.i);
      ctx.fillStyle = isPick ? '#ffffff' : `rgb(${r},${g},${b})`;
      const s = Math.max(1, p.persp * 2.2) * (isPick ? 1.6 : 1);
      ctx.fillRect(p.sx, p.sy, s, s);
      pcScreen.push({ sx: p.sx, sy: p.sy, i: p.i });
    }
    // 选中物体的三维包围盒(12 条边)
    if (pcPick) {
      const mn = pcPick.bbox.min, mx = pcPick.bbox.max;
      const corners = [
        [mn[0], mn[1], mn[2]], [mx[0], mn[1], mn[2]],
        [mx[0], mx[1], mn[2]], [mn[0], mx[1], mn[2]],
        [mn[0], mn[1], mx[2]], [mx[0], mn[1], mx[2]],
        [mx[0], mx[1], mx[2]], [mn[0], mx[1], mx[2]],
      ].map((c) => projPoint(c, W, H));
      const E = [[0,1],[1,2],[2,3],[3,0],[4,5],[5,6],[6,7],[7,4],[0,4],[1,5],[2,6],[3,7]];
      ctx.strokeStyle = 'rgba(61,220,132,0.9)';
      ctx.lineWidth = 1.5;
      for (const [a, b] of E) {
        ctx.beginPath();
        ctx.moveTo(corners[a].sx, corners[a].sy);
        ctx.lineTo(corners[b].sx, corners[b].sy);
        ctx.stroke();
      }
    }
  }
  function pcPickAt(clientX, clientY) {
    const cv = $('pcCanvas'); if (!cv || !pcScreen.length) return;
    const rect = cv.getBoundingClientRect();
    const sx = (clientX - rect.left) * (cv.width / rect.width);
    const sy = (clientY - rect.top) * (cv.height / rect.height);
    let best = -1, bestD = 40 * 40;
    for (const s of pcScreen) {
      const d2 = (s.sx - sx) ** 2 + (s.sy - sy) ** 2;
      if (d2 < bestD) { bestD = d2; best = s.i; }
    }
    if (best < 0) return;
    // 3D 半径聚类(R=130mm) → 包围盒/尺寸/体积
    const p0 = pcPoints[best];
    const R = 130;
    const idx = [];
    const mn = [1e9, 1e9, 1e9], mx = [-1e9, -1e9, -1e9];
    let cx = 0, cy = 0, cz = 0;
    for (let i = 0; i < pcPoints.length; i++) {
      const p = pcPoints[i];
      const dx = p[0] - p0[0], dy = p[1] - p0[1], dz = p[2] - p0[2];
      if (dx * dx + dy * dy + dz * dz <= R * R) {
        idx.push(i);
        cx += p[0]; cy += p[1]; cz += p[2];
        for (let k = 0; k < 3; k++) {
          if (p[k] < mn[k]) mn[k] = p[k];
          if (p[k] > mx[k]) mx[k] = p[k];
        }
      }
    }
    const dims = [mx[0] - mn[0], mx[1] - mn[1], mx[2] - mn[2]].sort((a, b) => b - a);
    const vol = dims[0] * dims[1] * dims[2];
    pcPick = {
      idx, bbox: { min: mn, max: mx },
      center: [cx / idx.length, cy / idx.length, cz / idx.length],
      dims, vol,
    };
    drawPointCloud();
    const el = $('pcReadout');
    if (el) {
      const c = pcPick.center;
      el.innerHTML = `
        <div class="row"><span class="big" style="color:var(--accent)">X ${c[0].toFixed(0)}　Y ${c[1].toFixed(0)}　Z ${c[2].toFixed(0)} mm</span>
          <span>空间坐标(中心)</span></div>
        <div class="row">长×宽×高: <span style="color:var(--ink);margin-left:6px">${dims[0].toFixed(0)} × ${dims[1].toFixed(0)} × ${dims[2].toFixed(0)} mm</span></div>
        <div class="row">体积: <span style="color:var(--ink);margin-left:6px">${(vol / 1000).toFixed(1)} cm³</span>　点数: ${idx.length}</div>
        <div class="row" style="color:var(--muted)">聚类半径 130mm · 近似内参估算</div>`;
    }
  }
  function initPointCloudInteract() {
    const wrap = document.querySelector('.pc-wrap');
    if (!wrap) return;
    let dragging = false, lastX = 0, lastY = 0, moved = 0;
    wrap.addEventListener('mousedown', (e) => {
      dragging = true; lastX = e.clientX; lastY = e.clientY; moved = 0;
    });
    window.addEventListener('mouseup', (e) => {
      if (dragging && moved < 6) pcPickAt(e.clientX, e.clientY);  // 未拖拽=点击拾取
      dragging = false;
    });
    window.addEventListener('mousemove', (e) => {
      if (!dragging) return;
      moved += Math.abs(e.clientX - lastX) + Math.abs(e.clientY - lastY);
      pcRotY += (e.clientX - lastX) * 0.01;
      pcRotX += (e.clientY - lastY) * 0.01;
      lastX = e.clientX; lastY = e.clientY;
      drawPointCloud();
    });
    wrap.addEventListener('wheel', (e) => {
      e.preventDefault();
      pcScale *= (e.deltaY < 0 ? 1.1 : 0.9);
      pcScale = Math.max(0.2, Math.min(5, pcScale));
      drawPointCloud();
    }, { passive: false });
    drawPointCloud();
  }

  async function measureAt(clientX, clientY) {
    if (!depthActive) return;
    const img = $('stream');
    const rect = img.getBoundingClientRect();
    const iw = img.naturalWidth || 640, ih = img.naturalHeight || 360;
    const scale = Math.min(rect.width / iw, rect.height / ih);
    const offX = (rect.width - iw * scale) / 2;
    const offY = (rect.height - ih * scale) / 2;
    const ix = Math.round((clientX - rect.left - offX) / scale);
    const iy = Math.round((clientY - rect.top - offY) / scale);
    if (ix < 0 || iy < 0 || ix >= iw || iy >= ih) return;
    try {
      const r = await fetch(`/depth/measure?x=${ix}&y=${iy}`, { cache: 'no-store' });
      const d = await r.json();
      if (!d.ok) { toast(d.error || '测距失败', true); return; }
      const mm = d.distance_mm;
      $('depthReadout').innerHTML = `
        <div class="row"><span class="big" style="color:var(--accent)">${mm != null ? mm.toFixed(1) + ' mm' : '无效'}</span>
          <span>点击位置 (${d.x}, ${d.y})</span></div>
        <div class="row">中心距离: <span style="color:var(--ink);margin-left:6px">${d.center_mm != null ? d.center_mm.toFixed(1) + ' mm' : '—'}</span></div>
        <div class="row">深度图尺寸: ${d.depth_size.join('×')}　比例: ${d.depth_scale}</div>
        <div class="row">邻域有效点: ${d.valid_pixels ?? '—'}/${d.sample_pixels ?? '—'} (${((d.local_valid_ratio || 0) * 100).toFixed(0)}%)</div>
        <div class="row">1 mm = 0.001 m</div>
        ${d.quality_warning ? `<div class="row depth-warning">${d.quality_warning}</div>` : ''}`;
      placeMarker(clientX, clientY, [0, 220, 120], mm != null ? mm.toFixed(0) + 'mm' : '?');
    } catch (e) {
      toast('测距失败: ' + e.message, true);
    }
  }

  function saveDepthFrame() {
    const a = document.createElement('a');
    a.href = '/depth/frame?t=' + Date.now();
    a.download = 'depth_' + Date.now() + '.jpg';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    toast('深度图已下载');
  }

  /* ---------------- 摄像头参数控制 ---------------- */

  function renderCamCaps(cap) {
    const grid = $('camGrid');
    const params = cap.params || [];
    const supported = params.filter((p) => p.supported);
    const changed = params.filter((p) => p.changed).length;
    $('camBadge').textContent = changed > 0 ? `${changed} 项已改` : '出厂默认';
    $('camBadge').style.color = changed > 0 ? 'var(--warn)' : 'var(--accent)';
    $('camBadge').style.borderColor = changed > 0
      ? 'rgba(240,180,41,.45)' : 'rgba(61,220,132,.45)';
    const dr = (cap.default_resolution || []).join('×');
    $('camSummary').textContent =
      `当前 ${(cap.current_resolution || []).join('×') || '—'} · ` +
      `默认 ${dr || '—'} · ` +
      `${supported.length} 可调 / ${params.length - supported.length} 不支持`;

    if (cap.current_resolution) {
      const sel = $('camRes');
      const want = cap.current_resolution.join('x');
      if (sel && [...sel.options].some((o) => o.value === want)) {
        sel.value = want;
      }
    }

    grid.innerHTML = params
      .filter((p) => p.ui !== 'resolution')
      .map((p) => {
        const rng = p.range || [0, 100];
        const tag = !p.supported ? '<span class="tag no">不支持</span>'
          : p.writable ? '<span class="tag ok">可调</span>'
          : '<span class="tag ro">只读</span>';
        const dis = (p.supported && p.writable) ? '' : 'disabled';
        const val = (p.current != null) ? p.current : '—';
        const def = (p.default != null) ? p.default : '—';
        const isChanged = !!p.changed;
        return `
          <div class="cam-item${p.supported ? '' : ' disabled'}${isChanged ? ' changed' : ''}">
            <div class="k">
              <span>${p.cn}${isChanged ? ' <span style="color:var(--warn)">●</span>' : ''}</span>
              ${tag}
            </div>
            <div class="row">
              <input type="range" data-prop="${p.name}"
                min="${rng[0]}" max="${rng[1]}" step="1"
                value="${p.current != null ? p.current : rng[0]}" ${dis}>
              <span class="val" id="camVal_${p.name}">${val}</span>
            </div>
            <div class="rng">
              默认 <b style="color:${isChanged ? 'var(--warn)' : 'var(--accent)'}">${def}</b>
              ${p.range ? `　范围 ${rng[0]}~${rng[1]}${p.unit ? ' ' + p.unit : ''}` : ''}
              ${isChanged ? '　<span style="color:var(--warn)">已修改</span>' : ''}
            </div>
            ${isChanged ? `<button class="mini-reset" data-reset="${p.name}">恢复默认</button>` : ''}
            ${p.note ? `<div class="note">${p.note}</div>` : ''}
          </div>`;
      }).join('');

    // 滑块实时回显
    grid.querySelectorAll('input[type=range]').forEach((el) => {
      el.addEventListener('input', () => {
        const v = $('camVal_' + el.dataset.prop);
        if (v) v.textContent = el.value;
      });
    });

    // 单参数恢复
    grid.querySelectorAll('[data-reset]').forEach((el) => {
      el.addEventListener('click', async () => {
        const name = el.dataset.reset;
        try {
          const r = await fetch('/camera/reset-param', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name }),
          });
          const d = await r.json();
          if (d.ok) toast(`${name} 已恢复默认`);
          else toast(d.error || '恢复失败', true);
        } catch (e) {
          toast('恢复失败: ' + e.message, true);
        }
        loadCamCaps();
      });
    });
  }

  async function loadCamCaps() {
    try {
      const r = await fetch('/camera/capabilities', { cache: 'no-store' });
      renderCamCaps(await r.json());
    } catch (e) {
      $('camGrid').innerHTML = `<div class="det-empty">加载失败: ${escapeHtml(e.message)}</div>`;
    }
  }

  async function applyCamSettings() {
    const body = {};
    document.querySelectorAll('#camGrid input[type=range]:not(:disabled)')
      .forEach((el) => { body[el.dataset.prop] = parseFloat(el.value); });
    const sel = $('camRes');
    if (sel && sel.value) {
      const [w, h] = sel.value.split('x').map(Number);
      body.FRAME_WIDTH = w;
      body.FRAME_HEIGHT = h;
    }
    const btn = $('btnCamApply');
    btn.disabled = true;
    try {
      const r = await fetch('/camera/settings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      const d = await r.json();
      const fails = Object.entries(d.results || {})
        .filter(([, v]) => !v.ok)
        .map(([k, v]) => `${k}: ${v.error}`);
      if (fails.length) {
        toast('部分参数被拒: ' + fails.join('；'), true);
      } else {
        toast('参数已应用');
      }
    } catch (e) {
      toast('应用失败: ' + e.message, true);
    } finally {
      btn.disabled = false;
      loadCamCaps();
    }
  }

  async function resetCamSettings() {
    try {
      await fetch('/camera/reset', { method: 'POST' });
      toast('已恢复默认参数');
      loadCamCaps();
    } catch (e) {
      toast('恢复失败: ' + e.message, true);
    }
  }

  async function probeCamRanges() {
    const btn = $('btnCamProbe');
    btn.disabled = true;
    $('camSummary').textContent = '标定中…';
    try {
      const r = await fetch('/camera/probe-ranges', { method: 'POST' });
      const d = await r.json();
      if (!d.ok) { toast(d.error || '标定失败', true); return; }
      const n = Object.keys(d.ranges || {}).length;
      toast(`标定完成，测得 ${n} 个参数范围`);
      loadCamCaps();
    } catch (e) {
      toast('标定失败: ' + e.message, true);
    } finally {
      btn.disabled = false;
    }
  }

  /* ---------------- 运动检测 / 入侵报警 ---------------- */

  function renderMotion(s) {
    const on = !!s.motion_enabled;
    $('motionBadge').textContent = on ? '监测中' : '关闭';
    $('motionBadge').style.color = on ? 'var(--accent)' : 'var(--muted)';
    $('motionBadge').style.borderColor = on ? 'rgba(61,220,132,.45)' : 'var(--line)';
    $('motionToggleText').textContent = on ? '停止监测' : '开始监测';
    $('btnMotionToggle').classList.toggle('running', on);

    $('motionSummary').textContent = on
      ? `已运行 ${s.uptime || 0}s · 报警 ${s.alarm_count || 0} 次`
      : '背景差分 · 纯算法';

    // 实时状态
    const live = $('motionLive');
    const lvl = s.motion_level || 0;
    live.innerHTML = `
      <div class="row"><span class="big" style="color:${lvl > 15 ? 'var(--danger)' : 'var(--accent)'}">${lvl.toFixed(1)}%</span>
        <span>当前运动量</span></div>
      <div class="row">运动面积: <span style="color:var(--ink);margin-left:6px">${s.motion_area || 0} px</span></div>
      <div class="row">已分析帧: <span style="color:var(--ink);margin-left:6px">${s.motion_frames || 0}</span></div>
      <div class="row">报警次数: <span style="color:var(--ink);margin-left:6px">${s.alarm_count || 0}</span></div>`;
    $('motionLevel').textContent = lvl.toFixed(1) + '%';
    const bar = $('motionBar');
    bar.style.width = Math.min(100, lvl) + '%';
    bar.style.background = lvl > 15 ? 'var(--danger)' : 'var(--accent)';

    // 报警日志
    const alarms = s.recent_alarms || [];
    $('alarmCount').textContent = `(${alarms.length})`;
    const list = $('alarmList');
    if (!alarms.length) {
      list.innerHTML = '<div class="det-empty">暂无报警</div>';
    } else {
      list.innerHTML = alarms.map((a) => `
        <div class="det-row">
          <span class="ar" style="color:var(--danger);min-width:22px">⚠</span>
          <span class="nm" style="font-family:var(--mono);font-size:11px">${a.time}</span>
          <span class="ar" style="min-width:60px">${a.area} px</span>
          <span class="ps">${a.level}%</span>
        </div>
        ${(a.colors || a.texts) ? `<div class="det-row" style="padding-left:31px;border-bottom:none">
          <span class="nm" style="color:var(--accent);font-size:10px">现场: ${(a.colors || []).map((c) => c.color).join('/')}${a.texts ? ' · ' + a.texts.map((t) => t.text).join(' ') : ''}</span>
        </div>` : ''}`).join('');
    }

    // 同步滑块显示
    if (document.activeElement !== $('motionSens')) $('motionSens').value = s.motion_sensitivity ?? 50;
    $('motionSensVal').textContent = s.motion_sensitivity ?? 50;
    if (document.activeElement !== $('motionCooldown')) $('motionCooldown').value = s.motion_cooldown ?? 3;
    $('motionCooldownVal').textContent = (s.motion_cooldown ?? 3) + 's';
    $('motionSave').checked = s.motion_save !== false;
    if ($('motionSmart')) $('motionSmart').checked = s.smart_recognize !== false;
  }

  async function fetchMotion() {
    try {
      const r = await fetch('/motion/status', { cache: 'no-store' });
      renderMotion(await r.json());
    } catch (e) { /* 静默 */ }
  }

  async function toggleMotion() {
    const btn = $('btnMotionToggle');
    btn.disabled = true;
    try {
      const on = $('motionToggleText').textContent.indexOf('停止') >= 0;
      const url = on ? '/motion/stop' : '/motion/start';
      const r = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ sensitivity: parseInt($('motionSens').value, 10) }),
      });
      const d = await r.json();
      renderMotion(d);
      toast(!on ? '运动检测已开启' : '运动检测已关闭');
    } catch (e) {
      toast('操作失败: ' + e.message, true);
    } finally {
      btn.disabled = false;
      fetchMotion();
    }
  }

  async function pushMotionSettings() {
    try {
      await fetch('/motion/settings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          sensitivity: parseInt($('motionSens').value, 10),
          cooldown: parseInt($('motionCooldown').value, 10),
          save_snapshot: $('motionSave').checked,
          smart_recognize: $('motionSmart') ? $('motionSmart').checked : true,
        }),
      });
    } catch (e) { /* 静默 */ }
  }

  async function clearMotionLog() {
    try {
      await fetch('/motion/clear', { method: 'POST' });
      toast('报警日志已清空');
      fetchMotion();
    } catch (e) {
      toast('清空失败: ' + e.message, true);
    }
  }

  /* ---------------- 测量工具 ---------------- */

  let measureMode = 'distance';
  let measurePoints = [];
  let measureHistory = [];
  const MEASURE_NEEDED = { distance: 2, angle: 3, area: 4 };
  const MEASURE_CN = { distance: '距离', angle: '角度', area: '面积' };

  function renderMeasure() {
    $('measureModeLabel').textContent = MEASURE_CN[measureMode] || measureMode;
    $('measureBadge').textContent = measurePoints.length
      ? `${measurePoints.length} 点` : '待机';
    $('measureBadge').style.color = measurePoints.length ? 'var(--accent)' : 'var(--muted)';
    $('measureBadge').style.borderColor = measurePoints.length
      ? 'rgba(61,220,132,.45)' : 'var(--line)';

    // 点位列表
    const pl = $('measurePoints');
    if (!measurePoints.length) {
      pl.innerHTML = '<div class="det-empty">暂无点位</div>';
    } else {
      pl.innerHTML = measurePoints.map((p, i) => `
        <div class="det-row">
          <span class="ar" style="color:var(--accent);min-width:24px">P${i + 1}</span>
          <span class="nm" style="font-family:var(--mono);font-size:11px">(${p[0].toFixed(0)}, ${p[1].toFixed(0)})</span>
          ${i === 1 && measureMode === 'angle' ? '<span class="ps" style="color:var(--warn)">顶点</span>' : ''}
        </div>`).join('');
    }

    // 历史
    const hl = $('measureHistory');
    if (!measureHistory.length) {
      hl.innerHTML = '<div class="det-empty">暂无记录</div>';
    } else {
      hl.innerHTML = measureHistory.slice(0, 8).map((h) => `
        <div class="det-row">
          <span class="ar" style="color:var(--accent);min-width:38px">${h.kind}</span>
          <span class="nm" style="font-family:var(--mono);font-size:11px">${h.summary}</span>
        </div>`).join('');
    }

    drawMeasureOverlay();
  }

  function drawMeasureOverlay() {
    const stage = $('stage');
    stage.querySelectorAll('.m-point,.m-line,.m-label').forEach((el) => el.remove());
    const rect = stage.getBoundingClientRect();

    // 画点
    measurePoints.forEach((p, i) => {
      const el = document.createElement('div');
      el.className = 'm-point';
      el.style.left = p[2] + 'px';
      el.style.top = p[3] + 'px';
      el.innerHTML = `<span>P${i + 1}</span>`;
      stage.appendChild(el);
    });

    // 画线段
    for (let i = 0; i < measurePoints.length - 1; i++) {
      const a = measurePoints[i], b = measurePoints[i + 1];
      const dx = b[2] - a[2], dy = b[3] - a[3];
      const len = Math.hypot(dx, dy);
      const ang = Math.atan2(dy, dx) * 180 / Math.PI;
      const el = document.createElement('div');
      el.className = 'm-line';
      el.style.left = a[2] + 'px';
      el.style.top = a[3] + 'px';
      el.style.width = len + 'px';
      el.style.transform = `rotate(${ang}deg)`;
      stage.appendChild(el);
    }
    // 面积模式闭合
    if (measureMode === 'area' && measurePoints.length >= 3) {
      const a = measurePoints[measurePoints.length - 1], b = measurePoints[0];
      const dx = b[2] - a[2], dy = b[3] - a[3];
      const len = Math.hypot(dx, dy);
      const ang = Math.atan2(dy, dx) * 180 / Math.PI;
      const el = document.createElement('div');
      el.className = 'm-line dashed';
      el.style.left = a[2] + 'px';
      el.style.top = a[3] + 'px';
      el.style.width = len + 'px';
      el.style.transform = `rotate(${ang}deg)`;
      stage.appendChild(el);
    }

    // 标签: 距离/角度/面积
    if (measurePoints.length >= 2) {
      let text = '';
      if (measureMode === 'distance' && measurePoints.length === 2) {
        const a = measurePoints[0], b = measurePoints[1];
        const px = Math.hypot(b[0] - a[0], b[1] - a[1]);
        text = `${px.toFixed(1)} px`;
      } else if (measureMode === 'angle' && measurePoints.length === 3) {
        const p1 = measurePoints[0], p2 = measurePoints[1], p3 = measurePoints[2];
        const v1 = [p1[0] - p2[0], p1[1] - p2[1]];
        const v2 = [p3[0] - p2[0], p3[1] - p2[1]];
        const n1 = Math.hypot(...v1), n2 = Math.hypot(...v2);
        if (n1 > 0 && n2 > 0) {
          const c = Math.max(-1, Math.min(1, (v1[0]*v2[0] + v1[1]*v2[1]) / (n1 * n2)));
          text = (Math.acos(c) * 180 / Math.PI).toFixed(1) + '°';
        }
      } else if (measureMode === 'area' && measurePoints.length >= 3) {
        const pts = measurePoints;
        let a2 = 0;
        for (let i = 0; i < pts.length; i++) {
          const j = (i + 1) % pts.length;
          a2 += pts[i][0] * pts[j][1] - pts[j][0] * pts[i][1];
        }
        text = Math.abs(a2 / 2).toFixed(0) + ' px²';
      }
      if (text) {
        const mid = measurePoints[Math.floor(measurePoints.length / 2)];
        const el = document.createElement('div');
        el.className = 'm-label';
        el.style.left = mid[2] + 'px';
        el.style.top = (mid[3] - 32) + 'px';
        el.textContent = text;
        stage.appendChild(el);
      }
    }
  }

  async function measureClick(clientX, clientY) {
    const img = $('stream');
    const rect = img.getBoundingClientRect();
    const iw = img.naturalWidth || 640, ih = img.naturalHeight || 360;
    const scale = Math.min(rect.width / iw, rect.height / ih);
    const offX = (rect.width - iw * scale) / 2;
    const offY = (rect.height - ih * scale) / 2;
    const ix = (clientX - rect.left - offX) / scale;
    const iy = (clientY - rect.top - offY) / scale;
    if (ix < 0 || iy < 0 || ix > iw || iy > ih) return;

    // stage 内的 DOM 坐标
    const stageRect = $('stage').getBoundingClientRect();
    const dx = clientX - stageRect.left;
    const dy = clientY - stageRect.top;
    measurePoints.push([ix, iy, dx, dy]);
    renderMeasure();

    const needed = MEASURE_NEEDED[measureMode];
    if (measurePoints.length >= needed) {
      await computeMeasure();
    }
  }

  async function computeMeasure() {
    const pts = measurePoints;
    try {
      if (measureMode === 'distance' && pts.length >= 2) {
        const a = pts[0], b = pts[1];
        const r = await fetch(`/measure/distance?x1=${a[0].toFixed(1)}&y1=${a[1].toFixed(1)}&x2=${b[0].toFixed(1)}&y2=${b[1].toFixed(1)}`, { cache: 'no-store' });
        const d = await r.json();
        if (d.ok) {
          const real = d.real_mm ? `　真实 <b style="color:var(--accent)">${d.real_mm} mm</b>` : '';
          $('measureLive').innerHTML = `
            <div class="row"><span class="big" style="color:var(--accent)">${d.pixel_distance} px</span></div>
            <div class="row">P1 (${d.p1.join(', ')}) → P2 (${d.p2.join(', ')})</div>
            ${real || '<div class="row" style="color:var(--warn)">深度模式未开启, 无法算真实尺寸</div>'}`;
          measureHistory.unshift({ kind: '距离', summary: `${d.pixel_distance} px` + (d.real_mm ? ` / ${d.real_mm} mm` : '') });
        }
      } else if (measureMode === 'angle' && pts.length >= 3) {
        const p1 = pts[0], p2 = pts[1], p3 = pts[2];
        const r = await fetch(`/measure/angle?x1=${p1[0].toFixed(1)}&y1=${p1[1].toFixed(1)}&x2=${p2[0].toFixed(1)}&y2=${p2[1].toFixed(1)}&x3=${p3[0].toFixed(1)}&y3=${p3[1].toFixed(1)}`, { cache: 'no-store' });
        const d = await r.json();
        if (d.ok) {
          $('measureLive').innerHTML = `
            <div class="row"><span class="big" style="color:var(--accent)">${d.angle_deg}°</span></div>
            <div class="row">顶点 (${d.vertex.join(', ')})</div>
            <div class="row">两边 ${d.legs_pixel[0]} / ${d.legs_pixel[1]} px</div>`;
          measureHistory.unshift({ kind: '角度', summary: `${d.angle_deg}°` });
        }
      } else if (measureMode === 'area' && pts.length >= 3) {
        const seg = pts.map((p) => `${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(';');
        const r = await fetch(`/measure/area?points=${encodeURIComponent(seg)}`, { cache: 'no-store' });
        const d = await r.json();
        if (d.ok) {
          $('measureLive').innerHTML = `
            <div class="row"><span class="big" style="color:var(--accent)">${d.area_px2} px²</span></div>
            <div class="row">顶点数 ${d.vertices}　周长 ${d.perimeter_px} px</div>`;
          measureHistory.unshift({ kind: '面积', summary: `${d.area_px2} px²` });
        }
      }
      renderMeasure();
    } catch (e) {
      toast('测量失败: ' + e.message, true);
    }
  }

  function clearMeasure() {
    measurePoints = [];
    renderMeasure();
    $('measureLive').innerHTML = '<div class="det-empty">点击视频画面放置点位</div>';
    toast('点位已清空');
  }

  function saveMeasureShot() {
    const a = document.createElement('a');
    a.href = '/snapshot?t=' + Date.now();
    a.download = 'measure_' + Date.now() + '.jpg';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    toast('测量截图已下载');
  }

  /* ---------------- Haar 目标检测 ---------------- */

  let haarTarget = 'face_front';
  const HAAR_CN = {
    face_front: '人脸(正面)', face_alt: '人脸(高精度)', face_profile: '人脸(侧面)',
    cat_face: '猫脸', smile: '微笑', eye: '眼睛', plate: '汽车牌照',
    fullbody: '全身人体', upperbody: '上半身', lowerbody: '下半身',
  };

  function renderHaar(d) {
    const n = d.count || 0;
    $('haarCount').textContent = `(${n} 个 · ${d.elapsed_ms || 0} ms)`;
    $('haarBadge').textContent = n > 0 ? `${n} 个` : '未检出';
    $('haarBadge').style.color = n > 0 ? 'var(--accent)' : 'var(--muted)';
    $('haarBadge').style.borderColor = n > 0 ? 'rgba(61,220,132,.45)' : 'var(--line)';
    $('haarSummary').textContent = n > 0
      ? `${d.target_cn} 检出 ${n} 个 · 耗时 ${d.elapsed_ms} ms`
      : 'Haar 级联 · 10 种目标 · 点击才检测';

    const list = $('haarList');
    const items = d.results || [];
    if (!items.length) {
      list.innerHTML = `<div class="det-empty">当前画面未检出${d.target_cn || '目标'}。换个角度/光线再试。</div>`;
    } else {
      list.innerHTML = items.map((it, i) => {
        const b = it.bbox;
        return `
          <div class="det-row">
            <span class="ar" style="color:var(--accent);min-width:24px">#${i + 1}</span>
            <span class="nm" style="font-family:var(--mono);font-size:11px">
              位置 (${b[0]}, ${b[1]})　大小 ${b[2]}×${b[3]}
            </span>
          </div>`;
      }).join('');
    }
    $('haarInfo').innerHTML = `
      <div class="row">目标: <b style="color:var(--accent);margin-left:6px">${d.target_cn || '—'}</b></div>
      <div class="row">检出: <span style="color:var(--ink);margin-left:6px">${n} 个</span></div>
      <div class="row">耗时: <span style="color:var(--ink);margin-left:6px">${d.elapsed_ms || 0} ms</span></div>
      <div class="row">算法: Haar 级联 (零数据集)</div>`;
  }

  function showHaarAnnotated(d) {
    $('previewTitle').textContent = `${d.target_cn} 检测结果 · ${d.count} 个`;
    $('previewBody').innerHTML = `<img src="/haar/annotated?t=${Date.now()}" alt="检测结果">`;
    $('previewMask').classList.add('show');
  }

  async function runHaar() {
    if (!running) { toast('请先点击 RUN 运行相机', true); return; }
    const btn = $('btnHaar');
    btn.disabled = true;
    $('haarBtnText').textContent = '检测中…';
    try {
      const r = await fetch('/haar/detect', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ target: haarTarget }),
      });
      const d = await r.json();
      if (!d.ok) { toast(d.error || '检测失败', true); return; }
      renderHaar(d);
      showHaarAnnotated(d);
      toast(d.count > 0 ? `检出 ${d.count} 个${d.target_cn}` : `未检出${d.target_cn}`);
    } catch (e) {
      toast('检测失败: ' + e.message, true);
    } finally {
      btn.disabled = false;
      $('haarBtnText').textContent = '检测';
    }
  }

  function saveHaarAnnotated() {
    const a = document.createElement('a');
    a.href = '/haar/annotated?t=' + Date.now();
    a.download = 'haar_' + haarTarget + '_' + Date.now() + '.jpg';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    toast('标注图已下载');
  }

  /* ---------------- 物体追踪 ---------------- */

  function renderTrack(s) {
    const on = !!s.track_enabled;
    $('trackBadge').textContent = s.track_lost ? '已丢失' : (on ? '追踪中' : '待机');
    $('trackBadge').style.color = s.track_lost ? 'var(--danger)' : (on ? 'var(--accent)' : 'var(--muted)');
    $('trackBadge').style.borderColor = s.track_lost
      ? 'rgba(255,107,107,.45)' : (on ? 'rgba(61,220,132,.45)' : 'var(--line)');
    $('trackBtnText').textContent = on ? '追踪中…' : '点击画面选目标';
    $('trackSummary').textContent = on
      ? `已追踪 ${s.uptime || 0}s · ${s.track_frames || 0} 帧`
      : 'CSRT 算法 · 点击画面选目标';

    const live = $('trackLive');
    if (s.track_bbox) {
      const [x, y, w, h] = s.track_bbox;
      live.innerHTML = `
        <div class="row"><span class="big" style="color:${s.track_lost ? 'var(--danger)' : 'var(--accent)'}">
          ${s.track_lost ? '已丢失' : '追踪中'}</span></div>
        <div class="row">当前位置: <span style="color:var(--ink);margin-left:6px">(${x}, ${y})</span></div>
        <div class="row">框大小: <span style="color:var(--ink);margin-left:6px">${w}×${h}</span></div>
        <div class="row">追踪帧数: <span style="color:var(--ink);margin-left:6px">${s.track_frames || 0}</span></div>
        ${s.track_lost ? '<div class="row" style="color:var(--danger)">目标丢失, 请重新选择</div>' : ''}`;
    } else {
      live.innerHTML = '<div class="det-empty">待机 · 点击画面选目标</div>';
    }
  }

  async function fetchTrack() {
    try {
      const r = await fetch('/track/status', { cache: 'no-store' });
      renderTrack(await r.json());
    } catch (e) { /* 静默 */ }
  }

  async function startTrackAt(clientX, clientY) {
    const img = $('stream');
    const rect = img.getBoundingClientRect();
    const iw = img.naturalWidth || 640, ih = img.naturalHeight || 360;
    const scale = Math.min(rect.width / iw, rect.height / ih);
    const offX = (rect.width - iw * scale) / 2;
    const offY = (rect.height - ih * scale) / 2;
    const ix = Math.round((clientX - rect.left - offX) / scale);
    const iy = Math.round((clientY - rect.top - offY) / scale);
    if (ix < 0 || iy < 0 || ix >= iw || iy >= ih) return;
    try {
      const r = await fetch('/track/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ x: ix, y: iy, size: parseInt($('trackSize').value, 10) }),
      });
      const d = await r.json();
      if (!d.ok) { toast(d.error || '追踪启动失败', true); return; }
      toast('开始追踪');
      fetchTrack();
    } catch (e) {
      toast('追踪启动失败: ' + e.message, true);
    }
  }

  async function stopTrack() {
    try {
      await fetch('/track/stop', { method: 'POST' });
      toast('已停止追踪');
      fetchTrack();
    } catch (e) {
      toast('停止失败: ' + e.message, true);
    }
  }

  /* ---------------- 折叠面板 ---------------- */

  let latestDiagnostics = null;

  async function runDiagnostics() {
    const button = $('btnDiagnosticsRun');
    button.disabled = true;
    button.textContent = '检查中…';
    try {
      const response = await fetch('/diagnostics', { cache: 'no-store' });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const result = await response.json();
      latestDiagnostics = result;
      const grid = $('diagGrid');
      grid.replaceChildren();
      const stateNames = { ok: '正常', warn: '注意', error: '异常', idle: '未启动' };
      result.checks.forEach((check) => {
        const card = document.createElement('article');
        card.className = 'diag-card';
        card.dataset.state = check.state;
        const head = document.createElement('div');
        head.className = 'diag-card-head';
        const title = document.createElement('h3');
        title.textContent = check.label;
        const state = document.createElement('span');
        state.className = 'diag-state';
        state.textContent = stateNames[check.state] || '未知';
        const detail = document.createElement('p');
        detail.textContent = check.detail;
        head.append(title, state);
        card.append(head, detail);
        grid.appendChild(card);
      });
      const overallNames = { ok: '检查完成', warn: '有可选项需注意', error: '发现异常' };
      $('diagBadge').textContent = overallNames[result.overall] || '检查完成';
      $('diagBadge').dataset.state = result.overall;
      $('diagSummary').textContent = `${result.checks.length} 项检查 · 只读`;
      $('diagUpdated').textContent = `检查时间：${result.generated_at} · ${result.privacy_note}`;
      $('btnDiagnosticsExport').disabled = false;
    } catch (error) {
      $('diagBadge').textContent = '检查失败';
      $('diagBadge').dataset.state = 'error';
      $('diagGrid').textContent = `无法读取诊断结果：${error.message}`;
      toast('诊断失败: ' + error.message, true);
    } finally {
      button.disabled = false;
      button.textContent = '重新检查';
    }
  }

  function exportDiagnostics() {
    if (!latestDiagnostics) return;
    const blob = new Blob([JSON.stringify(latestDiagnostics, null, 2)],
                          { type: 'application/json;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = `orbbec-diagnostics-${new Date().toISOString().replace(/[:.]/g, '-')}.json`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
    toast('诊断 JSON 已导出');
  }

  async function runConsoleCommand(command) {
    const output = $('commandOutput');
    const button = $('commandRun');
    const text = String(command || '').trim();
    if (!text) { output.textContent = '请输入命令。'; return; }
    output.dataset.state = 'running';
    output.textContent = `> ${text}\n执行中…`;
    button.disabled = true;
    try {
      const response = await fetch('/console', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ command: text }),
      });
      const result = await response.json();
      output.dataset.state = result.ok ? 'ok' : 'error';
      output.textContent = `> ${text}\n${(result.output || [result.error || '命令执行失败']).join('\n')}`;
      if (result.data && result.mode === 'yolo' && result.ok) {
        const detections = result.data.results || [];
        output.textContent += detections.length
          ? '\n' + detections.slice(0, 12).map((item) =>
              `  ${item.label_cn} ${(item.confidence * 100).toFixed(1)}%`).join('\n')
          : '\n当前帧没有检出目标。';
      }
      if (!result.ok) toast(result.error || '命令执行失败', true);
    } catch (error) {
      output.dataset.state = 'error';
      output.textContent = `> ${text}\n请求失败：${error.message}`;
      toast('指令窗口请求失败: ' + error.message, true);
    } finally {
      button.disabled = false;
    }
  }

  const robotProjectOutputIds = {
    'yolo-sort': 'robotProjectResultYolo',
    'color-sort': 'robotProjectResultColor',
    'qr-route': 'robotProjectResultQr',
    'depth-pick': 'robotProjectResultDepth',
  };

  async function runRobotProject(projectId, button) {
    const output = $(robotProjectOutputIds[projectId]);
    if (!output || !button) return;
    const oldLabel = button.textContent;
    button.disabled = true;
    button.textContent = '执行中…';
    output.dataset.state = 'running';
    output.textContent = '读取当前传感器并运行仿真流程…';
    try {
      const response = await fetch('/robot/projects/run', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ project: projectId }),
        cache: 'no-store',
      });
      const result = await response.json();
      if (!result.ok) {
        output.dataset.state = 'error';
        output.textContent = [
          `未执行：${result.error || '项目运行失败'}`,
          result.next_step || '',
          result.robot_state ? `机械臂状态：${result.robot_state.state}（仿真）` : '',
        ].filter(Boolean).join('\n');
        return;
      }
      const point = result.pick_point_mm_demo || [];
      const place = result.place_point_mm_demo || [];
      const details = [
        `仿真完成：${result.target} → ${result.destination}`,
        `抓取演示点 ${point.join(', ')} mm → 放置点 ${place.join(', ')} mm`,
        result.depth ? `中心深度 ${result.depth.distance_mm} mm · 有效像素 ${result.depth.valid_pixels}` : '',
        `流程：${(result.steps || []).join(' → ')}`,
        result.note || '未标定，仅仿真；没有驱动真实机械臂。',
      ].filter(Boolean);
      output.dataset.state = 'ok';
      output.textContent = details.join('\n');
      toast(`${result.project_name}：仿真完成`);
    } catch (error) {
      output.dataset.state = 'error';
      output.textContent = `请求失败：${error.message}`;
      toast('机械臂项目请求失败: ' + error.message, true);
    } finally {
      button.disabled = false;
      button.textContent = oldLabel;
    }
  }

  function togglePanel() {
    $('panelFiles').classList.toggle('open');
    if ($('panelFiles').classList.contains('open')) loadFiles(true);
  }

  /* ---------------- 事件绑定 ---------------- */

  btnRun.addEventListener('click', toggleRun);
  btnRec.addEventListener('click', toggleRec);
  $('btnSnap').addEventListener('click', snapshot);
  $('btnFull').addEventListener('click', toggleFull);
  $('btnFiles').addEventListener('click', togglePanel);
  $('panelFilesHead').addEventListener('click', togglePanel);
  $('panelDiagnosticsHead').addEventListener('click', () => {
    $('panelDiagnostics').classList.toggle('open');
  });
  $('btnDiagnosticsRun').addEventListener('click', runDiagnostics);
  $('btnDiagnosticsExport').addEventListener('click', exportDiagnostics);
  $('panelConsoleHead').addEventListener('click', () => {
    $('panelConsole').classList.toggle('open');
    if ($('panelConsole').classList.contains('open')) $('commandInput').focus();
  });
  $('commandForm').addEventListener('submit', (event) => {
    event.preventDefault();
    const input = $('commandInput');
    runConsoleCommand(input.value);
  });
  document.querySelectorAll('.command-preset').forEach((preset) => {
    preset.addEventListener('click', () => {
      const command = preset.dataset.command || '';
      $('commandInput').value = command;
      runConsoleCommand(command);
    });
  });
  document.querySelectorAll('[data-robot-project]').forEach((button) => {
    button.addEventListener('click', () =>
      runRobotProject(button.dataset.robotProject || '', button));
  });
  $('btnRefresh').addEventListener('click', fetchStatus);
  $('btnReloadFiles').addEventListener('click', () => loadFiles(false));

  // ---- 颜色识别 ----
  $('panelColorHead').addEventListener('click', () => {
    $('panelColor').classList.toggle('open');
  });
  $('btnColorDetect').addEventListener('click', (e) => {
    e.stopPropagation();
    runDetect();
  });
  $('btnColorSnap').addEventListener('click', (e) => {
    e.stopPropagation();
    saveAnnotated();
  });
  $('annotatedClose').addEventListener('click', () =>
    $('annotatedMask').classList.remove('show'));
  $('annotatedMask').addEventListener('click', (e) => {
    if (e.target === $('annotatedMask')) $('annotatedMask').classList.remove('show');
  });
  $('annotatedDownload').addEventListener('click', saveAnnotated);
  $('sensSlider').addEventListener('input', (e) => {
    $('sensVal').textContent = e.target.value;
    pushColorSettings();
  });
  $('areaSlider').addEventListener('input', (e) => {
    $('areaVal').textContent = e.target.value;
    pushColorSettings();
  });
  // 点击视频: 测量 / 追踪 / 深度测距 / 取色 (按展开的面板分流)
  $('stage').addEventListener('click', (e) => {
    if (e.target.closest('.btn')) return;
    if ($('panelTrack').classList.contains('open')) {
      startTrackAt(e.clientX, e.clientY);
      return;
    }
    if ($('panelMeasure').classList.contains('open')) {
      measureClick(e.clientX, e.clientY);
      return;
    }
    if (depthActive) {
      if ($('depthAutoMeasure').checked) measureAt(e.clientX, e.clientY);
    } else {
      pickColorAt(e.clientX, e.clientY);
    }
  });

  // ---- 物体追踪 ----
  $('panelTrackHead').addEventListener('click', () => {
    $('panelTrack').classList.toggle('open');
    fetchTrack();
  });
  $('btnTrackStart').addEventListener('click', (e) => {
    e.stopPropagation();
    toast('请直接点击视频画面选择追踪目标');
  });
  $('btnTrackStop').addEventListener('click', (e) => {
    e.stopPropagation();
    stopTrack();
  });
  $('trackSize').addEventListener('input', (e) => {
    $('trackSizeVal').textContent = e.target.value;
  });

  // ---- 测量工具 ----
  $('panelMeasureHead').addEventListener('click', () => {
    $('panelMeasure').classList.toggle('open');
    renderMeasure();
  });
  $('measureMode').addEventListener('click', (e) => {
    const tab = e.target.closest('.tab');
    if (!tab) return;
    measureMode = tab.dataset.mode;
    $('measureMode').querySelectorAll('.tab').forEach((t) =>
      t.classList.toggle('active', t === tab));
    measurePoints = [];
    renderMeasure();
    $('measureLive').innerHTML = `<div class="det-empty">点击视频画面放置点位（${MEASURE_CN[measureMode]}）</div>`;
  });
  $('btnMeasureClear').addEventListener('click', (e) => {
    e.stopPropagation();
    clearMeasure();
  });
  $('btnMeasureSave').addEventListener('click', (e) => {
    e.stopPropagation();
    saveMeasureShot();
  });

  // ---- Haar 目标检测 ----
  $('panelHaarHead').addEventListener('click', () => {
    $('panelHaar').classList.toggle('open');
  });
  $('haarTarget').addEventListener('click', (e) => {
    const tab = e.target.closest('.tab');
    if (!tab) return;
    haarTarget = tab.dataset.target;
    $('haarTarget').querySelectorAll('.tab').forEach((t) =>
      t.classList.toggle('active', t === tab));
    $('haarList').innerHTML = `<div class="det-empty">点击「检测」开始（${HAAR_CN[haarTarget]}）</div>`;
  });
  $('btnHaar').addEventListener('click', (e) => {
    e.stopPropagation();
    runHaar();
  });
  $('btnHaarSave').addEventListener('click', (e) => {
    e.stopPropagation();
    saveHaarAnnotated();
  });

  // ---- 深度与测距 ----
  $('panelDepthHead').addEventListener('click', () => {
    $('panelDepth').classList.toggle('open');
    fetchDepthStatus();
    refreshDepthImg();
  });
  $('btnDepthToggle').addEventListener('click', (e) => {
    e.stopPropagation();
    toggleDepth();
  });
  $('btnDepthSnap').addEventListener('click', (e) => {
    e.stopPropagation();
    saveDepthFrame();
  });
  const depthWrapEl = $('depthFrameWrap');
  if (depthWrapEl) {
    depthWrapEl.addEventListener('click', (e) => {
      e.stopPropagation();
      depthImgMeasure(depthWrapEl, $('depthImg'), $('depthCross'), e.clientX, e.clientY);
    });
  }
  const depthColorWrapEl = $('depthColorWrap');
  if (depthColorWrapEl) {
    depthColorWrapEl.addEventListener('click', (e) => {
      e.stopPropagation();
      depthImgMeasure(depthColorWrapEl, $('depthColorImg'), $('depthColorCross'), e.clientX, e.clientY);
    });
  }
  startDepthImgLoop();

  // ---- 三维点云 ----
  $('panelPointCloudHead').addEventListener('click', () => {
    $('panelPointCloud').classList.toggle('open');
  });
  $('btnPcLoad').addEventListener('click', (e) => { e.stopPropagation(); loadPointcloud(); });
  $('pcStep').addEventListener('input', () =>
    $('pcStepVal').textContent = $('pcStep').value);
  initPointCloudInteract();

  // ---- 文字识别 ----
  $('panelOcrHead').addEventListener('click', () => {
    $('panelOcr').classList.toggle('open');
  });
  $('btnOcr').addEventListener('click', (e) => {
    e.stopPropagation();
    runOcr();
  });
  $('btnOcrSave').addEventListener('click', (e) => {
    e.stopPropagation();
    saveOcrAnnotated();
  });
  $('ocrAlnumOnly').addEventListener('change', () => {
    // 勾选后若已有结果, 不自动重跑, 由用户决定
  });
  $('ocrClose').addEventListener('click', () =>
    $('ocrMask').classList.remove('show'));
  $('ocrMask').addEventListener('click', (e) => {
    if (e.target === $('ocrMask')) $('ocrMask').classList.remove('show');
  });
  $('ocrDownload').addEventListener('click', saveOcrAnnotated);

  // ---- 二维码识别 ----
  $('panelQrHead').addEventListener('click', () => {
    $('panelQr').classList.toggle('open');
  });
  $('btnQr').addEventListener('click', (e) => {
    e.stopPropagation();
    runQr();
  });
  $('btnQrSave').addEventListener('click', (e) => {
    e.stopPropagation();
    saveQrAnnotated();
  });
  $('qrClose').addEventListener('click', () =>
    $('qrMask').classList.remove('show'));
  $('qrMask').addEventListener('click', (e) => {
    if (e.target === $('qrMask')) $('qrMask').classList.remove('show');
  });
  $('qrDownload').addEventListener('click', saveQrAnnotated);

  // ---- 目标检测 / 水果识别 ----
  $('panelYoloHead').addEventListener('click', () => {
    $('panelYolo').classList.toggle('open');
  });
  $('btnYolo').addEventListener('click', (e) => {
    e.stopPropagation();
    runYolo();
  });
  $('btnYoloLive').addEventListener('click', (e) => {
    e.stopPropagation();
    toggleYoloLive();
  });
  $('yoloLiveInterval').addEventListener('input', (e) => {
    $('yoloLiveIntervalVal').textContent = (parseInt(e.target.value, 10) / 1000).toFixed(1) + ' 秒';
  });
  $('btnYoloSave').addEventListener('click', (e) => {
    e.stopPropagation();
    saveYoloAnnotated();
  });
  $('yoloConfSlider').addEventListener('input', (e) => {
    $('yoloConfVal').textContent = (parseInt(e.target.value, 10) / 100).toFixed(2);
  });
  $('yoloClose').addEventListener('click', () =>
    $('yoloMask').classList.remove('show'));
  $('yoloMask').addEventListener('click', (e) => {
    if (e.target === $('yoloMask')) $('yoloMask').classList.remove('show');
  });
  $('yoloDownload').addEventListener('click', saveYoloAnnotated);

  // ---- 摄像头参数 ----
  $('panelCamHead').addEventListener('click', () => {
    $('panelCam').classList.toggle('open');
    if ($('panelCam').classList.contains('open')) loadCamCaps();
  });
  $('btnCamApply').addEventListener('click', (e) => {
    e.stopPropagation();
    applyCamSettings();
  });
  $('btnCamReset').addEventListener('click', (e) => {
    e.stopPropagation();
    resetCamSettings();
  });
  $('btnCamProbe').addEventListener('click', (e) => {
    e.stopPropagation();
    probeCamRanges();
  });

  // ---- 运动检测 ----
  $('panelMotionHead').addEventListener('click', () => {
    $('panelMotion').classList.toggle('open');
    fetchMotion();
  });
  $('btnMotionToggle').addEventListener('click', (e) => {
    e.stopPropagation();
    toggleMotion();
  });
  $('btnMotionClear').addEventListener('click', (e) => {
    e.stopPropagation();
    clearMotionLog();
  });
  $('motionSens').addEventListener('input', (e) => {
    $('motionSensVal').textContent = e.target.value;
    pushMotionSettings();
  });
  $('motionCooldown').addEventListener('input', (e) => {
    $('motionCooldownVal').textContent = e.target.value + 's';
    pushMotionSettings();
  });
  $('motionSave').addEventListener('change', pushMotionSettings);
  $('motionSmart').addEventListener('change', pushMotionSettings);

  $('filterTabs').addEventListener('click', (e) => {
    const tab = e.target.closest('.tab');
    if (!tab) return;
    currentFilter = tab.dataset.filter;
    $('filterTabs').querySelectorAll('.tab').forEach((t) =>
      t.classList.toggle('active', t === tab));
    renderFiles();
  });

  $('fileGrid').addEventListener('click', (e) => {
    const card = e.target.closest('.file-card');
    if (!card) return;
    const name = decodeURIComponent(card.dataset.name);
    const actEl = e.target.closest('[data-act]');
    const act = actEl ? actEl.dataset.act : 'preview';
    if (act === 'preview') openPreview(name);
    else if (act === 'download') downloadFile(name);
    else if (act === 'delete') askDelete(name);
  });

  $('previewClose').addEventListener('click', closePreview);
  $('previewMask').addEventListener('click', (e) => {
    if (e.target === $('previewMask')) closePreview();
  });
  $('previewDownload').addEventListener('click', () => {
    if (currentPreview) downloadFile(currentPreview.name);
  });

  $('confirmClose').addEventListener('click', closeConfirm);
  $('confirmCancel').addEventListener('click', closeConfirm);
  $('confirmOk').addEventListener('click', doDelete);
  $('confirmMask').addEventListener('click', (e) => {
    if (e.target === $('confirmMask')) closeConfirm();
  });

  document.addEventListener('keydown', (e) => {
    if (e.repeat || e.ctrlKey || e.altKey || e.metaKey) return;
    const target = e.target;
    if (target && target.closest('input, textarea, select, button, [contenteditable="true"]')) return;
    const key = e.key.toLowerCase();
    const shortcuts = { ' ': btnRun, r: btnRec, s: $('btnSnap'), f: $('btnFull') };
    if (shortcuts[key]) {
      e.preventDefault();
      shortcuts[key].click();
      return;
    }
    if (e.key === 'Escape') {
      if ($('confirmMask').classList.contains('show')) closeConfirm();
      else if ($('previewMask').classList.contains('show')) closePreview();
    }
  });

  // 视频流断了自动重连
  streamImg.addEventListener('error', () => {
    if (running) {
      setTimeout(() => { streamImg.src = '/stream?t=' + Date.now(); }, 1500);
    }
  });

  /* ---------------- 图像增强 ---------------- */
  function enhancePayload() {
    return {
      enabled: $('enhanceEnable').checked,
      sharpen: +$('enhanceSharpen').value,
      denoise: +$('enhanceDenoise').value,
      equalize: $('enhanceEqualize').checked,
      edge: $('enhanceEdge').checked,
    };
  }
  function renderEnhance(d) {
    if (!d) return;
    const on = !!d.enabled;
    $('enhanceBadge').textContent = on ? '开启' : '关闭';
    $('enhanceBadge').style.color = on ? 'var(--accent)' : 'var(--muted)';
    $('enhanceBadge').style.borderColor = on ? 'rgba(61,220,132,.45)' : 'var(--line)';
    $('enhanceSummary').textContent = on
      ? `锐化 ${d.sharpen} · 去噪 ${d.denoise}${d.equalize ? ' · 直方图' : ''}${d.edge ? ' · 边缘' : ''}`
      : '锐化 / 去噪 / 直方图 / 边缘';
  }
  async function applyEnhance() {
    try {
      const r = await fetch('/enhance/settings', {
        method: 'POST', cache: 'no-store',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(enhancePayload()),
      });
      renderEnhance(await r.json());
    } catch (e) { /* 静默 */ }
  }
  async function fetchEnhance() {
    try {
      const r = await fetch('/enhance/status', { cache: 'no-store' });
      const d = await r.json();
      $('enhanceEnable').checked = !!d.enabled;
      $('enhanceSharpen').value = d.sharpen ?? 0;
      $('enhanceDenoise').value = d.denoise ?? 0;
      $('enhanceEqualize').checked = !!d.equalize;
      $('enhanceEdge').checked = !!d.edge;
      $('enhanceSharpenVal').textContent = d.sharpen ?? 0;
      $('enhanceDenoiseVal').textContent = d.denoise ?? 0;
      renderEnhance(d);
    } catch (e) { /* 静默 */ }
  }

  /* ---------------- 定时抓拍 ---------------- */
  async function fetchTimelapse() {
    try {
      const r = await fetch('/timelapse/status', { cache: 'no-store' });
      const d = await r.json();
      const on = !!d.running;
      $('timelapseBadge').textContent = on ? '抓拍中' : (d.saved ? '已停' : '待机');
      $('timelapseBadge').style.color = on ? 'var(--accent)' : 'var(--muted)';
      $('timelapseBadge').style.borderColor = on ? 'rgba(61,220,132,.45)' : 'var(--line)';
      $('timelapseSummary').textContent = on
        ? `每 ${d.interval}s · 已拍 ${d.saved} 张`
        : `已拍 ${d.saved} 张${d.last_file ? ' · ' + d.last_file : ''}`;
      $('tlStatus').innerHTML = `
        <div class="row"><span class="big" style="color:var(--accent)">${d.saved} 张</span>
          <span>${on ? '抓拍中 · 间隔 ' + d.interval + 's' : '已停止'}</span></div>
        <div class="row">最近: <span style="color:var(--ink);margin-left:6px">${escapeHtml(d.last_file || '—')}</span></div>
        ${d.error ? '<div class="row" style="color:var(--danger)">' + escapeHtml(d.error) + '</div>' : ''}`;
    } catch (e) { /* 静默 */ }
  }
  async function timelapseStart() {
    try {
      const r = await fetch('/timelapse/start', {
        method: 'POST', cache: 'no-store',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ interval: +$('tlInterval').value, count: +$('tlCount').value }),
      });
      const d = await r.json();
      if (d.ok === false) toast(d.error || '启动失败', true);
      else toast('定时抓拍已开始');
      fetchTimelapse();
    } catch (e) { toast('启动失败: ' + e.message, true); }
  }
  async function timelapseStop() {
    try {
      await fetch('/timelapse/stop', { method: 'POST', cache: 'no-store' });
      toast('定时抓拍已停止');
      fetchTimelapse();
    } catch (e) { /* 静默 */ }
  }

  // 图像增强控件
  $('enhanceEnable').addEventListener('change', applyEnhance);
  $('enhanceEqualize').addEventListener('change', applyEnhance);
  $('enhanceEdge').addEventListener('change', applyEnhance);
  $('enhanceSharpen').addEventListener('input', () => {
    $('enhanceSharpenVal').textContent = $('enhanceSharpen').value;
    applyEnhance();
  });
  $('enhanceDenoise').addEventListener('input', () => {
    $('enhanceDenoiseVal').textContent = $('enhanceDenoise').value;
    applyEnhance();
  });
  $('panelEnhanceHead').addEventListener('click', () => {
    $('panelEnhance').classList.toggle('open');
    fetchEnhance();
  });
  // 定时抓拍控件
  $('tlInterval').addEventListener('input', () =>
    $('tlIntervalVal').textContent = $('tlInterval').value);
  $('tlCount').addEventListener('input', () =>
    $('tlCountVal').textContent = $('tlCount').value);
  $('btnTlStart').addEventListener('click', (e) => { e.stopPropagation(); timelapseStart(); });
  $('btnTlStop').addEventListener('click', (e) => { e.stopPropagation(); timelapseStop(); });
  $('panelTimelapseHead').addEventListener('click', () => {
    $('panelTimelapse').classList.toggle('open');
    fetchTimelapse();
  });

  /* ---------------- 条形码识别 ---------------- */
  function renderBarcode(d) {
    const items = d.results || [];
    $('barcodeBadge').textContent = items.length ? `检出 ${items.length}` : '未检出';
    $('barcodeBadge').style.color = items.length ? 'var(--accent)' : 'var(--muted)';
    $('barcodeCount').textContent = `(${items.length} 个 · ${d.elapsed_ms}ms)`;
    if (!items.length) {
      $('barcodeList').innerHTML = '<div class="det-empty">未检测到条形码 · 请对准商品条码</div>';
      return;
    }
    $('barcodeList').innerHTML = items.map((it) => `
      <div class="det-row">
        <span class="nm" style="color:var(--ink)">${escapeHtml(it.text)}</span>
        <span class="ps">${escapeHtml(it.type)}</span>
      </div>`).join('');
  }
  async function runBarcode() {
    const btn = $('btnBarcode');
    btn.disabled = true;
    $('barcodeBtnText').textContent = '识别中…';
    $('barcodeList').innerHTML = '<div class="det-empty">识别中…</div>';
    try {
      const r = await fetch('/barcode', { method: 'POST', cache: 'no-store' });
      const d = await r.json();
      if (d.ok === false) {
        $('barcodeList').innerHTML = `<div class="det-empty">${escapeHtml(d.error || '识别失败')}</div>`;
        $('barcodeBadge').textContent = '失败';
      } else {
        renderBarcode(d);
      }
    } catch (e) {
      $('barcodeList').innerHTML = `<div class="det-empty">识别失败: ${escapeHtml(e.message)}</div>`;
    } finally {
      btn.disabled = false;
      $('barcodeBtnText').textContent = '识别条形码';
    }
  }
  function saveBarcodeAnnotated() {
    const a = document.createElement('a');
    a.href = '/barcode/annotated?t=' + Date.now();
    a.download = 'barcode_' + Date.now() + '.jpg';
    document.body.appendChild(a); a.click(); document.body.removeChild(a);
    toast('标注图已下载');
  }
  $('panelBarcodeHead').addEventListener('click', () =>
    $('panelBarcode').classList.toggle('open'));
  $('btnBarcode').addEventListener('click', (e) => { e.stopPropagation(); runBarcode(); });
  $('btnBarcodeSave').addEventListener('click', (e) => { e.stopPropagation(); saveBarcodeAnnotated(); });

  /* ---------------- 手势交互 ---------------- */
  const GESTURE_CN = {
    open_palm: '张掌', fist: '握拳', point: '指向', victory: 'V字',
    thumbs_up: '点赞', ok: 'OK', pinky: '小指', unknown: '未知',
  };
  function renderGesture(d) {
    const items = d.results || [];
    $('gestureBadge').textContent = items.length ? `检出 ${items.length} 只手` : '未检出';
    $('gestureBadge').style.color = items.length ? 'var(--accent)' : 'var(--muted)';
    $('gestureCount').textContent = `(${items.length} 只手 · ${d.elapsed_ms || 0}ms)`;
    const extra = d.arm ? `<div class="det-row"><span class="nm" style="color:var(--accent)">机械臂动作: ${escapeHtml(d.arm.action || '—')}</span></div>` : '';
    if (!items.length) {
      $('gestureList').innerHTML = `<div class="det-empty">未检测到手 · 请把手放入镜头</div>${extra}`;
      return;
    }
    $('gestureList').innerHTML = items.map((it) => `
      <div class="det-row">
        <span class="sw" style="background:var(--accent)"></span>
        <span class="nm">${GESTURE_CN[it.gesture] || it.gesture} <span style="color:var(--muted)">${it.gesture}</span></span>
        <span class="ar" style="color:var(--accent)">${(it.confidence * 100).toFixed(0)}%</span>
        <span class="ps">${it.hand === 'Left' ? '左手' : '右手'} (${it.wrist[0]},${it.wrist[1]})</span>
      </div>`).join('') + extra;
  }
  async function runGesture(control = false, silent = false) {
    const btn = control ? $('btnGestureControl') : $('btnGesture');
    if (!silent) {
      btn.disabled = true;
      $('gestureBtnText').textContent = control ? '控制中…' : '识别中…';
      $('gestureList').innerHTML = '<div class="det-empty">识别中…</div>';
    }
    try {
      const r = await fetch(control ? '/gesture/control' : '/gesture',
        { method: 'POST', cache: 'no-store' });
      const d = await r.json();
      if (d.ok === false) {
        if (!silent) $('gestureList').innerHTML = `<div class="det-empty">${escapeHtml(d.error || '识别失败')}</div>`;
      } else {
        renderGesture(d);
        if (control && d.arm && !silent && /回零|抓取|停止|\+1/.test(d.arm.action || '')) {
          toast('机械臂: ' + d.arm.action);
        }
      }
    } catch (e) {
      if (!silent) $('gestureList').innerHTML = `<div class="det-empty">识别失败: ${escapeHtml(e.message)}</div>`;
    } finally {
      if (!silent) {
        btn.disabled = false;
        $('gestureBtnText').textContent = '识别手势';
      }
    }
  }
  function saveGestureAnnotated() {
    const a = document.createElement('a');
    a.href = '/gesture/annotated?t=' + Date.now();
    a.download = 'gesture_' + Date.now() + '.jpg';
    document.body.appendChild(a); a.click(); document.body.removeChild(a);
    toast('标注图已下载');
  }
  $('panelGestureHead').addEventListener('click', () =>
    $('panelGesture').classList.toggle('open'));
  $('btnGesture').addEventListener('click', (e) => { e.stopPropagation(); runGesture(false); });
  $('btnGestureControl').addEventListener('click', (e) => { e.stopPropagation(); runGesture(true); });
  $('btnGestureSave').addEventListener('click', (e) => { e.stopPropagation(); saveGestureAnnotated(); });

  /* ---------------- 手势菜单 ---------------- */
  const MENU_STATE_CN = { idle: '待选择', armed: '已预选', done: '已执行' };
  function renderGestureMenu(d) {
    const el = $('gestureMenuState');
    if (!el) return;
    const m = d.menu || {};
    const res = m.result || {};
    el.innerHTML = `
      <div class="row"><span class="big" style="color:var(--accent)">${MENU_STATE_CN[m.state] || m.state || '—'}</span>
        <span>${m.armed_name ? '模式' + (m.armed_index + 1) + ': ' + escapeHtml(m.armed_name) : '伸手指示选择功能'}</span></div>
      <div class="row">本次: <span style="color:var(--ink);margin-left:6px">${escapeHtml(d.action || '—')}${d.repeated ? ' (保持中)' : ''}</span></div>
      ${res.summary ? `<div class="row" style="color:var(--accent)">结果: ${escapeHtml(res.summary)}</div>` : ''}`;
  }
  function menuModes() {
    return [1, 2, 3, 4, 5].map((i) => ($('gmMode' + i) ? $('gmMode' + i).value : '无'));
  }
  async function runGestureMenu(silent = false) {
    const btn = $('btnGestureMenu');
    if (!silent) {
      btn.disabled = true;
      $('gestureList').innerHTML = '<div class="det-empty">手势菜单识别中…</div>';
    }
    try {
      const r = await fetch('/gesture/menu', {
        method: 'POST', cache: 'no-store',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ modes: menuModes() }),
      });
      const d = await r.json();
      if (d.ok === false) {
        if (!silent) $('gestureList').innerHTML = `<div class="det-empty">${escapeHtml(d.error || '识别失败')}</div>`;
      } else {
        renderGesture(d);
        renderGestureMenu(d);
      }
    } catch (e) {
      if (!silent) $('gestureList').innerHTML = `<div class="det-empty">识别失败: ${escapeHtml(e.message)}</div>`;
    } finally {
      if (!silent) btn.disabled = false;
    }
  }
  $('btnGestureMenu').addEventListener('click', (e) => { e.stopPropagation(); runGestureMenu(false); });

  // 连续手势: 勾选后 ~1.4s 周期识别/控制/菜单, 面板关闭不空转
  let gestureContTimer = null;
  function gestureContTick() {
    const cb = $('gestureCont');
    if (!cb || !cb.checked) {
      if (gestureContTimer) { clearInterval(gestureContTimer); gestureContTimer = null; }
      return;
    }
    const panel = $('panelGesture');
    if (!panel || !panel.classList.contains('open')) return;
    const mode = $('gestureContMode') ? $('gestureContMode').value : 'watch';
    if (mode === 'menu') runGestureMenu(true);
    else if (mode === 'air') runGestureAir(true);
    else runGesture(mode === 'control', true);
  }
  if ($('gestureCont')) {
    $('gestureCont').addEventListener('change', () => {
      if ($('gestureCont').checked) {
        if (!gestureContTimer) gestureContTimer = setInterval(gestureContTick, 1400);
        toast('连续手势已开启');
      } else if (gestureContTimer) {
        clearInterval(gestureContTimer);
        gestureContTimer = null;
      }
    });
  }

  /* ---------------- 空中手势控制 ---------------- */
  function renderAir(d) {
    const el = $('airStatus');
    if (!el) return;
    const a = d.air || {};
    const acts = (d.actions || []).map((s) => escapeHtml(s)).join('；') || '—';
    el.innerHTML = `
      <div class="row"><span class="big" style="color:var(--accent)">${d.in_region ? '手在 3D 区域内' : (d.wrist_3d ? '区域外' : '未检测到手/需深度')}</span>
        <span>手势: ${escapeHtml(d.gesture || '—')}${d.wave ? ' · 挥手' + (d.wave === 'right' ? '向右' : '向左') : ''}</span></div>
      <div class="row">手部 3D: <span style="color:var(--ink);margin-left:6px">${d.wrist_3d ? 'X' + d.wrist_3d[0] + '  Y' + d.wrist_3d[1] + '  Z' + d.wrist_3d[2] + ' mm' : '(需深度模式)'}</span></div>
      <div class="row">动作: <span style="color:var(--accent);margin-left:6px">${acts}</span></div>
      ${(a.log || []).slice(0, 4).map((l) => `<div class="row" style="color:var(--muted)">${escapeHtml(l)}</div>`).join('')}`;
  }
  async function runGestureAir(silent = false) {
    const btn = $('btnAirToggle');
    if (!silent && btn) btn.disabled = true;
    try {
      const r = await fetch('/gesture/air', { method: 'POST', cache: 'no-store' });
      const d = await r.json();
      if (d.ok === false) {
        if (!silent) toast(d.error || '空中手势失败', true);
      } else {
        renderAir(d);
        if (!silent && d.actions && d.actions.length) toast(d.actions[0]);
      }
    } catch (e) {
      if (!silent) toast('空中手势失败: ' + e.message, true);
    } finally {
      if (!silent && btn) btn.disabled = false;
    }
  }
  function airApplyConfig() {
    const v = (id) => +($(id) ? $(id).value : 0);
    fetch('/gesture/air/config', {
      method: 'POST', cache: 'no-store',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        region: {
          x: [v('airXmin'), v('airXmax')],
          y: [v('airYmin'), v('airYmax')],
          z: [v('airZmin'), v('airZmax')],
        },
        action: $('airAction') ? $('airAction').value : 'match_score',
      }),
    }).then((r) => r.json())
      .then((d) => toast('区域/动作已更新: ' + (d.action_cn || d.action)))
      .catch((e) => toast('配置失败: ' + e.message, true));
  }
  if ($('btnAirToggle')) $('btnAirToggle').addEventListener('click', (e) => {
    e.stopPropagation();
    $('gestureContMode').value = 'air';
    $('gestureCont').checked = !$('gestureCont').checked;
    $('gestureCont').dispatchEvent(new Event('change'));
    $('btnAirToggle').textContent = $('gestureCont').checked ? '停止空中手势' : '启动空中手势';
  });
  if ($('btnAirOnce')) $('btnAirOnce').addEventListener('click', (e) => { e.stopPropagation(); runGestureAir(false); });
  if ($('btnAirConfig')) $('btnAirConfig').addEventListener('click', (e) => { e.stopPropagation(); airApplyConfig(); });

  /* ---------------- AI 视觉自动分拣 ---------------- */
  async function runAutoSort() {
    const btn = $('btnAutoSort');
    if (btn) btn.disabled = true;
    $('sortReport').innerHTML = '<div class="det-empty">分拣执行中…(识别 → 3D位置+抓取角 → 机械臂放置)</div>';
    try {
      const r = await fetch('/arm/autosort', {
        method: 'POST', cache: 'no-store',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          sort_by: $('sortBy') ? $('sortBy').value : 'color',
          max_objects: +($('sortMax') ? $('sortMax').value : 3),
        }),
      });
      const d = await r.json();
      if (d.ok === false) {
        $('sortReport').innerHTML = `<div class="det-empty">${escapeHtml(d.error || '分拣失败')}</div>`;
      } else {
        $('sortReport').innerHTML = `
          <div class="row"><span class="big" style="color:var(--accent)">${d.count} 个物体已分拣</span>
            <span>模式: ${d.sort_by === 'class' ? '目标类别' : '颜色'}</span></div>
          ${(d.objects || []).map((o) => `
          <div class="row">${escapeHtml(o.label)} → 位置(${o.position_mm.join(', ')})mm　抓取角 ${o.grasp_angle_deg}° → 料箱(${o.bin_mm.join(', ')}) <span style="color:var(--muted)">[${o.source === 'depth' ? '真实3D' : '演示映射'}]</span></div>`).join('')}
          <div class="row" style="color:var(--muted)">${escapeHtml(d.note || '')}</div>`;
        if (d.arm) renderArm(d.arm);
      }
    } catch (e) {
      $('sortReport').innerHTML = `<div class="det-empty">分拣失败: ${escapeHtml(e.message)}</div>`;
    } finally {
      if (btn) btn.disabled = false;
    }
  }
  if ($('btnAutoSort')) $('btnAutoSort').addEventListener('click', (e) => { e.stopPropagation(); runAutoSort(); });

  /* ---------------- 分类标签页 ---------------- */
  function switchCat(cat) {
    document.querySelectorAll('.cat-tab').forEach((b) =>
      b.classList.toggle('active', b.dataset.cat === cat));
    document.querySelectorAll('.panel-area .panel').forEach((p) => {
      if (p.dataset.cat) p.style.display = (p.dataset.cat === cat) ? '' : 'none';
    });
    try { localStorage.setItem('usbCamCat', cat); } catch (e) {}
  }
  document.querySelectorAll('.cat-tab').forEach((b) =>
    b.addEventListener('click', () => switchCat(b.dataset.cat)));
  let savedCat = 'identify';
  try { savedCat = localStorage.getItem('usbCamCat') || 'identify'; } catch (e) {}
  switchCat(savedCat);

  /* ---------------- YOLO 自定义模型 ---------------- */
  async function loadYoloModel() {
    const el = $('yoloModelPath');
    const path = (el && el.value || '').trim();
    if (!path) { toast('请填写模型 .pt 路径', true); return; }
    const btn = $('btnYoloLoadModel');
    btn.disabled = true;
    btn.textContent = '加载中…';
    try {
      const r = await fetch('/yolo/model', {
        method: 'POST', cache: 'no-store',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path }),
      });
      const d = await r.json();
      if (d.ok === false) {
        toast(d.error || '模型加载失败', true);
      } else {
        toast('模型已加载: ' + d.model);
        const nm = $('yoloModelName');
        if (nm) nm.textContent = '当前: ' + d.model;
      }
    } catch (e) {
      toast('模型加载失败: ' + e.message, true);
    } finally {
      btn.disabled = false;
      btn.textContent = '加载模型';
    }
  }
  $('btnYoloLoadModel').addEventListener('click', (e) => { e.stopPropagation(); loadYoloModel(); });

  /* ---------------- 机械臂控制器 ---------------- */
  async function armPost(path, body) {
    try {
      const r = await fetch(path, {
        method: 'POST', cache: 'no-store',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body || {}),
      });
      return await r.json();
    } catch (e) { return { ok: false, error: e.message }; }
  }
  function renderArm(d) {
    if (!d) return;
    const on = d.mode === 'real' && d.real_enabled;
    const emg = d.state === 'emergency';
    $('armBadge').textContent = emg ? '急停' : (on ? '真实驱动' : '仿真');
    $('armBadge').style.color = emg ? 'var(--danger)' : (on ? 'var(--warn)' : 'var(--accent)');
    $('armSummary').textContent = `关节 ${(d.joints || []).join('/')} · 状态 ${d.state}`;
    const info = $('armState');
    if (info) info.innerHTML = `
      <div class="row">模式: <span style="color:var(--ink);margin-left:6px">${d.mode}${on ? ' (真实)' : ''}</span>　状态: <span style="color:var(--ink)">${d.state}</span></div>
      <div class="row">关节: <span style="color:var(--ink);margin-left:6px">${(d.joints || []).map((j) => j + '°').join('  ')}</span></div>
      <div class="row">位置: <span style="color:var(--ink);margin-left:6px">X${(d.position || [])[0]} Y${(d.position || [])[1]} Z${(d.position || [])[2]} mm</span></div>
      <div class="row">驱动: <span style="color:var(--muted);margin-left:6px">${d.driver ? (d.driver.connected ? '已连接' : '未连接' + (d.driver.error ? ' · ' + d.driver.error : '')) : '—'}</span></div>`;
  }
  function renderMatch(m) {
    const el = $('armMatchState');
    if (!el || !m) return;
    el.innerHTML = `
      <div class="row"><span class="big" style="color:var(--accent)">${m.score} 分</span>
        <span>${m.running ? '进行中 · ' + escapeHtml(m.task) : (m.task ? '已结束 · ' + escapeHtml(m.task) : '未开始')}</span></div>
      <div class="row">用时: <span style="color:var(--ink);margin-left:6px">${m.elapsed}s</span>　目标 ${m.goals} 个</div>
      ${(m.log || []).map((l) => `<div class="row" style="color:var(--muted)">${escapeHtml(l)}</div>`).join('')}`;
  }
  async function fetchArm() {
    try {
      const r = await fetch('/arm/state', { cache: 'no-store' });
      const d = await r.json();
      renderArm(d);
      if (d.match) renderMatch(d.match);
    } catch (e) { /* 静默 */ }
  }
  function armAngles() {
    return [1, 2, 3, 4, 5, 6].map((i) => +($('armJ' + i) ? $('armJ' + i).value : 90));
  }
  // 运动
  $('btnArmHome').addEventListener('click', async (e) => { e.stopPropagation(); renderArm(await armPost('/arm/home')); });
  $('btnArmStop').addEventListener('click', async (e) => { e.stopPropagation(); renderArm(await armPost('/arm/stop')); });
  $('btnArmReset').addEventListener('click', async (e) => { e.stopPropagation(); renderArm(await armPost('/arm/reset')); });
  $('btnArmEmergency').addEventListener('click', async (e) => { e.stopPropagation(); renderArm(await armPost('/arm/emergency')); toast('急停已触发', true); });
  $('btnArmMove').addEventListener('click', async (e) => {
    e.stopPropagation();
    renderArm(await armPost('/arm/move', { x: +$('armX').value, y: +$('armY').value, z: +$('armZ').value }));
  });
  // 关节
  for (let i = 1; i <= 6; i++) {
    const s = $('armJ' + i), v = $('armJ' + i + 'Val');
    if (s && v) s.addEventListener('input', () => v.textContent = s.value);
  }
  $('btnArmJoints').addEventListener('click', async (e) => {
    e.stopPropagation();
    renderArm(await armPost('/arm/joints', { angles: armAngles() }));
  });
  // 比赛
  $('btnMatchStart').addEventListener('click', async (e) => {
    e.stopPropagation();
    const d = await armPost('/arm/match/start', { task: $('armTask').value });
    if (d.ok === false) toast(d.error || '开始失败', true); else { renderMatch(d.match); toast('比赛开始'); }
    fetchArm();
  });
  $('btnMatchScore').addEventListener('click', async (e) => {
    e.stopPropagation();
    const d = await armPost('/arm/match/score', { delta: 1 });
    if (d.ok === false) toast(d.error || '计分失败', true); else renderMatch(d.match);
  });
  $('btnMatchStop').addEventListener('click', async (e) => {
    e.stopPropagation();
    const d = await armPost('/arm/match/stop');
    if (d.ok === false) toast(d.error || '结束失败', true); else { renderMatch(d.match); toast('比赛结束'); }
    fetchArm();
  });
  // 真实驱动配置
  $('btnArmReal').addEventListener('click', async (e) => {
    e.stopPropagation();
    const enable = $('armRealEnable').checked;
    const d = await armPost('/arm/real', {
      port: $('armPort').value.trim(), model: $('armModel').value.trim(),
      protocol: $('armProtocol').value, enable,
    });
    renderArm(d);
    toast(enable ? '已请求启用真实驱动(需协议绑定)' : '已切换仿真', enable);
    fetchArm();
  });
  $('panelArmHead').addEventListener('click', () => {
    $('panelArm').classList.toggle('open');
    fetchArm();
  });

  /* ---------------- 定时器 ---------------- */

  // 录像计时 (本地计时 + 定期与后端同步)
  setInterval(() => {
    if (recording && recordStartedAt) {
      recTimer.textContent = 'REC ' + fmtElapsed((Date.now() - recordStartedAt) / 1000);
    }
  }, 500);

  fetchStatus();
  setInterval(fetchStatus, 1500);
  // 运动检测/物体追踪状态独立轮询 (只在面板打开时)
  setInterval(() => {
    if ($('panelMotion').classList.contains('open')) fetchMotion();
    if ($('panelTrack').classList.contains('open')) fetchTrack();
    if ($('panelTimelapse').classList.contains('open')) fetchTimelapse();
    if ($('panelArm') && $('panelArm').classList.contains('open')) fetchArm();
  }, 1500);

  // 初次加载文件列表(面板可能是折叠的, 静默加载)
  loadFiles(true);
})();
