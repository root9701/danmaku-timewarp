/* 数据推送卡片: 手动推送 + 自动推送设置。
   依赖 app.js 的 $ / api / toast（同页经典脚本，全局可见）。 */
'use strict';

function renderPush(j) {
  const s = j.settings || {};
  $('push-auto').checked = !!s.auto;
  $('push-retries').value = s.retries ?? 3;
  $('push-delay').value = s.delay_sec ?? 10;
  $('push-auto-text').textContent = s.auto ? '开' : '关';
  if (j.error) {
    $('push-pending').textContent = '—';
    $('push-result').textContent = j.error;
    return;
  }
  if (j.branch && j.branch !== 'main') {
    $('push-pending').textContent = '—';
    $('push-result').textContent = '当前分支 ' + j.branch + '，只推 main';
  } else {
    const pending = j.pending || 0;
    $('push-pending').textContent = pending ? (pending + ' 个') : '无';
    $('push-result').textContent = pending
      ? (j.data_only ? '可推送' : '含非数据改动，需人工推送')
      : '无待推送';
  }
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
  catch (e) { $('push-result').textContent = e.message; }
}

async function pushNow() {
  const btn = $('btn-push-now');
  btn.disabled = true;
  btn.textContent = '推送中…';
  try {
    const j = await api('/api/push', { method: 'POST' });
    await loadPush();
    $('push-result').textContent = j.message || '已推送';
    toast(j.message || '已推送');
  } catch (e) {
    $('push-result').textContent = e.message;
    toast(e.message, false);
  } finally {
    btn.disabled = false;
    btn.textContent = '立即推送';
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
