/* 发布进程开关 + 校准提交卡片。
   依赖 app.js 的 $ / api / toast（同页经典脚本，全局可见）。 */
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

function renderCalState(sch) {
  const lists = (sch && sch.lists) || {};
  const align = (sch && sch.align) || {};
  $('cal-date').textContent = (sch && sch.date) || '—';
  const parts = [];
  Object.keys(lists).sort().forEach((matchDate) => {
    (lists[matchDate] || []).forEach((it) => {
      const pages = it.pages && typeof it.pages === 'object'
        ? Object.keys(it.pages).length : 0;
      const total = it.total_pages || 0;
      let text = (it.title || '未命名') + '：';
      if (!it.bv) {
        text += '无回放';
      } else if (pages) {
        text += '已对齐 ' + pages + '/' + (total || '?') + 'P';
      } else {
        const rec = findAlign(align, matchDate, it.bv);
        text += rec && rec.align_failed ? '对齐失败' : '未对齐';
      }
      parts.push(text);
    });
  });
  $('cal-state').textContent = parts.length ? parts.join(' · ') : '还没有场次';
}

function findAlign(align, date, bv) {
  const exact = align[date + '|' + bv] || align[date + '|7734200|' + bv];
  if (exact && typeof exact === 'object') { return exact; }
  let hit = null;
  Object.keys(align).forEach((k) => {
    const v = align[k];
    if (v && typeof v === 'object' && String(v.bv || '') === bv) { hit = hit || v; }
  });
  return hit;
}

async function loadCalibration() {
  try { renderCalState(await api('/api/schedule')); }
  catch (e) { $('cal-state').textContent = e.message; }
}

async function calibrateNow() {
  const btn = $('btn-calibrate');
  const box = $('cal-output');
  btn.disabled = true;
  btn.textContent = '校准中…';
  box.hidden = false;
  box.textContent = '正在发现赛程、逐分P 对齐、分发并提交…（可能需要几分钟）';
  try {
    const j = await api('/api/calibrate', { method: 'POST' });
    box.textContent = (j.lines || []).join('\n') || j.output || '完成';
    toast('校准与提交已完成');
    await loadCalibration();
    if (typeof loadPush === 'function') { await loadPush(); }
  } catch (e) {
    box.textContent = (e.message || '校准失败') +
      (e.detail ? ('\n\n' + e.detail) : '');
    toast(e.message, false);
  } finally {
    btn.disabled = false;
    btn.textContent = '校准并提交';
    await loadCalibration();
  }
}

function bindPipeline() {
  $('btn-pipe-start').addEventListener('click', () => pipeAction('start'));
  $('btn-pipe-stop').addEventListener('click', () => pipeAction('stop'));
  $('btn-calibrate').addEventListener('click', calibrateNow);
  $('btn-cal-refresh').addEventListener('click', loadCalibration);
}

document.addEventListener('DOMContentLoaded', () => {
  bindPipeline();
  loadPipeline();
  loadCalibration();
  setInterval(loadPipeline, 30000);
  setInterval(loadCalibration, 30000);
});
