const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

const views = {
  query: { title: "问答工作台", kicker: "TEXT2SQL WORKSPACE" },
  trace: { title: "11 节点轨迹", kicker: "EXECUTION TRACE" },
  memory: { title: "记忆中心", kicker: "MEMORY CONTROL PLANE" },
  data: { title: "数据与知识", kicker: "DATABASE & KNOWLEDGE" },
  skills: { title: "Agent Policies", kicker: "ROLE-SCOPED POLICIES" },
  evaluation: { title: "评测与审核", kicker: "EVALUATION & REVIEW" },
  evolution: { title: "自进化中心", kicker: "SELF-EVOLUTION" },
};

const roleDetails = {
  "text2sql-lead": ["Lead", "查询路由、任务委派、语义计划审批与最终选择"],
  "schema-grounding": ["Schema Grounding", "将逻辑概念绑定到有证据支持的表、字段、值与 Join"],
  "query-planning": ["Query Planning", "生成不含物理表列和 SQL 的逻辑 QuerySpec"],
  "sql-generation": ["SQL Generation", "只把 ApprovedQueryPlan 翻译为只读 SQL 候选"],
  "text2sql-critic": ["Critic", "对候选 SQL 进行独立盲审与否决"],
};

const traceStageDetails = {
  "query-routing": ["Lead Routing", "text2sql-lead"],
  "schema-grounding": ["Schema Grounding", "schema-grounding"],
  "query-planning": ["Query Planning", "query-planning"],
  "semantic-plan-approval": ["Lead Plan Approval", "text2sql-lead"],
  "sql-generation": ["SQL Generation", "sql-generation"],
  "blind-review": ["Critic", "text2sql-critic"],
  "final-selection": ["Lead Final", "text2sql-lead"],
  "cached-result-answer": ["Lead Result Answer", "text2sql-lead"],
};

const runtimeNodeCatalog = [
  { id: "text2sql-lead-routing", label: "Lead Routing", actor: "text2sql-lead", kind: "agent", phase: "ROUTE", description: "识别 DATA / FOLLOW-UP / RESULT QA" },
  { id: "text2sql-evidence-orchestration", label: "Evidence", actor: "runtime", kind: "runtime", phase: "GROUND", description: "固定 Snapshot、Knowledge 与 Memory 证据" },
  { id: "text2sql-plan-workers", label: "Plan Workers", actor: "schema-grounding ∥ query-planning", kind: "agent", phase: "PLAN", description: "两个 Worker 在同一节点内并行", parallel: true },
  { id: "text2sql-plan-binding", label: "Plan Binding", actor: "text2sql-harness", kind: "harness", phase: "BIND", description: "确定性合并 QuerySpec 与 SchemaPlan" },
  { id: "text2sql-lead-plan-assessment", label: "Lead Assessment", actor: "text2sql-lead", kind: "agent", phase: "ASSESS", description: "检查语义完整性与冲突责任" },
  { id: "text2sql-plan-revisions-approval", label: "Revision + Approval", actor: "runtime + harness", kind: "runtime", phase: "APPROVE", description: "定向返工并铸造不可变计划" },
  { id: "text2sql-sql-generation", label: "SQL Generation", actor: "sql-generation", kind: "agent", phase: "GENERATE", description: "只翻译 ApprovedQueryPlan" },
  { id: "text2sql-candidate-gates", label: "Candidate Gates", actor: "text2sql-harness", kind: "harness", phase: "VERIFY", description: "Validate + Conformance + EXPLAIN" },
  { id: "text2sql-critic", label: "Blind Critic", actor: "text2sql-critic", kind: "agent", phase: "CRITIQUE", description: "匿名候选独立盲审" },
  { id: "text2sql-lead-final", label: "Lead Final", actor: "text2sql-lead", kind: "agent", phase: "SELECT", description: "只选择 Critic 接受的候选" },
  { id: "text2sql-final-gates-execute", label: "Final Gates", actor: "text2sql-harness", kind: "harness", phase: "EXECUTE", description: "重验后在本机只读执行" },
];

const knowledgeLabels = {
  schema: "Schema 结构",
  value: "字段取值",
  relationship: "表间关系",
  business_glossary: "业务术语",
  verified_example: "审核 Question-SQL",
};

let runtimeStatus = null;
let skillCatalog = null;
let traceCatalog = [];
let selectedTraceId = "";
let activeSql = "";
let activeTaskId = "";
let activeQueryType = "DATA_QUERY";
let activeChartModel = null;
let activeChartType = "bar";
let toastTimer = null;
let memoryPollTimer = null;
let experiencePollTimer = null;
const sessionHistory = [];
const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)");

function persistentSessionId() {
  const key = "evoagent.text2sql.session";
  try {
    const stored = localStorage.getItem(key);
    if (stored) return stored;
    const created = globalThis.crypto?.randomUUID?.() || `session-${Date.now()}-${Math.random().toString(16).slice(2)}`;
    localStorage.setItem(key, created);
    return created;
  } catch (_) {
    return `session-${Date.now()}-${Math.random().toString(16).slice(2)}`;
  }
}

const text2sqlSessionId = persistentSessionId();
const pendingQueryKey = "evoagent.text2sql.pending." + text2sqlSessionId;

function readPendingQuery() {
  try {
    const value = JSON.parse(localStorage.getItem(pendingQueryKey) || "null");
    return value?.taskId && value?.question ? value : null;
  } catch (_) {
    return null;
  }
}

function writePendingQuery(value) {
  try {
    if (value) localStorage.setItem(pendingQueryKey, JSON.stringify(value));
    else localStorage.removeItem(pendingQueryKey);
  } catch (_) {
    // The in-memory value still provides retry identity for this page lifetime.
  }
}

let pendingText2SQLQuery = readPendingQuery();

function escapeHtml(value) {
  const node = document.createElement("div");
  node.textContent = value ?? "";
  return node.innerHTML;
}

function short(value, length = 18) {
  const text = String(value || "");
  return text.length > length ? `${text.slice(0, length)}…` : text || "--";
}

function number(value) {
  return Number(value || 0).toLocaleString("zh-CN");
}

async function api(path, options = {}) {
  const response = await fetch(path, options);
  const contentType = response.headers.get("content-type") || "";
  const data = contentType.includes("json") ? await response.json() : await response.text();
  if (!response.ok) {
    const plain = typeof data === "string" && !/<[a-z][\s\S]*>/i.test(data) ? data.trim() : "";
    const message = typeof data === "object" ? data.error || data.detail : plain;
    throw new Error(message || `请求失败 (${response.status})`);
  }
  return data;
}

function toast(message) {
  const element = $("#toast");
  element.textContent = message;
  element.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => element.classList.remove("show"), 2600);
}

function show(view, updateHash = true) {
  const selected = views[view] ? view : "query";
  $$(".view").forEach((element) => element.classList.toggle("active", element.id === `view-${selected}`));
  $$(".nav-item").forEach((element) => {
    const active = element.dataset.view === selected;
    element.classList.toggle("active", active);
    element.setAttribute("aria-current", active ? "page" : "false");
  });
  $("#page-title").textContent = views[selected].title;
  $("#page-kicker").textContent = views[selected].kicker;
  document.title = views[selected].title + " · EvoSQL";
  if (updateHash || selected !== view) history.replaceState(null, "", `#${selected}`);
  window.scrollTo({ top: 0, behavior: reduceMotion.matches ? "auto" : "smooth" });
}

function statusCard(label, value, detail, tone = "") {
  return `<article class="panel text2sql-status-card ${tone}">
    <span>${escapeHtml(label)}</span>
    <strong>${escapeHtml(value)}</strong>
    <small>${escapeHtml(detail)}</small>
  </article>`;
}

function detailRows(rows) {
  return rows.map(([label, value, mono = false]) => `<div>
    <span>${escapeHtml(label)}</span>
    <strong${mono ? ' class="mono"' : ""} title="${escapeHtml(value)}">${escapeHtml(value)}</strong>
  </div>`).join("");
}

function renderQueryStatus(status) {
  const model = status.model || {};
  const database = status.database || {};
  const dataset = status.dataset || {};
  const knowledge = status.knowledge || {};
  const vanna = status.vanna || {};
  const stable = Number(knowledge.states?.stable || 0);
  const candidate = Number(knowledge.states?.candidate || 0);
  const configured = Boolean(model.configured);
  const ready = Boolean(status.ready);
  const provider = model.provider || model.requested_provider || "aliyun-dashscope";
  const modelName = model.model || "未配置模型";
  const runtime = status.deterministic_runtime || {};

  const readyBadge = $("#text2sql-ready");
  readyBadge.className = `status ${ready ? "status-online" : "status-neutral"}`;
  readyBadge.innerHTML = `<i></i>${ready ? "可以问答" : configured ? "资源校验中" : "等待模型配置"}`;
  $("#text2sql-model").textContent = configured ? `${provider} / ${modelName}` : `${modelName} 未连接`;
  $("#top-model").textContent = configured ? `${provider} · ${modelName}` : "模型未连接";
  $("#text2sql-runtime-note").textContent = configured
    ? "云端仅负责推理，SQLite 数据文件始终留在本机"
    : "数据库、知识库和评测集可独立检查，问答需要模型配置";
  $("#runtime-contract-chip").textContent = `${runtime.protocol || "plan-first-text2sql-v3"} · ${number(runtime.node_count || 11)} nodes`;
  $("#runtime-blueprint").innerHTML = renderRuntimeMap(
    { deterministic_runtime: runtime },
    { mode: "blueprint" },
  );
  $("#text2sql-status-grid").innerHTML = [
    statusCard("本地数据库", database.ready ? `${number(database.table_count)} 张表` : "不可用", database.readonly ? "SQLite · 强制只读" : "只读状态未确认", database.ready ? "is-ready" : "is-warning"),
    statusCard("人工审核评测集", dataset.review_verified ? `${number(dataset.reviewed_case_count)} / ${number(dataset.case_count)}` : "未验证", dataset.review_verified ? "签名证书有效 · 审核人 匿名审核员" : "需要审核证书", dataset.review_verified ? "is-ready" : "is-warning"),
    statusCard("Knowledge Evidence", `${number(stable)} 可用 / ${number(candidate)} 待审核`, `Schema、取值、关系与术语 · ${short(knowledge.stable_index_version, 22)}`, "is-ready"),
    statusCard("Vanna 语义检索", vanna.ready ? `${number(vanna.item_count)} 条向量` : "回退模式", vanna.ready ? `只检索 · ${short(vanna.index_version, 22)}` : "使用 Wiki/结构化知识检索", vanna.ready ? "is-ready" : "is-warning"),
  ].join("");

  const submit = $(".text2sql-submit");
  submit.disabled = !ready;
  submit.title = ready ? "" : "模型或运行资源尚未就绪";
  $("#text2sql-form-note").textContent = ready
    ? "问题、必要的 Schema / 知识上下文，以及结果追问所需的有限 QueryRun 快照会发送给阿里云百炼；SQLite 文件不上传，SQL 仅在本机只读执行。"
    : "当前不能提问：请检查模型、数据库和人工审核评测集状态。";
}

