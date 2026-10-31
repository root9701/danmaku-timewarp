/* 采集控制台交互: 任务队列 / 赛程 / 运行控制 / 日志。无框架、无构建链。 */
'use strict';

const $ = (id) => document.getElementById(id);
let taskCache = [];
let dataDates = [];
let cfgCache = {};

function toast(msg, ok = true) {
  const el = $('toast');
  el.textContent = msg;
  el.style.borderLeftColor = ok ? 'var(--blue)' : 'var(--warn)';
  el.style.display = 'block';
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { el.style.display = 'none'; }, 5000);
}

async function api(path, opts) {
  const r = await fetch(path, opts);
  let j;
  try {
    j = await r.json();
  } catch (e) {
    throw new Error('服务返回异常');
  }
  if (!j || j.ok === false) { throw new Error((j && j.error) || '请求失败'); }
  return j;
}

function td(text, cls) {
  const cell = document.createElement('td');
  if (text !== undefined && text !== null) { cell.textContent = String(text); }
  if (cls) { cell.className = cls; }
  return cell;
}

function input(value, cls, type) {
  const el = document.createElement('input');
  el.value = value == null ? '' : String(value);
  if (cls) { el.className = cls; }
  if (type) { el.type = type; }
  return el;
}

const STATUS_TEXT = {
  pending: '待执行', running: '采集中', done: '已完成',
  stopped: '已结束', missed: '已错过'
};

function statusCell(status) {
  const label = STATUS_TEXT[status] || status || '待执行';
  const cls = status === 'running' ? 'status running'
    : (status === 'missed' || status === 'stopped') ? 'status warn' : 'status';
  return td(label, cls);
}

/* ---------- 任务表 ---------- */
function renderTasks(tasks, dates) {
  taskCache = tasks || [];
  dataDates = dates || [];
  const tbody = $('task-rows');
  tbody.textContent = '';
  taskCache.forEach((t, i) => {
    const tr = document.createElement('tr');
    tr.appendChild(td('')).appendChild(input(t.date, '', 'date'));
    tr.appendChild(td('')).appendChild(input(t.start, 'narrow', 'time'));
    tr.appendChild(td('')).appendChild(input(t.end, 'narrow', 'time'));
    tr.appendChild(td('')).appendChild(input((t.rooms || []).join(','), 'rooms'));
    const write = td(dataDates[i] ? ('data/' + dataDates[i]) : '—');
    write.className = 'status';
    tr.appendChild(write);
    tr.appendChild(statusCell(t.status));
    const op = td('');
    const del = document.createElement('button');
    del.textContent = '删除';
    del.className = 'ghost';
    del.addEventListener('click', () => {
      taskCache.splice(i, 1);
      renderTasks(taskCache, dataDates);
    });
    op.appendChild(del);
    tr.appendChild(op);
    tbody.appendChild(tr);
  });
    $('task-empty').hidden = taskCache.length > 0;
}

function collectTasks() {
  const rows = Array.from($('task-rows').querySelectorAll('tr'));
  return rows.map((tr, i) => {
    const ins = tr.querySelectorAll('input');
    const rooms = ins[3].value.split(/[,，\s]+/)
      .map((x) => parseInt(x, 10)).filter((x) => Number.isFinite(x) && x > 0);
    const base = taskCache[i] || {};
    return Object.assign({}, base, {
      date: ins[0].value, start: ins[1].value, end: ins[2].value,
      rooms: rooms, enabled: base.enabled !== false,
      status: base.status || 'pending'
    });
  });
}

async function loadTasks() {
  const j = await api('/api/tasks');
  renderTasks(j.tasks, j.data_dates);
}

async function saveTasks() {
  const tasks = collectTasks();
  await api('/api/tasks', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ tasks: tasks })
  });
  toast('任务已保存');
  await loadTasks();
}

function addTask() {
  const today = new Date();
  const iso = today.toISOString().slice(0, 10);
  taskCache.push({
    date: iso, start: '00:30', end: '09:00',
    rooms: (cfgCache.rooms || []).slice(), enabled: true, status: 'pending'
  });
  dataDates.push('');
  renderTasks(taskCache, dataDates);
}

/* ---------- 运行控制 ---------- */
async function startQueue() {
  try {
    await api('/api/queue/start', { method: 'POST' });
    toast('队列已启动');
  } catch (e) { toast(e.message, false); }
}

async function stopQueue() {
  try {
    await api('/api/queue/stop', { method: 'POST' });
    toast('正在停止队列');
  } catch (e) { toast(e.message, false); }
}

async function stopTask() {
  try {
    await api('/api/stop', { method: 'POST' });
    toast('正在结束本次采集');
  } catch (e) { toast(e.message, false); }
}

async function extend(minutes) {
  try {
    const j = await api('/api/extend', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ minutes: minutes })
    });
    toast('已延长到 ' + j.end);
  } catch (e) { toast(e.message, false); }
}

async function runTest() {
  const seconds = Math.max(10, Math.min(600, parseInt($('test-sec').value, 10) || 60));
  try {
    await api('/api/start', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode: 'test', seconds: seconds })
    });
    toast('测试采集已开始');
  } catch (e) { toast(e.message, false); }
}

/* ---------- 配置 ---------- */
async function loadConfig() {
  const j = await api('/api/config');
  cfgCache = j.config || {};
  $('cfg-rooms').value = (cfgCache.rooms || []).join(',');
  $('cfg-poll').value = cfgCache.poll_interval_sec ?? 30;
}

