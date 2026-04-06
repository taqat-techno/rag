/**
 * RAG Tools — Semantic Map
 * Interactive 2D canvas visualization of the knowledge base.
 * Vanilla JS, no dependencies.
 */

// --- State ---
let canvas, ctx;
let points = [];
let projects = {};
let transform = { x: 0, y: 0, scale: 1 };
let hoveredPoint = null;
let selectedPoint = null;
let isDragging = false;
let dragStart = { x: 0, y: 0 };
let transformStart = { x: 0, y: 0 };
let canvasW = 0, canvasH = 0;

const PALETTE = [
    '#9B4DCA', '#6366F1', '#0D9488', '#D97706',
    '#E11D48', '#2563EB', '#7C3AED', '#059669',
];

const MIN_SCALE = 0.3;
const MAX_SCALE = 6;
const HIT_RADIUS = 20;
const BASE_RADIUS = 6;
const MAX_RADIUS = 18;

// --- Init ---

async function init() {
    canvas = document.getElementById('map-canvas');
    ctx = canvas.getContext('2d');

    setStatus('Loading map data...');
    try {
        const resp = await fetch('/api/map/points');
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = await resp.json();
        points = data.points || [];
    } catch (e) {
        setStatus('Failed to load map data. <button onclick="init()" class="btn btn-secondary btn-sm" style="margin-left:8px">Retry</button>', true);
        return;
    }

    if (points.length === 0) {
        setStatus('No indexed files. <a href="/index" style="color:var(--color-accent)">Run an index</a> to populate the map.');
        return;
    }

    buildProjectColors();
    buildLegend();
    resizeCanvas();
    centerView();
    render();
    setStatus(`${points.length} files across ${Object.keys(projects).length} projects`);

    canvas.addEventListener('mousemove', onMouseMove);
    canvas.addEventListener('mousedown', onMouseDown);
    canvas.addEventListener('mouseup', onMouseUp);
    canvas.addEventListener('mouseleave', onMouseLeave);
    canvas.addEventListener('click', onClick);
    canvas.addEventListener('wheel', onWheel, { passive: false });
    window.addEventListener('resize', () => { resizeCanvas(); render(); });
}

// --- Canvas Setup ---

function resizeCanvas() {
    const container = document.getElementById('map-container');
    const dpr = window.devicePixelRatio || 1;
    const rect = container.getBoundingClientRect();
    canvasW = rect.width;
    canvasH = rect.height;
    canvas.width = canvasW * dpr;
    canvas.height = canvasH * dpr;
    canvas.style.width = canvasW + 'px';
    canvas.style.height = canvasH + 'px';
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
}

function centerView() {
    const pad = 60;
    transform.x = pad;
    transform.y = pad;
    transform.scale = 1;
}

// --- Project Colors ---

function buildProjectColors() {
    const pids = [...new Set(points.map(p => p.project_id))].sort();
    projects = {};
    pids.forEach((pid, i) => {
        projects[pid] = {
            color: PALETTE[i % PALETTE.length],
            count: points.filter(p => p.project_id === pid).length,
        };
    });
}

// --- Legend ---

function buildLegend() {
    const el = document.getElementById('map-legend');
    if (!el) return;
    let html = '';
    for (const [pid, info] of Object.entries(projects)) {
        html += `<div class="legend-item">
            <span class="legend-dot" style="background:${info.color}"></span>
            <span>${esc(pid)}</span>
            <span class="legend-count">${info.count}</span>
        </div>`;
    }
    el.innerHTML = html;
}

// --- Coordinate Transform ---

function worldToScreen(wx, wy) {
    const drawW = canvasW - 120;
    const drawH = canvasH - 120;
    return {
        x: wx * drawW * transform.scale + transform.x,
        y: (1 - wy) * drawH * transform.scale + transform.y,
    };
}

function screenToWorld(sx, sy) {
    const drawW = canvasW - 120;
    const drawH = canvasH - 120;
    return {
        x: (sx - transform.x) / (drawW * transform.scale),
        y: 1 - (sy - transform.y) / (drawH * transform.scale),
    };
}

