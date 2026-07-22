const state = { page: 1, pageSize: 12, total: 0 };
const $ = (selector) => document.querySelector(selector);

function escapeHtml(value = "") {
  return String(value).replace(/[&<>'"]/g, char => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[char]));
}

function localISODate(offsetDays = 0) {
  const date = new Date();
  date.setDate(date.getDate() + offsetDays);
  return `${date.getFullYear()}-${String(date.getMonth()+1).padStart(2,"0")}-${String(date.getDate()).padStart(2,"0")}`;
}

function queryParams(includePage = true) {
  const params = new URLSearchParams();
  const q = $("#query").value.trim();
  const channel = $("#channel").value;
  const relevance = $("#relevance").value;
  const industry = $("#industry").value;
  const period = $("#period").value;
  if (q) params.set("q", q);
  if (channel) params.set("channel", channel);
  if (relevance) params.set("relevance", relevance);
  if (industry) params.set("industry", industry);
  if (period === "today") params.set("date_from", localISODate());
  else if (period !== "all") params.set("date_from", localISODate(-Number(period) + 1));
  if (includePage) { params.set("page", state.page); params.set("page_size", state.pageSize); }
  return params;
}

async function loadMetadata() {
  const response = await fetch("/api/metadata");
  if (!response.ok) throw new Error("统计信息读取失败");
  const data = await response.json();
  $("#todayCount").textContent = data.today.toLocaleString();
  $("#totalCount").textContent = data.total.toLocaleString();
  $("#highCount").textContent = data.high_relevance.toLocaleString();
  $("#channelCount").textContent = data.configured_sources.length.toLocaleString();
  const currentChannel = $("#channel").value;
  $("#channel").innerHTML = `<option value="">全部网址</option>${data.configured_sources.map(item =>
    `<option value="${escapeHtml(item.channel)}">${escapeHtml(item.url)}（${item.count}）</option>`).join("")}`;
  if ([...$("#channel").options].some(option => option.value === currentChannel)) $("#channel").value = currentChannel;
  const currentIndustry = $("#industry").value;
  $("#industry").innerHTML = `<option value="">全部招标单位行业</option>${data.industries.map(item =>
    `<option value="${escapeHtml(item.name)}">${escapeHtml(item.name)}（${item.count}）</option>`).join("")}`;
  if ([...$("#industry").options].some(option => option.value === currentIndustry)) $("#industry").value = currentIndustry;
}

function card(item, index) {
  const content = item.content || item.reason || "暂无采购内容摘要，请前往来源网站查看完整公告。";
  const deadline = item.deadline ? `截止 ${escapeHtml(item.deadline.slice(0, 16))}` : "截止时间待确认";
  const tags = [item.relevance ? `<span class="tag ${item.relevance === "高" ? "high" : ""}">业务匹配度：${escapeHtml(item.relevance)}</span>` : "",
    item.industry ? `<span class="tag">${escapeHtml(item.industry)}</span>` : ""].join("");
  const link = item.source_url ? `<a href="${escapeHtml(item.source_url)}" target="_blank" rel="noopener noreferrer">查看原文 ↗</a>` : "<span>暂无链接</span>";
  return `<article class="opportunity-row" style="animation-delay:${Math.min(index * 35, 250)}ms">
    <div class="row-source"><span class="channel">${escapeHtml(item.channel)}</span><span class="date">${escapeHtml(item.collected_date)} 收录</span></div>
    <div class="row-main"><h3>${escapeHtml(item.title || "未命名项目")}</h3><p class="org">${escapeHtml(item.organization || "招标单位待确认")}${item.project_id ? ` · ${escapeHtml(item.project_id)}` : ""}</p><p class="description">${escapeHtml(content)}</p></div>
    <div class="row-tags">${tags}</div><div class="row-action"><span>${deadline}</span>${link}</div></article>`;
}

function renderPagination() {
  const pages = Math.ceil(state.total / state.pageSize);
  if (pages <= 1) { $("#pagination").innerHTML = ""; return; }
  const numbers = [...new Set([1, state.page - 1, state.page, state.page + 1, pages])].filter(n => n >= 1 && n <= pages).sort((a,b) => a-b);
  let last = 0;
  const buttons = numbers.map(number => {
    const gap = number - last > 1 ? `<span>…</span>` : ""; last = number;
    return `${gap}<button data-page="${number}" class="${number === state.page ? "active" : ""}">${number}</button>`;
  }).join("");
  $("#pagination").innerHTML = `<button data-page="${state.page-1}" ${state.page === 1 ? "disabled" : ""}>←</button>${buttons}<button data-page="${state.page+1}" ${state.page === pages ? "disabled" : ""}>→</button>`;
}

async function loadOpportunities() {
  $("#resultLabel").textContent = "正在读取商机…";
  const params = queryParams();
  $("#exportLink").href = `/api/export.csv?${queryParams(false)}`;
  try {
    const response = await fetch(`/api/opportunities?${params}`);
    if (!response.ok) throw new Error("商机数据读取失败");
    const data = await response.json();
    state.total = data.total;
    $("#cards").innerHTML = data.items.map(card).join("");
    $("#empty").hidden = data.items.length > 0;
    $("#resultLabel").textContent = `共找到 ${data.total.toLocaleString()} 条商机${data.total ? ` · 第 ${data.page} 页` : ""}`;
    renderPagination();
  } catch (error) {
    $("#cards").innerHTML = ""; $("#empty").hidden = false;
    $("#resultLabel").textContent = error.message;
  }
}

$("#filters").addEventListener("submit", event => { event.preventDefault(); state.page = 1; loadOpportunities(); });
$("#channel").addEventListener("change", () => { state.page = 1; loadOpportunities(); });
$("#relevance").addEventListener("change", () => { state.page = 1; loadOpportunities(); });
$("#industry").addEventListener("change", () => { state.page = 1; loadOpportunities(); });
$("#period").addEventListener("change", () => { state.page = 1; loadOpportunities(); });
$("#clearFilters").addEventListener("click", () => { $("#filters").reset(); $("#period").value = "30"; state.page = 1; loadOpportunities(); });
$("#pagination").addEventListener("click", event => { const button = event.target.closest("button[data-page]"); if (!button || button.disabled) return; state.page = Number(button.dataset.page); loadOpportunities(); window.scrollTo({top: $(".workspace").offsetTop - 20, behavior: "smooth"}); });

Promise.all([loadMetadata(), loadOpportunities()]).catch(error => { $("#resultLabel").textContent = error.message; });
setInterval(() => Promise.all([loadMetadata(), loadOpportunities()]), 60_000);
