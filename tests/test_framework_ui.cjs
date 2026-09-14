// Run the shipped UI functions with a small DOM/request harness; no browser dependency.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8');

// Experience records marked needs_evidence are terminal for the current
// immutable revision.  Only fresh candidates may render review controls.
assert.match(source, /const review = isExperience && state === "candidate"/);
assert.doesNotMatch(source, /\["candidate", "needs_evidence"\]\.includes\(state\)/);
assert.match(source, /candidate\.prompt_fragment_change/);
assert.match(source, /查看 Prompt Fragment 前后变化/);

function section(start, end) {
  const first = source.indexOf(start);
  const last = source.indexOf(end, first + start.length);
  assert.ok(first >= 0 && last > first, `UI function boundaries missing: ${start}`);
  return source.slice(first, last);
}

const nodes = new Map();
function element(selector) {
  if (!nodes.has(selector)) {
    const classes = new Set(['hidden']);
    nodes.set(selector, {
      textContent: '', innerHTML: '', value: '', placeholder: '', className: '', dataset: {},
      classList: {
        add: name => classes.add(name), remove: name => classes.delete(name),
        contains: name => classes.has(name),
        toggle(name, force) { if (force) classes.add(name); else classes.delete(name); },
      },
      scrollIntoView() {}, focus() {},
    });
  }
  return nodes.get(selector);
}
const storage = new Map();
const requests = [];
let transportFailure = false;
let nextFailure = null;
let delayRequest = null;
const context = vm.createContext({
  $: element, $$: () => [], activeClarification: null, pendingText2SQLQuery: null, queryInFlight: false,
  activeTaskId: '', activeSql: '', activeQueryType: 'DATA_QUERY',
  text2sqlSessionId: 'test-session', runtimeStatus: { ready: true },
  governanceMode: false, reduceMotion: { matches: true }, runtimeNodeCatalog: Array(11).fill({}),
  sessionStorage: { setItem: (key, value) => storage.set(key, value), removeItem: key => storage.delete(key) },
  escapeHtml: value => String(value).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;'),
  number: value => Number(value || 0),
  answerSummary: answer => ({ value: String(answer.rows?.[0]?.[0] ?? ''), meta: '1 行' }),
  renderTable: () => '', renderVisualization() {}, renderRuntimeMap: () => '',
  renderAgentTrace: () => '', renderProtocolSummary: () => '', renderPins() {}, resetFeedback() {},
  addSession() {}, loadTraces() {}, loadMemory() {}, setBusy() {}, toast() {}, writePendingQuery() {},
  async api(url, options) {
    const body = JSON.parse(options.body);
    requests.push(body);
    if (nextFailure) { const error = nextFailure; nextFailure = null; throw error; }
    if (delayRequest) await delayRequest;
    if (transportFailure) { transportFailure = false; throw new Error('connection lost'); }
    const waiting = requests.length === 1;
    return {
      task_id: body.task_id, question: body.question,
      query_type: waiting ? 'CLARIFICATION' : 'DATA_QUERY',
      status: waiting ? 'needs_clarification' : 'success',
      clarification: waiting ? { stage: 'routing', questions: ['需要统计哪个岩爆等级？'] } : {},
      gates: { accepted: !waiting, errors: [] },
      answer: waiting ? {} : { columns: ['n'], rows: [[6]], row_count: 1 },
      final_sql: waiting ? '' : 'SELECT COUNT(*) AS n FROM cases', execution: {},
    };
  },
});

vm.runInContext([
  section('function setClarification(', '\nfunction escapeHtml('),
  section('function renderResult(', '\nfunction resetFeedback('),
  section('function runtimeNodeState(', '\nfunction runtimeStateLabel('),
  section('async function submitQuestion(', "\n$$('.nav-item[data-view]')"),
].join('\n'), context);

