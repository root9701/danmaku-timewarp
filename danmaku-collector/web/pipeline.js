/* 发布进程开关 + 校准 + 发布。
   依赖 app.js 的 $ / api / toast（同页经典脚本，全局可见）。

   校准与发布是**两个独立动作**, 不共用一个按钮:
     校准 = 发现赛程 + 逐分P 对齐, 只写本地层, 可以反复重算;
     发布 = 分发到 data/ + 提交 + 按开关推送, 过推送闸门。
   混在一起时用户点完不知道"算完了没有"和"提交了没有"。
 */
'use strict';

function fmtAge(sec) {
  const n = Number(sec);
  if (!Number.isFinite(n) || n < 0) { return '—'; }
  if (n < 60) { return n + ' 秒前'; }
  const m = Math.floor(n / 60);
  return m < 60 ? (m + ' 分钟前') : (Math.floor(m / 60) + ' 小时前');
}

function renderPipeline(p) {
  const info = p || {};
  const alive = !!info.alive;
  $('pipe-state').textContent = alive
    ? ('运行中 · PID ' + (info.pid || '?'))
    : (info.stale ? '已停止（心跳过期）' : '未运行');
  $('pipe-interval').textContent = info.interval
    ? (Math.round(info.interval / 60) + ' 分钟') : '—';
  $('pipe-heartbeat').textContent = info.age_sec !== undefined
    ? fmtAge(info.age_sec) : '—';
  $('pipe-hint').textContent = alive
    ? (info.owned ? '由本控制台启动。' : '由外部启动（终端/计划任务），控制台只监控。')
    : '';
  $('btn-pipe-start').disabled = alive;
  $('btn-pipe-stop').disabled = !alive;
}

async function loadPipeline() {
  try { renderPipeline(await api('/api/pipeline')); }
  catch (e) { $('pipe-state').textContent = e.message; }
}

async function pipeAction(action) {
  const btn = action === 'start' ? $('btn-pipe-start') : $('btn-pipe-stop');
  const old = btn.textContent;
  btn.disabled = true;
  btn.textContent = action === 'start' ? '启动中…' : '停止中…';
  try {
    await api('/api/pipeline', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action })
    });
    toast(action === 'start' ? '发布进程已启动' : '发布进程已停止');
  } catch (e) {
    toast(e.message, false);
  } finally {
    btn.textContent = old;
    await loadPipeline();
  }
}

/* ---------------- 校准卡片 ---------------- */

function calBadge(state) {
  if (state === '已校准') { return 'ok'; }
  if (state === '未校准') { return 'wait'; }
  return 'live';                       // 无场次: 还没发现到比赛
}

function renderCalibration(j) {
  const info = j || {};
  $('cal-date').textContent = info.date || '—';
  $('cal-state').textContent = info.state || '—';
  const n = info.aligned || 0;
  const t = info.total || 0;
  $('cal-progress').textContent = t
    ? (n + '/' + t + ' 个分P 已对齐')
    : (info.items && info.items.length ? '还没有分P 对齐' : '—');

  const tbody = $('cal-rows');
  tbody.innerHTML = '';
  const items = info.items || [];
  if (!items.length) {
    const tr = document.createElement('tr');
    const td = document.createElement('td');
    td.colSpan = 3;
    td.className = 'empty';
    td.textContent = '这一天还没有场次（先「刷新赛程」或等夜间巡检发现）';
    tr.appendChild(td);
    tbody.appendChild(tr);
  } else {
    items.forEach((it) => {
      const tr = document.createElement('tr');
      const title = document.createElement('td');
      title.textContent = it.title || '未命名';
      if (!it.bv) { title.title = '这条场次还没有回放 BV'; }
      const pages = document.createElement('td');
      pages.className = 'num';
      pages.textContent = it.total ? (it.aligned + '/' + it.total + 'P') : '—';
      const state = document.createElement('td');
      const badge = document.createElement('span');
      let text = '未校准';
      if (!it.bv) { text = '无回放'; }
      else if (it.aligned) { text = '已校准'; }
      else if (it.failed) { text = '对齐失败'; }
      badge.className = 'badge ' + (it.bv ? (it.aligned ? 'ok' : 'wait') : 'live');
      badge.textContent = text;
      state.appendChild(badge);
      tr.appendChild(title);
      tr.appendChild(pages);
      tr.appendChild(state);
      tbody.appendChild(tr);
    });
  }

  const btn = $('btn-calibrate');
  const busy = !!info.calibrating;
  btn.disabled = busy;
  btn.textContent = busy ? '校准中…' : '开始校准';
}

async function loadCalibration() {
  try {
    const j = await api('/api/calibration');
    renderCalibration(j);
    // 「发布」按钮的可用性跟着校准状态走: 没校准就不该点发布。
    const ready = (j.aligned || 0) > 0;
    $('pub-date').textContent = j.date || '—';
    $('pub-ready').textContent = ready ? '可以发布' : '未校准，发布会被闸门拦下';
    $('btn-publish').disabled = !ready || !!j.publishing;
    $('btn-publish').textContent = j.publishing ? '发布中…' : '发布';
  } catch (e) {
    $('cal-state').textContent = e.message;
  }
}

async function calibrateNow() {
  const btn = $('btn-calibrate');
  const box = $('cal-output');
  btn.disabled = true;
  btn.textContent = '校准中…';
  box.hidden = false;
  box.textContent = '正在发现赛程、逐分P 对齐…（不提交、不推送）';
  try {
    const j = await api('/api/calibrate', { method: 'POST' });
    box.textContent = (j.lines || []).join('\n') || j.output || '完成';
    toast('校准完成（结果在本地，尚未提交）');
  } catch (e) {
    box.textContent = (e.message || '校准失败') +
      (e.detail ? ('\n\n' + e.detail) : '');
    toast(e.message, false);
  } finally {
    btn.disabled = false;
    btn.textContent = '开始校准';
    await loadCalibration();
    if (typeof loadPush === 'function') { await loadPush(); }
  }
}

async function publishNow() {
  const btn = $('btn-publish');
  const box = $('pub-output');
  btn.disabled = true;
  btn.textContent = '发布中…';
  box.hidden = false;
  box.textContent = '正在分发、提交、按开关推送…';
  try {
    const j = await api('/api/publish', { method: 'POST' });
    box.textContent = (j.lines || []).join('\n') || j.output || '完成';
    toast('发布完成');
  } catch (e) {
    box.textContent = (e.message || '发布失败') +
      (e.detail ? ('\n\n' + e.detail) : '');
    toast(e.message, false);
  } finally {
    btn.textContent = '发布';
    await loadCalibration();
    if (typeof loadPush === 'function') { await loadPush(); }
  }
}

function bindPipeline() {
  $('btn-pipe-start').addEventListener('click', () => pipeAction('start'));
  $('btn-pipe-stop').addEventListener('click', () => pipeAction('stop'));
  $('btn-calibrate').addEventListener('click', calibrateNow);
  $('btn-cal-refresh').addEventListener('click', loadCalibration);
  $('btn-publish').addEventListener('click', publishNow);
}

document.addEventListener('DOMContentLoaded', () => {
  bindPipeline();
  loadPipeline();
  loadCalibration();
  setInterval(loadPipeline, 30000);
  setInterval(loadCalibration, 30000);
});
