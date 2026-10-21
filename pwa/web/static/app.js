/* Perry WeChat AI — 审核队列前端 */

const $ = (sel) => document.querySelector(sel);
const state = { status: "pending", drafts: [], selected: null };

// 状态标签：区分"草稿"（已生成的内容）与"待审核"（队列状态）。
// pending 同时具备两种性质，所以两个词都显示，避免把"已生成"当成"已发出"。
const STATUS_LABEL = {
  pending: "草稿 · 待审核",
  approved: "已批准 · 待发送",
  sent: "已发送",
  cancelled: "已取消",
  failed: "失败",
  unknown: "状态未知",
};

const STATUS_TONE = {
  pending: "st-pending",
  approved: "st-approved",
  sent: "st-sent",
  cancelled: "st-cancelled",
  failed: "st-failed",
  unknown: "st-unknown",
};

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = { error: text }; }
  if (!res.ok) throw new Error((data && (data.error || data.detail)) || `HTTP ${res.status}`);
  return data;
}

// ---------- 状态栏 ----------
async function refreshStatus() {
  try {
    const s = await api("/api/status");
    const bits = [];
    bits.push(s.running ? "运行中" : (s.emergency_stop ? "已紧急停止" : "已暂停"));
    if (s.allowed_chats.length) bits.push(`授权 ${s.allowed_chats.length} 个会话`);
    else bits.push("未授权任何会话");
    if (!s.primary_configured) bits.push("主模型未配置");
    if (!s.send_enabled) bits.push("发送已关闭");
    else if (s.send_dry_run) bits.push("dry-run：只填输入框，不真正发出");
    else bits.push("发送已启用（仍需逐条批准）");
    bits.push(`限流 ${s.send_rate_max} 条 / ${s.send_rate_window} 秒`);
    if (s.auto_reply_running) {
      bits.push(`自动回复中：${s.auto_reply_chat || "?"}（已回 ${s.auto_reply_replied} 条）`);
    }

    let line = bits.join(" · ");
    if (s.last_error) line += ` · 最近错误：${s.last_error}`;
    if (s.auto_reply_error) line += ` · 自动回复错误：${s.auto_reply_error}`;
    $("#statusline").textContent = line;

    $("#btn-pause").textContent = s.paused ? "恢复观察" : "暂停";
    $("#btn-pause").disabled = s.emergency_stop;

    renderStats(s);
  } catch (err) {
    $("#statusline").textContent = `状态获取失败：${err.message}`;
  }
}

function renderStats(s) {
  const c = s.draft_counts || {};
  const items = [
    ["待审核草稿", c.pending || 0, ""],
    ["已批准待发送", c.approved || 0, ""],
    ["已发送", c.sent || 0, ""],
    ["失败 / 未知", (c.failed || 0) + (c.unknown || 0), ""],
    ["累计成本", (s.total_cost || 0).toFixed(4), " USD"],
    ["主模型", s.primary_configured ? "已配置" : "未配置", ""],
    ["备用模型", s.fallback_configured ? "已配置" : "未配置", ""],
    ["本地模型", s.local_configured ? "已配置" : "未配置", ""],
  ];
  $("#stats").innerHTML = items
    .map(
      ([label, value, unit]) =>
        `<dl class="stat"><dt>${label}</dt><dd>${value}<small>${unit}</small></dd></dl>`
    )
    .join("");
}

// ---------- 草稿列表 ----------
async function refreshDrafts() {
  const q = state.status ? `?status=${state.status}` : "";
  try {
    state.drafts = await api(`/api/drafts${q}`);
  } catch (err) {
    $("#list").innerHTML = `<div class="empty">加载失败：${err.message}</div>`;
    return;
  }
  renderList();
}

function renderList() {
  const list = $("#list");
  if (!state.drafts.length) {
    list.innerHTML = `<div class="empty">暂无草稿。观察到的消息会在这里生成待审核回复。</div>`;
    return;
  }
  list.innerHTML = state.drafts
    .map((d) => {
      const who = d.is_group ? `群 · ${d.chat_name}` : d.chat_name;
      const when = new Date((d.created_at || 0) * 1000).toLocaleString("zh-CN", {
        month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
      });
      const trig = d.trigger_sender ? `${d.trigger_sender}: ${d.trigger_text}` : d.trigger_text;
      const meta = [
        `<span class="tag">${d.model || "—"}</span>`,
        `<span class="tag">${d.difficulty || "—"}</span>`,
        d.cost ? `<span class="tag">$${Number(d.cost).toFixed(4)}</span>` : "",
        d.edited ? `<span class="tag">已编辑</span>` : "",
      ].filter(Boolean).join("");
      return `
      <article class="card ${state.selected === d.id ? "is-selected" : ""}" data-id="${d.id}">
        <div class="card-title">${escapeHtml(who)}</div>
        <div class="card-time">${when}</div>
        <div class="card-trigger">${escapeHtml(trig)}</div>
        <div class="card-reply">${escapeHtml(d.reply_text || "")}</div>
        <div class="card-meta">
          <span class="tag tag-status ${STATUS_TONE[d.status] || ""}">${STATUS_LABEL[d.status] || d.status}</span>
          ${meta}
          ${d.error ? `<span class="tag st-failed">${escapeHtml(d.error)}</span>` : ""}
        </div>
      </article>`;
    })
    .join("");
}

