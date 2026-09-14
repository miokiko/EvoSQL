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
const governanceViews = new Set(["trace", "memory", "data", "skills", "evaluation", "evolution"]);

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
  { id: "text2sql-evidence-orchestration", label: "Evidence", actor: "runtime", kind: "runtime", phase: "GROUND", description: "固定 Snapshot、Vanna 与 Memory 证据" },
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
let governanceMode = false;
const selectedExperienceIds = new Set();
const selectedSemanticRuleIds = new Set();
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
let queryInFlight = false;
let activeClarification = null;
try {
  activeClarification = JSON.parse(sessionStorage.getItem("evosql_clarification") || "null");
} catch (_) {}

function setClarification(value, clearInput = false) {
  activeClarification = value;
  try {
    if (value) sessionStorage.setItem("evosql_clarification", JSON.stringify(value));
    else sessionStorage.removeItem("evosql_clarification");
  } catch (_) {}
  $("#clarification-panel").classList.toggle("hidden", !value);
  $("#clarification-original").textContent = value ? `原问题：${value.question}` : "";
  $("#clarification-questions").innerHTML = (value?.questions || [])
    .map((question) => `<li>${escapeHtml(question)}</li>`).join("");
  $("#text2sql-question").placeholder = value ? "请输入补充信息，然后发送" : "例如：强烈岩爆案例有多少个？";
  if (clearInput) $("#text2sql-question").value = "";
}

function setQueryStatus(state, message = "", error = null) {
  const panel = $("#query-status");
  panel.classList.toggle("hidden", !["running", "error"].includes(state));
  panel.dataset.state = state;
  $("#query-status-message").textContent = message;
  $("#query-status-details").classList.toggle("hidden", !error);
  $("#query-status-details").open = false;
  $("#query-status-technical").textContent = error
    ? `任务：${error.taskId || "未创建"}\n${error.code || "request_failed"}\n${error.message || ""}` : "";
  $("#query-retry").classList.toggle("hidden", state !== "error" || !pendingText2SQLQuery);
  $("#query-restart").classList.toggle("hidden", state !== "error" || Boolean(pendingText2SQLQuery));
  $("#clarification-panel").classList.toggle("hidden", state !== "clarification" || !activeClarification);
  if (state !== "success") $("#text2sql-result").classList.add("hidden");
}

function queryFailure(error) {
  const raw = String(error.message || "");
  if (error.code === "query_identity_conflict" || raw.includes("task_id was reused")) {
    return { fresh: true, message: "请求或运行版本已变化，旧任务无法继续。点击“重新发起”使用当前配置查询。" };
  }
  if (error.status === 401 || error.status === 403) {
    return { fresh: true, message: "当前登录状态或权限已变化，请确认登录后重新发起查询。" };
  }
  if (error.code === "query_response_contract_error" || /invalid action|final action/.test(raw)) {
    return { fresh: false, message: "模型未返回有效的查询指令。你的补充内容已保留，可以重试。" };
  }
  if (!error.status) return { fresh: false, message: "连接中断，暂时无法确认执行结果。重试会使用同一任务，避免重复执行。" };
  if (error.status >= 500) return { fresh: false, message: "本次查询未完成。输入内容已保留，请稍后重试。" };
  return { fresh: true, message: /[\u4e00-\u9fff]/.test(raw) ? raw : "请求未被接受，请检查问题后重新发起。" };
}

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

function sourceCount(value) {
  if (value && typeof value === "object") return Number(value.count ?? value.item_count ?? 0);
  return Number(value || 0);
}

