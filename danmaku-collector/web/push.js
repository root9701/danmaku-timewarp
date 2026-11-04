/* 数据推送卡片: 采集进度、小时数据块、待推提交、自动推送设置。
   依赖 app.js 的 $ / api / toast（同页经典脚本，全局可见）。 */
'use strict';

function fmtInt(n) {
  const v = Number(n);
  return Number.isFinite(v) ? v.toLocaleString('zh-CN') : '0';
}

function fmtCommit(s) {
  const m = String(s || '').match(/^(\S+)\s+\S+\s+(\d{2}:\d{2})/);
  return m ? (m[1] + ' ' + m[2]) : (s || '—');
}

function badgeClass(state) {
  if (state === '已推送') { return 'ok'; }
  if (state === '推送失败') { return 'fail'; }
  if (state === '采集中') { return 'live'; }
  return 'wait';
}

function renderBlocks(blocks, multipleRooms) {
  const tbody = $('push-blocks');
  tbody.innerHTML = '';
  // 空片(0 条且已推送)通常是收尾时补的 40 字节占位, 不占一行;
  // 采集中/待提交的空块仍要显示, 否则当前小时会“消失”。
  const shown = (blocks || []).filter((b) =>
    b.count > 0 || b.sc_count > 0 || b.active || b.state !== '已推送');
  if (!shown.length) {
    const tr = document.createElement('tr');
    const td = document.createElement('td');
    td.colSpan = 4;
    td.className = 'empty';
    td.textContent = '还没有数据块';
    tr.appendChild(td);
    tbody.appendChild(tr);
    return;
  }
  shown.forEach((b) => {
    const tr = document.createElement('tr');
    if (b.active) { tr.className = 'active'; }
    const time = document.createElement('td');
    time.textContent = multipleRooms ? (b.room + ' · ' + b.label) : b.label;
    const count = document.createElement('td');
    count.className = 'num';
    count.textContent = fmtInt(b.count);
    if (b.sc_count) {
      const sc = document.createElement('span');
      sc.className = 'sub';
      sc.textContent = ' +' + b.sc_count + ' SC';
      count.appendChild(sc);
    }
    const state = document.createElement('td');
    const badge = document.createElement('span');
    badge.className = 'badge ' + badgeClass(b.state);
    badge.textContent = b.state;
    state.appendChild(badge);
    const push = document.createElement('td');
    push.className = 'sub';
    push.textContent = fmtCommit(b.commit);
    push.title = b.name || '';
    tr.appendChild(time);
    tr.appendChild(count);
    tr.appendChild(state);
    tr.appendChild(push);
    tbody.appendChild(tr);
  });
}

function renderCommits(commits) {
  const box = $('push-commits');
  box.innerHTML = '';
  if (!commits || !commits.length) {
    const empty = document.createElement('div');
    empty.className = 'sub';
    empty.textContent = '没有待推提交';
    box.appendChild(empty);
    return;
  }
  commits.forEach((c) => {
    const row = document.createElement('div');
    row.className = 'commit';
    const tag = document.createElement('span');
    tag.className = 'tag ' + (c.kind === 'data' ? 'data' : 'code');
    tag.textContent = c.kind === 'data' ? '数据' : '代码';
    const text = document.createElement('span');
    text.textContent = ' ' + c.sha + '  ' + (c.subject || '') +
      (c.kind === 'data' ? '' : '  ← 需人工推送');
    text.title = (c.when || '') + (c.paths && c.paths.length ? '\n' + c.paths.join('\n') : '');
    row.appendChild(tag);
    row.appendChild(text);
    box.appendChild(row);
  });
}

function renderCalibration(cal) {
  const box = $('push-calibration');
  if (!cal) { box.textContent = ''; return; }
  if (cal.ok && cal.items && cal.items.length) {
    box.textContent = '校准：' + cal.items.map((i) => {
      const n = i.aligned || 0;
      const t = i.total || 0;
      return (i.title || '未命名') + ' ' + (n ? ('已对齐 ' + n + '/' + t + 'P') : '未对齐');
    }).join(' · ');
  } else {
    box.textContent = '校准：' + (cal.message || '还没有对齐记录');
  }
}