(async () => {
  element('#text2sql-question').value = '岩爆案例有多少个';
  await context.submitQuestion();
  assert.equal(element('#text2sql-answer-summary').textContent, '需要补充信息');
  assert.equal(element('#clarification-panel').classList.contains('hidden'), false);
  assert.equal(element('#text2sql-result').classList.contains('hidden'), true);
  assert.match(element('#clarification-questions').innerHTML, /哪个岩爆等级/);
  assert.equal(element('#text2sql-question').value, '');
  const pending = JSON.parse(storage.get('evosql_clarification'));
  context.activeClarification = null;
  context.setClarification(pending);
  assert.equal(context.activeClarification.taskId, requests[0].task_id);

  element('#text2sql-question').value = '强烈';
  transportFailure = true;
  await context.submitQuestion();
  assert.ok(context.pendingText2SQLQuery);
  assert.equal(element('#clarification-panel').classList.contains('hidden'), true);
  assert.equal(element('#query-status').classList.contains('hidden'), false);
  assert.match(element('#query-status-message').textContent, /连接中断/);
  const retryId = context.pendingText2SQLQuery.taskId;
  await context.submitQuestion();
  assert.equal(requests[1].task_id, retryId);
  assert.equal(requests[2].task_id, retryId);
  assert.notEqual(retryId, requests[0].task_id);
  assert.equal(requests[2].clarification_task_id, requests[0].task_id);
  assert.equal(requests[2].session_id, 'test-session');
  assert.equal(requests[2].question, '强烈');
  assert.equal(element('#text2sql-answer-summary').textContent, '6');
  assert.equal(element('#clarification-panel').classList.contains('hidden'), true);
  assert.equal(context.pendingText2SQLQuery, null);
  assert.equal(storage.has('evosql_clarification'), false);
  assert.equal(element('#query-status').classList.contains('hidden'), true);

  context.setClarification({ taskId: 'parent-business-query', question: '案例数', questions: ['哪个等级？'] });
  context.renderResult({ task_id: 'rejected-child', status: 'rejected', gates: { accepted: false },
    diagnostic: { code: 'missing_schema_binding', stage: 'plan_binding', message: '查询计划未能完成绑定' } });
  assert.equal(context.activeClarification.taskId, 'parent-business-query');
  assert.equal(element('#clarification-panel').classList.contains('hidden'), true);
  assert.equal(element('#query-status-details').classList.contains('hidden'), false);
  assert.match(element('#query-status-technical').textContent, /missing_schema_binding/);
  context.setClarification(null);

  element('#text2sql-question').value = '新问题';
  nextFailure = Object.assign(new Error('query task_id was reused with a different runtime identity'),
    { status: 409, code: 'query_identity_conflict' });
  await context.submitQuestion();
  const rejectedId = requests.at(-1).task_id;
  assert.equal(context.pendingText2SQLQuery, null);
  assert.equal(element('#query-restart').classList.contains('hidden'), false);
  assert.equal(element('#query-retry').classList.contains('hidden'), true);
  assert.equal(element('#text2sql-result').classList.contains('hidden'), true);
  assert.doesNotMatch(element('#query-status-message').textContent, /task_id|checkpoint/);
  assert.equal(element('#query-status-details').open, false);
  await context.submitQuestion();
  assert.notEqual(requests.at(-1).task_id, rejectedId);

  element('#text2sql-question').value = '并发提交';
  let release;
  delayRequest = new Promise(resolve => { release = resolve; });
  const requestCount = requests.length;
  const running = context.submitQuestion();
  await context.submitQuestion();
  assert.equal(requests.length, requestCount + 1);
  assert.equal(element('#text2sql-question').disabled, true);
  release(); await running; delayRequest = null;
  assert.equal(element('#text2sql-question').disabled, false);

  const html = fs.readFileSync(path.join(__dirname, '../web/index.html'), 'utf8');
  assert.match(html, /<details class="panel query-sql-details">/);
  assert.doesNotMatch(html, /<details class="panel query-sql-details" open/);

  for (const [stage, index] of Object.entries({ routing: 0, planning_workers: 2, plan_approval: 4, plan_revisions: 5 })) {
    const payload = { status: 'needs_clarification', clarification: { stage } };
    assert.equal(context.runtimeNodeState(payload, {}, index, 'result'), 'awaiting_input');
    assert.equal(context.runtimeNodeState(payload, {}, index + 1, 'result'), 'bypassed');
  }
  console.log('PASS: clarification display, restored state, reply identity, network retry, result display and skipped nodes');
})().catch(error => { console.error(error); process.exitCode = 1; });
