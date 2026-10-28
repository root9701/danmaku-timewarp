'use strict';
/* 弹幕回放台 · 前端逻辑（原生 JS，无依赖）
 * 区块: 选择场次(日期/场次/文件) → 弹幕校准(密度曲线+事件标注+一键对齐/微调) → 弹幕与显示
 * 播放: 多分段虚拟时间轴 getV/seekV, canvas 弹幕(预渲染精灵), 自绘控制条, 容器全屏
 */
(function () {
  function $(id) { return document.getElementById(id); }

  /** HTML 转义: 只用于**拼进 innerHTML 字符串**的文本。
   *  为什么需要: 场次标题来自 Riot/B站官方接口与本地 matches.json, 不是用户自己输入的,
   *  但那是外部数据 —— 名字里带 & < > " 完全可能(队名、赞助商后缀)。直接拼进 innerHTML
   *  会破坏结构, 极端情况下注入脚本。本文件其余地方一律用 textContent, 只有少数必须拼
   *  innerHTML 的地方(如总览图例)走这个函数。 */
  function escHtml(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

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
  var btnAutoAlign = $('btn-auto-align'), alignStatus = $('align-status');
  var btnDiscover = $('btn-discover');
  var btnUpdate = $('btn-update'), updateStatus = $('update-status');
  var ovCanvas = $('ov-canvas'), ovSummary = $('ov-summary'), ovLegend = $('ov-legend');
  var btnOvRefresh = $('btn-ov-refresh'), btnOvExpand = $('btn-ov-expand');
  var elTopMeta = $('topbar-meta');
  var btnMore = $('btn-more'), moreBox = $('more-box');

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
    pageOffsets: {},      // {page: offset_sec} 每个分P 各自的墙钟 offset(音频对齐结果)
    offsetDelta: 0,       // 手动微调量(作用于所有分P, 整体平移弹幕)
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

  /* ---------------- 分P 分段时间映射 ----------------
   * 播放器把官方回放的各分P **首尾相接**拼成一条"虚拟时间轴"(getV)。
   * 但直播侧两局之间有局间休息, 官方回放把这部分剪掉了 —— 于是"虚拟轴位置"和
   * "直播真实时刻"之间**不是一条直线**, 而是分段直线:
   *
   *   分P k 在虚拟轴上占 [S_k, S_k+Dur_k)
   *   该分P 内 局部时间 t 对应的墙钟 = anchor + offset_k + t
   *   => 虚拟位置 v 对应的墙钟 = anchor + offset_k*1000 + (v - S_k)*1000
   *   => 弹幕挂到虚拟轴上的位置 rel = ts - (anchor + offset_k*1000 - S_k*1000)
   *
   * 历史缺陷: 全程只用一个标量 offset(而且取的是第一分P 的), 等价于假设
   * "回放各分P 无缝相连且与直播等长"。实测偏差: AL 第二局 -213.9s、第三局 -207.8s,
   * TSW vs LOS 第二局 -224.9s —— 弹幕整体晚几分钟, 越往后越离谱。
   *
   * 局间休息里的弹幕(视频里根本不存在那段)直接**丢弃**, 不压缩到相邻分P:
   * 压缩会把休息期间的几百条弹幕堆到下一局开头, 比丢掉更难看出问题。
   */
  function pageOffsetOf(page) {
    if (page == null) { return null; }
    var o = state.pageOffsets[String(page)];
    return (typeof o === 'number' && isFinite(o)) ? o : null;
  }

  // 某分P 实际采用的 offset: 有对齐结果用它, 没有则把该页接在上一页结束处(顺序串联猜测)。
  // 返回 null 表示该页无法定位(连第一页都没有 offset)。
  function buildPageSegs() {
    var pms = (state.pageMarks || []).slice().sort(function (a, b) {
      return (a.start_sec || 0) - (b.start_sec || 0);
    });
    if (!pms.length) { return []; }
    var hasAny = false;
    for (var i = 0; i < pms.length; i++) {
      if (pageOffsetOf(pms[i].page) != null) { hasAny = true; break; }
    }
    if (!hasAny) { return []; }        // 完全没有分P 对齐数据 -> 调用方退化到单 offset
    var segs = [], prevEnd = null;
    for (var k = 0; k < pms.length; k++) {
      var pm = pms[k];
      var off = pageOffsetOf(pm.page);
      if (off == null) {
        // 该分P 没有对齐结果(自动校准失败/被时间窗拒绝)。
        // 不能"接在上一页末尾"猜一个位置: page_marks 来自 B站接口(回放真实有哪几个
        // 分P), 而 offset 来自我们的对齐结果, 两者**不是同一个集合**。猜出来的位置会
        // 把它之后的分P 一起带偏, 表现为"点 BLG 的块却显示 T1 的赛前评论"。
        // 也不必从 segs 里删掉它 —— segs 与视频分段(state.segs)必须同序同数, 否则
        // seekV 会算错。做法是保留占位, 但标 locatable=false, 映射时跳过。
        segs.push({ page: pm.page, part: pm.part, w0: null, w1: null,
                    s0: (pm.start_sec || 0) * 1000, estimated: false,
                    locatable: false });
        continue;
      }
      off += state.offsetDelta;                   // 手动微调整体平移
      var w0 = state.anchorMs + off * 1000;
      var dur = (typeof pm.duration === 'number' && pm.duration > 0) ? pm.duration : 0;
      segs.push({ page: pm.page, part: pm.part, w0: w0, w1: w0 + dur * 1000,
                  s0: (pm.start_sec || 0) * 1000, estimated: false,
                  locatable: true });
      prevEnd = w0 + dur * 1000;
    }
    segs.sort(function (a, b) { return a.w0 - b.w0; });
    return segs;
  }

  // 当前虚拟播放位置落在哪个分P 段(用于标题、进度标签等)
  function currentPageSeg() {
    var segs = buildPageSegs();
    if (!segs.length) { return null; }
    var v = getV() * 1000;
    for (var i = 0; i < segs.length; i++) {
      // 未对齐的分P 没有可信区间, 不能命中
      if (!segs[i].locatable) { continue; }
      if (v >= segs[i].s0 && v < segs[i].s0 + (segs[i].w1 - segs[i].w0)) { return segs[i]; }
    }
    // 兜底: 返回最后一个可定位的段(未对齐段不参与)
    for (var j = segs.length - 1; j >= 0; j--) {
      if (segs[j].locatable) { return segs[j]; }
    }
    return null;
  }

  // 当前画面时刻对应的真实墙钟(毫秒)。总览播放线、点空白跳转都用它。
  function wallNowMs() {
    var segs = buildPageSegs();
    var v = getV();
    if (!segs.length) { return state.anchorMs + (state.offsetSec + state.offsetDelta) * 1000 + v * 1000; }
    var seg = currentPageSeg();
    if (!seg) {
      // 一个可定位的分P 都没有: 退回标量 offset(不改状态, 只是算个近似值)
      return state.anchorMs + (state.offsetSec + state.offsetDelta) * 1000 + v * 1000;
    }
    return seg.w0 + (v * 1000 - seg.s0);
  }

  // 跳到指定墙钟时刻(把"总览上的一刻"翻译成虚拟轴位置)
  function seekToWallMs(wallMs) {
    var segs = buildPageSegs();
    if (!segs.length) { seekV(clampSeek((wallMs - baseMs()) / 1000)); return; }
    // 只有已对齐的分P 才有可信墙钟区间; 未对齐的(locatable=false)不参与命中与吸附。
    var loc = [];
    for (var q = 0; q < segs.length; q++) { if (segs[q].locatable) { loc.push(segs[q]); } }
    if (!loc.length) { seekV(clampSeek((wallMs - baseMs()) / 1000)); return; }
    for (var i = 0; i < loc.length; i++) {
      if (wallMs >= loc[i].w0 && wallMs < loc[i].w1) {
        seekV(clampSeek(loc[i].s0 / 1000 + (wallMs - loc[i].w0) / 1000));
        return;
      }
    }
    // 落在局间休息(该时刻没有视频): 取时间上最近的分P 边界
    var best = null, bd = Infinity;
    for (var k = 0; k < loc.length; k++) {
      var d = Math.min(Math.abs(wallMs - loc[k].w0), Math.abs(wallMs - loc[k].w1));
      if (d < bd) { bd = d; best = loc[k]; }
    }
    if (best) { seekV(clampSeek(best.s0 / 1000 + (wallMs - best.w0) / 1000)); }
  }

  // 按分P 分段重算全部弹幕的虚拟轴位置; 落在局间休息里的丢弃(见上方说明)。
  //
  // **必须按墙钟时间逐段处理, 不能对全表二分**:
  // 分P 的"上传顺序"与"真实时间顺序"不一定一致。实测 26 日 HLE vs IG:
  //   P1 第一局(墙钟 01:00)、P2 赛后采访(01:51)、P3 赛前评论(00:45)
  // 即 P3 在时间上**最早**, 但它在虚拟轴上排最后。若把 segs 排序后套一个二分,
  // 算出的 rel 会从 4406s 突然回退到 281s —— danmaku 不再按 rel 升序,
  // upperBound 的二分前提被破坏, 表现是"跨该分P 边界后弹幕整段消失"。
  //
  // 做法: 每个分P 段内独立映射(段内 rel 必然单调), 再按 rel 排序。
  // 段与段在虚拟轴上互不重叠, 故排序后整体单调。
  function recomputeRels() {
    var dm = state.danmaku || [];
    var segs = buildPageSegs();
    if (!segs.length) {
      var b0 = baseMs();
      for (var j = 0; j < dm.length; j++) { dm[j].rel = dm[j].ts - b0; }
      return;
    }
    var kept = [];
    for (var si = 0; si < segs.length; si++) {
      var s = segs[si];
      // 未对齐的分P(locatable=false)没有可信的墙钟区间, 它的弹幕直接丢弃 ——
      // 硬塞一个猜出来的位置比丢掉更糟: 会把别的分P 的弹幕错位到它那里。
      if (!s.locatable || s.w0 == null) { continue; }
      for (var i = 0; i < dm.length; i++) {
        var ts = dm[i].ts;
        if (ts >= s.w0 && ts < s.w1) {
          dm[i].rel = s.s0 + (ts - s.w0);
          kept.push(dm[i]);
        }
      }
    }
    // 按 rel 升序(同一时刻保持原 ts 相对顺序, 稳定排序)
    kept.sort(function (a, b) { return a.rel - b.rel || a.ts - b.ts; });
    state.danmaku = kept;
  }

  // 把弹幕映射重置到当前播放位置(改 offset / 换场次后调用)
  function resetDanmakuIndex() {
    active.length = 0;
    resetTracks();
    state.idx = upperBound(state.danmaku, getV() * 1000);
  }

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
          // total_pages: B 站当前实际有几个分P(服务端 matchlist 现取)。
          // 两者不一致 = 有分P 没对齐(常见: 赛前评论在直播开播前没录到, 或对齐被拒),
          // 必须如实标出来 —— 否则用户看到"已对齐"却少了一局, 会以为程序漏了。
          var totPages = (typeof m.total_pages === 'number') ? m.total_pages : 0;
          var nErr = (m.page_errors && typeof m.page_errors === 'object')
            ? Object.keys(m.page_errors).length : 0;
          var tag = '';
          if (!m.bv) {
            tag = '（缺 BV）';
          } else if (m.align_failed) {
            tag = '（未对齐 · 直播可能未转播）';
          } else if (nPages && totPages && nPages < totPages) {
            tag = '（已对齐 ' + nPages + '/' + totPages + ' 个分P）';
          } else if (nPages > 1) {
            tag = '（已对齐 ' + nPages + ' 个分P）';
          } else if (nPages === 1) {
            tag = '（已对齐）';
          } else if (!m.offset_sec) {
            tag = '（未对齐）';
          }
          o.textContent = m.title + tag;
          // 悬停给出具体原因: 哪几页没对齐、为什么
          var tips = [];
          if (m.align_error) { tips.push(m.align_error); }
          if (m.page_errors) {
            Object.keys(m.page_errors).forEach(function (k) {
              tips.push('P' + k + ' 未对齐: ' + m.page_errors[k]);
            });
          }
          o.title = tips.join('\n');
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
  // qn: 画质码(64=720P / 16=360P), 空则用服务端默认。
  function fetchPlay(bv, page, qn) {
    var q = '/api/playurl?bv=' + enc(bv) + (page ? ('&page=' + enc(page)) : '')
          + (qn ? ('&qn=' + enc(qn)) : '');
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
    // 带上 bv: 服务端按该场比赛返回逐分P 对齐结果(pages), 供分段映射使用。
    // 不带 bv 时只能拿到会话级标量 offset —— 多场共用一个会话时会串场。
    var pMatch = getJson('/api/match?date=' + enc(date) + '&file=' + enc(file) + '&bv=' + enc(bv))
      .then(function (j) {
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

  // 应用一批分P offset(自动校准返回后用), 并保持当前播放位置不变地重算弹幕.
  function applyPageOffsets(newOffsets) {
    if (!newOffsets) { return; }
    Object.keys(newOffsets).forEach(function (k) {
      if (typeof newOffsets[k] === 'number' && isFinite(newOffsets[k])) {
        state.pageOffsets[k] = newOffsets[k];
      }
    });
    // 顶层 offsetSec 同步为第一分P, 供"总览播放线/无分P数据"路径使用
    var keys = Object.keys(state.pageOffsets).map(Number).sort(function (a, b) { return a - b; });
    if (keys.length) { state.offsetSec = state.pageOffsets[String(keys[0])]; }
    state.offsetDelta = 0;
    inpOffset.value = String(Math.round(state.offsetSec * 1000) / 1000);
    state.idx = 0;
    recomputeRels();
    resetDanmakuIndex();
    var elOff = document.getElementById('align-cur');
    if (elOff) {
      elOff.textContent = 'offset: ' + describePageOffsets();
    }
    drawOverview();
  }

  function describePageOffsets() {
    var ks = Object.keys(state.pageOffsets);
    if (ks.length > 1) {
      return ks.map(function (k) { return 'P' + k + '=' + state.pageOffsets[k].toFixed(1); }).join(' ');
    }
    return String(Math.round(state.offsetSec * 1000) / 1000) + 's';
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
    // 画质: qualities 由服务端从 B站 accept_quality 转来, 是真实可选档位
    qualityList = (play.qualities || []).slice();
    curQuality = play.quality || 0;
    syncQualityBtn();
    renderQualityMenu();
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

    // 逐分P offset 来自 /api/match?bv=... (doApply 已带 bv 请求, 响应里 pages 就是分段映射数据)。
    // 每个分P 有自己的 offset; 只用标量会从第二局起整体错位。
    state.pageOffsets = {};
    state.offsetDelta = 0;
    if (match && match.pages && typeof match.pages === 'object') {
      Object.keys(match.pages).forEach(function (k) {
        var pv = match.pages[k];
        if (pv && typeof pv.offset_sec === 'number' && isFinite(pv.offset_sec)) {
          state.pageOffsets[k] = pv.offset_sec;
        }
      });
    }
    // 有分P 数据时, 顶层标量同步为第一分P(供无分P 路径与显示兜底)
    if (Object.keys(state.pageOffsets).length) {
      var ks = Object.keys(state.pageOffsets).map(Number).sort(function (a, b) { return a - b; });
      state.offsetSec = state.pageOffsets[String(ks[0])];
      state.offsetDelta = offset - state.offsetSec;   // 用户手动填的 offset 视为整体微调
    }

    state.danmaku = parseDanmaku(dmText);
    state.applied = true;
    recomputeRels();
    resetDanmakuIndex();
    dmList.innerHTML = ''; hideSc();
    syncTitleForPlayback();
    // 校准区一直显示当前生效的 offset。初始加载也要刷新, 否则停在占位符「offset: --」,
    // 用户看不到任何对齐值(要等点一次自动校准或微调才出现)。
    var elOff0 = document.getElementById('align-cur');
    if (elOff0) { elOff0.textContent = 'offset: ' + describePageOffsets(); }

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
    // 时间标记(一血/大龙…)随场次一起换。失败不影响播放, 所以不放在 Promise.all 里
    // 等待 —— 那会让"标记接口慢"变成"整场都打不开"。
    loadMarkers(bv);

    // 从总览点选进来的定位:
    //  - block 有值: 定位到那一局的开头(优先按分P 号精确到 page_marks)
    //  - block 为 null: 连播切过来, 从头开始(不定位)
    var po = state.pendingOverview;
    if (po && po.bv === bv) {
      state.pendingOverview = null;
      var target = null;
      if (po.block) {
        for (var k = 0; k < state.pageMarks.length; k++) {
          if (state.pageMarks[k].page === po.block.p) { target = state.pageMarks[k]; break; }
        }
      }
      if (target) {
        seekV(clampSeek(target.start_sec));
      } else if (po.block) {
        // 分P信息没取全时, 按墙钟时刻反查虚拟轴位置(分段映射, 不是简单相减)
        seekToWallMs(po.block.startMs);
      }
    }
  }

  /* ---------------- 播放控制 ---------------- */
  // 依据当前虚拟播放位置, 找到落在哪一局, 更新左上角标题与徽标。
  //
  // 优先用 currentPageSeg(按**真实墙钟**判定的分段), 而不是按 page_marks 的序号累加:
  // 分P 上传顺序与时间顺序可能不一致(实测 HLE vs IG: P3 赛前评论在时间上最早,
  // 却排在虚拟轴最后)。按序号累加会与分段映射用两套口径, 表现是标题写着「第一局」
  // 而弹幕已按「赛后采访」映射 —— 用户看到的就是"标题和内容对不上"。
  function currentPageMark() {
    if (!state.pageMarks || !state.pageMarks.length) { return null; }
    var seg = currentPageSeg();
    if (seg) {
      for (var k = 0; k < state.pageMarks.length; k++) {
        if (state.pageMarks[k].page === seg.page) { return state.pageMarks[k]; }
      }
    }
    var v = getV(), hit = null;
    for (var i = 0; i < state.pageMarks.length; i++) {
      var pm = state.pageMarks[i];
      if (v >= pm.start_sec - 0.5) { hit = pm; } else { break; }
    }
    return hit;
  }
  // 下一段(下一局)的起点; 已是最后一段返回 null。
  // 按**虚拟轴位置**排序找下一个, 不按 page_marks 数组下标 —— 上传顺序可能是乱的。
  function nextPageMark() {
    if (!state.pageMarks || !state.pageMarks.length) { return null; }
    var cur = currentPageMark();
    if (!cur) { return state.pageMarks[0]; }
    var nx = null;
    for (var k = 0; k < state.pageMarks.length; k++) {
      var pm = state.pageMarks[k];
      if (pm.start_sec > cur.start_sec &&
          (nx === null || pm.start_sec < nx.start_sec)) {
        nx = pm;
      }
    }
    return nx;
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

  // 播放失败的三种情况必须分开处理, 不能一律当成"浏览器阻止自动播放":
  //   1) NotAllowedError  —— 真的被自动播放策略拦了, 需要用户点击
  //   2) AbortError      —— play() 被新的 load() 打断。**这不是错误**: 切场时
  //                          setup 先 load() 再 play(), 上一次 play 会被作废,
  //                          此刻视频其实正在播。旧代码把这类也弹"点击开始",
  //                          结果就是"弹出提示但实际能播"(用户报告的现象)。
  //   3) 其他(如 NotSupportedError) —— 格式/直链问题, 走兜底重载或报错
  function startPlay() {
    pendingPlay = false;
    videoEl.play().then(noop).catch(function (err) {
      var name = (err && err.name) || '';
      if (name === 'AbortError') {
        // 被 load() 打断: 交给下一次 play 继续, 什么都不做。
        // 不弹遮罩 —— 弹了反而挡住正在播放的画面。
        return;
      }
      if (name === 'NotAllowedError') {
        pendingPlay = true;
        showMask('浏览器阻止了自动播放，请点击开始。', '开始播放', function () {
          hideMask();
          videoEl.play().then(function () { pendingPlay = false; })
            .catch(function () { showError('播放失败，请重试。'); });
        });
        return;
      }
      // 其他错误: 先尝试从遮罩点一下恢复(可能是直链刚过期), 仍失败才报错
      pendingPlay = true;
      showMask('播放遇到问题，请点击重试。', '继续播放', function () {
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
    // 换分P 时立刻把进度条归位到新分P 的起点。
    // 不重置的话, 上一分P 播完时进度条停在 100%, 切到下一分P 后
    // timeupdate 要等首帧才触发, 这段时间里"进度条在末尾、画面却是第二段开头",
    // 看起来像进度条坏了。这里先画一次, 保证切换瞬间就是对的。
    lastMarkSeg = -1;
    syncProgress();
  }
  // 按当前分P 刷新进度条与时间标签(时间轴只有一条: 每个分P 各自独立计时)。
  var lastMarkSeg = -1;    // 上次按哪个分P 画的标记(-1=还没画过)
  function syncProgress() {
    var segLen = state.segLens[state.segIdx] || 0;
    var segPos = videoEl.currentTime || 0;
    var pct = segLen > 0 ? Math.min(100, Math.max(0, segPos / segLen * 100)) : 0;
    progInner.style.width = pct + '%';
    progThumb.style.left = pct + '%';
    var label = fmtCnt(Math.floor(segPos)) + ' / ' + fmtCnt(Math.floor(segLen));
    var pm = currentPageMark();
    if (pm && pm.part) { label += '  ' + pm.part; }
    if (effectiveRate() !== 1) { label += '  ' + effectiveRate() + '×'; }
    timeLabel.textContent = label;
    // 标记是按"当前分P"换算的, 换段后必须重画。timeupdate 每秒触发多次,
    // 所以用 lastMarkSeg 挡住重复重建(标记列表不变时没必要动 DOM)。
    if (lastMarkSeg !== state.segIdx) {
      lastMarkSeg = state.segIdx;
      renderProgMarks();
    }
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
      // 走统一的 startPlay, 由它按错误类型决定"忽略 AbortError / 提示用户点击"。
      // 这里原来是 .catch(noop), 被 AbortError 打断时静默失败, 表现为"切到下一局后
      // 画面停在第一帧不动、也没有任何提示"。
      startPlay();
    }
  });
  videoEl.addEventListener('ended', function () {
    if (!state.applied) { return; }
    // 找到下一段"有对齐结果"的视频段。未对齐的分P(见 buildPageSegs 的 locatable)
    // 没有可信的墙钟位置, 它的弹幕已在 recomputeRels 里丢弃 —— 播它只会得到
    // "标题写着这一局、画面是另一段、一条弹幕都没有"的混乱体验, 直接跳过。
    var next = state.segIdx + 1;
    while (next < state.segs.length && !segIndexLocatable(next)) { next++; }
    if (next < state.segs.length) {
      switchSeg(next, 0, true);
      return;
    }
    // 整场(全部分P)播完 -> 若开了"连播", 自动接下一场
    if (state.autoAdvance) { advanceToNextMatch(); }
  });

  // 第 i 个视频段是否有对齐结果(可按 page_marks 与 video segs 的同序关系判定)。
  // page_marks 由 /api/playurl 按分P 顺序给出, 与 urls/seg_lengths 一一对应;
  // 拿它的 page 号去 pageOffsets 查即可。
  function segIndexLocatable(i) {
    var pms = state.pageMarks || [];
    if (!pms.length) { return true; }        // 没有分P 信息时不做限制
    if (i >= pms.length) { return true; }
    return pageOffsetOf(pms[i].page) != null;
  }

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
    // 连播要**从头**放整场(含开头的赛前/BP 等), 而不是跳到第一个分P 块 ——
    // 传 null 表示"不定位, 从 0 开始"。
    playFromOverview(nx, null, { keepAutoAdvance: true });
  }

  function syncAutoAdvanceBtn() {
    if (!btnAutoAdvance) { return; }
    // 工具条里的按钮自带名字, 文案写成"连播：开/关"
    btnAutoAdvance.textContent = '连播：' + (state.autoAdvance ? '开' : '关');
    btnAutoAdvance.classList.toggle('on', state.autoAdvance);
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
    // 弹出档位菜单(B站 式竖排)。键盘 << >> / Shift+, . 仍可逐档切换。
    toggleMenu('menu-rate');
  });

  // 倍速菜单: 竖排档位, 当前项高亮。点一项即生效并关菜单。
  function renderRateMenu() {
    var m = $('menu-rate');
    if (!m) { return; }
    m.innerHTML = '';
    RATE_STEPS.slice().reverse().forEach(function (r) {
      var el = document.createElement('button');
      el.type = 'button';
      el.className = 'cmenu-item' + (Math.abs(r - state.rate) < 1e-6 ? ' on' : '');
      el.textContent = (r === 1 ? '1.0' : String(r)) + '×';
      el.addEventListener('click', function () { closeMenus(); setRate(r); });
      m.appendChild(el);
    });
  }

  // 跳过当前分P剩余部分(局间采访/赛前评论等), 直接进入下一局
  if (btnSkip) {
    btnSkip.addEventListener('click', function () {
      var nx = nextPageMark();
      if (!nx || !state.applied) { return; }
      seekV(clampSeek(nx.start_sec + 0.2));
      // 用户主动点了"下一局": 失败要说清原因, 不能静默不动
      if (videoEl.paused) { startPlay(); }
    });
  }

  /* ---- 画质选择 ----
   * 可选项来自 B站 的 accept_quality(服务端 /api/playurl 的 qualities 字段),
   * **不自己编档位**: 只列这个视频真实能下发的画质。
   * 换画质 = 重新取直链 + 保持当前虚拟位置续播, 不能让用户从头开始看。
   */
  var qualityList = [];       // [{qn,label}] 当前视频可选档位
  var curQuality = 0;         // 实际在用的画质码(服务端下发值, 可能低于请求值)

  function qualityLabel(qn) {
    for (var i = 0; i < qualityList.length; i++) {
      if (qualityList[i].qn === qn) { return qualityList[i].label; }
    }
    return qn ? (qn + 'P') : '';
  }
  function syncQualityBtn() {
    var b = $('btn-quality');
    if (!b) { return; }
    if (!qualityList.length) {
      b.textContent = '画质';
      b.title = '该视频没有可选的画质档位';
      return;
    }
    // 按钮上只写分辨率主体(如 "720P"), 完整描述放进菜单和 title
    var lab = qualityLabel(curQuality) || '画质';
    b.textContent = lab.replace(/^(高清|超清|清晰|流畅|真彩)\s*/, '');
    b.title = '画质：' + lab;
  }
  function renderQualityMenu() {
    var m = $('menu-quality');
    if (!m) { return; }
    m.innerHTML = '';
    if (!qualityList.length) {
      m.innerHTML = '<div class="cmenu-item" style="color:#5c6472;cursor:default">无可选画质</div>';
      return;
    }
    qualityList.forEach(function (q) {
      var el = document.createElement('button');
      el.type = 'button';
      el.className = 'cmenu-item' + (q.qn === curQuality ? ' on' : '');
      el.textContent = q.label;
      el.disabled = (q.qn === curQuality);
      el.addEventListener('click', function () {
        closeMenus();
        setQuality(q.qn);
      });
      m.appendChild(el);
    });
  }

  // 换画质: 保持在同一个分P 的同一秒, 只换直链。
  // 为什么不能走 doApply: 那会重新选场次、重载弹幕、从 0 开始播。
  function setQuality(qn) {
    if (!state.applied || !state.curBv) { return; }
    if (qn === curQuality) { return; }
    var keepSeg = state.segIdx;
    var keepTime = videoEl.currentTime || 0;
    var wasPlaying = !videoEl.paused;
    showToast('正在切换到 ' + qualityLabel(qn) + '…');
    fetchPlay(state.curBv, 0, qn).then(function (play) {
      var segs = (play.urls && play.urls.length) ? play.urls.slice() : [play.url];
      var lens = (play.seg_lengths && play.seg_lengths.length === segs.length)
        ? play.seg_lengths.slice() : [play.duration || 0];
      // 分段数变了(极罕见)就不能按 segIdx 续播, 退回按虚拟轴位置
      var vPos = (keepSeg < state.segStarts.length)
        ? (state.segStarts[keepSeg] || 0) + keepTime : 0;
      state.segs = segs;
      state.segLens = lens;
      state.segStarts = [0];
      for (var s = 1; s < segs.length; s++) {
        state.segStarts.push(state.segStarts[s - 1] + lens[s - 1]);
      }
      state.duration = typeof play.duration === 'number' ? play.duration : 0;
      qualityList = (play.qualities || []).slice();
      curQuality = play.quality || qn;
      syncQualityBtn();
      renderQualityMenu();
      // 恢复位置: 在同一个分P 内就按原秒数回去, 否则按虚拟轴换算
      state.segIdx = Math.min(keepSeg, segs.length - 1);
      var local = (state.segIdx === keepSeg) ? keepTime : (vPos - (state.segStarts[state.segIdx] || 0));
      state.pendingSeekMs = Math.max(0, local);
      state.pendingAutoplay = wasPlaying;
      triedFallback = false;
      videoEl.src = segs[state.segIdx];
      videoEl.load();
      lastMarkSeg = -1;
      syncProgress();
      showToast('画质：' + qualityLabel(curQuality));
    }).catch(function (e) {
      showToast('切换画质失败：' + (e && e.message ? e.message : '网络错误'));
    });
  }

  /* ---- 控制条弹出菜单(画质/倍速/弹幕设置) ----
   * 三个菜单共用一套开关逻辑: 同时只开一个, 点外面关掉, Esc 关掉。
   * 全屏时菜单是 z-index 最高的元素, 不会被控制条的自动隐藏规则影响
   * (隐藏只作用于 #ctrl-bar 的 opacity, 而菜单在它内部 —— 所以鼠标进菜单时
   *  要让 fsReveal 持续刷新, 见下面的 hover 判断)。
   */
  var MENU_IDS = ['menu-quality', 'menu-rate', 'panel-dm-set'];

  function closeMenus() {
    MENU_IDS.forEach(function (id) {
      var el = $(id);
      if (el) { el.hidden = true; }
    });
  }
  function toggleMenu(id) {
    var el = $(id);
    if (!el) { return; }
    var willOpen = el.hidden;
    closeMenus();
    if (!willOpen) { return; }
    if (id === 'menu-rate') { renderRateMenu(); }
    if (id === 'menu-quality') { renderQualityMenu(); }
    el.hidden = false;
  }
  // 点控制条以外的地方关菜单。用 pointerdown 而非 click: 拖进度条时也要收起菜单。
  document.addEventListener('pointerdown', function (e) {
    var t = e.target;
    if (t && t.closest && t.closest('.cmenu-wrap')) { return; }
    closeMenus();
  });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') { closeMenus(); }
  });
  var btnQualityEl = $('btn-quality');
  if (btnQualityEl) {
    btnQualityEl.addEventListener('click', function () { toggleMenu('menu-quality'); });
  }
  var btnDmSetEl = $('btn-dm-set');
  if (btnDmSetEl) {
    btnDmSetEl.addEventListener('click', function () { toggleMenu('panel-dm-set'); });
  }


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

  /* ---------------- 时间标记(一血/大龙/水晶…) ----------------
   * 来源: B站 看点(view_points), 见 server.py 的 /api/markers。
   * 两条原则:
   *   1) 标记是**增强**, 不是依赖: 接口失败/没数据就什么都不画, 绝不弹错、绝不挡播放。
   *   2) 防剧透开启时**一根都不画** —— 一血/大龙/推掉水晶这些标记本身就会提前泄露
   *      局势和结局(例: 07:30 就出现"击杀纳什男爵", 还没看到那儿就知道谁赢了)。
   */
  var markers = [];

  function loadMarkers(bv) {
    markers = [];
    renderProgMarks();
    if (!bv) { return; }
    getJson('/api/markers?bv=' + enc(bv)).then(function (j) {
      if (!j || !j.ok || state.curBv !== bv) { return; }   // 已切场: 丢弃迟到结果
      markers = (j.markers || []).slice();
      renderProgMarks();
      showToast(j.count ? ('已载入 ' + j.count + ' 个时间标记') : '这个回放没有官方时间标记');
    }).catch(function () { /* 标记拿不到不影响播放 */ });
  }

  // 把标记画到进度条上。位置必须按**当前分P**换算: 标记的 at 是整条虚拟时间轴
  // 上的秒数, 而进度条现在只表示当前分P(见 syncProgress 的注释), 两者分母不同,
  // 直接拿 at/state.duration 会全部挤在前几像素。
  function renderProgMarks() {
    var box = $('prog-marks');
    if (!box) { return; }
    box.innerHTML = '';
    if (settings.spoiler || !markers.length) { return; }
    var segLen = state.segLens[state.segIdx] || 0;
    if (!(segLen > 0)) { return; }
    var segStart = state.segStarts[state.segIdx] || 0;
    markers.forEach(function (m) {
      var local = m.at - segStart;
      if (local < 0 || local > segLen) { return; }
      var el = document.createElement('i');
      el.className = 'prog-mark ' + (m.kind === 'kill' ? 't-kill' : 't-obj');
      el.style.left = (local / segLen * 100) + '%';
      el.title = fmtCnt(Math.floor(m.at)) + ' ' + m.label +
                 (m.team ? (' · ' + m.team) : '');
      box.appendChild(el);
    });
  }

  /* ---------------- 防剧透 ----------------
   * 用户要求默认开启。要藏两样东西, 因为它们都会泄露"打到第几局、谁赢了":
   *   - 进度条上的时间标记(一血/大龙/水晶 → 直接看出局势与结局)
   *   - 总览里的分P 色块(第一局/第二局/第三局 → 直接看出打了几局)
   * 保留: 场次色带(谁 vs 谁)与密度曲线 —— 那是"今晚有哪几场", 不泄露比分。
   */
  function syncSpoilerBtn() {
    var b = $('btn-spoiler');
    if (!b) { return; }
    // 工具条里的按钮自带名字(没有 <span> 标签), 所以文案要写成"防剧透：开/关"
    b.textContent = '防剧透：' + (settings.spoiler ? '开' : '关');
    b.classList.toggle('on', !!settings.spoiler);
    b.title = settings.spoiler
      ? '防剧透已开启：隐藏时间标记与分P 色块（点击关闭）'
      : '防剧透已关闭：显示时间标记与分P 色块（点击开启）';
    // 防剧透开着时, 展开分P 是没有意义的(展开也画不出来), 直接禁用并说明,
    // 否则用户点"展开分P"什么都不会发生, 看起来像按钮坏了。
    if (btnOvExpand) {
      btnOvExpand.disabled = !!settings.spoiler;
      btnOvExpand.title = settings.spoiler
        ? '防剧透开启中，分P 明细已隐藏。关闭防剧透后可展开。'
        : '展开/收起分P 行';
    }
  }
  function setSpoiler(on) {
    settings.spoiler = !!on;
    syncSpoilerBtn();
    saveSettings();
    renderProgMarks();
    drawOverview();
    showToast(settings.spoiler ? '防剧透已开启' : '防剧透已关闭');
  }
  var btnSpoilerEl = $('btn-spoiler');
  if (btnSpoilerEl) {
    btnSpoilerEl.addEventListener('click', function () { setSpoiler(!settings.spoiler); });
  }


  // 说明: 字体选的是系统黑体, 不提供"选字体"(用户明确说不用), 但**描边可关** ——
  // 白字加 4px 黑边在浅色画面上会糊成一坨, 关掉更清楚。
  var settings = {
    area: 1, opacity: 0.9, size: 26, density: 1,
    speed: 1,          // 弹幕滚动速度倍率(0.6~2.0), 与播放倍速无关
    stroke: true,      // 文字描边
    spoiler: true      // 防剧透(默认开): 隐藏进度条标记与总览分P 色块
  };
  var DM_DEFAULTS = { area: 1, opacity: 0.9, size: 26, density: 1, speed: 1, stroke: true };
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
        if (isFinite(saved.speed)) { settings.speed = Math.min(2, Math.max(0.6, +saved.speed)); }
        if (typeof saved.stroke === 'boolean') { settings.stroke = saved.stroke; }
        if (typeof saved.spoiler === 'boolean') { settings.spoiler = saved.spoiler; }
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
          size: settings.size, density: settings.density, speed: settings.speed,
          stroke: settings.stroke, spoiler: settings.spoiler,
          rate: state.rate, autoAdvance: state.autoAdvance }));
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
    var spd = $('rng-dm-speed');
    if (spd) { spd.value = String(Math.round(settings.speed * 100)); }
    var chk = $('chk-dm-stroke');
    if (chk) { chk.checked = !!settings.stroke; }
    syncSpoilerBtn();
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
    // 描边可关(用户要求"可以调整是否描边")。关掉时只在浅色画面上可能看不清,
    // 但那是用户自己的选择; 开着时 4px 黑边在最亮的画面里也压得住。
    if (settings.stroke) {
      g.lineWidth = 4;
      g.strokeStyle = '#000';
      g.strokeText(text, 4, TRACK_H / 2);
    }
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
    // 速度倍率: 基准速度与画布宽度挂钩(宽屏跑得快, 保持"穿过屏幕的时间"接近),
    // 再乘用户的 speed 设置。调慢可以让密集时段的弹幕看得清。
    var baseV = Math.max(140, W / 9) * settings.speed;
    var it = { sp: sp, w: sp.dw, x: W + 4, y: 0, v: baseV };
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
  // 极简风格的场次配色: 一个蓝点缀 + 多级灰。为什么不用彩虹:
  // 4 场用 4 个不同色相会让页面第一眼变成"彩色标签墙", 违背极简的前提。
  // 这里靠"蓝 → 亮灰 → 中灰 → 深灰"的明度阶梯区分, 同时保留一个可点亮的蓝。
  var OV_COLORS = ['#5b8cff', '#a0a6b0', '#7d8590', '#c3c8d0', '#8a8f98', '#6f757f'];

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
    // 顶栏与总览头部共用同一份环境数据, 但**只写短标签**: 这是状态栏, 不是说明。
    var spanMin = Math.round((j.span_sec || 0) / 60);
    var nTrack = (j.tracks || []).length;
    var nPart = (j.tracks || []).reduce(function (a, t) {
      return a + ((t.parts || []).length);
    }, 0);
    var head = [];
    head.push(j.match_date || '?');
    head.push('主轴 ' + spanMin + ' 分');
    head.push(nTrack + ' 场');
    if (nPart) { head.push(nPart + ' 个分P'); }
    ovSummary.textContent = head.join(' · ');

    if (elTopMeta) {
      var meta = [];
      meta.push(nTrack + ' 场');
      meta.push((j.dm_count || 0).toLocaleString() + ' 条弹幕');
      elTopMeta.textContent = meta.join(' · ');
    }
    buildOvLegend(j.tracks || []);
  }

  function buildOvLegend(tracks) {
    // 场次名已经画在时间轴的色带里了, 这里不再重复列一遍(那样又长又不直观)。
    // 只保留"需要注意"的场次: 未对齐、缺 BV —— 这些在轴上画不出块,
    // 不说明的话用户会以为"图画丢了"。
    var html = '';
    tracks.forEach(function (t) {
      var nParts = (t.parts || []).length;
      var unaligned = !nParts && !t.offset_sec;
      if (unaligned) {
        html += '<span style="opacity:.75"><i style="background:#3a3d43"></i>' +
                escHtml(t.title) + '<b style="color:#d99a6c"> 未对齐</b></span>';
        return;
      }
      if (!t.bv) {
        html += '<span style="opacity:.75"><i style="background:#3a3d43"></i>' +
                escHtml(t.title) + '<b style="color:#d99a6c"> 缺BV</b></span>';
      }
    });
    ovLegend.innerHTML = html;
  }

  // 逐场生成"分P 块"。
  // 优先用服务端给出的**每分P 独立 offset**(tr.parts)——这是音频指纹逐分P 对齐的结果,
  // 每段都锚在自己的真实时刻上, 因此段与段之间的留白就是真实的局间休息。
  // 旧做法是按 P 序号把 page_marks 累加串联, 那只在"各分P 首尾无缝相接"时才对,
  // 而直播侧两局之间明明有休息, 累加会把这部分时间吃掉, 越往后偏得越多。
  // 没有 parts 数据时(未对齐过)才退回旧串联方式。
  //
  // 分P 里的一切段落都画, 不按名字筛选: 赛前评论/赛后采访/抽签仪式同样是直播里真实
  // 存在的一段, 用户可能就想跳到那段看弹幕。是否"对局"由用户自己判断。
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
  // 把 #rrggbb 按比例调暗/调亮。amt<0 变暗, amt>0 变亮。
  // 用于场次色带: 同一个场次色, 色带用暗一档、分P 块用原色, 层次分明。
  function shadeColor(hex, amt) {
    var m = /^#?([0-9a-f]{6})$/i.exec(String(hex || ''));
    if (!m) { return hex || '#666'; }
    var n = parseInt(m[1], 16);
    var r = (n >> 16) & 255, gg = (n >> 8) & 255, b = n & 255;
    function f(v) {
      var x = amt < 0 ? v * (1 + amt) : v + (255 - v) * amt;
      return Math.max(0, Math.min(255, Math.round(x)));
    }
    return 'rgb(' + f(r) + ',' + f(gg) + ',' + f(b) + ')';
  }

  function layoutOverviewLanes() {
    var all = [];
    ov.tracks.forEach(function (tr, ti) {
      tr.blocks.forEach(function (b) {
        // **所有分P 都上轴**, 包括赛前评论/赛后采访/抽签这类配件。
        // 它们同样是直播里真实存在的一段, 用户可能就想跳到"赛后采访"看那段弹幕。
        // (曾短暂按名字过滤过配件, 结果用户发现"每场都有赛后采访, 图上却全没有" —— 那是错的。)
        all.push({ ti: ti, tr: tr, b: b, start: b.startMs, end: b.startMs + b.durMs });
      });
    });
    all.sort(function (a, b) { return a.start - b.start; });
    // 单条轴 + 层叠: 分P 之间本来就有秒级重叠(实测第二局止于 06:54:54、抽签仪式起于
    // 06:54:47, 重叠 7 秒, 是源视频重复了几秒画面), 这种直接压上去即可。
    // 只有**大面积重叠**(超过 60 秒, 说明两段内容真的并行/算错了)才另开一轨,
    // 那时多轨是为了让人看见"这里有两个东西压在一起"。
    var OVERLAP_TOL = 60000;
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

  // 总览的两种状态: 紧凑(钉顶常驻, 只画密度+场次色带) / 展开(多出分P 行与刻度)。
  // 为什么要有紧凑态: 它钉在页面顶部, 展开态 178px 会长期吃掉画面高度;
  // 而"切到哪一场"只需要看场次色带, 不需要看见每一局。
  var ovExpanded = false;

  function setOvExpanded(on) {
    ovExpanded = !!on;
    if (btnOvExpand) {
      btnOvExpand.textContent = ovExpanded ? '收起分P ▴' : '展开分P ▾';
      btnOvExpand.classList.toggle('on', ovExpanded);
    }
    drawOverview();
  }

  function drawOverview() {
    var w = ovCanvas.clientWidth, h = ovCanvas.clientHeight;
    if (w <= 0 || h <= 0) { return; }
    // 防剧透开启时只画"场次色带"(谁 vs 谁), 不画分P 色块(第一局/第二局…) ——
    // 分P 的个数与名字本身就泄露了"打了几局、有没有被零封"。
    var hideBlocks = !!settings.spoiler;
    var lanes = hideBlocks ? [] : (ov.layout || []);
    // 高度三档: 紧凑 66 / 只色带 96 / 展开 178。
    // 注意 hideBlocks 时**也要走 compact**: 防剧透虽然不画分P 行, 但密度与场次色带仍要显示,
    // 用 band(96px) 反而比紧凑态更高、还多留一行空白。
    var wrapEl = document.getElementById('ov-wrap');
    if (wrapEl) {
      var want;
      if (!ov.tracks.length && !lanes.length) {
        want = 'band';                       // 这晚没配赛程: 留一行提示
      } else {
        want = (lanes.length > 0 && ovExpanded) ? 'full' : 'compact';
      }
      if (wrapEl.getAttribute('data-mode') !== want) {
        wrapEl.setAttribute('data-mode', want);
        h = ovCanvas.clientHeight;      // 高度变了, 重新取
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
    //
    // 几何全部**按模式算**, 不再用"占高度 32%"这种比例:
    // 紧凑态只有 66px, 按比例算出来的密度区会被压成 14px, 看不出起伏。
    var full = (wrapEl && wrapEl.getAttribute('data-mode') === 'full');
    var densTop = full ? 14 : 8;
    var densH = full ? Math.round(h * 0.34) : Math.max(26, h - 40);
    if (ov.bins.length) {
      var plotW = w - 20;
      var colN = Math.min(ov.bins.length, Math.floor(plotW));
      var per = ov.bins.length / colN;          // 每列聚合多少个 bin
      g.fillStyle = 'rgba(140,148,160,.46)';
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
    // 时间刻度(每小时)。紧凑态只画刻度线, 不写文字 —— 66px 里塞下 8 个时间会糊成一排。
    g.font = '10px sans-serif';
    var tk = new Date(ov.t0);
    tk.setMinutes(0, 0, 0);
    while (tk.getTime() <= ov.t1) {
      var x = ovX(tk.getTime());
      if (x >= 10 && x <= w - 10) {
        g.fillStyle = 'rgba(255,255,255,.10)';
        g.fillRect(x, densTop, 1, h - (full ? 18 : 10));
        if (full) {
          g.fillStyle = '#8a8f98';
          g.fillText(two(tk.getHours()) + ':00', x - 11, h - 4);
        }
      }
      tk = new Date(tk.getTime() + 3600000);
    }

    // ---- 事件标记(一血/大龙/水晶/GG 等)画在密度区 ----
    // 这些标记已从"校准面板"移到总览: 那里不再重复画密度曲线,
    // 事件标记统一放在这条主时间轴上, 既能辅助校准(点它跳转), 也能看清节奏。
    ov.order = [];
    // ---- 下方: 分P块按时间摆进主轴轨道(不重叠的共用一条轨道) ----
    var laneTop = densTop + densH + 22;
    var avail = h - laneTop - 18;
    // 没有场次(tracks 为空)时不要画空轨道底纹 —— 那会让人以为"加载失败"。
    // 改为画一行说明, 明确告诉用户"这晚有弹幕但没配赛程", 而不是留一片空白。
    if (!ov.tracks.length && full) {
      g.fillStyle = '#8a8f98'; g.font = '12px sans-serif';
      g.fillText('这晚有弹幕数据，但没有能对上的比赛场次（未配置赛程或缺失 BV）', 12, laneTop + 14);
    }
    var laneN = Math.max(1, ov.laneCount || 1);
    var laneH = Math.max(10, Math.min(22, Math.floor(avail / laneN) - 3));
    // 注意: 这里**不能**重置 ov.order —— 上面事件标记已经 push 进去了,
    // 再清空会把标记从命中表里抹掉, 表现为"看得见竖线但点不到/hover 无提示"。

    // 轨道底纹 + 场次分界线
    if (lanes.length) {
      for (var li = 0; li < laneN; li++) {
        var ly = laneTop + li * (laneH + 3);
        g.fillStyle = 'rgba(255,255,255,.035)';
        g.fillRect(10, ly, w - 20, laneH);
      }
    }

    lanes.forEach(function (it) {
      var tr = it.tr, b = it.b;
      var y = laneTop + it.lane * (laneH + 3);
      var x0 = ovX(it.start), x1 = ovX(it.end);
      // 最小可点宽度: 赛后采访(386s)、抽签仪式(511s) 在 420 分钟的轴上只有 6~8 像素,
      // 按真实宽度画会被相邻块压住、也点不中, 看起来像"这个分P 不存在"。
      // 保底 4px, 同时保证它仍落在自己的真实起点上(不挪位置, 只加宽)。
      var realW = Math.max(1, x1 - x0);
      var bwid = Math.max(4, realW);
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
      // 块内文字: 第一行是"第一局/第二局"(分P 名), 第二行给场次名(只在够宽时画)。
      // 分P 名是用户选块的直接依据, 优先保证它可见。
      if (bwid > 26) {
        g.fillStyle = '#06121f'; g.font = 'bold 9px sans-serif';
        var label = String(b.part || ('P' + b.p));
        g.fillText(label.slice(0, Math.max(2, Math.floor(bwid / 8))), x0 + 3, y + laneH - 4);
      } else {
        // 太窄放不下文字: 用一道更亮的细边标出来, 保证"看得见、点得中",
        // 但**不加引线标签** —— 短分P 常常紧挨着主块, 标签会互相压住, 反而更乱。
        // 具体是什么, 由 hover 提示和上方色带说明。
        g.strokeStyle = 'rgba(255,255,255,.55)'; g.lineWidth = 1;
        g.strokeRect(x0 + 0.5, y + 0.5, Math.max(1, bwid - 1), laneH - 1);
      }
    });

    // ---- 场次色带 + 队伍名(画在分P 色块**上方**) ----
    // 需求: "在比赛的条上面显示谁 vs 谁, 而不是在下面显示"; 用一个统一的色带代表
    // 一整场(从该场第一个分P 开始到最后一个分P 结束), 队伍名写在色带里,
    // 分P 块落在色带下方 —— 视觉上是"一场比赛包含若干局", 而不是几个并列的色块。
    var bandH = 13;
    // 色带位置: 展开态在密度图下方(与分P 块留 6px); 紧凑态直接贴底, 因为那就是它唯一的内容。
    var bandTop = full ? (laneTop - bandH - 6) : Math.max(densTop + densH + 4, h - bandH - 6);
    ov.tracks.forEach(function (tr) {
      // 紧凑态也要画色带 —— 那是"切到哪一场"的唯一入口, 比密度图更重要。
      // 但紧凑态不显示分P 明细, 所以块的起止要靠 tr.blocks 算(块本身不画)。
      if (!tr.blocks || !tr.blocks.length) { return; }
      var bs = ovX(tr.blocks[0].startMs);
      var be = ovX(tr.blocks[tr.blocks.length - 1].startMs +
                   tr.blocks[tr.blocks.length - 1].durMs);
      if (!(be > bs)) { return; }
      var bw = Math.max(3, be - bs);
      // 色带: 实色(略暗)底 + 亮色顶边。半透明在深色背景上几乎看不见,
      // "谁 vs 谁"就白写了。
      g.globalAlpha = 0.9;
      g.fillStyle = shadeColor(tr.color, -0.45);
      g.fillRect(bs, bandTop, bw, bandH);
      g.globalAlpha = 1;
      g.fillStyle = tr.color;
      g.fillRect(bs, bandTop, bw, 2);
      // 队伍名: 色带够宽就写在色带里, 窄了挪到右侧(不截断到不可读)
      g.font = 'bold 10px sans-serif';
      g.textBaseline = 'middle';
      var name = String(tr.title || '');
      var tw = g.measureText(name).width;
      if (tw + 8 <= bw) {
        g.fillStyle = '#eef0f3';
        g.fillText(name, bs + 4, bandTop + bandH / 2 + 0.5);
      } else if (bw > 10) {
        g.fillStyle = '#b9bfc9';
        g.fillText(name, bs + bw + 4, bandTop + bandH / 2 + 0.5);
      }
      g.textBaseline = 'alphabetic';
      // 色带可点击: 紧凑态下它就是切场入口(点色带 = 跳该场第一局)
      ov.order.push({ band: true, tr: tr, x0: bs, x1: bs + bw,
                      y: bandTop, h: bandH });
    });

    // ---- 当前播放位置(竖直白线) ----
    if (state.applied && ov.t0) {
      // 用分段映射求当前画面的真实墙钟: 直接把 getV() 加到一个标量 offset 上会在
      // 第二局起跑偏(局间休息在回放里被剪掉), 实测 AL 第二局偏 -213.9s。
      var cx = ovX(wallNowMs());
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

  // 色带命中后要跳到"这一场的第一局"。紧凑态只画色带不画分P 块,
  // 如果不让色带可点, 收起分P 时就没法从总览切场了 —— 所以色带必须可点。
  function bandFirstBlock(band) {
    var bs = band.tr && band.tr.blocks;
    return (bs && bs.length) ? bs[0] : null;
  }

  ovCanvas.addEventListener('mousemove', function (e) {
    var r = ovCanvas.getBoundingClientRect();
    var hit = ovHit(e.clientX - r.left, e.clientY - r.top);
    if (hit && hit.it) {
      var tr = hit.it.tr, b = hit.it.b;
      ovCanvas.style.cursor = 'pointer';
      ovCanvas.title = tr.title + ' · ' + (b.part || ('P' + b.p)) +
        '（' + fmtCnt(Math.round((b.durMs) / 1000)) + '，点击播放这一局）';
    } else if (hit && hit.band) {
      ovCanvas.style.cursor = 'pointer';
      ovCanvas.title = hit.tr.title + '（点击播放这一场）';
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
    if (hit && hit.it) { playFromOverview(hit.it.tr, hit.it.b); return; }
    if (hit && hit.band) {
      var fb = bandFirstBlock(hit);
      if (fb) { playFromOverview(hit.tr, fb); return; }
      playFromOverview(hit.tr, null); return;
    }
    // 点空白: 跳到该墙钟时刻(分段映射翻译成虚拟轴位置, 落在局间休息时吸附到最近分P)
    if (state.applied) { seekToWallMs(ovMs(x)); }
  });

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
        // 按分P 号精确跳到该局起点; 找不到再用墙钟时刻反查(分段映射)
        var pm = null;
        for (var q = 0; q < state.pageMarks.length; q++) {
          if (state.pageMarks[q].page === block.p) { pm = state.pageMarks[q]; break; }
        }
        if (pm) { seekV(clampSeek(pm.start_sec)); } else { seekToWallMs(block.startMs); }
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
  if (btnOvExpand) {
    btnOvExpand.addEventListener('click', function () { setOvExpanded(!ovExpanded); });
  }

  // 「更多」: 展开低频项(BV / 初始偏移 / 手动微调)。
  if (btnMore && moreBox) {
    btnMore.addEventListener('click', function () {
      var open = moreBox.hidden;
      moreBox.hidden = !open;
      btnMore.textContent = open ? '收起 ▴' : '更多 ▾';
    });
  }


  // 自动校准失败时把「更多」展开, 让手动微调这个兜底入口可见。
  // 原来这里操作的是 <details>.open, 现在手动微调在「更多」里, 语义改成展开 More。
  function updateManualCalibOpen() {
    if (!moreBox || !btnMore) { return; }
    var autoFailed = alignStatus && !alignStatus.hidden &&
                     /失败|不可用|错误/.test(alignStatus.textContent || '');
    if (autoFailed && moreBox.hidden) {
      moreBox.hidden = false;
      btnMore.textContent = '收起 ▴';
    }
  }

  // 手动微调: 对所有分P 施加同一个平移量(delta), 然后重算弹幕映射并保存。
  // 为什么不直接改 state.offsetSec: 有分P 对齐数据时, offset 是"每页一个"的字典,
  // 改单个标量会出现"第一局对上了、第二局还错着"。用 delta 整体平移语义才一致。
  function applyOffsetValue(newOffset, fromAuto) {
    state.offsetSec = newOffset;
    inpOffset.value = String(Math.round(newOffset * 1000) / 1000);
    var elOff = document.getElementById('align-cur');
    if (elOff) { elOff.textContent = 'offset: ' + describePageOffsets(); }
    recomputeRels();
    resetDanmakuIndex();
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
    // 有分P 对齐数据: 平移 delta(不动每页的绝对值); 否则退化为改标量
    if (Object.keys(state.pageOffsets).length) {
      state.offsetDelta += delta;
      var elOff = document.getElementById('align-cur');
      if (elOff) {
        elOff.textContent = 'offset: ' + describePageOffsets() +
          (state.offsetDelta ? ' (微调 ' + (state.offsetDelta > 0 ? '+' : '') +
            state.offsetDelta + 's)' : '');
      }
      recomputeRels();
      resetDanmakuIndex();
      drawOverview();
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
    // 自动校准失败时把"手动校准"折叠面板铺开, 让兜底入口直接可见
    if (cls === 'err') { updateManualCalibOpen(); }
  }

  /* ---------------- 检查更新: 从 GitHub 增量拉数据 ---------------- */

  function setUpdateStatus(msg, cls) {
    updateStatus.hidden = !msg;
    updateStatus.textContent = msg || '';
    updateStatus.className = 'align-status' + (cls ? ' ' + cls : '');
  }

  function fmtMB(bytes) {
    var mb = (bytes || 0) / 1048576;
    return mb >= 1 ? mb.toFixed(1) + ' MB' : Math.max(1, Math.round(mb * 1024)) + ' KB';
  }

  var updateBusy = false;

  // 新数据落盘后必须重载日期下拉, 否则用户看不到刚下的一天。
  // 复用 loadDays(): 它已处理"数据目录日 vs 比赛日"的显示差异与测试会话分组 ——
  // 这里手写一份只会漏掉那些细节。代价是丢失用户当前的日期选择, 但刚下完新数据时
  // 用户本来就想看新的那天, 重置反而是期望行为。
  function reloadDays() {
    loadDays();
  }

  btnUpdate.addEventListener('click', function () {
    if (updateBusy) { return; }
    updateBusy = true;
    btnUpdate.disabled = true;
    btnUpdate.textContent = '检查中…';
    setUpdateStatus('正在检查远端数据…', '');

    getJson('/api/update/check').then(function (j) {
      if (!j || !j.ok) {
        throw new Error(j && j.error ? j.error : '检查失败');
      }
      if (!j.need) {
        setUpdateStatus('已是最新（本地 ' + (j.local_days || []).length +
          ' 天，远端最新 ' + (j.remote_latest || '?') + '）', 'ok');
        return null;
      }
      var days = (j.new_days || []).join('、');
      setUpdateStatus('发现 ' + j.need + ' 天新数据（' + fmtMB(j.new_bytes) +
        '）：' + days + '。开始下载…', '');
      btnUpdate.textContent = '下载中…';
      return fetch('/api/update/pull', { method: 'POST' }).then(function (r) {
        return r.json();
      });
    }).then(function (j) {
      if (!j) { return; }
      if (!j.ok) { throw new Error(j.error || '下载失败'); }
      if (j.verify_problems && j.verify_problems.length) {
        setUpdateStatus(j.message + '；但校验发现 ' + j.verify_problems.length +
          ' 个问题：' + j.verify_problems.slice(0, 3).join('；'), 'err');
      } else {
        setUpdateStatus(j.message + '（' + fmtMB(j.bytes) + '），已刷新日期列表', 'ok');
      }
      if ((j.updated || []).length) { reloadDays(); }
    }).catch(function (e) {
      setUpdateStatus('更新失败：' + (e && e.message ? e.message : '未知错误'), 'err');
    }).then(function () {
      updateBusy = false;
      btnUpdate.disabled = false;
      btnUpdate.textContent = '检查更新';
    });
  });

  // 自动校准: 对齐**这场比赛的全部分P**(page=0)。
  // 历史缺陷: 这里硬编码 page:1, 只对齐第一局; 且只读顶层 offset_sec —— 而后端对多分P
  // 只能返回 pages 字典, 于是 `j.ok && typeof j.offset_sec==='number'` 恒为假,
  // 后端明明算成功, 界面一律显示「自动校准失败: 未知错误」(实测每一天每一场都这样)。
  // 现在: 传 0(后端按 bili_view 返回的 pages 动态决定对齐哪些分P, 支持 Bo1/Bo3/Bo5 任意页数),
  // 并优先读 pages 字典, 顶层字段仅作单分P 兼容。
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
    setAlignStatus('自动校准进行中：正在拉取回放各分P 的音频并与整晚直播指纹比对（首次约半分钟到一分钟）…', '');
    fetch('/api/autocalign', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ date: state.curDate, file: state.curFile, bv: bv, page: 0 })
    }).then(function (r) { return r.json(); }).then(function (j) {
      if (!j || !j.ok) {
        setAlignStatus('自动校准失败: ' + (j && j.error ? j.error : '未知错误'), 'err');
        return;
      }
      applyAutocalignResult(j, bv);
    }).catch(function (e) {
      setAlignStatus('自动校准失败: ' + (e && e.message ? e.message : '网络错误'), 'err');
    }).finally(function () {
      autoAlignBusy = false;
      btnAutoAlign.disabled = false;
      btnAutoAlign.textContent = '自动校准';
    });
  });

  // 处理自动校准返回: 吸收每分P 的 offset, 重建分段映射, 并按"全成功/部分成功"分别提示。
  // 部分成功也是有效结果(某局直播侧确实缺音频), 必须如实显示哪几页没对齐及原因,
  // 而不是笼统一句"失败"让用户以为是功能坏了。
  function applyAutocalignResult(j, bv) {
    var pages = (j && j.pages && typeof j.pages === 'object') ? j.pages : {};
    var okPages = Object.keys(pages);
    var errs = (j && j.errors && typeof j.errors === 'object') ? j.errors : {};
    var errKeys = Object.keys(errs);
    if (!okPages.length && typeof j.offset_sec !== 'number') {
      setAlignStatus('自动校准失败: ' + (j.message || '没有分P 对齐成功'), 'err');
      return;
    }
    // 吸收到当前场次(只有 BV 一致才采纳, 避免把别的场的 offset 套到正在播的场)
    if (bv && state.curBv && state.curBv !== bv) { return; }
    // 后端 pages 的 key 就是分P 号
    var newOffsets = {};
    okPages.forEach(function (k) {
      var pv = pages[k];
      if (pv && typeof pv.offset_sec === 'number') { newOffsets[k] = pv.offset_sec; }
    });
    applyPageOffsets(newOffsets);

    var msg = '自动校准完成：' + okPages.length + '/' + (j.total_pages || okPages.length) +
              ' 个分P 对齐成功';
    var det = okPages.map(function (k) {
      var pv = pages[k];
      return 'P' + k + ' ' + (pv.part || '') + '=' + pv.offset_sec.toFixed(1) + 's' +
             (pv.confidence != null ? '(置信度' + pv.confidence.toFixed(1) + ')' : '') +
             (pv.suspect ? '(疑似重播,已忽略)' : '');
    }).join(', ');
    if (det) { msg += ' — ' + det; }
    if (errKeys.length) {
      msg += '；未对齐: ' + errKeys.map(function (k) { return 'P' + k + '(' + errs[k] + ')'; }).join('; ');
    }
    setAlignStatus(msg, errKeys.length ? 'warn' : 'ok');
  }

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
    // 用户点播放键: 失败要给出可操作提示(直链过期 / 被策略拦), 不能静默
    if (videoEl.paused) { startPlay(); } else { videoEl.pause(); }
  });
  function syncPlayLabel() {
    // 图标按钮: 暂停时显示 ▶, 播放中显示 ‖。
    // 不用 ❚(U+275A)/⏸ 这类生僻字形 —— Windows 上部分字体缺字会变成豆腐块。
    btnPlay.textContent = videoEl.paused ? '▶' : '‖';
    btnPlay.title = (videoEl.paused ? '播放' : '暂停') + '（空格）';
  }
  videoEl.addEventListener('play', syncPlayLabel);
  videoEl.addEventListener('pause', syncPlayLabel);
  videoEl.addEventListener('volumechange', syncMuteIcon);

  videoEl.addEventListener('timeupdate', function () {
    // 判据用**当前分P 的长度**, 不用 state.duration(整场总和):
    // 单分P 播放时 duration 可能为 0/NaN, 那种情况下原来的守卫会直接 return,
    // 进度条就永远不动了。
    if (!((state.segLens[state.segIdx] || 0) > 0)) { return; }
    // 进度条按**当前分P**算, 不按整场总和。
    // 一场回放通常是多个分P(第一局 2905s + 赛后采访 386s ...), 求和后是第一局的
    // 1.3 倍 —— 第一局播完时进度条只走到 76%, 看起来"播完了还没走完";
    // 接着自动切到下一分P 时 getV() 从 2905 跳到 0 附近, 进度条又猛地回退。
    // 用户要看的是"这一局播了多少", 分母就该是这一局的长度。
    syncProgress();
    syncTitleForPlayback();
  });

  function seekByPointer(e) {
    // 拖动只在当前分P 内定位(与进度条的分母保持一致)。
    // 进度条代表"当前分P 的进度", 所以拖动结果也要换算成该分P 内的位置,
    // 再交给 seekV(它接收的是虚拟轴坐标, 会自行换算到具体分P)。
    var segLen = state.segLens[state.segIdx] || 0;
    if (!(segLen > 0)) { return; }
    var rect = prog.getBoundingClientRect();
    var pct = Math.min(1, Math.max(0, (e.clientX - rect.left) / rect.width));
    var segStart = state.segStarts[state.segIdx] || 0;
    seekV(segStart + pct * segLen);
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
    syncMuteIcon();
  });
  // 音量用图标: 新控制条更紧凑, "音量/已静音"四个字会把进度条挤窄。
  function syncMuteIcon() {
    var v = videoEl.muted || videoEl.volume === 0;
    btnMute.textContent = v ? '🔇' : '🔊';
    btnMute.title = v ? '取消静音' : '静音';
    btnMute.classList.toggle('off', v);
  }
  btnMute.addEventListener('click', function () {
    videoEl.muted = !videoEl.muted;
    if (!videoEl.muted && videoEl.volume === 0) { videoEl.volume = 1; rngVol.value = '100'; }
    syncMuteIcon();
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
  var rngDmSpeed = $('rng-dm-speed');
  if (rngDmSpeed) {
    rngDmSpeed.addEventListener('input', function () {
      settings.speed = Math.min(2, Math.max(0.6, (+rngDmSpeed.value) / 100));
      // 速度只影响新入场的弹幕(见 emitDm 里的 v), 场上已有的不跳变, 不重建精灵。
      saveSettings();
    });
  }
  var chkDmStroke = $('chk-dm-stroke');
  if (chkDmStroke) {
    chkDmStroke.addEventListener('change', function () {
      settings.stroke = !!chkDmStroke.checked;
      // 描边是烘焙进精灵的, 改设置后场上旧弹幕仍是旧样式 —— 清空重来最直观
      reflectSettings(); saveSettings();
    });
  }
  var btnDmReset = $('btn-dm-reset');
  if (btnDmReset) {
    btnDmReset.addEventListener('click', function () {
      Object.keys(DM_DEFAULTS).forEach(function (k) { settings[k] = DM_DEFAULTS[k]; });
      reflectSettings(); saveSettings();
      showToast('弹幕设置已恢复默认');
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
    // 防剧透开关初值来自持久化设置(默认开)
    elClock.textContent = fmtClock(new Date());
    syncPlayLabel();
    syncMuteIcon();
    syncQualityBtn();
    syncSpoilerBtn();
    setOvExpanded(false);          // 总览默认紧凑(钉顶, 只显示密度 + 场次色带)
    loadDays();
    requestAnimationFrame(loop);
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();




