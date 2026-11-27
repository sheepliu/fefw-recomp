// fefw-recomp GitHub Pages site logic
// Pulls releases, download assets, and release notes live from the GitHub Releases API.

const DEFAULT_REPO = "metrist38/fefw-recomp";
const CACHE_KEY_PREFIX = "fefw_recomp_releases_v1:";
const CACHE_TTL_MS = 5 * 60 * 1000;

function detectRepo() {
  const host = window.location.hostname;
  if (host.endsWith(".github.io")) {
    const owner = host.slice(0, -".github.io".length);
    const seg = window.location.pathname.split("/").filter(Boolean)[0];
    if (owner && seg) {
      return `${owner}/${seg}`;
    }
  }
  return DEFAULT_REPO;
}

const REPO = detectRepo();
const REPO_URL = `https://github.com/${REPO}`;
const API_URL = `https://api.github.com/repos/${REPO}/releases?per_page=15`;

function detectOS() {
  const ua = (navigator.userAgent || "").toLowerCase();
  const platform = (navigator.platform || "").toLowerCase();
  if (ua.includes("linux") && !ua.includes("android")) return "linux";
  if (platform.includes("linux")) return "linux";
  return "windows";
}

function formatBytes(bytes) {
  if (!Number.isFinite(bytes) || bytes <= 0) return "";
  const units = ["B", "KB", "MB", "GB"];
  let value = bytes;
  let i = 0;
  while (value >= 1024 && i < units.length - 1) {
    value /= 1024;
    i++;
  }
  return `${value.toFixed(i === 0 ? 0 : 1)} ${units[i]}`;
}

function formatDate(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleDateString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}

function escapeHtml(str) {
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function safeUrl(url) {
  try {
    const parsed = new URL(url, REPO_URL);
    if (parsed.protocol === "https:" || parsed.protocol === "http:") {
      return parsed.href;
    }
  } catch {
    // ignore invalid URLs
  }
  return null;
}

function renderInlineMarkdown(raw) {
  // Split around inline code spans so we don't format inside `code`
  const parts = raw.split(/(`[^`]+`)/g);
  return parts
    .map((part) => {
      if (part.startsWith("`") && part.endsWith("`") && part.length >= 2) {
        return `<code>${escapeHtml(part.slice(1, -1))}</code>`;
      }
      let s = escapeHtml(part);

      // Markdown links: [text](url)
      s = s.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (_, label, href) => {
        const clean = safeUrl(href.replace(/&amp;/g, "&"));
        if (!clean) return label;
        return `<a href="${escapeHtml(clean)}" target="_blank" rel="noopener noreferrer">${label}</a>`;
      });

      // Bare URLs (not already inside an href attribute)
      s = s.replace(/(^|[\s(])(https?:\/\/[^\s)<]+)/g, (match, prefix, url) => {
        const trailing = url.match(/[.,;:!?]+$/)?.[0] || "";
        const trimmed = trailing ? url.slice(0, -trailing.length) : url;
        const clean = safeUrl(trimmed.replace(/&amp;/g, "&"));
        if (!clean) return match;
        let display = trimmed;
        const compareMatch = trimmed.match(/github\.com\/[^/]+\/[^/]+\/compare\/(.+)$/);
        const pullMatch = trimmed.match(/github\.com\/[^/]+\/[^/]+\/(pull|issues)\/(\d+)$/);
        if (compareMatch) display = compareMatch[1];
        else if (pullMatch) display = `#${pullMatch[2]}`;
        return `${prefix}<a href="${escapeHtml(clean)}" target="_blank" rel="noopener noreferrer">${escapeHtml(display)}</a>${trailing}`;
      });

      // Bold, italic, strikethrough
      s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
      s = s.replace(/__([^_]+)__/g, "<strong>$1</strong>");
      s = s.replace(/~~([^~]+)~~/g, "<del>$1</del>");
      s = s.replace(/(^|[\s(])\*([^*]+)\*(?=[\s.,;:!?)]|$)/g, "$1<em>$2</em>");

      // Line breaks
      s = s.replace(/&lt;br\s*\/?&gt;/gi, "<br>");

      // Issue / PR references like (#123) or #123
      s = s.replace(/(^|[\s(])#(\d+)\b/g, (_, prefix, num) => {
        return `${prefix}<a href="${REPO_URL}/issues/${num}" target="_blank" rel="noopener noreferrer">#${num}</a>`;
      });

      return s;
    })
    .join("");
}