function firstCount(...values) {
  const value = values.find((item) => item !== undefined && item !== null);
  return sourceCount(value);
}

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  const token = sessionStorage.getItem("evosql_access_token");
  if (token) headers.set("Authorization", "Bearer " + token);
  const response = await fetch(path, { ...options, headers });
  if (response.status === 401) {
    sessionStorage.removeItem("evosql_access_token");
    const dialog = $("#login-dialog");
    if (!dialog.open) dialog.showModal();
  }
  const contentType = response.headers.get("content-type") || "";
  const data = contentType.includes("json") ? await response.json() : await response.text();
  if (!response.ok) {
    const plain = typeof data === "string" && !/<[a-z][\s\S]*>/i.test(data) ? data.trim() : "";
    const message = typeof data === "object" ? data.error || data.detail : plain;
    const error = new Error(message || `请求失败 (${response.status})`);
    error.status = response.status;
    error.code = typeof data === "object" ? data.code || "" : "";
    throw error;
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

function setGovernanceMode(enabled, { navigate = true } = {}) {
  governanceMode = Boolean(enabled);
  document.body.classList.toggle("governance-mode", governanceMode);
  const toggle = $("#governance-toggle");
  toggle.classList.toggle("mode-active", governanceMode);
  toggle.setAttribute("aria-expanded", governanceMode ? "true" : "false");
  toggle.setAttribute("aria-label", governanceMode ? "退出高级治理" : "打开高级治理");
  $("span", toggle).textContent = governanceMode ? "退出高级治理" : "高级治理";
  const modeChip = $("#workspace-mode-chip");
  modeChip.textContent = governanceMode ? "高级治理模式" : "用户模式";
  modeChip.classList.toggle("is-governance", governanceMode);
  const currentView = $(".view.active")?.id.replace("view-", "") || "query";
  if (!governanceMode && navigate && governanceViews.has(currentView)) show("query");
}

function show(view, updateHash = true) {
  const selected = views[view] ? view : "query";
  if (governanceViews.has(selected)) setGovernanceMode(true, { navigate: false });
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

function jumpToCurrentFeedback(taskId = "") {
  show("query");
  const resultVisible = !$("#text2sql-result").classList.contains("hidden");
  if (resultVisible && activeTaskId && (!taskId || taskId === activeTaskId)) {
    $("#query-feedback-panel").scrollIntoView({ behavior: reduceMotion.matches ? "auto" : "smooth", block: "center" });
    return;
  }
  toast("历史记录仅用于回看；请在刚完成的查询结果页提交反馈");
}

function bindFeedbackJumps(root) {
  $$(".feedback-jump", root).forEach((button) => button.addEventListener("click", () => {
    jumpToCurrentFeedback(button.dataset.feedbackTaskId || "");
  }));
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
  const vanna = status.vanna || {};
  const knowledgeSources = status.knowledge_sources || {};
  const counts = vanna.counts || {};
  const corpusCounts = vanna.corpus_counts || {};
  const businessDocumentCount = firstCount(
    knowledgeSources.business_documents,
    corpusCounts.business_documents,
    counts.documentation,
  );
  const ddlCount = firstCount(counts.ddl, corpusCounts.ddl, corpusCounts.schema);
  const documentationCount = firstCount(counts.documentation, corpusCounts.documentation, businessDocumentCount);
  const questionSqlCount = firstCount(
    counts.question_sql,
    counts.sql,
    corpusCounts.question_sql,
    knowledgeSources.question_sql,
  );
  const indexedCount = vanna.item_count === undefined || vanna.item_count === null
    ? ddlCount + documentationCount + questionSqlCount
    : Number(vanna.item_count || 0);
  const configured = Boolean(model.configured);
  const ready = Boolean(status.ready);
  const provider = model.provider || model.requested_provider || "aliyun-dashscope";
  const modelName = model.model || "未配置模型";
  const runtime = status.deterministic_runtime || {};
  const reviewMode = dataset.review_signature_verified
    ? "签名证书有效 · 审核人 匿名审核员"
    : dataset.review_verified
      ? "本地完整性校验通过 · 尚不等同于签名发布认证"
      : "需要审核证书";

  const readyBadge = $("#text2sql-ready");
  readyBadge.className = `status ${ready ? "status-online" : "status-neutral"}`;
  readyBadge.innerHTML = `<i></i>${ready ? "可以问答" : configured ? "资源校验中" : "等待模型配置"}`;
  $("#text2sql-model").textContent = configured ? `${provider} / ${modelName}` : `${modelName} 未连接`;
  $("#top-model").textContent = configured ? `${provider} · ${modelName}` : "模型未连接";
  $("#text2sql-runtime-note").textContent = configured
    ? "云端仅负责推理，SQLite 数据文件始终留在本机"
    : "数据库、业务文档、Vanna 索引和评测集可独立检查，问答需要模型配置";
  $("#runtime-contract-chip").textContent = `${runtime.protocol || "plan-first-text2sql-v3"} · ${number(runtime.node_count || 11)} nodes`;
  $("#runtime-blueprint").innerHTML = renderRuntimeMap(
    { deterministic_runtime: runtime },
    { mode: "blueprint" },
  );
  $("#text2sql-status-grid").innerHTML = [
    statusCard("本地数据库", database.ready ? `${number(database.table_count)} 张表` : "不可用", database.readonly ? "SQLite · 强制只读" : "只读状态未确认", database.ready ? "is-ready" : "is-warning"),
    statusCard("人工审核评测集", dataset.review_verified ? `${number(dataset.reviewed_case_count)} / ${number(dataset.case_count)}` : "未验证", reviewMode, dataset.review_verified ? "is-ready" : "is-warning"),
    statusCard("业务文档", `${number(businessDocumentCount)} 个知识块`, "实体 · 指标 · 维度 · 粒度 · 规则", businessDocumentCount ? "is-ready" : "is-warning"),
    statusCard("Vanna RAG", vanna.ready ? `${number(indexedCount)} 条索引` : "索引未就绪", vanna.ready ? `DDL / Documentation / Q-SQL · ${short(vanna.index_version, 18)}` : "请重新构建检索索引", vanna.ready ? "is-ready" : "is-warning"),
  ].join("");

  const submit = $(".text2sql-submit");
  submit.disabled = !ready;
  submit.title = ready ? "" : "模型或运行资源尚未就绪";
  $("#text2sql-form-note").textContent = ready
    ? "业务问题会发送给阿里云百炼进行推理；SQLite 数据文件不上传，最终 SQL 只在本机只读执行。"
    : "当前不能提问：请检查模型、数据库和人工审核评测集状态。";
}

function renderData(status) {
  const database = status.database || {};
  const vanna = status.vanna || {};
  const knowledgeSources = status.knowledge_sources || {};
  const counts = vanna.counts || {};
  const corpusCounts = vanna.corpus_counts || {};
  const schemaCount = firstCount(knowledgeSources.schema, corpusCounts.schema, counts.ddl);
  const businessDocumentCount = firstCount(
    knowledgeSources.business_documents,
    corpusCounts.business_documents,
    counts.documentation,
  );
  const approvedJoinCount = firstCount(knowledgeSources.approved_joins, corpusCounts.approved_joins);
  const ddlCount = firstCount(counts.ddl, corpusCounts.ddl, schemaCount);
  const documentationCount = firstCount(counts.documentation, corpusCounts.documentation, businessDocumentCount);
  const questionSqlCount = firstCount(
    counts.question_sql,
    counts.sql,
    corpusCounts.question_sql,
    knowledgeSources.question_sql,
  );
  const indexedCount = vanna.item_count === undefined || vanna.item_count === null
    ? ddlCount + documentationCount + questionSqlCount
    : Number(vanna.item_count || 0);
  const excludedTables = Array.isArray(knowledgeSources.excluded_tables)
    ? knowledgeSources.excluded_tables
    : [];
  const snapshotId = database.snapshot_id || vanna.database_snapshot_id || "--";
  $("#data-status-grid").innerHTML = [
    statusCard("Schema Snapshot", `${number(database.table_count || schemaCount)} 张表`, "数据库物理事实", database.ready ? "is-ready" : "is-warning"),
    statusCard("Business Documents", `${number(businessDocumentCount)} 个知识块`, "业务语义真源", businessDocumentCount ? "is-ready" : "is-warning"),
    statusCard("Vanna RAG", `${number(indexedCount)} 条索引`, "DDL + Documentation + Q-SQL", vanna.ready ? "is-ready" : "is-warning"),
    statusCard("Confirmed Q-SQL", `${number(questionSqlCount)} 对`, "用户确认后直接写入检索索引", "is-ready"),
  ].join("");
  $("#database-detail").innerHTML = detailRows([
    ["快照 ID", short(snapshotId, 28), true],
    ["表数量", `${number(database.table_count)} 张`],
    ["知识内容", "表、列、类型与字段注释"],
    ["已确认 Join", `${number(approvedJoinCount)} 条`],
    ["排除表", excludedTables.length ? excludedTables.join("、") : "无"],
    ["执行边界", database.readonly ? "SQLite Read-only · 仅本机" : "状态异常"],
  ]);
  const businessDocState = $("#business-doc-state");
  businessDocState.className = `status ${businessDocumentCount ? "status-online" : "status-neutral"}`;
  businessDocState.innerHTML = `<i></i>${businessDocumentCount ? "已接入" : "等待文档"}`;
  $("#business-doc-detail").innerHTML = detailRows([
    ["来源形态", "本地 Markdown 文档"],
    ["目录", "knowledge/business", true],
    ["知识内容", "实体、指标、维度、粒度与规则"],
    ["知识块", `${number(businessDocumentCount)} 个`],
    ["Vanna 映射", `${number(documentationCount)} 条 Documentation`],
    ["更新方式", "保存后同步索引"],
  ]);
  const vannaState = $("#vanna-state");
  vannaState.className = `status ${vanna.ready ? "status-online" : "status-neutral"}`;
  vannaState.innerHTML = `<i></i>${vanna.ready ? "检索就绪" : "索引未就绪"}`;
  $("#vanna-detail").innerHTML = detailRows([
    ["运行模式", vanna.mode || "retriever_only", true],
    ["索引版本", short(vanna.index_version, 30), true],
    ["DDL", `${number(ddlCount)} 条`],
    ["Documentation", `${number(documentationCount)} 条`],
    ["Question-SQL", `${number(questionSqlCount)} 对`],
    ["绑定快照", short(vanna.database_snapshot_id || snapshotId, 28), true],
    ["数据来源", "Schema + 业务文档 + 用户确认案例"],
    ["Vanna 后端 SQL 生成", vanna.generation_enabled ? "开启（异常）" : "关闭"],
    ["Node 2 草稿", vanna.node2_draft_generation_enabled ? "冻结上下文生成，不执行" : "关闭"],
    ["SQL 执行", vanna.sql_execution_enabled ? "开启（异常）" : "永久关闭"],
  ]);
}

function renderEvaluation(status) {
  const dataset = status.dataset || {};
  const splits = dataset.split_counts || {};
  const verified = Boolean(dataset.review_verified);
  const signatureVerified = Boolean(dataset.review_signature_verified);
  const caseCount = number(dataset.case_count);
  const reviewedCount = number(dataset.reviewed_case_count);
  const verificationLabel = signatureVerified
    ? "SIGNED VERIFIED"
    : verified
      ? "LOCAL INTEGRITY"
      : "UNVERIFIED";
  const verificationDetail = signatureVerified
    ? "审核人：匿名审核员 · HMAC 签名有效 · 数据集和数据库快照已绑定"
    : "未配置 HMAC 发布签名 · 文件哈希、逐题审核记录和数据库快照已完成本地完整性校验";
  $("#evaluation-banner").className = `evaluation-banner panel ${verified ? "is-verified" : "is-warning"}`;
  $("#evaluation-banner").innerHTML = `<div><span>${verificationLabel}</span><strong>${verified ? `${caseCount} 条样本已完成人工审核` : "评测集审核证据不完整"}</strong>${verified ? '<p class="evaluation-scope">Policy 发布评测：Validation + Sealed Holdout，共 96 条。</p>' : ''}<small>${verified ? verificationDetail : escapeHtml(dataset.error || "请检查审核证书")}</small></div><b>${verified ? `${reviewedCount}/${caseCount}` : "--"}</b>`;
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
    ["审核证据", verified ? signatureVerified ? "完整（HMAC 签名已验）" : "完整（仅本地完整性校验）" : "不完整"],
    ["发布评测范围", "Validation 48 + Sealed 48 = 96 条"],
    ["审核人", "匿名审核员"],
    ["证书 SHA", short(dataset.certificate_sha256, 30), true],
    ["数据集 SHA", short(dataset.dataset_sha256, 30), true],
  ]);
}

