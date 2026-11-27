/**
 * app.js —— 扫码页前端
 *
 * 设计目标：手机摄像头经常拍不清二维码，所以采集端做三层兜底。
 *   1) 摄像头按高分辨率 + 连续对焦要流，并优先用 ImageCapture.takePhoto() 抓原尺寸照片；
 *   2) 浏览器端 jsQR 多路重试（原图 / 灰度+拉对比）；
 *   3) 还解不出来 → 把原图 POST 给 /decode，由服务端 zxing-cpp 多路解码。
 * 另外保留：系统相机拍照（capture=environment）、选图、拖图、粘贴、手写文本。
 */
(() => {
  'use strict';

  const $ = id => document.getElementById(id);
  const log = m => {
    const el = $('log');
    el.textContent = m + '\n' + el.textContent;
  };

  let stopped = false;
  let busy = false;
  let track = null;
  const canvas = document.createElement('canvas');
  const ctx = canvas.getContext('2d', { willReadFrequently: true });

  // ---------------------------------------------------------------- 网络
  async function postJSON(path, body) {
    const r = await fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    return r.json();
  }

  async function postImage(blob) {
    const r = await fetch('decode', {
      method: 'POST',
      headers: { 'Content-Type': blob.type || 'image/jpeg' },
      body: blob,
    });
    return r.json();
  }

  // ---------------------------------------------------------------- 结果展示
  function showResult(j, src) {
    const head = j.ok ? '✅ 成功' : (j.retry ? '⏱ 码已过期' : '❌ 未成功');
    log(head + '  ' + (j.detail || '') + (src ? '  [' + src + ']' : ''));

    (j.results || []).forEach(it => {
      const raw = it.raw || {};
      log('  · ' + (it.name || it.tag) + ' HTTP ' + (raw.http ?? '-') +
          '  status=' + (raw.status || '-') +
          '  ' + String(raw.body || '（无 body）').slice(0, 500));
      if (it.status) log('    名单确认 rollcall_status=' + it.status);
    });

    if (j.retry) {
      $('hint').textContent = '码过期了，再扫一次（单码只活十几秒）';
      setTimeout(() => { stopped = false; tick(); }, 500);
    } else {
      $('hint').textContent = j.ok ? '签到成功' : '没签上，看下面原因';
    }
  }

  // 已经拿到载荷：交给 /scan（服务端自己会多账号提交）
  async function submitPayload(text) {
    if (busy) return;
    busy = true; stopped = true;
    $('hint').textContent = '已读到码，提交中…';
    try {
      showResult(await postJSON('scan', { qr: text }), 'jsQR');
    } catch (e) {
      log('提交失败: ' + e);
    } finally {
      busy = false;
    }
  }

  // 整张图交给服务端解码（抗模糊）
  async function submitImage(blob, tag) {
    if (busy) return;
    busy = true; stopped = true;
    $('hint').textContent = '上传原图给服务器解码…';
    try {
      const j = await postImage(blob);
      if (j.payload && !j.ok && !(j.results || []).length) {
        log('服务器解出码: ' + String(j.payload).slice(0, 80));
      }
      showResult(j, tag + (j.decode ? ' · ' + j.decode : ''));
    } catch (e) {
      log('上传失败: ' + e);
    } finally {
      busy = false;
    }
  }

  // ---------------------------------------------------------------- 摄像头
  async function openCamera() {
    const want = {
      audio: false,
      video: {
        facingMode: { ideal: 'environment' },
        width: { ideal: 1920, min: 1280 },
        height: { ideal: 1080, min: 720 },
        advanced: [{ focusMode: 'continuous' }],
      },
    };
    let s;
    try {
      s = await navigator.mediaDevices.getUserMedia(want);
    } catch (e) {
      log('高分辨率被拒，退回默认: ' + e.name);
      s = await navigator.mediaDevices.getUserMedia({
        video: { facingMode: { ideal: 'environment' } }, audio: false,
      });
    }
    const v = $('v');
    v.srcObject = s;
    await v.play();
    track = s.getVideoTracks()[0];
    const st = track.getSettings ? track.getSettings() : {};
    log('摄像头: ' + (st.width || '?') + 'x' + (st.height || '?') +
        ' focus=' + (st.focusMode || '?'));
    const caps = track.getCapabilities ? track.getCapabilities() : {};
    if (caps.torch) $('torch').disabled = false;
  }

  $('torch').onclick = async () => {
    if (!track) return;
    const cur = (track.getSettings && track.getSettings().torch) || false;
    try {
      await track.applyConstraints({ advanced: [{ torch: !cur }] });
      log('手电筒 ' + (!cur ? '开' : '关'));
    } catch (e) {
      log('手电筒不可用: ' + e.name);
    }
  };

  $('go').onclick = async () => {
    if (!track) {
      try {
        await openCamera();
      } catch (e) {
        log('打开摄像头失败: ' + e + '（https 证书要点「继续访问」）');
        return;
      }
    }
    stopped = false;
    busy = false;
    tick();
  };

  // 灰度 + 2% 截断拉伸：模糊帧上这一步比任何参数都管用
  function contrastPass(img, w, h) {
    const g = new Uint8ClampedArray(img.data.length);
    const hist = new Uint32Array(256);
    for (let i = 0; i < img.data.length; i += 4) {
      const y = (img.data[i] * 0.299 + img.data[i + 1] * 0.587 + img.data[i + 2] * 0.114) | 0;
      g[i] = g[i + 1] = g[i + 2] = y;
      g[i + 3] = 255;
      hist[y]++;
    }
    let lo = 0, hi = 255, acc = 0;
    const total = w * h;
    for (let v = 0; v < 256; v++) { acc += hist[v]; if (acc > total * 0.02) { lo = v; break; } }
    acc = 0;
    for (let v = 255; v >= 0; v--) { acc += hist[v]; if (acc > total * 0.02) { hi = v; break; } }
    if (hi > lo + 10) {
      const k = 255 / (hi - lo);
      for (let i = 0; i < g.length; i += 4) {
        let y = (g[i] - lo) * k;
        y = y < 0 ? 0 : (y > 255 ? 255 : y);
        g[i] = g[i + 1] = g[i + 2] = y;
      }
    }
    return g;
  }

  function tick() {
    if (stopped || busy) return;
    const v = $('v');
    if (v.readyState >= 2 && v.videoWidth) {
      canvas.width = v.videoWidth;
      canvas.height = v.videoHeight;
      ctx.drawImage(v, 0, 0);
      const img = ctx.getImageData(0, 0, canvas.width, canvas.height);

      const raw = jsQR(img.data, canvas.width, canvas.height, { inversionAttempts: 'attemptBoth' });
      if (raw && raw.data) { log('jsQR(raw): ' + raw.data.slice(0, 70) + '…'); submitPayload(raw.data); return; }

      const g = contrastPass(img, canvas.width, canvas.height);
      const sharp = jsQR(g, canvas.width, canvas.height, { inversionAttempts: 'attemptBoth' });
      if (sharp && sharp.data) { log('jsQR(灰度): ' + sharp.data.slice(0, 70) + '…'); submitPayload(sharp.data); return; }
    }
    requestAnimationFrame(tick);
  }

  // 全分辨率静态照片：视频帧被压缩时靠这个
  async function snapFull() {
    if (!track || typeof ImageCapture === 'undefined') return null;
    try {
      const blob = await new ImageCapture(track).takePhoto();
      log('抓到原尺寸照片 ' + Math.round(blob.size / 1024) + 'KB');
      return blob;
    } catch (e) {
      log('takePhoto 不可用: ' + e.name);
      return null;
    }
  }

  // 摄像头开着时，每 4 秒抓一张原尺寸照片丢给服务端解
  setInterval(async () => {
    if (!track || stopped || busy) return;
    const blob = await snapFull();
    if (blob) submitImage(blob, '原尺寸照片');
  }, 4000);

  // ---------------------------------------------------------------- 选图 / 拖图 / 粘贴 / 文本
  function wireInput(input) {
    input.onchange = async e => {
      const f = e.target.files && e.target.files[0];
      input.value = '';
      if (!f) return;
      log('选到图片 ' + Math.round(f.size / 1024) + 'KB，上传解码…');
      await submitImage(f, '上传原图');
    };
  }
  wireInput($('file'));
  wireInput($('filecam'));
  $('cam').onclick = () => $('filecam').click();
  $('drop').onclick = () => $('file').click();
  $('send').onclick = () => {
    const t = $('manual').value.trim();
    if (t) submitPayload(t);
  };

  const drop = $('drop');
  ['dragenter', 'dragover'].forEach(ev => drop.addEventListener(ev, e => {
    e.preventDefault();
    drop.style.background = 'rgba(29,182,194,.15)';
  }));
  ['dragleave', 'drop'].forEach(ev => drop.addEventListener(ev, e => {
    e.preventDefault();
    drop.style.background = 'transparent';
  }));
  drop.addEventListener('drop', async e => {
    const f = e.dataTransfer.files && e.dataTransfer.files[0];
    if (f) { log('拖入图片 ' + Math.round(f.size / 1024) + 'KB'); await submitImage(f, '拖入原图'); }
  });
  window.addEventListener('paste', async e => {
    for (const it of (e.clipboardData || {}).items || []) {
      if (it.type && it.type.startsWith('image/')) {
        await submitImage(it.getAsFile(), '粘贴图片');
        return;
      }
    }
  });
})();