function renderData(status) {
  const database = status.database || {};
  const knowledge = status.knowledge || {};
  const vanna = status.vanna || {};
  const states = knowledge.states || {};
  const types = knowledge.types || {};
  const total = Object.values(types).reduce((sum, value) => sum + Number(value || 0), 0);
  $("#data-status-grid").innerHTML = [
    statusCard("数据库表", number(database.table_count), "当前 SQLite 快照"),
    statusCard("稳定知识", number(states.stable), "问答时可检索"),
    statusCard("候选知识", number(states.candidate), "默认不参与生产问答"),
    statusCard("Vanna 向量条目", number(vanna.item_count), vanna.ready ? "语义索引已固定" : "当前自动回退"),
  ].join("");
  $("#database-detail").innerHTML = detailRows([
    ["快照 ID", short(database.snapshot_id, 28), true],
    ["表数量", `${number(database.table_count)} 张`],
    ["执行模式", database.readonly ? "SQLite Read-only" : "状态异常"],
    ["数据位置", "仅本机执行，不上传"],
  ]);
  const state = $("#knowledge-state");
  state.className = `status ${Number(states.stable || 0) > 0 ? "status-online" : "status-neutral"}`;
  state.innerHTML = `<i></i>${Number(states.stable || 0) > 0 ? "稳定索引可用" : "索引为空"}`;
  const max = Math.max(...Object.values(types).map(Number), 1);
  $("#knowledge-bars").innerHTML = Object.entries(knowledgeLabels).map(([key, label]) => {
    const value = Number(types[key] || 0);
    const width = Math.max(value ? 5 : 0, Math.round((value / max) * 100));
    return `<div class="knowledge-bar"><span><b>${escapeHtml(label)}</b><em>${number(value)}</em></span><i><u style="width:${width}%"></u></i></div>`;
  }).join("");
  const vannaState = $("#vanna-state");
  vannaState.className = `status ${vanna.ready ? "status-online" : "status-neutral"}`;
  vannaState.innerHTML = `<i></i>${vanna.ready ? "只检索索引可用" : "Wiki 回退模式"}`;
  const counts = vanna.counts || {};
  $("#vanna-detail").innerHTML = detailRows([
    ["运行模式", vanna.mode || "retriever_only", true],
    ["索引版本", short(vanna.index_version, 30), true],
    ["DDL", `${number(counts.ddl)} 条`],
    ["文档", `${number(counts.documentation)} 条`],
    ["Question-SQL", `${number(counts.sql)} 条`],
    ["SQL 生成", vanna.generation_enabled ? "开启（异常）" : "永久关闭"],
    ["SQL 执行", vanna.sql_execution_enabled ? "开启（异常）" : "永久关闭"],
  ]);
}

function renderEvaluation(status) {
  const dataset = status.dataset || {};
  const splits = dataset.split_counts || {};
  const verified = Boolean(dataset.review_verified);
  $("#evaluation-banner").className = `evaluation-banner panel ${verified ? "is-verified" : "is-warning"}`;
  $("#evaluation-banner").innerHTML = `<div><span>${verified ? "VERIFIED" : "UNVERIFIED"}</span><strong>${verified ? "240 条评测样本已完成人工审核" : "评测集审核证据不完整"}</strong><small>${verified ? "审核人：匿名审核员 · 数据集和数据库快照已绑定" : escapeHtml(dataset.error || "请检查审核证书")}</small></div><b>${verified ? "240/240" : "--"}</b>`;
  const splitCards = [
    ["TRAIN", splits.train, "用于构建与错误归因"],
    ["VALIDATION", splits.validation, "用于候选策略离线比较"],
    ["SEALED HOLDOUT", splits.sealed_holdout ?? splits.holdout, "密封集 · 防止过拟合"],
  ];
  $("#dataset-splits").innerHTML = splitCards.map(([label, value, detail]) => `<article class="panel split-card"><span>${label}</span><strong>${number(value)}</strong><small>${detail}</small></article>`).join("");
  $("#certificate-detail").innerHTML = detailRows([
    ["数据集 ID", short(dataset.dataset_id, 32), true],
    ["样本数量", `${number(dataset.case_count)} 条`],
    ["已审核", `${number(dataset.reviewed_case_count)} 条`],
    ["审核结果", verified ? "全部通过" : "未通过"],
    ["审核人", "匿名审核员"],
    ["证书 SHA", short(dataset.certificate_sha256, 30), true],
    ["数据集 SHA", short(dataset.dataset_sha256, 30), true],
  ]);
}

function renderEvolution(status) {
  const evolution = status.evolution || {};
  const release = evolution.release || {};
  const experiences = evolution.experience_counts || {};
  $("#evolution-stats").innerHTML = [
    statusCard("稳定记忆", number(evolution.stable_memory_count), "生产问答可使用"),
    statusCard("Question-SQL Memory", number(experiences.promoted), "用户确认后写入 Stable Vanna"),
    statusCard("当前策略", evolution.active_policy_version || "未初始化", "稳定版本"),
    statusCard("发布阶段", release.status && release.status !== "inactive" ? release.status : "未启用", "Shadow / Canary 门禁"),
  ].join("");
  $("#evolution-detail").innerHTML = detailRows([
    ["策略版本", evolution.active_policy_version || "--", true],
    ["记忆快照", short(evolution.memory_snapshot_id, 32), true],
    ["发布状态", release.status || "inactive"],
    ["候选策略", release.candidate_policy_version || "无"],
    ["已晋升经验", `${number(experiences.promoted)} 条`],
    ["已拒绝经验", `${number(experiences.rejected)} 条`],
    ["演进原则", "失败驱动、候选隔离、门禁发布"],
  ]);
  const roles = status.roles || Object.keys(roleDetails);
  const roleCards = roles.map((role, index) => {
    const [name, detail] = roleDetails[role] || [role, "Text2SQL 协作角色"];
    return `<div><b>${String(index + 1).padStart(2, "0")}</b><span><strong>${escapeHtml(name)}</strong><small>${escapeHtml(detail)}</small></span></div>`;
  }).join("");
  $("#agent-role-grid").innerHTML = `${roleCards}<div><b>H</b><span><strong>Deterministic Harness</strong><small>负责绑定、候选 Gate 与最终只读执行；它不是 Agent，也不是可演化 Skill</small></span></div>`;
  $("#evolution-runtime-graph").innerHTML = renderRuntimeMap(
    { deterministic_runtime: status.deterministic_runtime || {} },
    { mode: "blueprint" },
  );
}

function renderStatus(status) {
  runtimeStatus = status;
  renderQueryStatus(status);
  renderData(status);
  renderEvaluation(status);
  renderEvolution(status);
  $("#system-status").textContent = status.ready ? "Text2SQL 运行就绪" : "Text2SQL 需要检查";
}

async function loadStatus() {
  try {
    renderStatus(await api("/api/text2sql/status"));
  } catch (error) {
    $("#system-status").textContent = "服务连接失败";
    $("#text2sql-ready").className = "status status-neutral";
    $("#text2sql-ready").textContent = "状态读取失败";
    $("#text2sql-model").textContent = "无法连接 Text2SQL 服务";
    $("#text2sql-runtime-note").textContent = error.message;
    $("#top-model").textContent = "服务不可用";
    $(".text2sql-submit").disabled = true;
    toast(error.message);
  }
}

function renderSkills(data) {
  skillCatalog = data;
  const skills = data.skills || [];
  const candidates = data.candidates || [];
  $("#skill-stats").innerHTML = [
    statusCard("Agent Roles", number(skills.length), "五个运行时角色 · Harness 非 Agent"),
    statusCard("稳定策略", short(data.active_policy_version, 24), "当前生产版本"),
    statusCard("候选版本", number(data.candidate_count), "隔离等待评测"),
    statusCard("提交契约", data.submission_contract || "--", "单次只允许修改一个角色 Policy"),
  ].join("");
  $("#text2sql-skill-list").innerHTML = skills.map((skill, index) => {
    const allowedTools = skill.allowed_tools || [];
    const tools = allowedTools.length
      ? allowedTools.map((tool) => `<em>${escapeHtml(tool)}</em>`).join("")
      : '<em class="tool-none">NO RUNTIME TOOLS</em>';
    const fragment = skill.prompt_fragment
      ? escapeHtml(skill.prompt_fragment)
      : "使用稳定基线指令；可以在下方提交增量指令候选。";
    return `<article class="panel skill-runtime-card">
      <div class="skill-runtime-head"><b>${String(index + 1).padStart(2, "0")}</b><span><small>AGENT ROLE · POLICY SLOT</small><strong>${escapeHtml(skill.name)}</strong></span><i>ACTIVE</i></div>
      <p>${escapeHtml(skill.description)}</p>
      <blockquote>${fragment}</blockquote>
      <div class="skill-tool-list">${tools}</div>
      <footer><span>${number(skill.field_alias_count)} 字段别名</span><span>${number(skill.value_alias_count)} 取值别名</span><span>${number(skill.few_shot_count)} Few-shot</span></footer>
    </article>`;
  }).join("") || '<div class="empty-state"><span>没有发现 Text2SQL Agent Policy</span></div>';
  const roleSelect = $("#skill-role");
  if (roleSelect && skills.length) {
    const previousRole = roleSelect.value;
    roleSelect.innerHTML = skills.map((skill) => `<option value="${escapeHtml(skill.name)}">${escapeHtml(skill.name)}</option>`).join("");
    roleSelect.value = skills.some((skill) => skill.name === previousRole) ? previousRole : "sql-generation";
  }
  $("#skill-candidates").innerHTML = candidates.length
    ? [...candidates].reverse().map((item) => `<div class="candidate-item"><span><strong>${escapeHtml(item.target_skill || "unknown")}</strong><small>${escapeHtml(item.change_reason || "无变更说明")}</small></span><div><b>${escapeHtml(item.status || "candidate")}</b><code title="${escapeHtml(item.policy_version || "")}">${escapeHtml(short(item.policy_version, 20))}</code></div></div>`).join("")
    : '<div class="empty-state compact"><span><b>暂无候选 Policy</b>稳定版本不会被直接覆盖。</span></div>';
}

async function loadSkills() {
  try {
    renderSkills(await api("/api/text2sql/skills"));
  } catch (error) {
    $("#text2sql-skill-list").innerHTML = `<div class="empty-state"><span>Agent Policies 加载失败：${escapeHtml(error.message)}</span></div>`;
  }
}