function renderEvolution(status) {
  const evolution = status.evolution || {};
  const release = evolution.release || {};
  const experiences = evolution.experience_counts || {};
  const semanticExperiences = evolution.semantic_experience_counts || {};
  const policyCandidates = Array.isArray(evolution.policy_candidates) ? evolution.policy_candidates : [];
  const vannaCounts = status.vanna?.counts || {};
  const vannaCorpusCounts = status.vanna?.corpus_counts || {};
  const confirmedQuestionSqlCount = firstCount(
    vannaCounts.question_sql,
    vannaCounts.sql,
    vannaCorpusCounts.question_sql,
    status.knowledge_sources?.question_sql,
    experiences.promoted,
  );
  $("#evolution-stats").innerHTML = [
    statusCard("已确认经验", number(semanticExperiences.confirmed), "不会直接进入 Agent Prompt"),
    statusCard("Vanna Question-SQL", number(confirmedQuestionSqlCount), "用户确认的检索案例"),
    statusCard("当前策略", evolution.active_policy_version || "未初始化", "稳定版本"),
    statusCard("发布阶段", release.status && release.status !== "inactive" ? release.status : "未启用", "Shadow / Canary 门禁"),
  ].join("");
  $("#evolution-detail").innerHTML = detailRows([
    ["策略版本", evolution.active_policy_version || "--", true],
    ["记忆快照", short(evolution.memory_snapshot_id, 32), true],
    ["发布状态", release.status || "inactive"],
    ["候选策略", release.candidate_policy_version || "无"],
    ["已确认 Q-SQL", `${number(confirmedQuestionSqlCount)} 对`],
    ["演进原则", "经验沉淀、单 Agent 候选、门禁发布"],
  ]);
  $("#evolution-candidates").innerHTML = policyCandidates.length
    ? [...policyCandidates].reverse().map((candidate) => {
        const metadata = candidate.proposal_metadata || {};
        const memoryIds = metadata.memory_ids || metadata.compiled_memory_ids || [];
        const ruleIds = metadata.semantic_rule_ids || [];
        const replay = candidate.target_replay || {};
        const replayStatus = replay.status || (memoryIds.length ? "pending" : "not_required");
        const replayArtifact = replay.artifact || {};
        const replaySummary = replayArtifact.summary || {};
        const replayFailures = Array.isArray(replayArtifact.results)
          ? replayArtifact.results.filter((item) => !item.passed)
              .flatMap((item) => item.reasons || [])
          : [];
        const replayDetail = replay.replay_id
          ? `<details class="policy-prompt-diff target-replay-detail"><summary>查看 Target Replay 结果</summary><div><section><b>RESULT</b><pre>${escapeHtml(`${number(replaySummary.passed_count)}/${number(replaySummary.source_experience_count)} 来源通过 · ${number(replaySummary.failed_count)} 失败`)}</pre></section><section><b>AUDIT</b><pre>${escapeHtml(`Replay ${short(replay.replay_id, 28)}\nArtifact ${short(replay.artifact_sha256, 32)}\n${replay.artifact_path || "Store only"}`)}</pre></section></div>${replayFailures.length ? `<small>失败代码 · ${escapeHtml([...new Set(replayFailures)].join(", "))}</small>` : ""}</details>`
          : "";
        const promptChange = candidate.prompt_fragment_change || {};
        const promptDiff = promptChange.changed
          ? `<details class="policy-prompt-diff"><summary>查看 Prompt Fragment 前后变化</summary><div><section><b>BEFORE</b><pre>${escapeHtml(promptChange.before || "（空）")}</pre></section><section><b>AFTER</b><pre>${escapeHtml(promptChange.after || "（空）")}</pre></section></div></details>`
          : "";
        return `<article class="candidate-item evolution-candidate-item"><span><strong>${escapeHtml(candidate.target_skill || "unknown")}</strong><small>${escapeHtml(candidate.change_reason || "无变更说明")}</small><small>来源 Experience · ${escapeHtml(memoryIds.length ? memoryIds.map((value) => short(value, 18)).join(", ") : "无")}</small>${ruleIds.length ? `<small>来源 Rule · ${escapeHtml(ruleIds.map((value) => short(value, 24)).join(", "))}</small>` : ""}</span><div><b>${escapeHtml(candidate.status || "candidate")}</b><code title="${escapeHtml(candidate.policy_version || "")}">${escapeHtml(short(candidate.policy_version, 20))}</code><em class="replay-status replay-${escapeHtml(replayStatus)}">TARGET REPLAY · ${escapeHtml(replayStatus)}</em></div>${replayDetail}${promptDiff}</article>`;
      }).join("")
    : '<div class="empty-state compact"><span><b>暂无 Experience 驱动的 Policy Candidate</b>先在 Memory 页面确认并选择同一 Agent 的经验。</span></div>';
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

function formatTraceTime(value) {
  if (!value) return "刚刚";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit" }).format(date);
}

function formatMemoryDateTime(value) {
  if (!value) return "时间未记录";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(date);
}

function formatDuration(value) {
  const duration = Number(value || 0);
  if (!duration) return "耗时未记录";
  if (duration < 1000) return `${number(Math.round(duration))} ms`;
  return `${(duration / 1000).toFixed(duration < 10000 ? 1 : 0)} s`;
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
  if (payload.status === "needs_clarification") {
    const stage = payload.clarification?.stage;
    const stoppedAt = { routing: 0, planning_workers: 2, plan_approval: 4, plan_revisions: 5 }[stage] ?? 0;
    return index < stoppedAt ? "completed" : index === stoppedAt ? "awaiting_input" : "bypassed";
  }
  if (queryType === "RESULT_QA") {
    if (index === 0) return "completed";
    if (index === runtimeNodeCatalog.length - 1) {
      return payload.status === "success" ? "replay" : "blocked";
    }
    return "bypassed";
  }

  const agents = Array.isArray(payload.agents) ? payload.agents : [];
  const agentRan = (stage) => agents.some((item) => item.stage === stage && !["not-run", "skipped"].includes(item.status));
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
      return generation.status && !["not-run", "skipped"].includes(generation.status) ? "completed" : "bypassed";
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
    awaiting_input: "待补充",
    completed: "DONE",
    blocked: "BLOCK",
    bypassed: "SKIP",
    replay: "REPLAY",
  }[state] || state;
}

