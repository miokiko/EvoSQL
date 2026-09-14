// Exercise the actual rule renderer and compilation action without a browser.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8');
const start = source.indexOf('function renderSemanticRules(');
const end = source.indexOf('\nasync function loadMemory(', start);
assert.ok(start >= 0 && end > start);
const nodes = new Map();
const requests = [];
const selected = new Set();
const element = selector => {
  if (!nodes.has(selector)) nodes.set(selector, {innerHTML:'', textContent:'', value:'', disabled:false});
  return nodes.get(selector);
};
const context = vm.createContext({
  $: element, $$: () => [], selectedSemanticRuleIds: selected,
  escapeHtml: value => String(value ?? '').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;'),
  number: value => Number(value || 0), memoryStateLabel: value => value,
  memoryEmpty: (title, body) => `${title}: ${body}`, toast() {},
  async loadMemory() {}, async loadSkills() {}, async loadStatus() {},
  async api(url, options) { requests.push({url, body: JSON.parse(options.body)}); return {status:'candidate'}; },
});
vm.runInContext(source.slice(start,end),context);
const candidate = {
  rule_id:'semantic-rule-one', state:'candidate', root_cause:'过滤条件遗漏', rule:'<script>unsafe</script>',
  applicability:['审批计划包含过滤'], exceptions:['不增加额外条件'], evidence_refs:['before_sql'],
  source_memory_ids:['memory-one'], source_task_id:'query-one', source_revision:1,
  evidence:{question:'问题',before_sql:['SELECT 1'],after_sql:['SELECT 2'],approved_query_plan:{contract:'ApprovedQueryPlan/v1'}},
};
(async () => {
  context.renderSemanticRules({items:[]});
  assert.equal(element('#generate-rule-policy').disabled,true);
  assert.match(element('#semantic-rule-list').innerHTML,/尚无语义规则/);
  selected.add(candidate.rule_id);
  context.renderSemanticRules({items:[candidate]});
  assert.equal(selected.size,0);
  const html = element('#semantic-rule-list').innerHTML;
  assert.match(html,/&lt;script&gt;/);
  assert.doesNotMatch(html,/<script>/);
  assert.match(html,/确认规则/);
  assert.match(html,/SELECT 1/);
  assert.match(html,/SELECT 2/);
  assert.equal(element('#generate-rule-policy').disabled,true);
  selected.add(candidate.rule_id);
  context.renderSemanticRules({items:[{...candidate,state:'confirmed'}]});
  assert.equal(element('#generate-rule-policy').disabled,false);
  assert.doesNotMatch(element('#semantic-rule-list').innerHTML,/rule-review-action/);
  element('#rule-policy-reason').value = '完整过滤条件';
  await element('#generate-rule-policy').onclick();
  assert.equal(requests.length,1);
  assert.equal(requests[0].url,'/v1/text2sql/policies/from-rules');
  assert.deepEqual(requests[0].body,{rule_ids:['semantic-rule-one'],change_reason:'完整过滤条件'});
  assert.equal(selected.size,0);
  assert.equal(element('#generate-rule-policy').disabled,true);
  console.log('SemanticRule UI: rendering, XSS escaping, review visibility and compile action passed');
})().catch(error => { console.error(error); process.exitCode=1; });