function renderExperiences(data) {
  const items = data.experiences || [];
  $("#experience-list").innerHTML = items.length
    ? items.map((item) => {
      const reasons = (item.eligibility_reasons || []).join("；");
      const evaluation = item.evaluation || {};
      const current = Number(evaluation.progress_current || 0);
      const total = Math.max(1, Number(evaluation.progress_total || 240));
      const progress = item.state === "evaluating"
        ? '<div class="memory-evaluation-progress"><div><span>Vanna Candidate · '
          + escapeHtml(evaluation.phase || "preparing") + '</span><b>' + number(current)
          + " / " + number(total) + '</b></div><i><u style="width:'
          + Math.min(100, Math.round(current / total * 100)) + '%"></u></i><small>'
          + escapeHtml(evaluation.error || "构建候选索引并运行 240 条对照评测") + '</small></div>'
        : "";
      const awaitingConfirmation = item.state === "ineligible"
        && (item.eligibility_reasons || []).includes("requires_human_feedback");
      const actions = awaitingConfirmation && item.confirmable
        ? `<div class="experience-review-editor">
            <input data-experience-confirm-note maxlength="2000" placeholder="确认说明（可选）">
            <div class="experience-actions"><button class="copy-button experience-incorrect-toggle" type="button">结果不正确</button><button class="button experience-confirm" data-experience-id="${escapeHtml(item.experience_id)}" type="button">确认结果正确</button></div>
            <div class="experience-incorrect-editor hidden">
              <label>错误原因（必填）<input data-experience-incorrect-note maxlength="2000" placeholder="说明结果或 SQL 哪里不正确"></label>
              <label>正确 SQL（可选）<textarea data-experience-corrected-sql rows="4" placeholder="如果知道正确 SQL，可在这里提交"></textarea></label>
              <button class="button experience-incorrect-submit" data-experience-id="${escapeHtml(item.experience_id)}" type="button">提交错误反馈</button>
            </div>
            <small>正确反馈经确定性复验后写入 Stable Vanna 与 Question-SQL Memory；错误反馈进入归因后的 Agent Semantic Memory Candidate。</small>
          </div>`
        : awaitingConfirmation
        ? `<div class="experience-confirm-unavailable"><b>等待确认</b><span>原 QueryRun 已不在当前运行历史中，不能跨过来源校验。</span></div>`
        : item.state === "candidate"
        ? `<div class="experience-review-editor"><input data-experience-review-note maxlength="2000" placeholder="审核评论（拒绝时必填）"><div class="experience-actions"><button class="copy-button experience-review" data-experience-id="${escapeHtml(item.experience_id)}" data-decision="reject" type="button">拒绝</button><button class="button experience-review" data-experience-id="${escapeHtml(item.experience_id)}" data-decision="approve" type="button">审核并跑 240 条</button></div></div>`
        : item.state === "evaluation_failed" || item.state === "evaluated"
        ? `<div class="experience-actions"><button class="button experience-evaluation" data-experience-id="${escapeHtml(item.experience_id)}" type="button">重新构建并评测</button></div>`
        : `<b>${escapeHtml(memoryStateLabel(item.state || "unknown"))}</b>`;
      return `<article class="experience-item">
        <div class="experience-copy"><span><strong>${escapeHtml(item.question || "未命名问题")}</strong><small>${escapeHtml(item.source_kind || "query_run")} · ${escapeHtml(item.experience_id || "")}</small></span><em class="experience-state state-${escapeHtml(item.state || "unknown")}">${escapeHtml(memoryStateLabel(item.state || "unknown"))}</em></div>
        <pre>${escapeHtml(item.sql || "--")}</pre>
        ${reasons ? `<p>${escapeHtml(reasons)}</p>` : ""}
        ${item.review_note ? `<blockquote class="review-note"><b>审核评论</b>${escapeHtml(item.review_note)}</blockquote>` : ""}
        ${progress}
        ${actions}
      </article>`;
    }).join("")
    : '<div class="empty-state compact"><span><b>暂无 Question-SQL 经验</b>成功查询经用户确认后，会写入 Stable Vanna 与长期记忆。</span></div>';
  $$(".experience-confirm").forEach((button) => button.addEventListener("click", async () => {
    const card = button.closest(".experience-item");
    const note = $('[data-experience-confirm-note]', card)?.value.trim() || "";
    button.disabled = true;
    try {
      await api(`/v1/text2sql/experiences/${encodeURIComponent(button.dataset.experienceId)}/confirm`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ note }),
      });
      toast("已写入 Stable Vanna 与 Question-SQL Memory");
      await Promise.all([loadExperiences(), loadStatus(), loadTraces(), loadMemory()]);
    } catch (error) {
      toast(error.message);
      button.disabled = false;
    }
  }));
  $$(".experience-incorrect-toggle").forEach((button) => button.addEventListener("click", () => {
    $(".experience-incorrect-editor", button.closest(".experience-item"))?.classList.toggle("hidden");
  }));
  $$(".experience-incorrect-submit").forEach((button) => button.addEventListener("click", async () => {
    const card = button.closest(".experience-item");
    const noteInput = $("[data-experience-incorrect-note]", card);
    const note = noteInput?.value.trim() || "";
    if (!note) {
      toast("结果不正确时必须填写原因");
      noteInput?.focus();
      return;
    }
    button.disabled = true;
    try {
      const result = await api(`/v1/text2sql/experiences/${encodeURIComponent(button.dataset.experienceId)}/feedback`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          decision: "incorrect",
          note,
          corrected_sql: $("[data-experience-corrected-sql]", card)?.value.trim() || "",
        }),
      });
      toast(result.corrected_experience_id
        ? "错误已归因；修正 SQL 与 Semantic Memory 分别进入候选队列"
        : "错误已归因并生成 Semantic Memory Candidate");
      await Promise.all([loadExperiences(), loadStatus(), loadTraces(), loadMemory()]);
    } catch (error) {
      toast(error.message);
      button.disabled = false;
    }
  }));
  $$(".experience-review").forEach((button) => button.addEventListener("click", async () => {
    const decision = button.dataset.decision;
    const card = button.closest(".experience-item");
    const reviewNote = $('[data-experience-review-note]', card)?.value.trim() || "";
    if (decision === "reject" && !reviewNote) {
      toast("拒绝候选经验时必须填写理由");
      $('[data-experience-review-note]', card)?.focus();
      return;
    }
    button.disabled = true;
    try {
      const result = await api(`/v1/text2sql/experiences/${encodeURIComponent(button.dataset.experienceId)}/review`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ decision, review_note: reviewNote }),
      });
      toast(decision === "approve" ? "候选 Vanna 构建与 240 条评测已启动" : "候选经验已拒绝");
      await Promise.all([loadExperiences(), loadStatus(), loadMemory()]);
    } catch (error) {
      toast(error.message);
      button.disabled = false;
    }
  }));
  $$(".experience-evaluation").forEach((button) => button.addEventListener("click", async () => {
    button.disabled = true;
    try {
      await api("/v1/text2sql/experiences/" + encodeURIComponent(button.dataset.experienceId) + "/evaluation", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      });
      toast("Question-SQL 候选评测已重新启动");
      await loadExperiences();
    } catch (error) {
      toast(error.message);
      button.disabled = false;
    }
  }));
  clearTimeout(experiencePollTimer);
  if (items.some((item) => item.state === "evaluating")) {
    experiencePollTimer = setTimeout(loadExperiences, 5000);
  }
}

async function loadExperiences() {
  try {
    renderExperiences(await api("/api/text2sql/experiences?limit=30"));
  } catch (error) {
    $("#experience-list").innerHTML = `<div class="empty-state"><span>经验队列加载失败：${escapeHtml(error.message)}</span></div>`;
  }
}

function formatTraceTime(value) {
  if (!value) return "刚刚";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit" }).format(date);
}

function traceList(value, limit = 6) {
  if (!Array.isArray(value)) return [];
  return value.slice(0, limit).map((item) => {
    if (item === null || item === undefined) return "";
    if (typeof item === "string" || typeof item === "number" || typeof item === "boolean") return String(item);
    if (typeof item === "object") return String(item.logical_name || item.slot_id || item.column || item.code || "");
    return "";
  }).filter(Boolean);
}

function traceBoolean(value) {
  if (value === true) return "PASS";
  if (value === false) return "BLOCK";
  return "--";
}

function runtimeDefinitions(payload = {}) {
  const declared = payload.deterministic_runtime?.nodes
    || runtimeStatus?.deterministic_runtime?.nodes
    || runtimeNodeCatalog.map((node) => node.id);
  const byId = Object.fromEntries(runtimeNodeCatalog.map((node) => [node.id, node]));
  const ordered = Array.isArray(declared) && declared.length === runtimeNodeCatalog.length
    ? declared
    : runtimeNodeCatalog.map((node) => node.id);
  return ordered.map((id, index) => byId[id] || {
    id,
    label: String(id).replace(/^text2sql-/, "").replaceAll("-", " "),
    actor: "runtime",
    kind: "runtime",
    phase: `NODE ${index + 1}`,
    description: "固定 Runtime Node",
  });
}

function runtimeNodeState(payload, node, index, mode) {
  if (mode === "blueprint") return "fixed";
  if (mode === "running") return index === 0 ? "running" : "pending";
  if (mode === "error") return index === 0 ? "blocked" : "pending";

  const queryType = payload.query_type || "DATA_QUERY";
  if (queryType === "RESULT_QA") {
    if (index === 0) return "completed";
    if (index === runtimeNodeCatalog.length - 1) {
      return payload.status === "success" ? "replay" : "blocked";
    }
    return "bypassed";
  }

  const agents = Array.isArray(payload.agents) ? payload.agents : [];
  const agentRan = (stage) => agents.some((item) => item.stage === stage && item.status !== "not-run");
  const workersRan = agentRan("schema-grounding") && agentRan("query-planning");
  const bound = payload.bound_query_plan || {};
  const approved = payload.approved_query_plan || {};
  const conflicts = Array.isArray(payload.binding_conflicts) ? payload.binding_conflicts : [];
  const generation = payload.sql_generation || {};
  const gateResults = Array.isArray(payload.candidate_gate_results) ? payload.candidate_gate_results : [];
  const rounds = Array.isArray(payload.candidate_gate_rounds) ? payload.candidate_gate_rounds : [];

  switch (node.id) {
    case "text2sql-lead-routing":
      return "completed";
    case "text2sql-evidence-orchestration":
      return workersRan ? "completed" : "blocked";
    case "text2sql-plan-workers":
      return workersRan ? "completed" : "blocked";
    case "text2sql-plan-binding":
      return Object.keys(bound).length ? "completed" : conflicts.length ? "blocked" : "bypassed";
    case "text2sql-lead-plan-assessment":
      return agentRan("semantic-plan-approval") ? "completed" : "bypassed";
    case "text2sql-plan-revisions-approval":
      return Object.keys(approved).length ? "completed" : "blocked";
    case "text2sql-sql-generation":
      return generation.status && generation.status !== "not-run" ? "completed" : "bypassed";
    case "text2sql-candidate-gates":
      return rounds.length || gateResults.length
        ? gateResults.some((item) => item.accepted) ? "completed" : "blocked"
        : "bypassed";
    case "text2sql-critic":
      return agentRan("blind-review") ? "completed" : "bypassed";
    case "text2sql-lead-final":
      return agentRan("final-selection") ? "completed" : "bypassed";
    case "text2sql-final-gates-execute":
      return payload.status === "success" && payload.gates?.accepted ? "completed" : "blocked";
    default:
      return "fixed";
  }
}

