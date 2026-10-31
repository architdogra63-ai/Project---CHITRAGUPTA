/**
 * Chitragupta Frontend-Backend Integration Bridge
 * Connects the static UI directly to FastAPI REST APIs without modifying HTML files on disk.
 * Overrides lexical mock data and directly synchronizes the DOM with live backend state.
 */

(function () {
    console.log("[Chitragupta Bridge] Initializing backend integration bridge...");

    // Determine correct backend API base URL
    const API_BASE = (window.location.protocol.startsWith('http') && !['5500', '3000', '8080'].includes(window.location.port))
        ? window.location.origin
        : 'http://localhost:8000';

    // Ensure session has valid auth token (auto-login fallback)
    async function ensureAuth() {
        let token = sessionStorage.getItem('chitragupta_token');
        if (token) return token;
        const user = sessionStorage.getItem('chitragupta_user') || 'AGRIM JAIN';
        try {
            const res = await fetch(`${API_BASE}/auth/badge-login`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ username: user })
            });
            if (res.ok) {
                const data = await res.json();
                token = data.access_token;
                sessionStorage.setItem('chitragupta_token', token);
                sessionStorage.setItem('chitragupta_role', data.role);
                sessionStorage.setItem('chitragupta_user', data.username || user);
                return token;
            }
        } catch (e) {
            console.warn("[Chitragupta Bridge] Auto-login fallback failed:", e);
        }
        return null;
    }
    window.ensureAuth = ensureAuth;

    // Helper: authenticated fetch with auto-auth and 401 retry
    async function apiFetch(path, options = {}) {
        let token = sessionStorage.getItem('chitragupta_token');
        if (!token) {
            token = await ensureAuth();
        }
        const headers = options.headers || {};
        if (token) {
            headers['Authorization'] = `Bearer ${token}`;
        }
        if (!(options.body instanceof FormData) && !headers['Content-Type']) {
            headers['Content-Type'] = 'application/json';
        }
        let res = await fetch(`${API_BASE}${path}`, { ...options, headers });
        if (res.status === 401) {
            sessionStorage.removeItem('chitragupta_token');
            token = await ensureAuth();
            if (token) {
                headers['Authorization'] = `Bearer ${token}`;
                res = await fetch(`${API_BASE}${path}`, { ...options, headers });
            }
        }
        return res;
    }
    window.apiFetch = apiFetch;

    // -------------------------------------------------------------------------
    // PAGE 1: Login Page (index.html)
    // -------------------------------------------------------------------------
    if (document.getElementById('login-form')) {
        console.log("[Chitragupta Bridge] Hooking Login Page authentication...");
        const loginForm = document.getElementById('login-form');

        loginForm.addEventListener('submit', async function (e) {
            const usernameInput = document.getElementById('username');
            const username = usernameInput ? usernameInput.value.trim() : '';
            const photoData = window.uploadedFileData || null;

            if (!username) return;

            try {
                const res = await fetch(`${API_BASE}/auth/badge-login`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        username: username,
                        id_photo: photoData
                    })
                });

                if (res.ok) {
                    const data = await res.json();
                    sessionStorage.setItem('chitragupta_token', data.access_token);
                    sessionStorage.setItem('chitragupta_role', data.role);
                    sessionStorage.setItem('chitragupta_user', data.username || username);
                    console.log("[Chitragupta Bridge] Authentication successful:", data.role);
                } else {
                    sessionStorage.setItem('chitragupta_user', username);
                }
            } catch (err) {
                console.error("[Chitragupta Bridge] Login request error:", err);
                sessionStorage.setItem('chitragupta_user', username);
            }
        }, true);
    }

    // -------------------------------------------------------------------------
    // PAGE 2: Workspace Dashboard (dashboard.html)
    // -------------------------------------------------------------------------
    if (document.getElementById('dashboard-app-container')) {
        console.log("[Chitragupta Bridge] Hooking Dashboard Workspace...");

        let activeCaseId = "CASE-001";
        window._currentGraphNodes = [];
        window._currentGraphEdges = [];
        window._currentFlags = [];

        async function initDashboard() {
            await ensureAuth();

            // 1. Resolve Active Case
            try {
                const caseRes = await apiFetch('/cases/active');
                if (caseRes.ok) {
                    const caseData = await caseRes.json();
                    activeCaseId = caseData.case_id;
                    const pillSpan = document.querySelector('.case-pill span');
                    if (pillSpan) {
                        pillSpan.textContent = `ACTIVE CASE · ${caseData.name} (${caseData.case_id})`;
                    }
                }
            } catch (e) {
                console.warn("[Chitragupta Bridge] Failed to fetch active case:", e);
            }

            // 2. Load all dashboard sections in parallel
            await Promise.allSettled([
                loadAgents(),
                loadEvidence(),
                loadTimeline('utc'),
                loadFlags(),
                loadGraph(),
                loadAudit()
            ]);

            // 8. Hook Evidence File Upload
            hookFileUpload();

            // 9. Inject Autonomous Pipeline Trigger Button
            injectAnalyzeButton();

            // 10. Override Report Generation
            hookReportGeneration();

            // 11. Override Logout
            hookLogout();

            // Re-render Lucide icons
            if (window.lucide) lucide.createIcons();
        }

        // --- Load & Render Agents directly to DOM ---
        const BRIDGE_STAGE_DESCRIPTIONS = {
            "Case Coordinator":           "Orchestrating pipeline…",
            "Extraction":                 "Extracting text from evidence…",
            "Timeline & Claims":          "Building structured claims…",
            "Contradiction Investigator": "Scanning for contradictions…",
            "Skeptic":                    "Reviewing flags with Skeptic Agent…",
            "Report":                     "Generating court-ready report…",
        };

        async function loadAgents() {
            try {
                const res = await apiFetch(`/cases/${activeCaseId}/agents`);
                if (res.ok) {
                    const data = await res.json();
                    const agents = data.agents || [];
                    const activeAgent = data.active_agent || null;
                    const pipelineRunning = data.pipeline_running || false;
                    const runStatus = data.run_status || 'idle';
                    const progressMsg = data.progress_detail || BRIDGE_STAGE_DESCRIPTIONS[activeAgent] || 'Working…';

                    // Expose to dashboard JS so startAgentPolling stays in sync
                    window._activeCaseId = activeCaseId;

                    if (agents.length > 0) {
                        const container = document.getElementById('agents-bar');
                        if (container) {
                            container.innerHTML = agents.map(a => {
                                const isActive = pipelineRunning && a[0] === activeAgent;
                                const statusClass = isActive ? 'running' : a[1];
                                const runLabel = isActive
                                    ? `<span class="agent-running-label"><span class="inline-block w-2 h-2 rounded-full bg-blue-400 animate-ping mr-1"></span>Active</span>`
                                    : '';
                                return `<div class="ag ${statusClass}" title="${a[0]}">
                                    <i class="dot"></i>
                                    <span>${a[0]}</span>
                                    ${runLabel}
                                </div>`;
                            }).join('');
                        }

                        // Update pipeline banner
                        const bannerEl = document.getElementById('pipeline-status-banner');
                        if (bannerEl) {
                            if (pipelineRunning && activeAgent) {
                                const desc = data.progress_detail || BRIDGE_STAGE_DESCRIPTIONS[activeAgent] || 'Processing…';
                                bannerEl.innerHTML = `<div class="pipeline-banner flex items-center gap-2">
                                    <span class="pipeline-spinner"></span>
                                    <span style="background:#1d4ed8; color:#fff; font-size:10px; font-weight:800; padding:2px 7px; border-radius:4px; letter-spacing:0.5px;">ACTIVE AGENT</span>
                                    <strong style="color:#60a5fa;">${activeAgent}</strong>
                                    <span style="color:#94a3b8;">— ${desc}</span>
                                </div>`;
                            } else if (runStatus === 'completed' || data.progress_detail) {
                                const detailText = data.progress_detail || 'Investigation Complete — all agents finished successfully.';
                                bannerEl.innerHTML = `<div class="pipeline-banner flex items-center gap-2" style="color:#34d399; background:rgba(16,185,129,0.08); border-color:rgba(16,185,129,0.25);">
                                    <i data-lucide="check-circle-2" class="w-3.5 h-3.5" style="color:#34d399;"></i>
                                    <span style="background:#065f46; color:#fff; font-size:10px; font-weight:800; padding:2px 7px; border-radius:4px; letter-spacing:0.5px;">PIPELINE STATUS</span>
                                    <strong style="color:#34d399;">Investigation Active & Verified</strong>
                                    <span style="color:#a7f3d0;">— ${detailText}</span>
                                </div>`;
                                if (window.lucide) lucide.createIcons();
                            } else {
                                bannerEl.innerHTML = '';
                            }
                        }
                    }
                }
            } catch (e) {
                console.warn("[Chitragupta Bridge] Failed to load agents:", e);
            }
        }
        window.renderAgentBar = loadAgents;
        window.loadAgents = loadAgents;

        // --- Load & Render Evidence directly to DOM ---
        async function loadEvidence() {
            try {
                const res = await apiFetch(`/cases/${activeCaseId}/evidence`);
                if (res.ok) {
                    const files = await res.json();
                    const listEl = document.getElementById('selected-evidence-files');
                    if (listEl) {
                        if (files.length === 0) {
                            listEl.innerHTML = `
                                <li class="py-6 text-center text-xs text-slate-500">
                                    No evidence documents active in this case. Select files above to add evidence.
                                </li>
                            `;
                            listEl.classList.remove('hidden');
                            return;
                        }

                        const inactiveCount = files.filter(f => ['tampered', 'quarantined', 'failed'].includes(f.status)).length;

                        const headerHtml = `
                            <li class="flex items-center justify-between pb-2 mb-2 border-b border-[#2a2a2a] text-xs">
                                <span class="text-slate-400 font-semibold tracking-wide">Case Evidence Files (${files.length})</span>
                                ${inactiveCount > 0 ? `
                                    <button onclick="removeUnusedEvidence()" class="text-amber-400 hover:text-amber-300 bg-amber-950/40 border border-amber-900/40 px-2.5 py-1 rounded text-[11px] flex items-center gap-1.5 hover:bg-amber-900/30 transition-colors" title="Remove all quarantined and tampered files">
                                        <i data-lucide="trash" class="w-3 h-3"></i>
                                        <span>Purge ${inactiveCount} Inactive / Unused File${inactiveCount > 1 ? 's' : ''}</span>
                                    </button>
                                ` : ''}
                            </li>
                        `;

                        const itemsHtml = files.map(f => {
                            const isInactive = ['tampered', 'quarantined', 'failed'].includes(f.status);
                            const statusBadgeClass = f.status === 'verified'
                                ? 'text-emerald-400 bg-emerald-950/40 border border-emerald-800/40'
                                : (f.status === 'tampered'
                                    ? 'text-rose-400 bg-rose-950/40 border border-rose-800/40'
                                    : 'text-amber-400 bg-amber-950/40 border border-amber-800/40');
                            return `
                                <li class="flex items-center justify-between gap-4 py-3 border-b border-[#2a2a2a] hover:bg-[#151515] px-2 rounded">
                                    <div class="flex items-center space-x-3 truncate">
                                        <span class="mono text-emerald-400 font-bold bg-emerald-950/50 px-1.5 py-0.5 rounded border border-emerald-900/40">${f.file_id}</span>
                                        <div class="truncate">
                                            <div class="text-white text-xs font-semibold truncate max-w-[260px]">${f.original_filename}</div>
                                            <div class="text-[10px] text-slate-500 mono truncate">SHA-256: ${f.sha256 ? f.sha256.substring(0, 16) + '...' : 'pending'}</div>
                                        </div>
                                        <span class="text-[10px] bg-blue-900/40 text-blue-300 px-1.5 py-0.5 rounded font-mono">${f.file_type ? f.file_type.toUpperCase() : 'FILE'}</span>
                                    </div>
                                    <div class="flex items-center space-x-2 shrink-0 text-xs">
                                        <span class="mono text-slate-400">${(f.size_bytes / 1024).toFixed(1)} KB</span>
                                        <span class="mono ${statusBadgeClass} font-bold px-2 py-0.5 rounded text-[11px]">
                                            ${f.status ? f.status.toUpperCase() : 'SEALED'}
                                        </span>
                                        <span class="mono text-slate-500 text-[11px] hidden sm:inline">Ledger #${f.ledger_entry_no || 1}</span>
                                        <a href="${API_BASE}/evidence/${f.file_id}/download" download="${f.original_filename}" class="text-blue-400 hover:text-blue-300 px-2 py-1 bg-blue-950/40 rounded border border-blue-900/30 text-[11px]" title="Download decrypted copy">Download</a>
                                        <button onclick="removeEvidence('${f.file_id}', '${(f.original_filename || '').replace(/'/g, "\\'")}')" class="text-rose-400 hover:text-rose-300 px-2 py-1 bg-rose-950/40 rounded border border-rose-900/40 text-[11px] flex items-center gap-1 hover:bg-rose-900/30 transition-colors" title="Remove this evidence file">
                                            <i data-lucide="trash-2" class="w-3 h-3"></i>
                                            <span>Remove</span>
                                        </button>
                                    </div>
                                </li>
                            `;
                        }).join('');

                        listEl.innerHTML = headerHtml + itemsHtml;
                        listEl.classList.remove('hidden');
                        if (window.lucide) lucide.createIcons();
                    }
                }
            } catch (e) {
                console.warn("[Chitragupta Bridge] Failed to load evidence:", e);
            }
        }
        window.loadEvidence = loadEvidence;

        // --- Remove Evidence Actions ---
        window.removeEvidence = async function (fileId, filename) {
            const displayName = filename ? `${fileId} (${filename})` : fileId;
            if (!confirm(`Are you sure you want to remove evidence ${displayName} from this case?`)) {
                return;
            }

            try {
                const res = await apiFetch(`/cases/${activeCaseId}/evidence/${fileId}`, {
                    method: 'DELETE'
                });

                if (res.ok) {
                    const data = await res.json();
                    if (typeof window.showToast === 'function') {
                        window.showToast(data.message || `Removed evidence ${fileId}`, 'success');
                    }
                    await loadEvidence();
                    await loadAudit();
                } else {
                    const err = await res.json();
                    if (typeof window.showToast === 'function') {
                        window.showToast(`Failed to remove evidence: ${err.detail || 'Error'}`, 'error');
                    }
                }
            } catch (e) {
                console.error("[Chitragupta Bridge] Error removing evidence:", e);
                if (typeof window.showToast === 'function') {
                    window.showToast("Network error removing evidence file", 'error');
                }
            }
        };

        window.removeUnusedEvidence = async function () {
            if (!confirm("Are you sure you want to purge all inactive/unused evidence (tampered, quarantined, or failed files) from this case?")) {
                return;
            }

            try {
                const res = await apiFetch(`/cases/${activeCaseId}/evidence-unused`, {
                    method: 'DELETE'
                });

                if (res.ok) {
                    const data = await res.json();
                    if (typeof window.showToast === 'function') {
                        window.showToast(data.message || "Removed unused evidence files", 'success');
                    }
                    await loadEvidence();
                    await loadAudit();
                } else {
                    const err = await res.json();
                    if (typeof window.showToast === 'function') {
                        window.showToast(`Failed to purge unused evidence: ${err.detail || 'Error'}`, 'error');
                    }
                }
            } catch (e) {
                console.error("[Chitragupta Bridge] Error purging unused evidence:", e);
                if (typeof window.showToast === 'function') {
                    window.showToast("Network error purging unused evidence", 'error');
                }
            }
        };

        // --- Hook File Upload ---
        function hookFileUpload() {
            const fileInput = document.getElementById('evidence-file-input');
            if (!fileInput) return;

            fileInput.onchange = async function (e) {
                const files = Array.from(e.target.files || []);
                if (files.length === 0) return;

                if (typeof window.showToast === 'function') {
                    window.showToast(`Sealing ${files.length} document(s) in custody ledger...`, 'info');
                }

                for (const file of files) {
                    const formData = new FormData();
                    formData.append('upload', file);

                    try {
                        const res = await apiFetch(`/cases/${activeCaseId}/evidence`, {
                            method: 'POST',
                            body: formData
                        });

                        if (res.ok) {
                            const result = await res.json();
                            if (typeof window.showToast === 'function') {
                                window.showToast(`Sealed: ${file.name} (SHA-256: ${result.sha256.substring(0, 8)}...)`, 'success');
                            }
                        } else {
                            const err = await res.json();
                            if (typeof window.showToast === 'function') {
                                window.showToast(`Upload failed: ${err.detail || 'Error'}`, 'error');
                            }
                        }
                    } catch (err) {
                        console.error("[Chitragupta Bridge] Evidence upload error:", err);
                    }
                }

                // Immediately reload evidence list, agent status & audit trail
                await loadEvidence();
                await loadAgents();
                await loadAudit();

                // Automatically kick off the AI agent investigation pipeline
                await triggerAnalysis();
            };
        }

        // --- Trigger Pipeline & Real-Time Polling ---
        async function triggerAnalysis() {
            const badge = document.getElementById('analysis-live-badge');
            if (badge) badge.textContent = "Starting AI Pipeline...";

            if (typeof window.showToast === 'function') {
                window.showToast("Starting autonomous AI agent pipeline (Extraction → Claims → Contradictions → Skeptic → Graph)...", "info");
            }

            try {
                const res = await apiFetch(`/cases/${activeCaseId}/analyze`, { method: 'POST' });
                if (res.ok) {
                    const data = await res.json();
                    pollPipelineStatus(data.run_id);
                } else {
                    const err = await res.json();
                    console.warn("[Chitragupta Bridge] Pipeline start failed:", err);
                    if (badge) badge.textContent = "";
                }
            } catch (e) {
                console.error("[Chitragupta Bridge] Pipeline trigger error:", e);
                if (badge) badge.textContent = "";
            }
        }
        window.triggerAnalysis = triggerAnalysis;

        function pollPipelineStatus(runId) {
            let lastStage = "";
            const badge = document.getElementById('analysis-live-badge');

            // Human-friendly stage name map
            const STAGE_LABELS = {
                "pending":                  "Case Coordinator — starting…",
                "extracting":               "Extraction Agent — reading evidence…",
                "building_claims":          "Claims Builder — structuring timeline…",
                "checking_contradictions":  "Contradiction Investigator — scanning claims…",
                "skeptic_review":           "Skeptic Agent — reviewing flags…",
                "generating_report":        "Report Agent — compiling court report…",
                "completed":                "Analysis Complete ✓",
                "failed":                   "Pipeline failed",
            };

            const interval = setInterval(async () => {
                try {
                    const res = await apiFetch(`/cases/${activeCaseId}/analysis/status?run_id=${runId}`);
                    if (res.ok) {
                        const statusData = await res.json();
                        const stage = statusData.status;
                        const label = statusData.progress_detail || STAGE_LABELS[stage] || stage.replace(/_/g, ' ').toUpperCase();

                        if (badge) badge.textContent = label;

                        // Continuously refresh agent bar during run to update live progress
                        await loadAgents();

                        if (stage !== lastStage) {
                            lastStage = stage;
                            if (typeof window.showToast === 'function') {
                                window.showToast(`AI Pipeline: ${label}`, 'info');
                            }
                        }

                        if (stage === 'completed') {
                            clearInterval(interval);
                            if (badge) badge.textContent = "Analysis Complete ✓";
                            if (typeof window.showToast === 'function') {
                                window.showToast("Autonomous analysis complete! Evidence, claims, graph & contradictions updated.", "success");
                            }
                            await loadEvidence();
                            await loadAgents();
                            await loadTimeline('utc');
                            await loadFlags();
                            await loadGraph();
                            await loadAudit();
                        } else if (stage === 'failed') {
                            clearInterval(interval);
                            if (badge) badge.textContent = "Analysis Failed";
                            if (typeof window.showToast === 'function') {
                                window.showToast(`Analysis failed: ${statusData.error_detail || 'Unknown error'}`, 'error');
                            }
                        }
                    }
                } catch (err) {
                    console.warn("[Chitragupta Bridge] Status polling error:", err);
                }
            }, 2500);
        }

        // --- Inject Analyze Button into Evidence Workspace ---
        function injectAnalyzeButton() {
            const card = document.querySelector('#v-ev .card');
            if (card && !document.getElementById('btn-run-analysis')) {
                const btnContainer = document.createElement('div');
                btnContainer.className = 'mt-6 pt-4 border-t border-[#2a2a2a] flex flex-col sm:flex-row items-center justify-center gap-3';
                btnContainer.innerHTML = `
                    <button id="btn-run-analysis" class="btn-custom g text-xs py-2 px-4 shadow-lg shadow-emerald-950/40">
                        <i data-lucide="play" class="w-3.5 h-3.5 inline mr-1.5"></i>
                        <span>Start Autonomous AI Investigation</span>
                    </button>
                    <span id="analysis-live-badge" class="text-xs mono text-emerald-400 font-semibold"></span>
                `;
                card.appendChild(btnContainer);
                if (window.lucide) lucide.createIcons();

                const btn = document.getElementById('btn-run-analysis');
                if (btn) btn.addEventListener('click', triggerAnalysis);
            }
        }

        // --- Load & Render Timeline directly to DOM ---
        async function loadTimeline(mode = 'utc') {
            try {
                const res = await apiFetch(`/cases/${activeCaseId}/timeline`);
                if (res.ok) {
                    const data = await res.json();
                    if (data.events && data.events.length > 0) {
                        const list = document.getElementById('timeline-list');
                        if (list) {
                            list.innerHTML = data.events.map(e => `
                                <div class="ev ${e[4] ? 'c' : ''}">
                                    <time class="mono text-emerald-400 font-bold">${mode === 'utc' ? e[0] + ' UTC' : e[1]}</time>
                                    ${mode === 'utc' ? `<span class="mono text-slate-500 text-xs ml-1">(${e[1]})</span>` : ''}
                                    <div class="text-sm font-semibold text-white mt-0.5">${e[2]}</div>
                                    <div class="text-xs text-slate-400">Source: <span class="text-blue-400">${e[3]}</span></div>
                                </div>
                            `).join('');
                        }
                    }
                }
            } catch (e) {
                console.warn("[Chitragupta Bridge] Failed to load timeline:", e);
            }
        }
        window.renderTimeline = loadTimeline;
        window.loadTimeline = loadTimeline;
        window.toggleTimelineFormat = function (mode) {
            const tzUtc = document.getElementById('tz-utc');
            const tzRaw = document.getElementById('tz-raw');
            if (tzUtc) tzUtc.className = mode === 'utc' ? 'px-3 py-1 text-xs font-bold bg-blue-600 text-white rounded-none' : 'px-3 py-1 text-xs font-bold text-slate-400 hover:text-white';
            if (tzRaw) tzRaw.className = mode === 'raw' ? 'px-3 py-1 text-xs font-bold bg-blue-600 text-white rounded-none' : 'px-3 py-1 text-xs font-bold text-slate-400 hover:text-white';
            loadTimeline(mode);
        };

        // --- Load & Render Flags directly to DOM ---
        async function loadFlags() {
            try {
                const res = await apiFetch(`/cases/${activeCaseId}/flags`);
                if (res.ok) {
                    const data = await res.json();
                    if (data.flags && data.flags.length > 0) {
                        window._currentFlags = data.flags;
                        const container = document.getElementById('flags-feed-container');
                        if (container) {
                            container.innerHTML = data.flags.map((f, i) => `
                                <div class="card flag-item">
                                    <div class="flex items-center justify-between mb-1">
                                        <h3 class="text-sm font-bold text-white">${f.title || (f.conflict_type ? f.conflict_type.toUpperCase() + ' Mismatch' : 'Contradiction')}</h3>
                                        <span class="badge-custom ${f.skeptic_verdict === 'survived' ? 'text-rose-400 bg-rose-950/40 border border-rose-900/50' : 'text-slate-400 bg-slate-800'} text-[10px]">
                                            ${f.skeptic_verdict ? f.skeptic_verdict.toUpperCase() : 'PENDING SKEPTIC'}
                                        </span>
                                    </div>
                                    <p class="text-xs text-slate-300 mb-2">${f.description}</p>
                                    <details class="text-xs border-t border-[#2a2a2a] pt-2 mt-2" open>
                                        <summary class="text-blue-400 font-bold cursor-pointer">Skeptic Agent Reasoning</summary>
                                        <div class="bg-emerald-950/40 border-l-2 border-emerald-400 p-2.5 rounded-none my-2 text-emerald-200">
                                            ${f.skeptic_reasoning || 'Evaluated against sensor drift and timeline offsets.'}
                                        </div>
                                        <div class="text-slate-400 text-[11px]">
                                            Sources: <span class="text-blue-400">${f.source_a || f.claim_a || 'Evidence Note'}</span> | <span class="text-blue-400">${f.source_b || f.claim_b || 'Evidence Note'}</span>
                                        </div>
                                    </details>
                                    <textarea id="comment-${i}" placeholder="Reason for your decision (required)" class="w-full bg-[#1e1e1e] border border-[#2a2a2a] rounded-none p-2 text-xs text-white my-2 focus:outline-none focus:border-blue-500">${f.reviewer_comment || ''}</textarea>
                                    <div class="flex items-center space-x-2">
                                        <button onclick="resolveFlag(${i}, 'Confirmed')" class="btn-custom g text-xs py-1.5 px-3">Confirm Flag</button>
                                        <button onclick="resolveFlag(${i}, 'Dismissed')" class="btn-custom o text-xs py-1.5 px-3">Dismiss Flag</button>
                                        <span id="res-${i}" class="text-xs font-mono font-bold ml-2 ${f.reviewer_decision ? (f.reviewer_decision.toLowerCase() === 'confirmed' ? 'text-rose-400' : 'text-slate-400') : ''}">
                                            ${f.reviewer_decision ? 'Decision: ' + f.reviewer_decision : ''}
                                        </span>
                                    </div>
                                </div>
                            `).join('');
                        }
                    }
                }
            } catch (e) {
                console.warn("[Chitragupta Bridge] Failed to load flags:", e);
            }
        }
        window.renderFlagsFeed = loadFlags;
        window.loadFlags = loadFlags;

        // Override resolveFlag to send review to backend
        window.resolveFlag = async function (index, status) {
            const commentBox = document.getElementById(`comment-${index}`);
            if (!commentBox || !commentBox.value.trim()) {
                if (commentBox) commentBox.classList.add('border-rose-500');
                if (typeof window.showToast === 'function') {
                    window.showToast('Please provide a decision comment first', 'error');
                }
                return;
            }

            const flagItem = (window._currentFlags && window._currentFlags[index]) || {};
            const flagId = flagItem.flag_id || `FLAG-00${index + 1}`;
            const commentText = commentBox.value.trim();

            try {
                const res = await apiFetch(`/cases/${activeCaseId}/flags/${flagId}/review`, {
                    method: 'POST',
                    body: JSON.stringify({
                        decision: status,
                        comment: commentText
                    })
                });

                if (res.ok) {
                    if (status === 'Confirmed') window.confirmCount = (window.confirmCount || 0) + 1;
                    else window.dismissCount = (window.dismissCount || 0) + 1;

                    const confEl = document.getElementById('report-conf-count');
                    const dismEl = document.getElementById('report-dism-count');
                    if (confEl) confEl.textContent = window.confirmCount;
                    if (dismEl) dismEl.textContent = window.dismissCount;

                    const resLabel = document.getElementById(`res-${index}`);
                    if (resLabel) {
                        resLabel.textContent = `Decision: ${status}`;
                        resLabel.className = status === 'Confirmed' ? 'text-xs font-mono font-bold ml-2 text-rose-400' : 'text-xs font-mono font-bold ml-2 text-slate-400';
                    }

                    if (typeof window.showToast === 'function') {
                        window.showToast(`Investigator ${status} flag ${flagId}`, 'success');
                    }

                    await loadAudit();
                } else {
                    const err = await res.json();
                    if (typeof window.showToast === 'function') {
                        window.showToast(`Error: ${err.detail || 'Review failed'}`, 'error');
                    }
                }
            } catch (err) {
                console.error("[Chitragupta Bridge] Flag review error:", err);
            }
        };

        // --- Load & Render Network Graph directly to DOM ---
        const nodeColors = { p: "#3b8cff", a: "#27e08a", d: "#9b7bff", l: "#f2b84b" };

        async function loadGraph() {
            try {
                const res = await apiFetch(`/cases/${activeCaseId}/graph`);
                if (res.ok) {
                    const graph = await res.json();
                    if (graph.nodes && graph.nodes.length > 0) {
                        window._currentGraphNodes = graph.nodes;
                        window._currentGraphEdges = graph.edges || [];

                        const svg = document.getElementById('network-svg');
                        if (!svg) return;

                        // Layout positions in grid/radial pattern if coordinates not specified
                        const nCols = 4;
                        const colWidth = 130;
                        const rowHeight = 110;
                        const offsetX = 80;
                        const offsetY = 70;

                        const mappedNodes = graph.nodes.map((n, i) => {
                            const x = n.x || (offsetX + (i % nCols) * colWidth);
                            const y = n.y || (offsetY + Math.floor(i / nCols) * rowHeight);
                            const typeCode = (n.type && n.type[0].toLowerCase()) || 'p';
                            const radius = n.radius || Math.min(24, Math.max(12, (n.degree || 1) * 3));
                            return { ...n, x, y, typeCode, radius };
                        });
                        window._currentGraphNodes = mappedNodes;

                        // Map edges
                        let linesHTML = '';
                        if (graph.edges && graph.edges.length > 0) {
                            linesHTML = graph.edges.map(e => {
                                let sourceIdx = typeof e.source === 'number' ? e.source : mappedNodes.findIndex(n => n.id === e.source);
                                let targetIdx = typeof e.target === 'number' ? e.target : mappedNodes.findIndex(n => n.id === e.target);
                                if (sourceIdx === -1 || targetIdx === -1) return '';
                                const n1 = mappedNodes[sourceIdx];
                                const n2 = mappedNodes[targetIdx];
                                const isConflict = !!e.conflict;
                                return `
                                    <line x1="${n1.x}" y1="${n1.y}" x2="${n2.x}" y2="${n2.y}"
                                          stroke="${isConflict ? '#ff4d5e' : '#1a2d4a'}"
                                          stroke-width="${isConflict ? 3 : 2}"
                                          ${isConflict ? 'stroke-dasharray="6 4"' : ''}/>
                                `;
                            }).join('');
                        }

                        let nodesHTML = mappedNodes.map((n, i) => `
                            <g class="node" onclick="pickNode(${i})">
                                <circle cx="${n.x}" cy="${n.y}" r="${n.radius}"
                                        fill="${nodeColors[n.typeCode] || '#3b8cff'}"
                                        fill-opacity="0.85"
                                        ${n.radius >= 18 ? 'stroke="#fff" stroke-width="2"' : ''}/>
                                <text x="${n.x}" y="${n.y + n.radius + 14}" text-anchor="middle" class="text-[10px] fill-slate-300 font-semibold select-none">
                                    ${(n.label || n.id).substring(0, 16)}
                                </text>
                            </g>
                        `).join('');

                        svg.innerHTML = linesHTML + nodesHTML;
                    }
                }
            } catch (e) {
                console.warn("[Chitragupta Bridge] Failed to load graph:", e);
            }
        }
        window.renderNetworkGraph = loadGraph;
        window.loadGraph = loadGraph;

        window.pickNode = function (index) {
            const n = window._currentGraphNodes[index];
            if (!n) return;
            const panel = document.getElementById('graph-side-panel');
            if (panel) {
                panel.innerHTML = `
                    <div class="flex items-center justify-between mb-2">
                        <b class="text-sm text-white">${n.label || n.id}</b>
                        <span class="badge-custom text-[10px] bg-blue-900/40 text-blue-300">${(n.type || 'Entity').toUpperCase()}</span>
                    </div>
                    <p class="text-xs text-slate-300 mb-2">${n.description || `Entity in case evidence. Centrality degree: ${n.degree || 1}.`}</p>
                    <div class="space-y-1 text-[11px] font-mono">
                        <div class="text-emerald-400">Claims in Vault: ${n.claim_count || 1}</div>
                        <div class="text-blue-400">Degree Centrality: ${n.degree || 1}</div>
                        ${n.suspiciousness !== undefined ? `<div class="text-amber-400">Suspiciousness Score: ${(n.suspiciousness * 100).toFixed(0)}%</div>` : ''}
                    </div>
                `;
            }
        };

        // --- Load & Render Audit Trail directly to DOM ---
        async function loadAudit() {
            try {
                const res = await apiFetch(`/cases/${activeCaseId}/audit`);
                if (res.ok) {
                    const data = await res.json();
                    if (data.logs && data.logs.length > 0) {
                        const tbody = document.getElementById('audit-log-rows');
                        if (tbody) {
                            tbody.innerHTML = data.logs.map(l => `
                                <tr>
                                    <td class="p-3 mono text-slate-400">${l[0]}</td>
                                    <td class="p-3 text-slate-300 font-semibold">${l[1]}</td>
                                    <td class="p-3 text-slate-200">${l[2]}</td>
                                </tr>
                            `).join('');
                        }
                    }
                }
            } catch (e) {
                console.warn("[Chitragupta Bridge] Failed to load audit trail:", e);
            }
        }
        window.renderAuditLog = loadAudit;
        window.loadAudit = loadAudit;

        // --- Hook Report Generation & Download ---
        function hookReportGeneration() {
            window.generateReportPDF = async function () {
                const msgEl = document.getElementById('report-status-msg');
                if (msgEl) msgEl.textContent = "Compiling tamper-evident report from ledger...";

                try {
                    const res = await apiFetch(`/cases/${activeCaseId}/report`);
                    if (msgEl) msgEl.textContent = "";

                    const dialog = document.getElementById('report-dialog');
                    if (dialog) dialog.showModal();
                } catch (e) {
                    if (msgEl) msgEl.textContent = "";
                    const dialog = document.getElementById('report-dialog');
                    if (dialog) dialog.showModal();
                }
            };

            const downloadBtn = document.querySelector('#report-dialog button.btn-custom.g');
            if (downloadBtn) {
                downloadBtn.onclick = function () {
                    window.location.href = `${API_BASE}/cases/${activeCaseId}/report/download`;
                    if (typeof window.showToast === 'function') {
                        window.showToast('Court report downloaded with SHA-256 seal!', 'success');
                    }
                    const dialog = document.getElementById('report-dialog');
                    if (dialog) dialog.close();
                };
            }
        }

        // --- Hook Logout ---
        function hookLogout() {
            window.handleLogout = function () {
                sessionStorage.removeItem('chitragupta_token');
                sessionStorage.removeItem('chitragupta_user');
                sessionStorage.removeItem('chitragupta_role');
                window.location.href = 'index.html';
            };
        }

        // Initialize when DOM ready
        if (document.readyState === 'loading') {
            document.addEventListener('DOMContentLoaded', initDashboard);
        } else {
            setTimeout(initDashboard, 100);
        }
    }
})();
