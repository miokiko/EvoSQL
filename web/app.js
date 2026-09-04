const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

const views = {
  query: { title: "问答工作台", kicker: "TEXT2SQL WORKSPACE" },
  trace: { title: "运行轨迹", kicker: "EXECUTION TRACE" },
  data: { title: "数据与知识", kicker: "DATABASE & KNOWLEDGE" },
  skills: { title: "Skills", kicker: "TEXT2SQL SKILLS" },
  evaluation: { title: "评测与审核", kicker: "EVALUATION & REVIEW" },
  evolution: { title: "自进化中心", kicker: "SELF-EVOLUTION" },
};

const roleDetails = {
  "text2sql-lead": ["Lead", "拆解问题、调度角色并选择最终候选"],
  "schema-grounding": ["Grounding", "定位表、字段、关联关系与结果粒度"],
  "sql-strategy": ["Strategy", "生成只读 SQL 候选并提供推理依据"],
  "text2sql-critic": ["Critic", "对候选 SQL 进行独立盲审与否决"],
  "vanna-draft-planner": ["Vanna Draft", "辅助生成不执行的草案，用于 AST 反向 Schema Linking"],
};

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
let toastTimer = null;
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
const pendingQueryKey = `evoagent.text2sql.pending.${text2sqlSessionId}`;

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
  document.title = `${views[selected].title} · EvoSQL`;
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

  const readyBadge = $("#text2sql-ready");
  readyBadge.className = `status ${ready ? "status-online" : "status-neutral"}`;
  readyBadge.innerHTML = `<i></i>${ready ? "可以问答" : configured ? "资源校验中" : "等待模型配置"}`;
  $("#text2sql-model").textContent = configured ? `${provider} / ${modelName}` : `${modelName} 未连接`;
  $("#top-model").textContent = configured ? `${provider} · ${modelName}` : "模型未连接";
  $("#text2sql-runtime-note").textContent = configured
    ? "云端仅负责推理，SQLite 数据文件始终留在本机"
    : "数据库、知识库和评测集可独立检查，问答需要模型配置";
  $("#text2sql-status-grid").innerHTML = [
    statusCard("本地数据库", database.ready ? `${number(database.table_count)} 张表` : "不可用", database.readonly ? "SQLite · 强制只读" : "只读状态未确认", database.ready ? "is-ready" : "is-warning"),
    statusCard("人工审核评测集", dataset.review_verified ? `${number(dataset.reviewed_case_count)} / ${number(dataset.case_count)}` : "未验证", dataset.review_verified ? "签名证书有效 · 审核人 匿名审核员" : "需要审核证书", dataset.review_verified ? "is-ready" : "is-warning"),
    statusCard("知识与记忆", `${number(stable)} 稳定 / ${number(candidate)} 候选`, `Wiki 索引 · ${short(knowledge.stable_index_version, 22)}`, "is-ready"),
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
    statusCard("Question-SQL 候选", number(experiences.candidate), "人工反馈后隔离审核"),
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
  $("#agent-role-grid").innerHTML = (status.roles || Object.keys(roleDetails)).map((role, index) => {
    const [name, detail] = roleDetails[role] || [role, "Text2SQL 协作角色"];
    return `<div><b>${String(index + 1).padStart(2, "0")}</b><span><strong>${escapeHtml(name)}</strong><small>${escapeHtml(detail)}</small></span></div>`;
  }).join("");
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
    statusCard("运行 Skills", number(skills.length), "四角色 Text2SQL 协议"),
    statusCard("稳定策略", short(data.active_policy_version, 24), "当前生产版本"),
    statusCard("候选版本", number(data.candidate_count), "隔离等待评测"),
    statusCard("提交契约", data.submission_contract || "--", "单次只允许修改一个 Skill"),
  ].join("");
  $("#text2sql-skill-list").innerHTML = skills.map((skill, index) => {
    const tools = (skill.allowed_tools || []).map((tool) => `<em>${escapeHtml(tool)}</em>`).join("");
    const fragment = skill.prompt_fragment
      ? escapeHtml(skill.prompt_fragment)
      : "使用稳定基线指令；可以在下方提交增量指令候选。";
    return `<article class="panel skill-runtime-card">
      <div class="skill-runtime-head"><b>${String(index + 1).padStart(2, "0")}</b><span><small>RUNTIME SKILL</small><strong>${escapeHtml(skill.name)}</strong></span><i>ACTIVE</i></div>
      <p>${escapeHtml(skill.description)}</p>
      <blockquote>${fragment}</blockquote>
      <div class="skill-tool-list">${tools}</div>
      <footer><span>${number(skill.field_alias_count)} 字段别名</span><span>${number(skill.value_alias_count)} 取值别名</span><span>${number(skill.few_shot_count)} Few-shot</span></footer>
    </article>`;
  }).join("") || '<div class="empty-state"><span>没有发现 Text2SQL Skill</span></div>';
  $("#skill-candidates").innerHTML = candidates.length
    ? [...candidates].reverse().map((item) => `<div class="candidate-item"><span><strong>${escapeHtml(item.target_skill || "unknown")}</strong><small>${escapeHtml(item.change_reason || "无变更说明")}</small></span><div><b>${escapeHtml(item.status || "candidate")}</b><code title="${escapeHtml(item.policy_version || "")}">${escapeHtml(short(item.policy_version, 20))}</code></div></div>`).join("")
    : '<div class="empty-state compact"><span><b>暂无候选 Skill</b>稳定版本不会被直接覆盖。</span></div>';
}