// --- Render ---

function render() {
    ctx.clearRect(0, 0, canvasW, canvasH);
    drawGrid();

    // Draw points (selected last so it's on top)
    for (const p of points) {
        if (p === selectedPoint || p === hoveredPoint) continue;
        drawPoint(p, false, false);
    }
    if (hoveredPoint && hoveredPoint !== selectedPoint) drawPoint(hoveredPoint, true, false);
    if (selectedPoint) drawPoint(selectedPoint, selectedPoint === hoveredPoint, true);
}

function drawGrid() {
    const step = 80 * transform.scale;
    if (step < 20) return;

    ctx.strokeStyle = 'rgba(229, 224, 240, 0.3)';
    ctx.lineWidth = 0.5;
    ctx.beginPath();

    const startX = transform.x % step;
    for (let x = startX; x < canvasW; x += step) {
        ctx.moveTo(x, 0);
        ctx.lineTo(x, canvasH);
    }
    const startY = transform.y % step;
    for (let y = startY; y < canvasH; y += step) {
        ctx.moveTo(0, y);
        ctx.lineTo(canvasW, y);
    }
    ctx.stroke();
}

function drawPoint(p, isHovered, isSelected) {
    const s = worldToScreen(p.x, p.y);
    const color = projects[p.project_id]?.color || '#9B4DCA';
    let r = getRadius(p.chunk_count);

    // Glow for hovered
    if (isHovered) {
        r += 3;
        ctx.shadowColor = color + '55';
        ctx.shadowBlur = 12;
    }

    // Fill circle
    ctx.beginPath();
    ctx.arc(s.x, s.y, r, 0, Math.PI * 2);
    ctx.fillStyle = color + 'CC';
    ctx.fill();

    // Stroke
    ctx.shadowColor = 'transparent';
    ctx.shadowBlur = 0;
    ctx.strokeStyle = color;
    ctx.lineWidth = 1.5;
    ctx.stroke();

    // Selected ring
    if (isSelected) {
        ctx.beginPath();
        ctx.arc(s.x, s.y, r + 4, 0, Math.PI * 2);
        ctx.strokeStyle = '#FFFFFF';
        ctx.lineWidth = 2.5;
        ctx.stroke();
        ctx.strokeStyle = color;
        ctx.lineWidth = 1;
        ctx.stroke();
    }
}

function getRadius(chunkCount) {
    return Math.min(BASE_RADIUS + Math.sqrt(chunkCount) * 1.5, MAX_RADIUS);
}

// --- Hit Testing ---

function hitTest(sx, sy) {
    let best = null;
    let bestDist = HIT_RADIUS;
    for (const p of points) {
        const s = worldToScreen(p.x, p.y);
        const dx = s.x - sx;
        const dy = s.y - sy;
        const dist = Math.sqrt(dx * dx + dy * dy);
        if (dist < bestDist) {
            bestDist = dist;
            best = p;
        }
    }
    return best;
}

// --- Mouse Events ---

function onMouseMove(e) {
    const rect = canvas.getBoundingClientRect();
    const mx = e.clientX - rect.left;
    const my = e.clientY - rect.top;

    if (isDragging) {
        transform.x = transformStart.x + (e.clientX - dragStart.x);
        transform.y = transformStart.y + (e.clientY - dragStart.y);
        render();
        return;
    }

    const prev = hoveredPoint;
    hoveredPoint = hitTest(mx, my);
    canvas.style.cursor = hoveredPoint ? 'pointer' : 'grab';

    if (hoveredPoint !== prev) {
        render();
        if (hoveredPoint) showTooltip(hoveredPoint, e.clientX, e.clientY);
        else hideTooltip();
    } else if (hoveredPoint) {
        moveTooltip(e.clientX, e.clientY);
    }
}