function renderRuntimeMap(payload = {}, { compact = false, mode = "result" } = {}) {
  const nodes = runtimeDefinitions(payload);
  const items = nodes.map((node, index) => {
    if (node.id === "text2sql-lead-final" && (payload.agents || []).some(
      (item) => item.stage === "final-selection" && item.detail?.selection_method === "deterministic_single_candidate"
    )) {
      node = { ...node, kind: "harness", actor: "text2sql-harness", label: "Candidate Selection",
        description: "直接选择唯一通过审查的候选，继续执行最终校验" };
    }
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
    ? `<div class="trace-memory-usage"><span>LEGACY RUNTIME MEMORY</span>${memoryHits.map((item) => `<div><b>${escapeHtml(item.role || "agent")} · ${escapeHtml(item.phase || "run")}</b><code>${escapeHtml((item.memory_ids || []).join(", "))}</code></div>`).join("")}</div>`
    : `<div class="trace-memory-usage empty"><span>LEGACY RUNTIME MEMORY</span><small>本次没有命中兼容保留的 AgentSemanticRule</small></div>`;
  const routeDetail = `<div class="trace-route"><span><b>${escapeHtml(queryType)}</b>路由类型</span><span><b>${escapeHtml(trace.parent_task_id ? short(trace.parent_task_id, 24) : "无")}</b>父 QueryRun</span><span><b>${number(retrieval.length)}</b>检索调用</span><span><b>${number(vannaHits.length)}</b>Vanna 命中批次</span><span><b>${number(memoryHits.length)}</b>Legacy 记忆注入阶段</span></div>`;
  const planning = queryType === "RESULT_QA" ? "" : `<div class="trace-planning"><div><span>SCHEMA PLAN</span><code>${escapeHtml(JSON.stringify({ tables: schemaPlan.tables || [], columns: schemaPlan.columns || [], joins: schemaPlan.joins || [] }))}</code></div><div><span>QUERY SPEC</span><code>${escapeHtml(JSON.stringify(querySpec))}</code></div></div>`;
  const forwardSummary = draftPack.forward_linking || {};
  const reverseSummary = {
    projection_columns: draftPack.projection_columns || [],
    filter_columns: draftPack.filter_columns || [],
    group_columns: draftPack.group_columns || [],
    order_columns: draftPack.order_columns || [],
    join_columns: draftPack.join_columns || [],
    unresolved_columns: draftPack.unresolved_columns || [],
    ambiguous_columns: draftPack.ambiguous_columns || [],
    column_owners: draftPack.column_owners || {},
  };
  const draftPlanning = queryType === "RESULT_QA" || !draftPack.contract ? "" : `<div class="trace-planning"><div><span>DRAFT SQL · UNTRUSTED</span><code>${escapeHtml(draftPack.draft_sql || "-- Vanna 草稿失败，已回退到正向链接")}</code></div><div><span>FORWARD LINKING · LLM + KEYWORD</span><code>${escapeHtml(JSON.stringify({ model: forwardSummary.model || {}, keyword: forwardSummary.keyword || {} }))}</code></div><div><span>REVERSE AST + SNAPSHOT</span><code>${escapeHtml(JSON.stringify(reverseSummary))}</code></div><div><span>SCHEMA LINK PACK</span><code>${escapeHtml(JSON.stringify({ tables: draftPack.tables || [], columns: draftPack.columns || [], joins: draftPack.joins || [], semantic_completion: draftPack.semantic_completion || {}, coverage: draftPack.coverage || {} }))}</code></div></div>`;
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
    confirmed: "已确认",
    needs_evidence: "待补证",
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

function semanticRuleFields(item, editable) {
  const rule = item && typeof item.rule === "object" ? item.rule : {};
  const values = {
    trigger: rule.trigger || "",
    action: rule.action || item.content || "",
    avoid: rule.avoid || "",
    rationale: rule.rationale || "",
  };
  const labels = {
    trigger: "何时触发",
    action: "应该怎么做",
    avoid: "禁止什么",
    rationale: "为什么",
  };
  if (editable) {
    return '<div class="semantic-rule-fields is-editable">'
      + Object.entries(labels).map(([field, label]) => '<label><span>' + escapeHtml(label)
        + '</span><textarea data-memory-rule-field="' + field + '" maxlength="900" rows="2">'
        + escapeHtml(values[field]) + '</textarea></label>').join("")
      + '</div>';
  }
  return '<div class="semantic-rule-fields">'
    + Object.entries(labels).map(([field, label]) => '<div><b>' + escapeHtml(label)
      + '</b><span>' + escapeHtml(values[field] || "未记录") + '</span></div>').join("")
    + '</div>';
}

function isExperienceMemory(item) {
  return item?.rule?.contract === "ExperienceMemory/v1" || item?.contract === "ExperienceMemory/v1";
}

function experienceMemoryFields(item) {
  const experience = isExperienceMemory(item) ? (item.rule?.contract === "ExperienceMemory/v1" ? item.rule : item) : {};
  const before = experience.before && typeof experience.before === "object" ? experience.before : {};
  const after = experience.after && typeof experience.after === "object" ? experience.after : {};
  const evidence = experience.evidence && typeof experience.evidence === "object" ? experience.evidence : {};
  const pins = Object.entries(evidence)
    .filter(([, value]) => typeof value === "string" || typeof value === "number" || typeof value === "boolean")
    .slice(0, 6)
    .map(([key, value]) => `<span><b>${escapeHtml(key.replaceAll("_", " "))}</b>${escapeHtml(short(value, 30))}</span>`)
    .join("");
  return `<div class="experience-fields">
    <div><b>适用场景</b><span>${escapeHtml(experience.scenario || "未记录")}</span></div>
    <div class="experience-problem"><b>发现的问题</b><span>${escapeHtml(experience.problem || "未记录")}</span></div>
    <div class="experience-correction"><b>验证后的修正</b><span>${escapeHtml(experience.correction || "尚缺少可验证修正")}</span></div>
  </div>
  <details class="experience-evidence"><summary>查看前后证据与版本 Pin</summary>
    <div class="experience-before-after"><div><b>BEFORE</b><code>${escapeHtml(JSON.stringify(before))}</code></div><div><b>AFTER</b><code>${escapeHtml(JSON.stringify(after))}</code></div></div>
    <div class="experience-pins">${pins || "<span>没有公开版本 Pin</span>"}</div>
  </details>`;
}

function renderMemory(data) {
  const layers = data.layers || {};
  const working = layers.working || {};
  const episodic = layers.episodic || {};
  const semantic = layers.semantic || {};
  const sessionView = data.session_view || {};
  const showingHistory = sessionView.mode === "latest_history";
  const displaySession = sessionView.display_session_id || data.session_id || "";
  const semanticCounts = semantic.experience_counts || semantic.counts || {};
  const ruleCounts = data.semantic_rules?.counts || {};
  const snapshot = short(data.memory_snapshot_id, 22);
  const workingLimit = Number(working.retention_limit_per_session || 100);
  const episodicLimit = Number(episodic.retention_limit || 50);
  const sessionDetail = showingHistory
    ? `最近历史会话 · ${short(displaySession, 18)}`
    : "当前浏览器会话";

  $("#memory-stats").innerHTML = [
    statusCard("Working Memory", `${number(working.count)} 条消息`, `${sessionDetail} · 最多保留 ${number(workingLimit)} 条`, "is-ready"),
    statusCard("Episodic Memory", `${number(episodic.count)} 次 QueryRun`, `${sessionDetail} · 页面展示最近 ${number(episodicLimit)} 次`, "is-ready"),
    statusCard(
      "Semantic Rules",
      `${number(ruleCounts.confirmed)} 条已确认规则`,
      `${number(ruleCounts.candidate)} 条待审核规则 · ${number(semanticCounts.confirmed)} 条已确认案例 · ${snapshot}`,
      ruleCounts.candidate || semanticCounts.needs_evidence ? "is-warning" : ""
    ),
  ].join("");

  const sessionNotice = $("#memory-session-notice");
  sessionNotice.classList.toggle("hidden", !showingHistory);
  sessionNotice.innerHTML = showingHistory
    ? `<b>当前会话暂无记录</b><span>正在回看最近历史会话 <code>${escapeHtml(short(displaySession, 24))}</code>；新查询仍会写入当前会话。</span>`
    : "";
  $("#working-memory-scope").textContent = showingHistory
    ? `历史会话快照 · 最多 ${number(workingLimit)} 条消息`
    : `当前会话 · 最多 ${number(workingLimit)} 条消息`;
  $("#episodic-memory-scope").textContent = showingHistory
    ? `历史 QueryRun · 页面展示最近 ${number(episodicLimit)} 个`
    : `当前 QueryRun · 页面展示最近 ${number(episodicLimit)} 个`;

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
        const temporal = item.temporal_context || {};
        const version = item.version_context || {};
        const harness = decisions.harness || {};
        const human = decisions.human || {};
        const harnessOutcome = harness.outcome || (item.status === "success" ? "accepted" : "rejected");
        const harnessLabel = { accepted: "放行", rejected: "拒绝", failed: "失败", deferred: "待新查询" }[harnessOutcome] || harnessOutcome;
        const humanLabel = { accepted: "确认", rejected: "拒绝" }[human.outcome] || "待审核";
        const humanReview = human.decision_id
          ? '<div class="episodic-human-result state-' + escapeHtml(human.outcome || "unknown") + '"><div><b>HUMAN · '
            + escapeHtml(humanLabel) + '</b><span>' + escapeHtml(human.actor || "人工审核") + '</span></div><p>'
            + escapeHtml(human.reason_text || "未填写评论") + '</p></div>'
          : '<div class="episodic-human-result state-pending"><div><b>HUMAN · 待反馈</b><span>历史记录只读</span></div><p>正确 / 错误反馈统一在刚完成的查询结果页提交。</p><button class="copy-button feedback-jump" data-feedback-task-id="'
            + escapeHtml(item.task_id || "") + '" type="button">返回问答工作台</button></div>';
        return '<article class="memory-entry episodic-entry" data-task-id="' + escapeHtml(item.task_id || "")
          + '"><div class="memory-entry-head"><span class="memory-state state-' + escapeHtml(harnessOutcome) + '">HARNESS · ' + escapeHtml(harnessLabel)
          + '</span><time>' + escapeHtml(formatMemoryDateTime(temporal.recorded_at || item.recorded_at)) + '</time></div>'
          + '<div class="episodic-time-context"><span><b>TURN</b>第 ' + number(temporal.turn_number || 1)
          + ' 次 QueryRun</span><span><b>DURATION</b>' + escapeHtml(formatDuration(temporal.duration_ms))
          + '</span><span><b>LINK</b>' + escapeHtml(item.parent_task_id ? "追问 " + short(item.parent_task_id, 16) : "独立查询")
          + '</span></div><p>'
          + escapeHtml(short(question, 150)) + '</p><code>' + escapeHtml(short(item.final_sql || "-- 未生成 SQL", 190))
          + '</code><div class="episodic-decision-reason"><b>' + escapeHtml(harness.reason_code || item.status || "unknown")
          + '</b><span>' + escapeHtml(harness.reason_text || "未记录 Harness 原因") + '</span></div><small>'
          + escapeHtml(item.query_type || "DATA_QUERY") + ' · HUMAN ' + escapeHtml(humanLabel)
          + ' · DB ' + escapeHtml(short(version.database_snapshot_id, 13))
          + ' · POLICY ' + escapeHtml(short(version.policy_version, 13)) + '</small>'
          + humanReview + '</article>';
      }).join("")
    : memoryEmpty("当前会话还没有情景记忆", "每次 QueryRun 的问题、SQL、门禁状态与反馈会形成可追溯片段。");
  bindFeedbackJumps($("#episodic-memory-list"));

  const semanticItems = Array.isArray(semantic.items) ? semantic.items : [];
  const confirmedIds = new Set(
    semanticItems
      .filter((item) => isExperienceMemory(item) && item.state === "confirmed")
      .map((item) => String(item.memory_id || ""))
  );
  [...selectedExperienceIds].forEach((memoryId) => {
    if (!confirmedIds.has(memoryId)) selectedExperienceIds.delete(memoryId);
  });
  $("#semantic-memory-list").innerHTML = semanticItems.length
    ? semanticItems.map((item) => {
        const state = String(item.state || "candidate");
        const isExperience = isExperienceMemory(item);
        const targetAgent = item.target_agent || item.target_skill || "shared";
        const problemCode = item.problem_code || item.failure_kind || "general";
        const provenance = item.reviewed_by
          ? "审核人 " + item.reviewed_by
          : "来源 " + (item.origin_split || "production_feedback");
        const selector = isExperience && state === "confirmed" && targetAgent !== "sql-generation"
          ? '<label class="experience-selector"><input type="checkbox" data-experience-select value="'
            + escapeHtml(item.memory_id || "") + '"' + (selectedExperienceIds.has(item.memory_id) ? " checked" : "")
            + '><span>用于生成 Policy</span></label>'
          : "";
        const review = isExperience && state === "candidate"
          ? '<div class="experience-review"><textarea data-experience-review-note maxlength="2000" rows="2" placeholder="审核备注；拒绝或待补证时必填"></textarea><div class="memory-review-actions"><button class="copy-button experience-review-action" data-decision="reject" type="button">拒绝</button><button class="copy-button experience-review-action" data-decision="needs_evidence" type="button">待补证</button><button class="button experience-review-action" data-decision="confirm" type="button">确认经验</button></div></div>'
          : "";
        const generateRule = isExperience && state === "confirmed" && Object.prototype.hasOwnProperty.call(roleDetails, targetAgent)
          ? '<button class="button generate-semantic-rule" type="button">归纳语义规则</button>' : "";
        const body = isExperience
          ? experienceMemoryFields(item) + review + generateRule
          : '<div class="legacy-memory-notice"><b>LEGACY RUNTIME HINT · 只读</b><span>旧 AgentSemanticRule/v1 不再由新流程创建，也不能在这里重新发布。</span></div>' + semanticRuleFields(item, false);
        const usedPolicies = Array.isArray(item.used_in_policy_versions) && item.used_in_policy_versions.length
          ? '<div class="experience-policy-links"><b>已用于 Policy</b>' + item.used_in_policy_versions.map((version) => '<code>' + escapeHtml(short(version, 24)) + '</code>').join("") + '</div>'
          : "";
        return '<article class="memory-entry semantic-entry" data-memory-id="' + escapeHtml(item.memory_id || "")
          + '" data-target-agent="' + escapeHtml(targetAgent)
          + '"><div class="memory-entry-head"><span class="memory-state state-'
          + escapeHtml(state.replace(/[^a-z0-9_-]/gi, "")) + '">' + escapeHtml(memoryStateLabel(state))
          + '</span>' + selector + '<time>' + escapeHtml(formatTraceTime(item.reviewed_at || item.created_at))
          + '</time></div>' + body + '<div class="memory-entry-tags"><span>'
          + escapeHtml(targetAgent) + '</span><span>' + escapeHtml(problemCode)
          + '</span><span>' + escapeHtml((item.rule?.evidence_grade || item.evidence_grade || "legacy"))
          + '</span></div>' + usedPolicies
          + (item.review_note ? '<blockquote class="review-note"><b>审核评论</b>' + escapeHtml(item.review_note) + '</blockquote>' : '')
          + '<small>' + escapeHtml(provenance) + ' · ' + escapeHtml(item.source_task_id || item.source_case_ids?.[0] || "无来源 Task") + '</small></article>';
      }).join("")
    : memoryEmpty("尚未沉淀 Semantic Experience", "普通成功查询不会生成经验；只有明确纠错或确定性修复才形成候选。");
  $$(".experience-review-action", $("#semantic-memory-list")).forEach((button) => button.addEventListener("click", async () => {
    const card = button.closest("[data-memory-id]");
    const decision = button.dataset.decision;
    const reviewNote = $("[data-experience-review-note]", card)?.value.trim() || "";
    const payload = {
      decision,
      review_note: reviewNote,
    };
    if (["reject", "needs_evidence"].includes(decision) && !reviewNote) {
      toast(decision === "reject" ? "拒绝 Experience 时必须填写理由" : "标记待补证时必须说明缺什么证据");
      $("[data-experience-review-note]", card)?.focus();
      return;
    }
    $$(".experience-review-action", card).forEach((item) => item.disabled = true);
    try {
      const result = await api("/v1/text2sql/memories/" + encodeURIComponent(card.dataset.memoryId) + "/review", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      toast(result.state === "confirmed" ? "Experience 已确认；运行时尚未改变" : result.state === "needs_evidence" ? "Experience 已标记为待补证" : "Experience 已拒绝");
      await Promise.all([loadMemory(), loadStatus()]);
    } catch (error) {
      toast(error.message);
      $$(".experience-review-action", card).forEach((item) => item.disabled = false);
    }
  }));
  $$(".generate-semantic-rule", $("#semantic-memory-list")).forEach((button) => button.addEventListener("click", async () => {
    const memoryId = button.closest("[data-memory-id]").dataset.memoryId;
    button.disabled = true;
    button.textContent = "正在归纳…";
    try {
      const result = await api("/v1/text2sql/semantic-rules/generate", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ memory_id: memoryId }),
      });
      toast(result.status === "skipped" ? `未生成规则：${result.reason}` : "已生成语义规则，待审核原因、适用条件与证据");
      await loadMemory();
    } catch (error) { toast(error.message); }
    finally { button.disabled = false; button.textContent = "归纳语义规则"; }
  }));
  const updateSelection = () => {
    const selectedCards = $$('[data-memory-id]', $("#semantic-memory-list"))
      .filter((card) => selectedExperienceIds.has(card.dataset.memoryId));
    const agents = new Set(selectedCards.map((card) => card.dataset.targetAgent));
    const valid = selectedCards.length > 0 && agents.size === 1;
    $("#experience-selection-count").textContent = selectedCards.length
      ? `已选择 ${selectedCards.length} 条 · ${agents.size === 1 ? selectedCards[0].dataset.targetAgent : "跨 Agent 不可生成"}`
      : "已选择 0 条 Confirmed Experience";
    $("#generate-experience-policy").disabled = !valid;
  };
  $$("[data-experience-select]", $("#semantic-memory-list")).forEach((input) => input.addEventListener("change", () => {
    if (input.checked) selectedExperienceIds.add(input.value);
    else selectedExperienceIds.delete(input.value);
    updateSelection();
  }));
  updateSelection();
  $("#generate-experience-policy").onclick = async () => {
    const button = $("#generate-experience-policy");
    const memoryIds = [...selectedExperienceIds];
    if (!memoryIds.length || button.disabled) return;
    button.disabled = true;
    button.textContent = "正在生成…";
    try {
      const result = await api("/v1/text2sql/policies/from-experiences", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ memory_ids: memoryIds, change_reason: $("#experience-policy-reason").value.trim() }),
      });
      selectedExperienceIds.clear();
      $("#experience-policy-reason").value = "";
      toast(`已生成 ${result.target_agent} Policy Candidate；等待定向回放`);
      await Promise.all([loadMemory(), loadSkills(), loadStatus()]);
    } catch (error) {
      toast(error.message);
    } finally {
      button.textContent = "生成 Policy 候选";
      updateSelection();
    }
  };

  renderSemanticRules(data.semantic_rules || {});
  clearTimeout(memoryPollTimer);
}