function runtimeStateLabel(state) {
  return {
    fixed: "FIXED",
    running: "RUNNING",
    pending: "WAIT",
    completed: "DONE",
    blocked: "BLOCK",
    bypassed: "SKIP",
    replay: "REPLAY",
  }[state] || state;
}

function renderRuntimeMap(payload = {}, { compact = false, mode = "result" } = {}) {
  const nodes = runtimeDefinitions(payload);
  const items = nodes.map((node, index) => {
    const state = runtimeNodeState(payload, node, index, mode);
    const parallel = node.parallel ? '<span class="runtime-parallel-badge">PARALLEL × 2</span>' : "";
    return `<li class="runtime-node kind-${escapeHtml(node.kind)} state-${escapeHtml(state)}${node.parallel ? " is-parallel" : ""}" title="${escapeHtml(node.description)}">
      <div class="runtime-node-top"><b>${String(index + 1).padStart(2, "0")}</b><em>${escapeHtml(runtimeStateLabel(state))}</em></div>
      <span class="runtime-node-phase">${escapeHtml(node.phase)}</span>
      <strong>${escapeHtml(node.label)}</strong>
      <small>${escapeHtml(node.actor)}</small>
      ${parallel}
      <p>${escapeHtml(node.description)}</p>
    </li>`;
  }).join("");
  return `<div class="runtime-map-shell-inner${compact ? " is-compact" : ""}"><ol class="runtime-map">${items}</ol></div>`;
}

function agentDetailSummary(agent = {}) {
  const detail = agent.detail || {};
  const stage = agent.stage || "";
  const values = [];
  if (stage === "query-routing") {
    values.push(`route=${detail.query_type || "--"}`);
    if (detail.parent_query_run_id) values.push(`parent=${short(detail.parent_query_run_id, 16)}`);
  } else if (stage === "schema-grounding") {
    const tables = traceList(detail.tables, 4);
    values.push(`${number(tables.length)} tables`, `${number((detail.columns || []).length)} columns`, `${number(detail.join_count)} joins`);
    if (tables.length) values.push(tables.join(", "));
  } else if (stage === "query-planning") {
    values.push(`intent=${detail.intent || "--"}`, `shape=${detail.expected_shape || "--"}`);
    values.push(`${number(detail.dimension_count)} dimensions`, `${number(detail.measure_count)} measures`, `${number(detail.filter_count)} filters`);
  } else if (stage === "semantic-plan-approval") {
    values.push(`approved=${traceBoolean(detail.approved)}`, `${number(detail.binding_conflict_count)} conflicts`, `${number(detail.revisions_applied)} revisions`);
    if (detail.bound_plan_fingerprint) values.push(`plan=${short(detail.bound_plan_fingerprint, 16)}`);
  } else if (stage === "sql-generation") {
    values.push(`${number(detail.candidate_count)} candidates`, `${number(detail.repair_count)} repairs`);
    if (Array.isArray(detail.generation_notes) && detail.generation_notes.length) values.push(`${number(detail.generation_notes.length)} notes`);
  } else if (stage === "blind-review") {
    values.push(`${number(detail.candidate_count)} reviewed`, `${number(detail.accepted_count)} accepted`);
  } else if (stage === "final-selection") {
    values.push(detail.final_candidate_index === null || detail.final_candidate_index === undefined
      ? "candidate=none"
      : `candidate=${number(detail.final_candidate_index)}`);
  } else if (stage === "cached-result-answer") {
    values.push(`requires_new_query=${detail.requires_new_query ? "yes" : "no"}`);
  }
  if (agent.evidence_count !== null && agent.evidence_count !== undefined) {
    values.push(`${number(agent.evidence_count)} evidence`);
  }
  return values.join(" · ");
}

function renderAgentTrace(agents, emptyText = "暂无 Agent 轨迹") {
  const items = Array.isArray(agents) ? agents : [];
  if (!items.length) return `<div class="empty-state compact"><span>${escapeHtml(emptyText)}</span></div>`;
  return items.map((agent, index) => {
    const [stageName, expectedRole] = traceStageDetails[agent.stage] || [agent.stage || agent.role || "Agent", agent.role || "agent"];
    const role = agent.role || expectedRole;
    const detail = agentDetailSummary(agent);
    return `<article class="text2sql-agent-item"><b>${String(index + 1).padStart(2, "0")}</b><span><strong>${escapeHtml(stageName)}</strong><small>${escapeHtml(role)} · ${escapeHtml(agent.summary || "已完成")}</small>${detail ? `<code>${escapeHtml(detail)}</code>` : ""}</span><em>${escapeHtml(agent.status || "completed")}</em></article>`;
  }).join("");
}

function protocolDisclosure(label, summary, lines = []) {
  const detail = lines.filter(Boolean).join("\n");
  return `<div><details><summary><span>${escapeHtml(label)}</span> <small>${escapeHtml(summary)}</small></summary>${detail ? `<code>${escapeHtml(detail)}</code>` : ""}</details></div>`;
}

function renderProtocolSummary(payload = {}) {
  const bound = payload.bound_query_plan || {};
  const approved = payload.approved_query_plan || {};
  const approvedBound = approved.bound_plan || {};
  const querySpec = bound.query_spec || approvedBound.query_spec || payload.query_spec || {};
  const schemaPlan = bound.schema_plan || approvedBound.schema_plan || payload.schema_plan || {};
  const bindings = Array.isArray(bound.bindings) ? bound.bindings : (Array.isArray(approvedBound.bindings) ? approvedBound.bindings : []);
  const tables = traceList(schemaPlan.tables, 8);
  const joins = Array.isArray(schemaPlan.joins) ? schemaPlan.joins : [];
  const conflicts = Array.isArray(payload.binding_conflicts) ? payload.binding_conflicts : [];
  const generation = payload.sql_generation || {};
  const repairCount = Number(payload.sql_generation_repairs ?? generation.repair_count ?? 0);
  const directGateResults = Array.isArray(payload.candidate_gate_results) ? payload.candidate_gate_results : [];
  let rounds = Array.isArray(payload.candidate_gate_rounds) ? payload.candidate_gate_rounds : [];
  if (!rounds.length && directGateResults.length) {
    rounds = [{ round: 0, candidate_gate_results: directGateResults }];
  }
  const runtime = payload.deterministic_runtime || {};
  const hasProtocolState = Object.keys(bound).length || Object.keys(approved).length || conflicts.length
    || Object.keys(generation).length || rounds.length || Object.keys(runtime).length;
  if (!hasProtocolState) return "";

  const bindingLines = bindings.slice(0, 10).map((item) => {
    const target = item.column || item.aggregation || item.kind || "unresolved";
    return `${item.slot_id || item.logical_name || "slot"} → ${target}`;
  });
  const boundFingerprint = bound.fingerprint || approved.bound_plan_fingerprint || approvedBound.fingerprint || "";
  const boundLines = [
    `Contract: ${bound.contract || approvedBound.contract || "BoundQueryPlan"}`,
    `Intent: ${querySpec.intent || "--"}`,
    `Expected shape: ${querySpec.expected_shape || "--"}`,
    `Tables: ${tables.join(", ") || "--"}`,
    `Fingerprint: ${boundFingerprint || "--"}`,
    ...bindingLines,
  ];
  const approvedFingerprint = approved.fingerprint || approved.bound_plan_fingerprint || boundFingerprint;
  const approvalLines = [
    `Approved by: ${approved.approved_by || (Object.keys(approved).length ? "text2sql-lead" : "--")}`,
    `Approval id: ${approved.approval_id || "--"}`,
    `Reason: ${approved.approval_reason || "--"}`,
    `Fingerprint: ${approvedFingerprint || "--"}`,
  ];
  const conflictLines = conflicts.slice(0, 12).map((item) => {
    const subject = item.slot_id || item.logical_name || "plan";
    return `${item.code || "binding_conflict"} · ${item.owner || "unassigned"} · ${subject}${item.message ? ` · ${short(item.message, 140)}` : ""}`;
  });
  const gateLines = [];
  rounds.slice(0, 3).forEach((round, index) => {
    const results = Array.isArray(round.candidate_gate_results) ? round.candidate_gate_results : [];
    const acceptedCount = round.accepted_candidate_count ?? results.filter((item) => item.accepted).length;
    const issueCodes = traceList((round.gate_issues || []).map((item) => item?.code), 8);
    gateLines.push(`Round ${Number(round.round ?? index) + 1}: ${results.length} candidates · ${acceptedCount} accepted${issueCodes.length ? ` · issues=${issueCodes.join(",")}` : ""}`);
    results.slice(0, 4).forEach((item, candidateIndex) => {
      const validation = item.validation || {};
      const conformance = item.plan_conformance || {};
      gateLines.push(`  ${item.candidate_id || `candidate-${candidateIndex + 1}`}: ${traceBoolean(Boolean(item.accepted))} · validate=${traceBoolean(validation.accepted)} · conform=${traceBoolean(conformance.accepted)} · errors=${number((item.errors || []).length)}`);
    });
  });
  const candidateCount = Number(generation.candidate_count ?? directGateResults.length ?? 0);
  const generationLines = [
    `Role: sql-generation`,
    `Status: ${generation.status || "not-run"}`,
    `Candidates: ${candidateCount}`,
    `Repairs: ${repairCount}`,
    `Generation notes: ${number((generation.generation_notes || []).length)}`,
  ];
  const bindingRuntime = runtime.binding || {};
  const candidateRuntime = runtime.candidate_gates || {};
  const finalRuntime = runtime.final_gates || {};
  const runtimeLines = [
    `Classification: ${runtime.classification || "deterministic-runtime"}`,
    `Skill: ${runtime.is_skill === true ? "yes" : "no"}`,
    `Binding: ${traceBoolean(bindingRuntime.accepted)}`,
    `Candidate rounds: ${number(candidateRuntime.round_count ?? rounds.length)}`,
    `Accepted candidates: ${number(candidateRuntime.accepted_count ?? directGateResults.filter((item) => item.accepted).length)}`,
    `Final gates: ${traceBoolean(finalRuntime.accepted)}`,
  ];

  return `<p class="trace-label">PLAN &amp; DETERMINISTIC HARNESS</p><div class="trace-planning">
    ${protocolDisclosure("BOUND QUERY PLAN", Object.keys(bound).length || Object.keys(approvedBound).length ? `${tables.length} tables · ${bindings.length} bindings · ${joins.length} joins` : "not created", boundLines)}
    ${protocolDisclosure("APPROVED QUERY PLAN", Object.keys(approved).length ? "Lead approved" : "not approved", approvalLines)}
    ${protocolDisclosure("BINDING CONFLICTS", conflicts.length ? `${conflicts.length} need revision` : "0 conflicts", conflictLines)}
    ${protocolDisclosure("SQL GENERATION", `${candidateCount} candidates · ${repairCount} repairs`, generationLines)}
    ${protocolDisclosure("CANDIDATE GATE ROUNDS", rounds.length ? `${rounds.length} deterministic rounds` : "not run", gateLines)}
    ${protocolDisclosure("TEXT2SQL HARNESS", "deterministic runtime · not a Skill", runtimeLines)}
  </div>`;
}