async function saveConfig() {
  const rooms = $('cfg-rooms').value.split(/[,，\s]+/)
    .map((x) => parseInt(x, 10)).filter((x) => Number.isFinite(x) && x > 0);
  try {
    await api('/api/config', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        rooms: rooms,
        poll_interval_sec: parseInt($('cfg-poll').value, 10) || 30,
        earliest_start: cfgCache.earliest_start || 'now',
        latest_end: cfgCache.latest_end || ''
      })
    });
    toast('配置已保存；队列任务不受影响');
    await loadConfig();
  } catch (e) { toast(e.message, false); }
}

/* ---------- 赛程 ---------- */
function fmtStart(ms) {
  if (!ms) { return '—'; }
  try {
    return new Date(ms).toLocaleString('zh-CN',
      { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' });
  } catch (e) { return '—'; }
}

function renderSchedule(j) {
  const tbody = $('schedule-rows');
  tbody.textContent = '';
  $('schedule-date').textContent = j.date ? ('数据日 ' + j.date) : '';
  const groups = j.lists || {};
  let count = 0;
  Object.keys(groups).sort().forEach((matchDate) => {
    (groups[matchDate] || []).forEach((it) => {
      const tr = document.createElement('tr');
      tr.appendChild(td(fmtStart(it.start_ms)));
      tr.appendChild(td(it.title || ''));
      tr.appendChild(td(it.bv ? it.bv : '未找到回放', it.bv ? '' : 'status warn'));
      const pages = it.pages && typeof it.pages === 'object' ? Object.keys(it.pages).length : 0;
      tr.appendChild(td(pages ? (pages + '/' + (it.total_pages || '?')) : '未对齐',
        pages ? 'status' : 'status warn'));
      tbody.appendChild(tr);
      count += 1;
    });
  });
  $('schedule-empty').hidden = count > 0;
}

async function loadSchedule() {
  try {
    renderSchedule(await api('/api/schedule'));
  } catch (e) { toast(e.message, false); }
}

async function refreshSchedule() {
  const btn = $('btn-refresh-schedule');
  btn.disabled = true;
  btn.textContent = '刷新中…';
  try {
    const j = await api('/api/schedule/refresh', { method: 'POST' });
    renderSchedule(j);
    toast('赛程已刷新');
  } catch (e) {
    toast(e.message, false);
  } finally {
    btn.disabled = false;
    btn.textContent = '刷新赛程';
  }
}

/* ---------- 状态轮询 ---------- */
function renderResult(r) {
  const box = $('result');
  box.textContent = '';
  if (!r) {
    const k = document.createElement('span'); k.className = 'k'; k.textContent = '状态';
    const v = document.createElement('span'); v.textContent = '暂无';
    box.append(k, v);
    return;
  }
  const rows = [
    ['房间', r.room], ['弹幕', r.dm_count], ['SC', r.sc_count],
    ['开始', r.record_start], ['结束', r.record_end], ['收尾', r.ended_by]
  ];
  rows.forEach(([k, v]) => {
    const ke = document.createElement('span'); ke.className = 'k'; ke.textContent = k;
    const ve = document.createElement('span'); ve.textContent = v == null ? '—' : String(v);
    box.append(ke, ve);
  });
}

async function refreshStatus() {
  let j;
  try {
    j = await api('/api/status');
  } catch (e) { return; }
  const running = !!j.running;
  const queued = !!j.queue_running;
  const pill = $('status');
  pill.textContent = running ? '采集中' : (queued ? '队列待命' : '空闲');
  pill.className = 'pill' + (running ? ' run' : (queued ? '' : ''));
  $('queue-info').textContent = queued ? '队列运行中' : '队列未启动';

  const t = j.current_task;
  $('current-task').textContent = t
    ? (t.date + ' ' + t.start + ' – ' + t.end) : '无';
  $('deadline').textContent = j.deadline || '—';
  $('btn-stop-task').disabled = !running;
  $('btn-extend-30').disabled = !running || !j.deadline;
  $('btn-extend-60').disabled = !running || !j.deadline;
  $('btn-queue-start').disabled = queued;
  $('btn-queue-stop').disabled = !queued;

  const log = $('log');
  if (j.log_tail) {
    const stick = log.scrollTop + log.clientHeight >= log.scrollHeight - 4;
    log.textContent = j.log_tail;
    if (stick) { log.scrollTop = log.scrollHeight; }
  }
  renderResult(j.last_result);
}

async function showDanmaku() {
  try {
    const j = await api('/api/danmaku');
    const box = $('dm-list');
    box.textContent = '';
    (j.rows || []).forEach((row) => {
      const line = document.createElement('div');
      line.textContent = row.un ? (row.un + '：' + row.t) : String(row.t);
      box.appendChild(line);
    });
    box.hidden = false;
  } catch (e) { toast(e.message, false); }
}

function bind() {
  $('btn-add-task').addEventListener('click', addTask);
  $('btn-save-tasks').addEventListener('click', saveTasks);
  $('btn-queue-start').addEventListener('click', startQueue);
  $('btn-queue-stop').addEventListener('click', stopQueue);
  $('btn-stop-task').addEventListener('click', stopTask);
  $('btn-extend-30').addEventListener('click', () => extend(30));
  $('btn-extend-60').addEventListener('click', () => extend(60));
  $('btn-test').addEventListener('click', runTest);
  $('btn-save-config').addEventListener('click', saveConfig);
  $('btn-refresh-schedule').addEventListener('click', refreshSchedule);
  $('btn-danmaku').addEventListener('click', showDanmaku);
}

async function init() {
  bind();
  try { await loadConfig(); } catch (e) { toast(e.message, false); }
  try { await loadTasks(); } catch (e) { toast(e.message, false); }
  await loadSchedule();
  await refreshStatus();
  setInterval(refreshStatus, 2000);
}

document.addEventListener('DOMContentLoaded', init);
