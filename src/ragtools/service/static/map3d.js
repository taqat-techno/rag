/**
 * RAG Tools — 3D Semantic Map (ECharts GL)
 * Provides a 3D scatter plot view of the knowledge base.
 * Activated when user toggles to 3D mode on the map page.
 */

let chart3d = null;
let points3d = [];
let projects3d = {};

const PALETTE_3D = [
    '#9B4DCA', '#6366F1', '#0D9488', '#D97706',
    '#E11D48', '#2563EB', '#7C3AED', '#059669',
];

function init3D(pointsData) {
    points3d = pointsData || [];
    if (!points3d.length) return;

    buildProjects3d();

    const container = document.getElementById('map-3d');
    if (!container) return;

    chart3d = echarts.init(container, null, { renderer: 'canvas' });

    const series = buildSeries3d();
    const option = {
        backgroundColor: 'transparent',
        tooltip: {
            formatter: function (params) {
                const d = params.data;
                if (!d || !d.meta) return '';
                const fname = d.meta.file_path.split('/').pop();
                return '<div style="font-weight:600;font-size:13px">' + escHtml(fname) + '</div>'
                     + '<div style="font-size:11.5px;color:#888;margin-top:2px">'
                     + escHtml(d.meta.project_id) + ' &middot; ' + d.meta.chunk_count + ' chunks</div>';
            },
            borderColor: 'rgba(45,27,105,0.12)',
            backgroundColor: getComputedStyle(document.documentElement).getPropertyValue('--color-surface').trim() || '#fff',
            textStyle: { color: '#2D1B69' },
        },
        legend: {
            data: Object.keys(projects3d),
            top: 12,
            right: 12,
            orient: 'vertical',
            textStyle: { color: '#555', fontSize: 12 },
            itemWidth: 10,
            itemHeight: 10,
            borderRadius: 5,
            backgroundColor: getComputedStyle(document.documentElement).getPropertyValue('--color-surface').trim() || '#fff',
            padding: [10, 14],
            borderColor: 'rgba(45,27,105,0.1)',
            borderWidth: 1,
        },
        xAxis3D: {
            type: 'value', min: 0, max: 1,
            axisLabel: { show: false, textStyle: { fontSize: 0 } },
            axisTick: { show: false },
            axisPointer: { show: false },
            splitLine: { lineStyle: { color: 'rgba(229,224,240,0.25)' } },
            axisLine: { lineStyle: { color: 'rgba(45,27,105,0.12)' } },
            name: '',
        },
        yAxis3D: {
            type: 'value', min: 0, max: 1,
            axisLabel: { show: false, textStyle: { fontSize: 0 } },
            axisTick: { show: false },
            axisPointer: { show: false },
            splitLine: { lineStyle: { color: 'rgba(229,224,240,0.25)' } },
            axisLine: { lineStyle: { color: 'rgba(45,27,105,0.12)' } },
            name: '',
        },
        zAxis3D: {
            type: 'value', min: 0, max: 1,
            axisLabel: { show: false, textStyle: { fontSize: 0 } },
            axisTick: { show: false },
            axisPointer: { show: false },
            splitLine: { lineStyle: { color: 'rgba(229,224,240,0.25)' } },
            axisLine: { lineStyle: { color: 'rgba(45,27,105,0.12)' } },
            name: '',
        },
        grid3D: {
            boxWidth: 100,
            boxHeight: 100,
            boxDepth: 100,
            viewControl: {
                autoRotate: false,
                autoRotateSpeed: 4,
                distance: 220,
                alpha: 25,
                beta: 35,
                minDistance: 80,
                maxDistance: 500,
            },
            light: {
                main: { intensity: 1.2, shadow: false },
                ambient: { intensity: 0.5 },
            },
            environment: 'none',
            postEffect: { enable: false },
        },
        series: series,
    };

    chart3d.setOption(option);

    // Click handler: show detail panel
    chart3d.on('click', function (params) {
        if (params.data && params.data.meta) {
            const p = params.data.meta;
            // Reuse the existing 2D detail panel
            if (typeof showDetail === 'function') {
                showDetail(p);
            }
        }
    });

    // Handle resize
    const resizeObs = new ResizeObserver(() => chart3d && chart3d.resize());
    resizeObs.observe(container);
}

function buildProjects3d() {
    const pids = [...new Set(points3d.map(p => p.project_id))].sort();
    projects3d = {};
    pids.forEach((pid, i) => {
        projects3d[pid] = {
            color: PALETTE_3D[i % PALETTE_3D.length],
            count: points3d.filter(p => p.project_id === pid).length,
        };
    });
}

function buildSeries3d() {
    const series = [];
    for (const [pid, info] of Object.entries(projects3d)) {
        const data = points3d
            .filter(p => p.project_id === pid)
            .map(p => ({
                value: [p.x, p.y, p.z || 0.5],
                meta: p,
                symbolSize: Math.min(6 + Math.sqrt(p.chunk_count) * 2, 22),
            }));

        series.push({
            type: 'scatter3D',
            name: pid,
            data: data,
            symbolSize: function (val, params) {
                return params.data.symbolSize || 8;
            },
            itemStyle: {
                color: info.color,
                opacity: 0.85,
                borderColor: info.color,
                borderWidth: 0.5,
            },
            label: { show: false },
            emphasis: {
                label: { show: false },
                itemStyle: {
                    opacity: 1,
                    borderColor: '#fff',
                    borderWidth: 2,
                },
            },
        });
    }
    return series;
}

function toggleAutoRotate() {
    if (!chart3d) return;
    const opt = chart3d.getOption();
    const current = opt.grid3D[0].viewControl.autoRotate;
    chart3d.setOption({
        grid3D: { viewControl: { autoRotate: !current } }
    });
    const btn = document.getElementById('btn-rotate');
    if (btn) btn.textContent = current ? 'Auto-rotate' : 'Stop rotation';
}

function dispose3D() {
    if (chart3d) {
        chart3d.dispose();
        chart3d = null;
    }
}

function escHtml(s) {
    const d = document.createElement('div');
    d.textContent = s;
    return d.innerHTML;
}