function splitTableRow(line) {
  let trimmed = line.trim();
  if (!trimmed.includes("|")) return null;

  if (trimmed.startsWith("|")) {
    trimmed = trimmed.slice(1);
  }
  if (trimmed.endsWith("|")) {
    let backslashes = 0;
    for (let j = trimmed.length - 2; j >= 0 && trimmed[j] === "\\"; j--) {
      backslashes++;
    }
    if (backslashes % 2 === 0) {
      trimmed = trimmed.slice(0, -1);
    }
  }

  const cells = [];
  let current = "";
  for (let i = 0; i < trimmed.length; i++) {
    const ch = trimmed[i];
    if (ch === "\\" && trimmed[i + 1] === "|") {
      current += "|";
      i++;
    } else if (ch === "|") {
      cells.push(current.trim());
      current = "";
    } else {
      current += ch;
    }
  }
  cells.push(current.trim());
  return cells;
}

function parseTableDelimiter(line) {
  const cells = splitTableRow(line);
  if (!cells || !cells.length) return null;

  const alignments = [];
  for (const cell of cells) {
    if (!/^:?-{1,}:?$/.test(cell)) return null;
    const left = cell.startsWith(":");
    const right = cell.endsWith(":");
    if (left && right) alignments.push("center");
    else if (right) alignments.push("right");
    else if (left) alignments.push("left");
    else alignments.push(null);
  }
  return alignments;
}

