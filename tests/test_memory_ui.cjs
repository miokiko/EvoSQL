// Confirmed experiences must render for every supported Agent without a ReferenceError.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8');
const nodes = new Map();
const element = selector => {
  if (!nodes.has(selector)) nodes.set(selector, {
    innerHTML: '', textContent: '', disabled: false, classList: { toggle() {} },
  });
  return nodes.get(selector);
};
const context = vm.createContext({
  $: element, $$: () => [], selectedExperienceIds: new Set(),
  short: value => String(value ?? ''), number: value => Number(value || 0),
  escapeHtml: value => String(value ?? ''), memoryStateLabel: value => value,
  statusCard: () => '', memoryEmpty: () => '', formatTraceTime: () => '',
  experienceMemoryFields: () => '', semanticRuleFields: () => '',
  bindFeedbackJumps() {}, renderSemanticRules() {}, clearTimeout() {}, memoryPollTimer: null,
});
function section(startText, endText) {
  const start = source.indexOf(startText);
  const end = source.indexOf(endText, start);
  assert.ok(start >= 0 && end > start);
  return source.slice(start, end);
}
vm.runInContext(section('const roleDetails =', '\nconst traceStageDetails'), context);
vm.runInContext(section('function isExperienceMemory(', '\nfunction experienceMemoryFields('), context);
vm.runInContext(section('function renderMemory(', '\nfunction renderSemanticRules('), context);
for (const agent of vm.runInContext('Object.keys(roleDetails)', context)) {
  context.renderMemory({ layers: { semantic: { items: [{
    contract: 'ExperienceMemory/v1', memory_id: 'confirmed-example', state: 'confirmed', target_agent: agent,
  }] } } });
  assert.match(element('#semantic-memory-list').innerHTML, /归纳语义规则/, agent);
}
for (const agent of ['unknown-agent', 'toString']) {
  context.renderMemory({ layers: { semantic: { items: [{
    contract: 'ExperienceMemory/v1', memory_id: 'unknown-example', state: 'confirmed', target_agent: agent,
  }] } } });
  assert.doesNotMatch(element('#semantic-memory-list').innerHTML, /归纳语义规则/, agent);
}
console.log('PASS: Memory renders confirmed experiences for all five Agents; unknown roles cannot generate rules');
