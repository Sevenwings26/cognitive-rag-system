/**
 * Enterprise Multi-Tenant RAG System
 * Observatory APM & Model Capacity Planner Controller
 * static/js/observatory.js
 */

(function (window, document) {
    'use strict';

    let currentObsRange = "7d";
    let obsPollTimer = null;
    let cachedHostInfo = null;

    function escapeHTML(str) {
        if (str == null) return '';
        return String(str)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#039;');
    }

    function formatGigabytes(value) {
        const amount = Number(value);
        return Number.isFinite(amount) ? `${amount.toFixed(1)} GB` : "Unavailable";
    }

    // =========================================================================
    // 1. Error Banner Controller
    // =========================================================================
    function showObservatoryError(message) {
        const banner = document.getElementById("obs-error-banner");
        const msgEl = document.getElementById("obs-error-message");
        if (banner && msgEl) {
            msgEl.textContent = message || "An unexpected error occurred while communicating with the telemetry service.";
            banner.hidden = false;
        }
    }

    function clearObservatoryError() {
        const banner = document.getElementById("obs-error-banner");
        if (banner) {
            banner.hidden = true;
        }
    }

    // =========================================================================
    // 2. Data Fetching & Polling
    // =========================================================================
    async function fetchObservatoryData(range = currentObsRange) {
        if (!Auth.hasAdminAccess()) {
            if (obsPollTimer) {
                clearInterval(obsPollTimer);
                obsPollTimer = null;
            }
            return;
        }

        try {
            clearObservatoryError();
            const data = await fetchAPI(`/api/observatory?range=${encodeURIComponent(range)}`);
            if (!data || data.status !== "ok") {
                const detailMsg = data?.detail || "Received non-OK response from observatory endpoint.";
                showObservatoryError(detailMsg);
                return;
            }

            // Update live timestamp
            const timeEl = document.getElementById("obs-live-time");
            if (timeEl) {
                timeEl.textContent = new Date().toLocaleTimeString();
            }

            // Render all sections
            renderObservatoryKpis(data.kpis);
            renderObservatoryGauges(data.gauges);
            renderObservatoryGpu(data.gpu_resource_allocation);
            renderObservatoryCharts(data.time_series);
        } catch (err) {
            console.error("Failed to fetch observatory telemetry:", err);
            if (err.status === 401) {
                showObservatoryError("Session expired. Please log in again.");
                Auth.logout();
            } else if (err.status === 403) {
                showObservatoryError("Access denied: Observatory APM requires administrator privileges.");
            } else {
                showObservatoryError(`Observatory update failed: ${err.message || 'Server error'}`);
            }
        }
    }

    async function fetchHostInfo(force = false) {
        if (!Auth.hasAdminAccess()) return;
        if (!force && cachedHostInfo) {
            renderHostAccelerators(cachedHostInfo);
            return;
        }

        try {
            cachedHostInfo = await fetchAPI("/api/host-info");
            renderHostAccelerators(cachedHostInfo);
        } catch (err) {
            console.error("Failed to inspect host hardware:", err);
            renderHostAccelerators(null);
        }
    }

    // =========================================================================
    // 3. KPI Rendering
    // =========================================================================
    function renderObservatoryKpis(kpis = {}) {
        const setTxt = (id, val) => {
            const el = document.getElementById(id);
            if (el) el.textContent = val;
        };

        setTxt("obs-kpi-uptime", kpis.uptime_human || "--");
        setTxt("obs-kpi-requests", kpis.total_requests != null ? Number(kpis.total_requests).toLocaleString() : "--");
        setTxt("obs-kpi-user-sub", kpis.user_requests != null ? Number(kpis.user_requests).toLocaleString() : "0");
        setTxt("obs-kpi-sys-sub", kpis.system_requests != null ? Number(kpis.system_requests).toLocaleString() : "0");
        setTxt("obs-kpi-concurrency", kpis.concurrency_current != null ? kpis.concurrency_current : "--");
        setTxt("obs-kpi-avg-resp", kpis.avg_response_time_sec != null ? `${Number(kpis.avg_response_time_sec).toFixed(2)}s` : "--");
        setTxt("obs-kpi-p95-resp", kpis.p95_response_time_sec != null ? `${Number(kpis.p95_response_time_sec).toFixed(2)}s` : "--");
        setTxt("obs-kpi-app-error", kpis.app_error_rate_pct != null ? `${Number(kpis.app_error_rate_pct).toFixed(1)}%` : "--");
        setTxt("obs-kpi-ext-error", kpis.ext_error_rate_pct != null ? `${Number(kpis.ext_error_rate_pct).toFixed(1)}%` : "--");
        setTxt("obs-sessions-val", kpis.active_sessions != null ? kpis.active_sessions : "--");
    }

    // =========================================================================
    // 4. Gauge Widgets Rendering
    // =========================================================================
    function renderObservatoryGauges(gauges = {}) {
        const CIRCLE_CIRCUMFERENCE = 314.15; // 2 * PI * 50

        // 1. Backend Split Donut
        const split = gauges.backend_split_total || {};
        const ragDonut = document.getElementById("obs-donut-rag");
        const ragText = document.getElementById("obs-donut-text");
        const ragVal = document.getElementById("obs-split-rag-val");
        const sqlVal = document.getElementById("obs-split-sql-val");

        if (split.rag_percent == null || split.sql_percent == null) {
            if (ragDonut) ragDonut.style.strokeDashoffset = CIRCLE_CIRCUMFERENCE;
            if (ragText) ragText.textContent = "--";
            if (ragVal) ragVal.textContent = "--";
            if (sqlVal) sqlVal.textContent = "--";
        } else {
            if (ragDonut) {
                const offset = CIRCLE_CIRCUMFERENCE * (1 - (split.rag_percent / 100));
                ragDonut.style.strokeDashoffset = offset;
            }
            if (ragText) ragText.textContent = `${Math.round(split.rag_percent)}%`;
            if (ragVal) ragVal.textContent = `${split.rag_percent}%`;
            if (sqlVal) sqlVal.textContent = `${split.sql_percent}%`;
        }

        // 2. Host CPU Gauge
        const cpu = gauges.host_cpu || {};
        const cpuGauge = document.getElementById("obs-gauge-cpu");
        const cpuText = document.getElementById("obs-gauge-cpu-text");
        const cpuSub = document.getElementById("obs-gauge-cpu-sub");

        if (cpu.percent == null || cpu.status === "unavailable") {
            if (cpuGauge) cpuGauge.style.strokeDashoffset = CIRCLE_CIRCUMFERENCE;
            if (cpuText) cpuText.textContent = "Unavailable";
            if (cpuSub) cpuSub.textContent = cpu.cores ? `${cpu.cores} cores · unprobed` : "--";
        } else {
            if (cpuGauge) {
                const offset = CIRCLE_CIRCUMFERENCE * (1 - (cpu.percent / 100));
                cpuGauge.style.strokeDashoffset = offset;
            }
            if (cpuText) cpuText.textContent = `${Math.round(cpu.percent)}%`;
            if (cpuSub) cpuSub.textContent = `${cpu.cores} cores · load ${cpu.load_average_1m != null ? cpu.load_average_1m : "--"}`;
        }

        // 3. Host Memory Gauge
        const mem = gauges.host_memory || {};
        const memGauge = document.getElementById("obs-gauge-mem");
        const memText = document.getElementById("obs-gauge-mem-text");
        const memSub = document.getElementById("obs-gauge-mem-sub");

        if (mem.percent == null || mem.status === "unavailable") {
            if (memGauge) memGauge.style.strokeDashoffset = CIRCLE_CIRCUMFERENCE;
            if (memText) memText.textContent = "Unavailable";
            if (memSub) memSub.textContent = "-- / -- GB";
        } else {
            if (memGauge) {
                const offset = CIRCLE_CIRCUMFERENCE * (1 - (mem.percent / 100));
                memGauge.style.strokeDashoffset = offset;
            }
            if (memText) memText.textContent = `${Math.round(mem.percent)}%`;
            if (memSub) memSub.textContent = `${mem.used_gb != null ? mem.used_gb.toFixed(1) : "--"} / ${mem.total_gb != null ? mem.total_gb.toFixed(1) : "--"} GB`;
        }
    }

    // =========================================================================
    // 5. Per-Service Accelerator Allocation Bars
    // =========================================================================
    function renderObservatoryGpu(allocations = []) {
        const container = document.getElementById("obs-gpu-bars-container");
        if (!container) return;
        container.innerHTML = "";

        const tag = document.getElementById("obs-gpu-service-tag");
        if (tag) {
            tag.textContent = allocations && allocations.length
                ? `${allocations.length} service${allocations.length === 1 ? "" : "s"}`
                : "Not configured";
        }

        if (!allocations || !allocations.length) {
            const emptyNotice = document.createElement("div");
            emptyNotice.className = "text-muted text-xs";
            emptyNotice.style.padding = "8px 0";
            emptyNotice.textContent = "No per-service allocation collector is configured. Host hardware remains visible above; Ollama or vLLM will appear when runtime-level attribution is enabled.";
            container.appendChild(emptyNotice);
            return;
        }

        allocations.forEach((item) => {
            const maxVal = Math.max(item.allocated_gb, item.used_gb, 1);
            const allocWidth = Math.min(100, Math.round((item.allocated_gb / maxVal) * 100));
            const usedWidth = Math.min(100, Math.round((item.used_gb / maxVal) * 100));

            const row = document.createElement("div");
            row.className = "obs-gpu-service-row";
            row.innerHTML = `
                <span class="service-title">${escapeHTML(item.service)}</span>
                <div class="progress-track-row">
                    <span class="progress-track-label label-alloc">Allocated</span>
                    <div class="progress-bar-bg">
                        <div class="progress-bar-fill fill-alloc" style="width: ${allocWidth}%"></div>
                    </div>
                    <span class="progress-val-text">${item.allocated_gb.toFixed(1)} GB</span>
                </div>
                <div class="progress-track-row">
                    <span class="progress-track-label label-used">Used</span>
                    <div class="progress-bar-bg">
                        <div class="progress-bar-fill fill-used" style="width: ${usedWidth}%"></div>
                    </div>
                    <span class="progress-val-text">${item.used_gb.toFixed(1)} GB</span>
                </div>
            `;
            container.appendChild(row);
        });
    }

    // =========================================================================
    // 6. Host Accelerator Inventory
    // =========================================================================
    function renderHostAccelerators(hostInfo) {
        const container = document.getElementById("obs-accelerator-inventory");
        const status = document.getElementById("obs-accelerator-status");
        if (!container || !status) return;

        const gpu = hostInfo?.gpu;
        const devices = Array.isArray(gpu?.devices) ? gpu.devices : [];
        if (!hostInfo || !gpu) {
            status.textContent = "Unavailable";
            container.innerHTML = `<p class="text-muted">Host accelerator inventory is unavailable.</p>`;
            return;
        }

        if (!gpu.gpu_available || !devices.length) {
            status.textContent = "CPU Only";
            container.innerHTML = `
                <div class="obs-accelerator-empty">
                    No GPU accelerator detected. Workloads will execute on Host CPU with ${escapeHTML(formatGigabytes(hostInfo.memory?.total_ram_gb))} RAM.
                </div>`;
            return;
        }

        status.textContent = `${devices.length} accelerator${devices.length === 1 ? "" : "s"}`;
        container.innerHTML = devices.map((device) => {
            const unified = String(device.memory_type || "").toLowerCase() === "unified";
            const primaryMemoryLabel = unified ? "Unified Memory Pool" : "Dedicated VRAM";
            const primaryMemory = unified
                ? (device.unified_memory_pool_gb ?? hostInfo.memory?.total_ram_gb)
                : (device.discrete_vram_total_gb || device.vram_total_gb);
            const usableLabel = unified ? "Estimated Usable for Workloads" : "Available VRAM at Probe";
            const usableMemory = unified
                ? device.estimated_usable_memory_gb
                : (device.discrete_vram_available_gb || device.vram_available_gb);
            const topologyNote = unified
                ? "Shared unified memory with the host; physical memory is shared between CPU and NPU/GPU."
                : "Discrete dedicated device memory reported by the hardware probe.";

            return `
                <article class="obs-accelerator-card">
                    <div class="obs-accelerator-heading">
                        <div>
                            <strong>${escapeHTML(device.model_name || "Unknown accelerator")}</strong>
                            <div>${escapeHTML(device.vendor || "Unknown vendor")} · ${escapeHTML(device.architecture || "Unknown arch")}</div>
                        </div>
                        <span class="obs-memory-type">${escapeHTML(unified ? "Unified Memory" : "Discrete GPU")}</span>
                    </div>
                    <div class="obs-accelerator-facts">
                        <div><span>${primaryMemoryLabel}</span><strong>${escapeHTML(formatGigabytes(primaryMemory))}</strong></div>
                        <div><span>${usableLabel}</span><strong>${escapeHTML(formatGigabytes(usableMemory))}</strong></div>
                        <div><span>Driver</span><strong>${escapeHTML(device.driver_version || "Unavailable")}</strong></div>
                        <div><span>Detection Method</span><strong>${escapeHTML(gpu.detection_method || "Unknown")}</strong></div>
                        <div><span>Probe Confidence</span><strong>${escapeHTML(device.detection_confidence || "Unknown")}</strong></div>
                        <div><span>Device Slot</span><strong>${escapeHTML(device.slot || "Unknown")}</strong></div>
                    </div>
                    <p class="obs-accelerator-note">${topologyNote}</p>
                </article>`;
        }).join("");
    }

    // =========================================================================
    // 7. Pure-SVG Chart Generators
    // =========================================================================
    function renderObservatoryCharts(series = {}) {
        renderLineChart("chart-response-time", series.response_time_over_time || [], ["avg", "p95"], ["#14B8A6", "#06B6D4"], "s");
        renderLineChart("chart-concurrency", series.concurrency_over_time || [], ["concurrency"], ["#8B5CF6"], "");
        renderBarChart("chart-backend-split", series.backend_split_over_time || []);
        renderLineChart(
            "chart-host-utilization",
            series.host_utilization_over_time || [],
            ["cpu_pct", "gpu_memory_utilization_percent", "gpu_compute_utilization_percent"],
            ["#14B8A6", "#06B6D4", "#8B5CF6"],
            "%"
        );
    }

    function renderLineChart(containerId, dataPoints, keys, colors, unit = "") {
        const el = document.getElementById(containerId);
        if (!el) return;
        if (!dataPoints || dataPoints.length === 0) {
            el.innerHTML = `<div style="display:flex;align-items:center;justify-content:center;height:100%;color:var(--text-muted);font-size:12px;">No activity data in range</div>`;
            return;
        }

        const width = el.clientWidth || 400;
        const height = 135;
        const padding = { top: 15, right: 15, bottom: 25, left: 35 };
        const plotW = width - padding.left - padding.right;
        const plotH = height - padding.top - padding.bottom;

        let maxVal = 0.1;
        dataPoints.forEach(p => {
            keys.forEach(k => {
                if (p[k] != null) {
                    const val = Number(p[k]);
                    if (val > maxVal) maxVal = val;
                }
            });
        });
        maxVal = Math.ceil(maxVal * 1.15) || 1;

        let svgLines = "";
        keys.forEach((k, ki) => {
            const pointsStr = dataPoints.map((p, i) => {
                const x = padding.left + (i / Math.max(dataPoints.length - 1, 1)) * plotW;
                const val = p[k] != null ? Number(p[k]) : 0;
                const y = padding.top + plotH - ((val / maxVal) * plotH);
                return `${x.toFixed(1)},${y.toFixed(1)}`;
            }).join(" ");

            svgLines += `<polyline fill="none" stroke="${colors[ki]}" stroke-width="2" stroke-linecap="round" points="${pointsStr}" />`;
        });

        const firstLabel = escapeHTML(dataPoints[0]?.timestamp || "");
        const lastLabel = escapeHTML(dataPoints[dataPoints.length - 1]?.timestamp || "");

        el.innerHTML = `
            <svg width="100%" height="${height}" viewBox="0 0 ${width} ${height}">
                <line x1="${padding.left}" y1="${padding.top}" x2="${width - padding.right}" y2="${padding.top}" stroke="var(--border-subtle)" stroke-dasharray="2" />
                <line x1="${padding.left}" y1="${padding.top + plotH / 2}" x2="${width - padding.right}" y2="${padding.top + plotH / 2}" stroke="var(--border-subtle)" stroke-dasharray="2" />
                <line x1="${padding.left}" y1="${padding.top + plotH}" x2="${width - padding.right}" y2="${padding.top + plotH}" stroke="var(--border-subtle)" />

                <text x="${padding.left - 6}" y="${padding.top + 4}" fill="var(--text-muted)" font-size="9" text-anchor="end">${maxVal.toFixed(unit === '%' ? 0 : 2)}${unit}</text>
                <text x="${padding.left - 6}" y="${padding.top + plotH}" fill="var(--text-muted)" font-size="9" text-anchor="end">0${unit}</text>

                ${svgLines}

                <text x="${padding.left}" y="${height - 6}" fill="var(--text-muted)" font-size="9">${firstLabel}</text>
                <text x="${width - padding.right}" y="${height - 6}" fill="var(--text-muted)" font-size="9" text-anchor="end">${lastLabel}</text>
            </svg>
        `;
    }

    function renderBarChart(containerId, buckets = []) {
        const el = document.getElementById(containerId);
        if (!el) return;
        if (!buckets || buckets.length === 0) {
            el.innerHTML = `<div style="display:flex;align-items:center;justify-content:center;height:100%;color:var(--text-muted);font-size:12px;">No request activity in range</div>`;
            return;
        }

        const width = el.clientWidth || 800;
        const height = 120;
        const padding = { top: 10, right: 15, bottom: 20, left: 35 };
        const plotW = width - padding.left - padding.right;
        const plotH = height - padding.top - padding.bottom;

        let maxVal = 5;
        buckets.forEach(b => {
            const total = (b.rag || 0) + (b.sql || 0);
            if (total > maxVal) maxVal = total;
        });
        maxVal = Math.ceil(maxVal * 1.15);

        const barSlotW = plotW / Math.max(buckets.length, 1);
        const barW = Math.max(4, Math.min(16, barSlotW * 0.6));

        let barsSvg = "";
        buckets.forEach((b, i) => {
            const x = padding.left + (i * barSlotW) + (barSlotW - barW) / 2;
            const ragH = ((b.rag || 0) / maxVal) * plotH;
            const sqlH = ((b.sql || 0) / maxVal) * plotH;

            const ragY = padding.top + plotH - ragH;
            const sqlY = ragY - sqlH;

            if (ragH > 0) {
                barsSvg += `<rect x="${x.toFixed(1)}" y="${ragY.toFixed(1)}" width="${barW}" height="${ragH.toFixed(1)}" fill="#EAB308" rx="2" />`;
            }
            if (sqlH > 0) {
                barsSvg += `<rect x="${x.toFixed(1)}" y="${sqlY.toFixed(1)}" width="${barW}" height="${sqlH.toFixed(1)}" fill="#3B82F6" rx="2" />`;
            }
        });

        const firstLabel = escapeHTML(buckets[0]?.timestamp?.slice(11, 16) || "00:00");
        const lastLabel = escapeHTML(buckets[buckets.length - 1]?.timestamp?.slice(11, 16) || "23:00");

        el.innerHTML = `
            <svg width="100%" height="${height}" viewBox="0 0 ${width} ${height}">
                <line x1="${padding.left}" y1="${padding.top + plotH}" x2="${width - padding.right}" y2="${padding.top + plotH}" stroke="var(--border-subtle)" />
                <text x="${padding.left - 6}" y="${padding.top + 8}" fill="var(--text-muted)" font-size="9" text-anchor="end">${maxVal}</text>
                <text x="${padding.left - 6}" y="${padding.top + plotH}" fill="var(--text-muted)" font-size="9" text-anchor="end">0</text>
                ${barsSvg}
                <text x="${padding.left}" y="${height - 4}" fill="var(--text-muted)" font-size="9">${firstLabel}</text>
                <text x="${width - padding.right}" y="${height - 4}" fill="var(--text-muted)" font-size="9" text-anchor="end">${lastLabel}</text>
            </svg>
        `;
    }

    // =========================================================================
    // 8. Model Capacity Planner (Autofit Sizing Form)
    // =========================================================================
    async function evaluateAutoFitForm() {
        const model = document.getElementById("autofit-model-select")?.value || "meta-llama/Llama-3.2-3B-Instruct";
        const context = document.getElementById("autofit-context-select")?.value || 8192;
        const concurrency = document.getElementById("autofit-concurrency-select")?.value || 4;

        const btn = document.getElementById("btn-eval-autofit");
        if (btn) {
            btn.disabled = true;
            btn.textContent = "Calculating…";
        }

        try {
            const data = await fetchAPI(`/api/autofit?model_id=${encodeURIComponent(model)}&context_window=${context}&concurrency=${concurrency}`);
            const box = document.getElementById("autofit-result-box");
            if (!box || !data || data.status !== "ok") return;

            box.hidden = false;

            // Status Badge
            const badge = document.getElementById("autofit-status-badge");
            if (badge) {
                badge.textContent = data.fit_status;
                badge.className = "autofit-badge";
                if (data.fit_status === "FITS") badge.classList.add("badge-fits");
                else if (data.fit_status === "FITS_WITH_QUANT") badge.classList.add("badge-quant");
                else badge.classList.add("badge-oom");
            }

            const nameEl = document.getElementById("autofit-model-name");
            if (nameEl) nameEl.textContent = data.model_id;

            // Budgets & Dynamic labels
            const mb = data.memory_budget || {};
            const hw = data.hardware_evaluated || {};
            const isCpu = (hw.total_gpus === 0 || hw.mode === "CPU-Only");
            const vendor = hw.vendor || (isCpu ? "CPU" : "Generic");
            const memType = mb.memory_type || (isCpu ? "host_ram" : "vram");

            const reserveLabel = document.getElementById("budget-reserve-label");
            if (reserveLabel) {
                if (isCpu) reserveLabel.textContent = "Host RAM Reserve";
                else if (vendor === "AMD") reserveLabel.textContent = "ROCm Reserve";
                else if (vendor === "NVIDIA") reserveLabel.textContent = "CUDA Reserve";
                else reserveLabel.textContent = "Runtime Reserve";
            }

            const totalLabel = document.getElementById("budget-total-label");
            if (totalLabel) {
                if (isCpu) totalLabel.textContent = "Required Host RAM";
                else if (memType === "unified") totalLabel.textContent = "Required Usable Memory";
                else totalLabel.textContent = "Required VRAM";
            }

            const setVal = (id, val) => {
                const el = document.getElementById(id);
                if (el) el.textContent = `${val} GB`;
            };

            setVal("budget-weights-unquant", mb.weights_unquantized_gb || 0);
            setVal("budget-weights-quant", mb.weights_quantized_gb || 0);
            setVal("budget-kv-cache", mb.kv_cache_gb || 0);
            setVal("budget-cuda-reserve", mb.runtime_overhead_gb !== undefined ? mb.runtime_overhead_gb : (mb.cuda_overhead_gb || 0));
            setVal("budget-total-req", mb.total_required_vram_gb || 0);
            setVal("budget-headroom", mb.headroom_vram_gb || 0);

            const selectedWeightsLabel = document.getElementById("budget-selected-weights-label");
            if (selectedWeightsLabel) {
                const quantization = String(data.recommendation?.optimal_quantization || "none").toLowerCase();
                selectedWeightsLabel.textContent = quantization === "none"
                    ? "Selected Weight Estimate (No Quantization)"
                    : `Selected Weight Estimate (${quantization.toUpperCase()})`;
            }

            // Advice
            const adviceEl = document.getElementById("autofit-advice-text");
            if (adviceEl) adviceEl.textContent = data.scaling_advice || "--";

            // Prerequisites & Deployment Plan
            const prereqBox = document.getElementById("autofit-prerequisites-box");
            const prereqList = document.getElementById("autofit-prerequisites-list");
            const cmdLabel = document.getElementById("autofit-cmd-label");
            const cmdEl = document.getElementById("autofit-cmd-code");

            const engineName = data.deployment_plan?.engine || data.recommendation?.launch_config?.engine || "";
            if (cmdLabel) {
                cmdLabel.textContent = engineName ? `Validated ${String(engineName).toUpperCase()} Command:` : "Validated Launch Command:";
            }

            const prerequisites = data.missing_prerequisites || [];
            if (prereqBox && prereqList) {
                prereqList.innerHTML = prerequisites.map(p => `<li>${escapeHTML(p)}</li>`).join("");
                prereqBox.hidden = prerequisites.length === 0;
            }

            if (cmdEl) {
                cmdEl.textContent = data.deployment_plan?.deployment_command || "No validated launch command available.";
            }

        } catch (err) {
            console.error("Autofit evaluation failed:", err);
            Toast.error("Capacity Estimation Failed", err.message || "Failed to calculate model capacity.");
        } finally {
            if (btn) {
                btn.disabled = false;
                btn.textContent = "Estimate Capacity";
            }
        }
    }

    // =========================================================================
    // 9. Page Initialization & Event Listeners
    // =========================================================================
    function initObservatory() {
        // Enforce Admin Authentication
        if (!Auth.hasAdminAccess()) {
            Toast.error("Access Denied", "Observatory APM requires Administrator access.");
            setTimeout(() => { window.location.href = '/enterprise/login'; }, 600);
            return;
        }

        // 1. Time range button clicks
        const rangeBtns = document.querySelectorAll(".obs-range-btn");
        rangeBtns.forEach(btn => {
            btn.addEventListener("click", () => {
                rangeBtns.forEach(b => b.classList.remove("active"));
                btn.classList.add("active");
                currentObsRange = btn.dataset.range;
                fetchObservatoryData(currentObsRange);
            });
        });

        // 2. Sync Status button
        const syncBtn = document.getElementById("obs-btn-sync");
        if (syncBtn) {
            syncBtn.addEventListener("click", async () => {
                syncBtn.disabled = true;
                syncBtn.textContent = "Syncing…";
                try {
                    await fetchHostInfo(true);
                    await fetchObservatoryData();
                    Toast.success("Synced", "Host hardware and APM telemetry refreshed.");
                } finally {
                    syncBtn.disabled = false;
                    syncBtn.textContent = "🔄 Sync Status";
                }
            });
        }

        // 3. Reset APM button
        const resetBtn = document.getElementById("obs-btn-reset");
        if (resetBtn) {
            resetBtn.addEventListener("click", async () => {
                const proceed = await Modal.confirm(
                    "Reset Observatory APM?",
                    "This will clear in-memory request counters and latency percentiles.",
                    "Yes, Reset APM"
                );
                if (!proceed) return;

                resetBtn.disabled = true;
                resetBtn.textContent = "Resetting…";
                try {
                    await fetchAPI("/api/observatory/reset", { method: "POST" });
                    clearObservatoryError();
                    await fetchObservatoryData();
                    Toast.success("Reset Complete", "Observatory metrics successfully reset.");
                } catch (err) {
                    Toast.error("Reset Failed", err.message);
                } finally {
                    resetBtn.disabled = false;
                    resetBtn.textContent = "⚠️ Reset APM";
                }
            });
        }

        // 4. Error Banner Dismiss & Retry
        const dismissBtn = document.getElementById("obs-error-dismiss");
        if (dismissBtn) dismissBtn.addEventListener("click", clearObservatoryError);

        const retryBtn = document.getElementById("obs-error-retry");
        if (retryBtn) retryBtn.addEventListener("click", () => fetchObservatoryData());

        // 5. Capacity Planner button
        const evalBtn = document.getElementById("btn-eval-autofit");
        if (evalBtn) evalBtn.addEventListener("click", evaluateAutoFitForm);

        // Initial Data Fetch
        fetchHostInfo();
        fetchObservatoryData();

        // 6. Background Polling (5s interval, suspended on hidden tab)
        if (!obsPollTimer) {
            obsPollTimer = setInterval(() => {
                if (document.visibilityState !== "visible") return;
                fetchObservatoryData();
            }, 5000);
        }

        document.addEventListener("visibilitychange", () => {
            if (document.visibilityState === "visible") {
                fetchObservatoryData();
            }
        });
    }

    // Auto-run on DOM Ready
    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", initObservatory);
    } else {
        initObservatory();
    }

})(window, document);