function renderTraceDetail(trace) {
  selectedTraceId = trace?.task_id || "";
  $$(".trace-item").forEach((item) => item.classList.toggle("active", item.dataset.traceId === selectedTraceId));
  if (!trace) {
    $("#trace-detail").innerHTML = '<div class="empty-state"><span><b>选择一条轨迹</b>这里会显示 SQL、五 Agent 执行阶段与 deterministic Harness 门禁。</span></div>';
    return;
  }
  const accepted = Boolean(trace.gates?.accepted) && trace.status === "success";
  const queryType = trace.query_type || "DATA_QUERY";
  const agents = renderAgentTrace(trace.agents, "没有 Agent 轨迹");
  const pins = Object.entries(trace.version_pins || {}).map(([key, value]) => `<div><b>${escapeHtml(key.replaceAll("_", " "))}</b><code>${escapeHtml(short(value, 24))}</code></div>`).join("");
  const schemaPlan = trace.schema_plan || {};
  const querySpec = trace.query_spec || {};
  const draftPack = trace.draft_link_pack || {};
  const retrieval = trace.retrieval || [];
  const vannaHits = retrieval.filter((item) => item.backend === "vanna-chromadb");
  const memoryHits = retrieval.filter((item) => item.backend === "semantic-memory");
  const memoryUsage = memoryHits.length
    ? `<div class="trace-memory-usage"><span>SEMANTIC MEMORY</span>${memoryHits.map((item) => `<div><b>${escapeHtml(item.role || "agent")} · ${escapeHtml(item.phase || "run")}</b><code>${escapeHtml((item.memory_ids || []).join(", "))}</code></div>`).join("")}</div>`
    : `<div class="trace-memory-usage empty"><span>SEMANTIC MEMORY</span><small>本次没有命中相关 Stable Memory</small></div>`;
  const routeDetail = `<div class="trace-route"><span><b>${escapeHtml(queryType)}</b>路由类型</span><span><b>${escapeHtml(trace.parent_task_id ? short(trace.parent_task_id, 24) : "无")}</b>父 QueryRun</span><span><b>${number(retrieval.length)}</b>检索调用</span><span><b>${number(vannaHits.length)}</b>Vanna 命中批次</span><span><b>${number(memoryHits.length)}</b>记忆注入阶段</span></div>`;
  const planning = queryType === "RESULT_QA" ? "" : `<div class="trace-planning"><div><span>SCHEMA PLAN</span><code>${escapeHtml(JSON.stringify({ tables: schemaPlan.tables || [], columns: schemaPlan.columns || [], joins: schemaPlan.joins || [] }))}</code></div><div><span>QUERY SPEC</span><code>${escapeHtml(JSON.stringify(querySpec))}</code></div></div>`;
  const draftPlanning = queryType === "RESULT_QA" || !draftPack.contract ? "" : `<div class="trace-planning"><div><span>DRAFT SQL · UNTRUSTED</span><code>${escapeHtml(draftPack.draft_sql || "-- 已回退到问题直连")}</code></div><div><span>DRAFT LINK PACK</span><code>${escapeHtml(JSON.stringify({ tables: draftPack.tables || [], columns: draftPack.columns || [], joins: draftPack.joins || [], coverage: draftPack.coverage || {} }))}</code></div></div>`;
  $("#trace-detail").innerHTML = `<div class="panel-head"><div><p class="eyebrow">TRACE DETAIL</p><h3>${escapeHtml(trace.question || "未命名查询")}</h3></div><span class="status ${accepted ? "status-online" : "status-neutral"}"><i></i>${accepted ? "门禁通过" : "已拦截"}</span></div>
    ${trace.standalone_question && trace.standalone_question !== trace.question ? `<p class="trace-standalone"><b>改写后的独立问题</b>${escapeHtml(trace.standalone_question)}</p>` : ""}
    ${routeDetail}
    <div class="trace-metrics"><span><b>${number(trace.execution?.llm_calls)}</b>LLM calls</span><span><b>${number(trace.execution?.tool_calls)}</b>Tool calls</span><span><b>${number(trace.execution?.total_tokens)}</b>Tokens</span><span><b>${number(trace.execution?.duration_ms)}</b>ms</span></div>
    <p class="trace-label">11-NODE RUNTIME</p>${renderRuntimeMap(trace)}
    <p class="trace-label">FINAL SQL</p><pre class="text2sql-sql">${escapeHtml(trace.final_sql || "-- 未生成 SQL")}</pre>
    ${draftPlanning}
    ${planning}
    ${queryType === "RESULT_QA" ? "" : renderProtocolSummary(trace)}
    ${memoryUsage}
    <p class="trace-label">AGENT TRACE</p><div class="text2sql-agent-trace">${agents}</div>
    <div class="version-pins trace-pins"><span>版本固定</span>${pins}</div>`;
}

function renderTraces(data) {
  traceCatalog = data.traces || [];
  const successes = traceCatalog.filter((item) => item.status === "success" && item.gates?.accepted).length;
  const latest = traceCatalog[0] || {};
  $("#trace-stats").innerHTML = [
    statusCard("最近运行", number(traceCatalog.length), "本地持久化 · 最多 50 条"),
    statusCard("门禁通过", number(successes), "AST + EXPLAIN + Read-only"),
    statusCard("最近耗时", latest.execution ? `${number(latest.execution.duration_ms)} ms` : "--", "端到端执行"),
    statusCard("最近 Token", latest.execution ? number(latest.execution.total_tokens) : "--", "多 Agent 总计"),
  ].join("");
  $("#trace-list").innerHTML = traceCatalog.length
    ? traceCatalog.map((trace, index) => `<button class="trace-item${trace.task_id === selectedTraceId || (!selectedTraceId && index === 0) ? " active" : ""}" data-trace-id="${escapeHtml(trace.task_id)}" type="button"><b>${String(traceCatalog.length - index).padStart(2, "0")}</b><span><strong>${escapeHtml(trace.question || "未命名查询")}</strong><small>${formatTraceTime(trace.recorded_at)} · ${number(trace.execution?.duration_ms)} ms</small></span><em class="${trace.status === "success" && trace.gates?.accepted ? "ok" : ""}">${trace.status === "success" && trace.gates?.accepted ? "PASS" : "BLOCK"}</em></button>`).join("")
    : '<div class="empty-state"><span><b>还没有运行轨迹</b>完成一次问答后，这里会记录 SQL 门禁和 Agent 执行步骤。</span></div>';
  $$(".trace-item").forEach((item) => item.addEventListener("click", () => renderTraceDetail(traceCatalog.find((trace) => trace.task_id === item.dataset.traceId))));
  const selected = traceCatalog.find((trace) => trace.task_id === selectedTraceId) || traceCatalog[0];
  renderTraceDetail(selected);
}

async function loadTraces() {
  try {
    renderTraces(await api("/api/text2sql/traces?limit=20"));
  } catch (error) {
    $("#trace-list").innerHTML = `<div class="empty-state"><span>Trace 加载失败：${escapeHtml(error.message)}</span></div>`;
  }
}

function memoryStateLabel(state) {
  return {
    stable: "稳定",
    candidate: "候选",
    approved: "待评测",
    evaluating: "评测中",
    evaluated: "评测通过",
    evaluation_failed: "评测未通过",
    rejected: "已拒绝",
    retired: "已撤销",
    promoted: "已发布",
    ineligible: "等待用户确认",
  }[state] || state || "未知";
}

function memoryEmpty(title, detail) {
  return '<div class="empty-state compact"><span><b>' + escapeHtml(title) + '</b>' + escapeHtml(detail) + '</span></div>';
}

