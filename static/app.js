/* 结算视图影响分析前端逻辑。状态中保留「分析时绑定的目录版本」，确保结果
 * 永远对应某一版已提交目录；导入新版本后旧结果会显式标记为过期。 */
(function () {
  "use strict";

  const state = {
    catalog: null,        // { version, sha256, summary, created_at }
    selectedTable: null,
    selectedColumn: null,
    result: null,          // 最近一次分析结果
    importFile: null,
    importFileText: null,
    collapsed: new Set(),
    filter: "",
    requestSeq: 0,
  };

  const $ = (id) => document.getElementById(id);
  const el = {
    dropzone: $("dropzone"),
    fileInput: $("file-input"),
    fileMeta: $("file-meta"),
    importBtn: $("import-btn"),
    sampleBtn: $("load-sample-btn"),
    importAlert: $("import-alert"),
    activeVersionText: $("active-version-text"),
    activeVersionBadge: $("active-version"),
    versionList: $("version-list"),
    fieldPicker: $("field-picker"),
    analysisStatus: $("analysis-status"),
    analysisResult: $("analysis-result"),
    catalogOverview: $("catalog-overview"),
    footerVersion: $("footer-version"),
  };

  async function api(path, options) {
    const res = await fetch(path, options);
    let body = null;
    const text = await res.text();
    if (text) {
      try { body = JSON.parse(text); } catch (_e) { body = { detail: text }; }
    }
    if (!res.ok) {
      const detail = body && body.detail;
      const message =
        typeof detail === "string"
          ? detail
          : detail && detail.message
            ? detail.message
            : `请求失败（HTTP ${res.status}）`;
      const err = new Error(message);
      err.status = res.status;
      err.payload = body;
      throw err;
    }
    return body;
  }

  function escapeHtml(value) {
    return String(value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function highlight(text) {
    if (!state.filter) return escapeHtml(text);
    const needle = state.filter.toLowerCase();
    const source = String(text);
    const lower = source.toLowerCase();
    let out = "";
    let cursor = 0;
    let idx = lower.indexOf(needle);
    while (idx !== -1) {
      out += escapeHtml(source.slice(cursor, idx));
      out += "<mark>" + escapeHtml(source.slice(idx, idx + needle.length)) + "</mark>";
      cursor = idx + needle.length;
      idx = lower.indexOf(needle, cursor);
    }
    out += escapeHtml(source.slice(cursor));
    return out;
  }

  function clauseLabel(clause) {
    const map = {
      SELECT: "投影",
      JOIN: "JOIN",
      WHERE: "WHERE",
      "GROUP BY": "GROUP BY",
      HAVING: "HAVING",
      "ORDER BY": "ORDER BY",
      UPSTREAM: "上游整体失效",
    };
    return map[clause] || clause || "-";
  }

  function renderPath(path) {
    if (!path || !path.length) return '<span class="muted">—</span>';
    const parts = path.map((node, index) => {
      const cls =
        node.kind === "field"
          ? "node-field"
          : node.full
            ? "node-full"
            : "node-view";
      const label = escapeHtml(node.label);
      if (index === 0) return `<span class="${cls}">${label}</span>`;
      const arrow = `<span class="arrow">──${escapeHtml(clauseLabel(node.clause))}→</span>`;
      return `${arrow}<span class="${cls}">${label}</span>`;
    });
    return `<div class="path">${parts.join(" ")}</div>`;
  }

  /* ---------------- 版本与目录 ---------------- */

  async function refreshCatalog() {
    const [catalogRes, versionsRes] = await Promise.all([
      api("/api/catalog"),
      api("/api/versions"),
    ]);
    state.catalog = catalogRes.exists ? catalogRes : null;
    renderActiveVersion(catalogRes);
    renderVersions(versionsRes.versions || []);
    renderFieldPicker();
    renderCatalogOverview();
    renderAnalysis();
  }

  function renderActiveVersion(catalog) {
    if (catalog && catalog.exists) {
      el.activeVersionBadge.classList.add("live");
      el.activeVersionText.textContent = `当前目录 v${catalog.version}`;
      el.footerVersion.textContent =
        `v${catalog.version} · sha256 ${String(catalog.sha256).slice(0, 12)}…`;
    } else {
      el.activeVersionBadge.classList.remove("live");
      el.activeVersionText.textContent = "尚未导入目录";
      el.footerVersion.textContent = "";
    }
  }

  function renderVersions(versions) {
    if (!versions.length) {
      el.versionList.innerHTML = '<li class="empty">暂无版本</li>';
      return;
    }
    el.versionList.innerHTML = versions
      .map((v) => {
        const cls = v.active ? ' class="active"' : "";
        const tag = v.active
          ? '<span class="v-tag">生效中</span>'
          : `<span class="v-tag" style="background:transparent;color:var(--muted);border:1px solid var(--border)">v${v.version}</span>`;
        return `<li${cls}>
          <span><span class="v-num">v${v.version}</span> · ${v.view_count} 个视图</span>
          ${tag}
        </li>`;
      })
      .join("");
  }

  function baseTables(summary) {
    if (!summary || !summary.base_tables) return [];
    return Object.entries(summary.base_tables).map(([name, cols]) => ({
      name,
      columns: cols,
    }));
  }

  function renderFieldPicker() {
    const tables = baseTables(state.catalog && state.catalog.summary);
    if (!tables.length) {
      el.fieldPicker.innerHTML =
        '<p class="empty">导入目录后，此处列出基础表与字段。</p>';
      return;
    }
    const search = `
      <input class="field-search" id="field-search" type="search"
             placeholder="搜索表名或字段名…" value="${escapeHtml(state.filter)}" />
    `;
    const groups = tables
      .map((table) => {
        const cols = table.columns;
        const visible = state.filter
          ? cols.filter(
              (c) =>
                c.toLowerCase().includes(state.filter.toLowerCase()) ||
                table.name.toLowerCase().includes(state.filter.toLowerCase()),
            )
          : cols;
        if (state.filter && visible.length === 0) return "";
        const collapsed = state.collapsed.has(table.name) && !state.filter;
        const chips = visible
          .map((col) => {
            const selected =
              state.selectedTable === table.name && state.selectedColumn === col;
            return `<button type="button" class="field-chip${selected ? " selected" : ""}"
              data-table="${escapeHtml(table.name)}" data-column="${escapeHtml(col)}"
              title="分析停用 ${escapeHtml(table.name)}.${escapeHtml(col)} 的影响">${highlight(col)}</button>`;
          })
          .join("");
        return `
          <div class="table-group${collapsed ? " collapsed" : ""}" data-table="${escapeHtml(table.name)}">
            <div class="table-head" data-toggle="${escapeHtml(table.name)}">
              <span><span class="chevron">▾</span><span class="t-name">${highlight(table.name)}</span></span>
              <span class="t-meta">${visible.length}/${cols.length} 字段</span>
            </div>
            <div class="field-list">${chips}</div>
          </div>`;
      })
      .join("");
    el.fieldPicker.innerHTML =
      search + (groups || '<p class="empty">没有匹配的表或字段。</p>');

    const searchInput = $("field-search");
    if (searchInput) {
      searchInput.addEventListener("input", (event) => {
        state.filter = event.target.value.trim();
        const pos = event.target.selectionStart;
        renderFieldPicker();
        const next = $("field-search");
        if (next) {
          next.focus();
          next.setSelectionRange(pos, pos);
        }
      });
    }
    el.fieldPicker.querySelectorAll("[data-toggle]").forEach((node) => {
      node.addEventListener("click", () => {
        const name = node.getAttribute("data-toggle");
        if (state.collapsed.has(name)) state.collapsed.delete(name);
        else state.collapsed.add(name);
        renderFieldPicker();
      });
    });
    el.fieldPicker.querySelectorAll(".field-chip").forEach((node) => {
      node.addEventListener("click", () => {
        selectField(node.getAttribute("data-table"), node.getAttribute("data-column"));
      });
    });
  }

  function renderCatalogOverview() {
    const summary = state.catalog && state.catalog.summary;
    if (!summary) {
      el.catalogOverview.innerHTML =
        '<p class="empty">尚未导入目录。可点击「载入示例」快速体验。</p>';
      return;
    }
    const tables = baseTables(summary)
      .map(
        (t) => `
        <div class="cat-card">
          <div class="c-name">${escapeHtml(t.name)}</div>
          <div class="c-cols">${t.columns.map(escapeHtml).join(" · ")}</div>
        </div>`,
      )
      .join("");
    const views = (summary.views || [])
      .map(
        (v) => `
        <div class="cat-card view">
          <div class="c-name">${escapeHtml(v.name)}</div>
          <div class="c-cols">${v.outputs.map(escapeHtml).join(" · ")}</div>
          ${
            v.depends_on && v.depends_on.length
              ? `<div class="c-deps">依赖：${v.depends_on
                  .map((d) => `<code>${escapeHtml(d)}</code>`)
                  .join("、")}</div>`
              : ""
          }
        </div>`,
      )
      .join("");
    el.catalogOverview.innerHTML = tables + views;
  }

  /* ---------------- 导入 ---------------- */

  function setImportFile(name, text) {
    state.importFile = name;
    state.importFileText = text;
    el.importBtn.disabled = false;
    el.dropzone.classList.add("has-file");
    el.fileMeta.textContent = `${name} · ${(new Blob([text]).size / 1024).toFixed(1)} KB，已就绪`;
  }

  function showImportAlert(kind, title, lines) {
    el.importAlert.hidden = false;
    el.importAlert.className = `alert ${kind}`;
    const list = lines && lines.length
      ? `<ul>${lines.map((line) => `<li>${escapeHtml(line)}</li>`).join("")}</ul>`
      : "";
    el.importAlert.innerHTML = `<div class="alert-title">${escapeHtml(title)}</div>${list}`;
  }

  async function readFile(file) {
    if (!file) return;
    if (!/\.json$/i.test(file.name) && file.type !== "application/json") {
      showImportAlert("error", "请选择 .json 文件", []);
      return;
    }
    const text = await file.text();
    setImportFile(file.name, text);
    el.importAlert.hidden = true;
  }

  /* label[for] 已负责打开文件选择框，这里只处理拖拽。 */
  el.fileInput.addEventListener("change", (event) => readFile(event.target.files[0]));
  ["dragenter", "dragover"].forEach((type) => {
    el.dropzone.addEventListener(type, (event) => {
      event.preventDefault();
      el.dropzone.classList.add("dragover");
    });
  });
  ["dragleave", "drop"].forEach((type) => {
    el.dropzone.addEventListener(type, (event) => {
      event.preventDefault();
      el.dropzone.classList.remove("dragover");
    });
  });
  el.dropzone.addEventListener("drop", (event) => {
    const file = event.dataTransfer.files && event.dataTransfer.files[0];
    readFile(file);
  });

  el.sampleBtn.addEventListener("click", async () => {
    try {
      const sample = await api("/api/sample");
      setImportFile("catalog.json（示例）", JSON.stringify(sample, null, 2));
      el.importAlert.hidden = true;
    } catch (err) {
      showImportAlert("error", err.message, []);
    }
  });

  el.importBtn.addEventListener("click", async () => {
    if (!state.importFileText) return;
    el.importBtn.disabled = true;
    const originalLabel = el.importBtn.textContent;
    el.importBtn.innerHTML = '<span class="spinner"></span>校验并导入…';
    const form = new FormData();
    form.append(
      "file",
      new Blob([state.importFileText], { type: "application/json" }),
      state.importFile || "catalog.json",
    );
    try {
      const result = await api("/api/catalog/import", { method: "POST", body: form });
      showImportAlert(
        "success",
        `导入成功：已整体替换为 v${result.version}`,
        [
          `${Object.keys(result.summary.base_tables).length} 张基础表，${result.summary.views.length} 个视图`,
          `sha256 ${result.sha256.slice(0, 16)}…`,
        ],
      );
      state.importFile = null;
      state.importFileText = null;
      el.dropzone.classList.remove("has-file");
      el.fileMeta.textContent = "base_tables + views（视图 SELECT 定义）";
      el.fileInput.value = "";
      await refreshCatalog();
    } catch (err) {
      const errors =
        err.payload && err.payload.detail && Array.isArray(err.payload.detail.errors)
          ? err.payload.detail.errors
          : null;
      showImportAlert(
        "error",
        errors ? "整份目录被拒绝，原版目录保持生效" : err.message,
        errors || [],
      );
    } finally {
      el.importBtn.textContent = originalLabel;
      el.importBtn.disabled = !state.importFileText;
    }
  });

  /* ---------------- 分析 ---------------- */

  async function selectField(table, column) {
    state.selectedTable = table;
    state.selectedColumn = column;
    renderFieldPicker();
    const seq = ++state.requestSeq;
    el.analysisStatus.textContent = "正在分析…";
    const pinnedVersion = state.catalog ? state.catalog.version : null;
    const body = { table, column };
    if (pinnedVersion !== null) body.version = pinnedVersion;
    try {
      const result = await api("/api/impact", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (seq !== state.requestSeq) return; // 丢弃过期响应
      state.result = result;
      renderAnalysis();
      syncActiveVersion();
    } catch (err) {
      if (seq !== state.requestSeq) return;
      state.result = null;
      el.analysisStatus.innerHTML = `<span style="color:var(--danger)">${escapeHtml(err.message)}</span>`;
      el.analysisResult.innerHTML = "";
    }
  }

  /** 分析期间可能发生导入；轻量同步生效版本，并把旧结果标记为过期。 */
  async function syncActiveVersion() {
    try {
      const catalog = await api("/api/catalog");
      const previous = state.catalog ? state.catalog.version : null;
      state.catalog = catalog.exists ? catalog : null;
      const current = state.catalog ? state.catalog.version : null;
      if (previous === current) return;
      renderActiveVersion(catalog);
      const versionsRes = await api("/api/versions");
      renderVersions(versionsRes.versions || []);
      renderFieldPicker();
      renderCatalogOverview();
      renderAnalysis();
    } catch (_err) {
      /* 同步失败不影响已有结果展示 */
    }
  }

  function renderAnalysis() {
    if (!state.result) {
      el.analysisStatus.innerHTML = state.catalog
        ? "选择左侧基础表字段，查看受影响视图。"
        : "";
      el.analysisResult.innerHTML = "";
      return;
    }
    const result = state.result;
    const activeVersion = state.catalog ? state.catalog.version : null;
    const stale =
      result.catalog.is_active === false ||
      (activeVersion !== null && activeVersion !== result.catalog.version);
    el.analysisStatus.innerHTML = `
      分析结果绑定目录 <span class="pin">v${result.catalog.version}</span>
      · sha256 ${escapeHtml(String(result.catalog.sha256).slice(0, 12))}…
      ${stale ? `<span class="stale">当前生效目录已是 v${activeVersion}，以下结果对应旧版本；重新选择字段可刷新。</span>` : ""}
    `;

    const summary = result.summary;
    const affectedViews = result.affected_views || [];
    const fullCount = (summary.full_impact_views || []).length;
    let html = `
      <div class="result-head">
        <div class="result-field">${escapeHtml(result.field.table)}.${escapeHtml(result.field.column)}</div>
        <div class="result-version">目录版本 <b>v${result.catalog.version}</b>${stale ? "（非当前生效）" : "（当前生效）"}</div>
      </div>
      <div class="stat-row">
        <div class="stat danger"><div class="n">${summary.affected_view_count}</div><div class="l">受影响视图 / 共 ${summary.total_view_count}</div></div>
        <div class="stat warn"><div class="n">${summary.affected_output_count}</div><div class="l">受影响输出列</div></div>
        <div class="stat"><div class="n">${fullCount}</div><div class="l">整视图失效</div></div>
        <div class="stat ok"><div class="n">${result.unaffected_views.length}</div><div class="l">不受影响视图</div></div>
      </div>`;

    if (!affectedViews.length) {
      html += `<div class="empty-state ok">没有视图引用 ${escapeHtml(
        result.field.table,
      )}.${escapeHtml(result.field.column)}，停用该字段不会影响任何结算视图。</div>`;
      el.analysisResult.innerHTML = html;
      return;
    }

    html += affectedViews
      .map((view) => {
        const fullTag = view.full_impact
          ? '<span class="tag full">整视图失效</span>'
          : "";
        const chainTag = view.depends_on && view.depends_on.length
          ? `<span class="tag chain">依赖 ${view.depends_on.map(escapeHtml).join("、")}</span>`
          : "";
        const unaffected =
          view.unaffected_outputs && view.unaffected_outputs.length
            ? `<span class="tag">未受影响：${view.unaffected_outputs.map(escapeHtml).join("、")}</span>`
            : "";
        const rows = view.affected_outputs
          .map((output) => {
            const reasons = (output.reasons || [])
              .map((reason) => {
                const via =
                  reason.via_view
                    ? ` via <code>${escapeHtml(reason.via_view)}.${escapeHtml(reason.via_output || "")}</code>`
                    : "";
                return `<div class="reason-line">${escapeHtml(
                  clauseLabel(reason.clause),
                )}${via} · <code>${escapeHtml(reason.expression)}</code></div>`;
              })
              .join("");
            return `<tr>
              <td class="col-name">${escapeHtml(output.name)}</td>
              <td>${renderPath(output.path)}${reasons}</td>
            </tr>`;
          })
          .join("");
        return `
          <div class="view-card${view.full_impact ? " full" : ""}">
            <div class="view-card-head">
              <span class="view-name">${escapeHtml(view.view)}</span>
              <span class="view-tags">
                ${fullTag}${chainTag}${unaffected}
                <button class="sql-toggle" data-sql="${escapeHtml(view.view)}">查看 SQL</button>
              </span>
            </div>
            <table class="output-table">
              <thead><tr><th style="width:30%">受影响输出列</th><th>引用路径</th></tr></thead>
              <tbody>${rows}</tbody>
            </table>
            <pre class="view-sql" hidden>${escapeHtml(view.sql)}</pre>
          </div>`;
      })
      .join("");

    if (result.unaffected_views.length) {
      html += `
        <div class="unaffected">
          <div class="section-title">不受影响的视图（${result.unaffected_views.length}）</div>
          <div class="unaffected-list">
            ${result.unaffected_views
              .map((name) => `<span class="pill">${escapeHtml(name)}</span>`)
              .join("")}
          </div>
        </div>`;
    }

    el.analysisResult.innerHTML = html;
    el.analysisResult.querySelectorAll("[data-sql]").forEach((node) => {
      node.addEventListener("click", () => {
        const card = node.closest(".view-card");
        const pre = card.querySelector(".view-sql");
        pre.hidden = !pre.hidden;
        node.textContent = pre.hidden ? "查看 SQL" : "收起 SQL";
      });
    });
  }

  /* ---------------- 启动 ---------------- */
  refreshCatalog().catch((err) => {
    el.catalogOverview.innerHTML = `<p class="empty">加载失败：${escapeHtml(err.message)}</p>`;
  });

  // 页面重新获得焦点时轻量核对生效版本，及时标记过期结果。
  window.addEventListener("focus", () => {
    if (state.result) syncActiveVersion();
  });
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && state.result) syncActiveVersion();
  });
})();
