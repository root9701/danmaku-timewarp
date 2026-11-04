/* 采集时钟/进度: 当前时间、计划起止、已运行/剩余。
   依赖 app.js 的 $（同页经典脚本，全局可见）。 */
'use strict';

function pad2(n) { return String(n).padStart(2, '0'); }

function fmtClock(d) {
  return pad2(d.getHours()) + ':' + pad2(d.getMinutes()) + ':' + pad2(d.getSeconds());
}

function fmtDateHM(d) {
  return (d.getMonth() + 1) + '-' + d.getDate() + ' ' +
    pad2(d.getHours()) + ':' + pad2(d.getMinutes());
}

function fmtSpan(sec) {
  sec = Math.max(0, Math.round(sec));
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  return h ? (h + '小时' + m + '分') : (m + '分');
}

function renderPlan(j) {
  const now = new Date();
  $('now-text').textContent = fmtClock(now);
  const start = j.plan_start_ts;
  const end = j.plan_end_ts;
  const kind = j.task_kind || 'none';
  if (!start) {
    $('plan-text').textContent = '没有计划任务';
    $('elapsed-text').textContent = '—';
    $('remaining-text').textContent = '—';
    $('clock').textContent = '现在 ' + fmtClock(now);
    $('plan-bar').style.width = '0%';
    return;
  }
  const s = new Date(start);
  const e = end ? new Date(end) : null;
  const endText = e ? (pad2(e.getHours()) + ':' + pad2(e.getMinutes())) : '';
  $('plan-text').textContent = (kind === 'next' ? '下一场 ' : '') +
    fmtDateHM(s) + (endText ? (' – ' + endText) : '');
  const total = e ? Math.max(1, (e - s) / 1000) : 0;
  const elapsed = Math.max(0, (now - s) / 1000);
  const remain = e ? Math.max(0, (e - now) / 1000) : null;
  if (kind === 'next' && now < s) {
    const wait = (s - now) / 1000;
    $('elapsed-text').textContent = '尚未开始';
    $('remaining-text').textContent = '距开始 ' + fmtSpan(wait);
    $('clock').textContent = '现在 ' + fmtClock(now) + ' · 距开始 ' + fmtSpan(wait);
    $('plan-bar').style.width = '0%';
    return;
  }
  $('elapsed-text').textContent = '已运行 ' + fmtSpan(elapsed);
  if (remain !== null) {
    $('remaining-text').textContent = remain > 0
      ? ('剩余 ' + fmtSpan(remain)) : '已到截止时间';
  } else {
    $('remaining-text').textContent = '未设截止';
  }
  $('clock').textContent = '现在 ' + fmtClock(now) +
    (remain !== null && remain > 0 ? (' · 剩余 ' + fmtSpan(remain)) : '');
  const pct = total ? Math.min(100, Math.max(0, elapsed / total * 100)) : 0;
  $('plan-bar').style.width = pct.toFixed(1) + '%';
}
