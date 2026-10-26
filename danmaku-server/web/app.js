'use strict';
/* 弹幕回放台 · 前端逻辑（原生 JS，无依赖）
 * 区块: 选择场次(日期/场次/文件) → 弹幕校准(密度曲线+事件标注+一键对齐/微调) → 弹幕与显示
 * 播放: 多分段虚拟时间轴 getV/seekV, canvas 弹幕(预渲染精灵), 自绘控制条, 容器全屏
 */
(function () {
  function $(id) { return document.getElementById(id); }

  var videoEl = $('video'), canvas = $('danmaku-canvas'), ctx = canvas.getContext('2d');
  var elMatchTitle = $('match-title'), elLiveBadge = $('live-badge'), elClock = $('clock-now');
  var dmList = $('dm-list'), scBanner = $('sc-banner'), chatBox = $('chat-box');
  var selDate = $('sel-date'), selFile = $('sel-file'), selMatch = $('sel-match');
  var inpBv = $('inp-bv'), inpOffset = $('inp-offset'), btnApply = $('btn-apply');
  var btnDmToggle = $('btn-dm-toggle'), mask = $('player-mask');
  var btnFs = $('btn-fs'), selDmArea = $('sel-dm-area');
  var rngDmOpacity = $('rng-dm-opacity'), selDmSize = $('sel-dm-size');
  var btnPlay = $('btn-play'), prog = $('prog'), progInner = $('prog-inner');
  var progThumb = $('prog-thumb'), timeLabel = $('time-label');
  var btnMute = $('btn-mute'), rngVol = $('rng-vol'), ctrlBar = $('ctrl-bar');
  var btnRate = $('btn-rate'), speedBadge = $('speed-badge'), btnSkip = $('btn-skip');
  var btnAutoAdvance = $('btn-auto-advance');
  var alignCur = $('align-cur'), btnAlignNow = $('btn-align-now');
  var btnAutoAlign = $('btn-auto-align'), alignStatus = $('align-status');
  var btnDiscover = $('btn-discover');
  var ovCanvas = $('ov-canvas'), ovSummary = $('ov-summary'), ovLegend = $('ov-legend');
  var btnOvRefresh = $('btn-ov-refresh'), chkKwSpoiler = $('chk-kw-spoiler');
  var manualCalib = $('manual-calib');

  /* ---------------- 全局状态 ---------------- */
  var state = {
    applied: false,
    dmOn: true,
    anchorMs: 0,
    offsetSec: 0,
    danmaku: [],
    idx: 0,
    duration: 0,
    playUrl: '',
    title: '',
    curDate: '', curFile: '', curMatchTitle: '', curBv: '',
    segs: [], segLens: [], segStarts: [0], segIdx: 0,
    pendingSeekMs: null, pendingAutoplay: false,
    pageMarks: [],        // [{page,part,start_sec,duration}] 每局在虚拟轴上的起点
    rate: 1,              // 用户设定的播放速度(持久化)
    boostRate: 0,         // 长按方向键的临时加速倍率(0=未激活)
    lastTapKey: 0, lastTapAt: 0,  // 双击 ←/→ 判定
    pendingOverview: null, // 从总览点选某局后, 待加载完成时定位的目标
    autoAdvance: false    // 连播: 一场播完自动接下一场
  };
  var daysCache = [];
  var matchListCache = [];
  var triedFallback = false;
  var scTimer = 0;
  var pendingPlay = false;

  function baseMs() { return state.anchorMs + state.offsetSec * 1000; }
  function noop() {}
  function two(n) { return (n < 10 ? '0' : '') + n; }
  function fmtClock(d) { return two(d.getHours()) + ':' + two(d.getMinutes()) + ':' + two(d.getSeconds()); }
  function fmtCnt(sec) {
    var h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = Math.floor(sec % 60);
    return two(h) + ':' + two(m) + ':' + two(s);
  }
  function enc(v) { return encodeURIComponent(v); }

  /* ---------------- 遮罩 ---------------- */
  function showMask(text, btnLabel, onBtn) {
    mask.hidden = false;  // 显隐由 [hidden] 属性控制(与 style.css 配套)
    mask.innerHTML = '';
    var p = document.createElement('p');
    p.textContent = text;
    mask.appendChild(p);
    if (btnLabel) {
      var b = document.createElement('button');
      b.type = 'button'; b.className = 'mask-btn'; b.textContent = btnLabel;
      b.addEventListener('click', onBtn || noop);
      mask.appendChild(b);
    }
  }
  function hideMask() { mask.hidden = true; }
  function showError(msg) { showMask(msg, '重试', function () { hideMask(); doApply(); }); }

  /* ---------------- 数据加载 ---------------- */
  function getJson(url) {
    return fetch(url).then(function (r) {
      if (!r.ok) { throw new Error('HTTP ' + r.status); }
      return r.json();
    });
  }

  function loadDays() {
    getJson('/api/days').then(function (j) {
      if (!j || !j.ok || !Array.isArray(j.days)) { throw new Error('数据格式错误'); }
      daysCache = j.days;
      selDate.innerHTML = '';
      daysCache.forEach(function (d) {
        var o = document.createElement('option');
        // value 用**数据目录日**(取文件要用它), 显示用**比赛日**。
        // 凌晨录的内容属前一晚比赛: data/2026-10-25/ 里装的是 10-26 凌晨的比赛,
        // 用户认知与 Riot 赛程都按比赛日组织, 所以下拉框必须显示 10-26,
        // 否则会出现"播放台日期对不上"的困惑(看到 10-25 却放了 26 号的比赛)。
        o.value = d.date;
        if (d.is_test_group) {
          // 测试会话单独成组: 明确标注, 免得被当成正赛数据
          var cnt = (d.rooms || []).reduce(function (a, r) {
            return a + (r.files || []).length; }, 0);
          o.textContent = '测试数据（' + cnt + ' 个会话）';
          o.title = 'data/_test/ 下的试录会话，不参与正赛统计';
          selDate.appendChild(o);
          return;
        }
        var md = d.match_date && d.match_date !== d.date ? d.match_date : '';
        o.textContent = md ? (md + '（数据 ' + d.date + '）') : d.date;
        o.title = md ? ('比赛日 ' + md + '；弹幕/指纹数据目录 ' + d.date) : d.date;
        selDate.appendChild(o);
      });
      // 默认选第一个**非测试组**的日期(测试数据排在最后, 不该被默认选中)
      var firstReal = -1;
      for (var di = 0; di < daysCache.length; di++) {
        if (!daysCache[di].is_test_group) { firstReal = di; break; }
      }
      if (daysCache.length) {
        var pick = firstReal >= 0 ? daysCache[firstReal].date : daysCache[0].date;
        selDate.value = pick;
        loadFiles(pick);
        loadOverview(pick);
        showMask('选好场次后点击「开始播放」');
        mask.classList.add('plain');
      } else {
        showMask('数据目录中还没有弹幕录制文件。');
        mask.classList.add('plain');
      }
    }).catch(function () {
      showError('加载日期列表失败，请确认弹幕服务已启动。');
    });
  }

  // 会话下拉框: 显示「日期 时段 · 条数 · 房间号」。
  // 排序按**真实开始时间升序**(时间轴顺序最符合直觉), 测试会话垫底。
  // 房间号放**最后**: 绝大多数晚上只录一个房间, 放开头每行都重复、没有区分度,
  // 真正决定"选哪个"的是日期与时段, 应该让它们落在行首(眼睛先看到的位置)。
  function fmtDmCount(n) {
    n = n || 0;
    if (n >= 10000) {
      var w = n / 10000;
      return (w >= 100 ? Math.round(w) : w.toFixed(1).replace(/\.0$/, '')) + '万';
    }
    return String(n);
  }
  function pad2(n) { return n < 10 ? '0' + n : String(n); }
  function fmtHm(ms) {
    var d = new Date(ms);
    return pad2(d.getHours()) + ':' + pad2(d.getMinutes());
  }
  function fmtYmd(ms) {
    var d = new Date(ms);
    return d.getFullYear() + '-' + pad2(d.getMonth() + 1) + '-' + pad2(d.getDate());
  }

  function fileLabel(f) {
    var s = f.start_ms, e = f.end_ms;
    var room = '';
    var mm = /(\d+)$/.exec(f.name || '');
    if (mm) { room = mm[1]; }
    if (!s || !e) {
      // 只有指纹没有弹幕的会话(采集异常残留): 明确标出来, 别让人误选
      return (f.name || '') + '（无弹幕数据）';
    }
    var sameDay = fmtYmd(s) === fmtYmd(e);
    var span = sameDay
      ? fmtHm(s) + '–' + fmtHm(e)
      : fmtHm(s) + '–' + fmtYmd(e).slice(5) + ' ' + fmtHm(e);
    var durH = (e - s) / 3600000;
    var dur = durH >= 1 ? durH.toFixed(1) + 'h' : Math.max(1, Math.round((e - s) / 60000)) + '分';
    return fmtYmd(s) + ' ' + span + '（' + dur + '）· ' +
           fmtDmCount(f.dm_count) + '条 · ' + room +
           (f.is_test ? ' · 测试' : '');
  }

  function loadFiles(date) {
    selFile.innerHTML = '';
    var files = [];
    daysCache.forEach(function (d) {
      if (d.date !== date) { return; }
      (d.rooms || []).forEach(function (r) { (r.files || []).forEach(function (f) { files.push(f); }); });
    });
    // 排序: 有数据的正式会话按开始时间升序 → 测试会话 → 无数据的残留会话
    files.sort(function (a, b) {
      var ha = !a.start_ms ? 1 : 0, hb = !b.start_ms ? 1 : 0;
      if (ha !== hb) { return ha - hb; }          // 无数据的排最后
      var ta = a.is_test ? 1 : 0, tb = b.is_test ? 1 : 0;
      if (ta !== tb) { return ta - tb; }          // 测试会话次于正式会话
      if (a.start_ms && b.start_ms && a.start_ms !== b.start_ms) {
        return a.start_ms - b.start_ms;           // 按真实开始时间, 时间轴顺序
      }
      return (a.name || '') < (b.name || '') ? -1 : (a.name || '') > (b.name || '') ? 1 : 0;
    });
    files.forEach(function (f) {
      var o = document.createElement('option');
      o.value = f.name;
      o.textContent = fileLabel(f);
      // tooltip 保留完整会话名, 便于排查("这个选项到底指向哪个目录")
      o.title = f.name + (f.start_ms
        ? '\n开始 ' + new Date(f.start_ms).toLocaleString() +
          '\n结束 ' + new Date(f.end_ms).toLocaleString()
        : '');
      selFile.appendChild(o);
    });
    // 默认选中"数据量最大的正式会话"= 当晚正赛, 而不是排序后的第一个。
    // (按时间排序时第一个往往是白天的零散/补看会话, 默认播它会让用户以为选错了)
    var best = null;
    files.forEach(function (f) {
      if (f.is_test || !f.start_ms) { return; }
      if (!best || (f.dm_count || 0) > (best.dm_count || 0)) { best = f; }
    });
    if (!best) {
      best = files.filter(function (f) { return !f.is_test && f.start_ms; })[0] ||
             files.find(function (f) { return !!f.start_ms; }) || files[0];
    }
    if (best) { selFile.value = best.name; }
    loadMatchList(date);
  }

  function loadMatchList(date) {
    matchListCache = [];
    selMatch.innerHTML = '';
    var o0 = document.createElement('option');
    o0.value = ''; o0.textContent = '（选择比赛场次）';
    selMatch.appendChild(o0);
    // 场次列表按比赛日组织; 弹幕会话按比赛日目录归属(旧数据回落到起始日)。
    // 服务端 /api/matchlist 已处理回落: 当日列表为空且次日有数据时返回次日赛程。
    // 注意: 两个请求**必须串行**(先 matchlist 再 match), 否则谁后返回谁写 inpOffset.
    // 旧实现并发发出, /api/match 冷启动可达 60ms 而 matchlist 仅 7ms,
    // 于是场次 offset(如 G1=5272.5) 被文件级旧值(align 表里 G2=8958.5)覆盖,
    // 点「开始播放」就用错近 1 小时的 offset —— 弹幕整体错位。
    getJson('/api/matchlist?date=' + enc(date)).then(function (j) {
      if (j && j.ok && Array.isArray(j.matches) && j.matches.length) {
        matchListCache = j.matches;
        j.matches.forEach(function (m) {
          var o = document.createElement('option');
          o.value = m.title;
          // 标出对齐状态, 避免用户以为"选了就能直接播":
          //   缺 BV    -> 连回放地址都没有
          //   未对齐   -> 有 BV 但没算出 offset(多半是那场直播没转播, 音频匹配不上)
          //   已对齐   -> 显示分P 数
          var nPages = (m.pages && typeof m.pages === 'object')
            ? Object.keys(m.pages).length : 0;
          var tag = '';
          if (!m.bv) {
            tag = '（缺 BV）';
          } else if (m.align_failed) {
            tag = '（未对齐 · 直播可能未转播）';
          } else if (nPages > 1) {
            tag = '（已对齐 ' + nPages + ' 个分P）';
          } else if (nPages === 1) {
            tag = '（已对齐）';
          } else if (!m.offset_sec) {
            tag = '（未对齐）';
          }
          o.textContent = m.title + tag;
          o.title = m.align_error || '';
          selMatch.appendChild(o);
        });
        selMatch.selectedIndex = 1;
        applyMatchToInputs();
      }
      // 仅当没有选中任何场次时, 才用文件级记录回填 offset(作为"该文件上次看的进度"兜底)
      if (!selMatch.value) {
        return getJson('/api/match?date=' + enc(date) + '&file=' + enc(selFile.value || ''))
          .then(function (mj) {
            if (!mj || !mj.ok) { return; }
            inpBv.value = '';
            inpOffset.value = mj.offset_sec || 0;
          });
      }
    }).catch(noop);
  }

  function applyMatchToInputs() {
    var t = selMatch.value;
    var m = null;
    matchListCache.forEach(function (x) { if (x.title === t) { m = x; } });
    if (!m) { return; }
    inpBv.value = m.bv || '';
    inpOffset.value = m.offset_sec || 0;
  }

  // page=0 → 服务端把全部"分P"(第一局/第二局/第三局)拼成一条连续时间轴,
  // 前端沿用已有的多分段虚拟时间轴顺序续播, 这样整场回放才能完整播放。
  function fetchPlay(bv, page) {
    var q = '/api/playurl?bv=' + enc(bv) + (page ? ('&page=' + enc(page)) : '');
    return getJson(q).then(function (j) {
      if (!j || !j.ok || !j.url) { throw new Error(j && j.error ? j.error : '获取回放播放地址失败'); }
      return j;
    });
  }

  /* ---------------- apply：加载弹幕/匹配/播放地址并开播 ---------------- */
  function doApply() {
    var date = selDate.value, file = selFile.value;
    if (!date || !file) { showMask('请先选择日期与弹幕文件。'); return; }
    var matchTitle = selMatch.value || '';
    var matchBv = '';
    if (matchTitle) {
      matchListCache.forEach(function (m) { if (m.title === matchTitle && m.bv) { matchBv = m.bv; } });
    }
    // 场次选中且有 BV 时, 场次优先(输入框的旧值可能是上次会话残留, 会造成弹幕↔视频错位)
    var customBv = (inpBv.value || '').trim();
    var bv = matchBv || customBv;
    if (!bv) {
      showMask('该场次没有配置回放 BV：在「自定义回放源」里填一个，或先看纯弹幕文件确认数据。');
      mask.classList.add('plain');
      return;
    }
    var offset = parseFloat(inpOffset.value);
    if (!isFinite(offset)) { offset = 0; }

    showMask('正在加载弹幕与回放…');
    mask.classList.remove('plain');
    triedFallback = false;

    var pDm = fetch('/api/danmaku?date=' + enc(date) + '&file=' + enc(file)).then(function (r) {
      if (!r.ok) { throw new Error('弹幕数据 HTTP ' + r.status); }
      return r.text();
    });
    var pMatch = getJson('/api/match?date=' + enc(date) + '&file=' + enc(file)).then(function (j) {
      if (!j || !j.ok || typeof j.anchor_ms !== 'number') { throw new Error('读取对齐信息失败'); }
      return j;
    });
    var pPlay = fetchPlay(bv);

    Promise.all([pDm, pMatch, pPlay]).then(function (res) {
      setup(date, file, res[0], res[1], res[2], bv, offset, matchTitle);
    }).catch(function (e) {
      showError('加载失败：' + (e && e.message ? e.message : '网络错误'));
    });
  }

  function parseDanmaku(text) {
    var out = [];
    String(text).split('\n').forEach(function (line) {
      line = line.trim();
      if (!line) { return; }
      var o;
      try { o = JSON.parse(line); } catch (e) { return; }
      if (!o || typeof o.ts !== 'number' || typeof o.t !== 'string') { return; }
      out.push({ ts: o.ts, un: typeof o.un === 'string' ? o.un : '',
        t: o.t, ty: o.ty === 'sc' ? 'sc' : 'dm',
        price: typeof o.price === 'number' ? o.price : 0, rel: 0 });
    });
    out.sort(function (a, b) { return a.ts - b.ts; });
    return out;
  }

  function setup(date, file, dmText, match, play, bv, offset, matchTitle) {
    state.anchorMs = match.anchor_ms; state.offsetSec = offset; state.playUrl = play.url;
    state.duration = typeof play.duration === 'number' ? play.duration : 0;
    // 标题优先级: 用户选中的场次(matchTitle) > 服务端 align 记录标题 > 回放视频标题。
    // 绝不能用直播间当时的标题(meta.room_title, 如"【预告】27号1点CFO vs T1"),
    // 那是采集时刻的房间名, 与正在看的这场比赛无关 —— 这正是"左上角比赛信息与选中场次不一致"的成因。
    state.title = matchTitle || match.title || play.title || '';
    state.curDate = date; state.curFile = file; state.curMatchTitle = matchTitle;
    state.curBv = bv;
    state.pageMarks = (play.page_marks && play.page_marks.length) ? play.page_marks.slice() : [];
    // 分段信息
    state.segs = (play.urls && play.urls.length) ? play.urls.slice() : [play.url];
    state.segLens = (play.seg_lengths && play.seg_lengths.length === state.segs.length)
      ? play.seg_lengths.slice() : [state.duration || 0];
    state.segStarts = [0];
    for (var s = 1; s < state.segs.length; s++) {
      state.segStarts.push(state.segStarts[s - 1] + state.segLens[s - 1]);
    }
    state.segIdx = 0;
    // 左上角标题: 场次名 + (多局时)当前局标签, 例如 "G4 GEN vs LYON · 第2局 第二局"。
    // 局标签随播放位置更新(见 syncTitleForPlayback), 保证"标题 == 正在看的内容"。
    state.baseTitle = state.title;
    elLiveBadge.textContent = '回放中';

    var list = parseDanmaku(dmText), base = baseMs();
    for (var i = 0; i < list.length; i++) { list[i].rel = list[i].ts - base; }
    state.danmaku = list;
    state.applied = true; active.length = 0; resetTracks();
    dmList.innerHTML = ''; hideSc();
    buildAlignChart();
    syncTitleForPlayback();

    fetch('/api/match', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ date: date, file: file, bv: bv, offset_sec: offset,
        title: matchTitle || undefined, list_title: matchTitle || undefined }) }).catch(noop);

    videoEl.muted = false;
    triedFallback = false;
    videoEl.src = state.segs[0];
    videoEl.load();
    videoEl.currentTime = 0;
    hideMask();
    startPlay();

    // 从总览点选进来的定位:
    //  - block 有值: 定位到那一局的开头
    //  - block 为 null: 连播切过来, 从头开始(不定位)
    var po = state.pendingOverview;
    if (po && po.bv === bv) {
      state.pendingOverview = null;
      var target = null, targetIdx = -1;
      if (po.block) {
        for (var k = 0; k < state.pageMarks.length; k++) {
          var pm = state.pageMarks[k];
          if (pm.page === po.block.p) { target = pm; targetIdx = k; break; }
        }
      }
      if (target) {
        seekV(clampSeek(target.start_sec));
      } else if (po.block) {
        // 分P信息没取全时, 退化为按时间差定位: 该块相对回放起点的秒
        var rel = (po.block.startMs - (state.anchorMs + po.offsetSec * 1000)) / 1000;
        seekV(clampSeek(rel));
      }
    }
  }

  /* ---------------- 播放控制 ---------------- */
  // 依据当前虚拟播放位置, 找到落在哪一局, 更新左上角标题与徽标
  function currentPageMark() {
    if (!state.pageMarks || !state.pageMarks.length) { return null; }
    var v = getV(), hit = null;
    for (var i = 0; i < state.pageMarks.length; i++) {
      var pm = state.pageMarks[i];
      if (v >= pm.start_sec - 0.5) { hit = pm; } else { break; }
    }
    return hit;
  }
  // 下一段(下一局)的起点; 已是最后一段返回 null
  function nextPageMark() {
    if (!state.pageMarks || !state.pageMarks.length) { return null; }
    var cur = currentPageMark();
    if (!cur) { return state.pageMarks[0]; }
    var i = state.pageMarks.indexOf(cur);
    return (i >= 0 && i + 1 < state.pageMarks.length) ? state.pageMarks[i + 1] : null;
  }
  function syncTitleForPlayback() {
    if (!state.applied) { return; }
    var pm = currentPageMark();
    var t = state.baseTitle || state.title || '';
    if (pm && pm.part) { t += ' · ' + pm.part; }
    if (elMatchTitle.textContent !== t) { elMatchTitle.textContent = t; }
    elMatchTitle.title = t;
    // 有下一局时才显示"跳过本局末尾"按钮(B站回放的分P末尾常带采访/评论, 可一键跳过)
    if (btnSkip) {
      var nx = nextPageMark();
      btnSkip.hidden = !nx;
      if (nx) { btnSkip.title = '跳到下一局：' + (nx.part || ('第' + nx.page + 'P')); }
    }
  }

  function startPlay() {
    pendingPlay = false;
    videoEl.play().then(noop).catch(function () {
      pendingPlay = true;
      showMask('浏览器阻止了自动播放，请点击开始。', '开始播放', function () {
        hideMask();
        videoEl.play().then(function () { pendingPlay = false; })
          .catch(function () { showError('播放失败，请重试。'); });
      });
    });
  }

  function clampSeek(sec) {
    if (sec < 0) { sec = 0; }
    if (state.duration > 1) { sec = Math.min(sec, state.duration - 1); }
    return sec;
  }

  /* ---- 多分段虚拟时间轴 ---- */
  function getV() { return (state.segStarts[state.segIdx] || 0) + videoEl.currentTime; }
  function switchSeg(i, seekSec, autoplay) {
    state.segIdx = i;
    triedFallback = false;
    state.pendingSeekMs = seekSec;
    state.pendingAutoplay = !!autoplay;
    videoEl.src = state.segs[i];
    videoEl.load();
  }
  function seekV(sec) {
    if (!state.segs.length) { videoEl.currentTime = sec; return; }
    var i = state.segs.length - 1;
    for (var k = 0; k < state.segs.length; k++) {
      if (sec < state.segStarts[k] + state.segLens[k]) { i = k; break; }
    }
    var local = Math.max(0, sec - state.segStarts[i]);
    if (i !== state.segIdx) {
      switchSeg(i, local, true);
    } else {
      videoEl.currentTime = local;
    }
  }
  videoEl.addEventListener('loadedmetadata', function () {
    if (state.pendingSeekMs != null) {
      try { videoEl.currentTime = state.pendingSeekMs; } catch (e) { /* 元数据未就绪 */ }
      state.pendingSeekMs = null;
    }
    if (state.pendingAutoplay) {
      state.pendingAutoplay = false;
      videoEl.play().catch(noop);
    }
  });
  videoEl.addEventListener('ended', function () {
    if (!state.applied) { return; }
    if (state.segIdx < state.segs.length - 1) {
      switchSeg(state.segIdx + 1, 0, true);
      return;
    }
    // 整场(全部分P)播完 -> 若开了"连播", 自动接下一场
    if (state.autoAdvance) { advanceToNextMatch(); }
  });

  /* ---- 连播: 自动接下一场 ---- */
  // 需求: "连续播放, 自动播放下一场"。以总览的轨道顺序(即比赛时间顺序)为准,
  // 播完当前场次自动切到时间上的下一场, 并从头开始放。
  // 注意: 总览数据未就绪(还在加载/加载失败)时不能直接放弃 —— 否则连播"开着却不工作"
  // 且没有任何提示。这里回退到下拉框里的场次顺序(它同样是按赛程排的)。
  function nextMatchAfter(bv) {
    var i, cur = -1;
    if (ov.loaded && ov.tracks.length) {
      for (i = 0; i < ov.tracks.length; i++) {
        if (ov.tracks[i].bv && ov.tracks[i].bv === bv) { cur = i; break; }
      }
      if (cur < 0) { return null; }
      for (var k = cur + 1; k < ov.tracks.length; k++) {
        if (ov.tracks[k].bv) { return ov.tracks[k]; }
      }
      return null;                  // 已是最后一场
    }
    // 回退: 按下拉框场次顺序找当前场的下一项
    var opts = selMatch ? selMatch.options : [];
    cur = -1;
    for (i = 0; i < opts.length; i++) {
      var m = null;
      for (var q = 0; q < matchListCache.length; q++) {
        if (matchListCache[q].title === opts[i].value) { m = matchListCache[q]; break; }
      }
      if (m && m.bv === bv) { cur = i; break; }
    }
    if (cur < 0) { return null; }
    for (var j = cur + 1; j < opts.length; j++) {
      for (var w = 0; w < matchListCache.length; w++) {
        if (matchListCache[w].title === opts[j].value && matchListCache[w].bv) {
          var mm = matchListCache[w];
          return { idx: -1, title: mm.title, bv: mm.bv,
                   offsetSec: mm.offset_sec || 0, blocks: [], color: '#4c8dff' };
        }
      }
    }
    return null;
  }

  function advanceToNextMatch() {
    var nx = nextMatchAfter(state.curBv);
    if (!nx) {
      // 注意: 这里**不能**把 autoAdvance 关掉。用户只是把当晚最后一场看完了,
      // 并不代表他不想连播; 若此时写回 false, 第二天连播会静默失效(设置是持久化的),
      // 表现为"连播按钮明明开着却不再自动切" —— 极难排查。
      showToast('本晚最后一场已播完');
      return;
    }
    showToast('自动播放下一场：' + nx.title);
    // 连播要**从头**放整场(含开头的赛前/BP 等), 而不是跳到第一个对局块 ——
    // blocks 已被 OV_SKIP_PART_RE 过滤掉非对局段, 用 blocks[0] 会跳过开场内容。
    // 传 null 表示"不定位, 从 0 开始"。
    playFromOverview(nx, null, { keepAutoAdvance: true });
  }

  function syncAutoAdvanceBtn() {
    if (!btnAutoAdvance) { return; }
    btnAutoAdvance.textContent = '连播: ' + (state.autoAdvance ? '开' : '关');
    btnAutoAdvance.classList.toggle('off', !state.autoAdvance);
    btnAutoAdvance.title = state.autoAdvance
      ? '当前：一场播完自动接下一场（点击关闭）'
      : '当前：播完停住（点击开启自动连播）';
  }
  if (btnAutoAdvance) {
    btnAutoAdvance.addEventListener('click', function () {
      state.autoAdvance = !state.autoAdvance;
      saveSettings();
      syncAutoAdvanceBtn();
      showToast('连播已' + (state.autoAdvance ? '开启' : '关闭'));
    });
  }

  setInterval(function () {
    elClock.textContent = fmtClock(new Date());
    if (state.applied) { drawOverview(); }   // 播放位置线画在总览主轴上
  }, 1000);

  /* ---- 播放速度: 用户档位 + 长按临时加速 ---- */
  var RATE_STEPS = [0.5, 0.75, 1, 1.25, 1.5, 2, 3];

  function effectiveRate() {
    return state.boostRate > 0 ? state.boostRate : state.rate;
  }
  // 把速率真正作用到 <video>: playbackRate 是浏览器原生能力, 音画同步由浏览器保证,
  // 且弹幕引擎用的是 video.currentTime(见 getV), 所以变倍速时弹幕天然跟着视频走,
  // 不需要另做时间补偿 —— 这是复用原生倍速而非自建时钟的关键原因。
  function applyRate() {
    var r = effectiveRate();
    try { videoEl.playbackRate = r; } catch (e) { /* 某些容器不支持极值, 忽略 */ }
    var boosted = state.boostRate > 0;
    btnRate.textContent = (state.rate === 1 ? '1.0' : String(state.rate)) + '×';
    btnRate.classList.toggle('off', state.rate !== 1);
    // 常驻倍速徽标只在下述情况显示: 用户设了非 1 倍速, 或正在长按临时加速
    if (boosted) {
      speedBadge.textContent = r + '× 加速中';
      speedBadge.hidden = false;
    } else if (state.rate !== 1) {
      speedBadge.textContent = r + '×';
      speedBadge.hidden = false;
    } else {
      speedBadge.hidden = true;
    }
  }
  function setRate(r) {
    state.rate = Math.min(4, Math.max(0.25, +r || 1));
    saveSettings();
    applyRate();
  }
  function stepRate(dir) {
    var cur = state.rate, i = RATE_STEPS.indexOf(cur);
    if (i < 0) {                       // 非标准档位: 就近吸附
      i = 0;
      for (var k = 0; k < RATE_STEPS.length; k++) {
        if (RATE_STEPS[k] <= cur) { i = k; }
      }
    }
    i = Math.min(RATE_STEPS.length - 1, Math.max(0, i + dir));
    setRate(RATE_STEPS[i]);
  }
  btnRate.addEventListener('click', function () {
    // 循环切换常用档位: 1 → 1.25 → 1.5 → 2 → 0.75 → 1
    var cycle = [1, 1.25, 1.5, 2, 0.75];
    var i = cycle.indexOf(state.rate);
    setRate(cycle[(i + 1) % cycle.length]);
  });

  // 跳过当前分P剩余部分(局间采访/赛前评论等), 直接进入下一局
  if (btnSkip) {
    btnSkip.addEventListener('click', function () {
      var nx = nextPageMark();
      if (!nx || !state.applied) { return; }
      seekV(clampSeek(nx.start_sec + 0.2));
      if (videoEl.paused) { videoEl.play().catch(noop); }
    });
  }

  /* ---- 快进/快退: 方向键；长按 → 2 倍速持续播放 ---- */
  var LONGPRESS_MS = 350;      // 按下超过该时长视为"长按加速", 松手不再触发单次跳转
  var SEEK_STEP = 5;           // 单击 ←/→ 跳 5 秒
  var FAST_STEP = 15;          // Shift+←/→ 跳 15 秒(与主流播放器一致)
  var holdTimer = 0, holdKey = '';
  var wasPlayingBeforeHold = false;

  function commitSeek(deltaSec) {
    if (!state.applied) { return; }
    seekV(clampSeek(getV() + deltaSec));
  }

  function startHold(key) {
    holdKey = key;
    holdTimer = setTimeout(function () {
      if (!state.applied) { return; }
      // 长按: 进入 2 倍速。若原本已 ≥2 倍速则不再降速, 避免"越按越慢"的反直觉
      state.boostRate = Math.max(2, state.rate);
      applyRate();
      wasPlayingBeforeHold = !videoEl.paused;
      if (videoEl.paused) { videoEl.play().catch(noop); }
    }, LONGPRESS_MS);
  }

  function endHold() {
    clearTimeout(holdTimer);
    holdTimer = 0;
    if (state.boostRate) {          // 退出临时加速
      state.boostRate = 0;
      applyRate();
      // 长按前是暂停状态, 松手后恢复暂停, 不擅自开始播放
      if (!wasPlayingBeforeHold && !videoEl.paused) { videoEl.pause(); }
    }
    holdKey = '';
  }

  function isTypingTarget(el) {
    if (!el) { return false; }
    var t = (el.tagName || '').toLowerCase();
    return t === 'input' || t === 'textarea' || t === 'select' || el.isContentEditable;
  }

  document.addEventListener('keydown', function (e) {
    if (isTypingTarget(e.target)) { return; }   // 正在输入框里打字, 不劫持按键
    var k = e.key;
    if (k === ' ' || k === 'Spacebar') {          // 空格: 播放/暂停
      e.preventDefault();
      if (state.applied) { btnPlay.click(); }
      return;
    }
    if (k === 'ArrowLeft' || k === 'ArrowRight') {
      e.preventDefault();
      if (!state.applied) { return; }
      // 系统的按键自动重复只用来"维持长按", 不跳转也不重启计时器。
      // 保证行为: 短按=跳 5s(在 keyup 里提交), 长按=切换 2 倍速匀速播放。
      if (e.repeat) { return; }
      if (holdKey && holdKey !== k) { endHold(); }
      startHold(k);
      return;
    }
    if (k === 'ArrowUp' || k === 'ArrowDown') {     // 音量
      e.preventDefault();
      var v = Math.min(1, Math.max(0, videoEl.volume + (k === 'ArrowUp' ? 0.05 : -0.05)));
      videoEl.volume = v; videoEl.muted = false;
      rngVol.value = String(Math.round(v * 100));
      btnMute.textContent = '音量';
      return;
    }
    if (k === 'f' || k === 'F') {                   // 全屏
      e.preventDefault(); btnFs.click(); return;
    }
    if (k === 'm' || k === 'M') {                   // 静音
      e.preventDefault(); btnMute.click(); return;
    }
    if (k === 'd' || k === 'D') {                   // 弹幕开关
      e.preventDefault(); setDmOn(!state.dmOn); return;
    }
    if (k === '>' || k === '.') { stepRate(1); return; }
    if (k === '<' || k === ',') { stepRate(-1); return; }
  });

  document.addEventListener('keyup', function (e) {
    if (isTypingTarget(e.target)) { return; }
    if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') { return; }
    var wasBoost = state.boostRate > 0;
    var dir = (e.key === 'ArrowRight') ? 1 : -1;
    endHold();
    if (wasBoost) { return; }        // 长按加速已生效: 松手只退出加速, 不再跳转
    if (!state.applied) { return; }
    commitSeek(dir * (e.shiftKey ? FAST_STEP : SEEK_STEP));
  });
  // 失焦时清理, 防止"按下 Alt+Tab"导致加速状态卡住
  window.addEventListener('blur', function () { endHold(); });


  /* ---------------- 轻量提示 toast ---------------- */
  var toastEl = null, toastTimer = 0;
  function showToast(msg, ms) {
    if (!toastEl) {
      toastEl = document.createElement('div');
      toastEl.id = 'toast';
      document.body.appendChild(toastEl);
    }
    toastEl.textContent = msg;
    toastEl.classList.add('show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { toastEl.classList.remove('show'); }, ms || 2600);
  }

  /* ---------------- 弹幕引擎 ---------------- */
  var settings = { area: 1, opacity: 0.9, size: 26, density: 1 };
  var FONT = 'bold 26px "Microsoft YaHei", "PingFang SC", sans-serif';
  var TRACK_H = 34;
  var MAX_ONSCREEN = 160;

  function applySettings() {
    try {
      var saved = JSON.parse(localStorage.getItem('dm-player-settings') || '{}');
      if (saved && typeof saved === 'object') {
        if ([1, 0.75, 0.5].indexOf(saved.area) >= 0) { settings.area = saved.area; }
        if ([1, 0.5, 0.25, 0.1].indexOf(+saved.density) >= 0) { settings.density = +saved.density; }
        if (isFinite(saved.opacity)) { settings.opacity = Math.min(1, Math.max(0.2, +saved.opacity)); }
        if ([20, 26, 34].indexOf(+saved.size) >= 0) { settings.size = +saved.size; }
        if (isFinite(saved.rate) && saved.rate >= 0.25 && saved.rate <= 4) { state.rate = +saved.rate; }
        if (typeof saved.autoAdvance === 'boolean') { state.autoAdvance = saved.autoAdvance; }
      }
    } catch (e) { /* 忽略坏数据 */ }
    reflectSettings();
    applyRate();
    syncAutoAdvanceBtn();
  }
  function saveSettings() {
    try {
      localStorage.setItem('dm-player-settings',
        JSON.stringify({ area: settings.area, opacity: settings.opacity,
          size: settings.size, density: settings.density, rate: state.rate,
          autoAdvance: state.autoAdvance }));
    } catch (e) {}
  }
  function reflectSettings() {
    FONT = 'bold ' + settings.size + 'px "Microsoft YaHei", "PingFang SC", sans-serif';
    TRACK_H = settings.size + 8;
    canvas.style.opacity = String(settings.opacity);
    selDmArea.value = String(settings.area);
    var dsel = $('sel-dm-density');
    if (dsel) { dsel.value = String(settings.density); }
    rngDmOpacity.value = String(Math.round(settings.opacity * 100));
    selDmSize.value = String(settings.size);
    active.length = 0;
    resetTracks();
  }

  var W = 0, H = 0, dpr = 1;
  var tracks = [];
  var active = [];

  function resetTracks() {
    var n = Math.max(1, Math.floor((H * settings.area) / TRACK_H));
    tracks = [];
    for (var i = 0; i < n; i++) { tracks.push(null); }
  }

  function fitCanvas() {
    var w = canvas.clientWidth, h = canvas.clientHeight;
    if (w <= 0 || h <= 0) { return; }
    dpr = Math.min(window.devicePixelRatio || 1, 1.5);
    var pw = Math.round(w * dpr), ph = Math.round(h * dpr);
    if (w !== W || h !== H || canvas.width !== pw || canvas.height !== ph) {
      W = w; H = h; canvas.width = pw; canvas.height = ph;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      resetTracks();
    }
  }

  function upperBound(arr, v) {
    var lo = 0, hi = arr.length;
    while (lo < hi) {
      var mid = (lo + hi) >> 1;
      if (arr[mid].rel <= v) { lo = mid + 1; } else { hi = mid; }
    }
    return lo;
  }

  function pump() {
    var nowMs = getV() * 1000;
    var n = upperBound(state.danmaku, nowMs);
    if (n < state.idx) {
      active.length = 0;
      resetTracks();
      state.idx = n;
    }
    var budget = 60;
    while (state.idx < n && budget-- > 0) { emitDm(state.danmaku[state.idx++]); }
    if (state.idx < n) { state.idx = n; }
  }

  function makeSprite(text) {
    ctx.font = FONT;
    var tw = Math.ceil(ctx.measureText(text).width);
    var c = document.createElement('canvas');
    var scale = dpr;
    c.width = Math.ceil((tw + 8) * scale);
    c.height = Math.ceil(TRACK_H * scale);
    var g = c.getContext('2d');
    g.scale(scale, scale);
    g.font = FONT;
    g.textBaseline = 'middle';
    g.lineJoin = 'round';
    g.lineWidth = 4;
    g.strokeStyle = '#000';
    g.strokeText(text, 4, TRACK_H / 2);
    g.fillStyle = '#fff';
    g.fillText(text, 4, TRACK_H / 2);
    return { c: c, dw: tw + 8, dh: TRACK_H };
  }

  function emitDm(d) {
    // 数量抽稀: 按文本哈希判定, 同一条弹幕在任意时间点/来回拖动下判定一致(不闪变)
    if (settings.density < 1) {
      var h = 0, s = d.t;
      for (var k = 0; k < s.length; k++) { h = (h * 31 + s.charCodeAt(k)) >>> 0; }
      if ((h % 100) / 100 >= settings.density) { return; }
    }
    appendChat(d, false);
    if (d.ty === 'sc') { showSc(d); return; }
    if (!state.dmOn) { return; }
    var sp = makeSprite(d.t);
    var it = { sp: sp, w: sp.dw, x: W + 4, y: 0, v: Math.max(140, W / 9) };
    var slot = -1;
    for (var i = 0; i < tracks.length; i++) {
      var last = tracks[i];
      if (!last || last.x + last.w < W - 24) { slot = i; break; }
    }
    if (slot < 0) { return; }
    tracks[slot] = it;
    it.y = slot * TRACK_H + TRACK_H / 2 + 2;
    if (active.length >= MAX_ONSCREEN) { return; }
    active.push(it);
  }

  var lastFrame = 0;
  function loop(ts) {
    requestAnimationFrame(loop);
    var dt = (ts - lastFrame) / 1000;
    lastFrame = ts;
    if (!isFinite(dt) || dt < 0 || dt > 0.05) { dt = Math.min(Math.max(dt, 0), 0.05); }
    fitCanvas();
    if (!state.applied || W === 0) { return; }
    pump();
    if (!state.dmOn) { return; }
    ctx.clearRect(0, 0, W, H);
    // 倍速播放时弹幕入场频率随视频时间同步变快(见 pump 用 getV), 因此屏幕上的
    // 移动速度也必须乘以播放倍率, 否则 2 倍速下弹幕会挤爆屏幕。dt 是真实秒,
    // 乘 effectiveRate() 后即"视频时间流逝速度"。
    var moveDt = dt * effectiveRate();
    for (var i = active.length - 1; i >= 0; i--) {
      var it = active[i];
      it.x -= it.v * moveDt;
      if (it.x + it.w < -20) { active.splice(i, 1); continue; }
      ctx.drawImage(it.sp.c, it.x, it.y - it.sp.dh / 2, it.sp.dw, it.sp.dh);
    }
  }

  /* ---------------- 当晚总览（剪辑软件式多轨时间轴） ---------------- */
  // 设计: 弹幕是"一条连续时间轴"(整晚录制), 官方回放是"按局切分的分段"。
  // 校准给出每场回放 t=0 对应的真实墙钟(offset), 于是可以把每场各分P
  // 摆到弹幕主轴上的确定位置, 形成多轨总览 —— 一眼看出哪局落在哪一刻、
  // 场次间空档多长、是否缺 BV。
  // 坐标统一用"绝对墙钟毫秒"(与后端 anchor_ts/offset 同一套), 避免自造原点。
  var ov = {
    loaded: false,
    date: '', matchDate: '', file: '',
    t0: 0, t1: 0,                    // 主轴起止(绝对 ms)
    bins: [], binMs: 30000, peak: 1,
    tracks: [],                      // [{idx,title,bv,offsetSec,auto,startMs,blocks:[...]}]
    order: [],                       // 已绘制块(命中测试用)
    layout: [], laneCount: 1,        // 轨道打包结果
    markers: [],                     // 事件标记(一血/大龙/水晶/GG), 防剧透可关
  };
  var OV_COLORS = ['#4c8dff', '#3fb96f', '#ffb020', '#a35cff', '#ff7a59', '#20c4d9', '#e05c9a'];

  function ovX(ms) {
    var w = ovCanvas.clientWidth;
    if (w <= 0) { return 0; }
    return (ms - ov.t0) / Math.max(1, ov.t1 - ov.t0) * (w - 20) + 10;
  }
  function ovMs(x) {
    var w = ovCanvas.clientWidth;
    return ov.t0 + (x - 10) / Math.max(1, w - 20) * (ov.t1 - ov.t0);
  }

  // 请求代际: 快速连续切日期时, 旧请求可能后返回并覆盖新数据。
  // 每次 loadOverview 递增, 回调里比对代际, 不是最新的一律丢弃。
  var ovSeq = 0;

  function loadOverview(date) {
    var mySeq = ++ovSeq;
    ov.loaded = false;
    ov.order = [];
    ov.layout = [];
    ov.laneCount = 1;
    ov.tracks = [];
    ov.bins = [];
    getJson('/api/overview?date=' + enc(date)).then(function (j) {
      if (mySeq !== ovSeq) { return; }             // 已有更新的请求, 丢弃本次结果
      if (!j || !j.ok) {
        ovSummary.textContent = '总览不可用: ' + ((j && j.error) || '加载失败');
        ovLegend.innerHTML = '';
        drawOverview();
        return;
      }
      ov.loaded = true;
      ov.date = j.date; ov.matchDate = j.match_date; ov.file = j.file;
      ov.t0 = j.anchor_ts; ov.t1 = j.end_ts;
      ov.bins = j.density.bins || [];
      ov.binMs = (j.density.bin_sec || 30) * 1000;
      ov.peak = j.density.peak || 1;
      // offset 语义: 回放内时间 t 对应墙钟 = anchor + offset + t。
      // 而主轴 0 点就是 anchor(首条弹幕墙钟), 所以该场在主轴上的起点 = offset*1000。
      ov.tracks = (j.tracks || []).map(function (t, i) {
        return {
          idx: t.idx, title: t.title, bv: t.bv,
          offsetSec: t.offset_sec, auto: t.auto,
          parts: t.parts || [],          // 逐分P 对齐结果(优先于按序号串联)
          color: OV_COLORS[i % OV_COLORS.length],
          startMs: ov.t0 + t.offset_sec * 1000,
          blocks: [],
        };
      });
      updateOvSummary(j);
      drawOverview();
      fillOverviewBlocks(mySeq);   // 逐场补分P结构(串行, 避免一次打太多 B 站接口)
    }).catch(function () {
      if (mySeq !== ovSeq) { return; }
      ovSummary.textContent = '总览加载失败';
      ovLegend.innerHTML = '';
      drawOverview();
    });
  }

  function updateOvSummary(j) {
    var parts = [];
    parts.push('比赛日 ' + (j.match_date || '?'));
    parts.push('主轴 ' + fmtCnt(j.span_sec));
    parts.push('弹幕 ' + (j.dm_count || 0).toLocaleString() + ' 条');
    if (j.afp_duration_sec) { parts.push('指纹 ' + Math.round(j.afp_duration_sec / 60) + ' 分'); }
    parts.push((j.tracks || []).length + ' 场');
    ovSummary.textContent = parts.join('  ·  ');
    buildOvLegend(j.tracks || []);
  }

  function buildOvLegend(tracks) {
    var html = '<span><i class="ov-dot" style="background:rgba(76,141,255,.75)"></i>弹幕密度</span>';
    tracks.forEach(function (t, i) {
      var c = OV_COLORS[i % OV_COLORS.length];
      // 未对齐的场次不分配色块(offset=0 会被误画到时间轴起点, 还会和别的块重叠),
      // 在图例里明确标出来, 让用户知道"这场没算出来"而不是"画丢了"。
      var nParts = (t.parts || []).length;
      var unaligned = !nParts && !t.offset_sec;
      if (unaligned) {
        html += '<span style="opacity:.5"><i style="background:#4b5563"></i>' +
                t.title + '<b style="color:#ff7a59"> 未对齐</b></span>';
        return;
      }
      html += '<span><i style="background:' + c + '"></i>' + t.title +
              (t.bv ? '' : '<b style="color:#ff7a59"> 缺BV</b>') + '</span>';
    });
    ovLegend.innerHTML = html;
  }

  // 分P里"赛前评论/赛后采访"等非对局内容对"总览看比赛节奏"是噪声:
  // 它们会把色块拉长, 挤压真正的对局块, 还会制造假重叠。默认只画对局块。
  var OV_SKIP_PART_RE = /(赛前|赛后|评论|采访|花絮|预告|广告|开幕式|颁奖)/;

  // 逐场生成"分P 块"。
  // 优先用服务端给出的**每分P 独立 offset**(tr.parts)——这是音频指纹逐分P 对齐的结果,
  // 每段都锚在自己的真实时刻上, 因此段与段之间的留白就是真实的局间休息。
  // 旧做法是按 P 序号把 page_marks 累加串联, 那只在"各分P 首尾无缝相接"时才对,
  // 而直播侧两局之间明明有休息, 累加会把这部分时间吃掉, 越往后偏得越多。
  // 没有 parts 数据时(未对齐过)才退回旧串联方式。
  function blocksFromParts(tr) {
    // 注意字段名是驼峰 offsetSec(前端 state 用驼峰), 不是接口里的 offset_sec。
    // 写错会得到 undefined, 参与乘法后整块变 NaN, 表现为"块算出来了但画不出来"。
    var baseOff = (typeof tr.offsetSec === 'number') ? tr.offsetSec : 0;
    return (tr.parts || []).map(function (p) {
      return {
        p: p.page,
        part: p.part,
        // offset_sec = 该分P 在回放内的 0 秒所对应的真实墙钟; 相对本场第一分P 的偏移
        // 就是它在主轴上的实际位置(各分P 独立对齐, 不按 P 序号串联)。
        startMs: tr.startMs + ((p.offset_sec || 0) - baseOff) * 1000,
        durMs: (p.duration || 0) * 1000,
        conf: p.confidence,
        // 赛前评论/赛后采访这类配件只占几分钟且不在对局中, 默认不画块
        // (保留数据, 只在图上跳过), 避免把"比赛节奏"切碎。
        accessory: OV_SKIP_PART_RE.test(String(p.part || '')),
      };
    }).filter(function (b) { return b.durMs > 0; });
  }

  function fillOverviewBlocks(seq) {
    // 块的位置**只能**来自逐分P 对齐结果(tr.parts), 没有就别画。
    // 旧实现还有一条"按 P 序号从 tr.startMs 累加 page_marks"的兜底路径, 那条路径在
    // offset=0(未对齐)时会把所有分P 堆到时间轴起点, 画出一排从 00:00 开始的长条,
    // 既不是真实位置也点不对 —— 已经删掉, 宁可什么都不画。
    ov.tracks.forEach(function (tr) {
      tr.blocks = blocksFromParts(tr);
    });
    layoutOverviewLanes();
    drawOverview();
  }

  // 轨道打包(按时间排布, 不重叠的块共用同一条轨道)。
  // 为什么需要: 一晚上的比赛在时间上**是顺序的**(G1 打完才打 G2), 所以正常情况下
  // 所有分P块能全部落在同一条轨道上 —— 这才是"把分段放进弹幕主轴"该有的样子。
  // 只有校准异常导致时间重叠时, 才会自动多开一条轨道错开显示。
  // 所有分P 块共享**同一条**时间轴(它们本来就是同一条直播时间线上的不同切片)。
  // 旧实现为了"不重叠"会自动开多条轨道, 但实测相邻分P 之间常有约 1 秒的重叠
  // (对齐误差), 于是 4 个块被拆成 4 条轴 —— 视觉上像 4 条独立的时间线, 很乱。
  // 现在统一放一条轴, 用**颜色区分场次**、用**深浅区分分P**, 重叠时后者压前者即可。
  function layoutOverviewLanes() {
    var all = [];
    ov.tracks.forEach(function (tr, ti) {
      tr.blocks.forEach(function (b) {
        all.push({ ti: ti, tr: tr, b: b, start: b.startMs, end: b.startMs + b.durMs });
      });
    });
    all.sort(function (a, b) { return a.start - b.start; });
    // 同一条轴的"已占用结束点"; 只有真正冲突(重叠超过容忍值)时才会走到第二条。
    // 容忍值给 5 秒: 实测分P 间重叠约 1 秒, 属对齐误差不是真冲突。
    var OVERLAP_TOL = 5000;
    var laneEnds = [];
    all.forEach(function (it) {
      var placed = -1;
      for (var i = 0; i < laneEnds.length; i++) {
        if (it.start >= laneEnds[i] - OVERLAP_TOL) { placed = i; break; }
      }
      if (placed < 0) { laneEnds.push(0); placed = laneEnds.length - 1; }
      laneEnds[placed] = Math.max(laneEnds[placed], it.end);
      it.lane = placed;
    });
    ov.layout = all;
    // 正常情况下只有 1 条; 极端重叠(如两个不同场次被误对齐到同一位置)才可能多出来,
    // 那时保留多轨是为了让用户看见"这里有两个东西压在一起", 而不是互相盖住。
    ov.laneCount = Math.max(1, laneEnds.length);
  }

  function drawOverview() {
    var w = ovCanvas.clientWidth, h = ovCanvas.clientHeight;
    if (w <= 0 || h <= 0) { return; }
    // 有轨道块时把容器加高, 给分P 色块留出空间(否则密度图吃满 118px, 块高度为负画不出)
    var wrapEl = document.getElementById('ov-wrap');
    if (wrapEl) {
      var wantLanes = (ov.layout || []).length > 0;
      if (wantLanes && !wrapEl.classList.contains('has-lanes')) {
        wrapEl.classList.add('has-lanes');
        h = ovCanvas.clientHeight;      // 加高后重新取高度
      } else if (!wantLanes && wrapEl.classList.contains('has-lanes')) {
        wrapEl.classList.remove('has-lanes');
        h = ovCanvas.clientHeight;
      }
    }
    var d = Math.min(window.devicePixelRatio || 1, 2);
    if (ovCanvas.width !== Math.round(w * d) || ovCanvas.height !== Math.round(h * d)) {
      ovCanvas.width = Math.round(w * d); ovCanvas.height = Math.round(h * d);
    }
    var g = ovCanvas.getContext('2d');
    g.setTransform(d, 0, 0, d, 0, 0);
    g.clearRect(0, 0, w, h);
    g.fillStyle = 'rgba(255,255,255,.03)';
    g.fillRect(0, 0, w, h);

    if (!ov.loaded) {
      g.fillStyle = '#8b93a3'; g.font = '12px sans-serif';
      g.fillText('（加载当晚总览…）', 12, h / 2);
      return;
    }
    if (!(ov.t1 > ov.t0)) {
      g.fillStyle = '#8b93a3'; g.font = '12px sans-serif';
      g.fillText('（该晚没有可用数据）', 12, h / 2);
      return;
    }

    // ---- 顶部: 弹幕密度(真实直播热度), 作为主轴底图 ----
    // 渲染要点: 密度 bin 数可能远多于画布像素(实测 1133 bin vs 1253px; 某晚更长时 bin 更多)。
    // 这里按"一个像素列聚合多个 bin"来画, 用画布列宽反推,
    // 绝不能每 bin 强画 1px —— 那会让相邻柱互相覆盖, 把密度结构糊成一片(校准图的老毛病)。
    // 聚合取**均值**而非最大值: 最大值会让稀疏时段被单点尖峰拉高, 曲线整体偏胖、丢失低谷。
    var densTop = 14, densH = Math.round(h * 0.32);
    if (ov.bins.length) {
      var plotW = w - 20;
      var colN = Math.min(ov.bins.length, Math.floor(plotW));
      var per = ov.bins.length / colN;          // 每列聚合多少个 bin
      g.fillStyle = 'rgba(76,141,255,.62)';
      var colW = plotW / colN;
      for (var c = 0; c < colN; c++) {
        var a = Math.floor(c * per), b2 = Math.max(a + 1, Math.floor((c + 1) * per));
        var sum = 0, cnt = 0;
        for (var k = a; k < b2 && k < ov.bins.length; k++) { sum += ov.bins[k]; cnt++; }
        var v = cnt ? sum / cnt : 0;
        var bh = Math.pow(v / ov.peak, 0.55) * (densH - 2);
        if (bh < 0.6) { bh = 0.6; }             // 保底一像素, 空白段也能看出"有数据"
        g.fillRect(10 + c * colW, densTop + densH - bh, Math.max(1, colW - 0.2), bh);
      }
    }
    // 时间刻度(每小时)
    g.font = '10px sans-serif';
    var tk = new Date(ov.t0);
    tk.setMinutes(0, 0, 0);
    while (tk.getTime() <= ov.t1) {
      var x = ovX(tk.getTime());
      if (x >= 10 && x <= w - 10) {
        g.fillStyle = 'rgba(255,255,255,.14)';
        g.fillRect(x, densTop, 1, h - 18);
        g.fillStyle = '#8b93a3';
        g.fillText(two(tk.getHours()) + ':00', x - 11, h - 4);
      }
      tk = new Date(tk.getTime() + 3600000);
    }

    // ---- 事件标记(一血/大龙/水晶/GG 等)画在密度区 ----
    // 这些标记已从"校准面板"移到总览: 那里不再重复画密度曲线,
    // 事件标记统一放在这条主时间轴上, 既能辅助校准(点它跳转), 也能看清节奏。
    ov.order = [];
    if (ov.markers && ov.markers.length && kwMarkersEnabled()) {
      // 标签防重叠: 按 x 排序后贪心分配"行", 每行维护已占用到的最小 x,
      // 保证同一行内标签不叠字。行数上限 3, 再密就只留竖线。
      var placed = [];
      ov.markers.forEach(function (m) {
        m._x = ovX(m.wallMs);
        placed.push(m);
      });
      placed.sort(function (a, b) { return a._x - b._x; });
      var rowEnd = [-999, -999, -999];
      placed.forEach(function (m) {
        var lab = m.kw, lw = lab.length * 9 + 4;
        var row = 0;
        for (var r = 0; r < 3; r++) {
          if (m._x > rowEnd[r] + 4) { row = r; break; }
          row = -1;
        }
        if (row < 0) { m._row = -1; return; }     // 挤不下: 只画竖线不写标签
        m._row = row;
        rowEnd[row] = m._x + lw;
      });
      placed.forEach(function (m) {
        var mx = m._x;
        if (mx < 8 || mx > w - 8) { return; }
        var sel = (typeof alignMarkers !== 'undefined' && alignSelIdx >= 0 && alignMarkers[alignSelIdx] === m);
        g.strokeStyle = m.color;
        g.lineWidth = sel ? 2.5 : 1;
        g.globalAlpha = sel ? 1 : 0.85;
        g.beginPath(); g.moveTo(mx, densTop); g.lineTo(mx, densTop + densH); g.stroke();
        g.globalAlpha = 1;
        if (m._row >= 0) {
          g.fillStyle = m.color; g.font = '9px sans-serif';
          var lx = Math.min(Math.max(2, mx - 5), w - m.kw.length * 9 - 4);
          g.fillText(m.kw, lx, densTop - 4 - m._row * 10);
        }
        ov.order.push({ marker: m, x0: mx - 5, x1: mx + 5, y: densTop, h: densH });
      });
    }

    // ---- 下方: 分P块按时间摆进主轴轨道(不重叠的共用一条轨道) ----
    var laneTop = densTop + densH + 10;
    var avail = h - laneTop - 18;
    // 没有场次(tracks 为空)时不要画空轨道底纹 —— 那会让人以为"加载失败"。
    // 改为画一行说明, 明确告诉用户"这晚有弹幕但没配赛程", 而不是留一片空白。
    if (!ov.tracks.length) {
      g.fillStyle = '#8b93a3'; g.font = '12px sans-serif';
      g.fillText('这晚有弹幕数据，但没有能对上的比赛场次（未配置赛程或缺失 BV）', 12, laneTop + 14);
    }
    var laneN = Math.max(1, ov.laneCount || 1);
    var laneH = Math.max(10, Math.min(22, Math.floor(avail / laneN) - 3));
    // 注意: 这里**不能**重置 ov.order —— 上面事件标记已经 push 进去了,
    // 再清空会把标记从命中表里抹掉, 表现为"看得见竖线但点不到/hover 无提示"。

    // 轨道底纹 + 场次分界线
    if (ov.tracks.length) {
      for (var li = 0; li < laneN; li++) {
        var ly = laneTop + li * (laneH + 3);
        g.fillStyle = 'rgba(255,255,255,.035)';
        g.fillRect(10, ly, w - 20, laneH);
      }
    }

    (ov.layout || []).forEach(function (it) {
      var tr = it.tr, b = it.b;
      var y = laneTop + it.lane * (laneH + 3);
      var x0 = ovX(it.start), x1 = ovX(it.end);
      var bwid = Math.max(2, x1 - x0);
      // 颜色: 同一场的所有分P 用**同一个色相**, 靠**透明度**区分第几个分P。
      // 这样"一场比赛是连续的一段"在视觉上立刻成立, 而不是几个不相干的色块。
      var idxInTrack = (tr.blocks || []).indexOf(b);
      var nInTrack = (tr.blocks || []).length;
      var shade = nInTrack <= 1 ? 0.82 : (0.9 - 0.28 * (idxInTrack / Math.max(1, nInTrack - 1)));
      g.fillStyle = tr.color;
      g.globalAlpha = shade;
      g.fillRect(x0, y, bwid, laneH);
      g.globalAlpha = 1;
      // 分P 之间画一条竖线分隔(同色相邻块容易糊成一片, 看不出分界)
      if (idxInTrack > 0) {
        g.strokeStyle = 'rgba(6,18,31,.55)'; g.lineWidth = 1;
        g.beginPath(); g.moveTo(x0 + 0.5, y); g.lineTo(x0 + 0.5, y + laneH); g.stroke();
      }
      ov.order.push({ it: it, x0: x0, x1: x0 + bwid, y: y, h: laneH });
      if (bwid > 30) {
        g.fillStyle = '#06121f'; g.font = 'bold 9px sans-serif';
        // 块内直接写**分P 名称**(如 "第一局"/"赛前评论"), 不再加 "G1·" 序号前缀 ——
        // 序号对用户没信息量, 而且上传顺序与真实时间顺序常常不一致, 容易误导。
        var label = String(b.part || ('P' + b.p));
        g.fillText(label.slice(0, Math.max(2, Math.floor(bwid / 8))), x0 + 3, y + laneH - 4);
      }
    });

    // ---- 当前播放位置(竖直白线) ----
    if (state.applied && ov.t0) {
      var cx = ovX(baseMs() + getV() * 1000);
      if (cx >= 0 && cx <= w) {
        g.strokeStyle = '#fff'; g.lineWidth = 1.5;
        g.beginPath(); g.moveTo(cx, 0); g.lineTo(cx, h - 14); g.stroke();
      }
    }
  }

  function ovHit(x, y) {
    for (var i = ov.order.length - 1; i >= 0; i--) {
      var o = ov.order[i];
      if (x >= o.x0 && x <= o.x1 && y >= o.y && y <= o.y + o.h) { return o; }
    }
    return null;
  }

  ovCanvas.addEventListener('mousemove', function (e) {
    var r = ovCanvas.getBoundingClientRect();
    var hit = ovHit(e.clientX - r.left, e.clientY - r.top);
    if (hit && hit.marker) {
      // 事件标记: 这是手动校准的兜底入口 —— 点它跳过去, 再「对齐到此刻」
      var m = hit.marker;
      ovCanvas.style.cursor = 'pointer';
      ovCanvas.title = m.label + '（' + m.count + ' 条弹幕）— 点击跳到该时刻，' +
                       '再用「对齐到此刻」手动校准';
    } else if (hit && hit.it) {
      var tr = hit.it.tr, b = hit.it.b;
      ovCanvas.style.cursor = 'pointer';
      ovCanvas.title = tr.title + ' · ' + (b.part || ('P' + b.p)) +
        '（' + fmtCnt(Math.round((b.durMs) / 1000)) + '，点击播放这一局）';
    } else {
      ovCanvas.style.cursor = 'crosshair';
      ovCanvas.title = '点击跳到该时刻';
    }
  });

  ovCanvas.addEventListener('click', function (e) {
    if (!ov.loaded) { return; }
    var r = ovCanvas.getBoundingClientRect();
    var x = e.clientX - r.left, y = e.clientY - r.top;
    var hit = ovHit(x, y);
    if (hit && hit.marker) { playEventMarker(hit.marker); return; }
    if (hit && hit.it) { playFromOverview(hit.it.tr, hit.it.b); return; }
    if (state.applied) { seekV(clampSeek((ovMs(x) - baseMs()) / 1000)); }
  });

  // 点事件标记: 跳到该时刻并选中它, 让「对齐到此刻」可用(手动校准兜底路径)
  function playEventMarker(m) {
    alignSelIdx = alignMarkers.indexOf(m);
    if (state.applied) { seekV(clampSeek((m.wallMs - baseMs()) / 1000)); }
    updateAlignNowBtn();
    drawOverview();
  }

  // 点总览某场某局 -> 切场并定位到该分P起点
  function playFromOverview(tr, block, opts) {
    opts = opts || {};
    if (!tr.bv) {
      ovSummary.textContent = tr.title + ' 没有配置回放 BV，无法播放（点「刷新赛程」可补全）';
      return;
    }
    // 已经是当前场次: 直接定位, 不重新加载(不打断播放)
    if (state.applied && state.curBv === tr.bv) {
      if (block) {
        var relSec = (block.startMs - state.anchorMs) / 1000 - tr.offsetSec;
        seekV(clampSeek(relSec));
      }
      return;
    }
    var found = false;
    for (var i = 0; i < selMatch.options.length; i++) {
      if (selMatch.options[i].value === tr.title) { selMatch.selectedIndex = i; found = true; break; }
    }
    if (!found) {
      ovSummary.textContent = '该场次不在当前列表里，请先「刷新赛程」';
      return;
    }
    applyMatchToInputs();
    // 记下加载完成后要定位到哪一局(由 doApply 的成功回调消费)
    state.pendingOverview = block
      ? { bv: tr.bv, offsetSec: tr.offsetSec, block: block }
      : { bv: tr.bv, offsetSec: tr.offsetSec, block: null };
    doApply();
  }

  if (btnOvRefresh) {
    btnOvRefresh.addEventListener('click', function () {
      if (selDate.value) { loadOverview(selDate.value); loadMatchList(selDate.value); }
    });
  }

  /* ---------------- 事件标注 / 防剧透开关 ---------------- */
  // 防剧透: 事件关键词(一血/大龙/水晶/GG...)会直接透露比赛进程, 看"准直播"时等于剧透。
  // 开关放在总览面板(事件标记现在也只画在总览上), 共用一份持久化设置。
  var KW_SPOILER_KEY = 'dm-kw-spoiler-hidden';
  function kwMarkersEnabled() {
    try { return localStorage.getItem(KW_SPOILER_KEY) !== '1'; } catch (e) { return true; }
  }
  function setKwMarkersEnabled(on) {
    try { localStorage.setItem(KW_SPOILER_KEY, on ? '0' : '1'); } catch (e) { /* 忽略 */ }
    if (chkKwSpoiler) { chkKwSpoiler.checked = on; }
    // 关掉时不生成标记 = 只显示密度, 不透露任何事件
    buildAlignChart();
  }
  if (chkKwSpoiler) {
    chkKwSpoiler.addEventListener('change', function () { setKwMarkersEnabled(chkKwSpoiler.checked); });
  }

  // 事件关键词: 用于在"当晚总览"的主时间轴上标注比赛关键节点。
  // 注意(防剧透): 这些词会透露比赛进程, 由 kwMarkersEnabled() 统一开关。
  var EV_KWS = [
    { kw: '一血', min: 1, color: '#ff5c5c' },
    { kw: '五杀', min: 1, color: '#ff5c5c' },
    { kw: '团灭', min: 3, color: '#ffb020' },
    { kw: '大龙', min: 3, color: '#9d5cff' },
    { kw: '水晶', min: 3, color: '#4cd964' },
    { kw: '结束了', min: 3, color: '#40b3ff' },
    { kw: 'GG', min: 2, color: '#40b3ff' }
  ];
  var alignMarkers = [];    // [{wallMs, kw, count, label, color, row}]
  var alignSelIdx = -1;

  // 从弹幕流里聚簇出事件标记, 写入总览(ov.markers)并重绘。
  // 密度曲线不再在这里画 —— 它和总览的密度区重复, 已合并到总览。
  function buildAlignChart() {
    alignMarkers = []; alignSelIdx = -1;
    ov.markers = [];
    if (state.danmaku.length && kwMarkersEnabled()) {
      EV_KWS.forEach(function (cfg) {
        var tsList = [];
        state.danmaku.forEach(function (d) {
          if (d.ty === 'dm' && d.t && d.t.indexOf(cfg.kw) >= 0) { tsList.push(d.ts); }
        });
        tsList.sort(function (a, b) { return a - b; });
        var cluster = [], cnt = 0;
        function flush() {
          if (cluster.length && cnt >= cfg.min) {
            var wall = cluster[Math.floor(cluster.length / 2)];
            var dt = new Date(wall);
            alignMarkers.push({ wallMs: wall, kw: cfg.kw, count: cnt, color: cfg.color,
              label: cfg.kw + ' ' + two(dt.getHours()) + ':' + two(dt.getMinutes()) });
          }
          cluster = []; cnt = 0;
        }
        tsList.forEach(function (t) {
          if (cluster.length && t - cluster[cluster.length - 1] > 30000) { flush(); }
          cluster.push(t); cnt++;
        });
        flush();
      });
    }
    // 最多 14 个, 按强度取; 再按时间排, 交错两行放标签
    alignMarkers.sort(function (a, b) { return b.count - a.count; });
    alignMarkers = alignMarkers.slice(0, 14);
    alignMarkers.sort(function (a, b) { return a.wallMs - b.wallMs; });
    alignMarkers.forEach(function (m, i) { m.row = i % 2; });
    ov.markers = alignMarkers;
    updateAlignNowBtn();
    drawOverview();      // 事件标记画在总览主轴上
  }

  // 总览横轴: 事件标记/播放位置都用同一套映射
  function alignX(wallMs) { return ovX(wallMs); }
  function alignWall(x) { return ovMs(x); }
  function drawAlignChart() { drawOverview(); }   // 兼容旧调用点: 统一重绘总览


  function updateAlignNowBtn() {
    var m = alignMarkers[alignSelIdx];
    btnAlignNow.disabled = !m;
    btnAlignNow.textContent = m
      ? '对齐到此刻（事件：' + m.label + '）'
      : '对齐到此刻（先在总览点选事件）';
    // 手动校准默认折叠: 自动校准失败/需要微调时把它展开, 让兜底入口可见
    if (manualCalib) {
      var autoFailed = alignStatus && !alignStatus.hidden &&
                       /失败|不可用|错误/.test(alignStatus.textContent || '');
      if (m || autoFailed) { manualCalib.open = true; }
    }
  }

  btnAlignNow.addEventListener('click', function () {
    var m = alignMarkers[alignSelIdx];
    if (!m || !state.applied) { return; }
    var wantOffset = (m.wallMs - state.anchorMs - getV() * 1000) / 1000;
    var delta = Math.round(wantOffset - state.offsetSec);
    if (delta === 0) { alignSelIdx = -1; updateAlignNowBtn(); return; }
    adjustOffset(delta);
    alignSelIdx = -1;
    updateAlignNowBtn();
  });

  // 应用新 offset(绝对值): 即时平移弹幕映射(不重载)并保存。
  // 手动微调 adjustOffset 与「自动校准」共用同一条路径。
  function applyOffsetValue(newOffset, fromAuto) {
    state.offsetSec = newOffset;
    inpOffset.value = String(Math.round(newOffset * 1000) / 1000);
    var base = baseMs();
    for (var i = 0; i < state.danmaku.length; i++) {
      state.danmaku[i].rel = state.danmaku[i].ts - base;
    }
    active.length = 0;
    resetTracks();
    state.idx = upperBound(state.danmaku, getV() * 1000);
    alignCur.textContent = 'offset: ' + state.offsetSec + 's';
    drawOverview();
    // manual 标记: 手动微调要让服务端清掉旧的 auto(自动校准置信度)记录,
    // 避免条目里挂着"高置信度自动校准"的假象。自动校准写回时不需要(它自己写 auto)。
    fetch('/api/match', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ date: state.curDate, file: state.curFile,
        bv: (inpBv.value || '').trim() || state.curBv, offset_sec: state.offsetSec,
        list_title: state.curMatchTitle || undefined,
        manual: fromAuto ? undefined : true }) }).catch(noop);
  }
  // 对齐微调: 即时平移弹幕映射(不重载), 并保存
  function adjustOffset(delta) {
    if (!state.applied) {
      inpOffset.value = String((parseFloat(inpOffset.value) || 0) + delta);
      return;
    }
    applyOffsetValue(state.offsetSec + delta);
  }
  document.querySelectorAll('.nbtn[data-d]').forEach(function (b) {
    b.addEventListener('click', function () { adjustOffset(parseInt(b.getAttribute('data-d'), 10) || 0); });
  });

  /* ---------------- 自动校准(音频指纹对齐) ---------------- */
  var autoAlignBusy = false;
  function setAlignStatus(msg, cls) {
    alignStatus.hidden = !msg;
    alignStatus.textContent = msg || '';
    alignStatus.className = 'align-status' + (cls ? ' ' + cls : '');
  }

  btnAutoAlign.addEventListener('click', function () {
    if (autoAlignBusy) { return; }
    if (!state.applied) {
      setAlignStatus('请先「开始播放」加载弹幕与回放，再点自动校准。', 'err');
      return;
    }
    var bv = (inpBv.value || '').trim() || state.curBv;
    if (!bv) {
      setAlignStatus('缺少回放 BV 号，无法自动校准。', 'err');
      return;
    }
    autoAlignBusy = true;
    btnAutoAlign.disabled = true;
    btnAutoAlign.textContent = '自动校准中…';
    setAlignStatus('自动校准进行中：正在拉取回放音频并与直播音频指纹比对（首次约半分钟到一分钟）…', '');
    fetch('/api/autocalign', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ date: state.curDate, file: state.curFile, bv: bv, page: 1 })
    }).then(function (r) { return r.json(); }).then(function (j) {
      if (j && j.ok && typeof j.offset_sec === 'number') {
        applyOffsetValue(j.offset_sec, true);   // 自动校准: 与手动微调同一条 offset 应用路径(但保留 auto 记录)
        setAlignStatus('自动校准完成: offset=' + j.offset_sec.toFixed(1) +
          ' s (置信度 ' + (typeof j.confidence === 'number' ? j.confidence.toFixed(1) : '?') + ')', 'ok');
      } else {
        setAlignStatus('自动校准失败: ' + (j && j.error ? j.error : '未知错误'), 'err');
      }
    }).catch(function (e) {
      setAlignStatus('自动校准失败: ' + (e && e.message ? e.message : '网络错误'), 'err');
    }).finally(function () {
      autoAlignBusy = false;
      btnAutoAlign.disabled = false;
      btnAutoAlign.textContent = '自动校准';
    });
  });

  /* ---------------- 刷新赛程(自动发现当天场次 + 搜索官方回放BV) ---------------- */
  var discoverBusy = false;
  btnDiscover.addEventListener('click', function () {
    if (discoverBusy) { return; }
    var date = selDate.value;
    if (!date) {
      setAlignStatus('请先选择日期，再刷新赛程。', 'err');
      return;
    }
    discoverBusy = true;
    btnDiscover.disabled = true;
    btnDiscover.textContent = '刷新中…';
    setAlignStatus('正在获取 Riot 赛程并搜索B站官方回放（约数秒到十几秒）…', '');
    getJson('/api/discover?date=' + enc(date)).then(function (j) {
      if (j && j.ok && Array.isArray(j.matches)) {
        var matched = 0;
        j.matches.forEach(function (m) { if (m && m.found) { matched++; } });
        setAlignStatus('发现 ' + (j.riot_count || 0) + ' 场, 已匹配 ' + matched +
          ' 个回放BV' + (matched < (j.matches.length) ? '（其余回放未上传或未搜到，稍后可再点刷新回填）' : ''), 'ok');
        loadMatchList(date);   // 重载场次下拉
      } else {
        setAlignStatus('赛程刷新失败: ' + (j && j.error ? j.error : '未知错误'), 'err');
      }
    }).catch(function (e) {
      setAlignStatus('赛程刷新失败: ' + (e && e.message ? e.message : '网络错误'), 'err');
    }).finally(function () {
      discoverBusy = false;
      btnDiscover.disabled = false;
      btnDiscover.textContent = '刷新赛程';
    });
  });

  /* ---------------- 右侧栏 ---------------- */
  function nearBottom() {
    return dmList.scrollHeight - dmList.scrollTop - dmList.clientHeight < 40;
  }
  function appendChat(d, mine) {
    var div = document.createElement('div');
    div.className = 'dm-item' + (d.ty === 'sc' ? ' sc' : '') + (mine ? ' me' : '');
    var prefix = mine ? '[我] ' : (d.ty === 'sc' ? '[SC ¥' + d.price + '] ' : '');
    div.textContent = prefix + (mine ? '' : (d.un ? d.un + ': ' : '')) + d.t;
    var stick = nearBottom();
    dmList.appendChild(div);
    while (dmList.children.length > 200) { dmList.removeChild(dmList.firstChild); }
    if (stick) { dmList.scrollTop = dmList.scrollHeight; }
  }
  function showSc(d) {
    scBanner.hidden = false;
    scBanner.innerHTML = '';
    var div = document.createElement('div');
    div.className = 'sc-line';
    div.textContent = '¥' + d.price + ' ' + (d.un || '匿名') + '：' + d.t;
    scBanner.appendChild(div);
    clearTimeout(scTimer);
    scTimer = setTimeout(hideSc, 20000);
  }
  function hideSc() {
    clearTimeout(scTimer);
    scBanner.hidden = true;
    scBanner.innerHTML = '';
  }

  /* ---------------- 控制条 ---------------- */
  btnPlay.addEventListener('click', function () {
    if (videoEl.paused) { videoEl.play().catch(noop); } else { videoEl.pause(); }
  });
  function syncPlayLabel() { btnPlay.textContent = videoEl.paused ? '播放' : '暂停'; }
  videoEl.addEventListener('play', syncPlayLabel);
  videoEl.addEventListener('pause', syncPlayLabel);

  videoEl.addEventListener('timeupdate', function () {
    if (!isFinite(state.duration) || state.duration <= 0) { return; }
    var pct = Math.min(100, getV() / state.duration * 100);
    progInner.style.width = pct + '%';
    progThumb.style.left = pct + '%';
    var label = fmtCnt(Math.floor(getV())) + ' / ' + fmtCnt(Math.floor(state.duration));
    var pm = currentPageMark();
    if (pm && pm.part) { label += '  ' + pm.part; }
    if (effectiveRate() !== 1) { label += '  ' + effectiveRate() + '×'; }
    timeLabel.textContent = label;
    syncTitleForPlayback();
  });

  function seekByPointer(e) {
    if (!isFinite(state.duration) || state.duration <= 0) { return; }
    var rect = prog.getBoundingClientRect();
    var pct = Math.min(1, Math.max(0, (e.clientX - rect.left) / rect.width));
    seekV(pct * state.duration);
  }
  var progDrag = false;
  prog.addEventListener('pointerdown', function (e) {
    progDrag = true;
    try { prog.setPointerCapture(e.pointerId); } catch (err) {}
    seekByPointer(e);
  });
  prog.addEventListener('pointermove', function (e) { if (progDrag) { seekByPointer(e); } });
  prog.addEventListener('pointerup', function () { progDrag = false; });

  rngVol.addEventListener('input', function () {
    videoEl.volume = (+rngVol.value) / 100;
    videoEl.muted = false;
    btnMute.textContent = '音量';
  });
  btnMute.addEventListener('click', function () {
    videoEl.muted = !videoEl.muted;
    btnMute.textContent = videoEl.muted ? '已静音' : '音量';
  });

  // 全屏: 整容器(含弹幕层)
  btnFs.addEventListener('click', function () {
    var wrap = videoEl.closest('.player-wrap');
    if (document.fullscreenElement) {
      document.exitFullscreen().catch(noop);
    } else if (wrap && wrap.requestFullscreen) {
      wrap.requestFullscreen().catch(noop);
    }
  });

  // 全屏下控制条自动隐藏: 鼠标移动/触摸时浮现, 静止 3 秒后连同鼠标指针一起隐藏。
  // 旧实现是 CSS 里 `:fullscreen #ctrl-bar{opacity:1}` 常显, 全屏时一直挡着画面底部。
  var fsHideTimer = null;
  var FS_IDLE_MS = 3000;
  function fsWrap() { return videoEl.closest('.player-wrap'); }
  function fsReveal() {
    var w = fsWrap();
    if (!w || !document.fullscreenElement) { return; }
    w.classList.add('ctrl-visible');
    w.classList.remove('ctrl-hidden');
    if (fsHideTimer) { clearTimeout(fsHideTimer); }
    fsHideTimer = setTimeout(function () {
      var w2 = fsWrap();
      if (!w2 || !document.fullscreenElement) { return; }
      // 鼠标停在控制条上时不要藏(否则想点按钮它会消失)
      if (w2.matches(':hover') && document.getElementById('ctrl-bar').matches(':hover')) { return; }
      w2.classList.remove('ctrl-visible');
      w2.classList.add('ctrl-hidden');
    }, FS_IDLE_MS);
  }
  function fsReset() {
    var w = fsWrap();
    if (w) { w.classList.remove('ctrl-visible', 'ctrl-hidden'); }
    if (fsHideTimer) { clearTimeout(fsHideTimer); fsHideTimer = null; }
  }
  document.addEventListener('fullscreenchange', function () {
    if (document.fullscreenElement) {
      fsReveal();               // 刚进全屏先显示一下, 让用户知道控制条在哪
    } else {
      fsReset();
    }
  });
  ['mousemove', 'mousedown', 'touchstart', 'keydown'].forEach(function (ev) {
    document.addEventListener(ev, function () {
      if (document.fullscreenElement) { fsReveal(); }
    }, { passive: true });
  });

  videoEl.addEventListener('seeked', function () {
    if (!state.applied) { return; }
    active.length = 0;
    resetTracks();
    state.idx = upperBound(state.danmaku, getV() * 1000);
  });

  videoEl.addEventListener('error', function () {
    if (!state.applied) { return; }
    var u = state.segs[state.segIdx] || state.playUrl;
    if (!triedFallback && u) {
      triedFallback = true;
      videoEl.src = '/api/stream?url=' + enc(u);
      videoEl.load();
      return;
    }
    showError('视频加载失败：直链可能过期或被 CDN 拒绝，请重试。');
  });

  /* ---------------- 其他事件 ---------------- */
  selDate.addEventListener('change', function () {
    loadFiles(selDate.value);
    loadOverview(selDate.value);     // 总览必须跟日期一起换, 否则停留在上一晚的数据
  });
  selMatch.addEventListener('change', applyMatchToInputs);
  selFile.addEventListener('change', function () {
    getJson('/api/match?date=' + enc(selDate.value) + '&file=' + enc(selFile.value)).then(function (j) {
      if (j && j.ok) { inpOffset.value = j.offset_sec || 0; }
    }).catch(noop);
  });
  btnApply.addEventListener('click', doApply);

  selDmArea.addEventListener('change', function () {
    settings.area = parseFloat(selDmArea.value) || 1; reflectSettings(); saveSettings();
  });
  rngDmOpacity.addEventListener('input', function () {
    settings.opacity = (+rngDmOpacity.value) / 100; reflectSettings(); saveSettings();
  });
  selDmSize.addEventListener('change', function () {
    settings.size = parseInt(selDmSize.value, 10) || 26; reflectSettings(); saveSettings();
  });
  var selDmDensity = $('sel-dm-density');
  if (selDmDensity) {
    selDmDensity.addEventListener('change', function () {
      settings.density = parseFloat(selDmDensity.value) || 1; reflectSettings(); saveSettings();
    });
  }

  function setDmOn(on) {
    state.dmOn = on;
    btnDmToggle.classList.toggle('off', !on);
    btnDmToggle.textContent = on ? '弹幕: 开' : '弹幕: 关';
    var qb = $('btn-dm-quick');
    if (qb) {
      qb.classList.toggle('off', !on);
      qb.textContent = on ? '弹' : '弹(关)';
      qb.title = on ? '隐藏弹幕' : '显示弹幕';
    }
    if (!on) {
      active.length = 0; resetTracks();
      if (W > 0) { ctx.clearRect(0, 0, W, H); }
    }
  }
  btnDmToggle.addEventListener('click', function () { setDmOn(!state.dmOn); });
  var btnDmQuick = $('btn-dm-quick');
  if (btnDmQuick) {
    btnDmQuick.addEventListener('click', function () { setDmOn(!state.dmOn); });
  }

  var chatInput = null;
  if (chatBox) {
    chatInput = (chatBox.tagName === 'INPUT' || chatBox.tagName === 'TEXTAREA')
      ? chatBox : chatBox.querySelector('input,textarea');
  }
  if (chatInput) {
    chatInput.addEventListener('keydown', function (e) {
      if (e.key !== 'Enter') { return; }
      var v = chatInput.value.trim();
      if (!v) { return; }
      chatInput.value = '';
      appendChat({ un: '', t: v, ty: 'dm', price: 0 }, true);
    });
  }

  window.addEventListener('resize', function () { drawOverview(); });

  /* ---------------- 启动 ---------------- */
  function init() {
    applySettings();
    // 防剧透开关初值来自持久化设置(默认显示事件标记)
    var kwOn = kwMarkersEnabled();
    if (chkKwSpoiler) { chkKwSpoiler.checked = kwOn; }
    elClock.textContent = fmtClock(new Date());
    loadDays();
    requestAnimationFrame(loop);
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();