// ---------- 详情 ----------
async function openDetail(id) {
  state.selected = id;
  renderList();
  let d;
  try {
    d = await api(`/api/drafts/${id}`);
  } catch (err) {
    alert(`打开失败：${err.message}`);
    return;
  }
  $("#d-title").textContent = d.is_group ? `群聊 · ${d.chat_name}` : d.chat_name;

  const ctx = (d.context || []).map((m) =>
    `<div class="bubble context">${escapeHtml(m.sender ? m.sender + ": " + m.text : m.text)}</div>`
  ).join("");

  $("#d-body").innerHTML = `
    <div>
      <p class="field-label">触发消息</p>
      <div class="bubble">${escapeHtml(
        d.trigger_sender ? d.trigger_sender + ": " + d.trigger_text : d.trigger_text
      )}</div>
    </div>
    ${ctx ? `<div><p class="field-label">上下文</p>${ctx}</div>` : ""}
    <div>
      <p class="field-label">建议回复（可编辑）</p>
      <textarea id="d-text">${escapeHtml(d.reply_text || "")}</textarea>
    </div>
    <div>
      <p class="field-label">元信息</p>
      <dl class="kv">
        <dt>模型</dt><dd>${escapeHtml(d.model || "—")}</dd>
        <dt>端点</dt><dd>${escapeHtml(d.endpoint || "—")}</dd>
        <dt>难度</dt><dd>${escapeHtml(d.difficulty || "—")}</dd>
        <dt>Tokens</dt><dd>${d.prompt_tokens} in / ${d.completion_tokens} out</dd>
        <dt>成本</dt><dd>$${Number(d.cost || 0).toFixed(6)}</dd>
        <dt>创建时间</dt><dd>${new Date((d.created_at || 0) * 1000).toLocaleString("zh-CN")}</dd>
        <dt>状态</dt><dd><span class="tag tag-status ${STATUS_TONE[d.status] || ""}">${STATUS_LABEL[d.status] || d.status}</span></dd>
        ${d.error ? `<dt>失败原因</dt><dd style="color:var(--danger)">${escapeHtml(d.error)}</dd>` : ""}
      </dl>
    </div>
    <div>
      <p class="field-label">处理记录</p>
      <div class="events">${(d.events || []).map((e) => `
        <div class="event">
          <span class="event-kind">${escapeHtml(e.kind)}</span>
          <span class="event-time">${new Date(e.created_at * 1000).toLocaleTimeString("zh-CN")}</span>
          <span class="event-detail">${escapeHtml(e.detail || "")}</span>
        </div>`).join("") || '<div class="event"><span class="event-detail">暂无记录</span></div>'}
      </div>
    </div>`;

  const editable = ["pending", "approved", "failed"].includes(d.status);
  $("#d-foot").innerHTML = `
    <button class="btn btn-ghost" id="d-copy">复制回复</button>
    ${editable ? '<button class="btn" id="d-save">保存修改</button>' : ""}
    ${["pending", "failed"].includes(d.status) ? '<button class="btn btn-primary" id="d-approve">批准</button>' : ""}
    ${d.status === "approved" ? '<button class="btn btn-primary" id="d-send">发送</button>' : ""}
    ${["pending", "approved", "failed"].includes(d.status) ? '<button class="btn btn-danger" id="d-cancel">取消</button>' : ""}`;

  const copy = $("#d-copy");
  if (copy) copy.onclick = () => copyReply($("#d-text").value);

  const save = $("#d-save");
  if (save) save.onclick = () => saveEdit(d.id, $("#d-text").value);

  const approve = $("#d-approve");
  if (approve) approve.onclick = () => act(d.id, "approve");

  const send = $("#d-send");
  if (send) send.onclick = () => act(d.id, "send");

  const cancel = $("#d-cancel");
  if (cancel) cancel.onclick = () => act(d.id, "cancel");

  $("#detail").showModal();
}