async function loadSkills() {
  try {
    renderSkills(await api("/api/text2sql/skills"));
  } catch (error) {
    $("#text2sql-skill-list").innerHTML = `<div class="empty-state"><span>Skills 加载失败：${escapeHtml(error.message)}</span></div>`;
  }
}

function renderExperiences(data) {
  const items = data.experiences || [];
  $("#experience-list").innerHTML = items.length
    ? items.map((item) => {
      const reasons = (item.eligibility_reasons || []).join("；");
      const actions = item.state === "candidate"
        ? `<div class="experience-actions"><button class="copy-button experience-review" data-experience-id="${escapeHtml(item.experience_id)}" data-decision="reject" type="button">拒绝</button><button class="button experience-review" data-experience-id="${escapeHtml(item.experience_id)}" data-decision="approve" type="button">审核通过</button></div>`
        : `<b>${escapeHtml(item.state || "unknown")}</b>`;
      return `<article class="experience-item">
        <div class="experience-copy"><span><strong>${escapeHtml(item.question || "未命名问题")}</strong><small>${escapeHtml(item.source_kind || "query_run")} · ${escapeHtml(item.experience_id || "")}</small></span><em class="experience-state state-${escapeHtml(item.state || "unknown")}">${escapeHtml(item.state || "unknown")}</em></div>
        <pre>${escapeHtml(item.sql || "--")}</pre>
        ${reasons ? `<p>${escapeHtml(reasons)}</p>` : ""}
        ${actions}
      </article>`;
    }).join("")
    : '<div class="empty-state compact"><span><b>暂无 Question-SQL 候选经验</b>正确反馈会先进入这里，审核后才进入稳定知识。</span></div>';
  $$(".experience-review").forEach((button) => button.addEventListener("click", async () => {
    const decision = button.dataset.decision;
    button.disabled = true;
    try {
      const result = await api(`/v1/text2sql/experiences/${encodeURIComponent(button.dataset.experienceId)}/review`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ decision }),
      });
      toast(decision === "approve" ? "经验已审核，等待重建 Vanna 稳定索引" : "候选经验已拒绝");
      await Promise.all([loadExperiences(), loadStatus()]);
      if (result.vanna_rebuild_required) show("data");
    } catch (error) {
      toast(error.message);
      button.disabled = false;
    }
  }));
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