function renderSemanticRules(data) {
  const list = $("#semantic-rule-list");
  if (!list) return;
  const items = Array.isArray(data.items) ? data.items : [];
  const confirmed = new Set(items.filter((item) => item.state === "confirmed").map((item) => item.rule_id));
  [...selectedSemanticRuleIds].forEach((id) => { if (!confirmed.has(id)) selectedSemanticRuleIds.delete(id); });
  list.innerHTML = items.length ? items.map((item) => {
    const field = (label, value) => `<div><b>${label}</b><p>${escapeHtml(value)}</p></div>`;
    const targetAgent = item.target_agent || "unknown";
    const select = item.state === "confirmed"
      ? `<label class="experience-selector"><input type="checkbox" data-rule-select value="${escapeHtml(item.rule_id)}"${selectedSemanticRuleIds.has(item.rule_id) ? " checked" : ""}>用于编译策略</label>` : "";
    const review = item.state === "candidate"
      ? '<div class="experience-review"><textarea data-rule-note maxlength="2000" rows="2" placeholder="审核备注；拒绝时必填"></textarea><div class="memory-review-actions"><button class="copy-button rule-review-action" data-decision="reject" type="button">拒绝</button><button class="button rule-review-action" data-decision="confirm" type="button">确认规则</button></div></div>' : "";
    const evidence = item.evidence || {};
    const sqlRepairEvidence = Array.isArray(evidence.before_sql) || Array.isArray(evidence.after_sql);
    const evidenceDetails = sqlRepairEvidence
      ? `<details class="policy-prompt-diff"><summary>核对原问题、前后 SQL 与审批证据</summary><div><section><b>原问题</b><pre>${escapeHtml(evidence.question || "")}</pre><b>修复前 SQL</b><pre>${escapeHtml((evidence.before_sql || []).join("\n\n"))}</pre><b>首轮门禁</b><pre>${escapeHtml(JSON.stringify(evidence.before_gate || [], null, 2))}</pre></section><section><b>修复后 SQL</b><pre>${escapeHtml((evidence.after_sql || []).join("\n\n"))}</pre><b>修复后门禁</b><pre>${escapeHtml(JSON.stringify(evidence.after_gate || [], null, 2))}</pre><b>审批计划</b><pre>${escapeHtml(JSON.stringify(evidence.approved_query_plan || {}, null, 2))}</pre></section></div></details>`
      : `<details class="policy-prompt-diff"><summary>核对 Experience 与角色证据</summary><div><section><b>原问题</b><pre>${escapeHtml(evidence.question || "")}</pre><b>Experience</b><pre>${escapeHtml(JSON.stringify(evidence.experience || {}, null, 2))}</pre><b>QueryRun</b><pre>${escapeHtml(JSON.stringify(evidence.query_run || {}, null, 2))}</pre></section><section><b>角色证据</b><pre>${escapeHtml(JSON.stringify(evidence.role_artifacts || {}, null, 2))}</pre><b>人工决策</b><pre>${escapeHtml(JSON.stringify(evidence.human_decisions || [], null, 2))}</pre></section></div></details>`;
    return `<article class="memory-entry semantic-entry" data-rule-id="${escapeHtml(item.rule_id)}" data-target-agent="${escapeHtml(targetAgent)}"><div class="memory-entry-head"><span class="memory-state">${escapeHtml(memoryStateLabel(item.state))}</span><span class="memory-policy-chip">${escapeHtml(targetAgent)}</span>${select}</div><div class="semantic-rule-fields">`
      + field("原因", item.root_cause) + field("可复用规则", item.rule)
      + field("适用条件", (item.applicability || []).join("；")) + field("例外", (item.exceptions || []).join("；"))
      + `</div>${evidenceDetails}`
      + field("引用证据", (item.evidence_refs || []).join("、"))
      + `<small>来源 ${escapeHtml((item.source_memory_ids || []).join(", "))} · ${escapeHtml(item.source_task_id)} · revision ${number(item.source_revision)}</small>`
      + (item.reviewed_by ? field("审核", `${item.reviewed_by} · ${item.review_note || "已确认"}`) : "")
      + review + '</article>';
  }).join("") : memoryEmpty("尚无语义规则", "先确认任一 Agent 的纠错案例，再点击“归纳语义规则”；证据不足或没有可复用结论时会跳过。");
  $$(".rule-review-action", list).forEach((button) => button.addEventListener("click", async () => {
    const card = button.closest("[data-rule-id]");
    const note = $("[data-rule-note]", card)?.value.trim() || "";
    if (button.dataset.decision === "reject" && !note) { toast("请说明拒绝规则的原因"); return; }
    $$(".rule-review-action", card).forEach((control) => control.disabled = true);
    try {
      await api(`/v1/text2sql/semantic-rules/${encodeURIComponent(card.dataset.ruleId)}/review`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ decision: button.dataset.decision, review_note: note }),
      });
      toast(button.dataset.decision === "confirm" ? "规则已确认；编译、评测并发布策略后才生效" : "规则已拒绝");
      await loadMemory();
    } catch (error) { toast(error.message); $$(".rule-review-action", card).forEach((control) => control.disabled = false); }
  }));
  const updateSelection = () => {
    const selectedItems = items.filter((item) => selectedSemanticRuleIds.has(item.rule_id));
    const agents = new Set(selectedItems.map((item) => item.target_agent));
    const valid = selectedItems.length > 0 && selectedItems.length <= 20 && agents.size === 1;
    $("#rule-selection-count").textContent = selectedItems.length
      ? `已选择 ${selectedItems.length} 条 · ${agents.size === 1 ? selectedItems[0].target_agent : "跨 Agent 不可编译"}`
      : "已选择 0 条已确认规则";
    $("#generate-rule-policy").disabled = !valid;
  };
  $$("[data-rule-select]", list).forEach((input) => input.addEventListener("change", () => {
    if (input.checked) selectedSemanticRuleIds.add(input.value); else selectedSemanticRuleIds.delete(input.value);
    updateSelection();
  }));
  updateSelection();
  $("#generate-rule-policy").onclick = async () => {
    const button = $("#generate-rule-policy");
    button.disabled = true; button.textContent = "正在编译策略…";
    try {
      const result = await api("/v1/text2sql/policies/from-rules", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ rule_ids: [...selectedSemanticRuleIds], change_reason: $("#rule-policy-reason").value.trim() }),
      });
      selectedSemanticRuleIds.clear(); $("#rule-policy-reason").value = "";
      toast(`已生成 ${result.target_agent || "目标 Agent"} 策略候选；下一步运行定向回放`);
      await Promise.all([loadMemory(), loadSkills(), loadStatus()]);
    } catch (error) { toast(error.message); }
    finally { button.textContent = "编译 Agent 策略候选"; updateSelection(); }
  };
}