function renderMemory(data) {
  const layers = data.layers || {};
  const working = layers.working || {};
  const episodic = layers.episodic || {};
  const semantic = layers.semantic || {};
  const semanticCounts = semantic.counts || {};
  const questionSql = data.question_sql || {};
  const experienceCounts = questionSql.counts || {};
  const evaluationJobs = Array.isArray(data.evaluations?.items) ? data.evaluations.items : [];
  const jobByMemory = new Map();
  evaluationJobs.forEach((job) => {
    if (!jobByMemory.has(job.memory_id)) jobByMemory.set(job.memory_id, job);
  });
  const snapshot = short(data.memory_snapshot_id, 22);

  $("#memory-stats").innerHTML = [
    statusCard("Working Memory", number(working.count), "当前会话 · 最多保留 " + number(working.retention_limit_per_session) + " 条", "is-ready"),
    statusCard("Episodic Memory", number(episodic.count), "当前会话 QueryRun · 最多 " + number(episodic.retention_limit) + " 条", "is-ready"),
    statusCard(
      "Agent Semantic",
      `${number(semanticCounts.stable)} 稳定 / ${number(semanticCounts.candidate)} 候选`,
      "角色级失败经验 · 快照 " + snapshot,
      semanticCounts.candidate ? "is-warning" : ""
    ),
    statusCard("Question-SQL Memory", number(experienceCounts.promoted), "用户确认正确 · Stable Vanna", experienceCounts.promoted ? "is-ready" : ""),
  ].join("");

  const workingItems = Array.isArray(working.items) ? working.items : [];
  $("#working-memory-list").innerHTML = workingItems.length
    ? workingItems.map((item) => {
        const role = String(item.role || "message").toUpperCase();
        return '<article class="memory-entry"><div class="memory-entry-head"><span class="memory-role">' + escapeHtml(role)
          + '</span><time>' + escapeHtml(formatTraceTime(item.created_at)) + '</time></div><p>'
          + escapeHtml(short(item.content, 180)) + '</p><small>Task · '
          + escapeHtml(short(item.task_id, 28)) + '</small></article>';
      }).join("")
    : memoryEmpty("当前会话还没有工作记忆", "完成一次问答后，会在这里保留有限的用户与助手消息。");

  const episodicItems = Array.isArray(episodic.items) ? episodic.items : [];
  $("#episodic-memory-list").innerHTML = episodicItems.length
    ? episodicItems.map((item) => {
        const question = item.standalone_question || item.original_question || "未命名查询";
        const decisions = item.decisions || {};
        const harness = decisions.harness || {};
        const human = decisions.human || {};
        const harnessOutcome = harness.outcome || (item.status === "success" ? "accepted" : "rejected");
        const harnessLabel = { accepted: "放行", rejected: "拒绝", failed: "失败", deferred: "待新查询" }[harnessOutcome] || harnessOutcome;
        const humanLabel = { accepted: "确认", rejected: "拒绝" }[human.outcome] || "待审核";
        const canConfirm = item.status === "success" && Boolean(item.final_sql);
        const humanReview = human.decision_id
          ? '<div class="episodic-human-result state-' + escapeHtml(human.outcome || "unknown") + '"><div><b>HUMAN · '
            + escapeHtml(humanLabel) + '</b><span>' + escapeHtml(human.actor || "人工审核") + '</span></div><p>'
            + escapeHtml(human.reason_text || "未填写评论") + '</p></div>'
          : '<div class="episodic-review"><input data-episodic-review-note maxlength="2000" placeholder="人工评论（拒绝时必填）"><div>'
            + (canConfirm ? '<button class="button episodic-review-action" data-decision="correct" type="button">确认结果</button>' : '')
            + '<button class="copy-button episodic-review-action" data-decision="incorrect" type="button">拒绝结果</button></div></div>';
        return '<article class="memory-entry episodic-entry" data-task-id="' + escapeHtml(item.task_id || "")
          + '"><div class="memory-entry-head"><span class="memory-state state-' + escapeHtml(harnessOutcome) + '">HARNESS · ' + escapeHtml(harnessLabel)
          + '</span><time>' + escapeHtml(formatTraceTime(item.recorded_at)) + '</time></div><p>'
          + escapeHtml(short(question, 150)) + '</p><code>' + escapeHtml(short(item.final_sql || "-- 未生成 SQL", 190))
          + '</code><div class="episodic-decision-reason"><b>' + escapeHtml(harness.reason_code || item.status || "unknown")
          + '</b><span>' + escapeHtml(harness.reason_text || "未记录 Harness 原因") + '</span></div><small>'
          + escapeHtml(item.query_type || "DATA_QUERY") + ' · HUMAN ' + escapeHtml(humanLabel) + '</small>'
          + humanReview + '</article>';
      }).join("")
    : memoryEmpty("当前会话还没有情景记忆", "每次 QueryRun 的问题、SQL、门禁状态与反馈会形成可追溯片段。");
  $$(".episodic-review-action", $("#episodic-memory-list")).forEach((button) => button.addEventListener("click", async () => {
    const card = button.closest("[data-task-id]");
    const decision = button.dataset.decision;
    const noteInput = $('[data-episodic-review-note]', card);
    const note = noteInput?.value.trim() || "";
    if (decision === "incorrect" && !note) {
      toast("人工拒绝时必须填写理由");
      noteInput?.focus();
      return;
    }
    $$(".episodic-review-action", card).forEach((item) => item.disabled = true);
    try {
      await api(`/v1/text2sql/queries/${encodeURIComponent(card.dataset.taskId)}/feedback`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ decision, note, corrected_sql: "", session_id: text2sqlSessionId }),
      });
      toast(decision === "correct" ? "已保存人工确认" : "已保存人工拒绝及理由");
      await Promise.all([loadMemory(), loadExperiences(), loadStatus(), loadTraces()]);
    } catch (error) {
      toast(error.message);
      $$(".episodic-review-action", card).forEach((item) => item.disabled = false);
    }
  }));

  const semanticItems = Array.isArray(semantic.items) ? semantic.items : [];
  $("#semantic-memory-list").innerHTML = semanticItems.length
    ? semanticItems.map((item) => {
        const state = String(item.state || "candidate");
        const job = jobByMemory.get(item.memory_id) || {};
        const provenance = item.reviewed_by
          ? "审核人 " + item.reviewed_by
          : "来源 " + (item.origin_split || "production_feedback");
        const roleOptions = Object.entries(roleDetails).map(([value, detail]) =>
          '<option value="' + escapeHtml(value) + '"' + (value === item.target_skill ? " selected" : "") + '>'
          + escapeHtml(detail[0] + " · " + value) + '</option>'
        ).join("");
        let lifecycleAction = "";
        if (state === "approved" || state === "evaluation_failed") {
          lifecycleAction = '<div class="memory-release-gate"><span><b>240</b> 条人工审核集 · Baseline/Candidate 对照 · 零安全回退</span><button class="button memory-lifecycle-action" data-action="evaluation" type="button">'
            + (state === "evaluation_failed" ? "重新评测" : "启动后台评测") + '</button></div>';
        } else if (state === "evaluating") {
          const current = Number(job.progress_current || 0);
          const total = Math.max(1, Number(job.progress_total || 240));
          const percent = Math.min(100, Math.round(current / total * 100));
          lifecycleAction = '<div class="memory-evaluation-progress"><div><span>后台评测 · '
            + escapeHtml(job.phase || "preparing") + '</span><b>' + number(current) + " / " + number(total)
            + '</b></div><i><u style="width:' + percent + '%"></u></i><small>'
            + escapeHtml(job.error || "Checkpoint 持续写入，可在服务重启后查看进度") + '</small></div>';
        } else if (state === "evaluated") {
          lifecycleAction = '<div class="memory-release-gate passed"><span><b>PASS</b> 240 条回归门禁通过，等待最终人工发布</span><button class="button memory-lifecycle-action" data-action="activate" type="button">发布 Stable</button></div>';
        } else if (state === "stable") {
          lifecycleAction = '<div class="memory-release-gate stable"><span><b>LIVE</b> 当前可注入目标 Agent</span><button class="copy-button memory-lifecycle-action" data-action="rollback" type="button">撤销记忆</button></div>';
        }
        const editor = state === "candidate"
          ? '<div class="memory-review-editor"><label>目标 Agent<select data-memory-field="target_skill">'
            + roleOptions + '</select></label><label>失败类型<input data-memory-field="failure_kind" maxlength="100" value="'
            + escapeHtml(item.failure_kind || "") + '"></label><label class="memory-content-field">可复用经验<textarea data-memory-field="content" maxlength="1500" rows="4">'
            + escapeHtml(item.content || "") + '</textarea></label><label class="memory-content-field">审核评论<input data-memory-field="review_note" maxlength="2000" placeholder="拒绝时必须填写理由"></label><div class="memory-review-actions"><button class="copy-button memory-review-action" data-decision="reject" type="button">拒绝</button><button class="button memory-review-action" data-decision="approve" type="button">审核并进入评测</button></div></div>'
          : '<p>' + escapeHtml(short(item.content, 220)) + '</p>'
            + (item.review_note ? '<blockquote class="review-note"><b>审核评论</b>' + escapeHtml(item.review_note) + '</blockquote>' : '');
        return '<article class="memory-entry semantic-entry" data-memory-id="' + escapeHtml(item.memory_id || "")
          + '"><div class="memory-entry-head"><span class="memory-state state-'
          + escapeHtml(state.replace(/[^a-z0-9_-]/gi, "")) + '">' + escapeHtml(memoryStateLabel(state))
          + '</span><time>' + escapeHtml(formatTraceTime(item.reviewed_at || item.created_at))
          + '</time></div>' + editor + '<div class="memory-entry-tags"><span>'
          + escapeHtml(item.target_skill || "shared") + '</span><span>' + escapeHtml(item.failure_kind || "general")
          + '</span></div>' + lifecycleAction + '<small>' + escapeHtml(provenance) + '</small></article>';
      }).join("")
    : memoryEmpty("尚未沉淀长期经验", "错误和反馈不会直接进入长期记忆；需要先归因、审核，再晋升为 Stable。");
  $$(".memory-review-action", $("#semantic-memory-list")).forEach((button) => button.addEventListener("click", async () => {
    const card = button.closest("[data-memory-id]");
    const decision = button.dataset.decision;
    const payload = {
      decision,
      target_skill: $('[data-memory-field="target_skill"]', card)?.value || "",
      failure_kind: $('[data-memory-field="failure_kind"]', card)?.value.trim() || "",
      content: $('[data-memory-field="content"]', card)?.value.trim() || "",
      review_note: $('[data-memory-field="review_note"]', card)?.value.trim() || "",
    };
    if (decision === "approve" && (!payload.target_skill || !payload.failure_kind || !payload.content)) {
      toast("请补全目标 Agent、失败类型和可复用经验");
      return;
    }
    if (decision === "reject" && !payload.review_note) {
      toast("拒绝候选记忆时必须填写理由");
      $('[data-memory-field="review_note"]', card)?.focus();
      return;
    }
    $$(".memory-review-action", card).forEach((item) => item.disabled = true);
    try {
      const result = await api("/v1/text2sql/memories/" + encodeURIComponent(card.dataset.memoryId) + "/review", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      toast(result.state === "approved" ? "审核通过，等待 240 条回归评测" : "候选记忆已拒绝");
      await Promise.all([loadMemory(), loadStatus()]);
    } catch (error) {
      toast(error.message);
      $$(".memory-review-action", card).forEach((item) => item.disabled = false);
    }
  }));
  $$(".memory-lifecycle-action", $("#semantic-memory-list")).forEach((button) => button.addEventListener("click", async () => {
    const card = button.closest("[data-memory-id]");
    const action = button.dataset.action;
    button.disabled = true;
    button.textContent = action === "evaluation" ? "正在启动…" : action === "activate" ? "正在发布…" : "正在撤销…";
    try {
      const result = await api(
        "/v1/text2sql/memories/" + encodeURIComponent(card.dataset.memoryId) + "/" + action,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            reason: action === "activate" ? "240 条回归门禁通过后人工发布" : "前端人工撤销稳定记忆",
          }),
        }
      );
      toast(
        action === "evaluation"
          ? "240 条 Memory 对照评测已在后台启动"
          : action === "activate"
          ? "Semantic Memory 已发布为 Stable"
          : "Stable Memory 已撤销"
      );
      await Promise.all([loadMemory(), loadStatus()]);
    } catch (error) {
      toast(error.message);
      button.disabled = false;
    }
  }));

  const questionSqlItems = Array.isArray(questionSql.items) ? questionSql.items : [];
  $("#question-sql-memory-list").innerHTML = questionSqlItems.length
    ? questionSqlItems.map((item) => `<article class="memory-entry question-sql-entry">
        <div class="memory-entry-head"><span class="memory-state state-promoted">VANNA · STABLE</span><time>${escapeHtml(formatTraceTime(item.reviewed_at || item.created_at))}</time></div>
        <p>${escapeHtml(item.question || "未命名问题")}</p>
        <code>${escapeHtml(item.sql || "--")}</code>
        <div class="memory-entry-tags"><span>Question-SQL</span><span>${escapeHtml(short(item.knowledge_evidence_id, 28))}</span></div>
        <small>${escapeHtml(item.reviewed_by ? "确认人 " + item.reviewed_by : "Human Confirmed")}</small>
      </article>`).join("")
    : memoryEmpty("还没有 Question-SQL 长期记忆", "在查询结果或经验审计中确认 SQL 正确后，会立即写入 Stable Vanna。");
  clearTimeout(memoryPollTimer);
  if (semanticItems.some((item) => item.state === "evaluating")) {
    memoryPollTimer = setTimeout(loadMemory, 5000);
  }
}