function renderTraceDetail(trace) {
  selectedTraceId = trace?.task_id || "";
  $$(".trace-item").forEach((item) => item.classList.toggle("active", item.dataset.traceId === selectedTraceId));
  if (!trace) {
    $("#trace-detail").innerHTML = '<div class="empty-state"><span><b>选择一条轨迹</b>这里会显示 SQL、门禁结果和四角色执行过程。</span></div>';
    return;
  }
  const accepted = Boolean(trace.gates?.accepted) && trace.status === "success";
  const queryType = trace.query_type || "DATA_QUERY";
  const agents = (trace.agents || []).map((agent, index) => `<article class="text2sql-agent-item"><b>${String(index + 1).padStart(2, "0")}</b><span><strong>${escapeHtml(agent.role || "agent")}</strong><small>${escapeHtml(agent.summary || agent.stage || "已完成")}</small></span><em>${escapeHtml(agent.status || "completed")}</em></article>`).join("");
  const pins = Object.entries(trace.version_pins || {}).map(([key, value]) => `<div><b>${escapeHtml(key.replaceAll("_", " "))}</b><code>${escapeHtml(short(value, 24))}</code></div>`).join("");
  const schemaPlan = trace.schema_plan || {};
  const querySpec = trace.query_spec || {};
  const draftPack = trace.draft_link_pack || {};
  const retrieval = trace.retrieval || [];
  const vannaHits = retrieval.filter((item) => item.backend === "vanna-chromadb");
  const routeDetail = `<div class="trace-route"><span><b>${escapeHtml(queryType)}</b>路由类型</span><span><b>${escapeHtml(trace.parent_task_id ? short(trace.parent_task_id, 24) : "无")}</b>父 QueryRun</span><span><b>${number(retrieval.length)}</b>检索调用</span><span><b>${number(vannaHits.length)}</b>Vanna 命中批次</span></div>`;
  const planning = queryType === "RESULT_QA" ? "" : `<div class="trace-planning"><div><span>SCHEMA PLAN</span><code>${escapeHtml(JSON.stringify({ tables: schemaPlan.tables || [], columns: schemaPlan.columns || [], joins: schemaPlan.joins || [] }))}</code></div><div><span>QUERY SPEC</span><code>${escapeHtml(JSON.stringify(querySpec))}</code></div></div>`;
  const draftPlanning = queryType === "RESULT_QA" || !draftPack.contract ? "" : `<div class="trace-planning"><div><span>DRAFT SQL · UNTRUSTED</span><code>${escapeHtml(draftPack.draft_sql || "-- 已回退到问题直连")}</code></div><div><span>DRAFT LINK PACK</span><code>${escapeHtml(JSON.stringify({ tables: draftPack.tables || [], columns: draftPack.columns || [], joins: draftPack.joins || [], coverage: draftPack.coverage || {} }))}</code></div></div>`;
  $("#trace-detail").innerHTML = `<div class="panel-head"><div><p class="eyebrow">TRACE DETAIL</p><h3>${escapeHtml(trace.question || "未命名查询")}</h3></div><span class="status ${accepted ? "status-online" : "status-neutral"}"><i></i>${accepted ? "门禁通过" : "已拦截"}</span></div>
    ${trace.standalone_question && trace.standalone_question !== trace.question ? `<p class="trace-standalone"><b>改写后的独立问题</b>${escapeHtml(trace.standalone_question)}</p>` : ""}
    ${routeDetail}
    <div class="trace-metrics"><span><b>${number(trace.execution?.llm_calls)}</b>LLM calls</span><span><b>${number(trace.execution?.tool_calls)}</b>Tool calls</span><span><b>${number(trace.execution?.total_tokens)}</b>Tokens</span><span><b>${number(trace.execution?.duration_ms)}</b>ms</span></div>
    <p class="trace-label">FINAL SQL</p><pre class="text2sql-sql">${escapeHtml(trace.final_sql || "-- 未生成 SQL")}</pre>
    ${draftPlanning}
    ${planning}
    <p class="trace-label">AGENT TRACE</p><div class="text2sql-agent-trace">${agents || '<div class="empty-state compact"><span>没有 Agent 轨迹</span></div>'}</div>
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

async function loadWorkspace() {
  const refresh = $("#refresh");
  refresh.disabled = true;
  refresh.textContent = "刷新中…";
  try {
    await Promise.all([loadStatus(), loadSkills(), loadTraces(), loadExperiences()]);
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
  $("#text2sql-row-count").textContent = `${number(answer.row_count)} 行${answer.truncated ? " · 已截断" : ""}`;
  $("#text2sql-agent-trace").innerHTML = (result.agents || []).map((agent, index) => {
    const detail = agent.detail && Object.keys(agent.detail).length ? `<code>${escapeHtml(JSON.stringify(agent.detail))}</code>` : "";
    return `<article class="text2sql-agent-item"><b>${String(index + 1).padStart(2, "0")}</b><span><strong>${escapeHtml(agent.role || "agent")}</strong><small>${escapeHtml(agent.summary || agent.stage || "已完成")}</small>${detail}</span><em>${escapeHtml(agent.status || "completed")}</em></article>`;
  }).join("") || '<div class="empty-state"><span>暂无 Agent 轨迹</span></div>';
  const usage = result.execution || {};
  $("#text2sql-usage").textContent = `${number(usage.llm_calls)} LLM · ${number(usage.total_tokens)} tokens · ${number(usage.duration_ms)} ms`;
  renderPins(result.version_pins);
  resetFeedback(activeQueryType !== "RESULT_QA" && result.status === "success" && Boolean(activeSql));
  addSession(result.question || $("#text2sql-question").value.trim(), result, summary);
  loadTraces();
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
  const payload = {
    decision,
    session_id: text2sqlSessionId,
    note: $("#feedback-note").value.trim(),
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
  await Promise.all([loadExperiences(), loadStatus(), loadTraces()]);
  toast(result.experience_id ? "反馈已生成候选 Question-SQL 经验" : "错误反馈已进入归因队列");
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
      taskId: `text2sql-web-${globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`}`,
    };
    writePendingQuery(pendingText2SQLQuery);
  }
  const button = $(".text2sql-submit");
  let failed = false;
  setBusy(button, true);
  $("#text2sql-form-note").textContent = "Leader 正在判断独立查询、追问修改或上次结果问答；需要查库时才调度 Grounding 与 Strategy…";
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
    const identityRejected = String(error.message || "").includes("task_id was reused");
    if (identityRejected) {
      pendingText2SQLQuery = null;
      writePendingQuery(null);
    }
    $("#text2sql-form-note").textContent = identityRejected
      ? `${error.message}；旧恢复标识已清除，再次发送会创建新任务。`
      : `${error.message}；再次发送同一问题将从 checkpoint 续跑。`;
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
    output.innerHTML = `<b>候选 Skill 已保存</b><span>${escapeHtml(result.skill_name)} · ${escapeHtml(result.candidate_policy_version)}</span><small>下一步：Validation 与 Sealed Holdout 离线评测。稳定版本尚未改变。</small>`;
    form.reset();
    $("#skill-role").value = "sql-strategy";
    await Promise.all([loadSkills(), loadStatus()]);
    toast("候选 Skill 已进入隔离队列");
  } catch (error) {
    toast(error.message);
  } finally {
    setBusy(button, false);
  }
});
$("#refresh-skills").addEventListener("click", loadSkills);
$("#refresh-experiences").addEventListener("click", loadExperiences);
$("#refresh").addEventListener("click", loadWorkspace);
window.addEventListener("hashchange", () => show(location.hash.slice(1), false));

show(location.hash.slice(1), false);
loadWorkspace();