async function loadMemory() {
  try {
    const path = "/api/text2sql/memory?session_id=" + encodeURIComponent(text2sqlSessionId) + "&limit=12";
    renderMemory(await api(path));
  } catch (error) {
    $("#memory-stats").innerHTML = statusCard("记忆服务", "加载失败", error.message, "is-warning");
    $("#working-memory-list").innerHTML = memoryEmpty("Working Memory 加载失败", error.message);
    $("#episodic-memory-list").innerHTML = memoryEmpty("Episodic Memory 加载失败", error.message);
    $("#semantic-memory-list").innerHTML = memoryEmpty("Semantic Experience 加载失败", error.message);
  }
}

async function loadWorkspace() {
  const refresh = $("#refresh");
  refresh.disabled = true;
  refresh.textContent = "刷新中…";
  try {
    await Promise.all([loadStatus(), loadSkills(), loadTraces(), loadMemory()]);
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
  const needsClarification = result.status === "needs_clarification";
  setClarification(needsClarification ? {
    taskId: result.task_id, question: result.question,
    questions: result.clarification?.questions || [],
  } : accepted ? null : activeClarification, needsClarification);
  activeTaskId = result.task_id || "";
  activeQueryType = result.query_type || "DATA_QUERY";
  activeSql = result.final_sql || "";
  $("#text2sql-result").classList.remove("hidden");
  setQueryStatus(accepted ? "success" : needsClarification ? "clarification" : "error",
    result.diagnostic?.message || "查询未完成，请检查问题或查看运行记录。",
    !accepted && !needsClarification ? {
      taskId: result.task_id,
      code: result.diagnostic?.code || "query_rejected",
      message: [result.diagnostic?.stage, ...(result.diagnostic?.related_codes || [])].filter(Boolean).join(" · "),
    } : null);
  $("#text2sql-final-sql").textContent = activeSql || (activeQueryType === "RESULT_QA" ? "-- 使用历史 QueryRun 结果，本次没有生成或执行 SQL" : "-- 未生成可执行 SQL");
  $("#text2sql-answer-summary").textContent = accepted ? summary.value : needsClarification ? "需要补充信息" : "查询未执行";
  $("#text2sql-answer-meta").textContent = accepted ? summary.meta : needsClarification
    ? (result.clarification?.questions || []).join("；")
    : result.diagnostic?.message || answer.summary_text || (gates.errors || []).join("；");
  const gate = $("#text2sql-gate-status");
  gate.className = `status ${accepted ? "status-online" : "status-neutral"}`;
  gate.innerHTML = accepted
    ? `<i></i>${activeQueryType === "RESULT_QA" ? "会话结果回答" : "安全门禁通过"}`
    : needsClarification ? "等待补充" : `未执行${gates.errors?.length ? ` · ${gates.errors.length} 项拦截` : ""}`;
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
  (accepted ? $("#text2sql-result") : $("#text2sql-form")).scrollIntoView({ behavior: reduceMotion.matches ? "auto" : "smooth", block: "start" });
  if (needsClarification) $("#text2sql-question").focus();
}

function resetFeedback(enabled) {
  const panel = $("#query-feedback-panel");
  panel.classList.toggle("hidden", !enabled);
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
  await Promise.all([loadStatus(), loadTraces(), loadMemory()]);
  if (decision === "correct" && (result.question_sql_id || result.vanna_item_id || result.experience_id)) {
    toast("已写入 Vanna Question-SQL，可供后续检索");
  } else if (result.experience_id && result.memory_id) {
    toast("错误已记录；修正 SQL 和规则改进线索已保留");
  } else if (result.experience_id) {
    toast("错误已记录；修正 SQL 已作为纠错依据保留");
  } else if (result.memory_id) {
    toast("错误已记录，并形成一条规则改进线索");
  } else {
    toast("反馈已记录");
  }
}

function addSession(question, result, summary) {
  sessionHistory.unshift({ question, sql: result.final_sql || "--", value: result.status === "success" ? summary.value : result.status === "needs_clarification" ? "待补充" : "未执行", status: result.status });
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
  if (queryInFlight) return;
  const question = $("#text2sql-question").value.trim();
  if (!question) return;
  const clarificationTaskId = activeClarification?.taskId || "";
  if (!pendingText2SQLQuery || pendingText2SQLQuery.question !== question
      || (pendingText2SQLQuery.clarificationTaskId || "") !== clarificationTaskId) {
    pendingText2SQLQuery = {
      question,
      clarificationTaskId,
      taskId: "text2sql-web-" + (globalThis.crypto?.randomUUID?.() || Date.now() + "-" + Math.random().toString(16).slice(2)),
    };
    writePendingQuery(pendingText2SQLQuery);
  }
  const button = $(".text2sql-submit");
  const request = { ...pendingText2SQLQuery };
  queryInFlight = true;
  let failed = false;
  setBusy(button, true);
  $("#text2sql-question").disabled = true;
  $$("[data-sql-question], #clarification-cancel, #query-retry, #query-restart, #new-query").forEach(item => { item.disabled = true; });
  setQueryStatus("running", "正在理解问题并查询数据…");
  $("#runtime-blueprint").innerHTML = renderRuntimeMap({}, { mode: "running" });
  $("#text2sql-form-note").textContent = governanceMode
    ? "Lead 正在路由问题并编排证据；需要查库时会并行调度 Schema Grounding 与 Query Planning，再进行计划绑定、审批和 SQL Generation…"
    : "正在理解问题、核对数据并生成只读查询，请稍候…";
  try {
    const result = await api("/v1/text2sql/query", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        question,
        session_id: text2sqlSessionId,
        task_id: request.taskId,
        clarification_task_id: request.clarificationTaskId || "",
      }),
    });
    pendingText2SQLQuery = null;
    writePendingQuery(null);
    renderResult(result);
  } catch (error) {
    failed = true;
    $("#runtime-blueprint").innerHTML = renderRuntimeMap({}, { mode: "error" });
    const failure = queryFailure(error);
    if (failure.fresh) {
      pendingText2SQLQuery = null;
      writePendingQuery(null);
    }
    setQueryStatus("error", failure.message, { taskId: request.taskId, code: error.code, message: error.message });
    $("#text2sql-form-note").textContent = "输入内容已保留。";
  } finally {
    queryInFlight = false;
    $("#text2sql-question").disabled = false;
    $$("[data-sql-question], #clarification-cancel, #query-retry, #query-restart, #new-query").forEach(item => { item.disabled = false; });
    setBusy(button, false);
    if (runtimeStatus?.ready && !failed) $("#text2sql-form-note").textContent = "支持连续追问 · 数据库只读查询";
  }
}