function onMouseDown(e) {
    if (e.button !== 0) return;
    isDragging = true;
    dragStart = { x: e.clientX, y: e.clientY };
    transformStart = { x: transform.x, y: transform.y };
    canvas.style.cursor = 'grabbing';
}

function onMouseUp(e) {
    isDragging = false;
    canvas.style.cursor = hoveredPoint ? 'pointer' : 'grab';
}

function onMouseLeave() {
    isDragging = false;
    hoveredPoint = null;
    hideTooltip();
    render();
}

function onClick(e) {
    // Ignore if we just dragged
    const dx = Math.abs(e.clientX - dragStart.x);
    const dy = Math.abs(e.clientY - dragStart.y);
    if (dx > 4 || dy > 4) return;

    const rect = canvas.getBoundingClientRect();
    const hit = hitTest(e.clientX - rect.left, e.clientY - rect.top);

    if (hit) {
        selectedPoint = hit;
        showDetail(hit);
    } else {
        selectedPoint = null;
        hideDetail();
    }
    render();
}

function onWheel(e) {
    e.preventDefault();
    const rect = canvas.getBoundingClientRect();
    const mx = e.clientX - rect.left;
    const my = e.clientY - rect.top;

    const factor = e.deltaY < 0 ? 1.1 : 1 / 1.1;
    const newScale = Math.max(MIN_SCALE, Math.min(MAX_SCALE, transform.scale * factor));

    // Zoom toward cursor
    const ratio = newScale / transform.scale;
    transform.x = mx - ratio * (mx - transform.x);
    transform.y = my - ratio * (my - transform.y);
    transform.scale = newScale;

    render();
}

// --- Tooltip ---

function showTooltip(p, cx, cy) {
    const el = document.getElementById('map-tooltip');
    if (!el) return;
    const fname = p.file_path.split('/').pop();
    el.innerHTML = `
        <div class="tt-name">${esc(fname)}</div>
        <div class="tt-meta">${esc(p.project_id)} &middot; ${p.chunk_count} chunks</div>
    `;
    el.style.display = 'block';
    moveTooltip(cx, cy);
}

function moveTooltip(cx, cy) {
    const el = document.getElementById('map-tooltip');
    if (!el) return;
    el.style.left = (cx + 14) + 'px';
    el.style.top = (cy + 14) + 'px';
}

function hideTooltip() {
    const el = document.getElementById('map-tooltip');
    if (el) el.style.display = 'none';
}

// --- Detail Panel ---

function showDetail(p) {
    const el = document.getElementById('map-detail');
    if (!el) return;

    const fname = p.file_path.split('/').pop();
    const headings = (p.headings || []).map(h => `<li>${esc(h)}</li>`).join('');

    el.innerHTML = `
        <button class="detail-close" onclick="hideDetail(); selectedPoint=null; render();">&times;</button>
        <h3 class="detail-title">${esc(fname)}</h3>
        <div class="detail-path">${esc(p.file_path)}</div>
        <div class="detail-project">
            <span class="legend-dot" style="background:${projects[p.project_id]?.color || '#9B4DCA'}"></span>
            ${esc(p.project_id)}
        </div>
        <div class="detail-stats">
            <span>${p.chunk_count} chunks</span>
        </div>
        ${headings ? `<div class="detail-headings"><h4>Headings</h4><ul>${headings}</ul></div>` : ''}
        <a href="/search?query=${encodeURIComponent(fname)}" class="btn btn-secondary btn-sm" style="margin-top:16px;">Search this file</a>
    `;
    el.classList.add('open');
}

function hideDetail() {
    const el = document.getElementById('map-detail');
    if (el) el.classList.remove('open');
}

// --- Status ---

function setStatus(html, isError) {
    const el = document.getElementById('map-status');
    if (el) {
        el.innerHTML = html;
        el.style.color = isError ? 'var(--color-danger)' : '';
    }
}

// --- Util ---

function esc(s) {
    const d = document.createElement('div');
    d.textContent = s;
    return d.innerHTML;
}

// --- Start ---

document.addEventListener('DOMContentLoaded', init);