function renderMarkdown(md) {
  if (!md || !md.trim()) {
    return "<p>No release notes provided for this release.</p>";
  }

  const lines = md.replace(/\r\n?/g, "\n").split("\n");
  const out = [];
  let inCode = false;
  let codeLines = [];
  let listType = null; // "ul" | "ol"
  let paraLines = [];

  const flushPara = () => {
    if (!paraLines.length) return;
    out.push(`<p>${renderInlineMarkdown(paraLines.join(" "))}</p>`);
    paraLines = [];
  };

  const closeList = () => {
    if (!listType) return;
    out.push(`</${listType}>`);
    listType = null;
  };

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (line.trim().startsWith("```")) {
      flushPara();
      closeList();
      if (!inCode) {
        inCode = true;
        codeLines = [];
      } else {
        out.push(`<pre><code>${escapeHtml(codeLines.join("\n"))}</code></pre>`);
        inCode = false;
      }
      continue;
    }

    if (inCode) {
      codeLines.push(line);
      continue;
    }

    const trimmed = line.trim();
    if (!trimmed) {
      flushPara();
      closeList();
      continue;
    }

    // GFM Table (header row + delimiter row + optional body rows)
    if (i + 1 < lines.length && trimmed.includes("|")) {
      const headerCells = splitTableRow(line);
      const alignments = parseTableDelimiter(lines[i + 1]);
      if (headerCells && alignments && headerCells.length === alignments.length) {
        flushPara();
        closeList();

        const bodyRows = [];
        i += 2;
        while (i < lines.length) {
          const rowLine = lines[i];
          const rowTrimmed = rowLine.trim();
          if (
            !rowTrimmed ||
            !rowTrimmed.includes("|") ||
            rowTrimmed.startsWith("```") ||
            /^([-*_])\1{2,}$/.test(rowTrimmed) ||
            /^#{1,6}\s+/.test(rowTrimmed) ||
            /^>\s?/.test(rowTrimmed) ||
            /^\s*[-*+]\s+/.test(rowLine) ||
            /^\s*\d+\.\s+/.test(rowLine)
          ) {
            break;
          }
          const rowCells = splitTableRow(rowLine);
          if (!rowCells) break;
          bodyRows.push(rowCells);
          i++;
        }
        i--;

        const alignAttr = (idx) =>
          alignments[idx] ? ` style="text-align:${alignments[idx]}"` : "";

        const thead = `<thead><tr>${headerCells
          .map((cell, idx) => `<th${alignAttr(idx)}>${renderInlineMarkdown(cell)}</th>`)
          .join("")}</tr></thead>`;

        const tbody = bodyRows.length
          ? `<tbody>${bodyRows
              .map(
                (row) =>
                  `<tr>${headerCells
                    .map(
                      (_, idx) =>
                        `<td${alignAttr(idx)}>${renderInlineMarkdown(row[idx] ?? "")}</td>`
                    )
                    .join("")}</tr>`
              )
              .join("")}</tbody>`
          : "";

        out.push(`<div class="table-wrap"><table>${thead}${tbody}</table></div>`);
        continue;
      }
    }

    // Horizontal rule
    if (/^([-*_])\1{2,}$/.test(trimmed)) {
      flushPara();
      closeList();
      out.push("<hr>");
      continue;
    }

    // Headings
    const headingMatch = trimmed.match(/^(#{1,4})\s+(.+)$/);
    if (headingMatch) {
      flushPara();
      closeList();
      const level = Math.min(headingMatch[1].length + 1, 4);
      out.push(`<h${level}>${renderInlineMarkdown(headingMatch[2])}</h${level}>`);
      continue;
    }

    // Blockquote
    const quoteMatch = trimmed.match(/^>\s?(.*)$/);
    if (quoteMatch) {
      flushPara();
      closeList();
      out.push(`<blockquote>${renderInlineMarkdown(quoteMatch[1])}</blockquote>`);
      continue;
    }

    // Unordered list
    const ulMatch = line.match(/^\s*[-*+]\s+(.+)$/);
    if (ulMatch) {
      flushPara();
      if (listType !== "ul") {
        closeList();
        listType = "ul";
        out.push("<ul>");
      }
      out.push(`<li>${renderInlineMarkdown(ulMatch[1])}</li>`);
      continue;
    }

    // Ordered list
    const olMatch = line.match(/^\s*\d+\.\s+(.+)$/);
    if (olMatch) {
      flushPara();
      if (listType !== "ol") {
        closeList();
        listType = "ol";
        out.push("<ol>");
      }
      out.push(`<li>${renderInlineMarkdown(olMatch[1])}</li>`);
      continue;
    }

    closeList();
    paraLines.push(trimmed);
  }

  if (inCode) {
    out.push(`<pre><code>${escapeHtml(codeLines.join("\n"))}</code></pre>`);
  }
  flushPara();
  closeList();

  return out.join("\n");
}

function classifyAssets(assets = []) {
  let winAsset = null;
  let linuxAsset = null;
  const others = [];

  for (const asset of assets) {
    const name = (asset.name || "").toLowerCase();
    const isAuxiliary = name.includes("symbols") || name.includes("debug");
    if (!winAsset && !isAuxiliary && (name.includes("win64") || name.includes("windows")) && name.endsWith(".zip")) {
      winAsset = asset;
    } else if (!linuxAsset && !isAuxiliary && name.includes("linux") && (name.endsWith(".tar.gz") || name.endsWith(".appimage") || name.endsWith(".zip"))) {
      linuxAsset = asset;
    } else {
      others.push(asset);
    }
  }

  return { winAsset, linuxAsset, others };
}

async function fetchReleases() {
  const cacheKey = `${CACHE_KEY_PREFIX}${REPO}`;
  try {
    const cached = sessionStorage.getItem(cacheKey);
    if (cached) {
      const parsed = JSON.parse(cached);
      if (Date.now() - parsed.ts < CACHE_TTL_MS && Array.isArray(parsed.data)) {
        return parsed.data;
      }
    }
  } catch {
    // ignore storage errors
  }

  const res = await fetch(API_URL, {
    headers: { Accept: "application/vnd.github+json" },
  });
  if (!res.ok) {
    const err = new Error(`GitHub API returned ${res.status}`);
    err.status = res.status;
    throw err;
  }

  const data = await res.json();
  const published = (Array.isArray(data) ? data : []).filter((r) => !r.draft);

  try {
    sessionStorage.setItem(cacheKey, JSON.stringify({ ts: Date.now(), data: published }));
  } catch {
    // ignore storage quota errors
  }

  return published;
}

function updateRepoLinks() {
  const map = {
    "nav-github": REPO_URL,
    "all-releases-link": `${REPO_URL}/releases`,
    "link-config-docs": `${REPO_URL}/blob/master/runtime/docs/CONFIG.md`,
    "link-building-docs": `${REPO_URL}/blob/master/docs/BUILDING.md`,
    "footer-repo": REPO_URL,
    "footer-issues": `${REPO_URL}/issues`,
    "footer-legal": `${REPO_URL}/blob/master/LEGAL.md`,
    "footer-license": `${REPO_URL}/blob/master/LICENSE`,
  };
  for (const [id, href] of Object.entries(map)) {
    const el = document.getElementById(id);
    if (el) el.href = href;
  }
}

function renderDownloadCard({ title, subtitle, asset, fallbackUrl, recommended, tag }) {
  const hasAsset = Boolean(asset);
  const href = hasAsset ? asset.browser_download_url : fallbackUrl;
  const filename = hasAsset ? asset.name : "View on GitHub Releases";
  const size = hasAsset ? formatBytes(asset.size) : "";
  const badge = recommended ? `<span class="pill pill-accent">Recommended</span>` : "";

  return `
    <article class="card download-card ${recommended ? "is-recommended" : ""}">
      <div>
        <div class="download-card-top">
          <div>
            <h3>${escapeHtml(title)}</h3>
            <p class="download-subtitle">${escapeHtml(subtitle)}</p>
          </div>
          ${badge}
        </div>
      </div>
      <div>
        <div class="download-meta">
          <span>${escapeHtml(filename)}</span>
          ${size ? `<span>${escapeHtml(size)}</span>` : ""}
          ${tag ? `<span>${escapeHtml(tag)}</span>` : ""}
        </div>
      </div>
      <div class="download-card-actions">
        <a href="${escapeHtml(href)}" class="btn ${recommended ? "btn-primary" : "btn-secondary"}" ${hasAsset ? "" : 'target="_blank" rel="noopener noreferrer"'}>
          <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
            <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
            <polyline points="7 10 12 15 17 10"/>
            <line x1="12" y1="15" x2="12" y2="3"/>
          </svg>
          ${hasAsset ? `Download ${escapeHtml(title.split(" ")[0])}` : "Open GitHub Releases"}
        </a>
      </div>
    </article>
  `;
}

// Everything about the handheld installer stays hidden until the latest release has it.
function showInstaller(release) {
  const asset = (release.assets || []).find((a) => a.name === "fefw-recomp-install.desktop");
  const url = asset && safeUrl(asset.browser_download_url);
  if (!url) return;
  document.querySelectorAll("[data-needs-installer]").forEach((el) => {
    el.hidden = false;
  });
  const download = document.getElementById("handheld-download");
  if (download) download.href = url;
}

function renderReleases(releases) {
  const userOS = detectOS();
  const latest = releases.find((r) => !r.prerelease) || releases[0];

  if (!latest) {
    renderError("No published releases were found yet.");
    return;
  }

  const tag = latest.tag_name || latest.name || "Latest";
  const dateStr = formatDate(latest.published_at);
  const { winAsset, linuxAsset, others } = classifyAssets(latest.assets);

  // Hero pills & primary CTA
  const versionPill = document.getElementById("hero-version-pill");
  if (versionPill) {
    versionPill.textContent = dateStr ? `${tag} · ${dateStr}` : tag;
  }

  const heroMeta = document.getElementById("hero-release-meta");
  if (heroMeta) {
    heroMeta.textContent = dateStr ? `Latest release ${tag} (${dateStr})` : `Latest release ${tag}`;
  }

  const primaryBtn = document.getElementById("hero-primary-download");
  const primaryLabel = document.getElementById("hero-primary-label");
  const preferredAsset = userOS === "linux" ? (linuxAsset || winAsset) : (winAsset || linuxAsset);
  if (primaryBtn && primaryLabel) {
    if (preferredAsset) {
      primaryBtn.href = preferredAsset.browser_download_url;
      primaryLabel.textContent =
        preferredAsset === linuxAsset
          ? `Download for Linux (${tag})`
          : `Download for Windows (${tag})`;
    } else {
      primaryBtn.href = latest.html_url || `${REPO_URL}/releases/latest`;
      primaryLabel.textContent = `Download ${tag}`;
    }
  }

  showInstaller(latest);

  // Downloads grid
  const downloadsGrid = document.getElementById("downloads-grid");
  if (downloadsGrid) {
    downloadsGrid.innerHTML = [
      renderDownloadCard({
        title: "Windows (64-bit)",
        subtitle: "Windows 10 / 11 with fefw-launcher.exe. Extract and run.",
        asset: winAsset,
        fallbackUrl: latest.html_url || `${REPO_URL}/releases/latest`,
        recommended: userOS === "windows",
        tag,
      }),
      renderDownloadCard({
        title: "Linux & Steam Deck (x64)",
        subtitle: "AppImage launcher with Proton, Steam integration, and LSFG-VK.",
        asset: linuxAsset,
        fallbackUrl: latest.html_url || `${REPO_URL}/releases/latest`,
        recommended: userOS === "linux",
        tag,
      }),
    ].join("");
  }

  // Extra assets
  const extraWrap = document.getElementById("extra-assets-wrap");
  const extraList = document.getElementById("extra-assets-list");
  if (extraWrap && extraList && others.length > 0) {
    extraList.innerHTML = others
      .map(
        (a) => `
        <li>
          <a href="${escapeHtml(a.browser_download_url)}">${escapeHtml(a.name)}</a>
          <span class="asset-size">${escapeHtml(formatBytes(a.size))}</span>
        </li>
      `
      )
      .join("");
    extraWrap.hidden = false;
  }

  // Latest release notes
  const latestBox = document.getElementById("latest-release-notes");
  if (latestBox) {
    const title = latest.name && latest.name !== tag ? latest.name : `Release ${tag}`;
    latestBox.innerHTML = `
      <div class="release-header">
        <div>
          <div class="release-title-row">
            <h3>${escapeHtml(title)}</h3>
            <span class="pill pill-accent">${escapeHtml(tag)}</span>
            ${latest.prerelease ? '<span class="pill pill-gold">Pre-release</span>' : '<span class="pill pill-gold">Latest</span>'}
          </div>
          ${dateStr ? `<p class="release-date">Published ${escapeHtml(dateStr)}</p>` : ""}
        </div>
        <a href="${escapeHtml(latest.html_url || `${REPO_URL}/releases/latest`)}" class="btn btn-ghost btn-sm" target="_blank" rel="noopener noreferrer">
          View on GitHub &rarr;
        </a>
      </div>
      <div class="markdown-body">${renderMarkdown(latest.body)}</div>
    `;
  }

  // Older releases
  const older = releases.filter((r) => r.id !== latest.id).slice(0, 10);
  const olderWrap = document.getElementById("older-releases-wrap");
  const olderList = document.getElementById("older-releases-list");
  if (olderWrap && olderList && older.length > 0) {
    olderList.innerHTML = older
      .map((rel) => {
        const rTag = rel.tag_name || rel.name || "Release";
        const rTitle = rel.name && rel.name !== rTag ? rel.name : rTag;
        const rDate = formatDate(rel.published_at);
        const assetButtons = (rel.assets || [])
          .slice(0, 6)
          .map(
            (a) =>
              `<a href="${escapeHtml(a.browser_download_url)}" class="btn btn-ghost btn-sm">${escapeHtml(a.name)} (${escapeHtml(formatBytes(a.size))})</a>`
          )
          .join("");

        return `
          <details class="release-accordion">
            <summary>
              <span class="release-accordion-left">
                <span class="pill pill-accent">${escapeHtml(rTag)}</span>
                <span class="release-accordion-name">${escapeHtml(rTitle)}</span>
                ${rel.prerelease ? '<span class="pill pill-gold">Pre-release</span>' : ""}
              </span>
              <span class="release-accordion-date">${escapeHtml(rDate)}</span>
            </summary>
            <div class="release-accordion-body">
              <div class="markdown-body">${renderMarkdown(rel.body)}</div>
              <div class="release-accordion-assets">
                ${assetButtons}
                <a href="${escapeHtml(rel.html_url)}" class="btn btn-secondary btn-sm" target="_blank" rel="noopener noreferrer">View on GitHub &rarr;</a>
              </div>
            </div>
          </details>
        `;
      })
      .join("");
    olderWrap.hidden = false;
  }
}

function renderError(message) {
  const status = document.getElementById("download-status");
  if (status) {
    status.innerHTML = `${escapeHtml(message)} You can download the latest build directly from <a href="${REPO_URL}/releases" target="_blank" rel="noopener noreferrer">GitHub Releases</a>.`;
    status.hidden = false;
  }

  const versionPill = document.getElementById("hero-version-pill");
  if (versionPill) versionPill.textContent = "See GitHub Releases";

  const heroMeta = document.getElementById("hero-release-meta");
  if (heroMeta) heroMeta.textContent = "Available on GitHub Releases";

  const primaryBtn = document.getElementById("hero-primary-download");
  if (primaryBtn) primaryBtn.href = `${REPO_URL}/releases/latest`;

  const downloadsGrid = document.getElementById("downloads-grid");
  if (downloadsGrid) {
    downloadsGrid.innerHTML = [
      renderDownloadCard({
        title: "Windows (64-bit)",
        subtitle: "Download fefw-recomp-<version>-win64.zip from GitHub Releases.",
        asset: null,
        fallbackUrl: `${REPO_URL}/releases/latest`,
        recommended: detectOS() === "windows",
      }),
      renderDownloadCard({
        title: "Linux & Steam Deck (x64)",
        subtitle: "Download fefw-recomp-<version>-linux-x64.tar.gz from GitHub Releases.",
        asset: null,
        fallbackUrl: `${REPO_URL}/releases/latest`,
        recommended: detectOS() === "linux",
      }),
    ].join("");
  }

  const latestBox = document.getElementById("latest-release-notes");
  if (latestBox) {
    latestBox.innerHTML = `
      <p style="margin:0;color:var(--color-text-dim)">
        Unable to load release notes right now.
        <a href="${REPO_URL}/releases" target="_blank" rel="noopener noreferrer">Read the latest release notes on GitHub &rarr;</a>
      </p>
    `;
  }
}

function initTabs() {
  const buttons = document.querySelectorAll(".tab-btn");
  const panels = document.querySelectorAll(".tab-panel");

  const activate = (name) => {
    buttons.forEach((btn) => {
      const active = btn.dataset.tab === name;
      btn.classList.toggle("is-active", active);
      btn.setAttribute("aria-selected", String(active));
    });
    panels.forEach((panel) => {
      panel.hidden = panel.dataset.panel !== name;
    });
  };

  buttons.forEach((btn) => {
    btn.addEventListener("click", () => activate(btn.dataset.tab));
  });

  if (detectOS() === "linux") {
    activate("linux");
  }
}

function initNav() {
  const toggle = document.querySelector(".nav-toggle");
  const nav = document.getElementById("primary-nav");
  if (!toggle || !nav) return;

  toggle.addEventListener("click", () => {
    const open = nav.classList.toggle("is-open");
    toggle.setAttribute("aria-expanded", String(open));
  });

  nav.querySelectorAll("a").forEach((link) => {
    link.addEventListener("click", () => {
      nav.classList.remove("is-open");
      toggle.setAttribute("aria-expanded", "false");
    });
  });
}

async function init() {
  updateRepoLinks();
  initTabs();
  initNav();

  try {
    const releases = await fetchReleases();
    renderReleases(releases);
  } catch (err) {
    const msg =
      err?.status === 403
        ? "GitHub API rate limit reached."
        : "Could not reach the GitHub Releases API.";
    renderError(msg);
  }
}

init();