async function saveEdit(id, text) {
  if (!text.trim()) { alert("回复内容不能为空"); return; }
  try {
    await api(`/api/drafts/${id}/edit`, { method: "POST", body: JSON.stringify({ reply_text: text }) });
    await openDetail(id);
    await refreshDrafts();
  } catch (err) { alert(`保存失败：${err.message}`); }
}

async function act(id, action) {
  try {
    if (action === "send") {
      const text = $("#d-text") ? $("#d-text").value : "";
      const res = await api(`/api/drafts/${id}/send`, { method: "POST", body: JSON.stringify({ draft_id: id, text }) });
      if (!res.ok) { alert(`发送被拦截：${res.error}`); return; }
      if (res.typed_only) {
        alert("已填入微信输入框（dry-run 模式，没有真正发出）。\n请到微信里确认内容后手动发送。");
      } else if (res.status === "unknown") {
        alert("发送结果不确定，请到微信里人工确认，系统不会自动重试。");
      }
    } else {
      await api(`/api/drafts/${id}/${action}`, { method: "POST", body: JSON.stringify({ draft_id: id }) });
    }
    await openDetail(id);
    await refreshDrafts();
    await refreshStatus();
  } catch (err) {
    if (action === "send") {
      alert(`发送被安全门拦截：${err.message}`);
      await openDetail(id);
      await refreshDrafts();
      return;
    }
    alert(`操作失败：${err.message}`);
  }
}

async function copyReply(text) {
  try {
    await navigator.clipboard.writeText(text || "");
    const btn = $("#d-copy");
    if (btn) { const old = btn.textContent; btn.textContent = "已复制"; setTimeout(() => (btn.textContent = old), 1400); }
  } catch (err) {
    alert(`复制失败：${err.message}`);
  }
}

// ---------- 事件 ----------
async function refreshEvents() {
  try {
    const rows = await api("/api/events?limit=60");
    $("#events").innerHTML = rows.map((e) => `
      <div class="event">
        <span class="event-kind">${escapeHtml(e.kind)}</span>
        <span class="event-time">${new Date(e.created_at * 1000).toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" })}</span>
        <span class="event-detail">${escapeHtml(e.detail || "")}</span>
      </div>`).join("") || '<div class="event"><span class="event-detail">暂无记录</span></div>';
  } catch {
    $("#events").innerHTML = '<div class="event"><span class="event-detail">加载失败</span></div>';
  }
}

function escapeHtml(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );
}

// ---------- 绑定 ----------
document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll(".chip").forEach((chip) => {
    chip.onclick = () => {
      document.querySelectorAll(".chip").forEach((c) => c.classList.remove("is-active"));
      chip.classList.add("is-active");
      state.status = chip.dataset.status;
      refreshDrafts();
    };
  });

  $("#list").onclick = (e) => {
    const card = e.target.closest(".card");
    if (card) openDetail(card.dataset.id);
  };
  $("#d-close").onclick = () => $("#detail").close();
  $("#btn-poll").onclick = async () => {
    const btn = $("#btn-poll");
    btn.disabled = true;
    try {
      const r = await api("/api/observe/poll", { method: "POST" });
      await refreshDrafts();
      await refreshStatus();
      await refreshEvents();
      if (!r.ok) alert(r.error);
    } catch (err) { alert(`观察失败：${err.message}`); }
    btn.disabled = false;
  };
  $("#btn-pause").onclick = async () => {
    const paused = $("#btn-pause").textContent === "暂停";
    await api(paused ? "/api/observe/pause" : "/api/observe/resume", { method: "POST" });
    await refreshStatus();
  };
  $("#btn-stop").onclick = async () => {
    if (!confirm("紧急停止会立刻中断观察与自动回复，并拒绝之后的所有发送。\n确认继续？")) return;
    // 观察侧与自动回复侧都要停：自动回复侧的停止会同时 trip 发送闸门。
    const results = await Promise.allSettled([
      api("/api/observe/stop", { method: "POST" }),
      api("/api/auto-reply/stop", { method: "POST" }),
    ]);
    const failed = results.filter((r) => r.status === "rejected");
    if (failed.length === results.length) {
      alert(`紧急停止失败：${failed[0].reason.message}`);
    }
    await refreshStatus();
  };
  $("#btn-auto-stop").onclick = async () => {
    try {
      const r = await api("/api/auto-reply/stop", { method: "POST" });
      alert(`自动回复已停止（累计回复 ${r.replied_count} 条）。发送闸门已 trip，之后的发送都会被拒绝。`);
    } catch (err) {
      alert(`停止失败：${err.message}`);
    }
    await refreshStatus();
  };

  refreshStatus();
  refreshDrafts();
  refreshEvents();
  setInterval(() => { refreshStatus(); refreshEvents(); }, 5000);
});