async function loadMemory() {
  try {
    const path = "/api/text2sql/memory?session_id=" + encodeURIComponent(text2sqlSessionId) + "&limit=12";
    renderMemory(await api(path));
  } catch (error) {
    $("#memory-stats").innerHTML = statusCard("记忆服务", "加载失败", error.message, "is-warning");
    $("#working-memory-list").innerHTML = memoryEmpty("Working Memory 加载失败", error.message);
    $("#episodic-memory-list").innerHTML = memoryEmpty("Episodic Memory 加载失败", error.message);
    $("#semantic-memory-list").innerHTML = memoryEmpty("Semantic Memory 加载失败", error.message);
    $("#question-sql-memory-list").innerHTML = memoryEmpty("Question-SQL Memory 加载失败", error.message);
  }
}

async function loadWorkspace() {
  const refresh = $("#refresh");
  refresh.disabled = true;
  refresh.textContent = "刷新中…";
  try {
    await Promise.all([loadStatus(), loadSkills(), loadTraces(), loadExperiences(), loadMemory()]);
  } finally {
    refresh.disabled = false;
    refresh.textContent = "刷新";
  }
}

function renderTable(answer = {}) {
  const columns = Array.isArray(answer.columns) ? answer.columns : [];
  const rows = Array.isArray(answer.rows) ? answer.rows : [];
  if (!columns.length) return '<div class="empty-state"><span><b>没有可展示的结果</b>查询已完成，但没有返回列。</span></div>';
  const head = columns.map((column) => `<th>${escapeHtml(column)}</th>`).join("");
  const body = rows.map((row) => {
    const values = Array.isArray(row) ? row : columns.map((column) => row?.[column]);
    return `<tr>${values.map((value) => `<td>${escapeHtml(value === null ? "NULL" : String(value))}</td>`).join("")}</tr>`;
  }).join("");
  return `<table class="text2sql-table"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
}

function finiteChartNumber(value) {
  if (typeof value === "boolean" || value === null || value === "") return null;
  const parsed = typeof value === "number" ? value : Number(String(value).replaceAll(",", ""));
  return Number.isFinite(parsed) ? parsed : null;
}

function buildChartModel(answer = {}) {
  const columns = Array.isArray(answer.columns) ? answer.columns.map(String) : [];
  const sourceRows = Array.isArray(answer.rows) ? answer.rows : [];
  if (columns.length < 2 || sourceRows.length < 2) return null;
  const rows = sourceRows.map((row) => Array.isArray(row) ? row : columns.map((column) => row?.[column]));
  const numericIndexes = columns.map((_, index) => index).filter((index) => {
    const values = rows.map((row) => row[index]).filter((value) => value !== null && value !== "");
    return values.length > 0 && values.every((value) => finiteChartNumber(value) !== null);
  });
  if (!numericIndexes.length) return null;
  let categoryIndex = columns.findIndex((_, index) => !numericIndexes.includes(index));
  let valueIndex = numericIndexes.find((index) => index !== categoryIndex);
  if (categoryIndex < 0) {
    categoryIndex = 0;
    valueIndex = numericIndexes.find((index) => index !== categoryIndex);
  }
  if (valueIndex === undefined) return null;
  const points = rows.slice(0, 12).map((row) => ({
    label: row[categoryIndex] === null ? "NULL" : String(row[categoryIndex]),
    value: finiteChartNumber(row[valueIndex]),
  })).filter((point) => point.value !== null);
  if (points.length < 2) return null;
  const categoryName = columns[categoryIndex];
  const valueName = columns[valueIndex];
  const temporal = /(date|time|year|month|day|日期|时间|年份|年度|月份|月|年)/i.test(categoryName)
    || points.every((point) => /^\d{4}(?:[-/年]\d{1,2})?/.test(point.label));
  return {
    categoryName,
    valueName,
    points,
    truncated: sourceRows.length > points.length,
    suggestedType: temporal ? "line" : "bar",
  };
}

function formatChartValue(value) {
  return Number(value).toLocaleString("zh-CN", { maximumFractionDigits: 2 });
}

function barChartSvg(model) {
  const width = 760;
  const rowHeight = 46;
  const height = 32 + model.points.length * rowHeight;
  const plotLeft = 170;
  const plotRight = 92;
  const plotWidth = width - plotLeft - plotRight;
  const values = model.points.map((point) => point.value);
  const domainMin = Math.min(0, ...values);
  const domainMax = Math.max(0, ...values);
  const span = domainMax - domainMin || 1;
  const scale = (value) => plotLeft + ((value - domainMin) / span) * plotWidth;
  const baseline = scale(0);
  const grid = [0, .25, .5, .75, 1].map((ratio) => {
    const x = plotLeft + ratio * plotWidth;
    return '<line class="chart-grid-line" x1="' + x + '" x2="' + x + '" y1="10" y2="' + (height - 14) + '" />';
  }).join("");
  const bars = model.points.map((point, index) => {
    const y = 18 + index * rowHeight;
    const valueX = scale(point.value);
    const x = Math.min(baseline, valueX);
    const barWidth = Math.max(2, Math.abs(valueX - baseline));
    const label = escapeHtml(short(point.label, 18));
    const value = escapeHtml(formatChartValue(point.value));
    const valueLabelX = point.value >= 0 ? Math.min(valueX + 10, width - 48) : Math.max(valueX - 10, plotLeft - 8);
    const anchor = point.value >= 0 ? "start" : "end";
    return '<g class="chart-mark" style="--mark-index:' + index + '">'
      + '<title>' + escapeHtml(point.label) + '：' + value + '</title>'
      + '<text class="chart-axis-label" x="154" y="' + (y + 18) + '" text-anchor="end">' + label + '</text>'
      + '<rect class="chart-bar" x="' + x + '" y="' + y + '" width="' + barWidth + '" height="25" rx="7" />'
      + '<text class="chart-value-label" x="' + valueLabelX + '" y="' + (y + 18) + '" text-anchor="' + anchor + '">' + value + '</text>'
      + '</g>';
  }).join("");
  return '<svg class="result-chart" viewBox="0 0 ' + width + ' ' + height + '" role="img" aria-label="查询结果柱状图">'
    + grid
    + '<line class="chart-zero-line" x1="' + baseline + '" x2="' + baseline + '" y1="10" y2="' + (height - 14) + '" />'
    + bars
    + '</svg>';
}

function lineChartSvg(model) {
  const width = 760;
  const height = 330;
  const left = 70;
  const right = 30;
  const top = 28;
  const bottom = 72;
  const plotWidth = width - left - right;
  const plotHeight = height - top - bottom;
  const values = model.points.map((point) => point.value);
  let min = Math.min(...values);
  let max = Math.max(...values);
  if (min === max) {
    const padding = Math.abs(min || 1) * .1;
    min -= padding;
    max += padding;
  }
  const xFor = (index) => left + (model.points.length === 1 ? 0 : index / (model.points.length - 1)) * plotWidth;
  const yFor = (value) => top + (1 - (value - min) / (max - min)) * plotHeight;
  const grid = [0, .25, .5, .75, 1].map((ratio) => {
    const y = top + ratio * plotHeight;
    const value = max - ratio * (max - min);
    return '<g><line class="chart-grid-line" x1="' + left + '" x2="' + (width - right) + '" y1="' + y + '" y2="' + y + '" />'
      + '<text class="chart-tick-label" x="' + (left - 12) + '" y="' + (y + 4) + '" text-anchor="end">' + escapeHtml(formatChartValue(value)) + '</text></g>';
  }).join("");
  const coordinates = model.points.map((point, index) => xFor(index) + "," + yFor(point.value)).join(" ");
  const marks = model.points.map((point, index) => {
    const x = xFor(index);
    const y = yFor(point.value);
    return '<g class="chart-mark" style="--mark-index:' + index + '"><title>'
      + escapeHtml(point.label) + '：' + escapeHtml(formatChartValue(point.value))
      + '</title><circle class="chart-dot" cx="' + x + '" cy="' + y + '" r="6" />'
      + '<text class="chart-x-label" x="' + x + '" y="' + (height - 38) + '" text-anchor="middle">' + escapeHtml(short(point.label, 10)) + '</text></g>';
  }).join("");
  return '<svg class="result-chart" viewBox="0 0 ' + width + ' ' + height + '" role="img" aria-label="查询结果折线图">'
    + grid
    + '<polyline class="chart-line" points="' + coordinates + '" />'
    + marks
    + '</svg>';
}

function renderActiveChart() {
  if (!activeChartModel) return;
  $$("[data-chart-type]", $("#text2sql-chart-controls")).forEach((button) => {
    button.classList.toggle("active", button.dataset.chartType === activeChartType);
  });
  $("#text2sql-chart").innerHTML = activeChartType === "line"
    ? lineChartSvg(activeChartModel)
    : barChartSvg(activeChartModel);
}

function renderVisualization(answer = {}, accepted = false) {
  const panel = $("#text2sql-chart-panel");
  activeChartModel = accepted ? buildChartModel(answer) : null;
  panel.classList.toggle("hidden", !activeChartModel);
  if (!activeChartModel) {
    $("#text2sql-chart").innerHTML = "";
    return;
  }
  activeChartType = activeChartModel.suggestedType;
  $("#text2sql-chart-description").textContent = activeChartModel.categoryName + " × " + activeChartModel.valueName
    + (activeChartModel.truncated ? " · 展示前 12 个结果点" : " · " + activeChartModel.points.length + " 个结果点");
  renderActiveChart();
}

function answerSummary(answer = {}) {
  if (answer.summary_text) {
    return { value: String(answer.summary_text), meta: "基于已授权的历史 QueryRun 结果 · 未重新执行 SQL" };
  }
  const columns = Array.isArray(answer.columns) ? answer.columns : [];
  const rows = Array.isArray(answer.rows) ? answer.rows : [];
  if (columns.length === 1 && rows.length === 1) {
    const row = rows[0];
    const value = Array.isArray(row) ? row[0] : row?.[columns[0]];
    return { value: value === null ? "NULL" : String(value), meta: `${columns[0]} · 1 行` };
  }
  return { value: `${number(answer.row_count)} 行`, meta: `${number(columns.length)} 列${answer.truncated ? " · 结果已截断" : ""}` };
}

function renderPins(pins = {}) {
  const entries = Object.entries(pins);
  $("#result-version-pins").innerHTML = entries.length
    ? `<span>版本固定</span>${entries.map(([key, value]) => `<div><b>${escapeHtml(key.replaceAll("_", " "))}</b><code title="${escapeHtml(value)}">${escapeHtml(short(value, 20))}</code></div>`).join("")}`
    : "";
}

function renderResult(result) {
  const gates = result.gates || {};
  const accepted = Boolean(gates.accepted) && result.status === "success";
  const answer = result.answer || {};
  const summary = answerSummary(answer);
  activeTaskId = result.task_id || "";
  activeQueryType = result.query_type || "DATA_QUERY";
  activeSql = result.final_sql || "";
  $("#text2sql-result").classList.remove("hidden");
  $("#text2sql-final-sql").textContent = activeSql || (activeQueryType === "RESULT_QA" ? "-- 使用历史 QueryRun 结果，本次没有生成或执行 SQL" : "-- 未生成可执行 SQL");
  $("#text2sql-answer-summary").textContent = accepted ? summary.value : "查询未执行";
  $("#text2sql-answer-meta").textContent = accepted ? summary.meta : (gates.errors || []).join("；");
  const gate = $("#text2sql-gate-status");
  gate.className = `status ${accepted ? "status-online" : "status-neutral"}`;
  gate.innerHTML = accepted
    ? `<i></i>${activeQueryType === "RESULT_QA" ? "会话结果回答" : "安全门禁通过"}`
    : `未执行${gates.errors?.length ? ` · ${gates.errors.length} 项拦截` : ""}`;
  $("#text2sql-answer").innerHTML = renderTable(answer);
  renderVisualization(answer, accepted);
  $("#text2sql-row-count").textContent = `${number(answer.row_count)} 行${answer.truncated ? " · 已截断" : ""}`;
  $("#text2sql-runtime-trace").innerHTML = renderRuntimeMap(result, { compact: true });
  $("#runtime-blueprint").innerHTML = renderRuntimeMap(result);
  $("#text2sql-agent-trace").innerHTML = renderAgentTrace(result.agents)
    + (activeQueryType === "RESULT_QA" ? "" : renderProtocolSummary(result));
  const usage = result.execution || {};
  $("#text2sql-usage").textContent = `${number(usage.llm_calls)} LLM · ${number(usage.total_tokens)} tokens · ${number(usage.duration_ms)} ms`;
  renderPins(result.version_pins);
  resetFeedback(activeQueryType !== "RESULT_QA" && result.status === "success" && Boolean(activeSql));
  addSession(result.question || $("#text2sql-question").value.trim(), result, summary);
  loadTraces();
  loadMemory();
  $("#text2sql-result").scrollIntoView({ behavior: reduceMotion.matches ? "auto" : "smooth", block: "start" });
}

function resetFeedback(enabled) {
  const panel = $("#query-feedback-panel");
  panel.classList.toggle("feedback-disabled", !enabled);
  $("#feedback-correct").disabled = !enabled;
  $("#feedback-incorrect").disabled = !enabled;
  $("#feedback-submit-incorrect").disabled = !enabled;
  $("#feedback-correction").classList.add("hidden");
  $("#feedback-corrected-sql").value = activeSql || "";
  $("#feedback-note").value = "";
  const status = $("#query-feedback-status");
  status.className = "status status-neutral";
  status.textContent = enabled ? "等待反馈" : "本次无需 SQL 反馈";
}

async function submitQueryFeedback(decision) {
  if (!activeTaskId || activeQueryType === "RESULT_QA") return;
  const note = $("#feedback-note").value.trim();
  if (decision === "incorrect" && !note) {
    toast("拒绝结果时必须填写理由");
    $("#feedback-note").focus();
    return;
  }
  const payload = {
    decision,
    session_id: text2sqlSessionId,
    note,
    corrected_sql: decision === "incorrect" ? $("#feedback-corrected-sql").value.trim() : "",
  };
  const result = await api(`/v1/text2sql/queries/${encodeURIComponent(activeTaskId)}/feedback`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const status = $("#query-feedback-status");
  status.className = `status ${decision === "correct" ? "status-online" : "status-neutral"}`;
  status.innerHTML = `<i></i>${decision === "correct" ? "已确认正确" : "已记录错误"}`;
  $("#feedback-correct").disabled = true;
  $("#feedback-incorrect").disabled = true;
  $("#feedback-submit-incorrect").disabled = true;
  $("#feedback-correction").classList.add("hidden");
  await Promise.all([loadExperiences(), loadStatus(), loadTraces(), loadMemory()]);
  if (decision === "correct" && result.experience_id) {
    toast("已写入 Stable Vanna 与 Question-SQL Memory");
  } else if (result.experience_id && result.memory_id) {
    toast("修正 SQL 与归因记忆已分别进入审核队列");
  } else if (result.experience_id) {
    toast("修正 SQL 已进入 Question-SQL 候选队列");
  } else if (result.memory_id) {
    toast("错误已自动归因，并生成 Semantic Candidate");
  } else {
    toast("反馈已记录");
  }
}

function addSession(question, result, summary) {
  sessionHistory.unshift({ question, sql: result.final_sql || "--", value: result.status === "success" ? summary.value : "未执行", status: result.status });
  sessionHistory.splice(6);
  $("#session-section").classList.remove("hidden");
  $("#session-list").innerHTML = sessionHistory.map((item, index) => `<article class="session-item"><b>${String(sessionHistory.length - index).padStart(2, "0")}</b><div><strong>${escapeHtml(item.question)}</strong><code>${escapeHtml(item.sql)}</code></div><span class="${item.status === "success" ? "ok" : ""}">${escapeHtml(item.value)}</span></article>`).join("");
}

function setBusy(button, busy) {
  if (busy) {
    button.dataset.label = button.textContent;
    button.textContent = "多 Agent 推理中…";
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
  } else {
    button.textContent = button.dataset.label || "发送问题";
    button.disabled = !runtimeStatus?.ready;
    button.setAttribute("aria-busy", "false");
  }
}

async function submitQuestion() {
  const question = $("#text2sql-question").value.trim();
  if (!question) return;
  if (!pendingText2SQLQuery || pendingText2SQLQuery.question !== question) {
    pendingText2SQLQuery = {
      question,
      taskId: "text2sql-web-" + (globalThis.crypto?.randomUUID?.() || Date.now() + "-" + Math.random().toString(16).slice(2)),
    };
    writePendingQuery(pendingText2SQLQuery);
  }
  const button = $(".text2sql-submit");
  let failed = false;
  setBusy(button, true);
  $("#runtime-blueprint").innerHTML = renderRuntimeMap({}, { mode: "running" });
  $("#text2sql-form-note").textContent = "Lead 正在路由问题并编排证据；需要查库时会并行调度 Schema Grounding 与 Query Planning，再进行计划绑定、审批和 SQL Generation…";
  try {
    const result = await api("/v1/text2sql/query", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        question,
        session_id: text2sqlSessionId,
        task_id: pendingText2SQLQuery.taskId,
      }),
    });
    renderResult(result);
    pendingText2SQLQuery = null;
    writePendingQuery(null);
    toast(result.status === "success" ? "Text2SQL 查询完成" : "查询被安全门禁拦截");
  } catch (error) {
    failed = true;
    $("#runtime-blueprint").innerHTML = renderRuntimeMap({}, { mode: "error" });
    const identityRejected = String(error.message || "").includes("task_id was reused");
    if (identityRejected) {
      pendingText2SQLQuery = null;
      writePendingQuery(null);
    }
    $("#text2sql-form-note").textContent = identityRejected
      ? error.message + "；旧恢复标识已清除，再次发送会创建新任务。"
      : error.message + "；再次发送同一问题将从 checkpoint 续跑。";
    toast(error.message);
  } finally {
    setBusy(button, false);
    if (runtimeStatus?.ready && !failed) $("#text2sql-form-note").textContent = "问题、必要的 Schema / 知识上下文，以及结果追问所需的有限 QueryRun 快照会发送给阿里云百炼；SQLite 文件不上传，SQL 仅在本机只读执行。";
  }
}

$$('.nav-item').forEach((button) => button.addEventListener("click", () => show(button.dataset.view)));
$$('[data-sql-question]').forEach((button) => button.addEventListener("click", () => {
  $("#text2sql-question").value = button.dataset.sqlQuestion;
  $("#text2sql-question").focus();
}));
$("#text2sql-form").addEventListener("submit", (event) => {
  event.preventDefault();
  submitQuestion();
});
$("#text2sql-question").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    if (!$(".text2sql-submit").disabled) submitQuestion();
  }
});
$("#text2sql-chart-controls").addEventListener("click", (event) => {
  const button = event.target.closest("[data-chart-type]");
  if (!button || !activeChartModel) return;
  activeChartType = button.dataset.chartType;
  renderActiveChart();
});
$("#copy-sql").addEventListener("click", async () => {
  if (!activeSql) return;
  try {
    await navigator.clipboard.writeText(activeSql);
    toast("SQL 已复制");
  } catch (_) {
    toast("复制失败，请手动选择 SQL");
  }
});
$("#feedback-correct").addEventListener("click", async () => {
  try {
    await submitQueryFeedback("correct");
  } catch (error) {
    toast(error.message);
  }
});
$("#feedback-incorrect").addEventListener("click", () => {
  $("#feedback-correction").classList.toggle("hidden");
});
$("#feedback-submit-incorrect").addEventListener("click", async () => {
  try {
    await submitQueryFeedback("incorrect");
  } catch (error) {
    toast(error.message);
  }
});
$("#skill-submit-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const button = $('button[type="submit"]', form);
  const skillName = $("#skill-role").value;
  const promptFragment = $("#skill-instructions").value.trim();
  const changeReason = $("#skill-reason").value.trim();
  if (!promptFragment || !changeReason) return;
  setBusy(button, true);
  try {
    const result = await api("/v1/text2sql/skills/propose", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        skill_name: skillName,
        patch: { prompt_fragment: promptFragment },
        change_reason: changeReason,
      }),
    });
    const output = $("#skill-submit-result");
    output.classList.remove("hidden");
    output.innerHTML = `<b>候选 Agent Policy 已保存</b><span>${escapeHtml(result.skill_name)} · ${escapeHtml(result.candidate_policy_version)}</span><small>下一步：Validation 与 Sealed Holdout 离线评测。稳定版本尚未改变。</small>`;
    form.reset();
    $("#skill-role").value = "sql-generation";
    await Promise.all([loadSkills(), loadStatus()]);
    toast("候选 Agent Policy 已进入隔离队列");
  } catch (error) {
    toast(error.message);
  } finally {
    setBusy(button, false);
  }
});
$("#refresh-skills").addEventListener("click", loadSkills);
$("#refresh-experiences").addEventListener("click", loadExperiences);
$("#refresh-memory").addEventListener("click", loadMemory);
$("#refresh").addEventListener("click", loadWorkspace);
window.addEventListener("hashchange", () => show(location.hash.slice(1), false));

$("#runtime-blueprint").innerHTML = renderRuntimeMap({}, { mode: "blueprint" });
$("#evolution-runtime-graph").innerHTML = renderRuntimeMap({}, { mode: "blueprint" });
show(location.hash.slice(1), false);
loadWorkspace();