function renderPush(j) {
  const s = j.settings || {};
  $('push-auto').checked = !!s.auto;
  $('push-retries').value = s.retries ?? 3;
  $('push-delay').value = s.delay_sec ?? 10;
  $('push-auto-text').textContent = s.auto ? '开' : '关';
  $('push-remote').textContent = j.remote_head
    ? ('origin/main @ ' + j.remote_head) : '—';

  const pending = j.pending_commits || [];
  const dataN = typeof j.data_pending === 'number' ? j.data_pending : 0;
  const codeN = typeof j.code_pending === 'number' ? j.code_pending : 0;
  let summary;
  if (j.error) {
    summary = j.error;
  } else if (j.branch && j.branch !== 'main') {
    summary = '当前分支 ' + j.branch + '，只推 main';
  } else if (pending.length) {
    summary = '待推 ' + pending.length + ' 个（数据 ' + dataN +
      ' / 代码 ' + codeN + '）';
    if (codeN) { summary += ' · 代码提交需人工推送'; }
  } else {
    summary = '没有待推送';
  }
  const total = (j.blocks || []).reduce((s, b) => s + (b.count || 0), 0);
  if (total) { summary += ' · 共 ' + fmtInt(total) + ' 条'; }
  const scopeDate = j.scope && j.scope.date ? ('数据日 ' + j.scope.date + ' · ') : '';
  $('push-summary').textContent = scopeDate + summary;

  const last = j.last_push || {};
  if (last.message) {
    $('push-result').textContent = (last.time ? last.time + ' ' : '') + last.message;
  } else {
    $('push-result').textContent = j.last_log || '还没有推送记录';
  }

  const multipleRooms = (j.scope && j.scope.rooms && j.scope.rooms.length > 1);
  renderBlocks(j.blocks || [], multipleRooms);
  renderCalibration(j.calibration);
  renderCommits(pending);

  const btn = $('btn-push-now');
  btn.disabled = !j.can_push || !!j.running;
  btn.title = j.running ? '正在推送'
    : (pending.length && !j.data_only ? '含代码/文档提交，需人工推送'
      : (!pending.length ? '没有待推数据' : ''));

  if (j.last_log) {
    const box = $('push-log');
    box.textContent = j.last_log;
    box.hidden = false;
  }
}

async function loadPushLog() {
  try {
    const j = await api('/api/push/log');
    const lines = j.lines || [];
    const box = $('push-log');
    box.textContent = lines.length ? lines.join('\n') : '（还没有推送记录）';
    box.hidden = false;
  } catch (e) { /* 日志拉不到不打扰用户, 卡片上已有最近结果 */ }
}

async function loadPush() {
  try { renderPush(await api('/api/push')); }
  catch (e) { $('push-summary').textContent = e.message; }
}

async function pushNow() {
  const btn = $('btn-push-now');
  btn.disabled = true;
  btn.textContent = '推送中…';
  try {
    const j = await api('/api/push', { method: 'POST' });
    toast(j.message || '已推送');
  } catch (e) {
    $('push-summary').textContent = e.message;
    toast(e.message, false);
  } finally {
    btn.textContent = '立即推送';
    await loadPush();
  }
}

async function savePushSettings() {
  const body = {
    auto: $('push-auto').checked,
    retries: parseInt($('push-retries').value, 10) || 3,
    delay_sec: parseInt($('push-delay').value, 10) || 10
  };
  try {
    await api('/api/push/settings', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    });
    toast('推送设置已保存，下一轮巡检生效');
    await loadPush();
  } catch (e) { toast(e.message, false); }
}

function bindPush() {
  $('btn-push-now').addEventListener('click', pushNow);
  $('btn-push-refresh').addEventListener('click', loadPush);
  $('btn-save-push').addEventListener('click', savePushSettings);
  $('push-log').addEventListener('click', loadPushLog);
}

document.addEventListener('DOMContentLoaded', () => {
  bindPush();
  loadPush();
  loadPushLog();
});