$$('.nav-item[data-view]').forEach((button) => button.addEventListener("click", () => show(button.dataset.view)));
$("#governance-toggle").addEventListener("click", () => setGovernanceMode(!governanceMode));
$$('[data-sql-question]').forEach((button) => button.addEventListener("click", () => {
  if (queryInFlight) return;
  pendingText2SQLQuery = null;
  writePendingQuery(null);
  setClarification(null);
  setQueryStatus("idle");
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
$("#refresh-memory").addEventListener("click", loadMemory);
$("#refresh").addEventListener("click", loadWorkspace);
window.addEventListener("hashchange", () => show(location.hash.slice(1), false));

$("#runtime-blueprint").innerHTML = renderRuntimeMap({}, { mode: "blueprint" });
setClarification(activeClarification);
$("#query-retry").addEventListener("click", () => submitQuestion());
$("#query-restart").addEventListener("click", () => {
  pendingText2SQLQuery = null;
  writePendingQuery(null);
  submitQuestion();
});
$("#new-query").addEventListener("click", () => {
  if (queryInFlight) return;
  pendingText2SQLQuery = null;
  writePendingQuery(null);
  setClarification(null, true);
  setQueryStatus("idle");
  $("#text2sql-question").focus();
});
$("#clarification-cancel").addEventListener("click", () => {
  if (queryInFlight) return;
  setClarification(null, true);
  setQueryStatus("idle");
  pendingText2SQLQuery = null;
  writePendingQuery(null);
  $("#text2sql-question").focus();
});
$("#evolution-runtime-graph").innerHTML = renderRuntimeMap({}, { mode: "blueprint" });
show(location.hash.slice(1), false);
loadWorkspace();

$("#login-dialog").addEventListener("cancel", (event) => event.preventDefault());
$("#login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = event.currentTarget.querySelector("button");
  button.disabled = true;
  $("#login-error").textContent = "";
  try {
    const result = await api("/v1/auth/login", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: $("#login-username").value, password: $("#login-password").value }),
    });
    sessionStorage.setItem("evosql_access_token", result.access_token);
    $("#login-password").value = "";
    $("#login-dialog").close();
    $("#sign-out").classList.remove("hidden");
    await loadWorkspace();
  } catch (error) {
    $("#login-error").textContent = error.message;
  } finally {
    button.disabled = false;
  }
});
$("#sign-out").classList.toggle("hidden", !sessionStorage.getItem("evosql_access_token"));
$("#sign-out").addEventListener("click", () => {
  setClarification(null);
  pendingText2SQLQuery = null;
  writePendingQuery(null);
  sessionStorage.removeItem("evosql_access_token");
  location.reload();
});
