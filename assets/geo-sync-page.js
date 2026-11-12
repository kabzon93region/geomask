'use strict';
'require view';
'require rpc';
'require ui';

// >>> geo-rpc generated:decl (tools/geo-rpc.json) >>>
var callStatus                = rpc.declare({ object: 'geo-sync', method: 'status', expect: {} });
var callLeases                = rpc.declare({ object: 'geo-sync', method: 'leases', expect: {} });
var callLog                   = rpc.declare({ object: 'geo-sync', method: 'log', expect: {} });
var callBackups               = rpc.declare({ object: 'geo-sync', method: 'backups', expect: {} });
var callStack                 = rpc.declare({ object: 'geo-sync', method: 'stack', expect: {} });
var callNodescan              = rpc.declare({ object: 'geo-sync', method: 'nodescan', expect: {} });
var callNodesGet              = rpc.declare({ object: 'geo-sync', method: 'nodes_get', expect: {} });
var callNodesPathsGet         = rpc.declare({ object: 'geo-sync', method: 'nodes_paths_get', expect: {} });
/* params as OBJECT (not array): array form expects positional args and would
   wrap {mode:...} into {"mode":{...}} breaking rpcd json_get_var */
var callNodesPathSave         = rpc.declare({ object: 'geo-sync', method: 'nodes_path_save', params: [ 'path_id', 'countries', 'expected_revision' ], expect: {} });
var callNodesPathCheck        = rpc.declare({ object: 'geo-sync', method: 'nodes_path_check', params: [ 'path_id' ], expect: {} });
var callNodesAuditEventsGet   = rpc.declare({ object: 'geo-sync', method: 'nodes_audit_events_get', params: [ 'path_id', 'before', 'limit' ], expect: {} });
var callNodesAuditJobGet      = rpc.declare({ object: 'geo-sync', method: 'nodes_audit_job_get', params: [ 'job_id' ], expect: {} });
var callNodesAuditCancel      = rpc.declare({ object: 'geo-sync', method: 'nodes_audit_cancel', params: [ 'job_id' ], expect: {} });
var callNodesSet              = rpc.declare({ object: 'geo-sync', method: 'nodes_set', params: { enabled: null, schedule: null, countries: null, asns: null, base: null, provider: null }, expect: {} });
var callSyncSet               = rpc.declare({ object: 'geo-sync', method: 'sync_set', params: { interval: null }, expect: {} });
var callSyncdSet              = rpc.declare({ object: 'geo-sync', method: 'syncd_set', params: { state: null }, expect: {} });
var callPinSet                = rpc.declare({ object: 'geo-sync', method: 'pin_set', params: { state: null }, expect: {} });
var callListsGet              = rpc.declare({ object: 'geo-sync', method: 'lists_get', expect: {} });
var callListsSeed             = rpc.declare({ object: 'geo-sync', method: 'lists_seed', expect: {} });
var callYtGet                 = rpc.declare({ object: 'geo-sync', method: 'yt_get', expect: {} });
var callYtMode                = rpc.declare({ object: 'geo-sync', method: 'yt_mode', params: { mode: null }, expect: {} });
var callYtReset               = rpc.declare({ object: 'geo-sync', method: 'yt_reset', expect: {} });
var callTorGet                = rpc.declare({ object: 'geo-sync', method: 'torrent_get', expect: {} });
var callTorRefresh            = rpc.declare({ object: 'geo-sync', method: 'torrent_refresh', expect: {} });
var callTorListener           = rpc.declare({ object: 'geo-sync', method: 'torrent_listener', params: { state: null }, expect: {} });
var callTorExtra              = rpc.declare({ object: 'geo-sync', method: 'torrent_extra', params: { hosts: null }, expect: {} });
var callLists                 = rpc.declare({ object: 'geo-sync', method: 'sync_lists', expect: {} });
var callGoogle                = rpc.declare({ object: 'geo-sync', method: 'google', expect: {} });
var callGlobal                = rpc.declare({ object: 'geo-sync', method: 'global', params: { mode: null }, expect: {} });
var callIpv6                  = rpc.declare({ object: 'geo-sync', method: 'ipv6', params: { state: null }, expect: {} });
var callDevAdd                = rpc.declare({ object: 'geo-sync', method: 'dev_add', params: { ip: null, mode: null, mac: null, name: null }, expect: {} });
var callDevDel                = rpc.declare({ object: 'geo-sync', method: 'dev_del', params: { ip: null }, expect: {} });
var callAiSubGet              = rpc.declare({ object: 'geo-sync', method: 'ai_sub_get', expect: {} });
var callAiSubSet              = rpc.declare({ object: 'geo-sync', method: 'ai_sub_set', params: { url: null }, expect: {} });
var callSubsList              = rpc.declare({ object: 'geo-sync', method: 'subs_list', expect: {} });
var callSubsSeed              = rpc.declare({ object: 'geo-sync', method: 'subs_seed', expect: {} });
var callSubAdd                = rpc.declare({ object: 'geo-sync', method: 'sub_add', params: { name: null, url: null }, expect: {} });
var callSubDel                = rpc.declare({ object: 'geo-sync', method: 'sub_del', params: { id: null }, expect: {} });
var callAiSubUse              = rpc.declare({ object: 'geo-sync', method: 'ai_sub_use', params: { id: null }, expect: {} });
var callVpnSubUse             = rpc.declare({ object: 'geo-sync', method: 'vpn_sub_use', params: { id: null }, expect: {} });
var callVpnnpSubUse           = rpc.declare({ object: 'geo-sync', method: 'vpnnp_sub_use', params: { id: null }, expect: {} });
var callModList               = rpc.declare({ object: 'geo-sync', method: 'mod_list', expect: {} });
var callModCheck              = rpc.declare({ object: 'geo-sync', method: 'mod_check', expect: {} });
var callModUpdate             = rpc.declare({ object: 'geo-sync', method: 'mod_update', params: [ 'module_id', 'force' ], expect: {} });
var callDiagList              = rpc.declare({ object: 'geo-sync', method: 'diag_list', expect: {} });
var callDiagRun               = rpc.declare({ object: 'geo-sync', method: 'diag_run', params: [ 'check' ], expect: {} });
var callDiagLog               = rpc.declare({ object: 'geo-sync', method: 'diag_log', params: [ 'check' ], expect: {} });
var callDiagDomain            = rpc.declare({ object: 'geo-sync', method: 'diag_domain', params: [ 'domain', 'live' ], expect: {} });
var callUpdCheck              = rpc.declare({ object: 'geo-sync', method: 'upd_check', params: { force: null }, expect: {} });
var callUpdStatus             = rpc.declare({ object: 'geo-sync', method: 'upd_status', expect: {} });
var callUpdApply              = rpc.declare({ object: 'geo-sync', method: 'upd_apply', expect: {} });
var callNodesPathPreview      = rpc.declare({ object: 'geo-sync', method: 'nodes_path_preview', params: [ 'path_id', 'report_id', 'expected_path_revision', 'expected_source_revision' ], expect: {} });
var callNodesPathApply        = rpc.declare({ object: 'geo-sync', method: 'nodes_path_apply', params: [ 'path_id', 'preview_id', 'expected_path_revision' ], expect: {} });
var callNodesPathRevoke       = rpc.declare({ object: 'geo-sync', method: 'nodes_path_revoke', params: [ 'path_id' ], expect: {} });
var callNodesPathApplyStatus  = rpc.declare({ object: 'geo-sync', method: 'nodes_path_apply_status', params: [ 'path_id' ], expect: {} });
var callMtrickleSync          = rpc.declare({ object: 'geo-sync', method: 'mtrickle_sync', expect: {} });
var callNodesAuditScheduleGet = rpc.declare({ object: 'geo-sync', method: 'nodes_audit_schedule_get', params: [ 'path_id' ], expect: {} });
var callNodesAuditScheduleSet = rpc.declare({ object: 'geo-sync', method: 'nodes_audit_schedule_set', params: [ 'path_id', 'enabled', 'time', 'mode' ], expect: {} });
// <<< geo-rpc generated:decl <<<
var PANEL_VERSION = '@@PANEL_VERSION@@';

/* Job observers, keyed by path id. They live outside the view because reload() builds a
   new instance while the old timer would otherwise keep polling. */
var POLL_TIMERS = {};
/* Copy text to clipboard over plain http (LuCI): execCommand fallback for non-secure context. */
function copyToClipboard(txt, btn) {
	function flash(ok) {
		if (!btn) return;
		var old = btn.textContent;
		btn.textContent = ok ? 'Скопировано ✓' : 'Не удалось';
		setTimeout(function () { btn.textContent = old; }, 1600);
	}
	function fallback() {
		var ta = document.createElement('textarea');
		ta.value = txt;
		ta.style.position = 'fixed'; ta.style.left = '-1000px';
		document.body.appendChild(ta); ta.focus(); ta.select();
		var ok = false;
		try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
		document.body.removeChild(ta);
		flash(ok);
	}
	if (window.isSecureContext && navigator.clipboard) {
		navigator.clipboard.writeText(txt).then(function () { flash(true); }, fallback);
	} else {
		fallback();
	}
}

var CCNAME = {
	DE: 'Германия', NL: 'Нидерланды', FI: 'Финляндия', LV: 'Латвия', LT: 'Литва',
	EE: 'Эстония', PL: 'Польша', DK: 'Дания', SE: 'Швеция', NO: 'Норвегия',
	US: 'США', TR: 'Турция', AL: 'Албания', FR: 'Франция', IT: 'Италия',
	ES: 'Испания', AT: 'Австрия', CH: 'Швейцария', GB: 'Великобритания'
};

var STYLE = [
	'.gs-wrap{display:flex;flex-direction:column;gap:14px;max-width:960px}',
	'.gs-hero{background:linear-gradient(135deg,#0d419d22,#1f6feb11);border:1px solid #1f6feb55;border-radius:12px;padding:18px}',
	'.gs-hero h2{margin:0 0 4px;font-size:17px}',
	'.gs-hero .gs-sub{color:#8b949e;font-size:13px}',
	'.gs-duo{display:flex;gap:12px;flex-wrap:wrap;margin-top:14px}',
	'.gs-sub-card{flex:1;min-width:280px;background:var(--bg-color,#161b22);border:1px solid var(--border-color,#30363d);border-radius:10px;padding:14px 16px}',
	'.gs-sub-card .gs-ent{font-size:11px;text-transform:uppercase;letter-spacing:.5px;color:#8b949e}',
	'.gs-sub-card .gs-node{font-size:15px;font-weight:600;margin:6px 0 2px}',
	'.gs-sub-card .gs-meta{font-size:12px;color:#8b949e}',
	'.gs-cc{display:inline-block;min-width:30px;text-align:center;font-size:11px;font-weight:700;letter-spacing:.4px;color:#58a6ff;background:#1f6feb22;border:1px solid #1f6feb55;border-radius:6px;padding:2px 7px;margin-right:8px;vertical-align:1px}',
	'.gs-counters{display:flex;gap:12px;flex-wrap:wrap;margin-top:12px}',
	'.gs-counter{flex:1;min-width:150px;background:#0d1117;border:1px solid #30363d;border-radius:9px;padding:12px 14px}',
	'.gs-counter .gs-num{font-size:26px;font-weight:700;line-height:1.1}',
	'.gs-counter .gs-cap{font-size:12px;color:#8b949e;margin-top:3px}',
	'.gs-card{background:var(--bg-color,#161b22);border:1px solid var(--border-color,#30363d);border-radius:10px;padding:16px}',
	'.gs-card h3{margin:0 0 10px;font-size:15px}',
	'.gs-row{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:7px 0;flex-wrap:wrap}',
	'.gs-label{color:var(--text-color,#c9d1d9)}',
	'.gs-val{font-weight:600}',
	'.gs-badge{display:inline-block;padding:2px 9px;border-radius:20px;font-size:12px;font-weight:600}',
	'.gs-on{background:#1f6feb33;color:#58a6ff;border:1px solid #1f6feb66}',
	'.gs-ok{background:#23863633;color:#3fb950;border:1px solid #23863666}',
	'.gs-off{background:#f8514922;color:#f85149;border:1px solid #f8514966}',
	'.gs-warn{background:#d2992222;color:#d29922;border:1px solid #d2992266}',
	'.gs-seg{display:inline-flex;border:1px solid #30363d;border-radius:8px;overflow:hidden}',
	'.gs-seg div{padding:5px 14px;cursor:pointer;font-size:13px}',
	'.gs-seg .gs-active{background:#1f6feb;color:#fff}',
	'.gs-btn{padding:6px 16px;border-radius:7px;border:1px solid #30363d;background:#21262d;color:#c9d1d9;cursor:pointer;font-size:13px}',
	'.gs-btn:hover{background:#30363d}',
	'.gs-btn-primary{background:#1f6feb;border-color:#1f6feb;color:#fff}',
	'.gs-btn-primary:hover{background:#388bfd}',
	'.gs-btn-warn{border-color:#f8514966;color:#f85149}',
	'.gs-btn:disabled{opacity:.55;cursor:default}',
	'.gs-actions{display:flex;gap:10px;flex-wrap:wrap;margin-top:12px}',
	'.gs-log{background:#0d1117;color:#8b949e;font-family:monospace;font-size:12px;padding:10px;border-radius:7px;max-height:220px;overflow:auto;white-space:pre-wrap}',
	'.gs-table{width:100%;border-collapse:collapse;font-size:13px}',
	'.gs-table td,.gs-table th{padding:6px 8px;border-bottom:1px solid #30363d;text-align:left}',
	'.gs-subs{width:100%;table-layout:fixed}',
	'.gs-subs td{overflow-wrap:anywhere;vertical-align:middle}',
	'.gs-subs td:first-child{width:126px;font-weight:600}',
	'.gs-subs td:nth-child(2){font-family:monospace;font-size:12px;color:#8b949e}',
	'.gs-subs td:nth-child(3){width:104px}',
	'.gs-subs td:nth-child(3) .gs-badge{white-space:nowrap}',
	'.gs-subs td:last-child{width:272px;white-space:normal}',
	'.gs-subs .gs-btn-use{background:#21262d;border-color:#30363d;color:#c9d1d9;padding:5px 9px;margin:0 4px 4px 0}',
	'.gs-btn-vpn-on{background:#1f6feb;border-color:#58a6ff;color:#fff}',
	'.gs-btn-vpn-on:hover{background:#388bfd}',
	'.gs-btn-ai-on{background:#238636;border-color:#3fb950;color:#fff}',
	'.gs-btn-ai-on:hover{background:#2ea043}',
	'.gs-subs .gs-btn-use:hover{background:#30363d}',
	'.gs-subs .gs-btn-warn{padding:5px 10px}',
	'.gs-input{background:#0d1117;border:1px solid #30363d;color:#c9d1d9;border-radius:6px;padding:6px 9px;font-size:13px}',
	'.gs-hint{color:#8b949e;font-size:12px;margin-top:6px}',
	'.gs-out{background:#0d1117;color:#8b949e;font-family:monospace;font-size:12px;padding:9px 11px;border-radius:7px;margin-top:10px;white-space:pre-wrap;max-height:130px;overflow:auto}',
	'.gs-mono{font-family:monospace}',
	'.gs-upd-meta{color:#8b949e;font-size:12px}',
	'.gs-stack{display:flex;flex-wrap:wrap;gap:6px 16px;font-size:12px;color:#8b949e;margin-top:6px}',
	'.gs-stack span b{color:#c9d1d9;font-weight:600}',
	'.gs-upd-status{margin-top:8px;font-size:13px;min-height:18px}',
	'.gs-console{background:#0d1117;border:1px solid #30363d;border-radius:8px;padding:10px 12px;margin-top:8px;font-family:ui-monospace,Consolas,monospace;font-size:12px;line-height:1.5;max-height:300px;overflow-y:auto;white-space:pre-wrap;color:#c9d1d9}',
	'.gs-console .c-err{color:#f85149}',
	'.gs-console .c-done{color:#3fb950}',
	'.gs-console .c-run{color:#d29922}',
	'.gs-console .c-dim{color:#8b949e}',
	'.gs-log .c-err{color:#f85149}',
	'.gs-log .c-done{color:#3fb950}',
	'.gs-log .c-run{color:#d29922}',
	'.gs-log .c-dim{color:#8b949e}',
	'.gs-bk{margin-top:10px;border-top:1px solid #30363d;padding-top:6px}',
	'.gs-bk h4{margin:2px 0 4px;font-size:13px;color:#e6edf3}',
	'.gs-tabs{display:flex;gap:6px;flex-wrap:wrap;border-bottom:2px solid #1f6feb44;padding-bottom:0}',
	'.gs-tab{padding:7px 16px;border:1px solid #30363d;border-bottom:none;border-radius:9px 9px 0 0;background:#161b22;color:#8b949e;cursor:pointer;font-size:13px;font-weight:600}',
	'.gs-tab:hover{background:#1c2129}',
	'.gs-tab.gs-tab-active{background:#0d419d33;color:#58a6ff;border-color:#1f6feb88}',
	'.gs-panels{display:flex;flex-direction:column;gap:14px;padding-top:14px}',
	'.gs-panel{display:flex;flex-direction:column;gap:14px}',
	'.gs-check{display:inline-flex;align-items:center;gap:8px;cursor:pointer;font-size:13px}',
	'.gs-text{background:#0d1117;border:1px solid #30363d;color:#c9d1d9;border-radius:6px;padding:6px 9px;font-size:13px;font-family:monospace}',
	'.gs-blocklist{background:#0d1117;border:1px solid #30363d;border-radius:7px;padding:8px 11px;font-family:monospace;font-size:12px;color:#8b949e;max-height:180px;overflow:auto;white-space:pre-wrap;margin-top:6px}'
].join('\n');

function injectStyle() {
	if (document.getElementById('gs-style')) return;
	var s = document.createElement('style');
	s.id = 'gs-style';
	s.textContent = STYLE;
	document.head.appendChild(s);
}

function badge(on, yes, no) {
	return E('span', { 'class': 'gs-badge ' + (on ? 'gs-on' : 'gs-off') }, on ? yes : no);
}

function badgeKlass(on, okCls, offCls, yes, no) {
	return E('span', { 'class': 'gs-badge ' + (on ? okCls : offCls) }, on ? yes : no);
}

/* Strip emoji from mihomo node names (flags, pictographs, VS16, ZWJ) - LuCI font
   renders them as tofu boxes, so we show clean text instead. */
function cleanName(s) {
	if (!s) return '';
	var out = String(s)
		.replace(/[\u{1F1E6}-\u{1F1FF}]/gu, '')   /* regional indicators (flag halves) */
		.replace(/[\u{1F300}-\u{1FAFF}]/gu, '')   /* pictographs: gamepad, rockets etc */
		.replace(/[\u{2600}-\u{27BF}]/gu, '')     /* misc symbols & dingbats */
		.replace(/[\u{FE0E}\u{FE0F}\u{200D}\u{200B}]/gu, '') /* VS15/VS16, ZWJ, ZWSP */
		.replace(/\s{2,}/g, ' ')
		.trim();
	return out || String(s).trim();
}

/* Country badge as plain text (code chip) - no flag emoji, renders everywhere. */
function flagOf(s) {
	/* the first flag emoji in a node name, e.g. "🇦🇱 Албания-1" -> "🇦🇱" */
	var m = String(s || '').match(/[\u{1F1E6}-\u{1F1FF}][\u{1F1E6}-\u{1F1FF}]/u);
	return m ? m[0] : '';
}

function ccBadge(cc, name) {
	/* cleanName() strips flags from the node text on purpose; the badge is where the flag
	   belongs, so it is taken from the raw name and kept here (owner, 31.10: "пропали банеры") */
	var f = flagOf(name);
	return E('span', { 'class': 'gs-cc' }, (f ? f + ' ' : '') + (cc || '—'));
}

var E = (function(raw) {
	return function() {
		var a = Array.prototype.slice.call(arguments), i = a.length - 1;
		if (i >= 1 && (typeof a[i] === 'string' || typeof a[i] === 'number')) a[i] = [ String(a[i]) ];
		return raw.apply(null, a);
	};
})(window.E);

return view.extend({
	/* v1.12.2: this panel saves via its own per-card buttons (rpc -> backend), no
	   UCI form here - hide the generic LuCI apply/save/reset footer so it cannot
	   be mistaken for the way to commit our fields. */
	handleSaveApply: null,
	handleSave: null,
	handleReset: null,

	load: function() {
		injectStyle();
		return Promise.all([ callStatus(), callLeases(), callLog(), callAiSubGet(), callSubsList(),
			callUpdCheck({ force: null }), callUpdStatus(), callBackups(), callStack(), callNodesGet(), callListsGet(), callYtGet(), callTorGet(), callNodesPathsGet(), callModList(), callDiagList() ]);
	},

	render: function(all) {
		var st = all[0] || {}, leases = (all[1] || {}).leases || [], lg = (all[2] || {}).log || '';
		var aicfg = all[3] || {};
		var subs = (all[4] || {}).subs || [];
		var upd = all[5] || {};
		var upds = all[6] || {};
		var bks = all[7] || {};
		var stk = all[8] || {};
		var nd = all[9] || {};
		var lt = all[10] || {};
		var yt = all[11] || {};
		var tor = all[12] || {};
		var pathAudit = all[13] || {};
		var mods = all[14] || {};
		var diag = all[15] || {};
		var view = this;
		if (view._updTimer) { clearInterval(view._updTimer); view._updTimer = null; }
		var wrap = E('div', { 'class': 'gs-wrap' });

		/* ===================== TABS shell (v1.12.0) ===================== */
		var TABS = [
			['obzor', 'Обзор'], ['dev', 'Устройства'], ['subs', 'Подписки'],
			['lists', 'Списки и синк'], ['nodes', 'Узлы подписки'],
			['diag', 'Диагностика'],
			['bk', 'Бэкапы'], ['mods', 'Модули'], ['set', 'Настройки']
		];
		var curTab = 'obzor';
		try { curTab = localStorage.getItem('gs-tab') || 'obzor'; } catch (e) {}
		var panels = {}, tabBtns = [];
		var tabbar = E('div', { 'class': 'gs-tabs' });
		var panelBox = E('div', { 'class': 'gs-panels' });
		TABS.forEach(function(t) {
			var btn = E('button', { 'class': 'gs-tab', 'click': function() { showTab(t[0]); } }, t[1]);
			tabBtns.push([t[0], btn]);
			tabbar.appendChild(btn);
			var p = E('div', { 'class': 'gs-panel' });
			panels[t[0]] = p;
			panelBox.appendChild(p);
		});
		function showTab(id) {
			curTab = id;
			try { localStorage.setItem('gs-tab', id); } catch (e) {}
			tabBtns.forEach(function(tb) { tb[1].classList.toggle('gs-tab-active', tb[0] === id); });
			TABS.forEach(function(t) { panels[t[0]].style.display = (t[0] === id) ? '' : 'none'; });
		}
		wrap.appendChild(tabbar);
		wrap.appendChild(panelBox);
		showTab(panels[curTab] ? curTab : 'obzor');

		/* ===================== HERO: dual subscription ===================== */
		var aiCc = st.ai_cc || '', vpnCc = st.vpn_nop2p_cc || '', laneCc = st.vpn_cc || '';
		var aiSub = E('div', { 'class': 'gs-sub-card' }, [
			E('div', { 'class': 'gs-ent' }, 'ИИ · Google / OpenAI / Claude → AI-подписка'),
			E('div', { 'class': 'gs-node' }, [ ccBadge(aiCc, st.ai_node), cleanName(st.ai_node) || '—' ]),
			E('div', { 'class': 'gs-meta' }, 'Слот AI · выход ' + ((CCNAME[aiCc] || aiCc) ? (CCNAME[aiCc] || aiCc) : '—') +
				(st.google ? ' · Google видит: ' + st.google : '')),
			E('div', { 'class': 'gs-meta' }, 'DNS AI-устройств: ' + (
				st.aidns === 1 ? 'через AI-туннель (утечки нет)' :
				st.aidns === 2 ? 'ПРОБЛЕМА — не через туннель (проверьте шаг aidns)' : 'не настроено (утечка DNS!)'))
		]);
		var vpnSub = E('div', { 'class': 'gs-sub-card' }, [
			E('div', { 'class': 'gs-ent' }, 'VPN-noP2P · доменные списки MagiTrickle'),
			E('div', { 'class': 'gs-node' }, [ ccBadge(vpnCc, st.vpn_nop2p_node), cleanName(st.vpn_nop2p_node) || '—' ]),
			E('div', { 'class': 'gs-meta' }, 'Слот VPN-noP2P · выход ' + ((CCNAME[vpnCc] || vpnCc) ? (CCNAME[vpnCc] || vpnCc) : '—') +
				' · сюда нельзя торренты'),
			E('div', { 'class': 'gs-meta' }, 'IP-правил на этом пути: быть не должно (проверка geomask --check)')
		]);
		var laneSub = E('div', { 'class': 'gs-sub-card' }, [
			E('div', { 'class': 'gs-ent' }, 'VPN · полоса для P2P и IP-списков'),
			E('div', { 'class': 'gs-node' }, [ ccBadge(laneCc, st.vpn_node), cleanName(st.vpn_node) || 'не настроена' ]),
			E('div', { 'class': 'gs-meta' }, 'Слот VPN · выход ' + ((CCNAME[laneCc] || laneCc) ? (CCNAME[laneCc] || laneCc) : '—')),
			E('div', { 'class': 'gs-meta' }, 'mtrickle_bulk (IP), пины googlevideo, ответы входящим пирам')
		]);

		var counters = E('div', { 'class': 'gs-counters' }, [
			E('div', { 'class': 'gs-counter' }, [ E('div', { 'class': 'gs-num' }, String(st.ai_rules != null ? st.ai_rules : '—')),
				E('div', { 'class': 'gs-cap' }, 'правил ИИ-пути (ai.yaml → AI)') ]),
			E('div', { 'class': 'gs-counter' }, [ E('div', { 'class': 'gs-num' }, String(st.mt_rules != null ? st.mt_rules : '—')),
				E('div', { 'class': 'gs-cap' }, 'правил общего пути (mtrickle.yaml → VPN)') ]),
			E('div', { 'class': 'gs-counter' }, [ E('div', { 'class': 'gs-num' }, String(st.n_bypass != null ? st.n_bypass : (st.devices || []).length)),
				E('div', { 'class': 'gs-cap' }, 'мимо VPN (реальный IP)') ])
		]);

		var hero = E('div', { 'class': 'gs-hero' }, [
			E('h2', {}, 'Гео-маскировка и маршрутизация AI'),
			E('div', { 'class': 'gs-sub' }, 'Google и ИИ-сервисы идут через AI-подписку (какую страну видит Google — показывает блок выше), всё остальное — через общую VPN.'),
			E('div', { 'class': 'gs-duo' }, [ laneSub, aiSub, vpnSub ]),
			counters
		]);
		panels.obzor.appendChild(hero);

		/* ===================== Subscriptions registry ===================== */
		function maskUrl(u) {
			if (!u) return '—';
			return String(u).replace(/\/[^/]*$/, '/...');
		}
		/* Assignment badges. The scalar *_sub fields are single-valued and were derived from
		   the config text; after an apply re-serialises the config they were all empty at once.
		   subscription_bindings is computed structurally and knows a managed pool's original
		   subscription, so it wins whenever it is present - even when its list is empty. */
		var bindings = st.subscription_bindings || null;
		function bindingIds(pathId) {
			if (!bindings) return null;
			var record = bindings[pathId] || {};
			return Array.isArray(record.subscription_ids) ? record.subscription_ids : [];
		}
		function hasBinding(pathId, id) {
			var ids = bindingIds(pathId);
			if (ids === null) return false;
			return ids.indexOf(id) >= 0;
		}
		var REASON_TEXT = {
			group_missing: 'группа пути не найдена в конфиге',
			group_duplicate: 'в конфиге несколько групп с таким именем',
			group_has_no_providers: 'группа не подключена к провайдеру',
			group_bad_use: 'список провайдеров группы некорректен',
			provider_without_url: 'у провайдера нет ссылки на подписку',
			managed_source_unrecorded: 'не записано, из какой подписки собран проверенный пул',
			managed_source_unverified: 'проверенный пул не подтверждён записью применения',
			managed_source_missing: 'исходная подписка проверенного пула отсутствует в конфиге',
			registry_has_no_match: 'ни одна запись реестра не совпала с источником',
			config_unreadable: 'конфиг mihomo не читается',
			state_unreadable: 'состояние проверок не читается',
			snapshot_changed: 'конфиг менялся во время чтения',
			unreadable_input: 'не удалось прочитать данные назначения'
		};
		function assignmentNotice() {
			if (!bindings) return null;
			var lines = [];
			[['vpn', 'VPN'], ['vpn_nop2p', 'VPN-noP2P'], ['ai', 'AI']].forEach(function(pair) {
				var record = bindings[pair[0]] || {};
				if (record.status === 'partial' || record.status === 'unknown' ||
					record.status === 'unregistered') {
					var why = REASON_TEXT[record.reason_code] || record.reason_code || 'причина неизвестна';
					lines.push(pair[1] + ': назначение не подтверждено (' + why + ')');
				}
			});
			var known = {};
			subs.forEach(function(s) { known[s.id] = true; });
			var dangling = false;
			Object.keys(bindings).forEach(function(key) {
				((bindings[key] || {}).subscription_ids || []).forEach(function(id) {
					if (!known[id]) dangling = true;
				});
			});
			if (dangling) {
				lines.push('данные назначений изменились — обновите страницу');
			}
			if (!lines.length) return null;
			return E('div', { 'class': 'gs-hint gs-warn', style: 'margin:0 0 8px' }, lines.join(' · '));
		}
		function managedNote() {
			if (!bindings) return E('span', {});
			var notes = [];
			[['vpn', 'VPN'], ['vpn_nop2p', 'VPN-noP2P'], ['ai', 'AI']].forEach(function(pair) {
				var record = bindings[pair[0]] || {};
				if (record.via === 'managed' && (record.subscription_ids || []).length) {
					notes.push(pair[1] + ' работает на проверенном пуле — ниже его исходная подписка');
				}
			});
			if (!notes.length) return E('span', {});
			return E('div', { 'class': 'gs-hint', style: 'margin:0 0 8px' }, notes.join(' · '));
		}
		function subRow(s) {
			var isVpn = bindings ? hasBinding('vpn', s.id) : !!(st.vpn_sub && st.vpn_sub === s.id);
			var isVpnNP = bindings ? hasBinding('vpn_nop2p', s.id) : !!(st.vpn_nop2p_sub && st.vpn_nop2p_sub === s.id);
			var isAi = bindings ? hasBinding('ai', s.id) : !!(st.ai_sub && st.ai_sub === s.id);
			var dest = [];
			if (isVpn)   dest.push(E('span', { 'class': 'gs-badge gs-on' }, 'VPN'));
			if (isAi)    dest.push(E('span', { 'class': 'gs-badge gs-ok' }, 'AI'));
			if (isVpnNP) dest.push(E('span', { 'class': 'gs-badge gs-warn' }, 'VPN-noP2P'));
			if (!dest.length) dest = ['—'];
			var bV = { 'class': 'gs-btn gs-btn-use' + (isVpn ? ' gs-btn-vpn-on' : ''), 'title': isVpn ? 'Эта подписка держит слот VPN (P2P разрешён)' : 'Отдать слот VPN (полоса для торрентов и IP-списков) этой подписке' };
			bV.click = function() {
				if (!confirm('Отдать слот VPN этой подписке?\n\nНа неё перейдут правила по IP (mtrickle_bulk), пины зависших YouTube-рёбер и ответы входящим пирам. Полоса создаётся автоматически, если её ещё нет.')) return;
				view.act(callVpnSubUse, { id: s.id }, 'Подписка применена для VPN');
			};
			var bN = { 'class': 'gs-btn gs-btn-use' + (isVpnNP ? ' gs-btn-vpn-on' : ''), 'title': isVpnNP ? 'Эта подписка держит слот VPN-noP2P (провайдер запрещает торренты)' : 'Отдать слот VPN-noP2P (общий VPN по доменным спискам) этой подписке' };
			bN.click = function() {
				if (!confirm('Отдать слот VPN-noP2P этой подписке?\n\nНа неё останутся только доменные правила (mtrickle) и STUN. Убедитесь, что провайдер этой подписки разрешает трафик из ваших списков.')) return;
				view.act(callVpnnpSubUse, { id: s.id }, 'Подписка применена для VPN-noP2P');
			};
			// v1.25.2: the "→ AI" slot switch is back. AI points at this subscription via the AI
			// group. If AI shares its provider with VPN/VPN-noP2P, the backend creates a dedicated
			// provider instead of silently re-pointing the shared one - other lanes never move.
			var bA = { 'class': 'gs-btn gs-btn-use' + (isAi ? ' gs-btn-ai-on' : ''), 'title': isAi ? 'Эта подписка используется для AI-маршрутов' : 'Переключить AI-маршруты на эту подписку' };
			bA.click = function() {
				if (!confirm('Отдать слот AI этой подписке?\n\nПути Google/OpenAI/Claude пойдут через её узлы. Если этот провайдер уже питает VPN или VPN-noP2P, для AI будет создан собственный провайдер - чужие полосы не сдвинутся.')) return;
				view.act(callAiSubUse, { id: s.id }, 'Подписка применена для AI');
			};
			var bD = { 'class': 'gs-btn gs-btn-warn', 'title': 'Убрать из реестра', 'click': function() { view.act(callSubDel, { id: s.id }, 'Подписка удалена'); } };
			return E('tr', {}, [
				E('td', {}, s.name || s.id),
				E('td', { 'class': 'gs-subs-url' }, maskUrl(s.url)),
				E('td', {}, dest),
				E('td', { 'class': 'gs-subs-actions' }, [
					E('button', bV, '→ VPN'),
					E('button', bA, '→ AI'),
					E('button', bN, '→ VPN-noP2P'),
					E('button', bD, 'Удалить')
				])
			]);
		}
		var subNameIn = E('input', { 'class': 'gs-input', 'style': 'width:150px', 'placeholder': 'Например: США' });
		var subUrlIn  = E('input', { 'class': 'gs-input', 'style': 'flex:1;min-width:260px', 'placeholder': 'https://... ссылка подписки' });
		var btnSubAdd = E('button', { 'class': 'gs-btn gs-btn-primary',
			'click': function() {
				var u = (subUrlIn.value || '').trim();
				if (!/^https?:\/\//.test(u)) {
					ui.addNotification(null, E('p', {}, 'Ссылка должна начинаться с http:// или https://'), 'warning');
					return;
				}
				btnSubAdd.disabled = true; btnSubAdd.textContent = 'Добавление…';
				callSubAdd({ name: subNameIn.value || '', url: u }).then(function(res) {
					btnSubAdd.disabled = false; btnSubAdd.textContent = 'Добавить';
					if (res && res.error) { ui.addNotification(null, E('p', {}, 'Ошибка: ' + res.error), 'error'); return; }
					subNameIn.value = ''; subUrlIn.value = '';
					ui.addNotification(null, E('p', {}, 'Подписка добавлена в реестр'), 'info');
					return view.reload();
				}).catch(function(e) {
					btnSubAdd.disabled = false; btnSubAdd.textContent = 'Добавить';
					ui.addNotification(null, E('p', {}, 'Ошибка: ' + e), 'error');
				});
			} }, 'Добавить');
		var btnSubSeed = E('button', { 'class': 'gs-btn',
			'title': 'Импортировать текущие подписки из конфига mihomo в реестр',
			'click': function() {
				btnSubSeed.disabled = true; btnSubSeed.textContent = 'Импорт…';
				callSubsSeed().then(function(res) {
					btnSubSeed.disabled = false; btnSubSeed.textContent = 'Подтянуть текущие из конфига';
					if (res && res.error) { ui.addNotification(null, E('p', {}, 'Ошибка: ' + res.error), 'error'); return; }
					ui.addNotification(null, E('p', {}, 'Текущие подписки добавлены в реестр'), 'info');
					return view.reload();
				}).catch(function(e) {
					btnSubSeed.disabled = false; btnSubSeed.textContent = 'Подтянуть текущие из конфига';
					ui.addNotification(null, E('p', {}, 'Ошибка: ' + e), 'error');
				});
			} }, 'Подтянуть текущие из конфига');
		var subsCard = E('div', { 'class': 'gs-card' }, [
			E('h3', {}, 'Подписки'),
			E('div', { 'class': 'gs-hint', 'style': 'margin:0 0 8px' },
				'Реестр подписок. В колонке «Назначение» показано, чем подписка занята СЕЙЧАС. Три слота: ' +
				'«VPN-noP2P» — общий VPN по доменным спискам (сюда нельзя торренты: провайдер это запрещает), ' +
				'«VPN» — полоса для всего, что выглядит как P2P (правила по IP, пины зависших YouTube-рёбер, ' +
				'ответы входящим пирам), «AI» — Google и AI-сервисы. Кнопка активного слота подсвечена. ' +
				'Переключение — через API mihomo, без перезапуска и обрыва соединений.'),
			assignmentNotice() || E('span', {}),
			managedNote(),
			E('table', { 'class': 'gs-table gs-subs' },
				[ E('tr', {}, [ E('th', {}, 'Имя'), E('th', {}, 'Ссылка'), E('th', {}, 'Назначение'), E('th', {}, '') ]) ]
					.concat(subs.map(subRow))),
			E('div', { 'class': 'gs-row', 'style': 'margin-top:10px' }, [ subNameIn, subUrlIn, btnSubAdd ]),
			E('div', { 'class': 'gs-row' }, [ btnSubSeed ])
		]);
		panels.subs.appendChild(subsCard);

		/* ===================== ACTIONS: sync / google check ===================== */
		var listsOut = E('div', {});
		var btnLists = E('button', { 'class': 'gs-btn gs-btn-primary',
			'click': function() {
				btnLists.disabled = true; btnLists.textContent = 'Синхронизация…';
				callLists().then(function(res) {
					btnLists.disabled = false; btnLists.textContent = 'Синхронизировать списки сейчас';
					var out = (res && res.output) ? res.output : '(нет вывода)';
					listsOut.textContent = out;
					ui.addNotification(null, E('p', {}, 'Списки синхронизированы: ИИ=' +
						(res && res.ai_rules) + ', VPN=' + (res && res.mt_rules)), 'info');
					return view.reload();
				}).catch(function(e) {
					btnLists.disabled = false; btnLists.textContent = 'Синхронизировать списки сейчас';
					ui.addNotification(null, E('p', {}, 'Ошибка: ' + e), 'error');
				});
			} }, 'Синхронизировать списки сейчас');

		var btnGoogle = E('button', { 'class': 'gs-btn',
			'click': function() {
				btnGoogle.disabled = true; btnGoogle.textContent = 'Проверка Google…';
				callGoogle().then(function(res) {
					btnGoogle.disabled = false; btnGoogle.textContent = 'Что видит Google?';
					var cc = res && res.google ? res.google : '(нет ответа)';
					ui.addNotification(null, E('p', {}, 'Google видит: ' + cc), 'info');
					return view.reload();
				}).catch(function(e) {
					btnGoogle.disabled = false; btnGoogle.textContent = 'Что видит Google?';
					ui.addNotification(null, E('p', {}, 'Ошибка: ' + e), 'error');
				});
			} }, 'Что видит Google?');

		var btnScan = E('button', { 'class': 'gs-btn',
			'click': function() {
				btnScan.disabled = true; btnScan.textContent = 'Проверка…';
				callNodescan().then(function() {
					btnScan.textContent = 'Проверить узлы сейчас';
					ui.addNotification(null, E('p', {}, 'Аудит узлов запущен в фоне (до ~1 минуты); итог появится в этой карточке после обновления.'), 'info');
					setTimeout(function() { view.reload(); }, 45000);
				}).catch(function(e) {
					btnScan.disabled = false; btnScan.textContent = 'Проверить узлы сейчас';
					ui.addNotification(null, E('p', {}, 'Ошибка: ' + e), 'error');
				});
			} }, 'Проверить узлы сейчас');

		var syncCard = E('div', { 'class': 'gs-card' }, [
			E('h3', {}, 'Синхронизация и проверка'),
			E('div', { 'class': 'gs-hint', 'style': 'margin:0 0 6px' },
				'Событийный сторож применяет изменения списков magitrickle в mihomo за секунды после сохранения ' +
				'(cron каждые 5 минут остался страховкой). Кнопка ниже — ручной синк "всей силы", если не доверяете автомату.'),
			(function() {
				var s = stk.sync || {}, rows = [];
				var syncTxt;
				if (stk.ok !== true) { syncTxt = 'нет данных (бэкенд stack не ответит?)'; }
				else {
					var last = s.ts ? (s.ts + (s.changed
						? (' — обновлено: VPN=' + (s.mt || '?') + ', AI=' + (s.ai || '?')
							+ (s.moved ? (', в AI по маске: ' + s.moved) : '')
							+ ', mihomo ' + (s.reload ? 'перечитил' : 'НЕ перечитал (!)'))
						: ' — без изменений')) : 'после загрузки синков ещё не было';
					syncTxt = (stk.syncd ? 'сторож активен · ' : 'СТОРОЖ НЕ РАБОТАЕТ (только cron) · ') + last;
				}
				rows.push(E('div', { 'class': 'gs-row' }, [
					E('span', { 'class': 'gs-label' }, 'Автосинк списков'),
					E('span', { 'class': (stk.syncd ? 'gs-val' : 'gs-val gs-badge gs-warn') }, syncTxt)
				]));
				if (stk.ok === true) {
					var ns = stk.nodescan;
					rows.push(E('div', { 'class': 'gs-row' }, [
						E('span', { 'class': 'gs-label' }, 'Узлы подписки (аудит)'),
						E('span', { 'class': 'gs-val gs-upd-meta' },
							ns ? (ns.ts + ' · узлов ' + ns.total + ', с РФ-меткой ' + ns.blocked
								+ (ns.changed ? ' (фильтр обновлён)' : '') + (ns.errors ? ' · сбоевlookup: ' + ns.errors : ''))
								: 'ночной скан ещё не запускался (03:30)')
					]));
					var L = stk.lists || {}, H = stk.health || {};
					rows.push(E('div', { 'class': 'gs-row' }, [
						E('span', { 'class': 'gs-label' }, 'DPI (zapret)'),
						E('span', { 'class': 'gs-val gs-mono gs-upd-meta' },
							(String(stk.strategy || '?') + ' · списки: user=' + (L.user || 0)
								+ ' excl=' + (L.exclude || 0) + (L.nochange ? ' #nochange✓' : ' #nochange✗')
								+ ' google=' + (L.google || 0) + ' rkn=' + (L.rkn || 0))
						)
					]));
					rows.push(E('div', { 'class': 'gs-row' }, [
						E('span', { 'class': 'gs-label' }, 'Пины/здоровье'),
						E('span', { 'class': 'gs-val gs-upd-meta' },
							(stk.pin ? 'рестарты mihomo запрещены пином' : 'пинов нет')
							+ ' · tg-прокси: ' + (H.tg || 0) + ' · лендинг: ' + (H.landing ? 'ок' : 'НЕ отвечает'))
					]));
					var gl = stk.guard || [];
					rows.push(E('div', { 'class': 'gs-row', 'style': 'align-items:flex-start' }, [
						E('span', { 'class': 'gs-label' }, 'Страж маршрутизации'),
						E('span', { 'class': 'gs-val gs-mono gs-upd-meta', 'style': 'white-space:pre-wrap;font-size:11px' },
							gl.length ? gl.slice(-3).join('\n') : 'тихий (последних событий нет)')
					]));
					var K = stk.links || {};
					var lnk = function(t, u) { return u ? E('a', { 'href': u, 'target': '_blank', 'style': 'color:#58a6ff' }, t) : null; };
					rows.push(E('div', { 'class': 'gs-row' }, [
						E('span', { 'class': 'gs-label' }, 'Панели'),
						E('span', {}, [ lnk('MagiTrickle', K.magitrickle), ' · ', lnk('Zapret DPI', K.zapret),
							' · ', lnk('Лендинг', K.landing), ' · ', lnk('LuCI', K.luci) ] || '')
					]));
				}
				return E('div', { 'style': 'margin:0 0 8px' }, rows);
			})(),
			E('div', { 'class': 'gs-actions' }, [ btnLists, btnGoogle, btnScan ]),
			listsOut
		]);
		panels.lists.appendChild(syncCard);

		/* ===================== Routing ===================== */
		var globalSeg = E('div', { 'class': 'gs-seg' }, [
			E('div', { 'class': st.global === 'DIRECT' ? 'gs-active' : '',
				'click': function() { view.act(callGlobal, { mode: 'DIRECT' }); } }, 'DIRECT'),
			E('div', { 'class': st.global === 'VPN' ? 'gs-active' : '',
				'click': function() { view.act(callGlobal, { mode: 'VPN' }); } }, 'VPN')
		]);
		var routeCard = E('div', { 'class': 'gs-card' }, [
			E('h3', {}, 'Маршрутизация'),
			E('div', { 'class': 'gs-row' }, [ E('span', { 'class': 'gs-label' }, 'Глобальный режим (трафик мимо списков)'),
				globalSeg ]),
			E('div', { 'class': 'gs-hint' }, 'DIRECT — мимо списков напрямую (локальные сервисы свободны). VPN — весь остаток через общую подписку.')
		]);
		panels.obzor.appendChild(routeCard);

		/* ===================== IPv6 ===================== */
		var v6Card = E('div', { 'class': 'gs-card' }, [
			E('h3', {}, 'IPv6'),
			E('div', { 'class': 'gs-row' }, [
				E('span', { 'class': 'gs-label' }, 'IPv6-стек (утечка реального IP мимо VPN)'),
				badgeKlass(st.ipv6 === 1, 'gs-warn', 'gs-ok', 'включён — УТЕЧКА', 'выключен'),
				E('button', { 'class': 'gs-btn' + (st.ipv6 ? ' gs-btn-warn' : ''),
					'click': function() { view.act(callIpv6, { state: st.ipv6 ? 'off' : 'on' }); } },
					st.ipv6 ? 'Отключить' : 'Включить')
			]),
			E('div', { 'class': 'gs-hint' }, 'Отключение снимает глобальные IPv6-адреса с роутера и LAN-устройств, утечка закрывается полностью.')
		]);
		panels.obzor.appendChild(v6Card);

		/* ===================== Per-device geo policy ===================== */
		var MODEINFO = {
			bypass: { label: 'Мимо VPN', klass: 'gs-warn',
				hint: 'Реальный IP и часовой пояс ПК. Трафик напрямую или через zapret — mihomo не используется.' },
			realtz: { label: 'Через VPN, часовой пояс ПК свой', klass: 'gs-ok',
				hint: 'IP маскирован через mihomo, но часовой пояс самого ПК не трогается (GeoSync.ps1 на этом ПК выключен).' },
			ai: { label: 'Через AI-подписку', klass: 'gs-on',
				hint: 'ВЕСЬ трафик устройства идёт через AI-подписку (сейчас: Германия). Отдельный туннель AI, VPN не прерывается.' }
		};
		var modeSel = E('select', { 'class': 'gs-input', 'style': 'width:220px' }, [
			E('option', { 'value': 'bypass' }, 'Мимо VPN (реальный IP)'),
			E('option', { 'value': 'realtz' }, 'Через VPN, часовой пояс ПК свой'),
			E('option', { 'value': 'ai' }, 'Через AI-подписку (весь трафик)')
		]);
		var ipIn = E('input', { 'class': 'gs-input', 'style': 'width:130px', 'placeholder': 'IP' });
		var nameIn = E('input', { 'class': 'gs-input', 'style': 'width:150px', 'placeholder': 'Имя' });
		var leaseSel = E('select', { 'class': 'gs-input',
			'change': function(ev) {
				var parts = ev.target.value.split('|');
				if (parts[0]) ipIn.value = parts[0];
				if (parts[1]) nameIn.value = parts[2] || parts[1];
			} },
			[ E('option', { 'value': '' }, '— из DHCP-leases —') ].concat(
				leases.map(function(l) { return E('option', { 'value': l.ip + '|' + l.mac + '|' + (l.name || '') }, l.ip + '  ' + (l.name || l.mac)); })));
		var devRows = (st.devices || []).map(function(x) {
			var mi = MODEINFO[x.mode] || { label: x.mode || '—', klass: 'gs-badge', hint: '' };
			return E('tr', {}, [
				E('td', {}, x.ip),
				E('td', {}, x.name || ''),
				E('td', {}, E('span', { 'class': 'gs-badge ' + mi.klass, 'title': mi.hint }, mi.label)),
				E('td', {}, E('button', { 'class': 'gs-btn gs-btn-warn',
					'click': function() { view.act(callDevDel, { ip: x.ip }); } }, 'Убрать'))
			]);
		});
		var devTable = E('table', { 'class': 'gs-table' },
			[ E('tr', {}, [ E('th', {}, 'IP'), E('th', {}, 'Имя'), E('th', {}, 'Режим'), E('th', {}, '') ]) ].concat(devRows));
		var devCard = E('div', { 'class': 'gs-card' }, [
			E('h3', {}, 'Персональные режимы устройств'),
			E('div', { 'class': 'gs-hint', 'style': 'margin:0 0 8px' },
				'По умолчанию все устройства маскируются: IP идёт через mihomo, а часовой пояс каждого ПК настраивает его ОС. ' +
				'Здесь задаются исключения: мимо VPN / через VPN / весь трафик через AI-подписку' +
				(st.n_ai != null ? ' (сейчас через AI: ' + st.n_ai + ', туннель AI: ' + (st.ai_tun ? 'поднят' : 'нет') + ')' : '') + '.'),
			(st.ai_tun && !st.ai_listener) ? E('div', { 'class': 'gs-hint', 'style': 'color:#d29922' },
				'Внимание: слушатель AI-устройств (порт 7891) не активен — перезапустите mihomo один раз, когда будет удобно: sh /etc/init.d/mihomo restart. До этого устройства с режимом «Через AI» не получат трафик.') : E('div', {}),
			devTable,
			E('div', { 'class': 'gs-row', 'style': 'margin-top:10px' }, [ leaseSel, modeSel ]),
			E('div', { 'class': 'gs-row' }, [ ipIn, nameIn,
				E('button', { 'class': 'gs-btn', 'click': function() {
					if (!ipIn.value) { ui.addNotification(null, E('p', {}, 'Укажите IP'), 'warning'); return; }
					view.act(callDevAdd, { ip: ipIn.value, mode: modeSel.value, mac: (leaseSel.value.split('|')[1] || ''), name: nameIn.value || ipIn.value });
					ipIn.value = ''; nameIn.value = ''; leaseSel.value = '';
				} }, 'Добавить')
			])
		]);
		panels.dev.appendChild(devCard);

		/* ===================== Log ===================== */
		var logCard = E('div', { 'class': 'gs-card' }, [
			E('h3', {}, 'Журнал'),
			(function() {
				/* v1.12.3: same semantic colors as the update console */
				var box = E('pre', { 'class': 'gs-log' });
				String(lg || '(пусто)').split('\n').forEach(function(l) {
					var cls = 'c-dim';
					if (/\[error\]|FAILED|FAIL\b|abort/i.test(l)) cls = 'c-err';
					else if (/\bwarn/i.test(l)) cls = 'c-run';
					else if (/\[done\]|updated to|SYNC changed|no change/i.test(l)) cls = 'c-done';
					box.appendChild(E('div', { 'class': cls }, l));
				});
				return box;
			})()
		]);
		panels.obzor.appendChild(logCard);

		/* ===================== Updates (bottom - rarely used block) ===================== */
		var updStatus = E('div', { 'class': 'gs-upd-status' });
		var updLog = E('pre', { 'class': 'gs-console' });
		updLog.style.display = 'none';
		var lastLogKey = null;
		function updLineClass(l) {
			/* colors carry meaning: red=error, yellow=real warning, green=finished,
			   dim=normal process step (was yellow before v1.12.3 - read as false warnings) */
			if (/\[error\]|FAILED|fail\b/i.test(l)) return 'c-err';
			if (/\bwarn(ing)?\b/i.test(l)) return 'c-run';
			if (l.indexOf('[done]') >= 0 || /updated to|filter updated|no change/.test(l)) return 'c-done';
			return 'c-dim';
		}
		function updRenderLines(lines) {
			lines = lines || [];
			var key = lines.join('\n');
			if (key === lastLogKey) return;
			lastLogKey = key;
			updLog.textContent = '';
			if (!lines.length) { updLog.style.display = 'none'; return; }
			updLog.style.display = '';
			lines.forEach(function(l) { updLog.appendChild(E('div', { 'class': updLineClass(l) }, l)); });
			updLog.scrollTop = updLog.scrollHeight;
		}
		/* --- backups folder (SMB-shareable, v1.9.0) --- */
		var bkBox = E('div', { 'class': 'gs-bk' });
		function fmtSize(b) {
			b = +b || 0;
			return (b >= 1048576) ? (b / 1048576).toFixed(1) + ' MB' : Math.max(1, Math.round(b / 1024)) + ' KB';
		}
		function renderBk(b) {
			b = b || {};
			bkBox.textContent = '';
			bkBox.appendChild(E('h4', {}, 'Резервные копии (папка на роутере, SMB-шар)'));
			if (b.share_ok === false)
				bkBox.appendChild(E('div', { 'class': 'gs-row' }, [ E('span', { 'class': 'gs-badge gs-warn' },
					'SMB-шар geomask-backups не найден — прогоните шаг geo в geomask') ]));
			var unc = b.unc || '', smbu = b.smb || '';
			bkBox.appendChild(E('div', { 'class': 'gs-row' }, [
				E('span', { 'class': 'gs-label' }, 'Папка'),
				E('span', { 'class': 'gs-val gs-mono' }, unc || b.dir || '—'),
				E('span', {}, [
					E('button', { 'class': 'gs-btn', 'click': function () { copyToClipboard(unc, this); } }, 'Копировать (Windows)'),
					E('button', { 'class': 'gs-btn', 'click': function () { copyToClipboard(smbu, this); } }, 'Копировать (smb://)')
				])
			]));
			var files = b.files || [];
			if (!files.length) {
				bkBox.appendChild(E('div', { 'class': 'gs-hint' },
					'Свежей копии нет — она создаётся автоматически перед каждым обновлением и при geomask --backup.'));
			} else {
				files.forEach(function (f) {
					bkBox.appendChild(E('div', { 'class': 'gs-row' }, [
						E('span', { 'class': 'gs-val gs-mono' }, String(f.n || '')),
						E('span', { 'class': 'gs-upd-meta' }, fmtSize(f.s) + ' · ' + String(f.t || '')),
						E('span', {}, E('button', { 'class': 'gs-btn', 'click': function () { copyToClipboard(unc + '\\' + String(f.n || ''), this); } }, 'Копировать путь к файлу'))
					]));
				});
			}
			bkBox.appendChild(E('div', { 'class': 'gs-hint' },
				'Хранится самая свежая копия (роутер экономит место); полная история — на ПК: geomask\\fetch_backup.ps1. ' +
				'Провод Windows: Win+R → вставить скопированный путь. Восстановление: LuCI → System → Backup / Flash Firmware → Restore.'));
			if (b.user && b.pass) {
				var nu = 'net use ' + unc + ' /user:' + b.user + ' ' + b.pass + ' /persistent:yes';
				bkBox.appendChild(E('div', { 'class': 'gs-row' }, [
					E('span', { 'class': 'gs-label' }, 'Windows: вход backup / backup (или без пароля — гость)'),
					E('span', { 'class': 'gs-mono gs-upd-meta' }, b.user + ' / ' + b.pass),
					E('span', {}, E('button', { 'class': 'gs-btn', 'click': function () { copyToClipboard(nu, this); } }, 'Копировать net use'))
				]));
			}
		}
		renderBk(bks);
		var latest = upd.latest || {}, loc = upd.local || {};
		var stack = upd.stack || [];
		var updErr = upd.error || null;
		var coreV = loc.core || 'none', latV = latest.version || '',
			latTag = (latest.tag || '').replace(/^v/, '');   /* display without the tag 'v': 1.7.3 */
		var UPD_RUNNING = ['queued', 'backup', 'download', 'validate', 'apply'];

		function verRow(label, val, hint) {
			return E('div', { 'class': 'gs-row' }, [
				E('span', { 'class': 'gs-label' }, label),
				E('span', { 'class': 'gs-val gs-mono' }, val || '—'),
				hint || E('span', {})
			]);
		}

		function updPoll() {
			if (view._updTimer) clearInterval(view._updTimer);
			view._updTimer = setInterval(function() {
				callUpdStatus().then(function(s) {
					s = s || {};
					var ph = s.phase || 'idle';
					updRenderLines(s.lines);
					if (ph === 'done') {
						clearInterval(view._updTimer); view._updTimer = null;
						updStatus.style.color = '';
						updStatus.textContent = '';
						updStatus.appendChild(E('span', { 'class': 'gs-badge gs-ok' }, 'Готово: ' + (s.msg || '')));
						updStatus.appendChild(' ');
						updStatus.appendChild(E('button', { 'class': 'gs-btn gs-btn-primary',
							'click': function() { location.reload(); } }, 'Обновить страницу'));
						callBackups().then(function(b) { renderBk(b || {}); });
					} else if (ph === 'error') {
						clearInterval(view._updTimer); view._updTimer = null;
						updStatus.style.color = '#f85149';
						updStatus.textContent = 'Ошибка: ' + (s.msg || 'unknown');
					} else if (UPD_RUNNING.indexOf(ph) >= 0) {
						updStatus.style.color = '#d29922';
						updStatus.textContent = (s.msg || ph) + '  [' + (s.ts || '') + ']';
					}
				});
			}, 2000);
		}

		var updChildren = [
			E('h3', {}, 'Релиз geomask: обновление целиком'),
			E('div', { 'class': 'gs-hint', 'style': 'margin:0 0 8px' },
				'Проверка версий — при открытии страницы (кэш 1 ч) и по кнопке. ' +
				'Обновление geomask: полный бэкап роутера → скачивание релиза с GitHub (sha256) → ' +
				'установка модулей и панели без перезапуска сервисов → после «Готово» обнови страницу (Ctrl+F5). ' +
				'Каждый шаг пишется отдельной строкой в консоль ниже и в общий журнал (/root/geo-sync.log). ' +
				'Бэкап перед обновлением хранится в папке «Резервные копии» ниже (SMB-шар на роутере, ' +
				'самая свежая копия; полная история на ПК — geomask\\fetch_backup.ps1); восстановление — ' +
				'LuCI System → Backup / Flash Firmware → Restore.')
		];
		if (updErr) {
			updChildren.push(E('div', { 'class': 'gs-row' }, [
				E('span', { 'class': 'gs-label' }, 'Проверка версий'),
				E('span', { 'class': 'gs-badge gs-warn' }, 'ошибка'),
				E('span', { 'class': 'gs-upd-meta' }, String(updErr))
			]));
		} else {
			/* v1.30.3: show the installed release next to the GitHub one. Without it the card
			   read as "1.25.3 - доступно обновление" on a router that already ran 1.30.0. */
			var localRelease = loc.core || 'none';
			var localNewer = false;
			try {
				var a = String(localRelease).split('.').map(Number),
					b = String(latV).split('.').map(Number);
				localNewer = localRelease !== 'none' && latV &&
					(a[0] > b[0] || (a[0] === b[0] && (a[1] > b[1] ||
					 (a[1] === b[1] && a[2] > b[2]))));
			} catch (e) { localNewer = false; }
			updChildren.push(verRow('Установлено', localRelease,
				localNewer ? E('span', { 'class': 'gs-upd-meta' }, 'новее релиза на GitHub') : null));
			updChildren.push(E('div', { 'class': 'gs-row' }, [
				E('span', { 'class': 'gs-label' }, 'Доступно на GitHub'),
				latV ? E('span', { 'class': 'gs-val gs-upd-latest' }, latTag) : E('span', {}, '—'),
				upd.need === true
					? E('span', { 'class': 'gs-badge gs-warn' }, 'доступно обновление')
					: (localNewer
						? E('span', { 'class': 'gs-badge gs-ok' }, 'локальная новее — обновлять не нужно')
						: E('span', { 'class': 'gs-badge gs-ok' }, 'актуально'))
			]));
			updChildren.push(E('div', { 'class': 'gs-hint', 'style': 'margin:2px 0 8px' },
				'Части: панель ' + (loc.panel || '—') + ' · бэкенд ' + (loc.backend || '—') +
				' · guard ' + (loc.guard || '—') + '. Версии модулей — в списке выше.'));
			/* The GitHub answer is cached for an hour on purpose (unauthenticated API allows
			   ~60 requests/hour), so a release published minutes ago stays invisible. This
			   button forces one live poll. */
			var btnUpdPoll = E('button', { 'class': 'gs-btn', 'click': function() {
				btnUpdPoll.disabled = true; btnUpdPoll.textContent = 'Проверяю…';
				callUpdCheck({ force: 'force' }).then(function() {
					btnUpdPoll.disabled = false; btnUpdPoll.textContent = 'Проверить сейчас';
					return view.reload();
				}).catch(function(e) {
					btnUpdPoll.disabled = false; btnUpdPoll.textContent = 'Проверить сейчас';
					ui.addNotification(null, E('p', {}, 'Не удалось проверить: ' + e), 'error');
				});
			} }, 'Проверить сейчас');
			updChildren.push(E('div', { 'class': 'gs-row' }, [
				E('span', { 'class': 'gs-hint' }, 'опрос GitHub кэшируется на 60 мин — кнопка ниже опрашивает его напрямую'),
				btnUpdPoll
			]));
			if (upd.need === true) {
				var btnUpd = E('button', { 'class': 'gs-btn gs-btn-primary', 'click': function() {
					if (!confirm('Обновить geomask до ' + latTag + '?\n\n' +
						'Будет создан полный бэкап роутера, файлы модулей и панель заменятся на ' + latTag + '.\n' +
						'Сервисы (mihomo, magitrickle и т.д.) не перезапускаются.')) return;
					btnUpd.disabled = true; btnUpd.textContent = 'Запуск…';
					callUpdApply().then(function(res) {
						btnUpd.disabled = false; btnUpd.textContent = 'Обновить до ' + latTag;
						if (res && (res.error || res.ok === false)) {
							updStatus.style.color = '#f85149';
							updStatus.textContent = 'Ошибка: ' + (res.error || res.err || 'отклонено');
							return;
						}
						updStatus.style.color = '#d29922';
						updStatus.textContent = 'Обновление запущено…';
						updPoll();
					}).catch(function(e) {
						btnUpd.disabled = false; btnUpd.textContent = 'Обновить до ' + latTag;
						updStatus.style.color = '#f85149';
						updStatus.textContent = 'Ошибка: ' + e;
					});
				} }, 'Обновить до ' + latTag);
				updChildren.push(E('div', { 'class': 'gs-actions' }, [ btnUpd ]));
			}
			updChildren.push(E('div', { 'class': 'gs-row' }, [
				E('span', { 'class': 'gs-label' }, 'Стек (справочно)'),
				(function() {
					var btnCh = E('button', { 'class': 'gs-btn', 'click': function() {
						btnCh.disabled = true; btnCh.textContent = 'Проверка…';
						callUpdCheck({ force: '1' }).then(function() { return view.reload(); })
							.catch(function() { btnCh.disabled = false; btnCh.textContent = 'Проверить ещё раз'; });
					} }, 'Проверить ещё раз');
					return btnCh;
				})()
			]));
			updChildren.push(E('div', { 'class': 'gs-stack' }, stack.length
				? stack.map(function(s) { return E('span', {}, [ E('b', {}, (s.n || '?') + ': '), String(s.v || '?') ]); })
				: [ E('span', {}, 'нет данных') ]));
			if (upd.checked_at) updChildren.push(E('div', { 'class': 'gs-upd-meta' },
				'Проверено: ' + upd.checked_at + ' · источник: ' + (upd.src || '?') + (upd.need === true && latest.digest ? ' · sha256: ' + String(latest.digest).slice(0, 19) + '…' : '')));
		}
		updChildren.push(updStatus);
		updChildren.push(updLog);
		var updCard = E('div', { 'class': 'gs-card' }, updChildren);
		/* v1.30.0: the release card lives in the module tab (appended after the list) */
		updRenderLines((upds || {}).lines);
		if (UPD_RUNNING.indexOf((upds || {}).phase) >= 0) {
			updStatus.style.color = '#d29922';
			updStatus.textContent = 'Идет обновление… подробности в консоли ниже.';
			updPoll();
		}

		/* ===================== Backups as own tab card (v1.12.0) ===================== */

		/* ===================== МОДУЛИ: отдельное обновление (v1.28.0) ===================== */
		function modState(state) {
			var map = {
				'current': ['gs-on', 'актуален'],
				'update-available': ['gs-warn', 'есть обновление'],
				'modified': ['gs-warn', 'изменён локально'],
				'incomplete': ['gs-off', 'неполный'],
				'not-selected': ['gs-off', 'не выбран']
			};
			var it = map[state] || ['gs-off', state || '—'];
			return E('span', { 'class': 'gs-badge ' + it[0] }, it[1]);
		}
		var modRows = (mods.modules || []).map(function(m) {
			var same = !m.available_version || m.available_version === m.installed_version;
			var label = m.state === 'modified' ? 'Перезаписать' : 'Обновить';
			var btn = E('button', {
				'class': 'gs-btn gs-btn-ok',
				'disabled': (m.state === 'current' || m.state === 'incomplete'
					|| m.state === 'not-selected' || !m.files) ? 'disabled' : null,
				'click': function() {
					view.act(callModUpdate, { module_id: m.id },
						'Модуль ' + (m.name || m.id) + ' обновлён');
				}
			}, label);
			return E('div', { 'class': 'gs-row', style: 'align-items:center;gap:10px;padding:6px 0;border-bottom:1px solid var(--border-color,#30363d)' }, [
				E('span', { 'class': 'gs-cc' }, m.id),
				E('span', { 'style': 'flex:1' }, m.name || ''),
				E('span', { 'class': 'gs-meta' }, same ? (m.installed_version || '—')
					: (m.installed_version || '—') + ' → ' + m.available_version),
				modState(m.state),
				E('span', { 'class': 'gs-meta' }, (m.files || 0) + ' ф.'),
				btn
			]);
		});
		if (!modRows.length) modRows = [E('div', { 'class': 'gs-hint' }, 'Список модулей пуст — нажмите «Проверить обновления».')];
		panels.mods.appendChild(E('div', { 'class': 'gs-card' }, [
			E('h3', {}, 'Модули'),
			E('div', { 'class': 'gs-hint' }, 'Каждый модуль обновляется отдельно и имеет собственную версию. Файлы проверяются по SHA-256, локальные правки не перезаписываются без подтверждения.'),
			E('div', { 'class': 'gs-hint' }, 'Модуль «не выбран» не устанавливался на этот роутер — его набор задаётся при установке (sh geomask.sh --list-modules).'),
			E('div', { 'class': 'gs-row', style: 'margin:8px 0' }, [
				E('button', { 'class': 'gs-btn', 'click': function() {
					view.act(callModCheck, {}, 'Индекс обновлений обновлён');
				} }, 'Проверить обновления')
			]),
			E('div', {}, modRows)
		]));
		/* v1.30.0: the release card (former «Обновление» tab) sits right here: one place
		   to watch and update every part of geomask - modules first, the release below. */
		panels.mods.appendChild(updCard);

		/* ===================== ДИАГНОСТИКА (v1.29.1) ===================== */
		var diagData = (diag.checks || []).slice();
		var diagSummary = E('span', { 'class': 'gs-meta' }, '');
		var diagVerdict = E('div', { 'class': 'gs-hint', style: 'margin:6px 0' },
			diag.verdict || 'Проверок ещё не было — нажмите «Проверить всё».');
		if (diag.verdict_state) {
			diagVerdict.style.color = diag.verdict_state === 'fail' ? '#f85149'
				: (diag.verdict_state === 'warn' ? '#d29922' : '#3fb950');
		}
		var diagRowsBox = E('div', {});
		var diagLogBox = E('pre', { 'class': 'gs-log', style: 'display:none' });
		var domInput = E('input', { 'class': 'gs-input', 'placeholder': 'example.com', style: 'min-width:240px' });
		var domLive = E('input', { 'type': 'checkbox', style: 'margin:0 6px 0 12px' });
		var domOut = E('pre', { 'class': 'gs-console', style: 'display:none' });
		var domLog = E('span', { 'class': 'gs-meta' }, '');

		function diagBadge(state) {
			var map = {
				'ok': ['gs-on', 'в порядке'],
				'warn': ['gs-warn', 'внимание'],
				'fail': ['gs-off', 'проблема']
			};
			var it = map[state] || ['gs-off', 'не проверялось'];
			return E('span', { 'class': 'gs-badge ' + it[0] }, it[1]);
		}

		function diagMerge(checks) {
			(checks || []).forEach(function(r) {
				var found = false;
				for (var i = 0; i < diagData.length; i++) {
					if (diagData[i].id === r.id) {
						diagData[i].last_state = r.state;
						diagData[i].last_summary = r.summary;
						diagData[i]._details = r.details || [];
						diagData[i]._fix = r.fix || '';
						found = true;
						break;
					}
				}
				if (!found) diagData.push({ id: r.id, area: r.area, title: r.title, live: r.live,
					last_state: r.state, last_summary: r.summary, _details: r.details || [],
					_fix: r.fix || '' });
			});
			diagRender();
		}

		function diagRender() {
			while (diagRowsBox.firstChild) diagRowsBox.removeChild(diagRowsBox.firstChild);
			if (!diagData.length) {
				diagRowsBox.appendChild(E('div', { 'class': 'gs-hint' },
					diag.error ? ('Диагностика недоступна: ' + diag.error) : 'Список проверок пуст.'));
				return;
			}
			[['modules', 'Модули'], ['router', 'Роутер'], ['network', 'Сеть'], ['paths', 'Пути']].forEach(function(area) {
				var items = diagData.filter(function(c) { return c.area === area[0]; });
				if (!items.length) return;
				diagRowsBox.appendChild(E('div', { 'class': 'gs-hint', style: 'margin-top:12px' }, area[1]));
				items.forEach(function(c) {
					var rows = [
						E('span', { 'style': 'flex:1' }, c.title + (c.live ? ' · живая' : '')),
						E('span', { 'class': 'gs-meta' }, c.last_summary || ''),
						diagBadge(c.last_state),
						E('button', { 'class': 'gs-btn', 'click': function(ev) { diagRun(c.id, ev.target); } }, 'Проверить'),
						E('button', { 'class': 'gs-btn', 'click': function() { diagShowLog(c.id, c.title); } }, 'Лог'),
						E('div', { 'class': 'gs-meta', style: 'flex-basis:100%;color:#8b949e' },
							(c._details && c._details.length) ? c._details.join(' · ') : '')
					];
					if (c._fix && c.last_state !== 'ok') {
						rows.push(E('div', { 'class': 'gs-meta', style: 'flex-basis:100%;color:#d29922' },
							'→ ' + c._fix));
					}
					diagRowsBox.appendChild(E('div', { 'class': 'gs-row', style: 'flex-wrap:wrap;align-items:center;gap:8px;padding:5px 0;border-bottom:1px solid var(--border-color,#30363d)' }, rows));
				});
			});
		}

		function diagRun(id, btn) {
			if (btn) btn.disabled = true;
			diagSummary.textContent = 'идёт проверка…';
			return callDiagRun(id).then(function(res) {
				if (res && res.error) {
					ui.addNotification(null, E('p', {}, 'Ошибка: ' + res.error), 'error');
					return;
				}
				diagMerge(res && res.checks);
				if (res && res.verdict) {
					diagVerdict.textContent = res.verdict;
					diagVerdict.style.color = res.verdict_state === 'fail' ? '#f85149'
						: (res.verdict_state === 'warn' ? '#d29922' : '#3fb950');
				}
			}).catch(function(e) {
				ui.addNotification(null, E('p', {}, 'Ошибка: ' + e), 'error');
			}).then(function() {
				if (btn) btn.disabled = false;
				diagSummary.textContent = '';
			});
		}

		function diagShowLog(id, title) {
			callDiagLog(id).then(function(res) {
				diagLogBox.style.display = '';
				diagLogBox.textContent = (title ? ('# ' + title + '\n') : '') + ((res && res.text) || 'лог пока пуст');
			}).catch(function(e) {
				diagLogBox.style.display = '';
				diagLogBox.textContent = 'Ошибка чтения лога: ' + e;
			});
		}

		function domRun() {
			var name = (domInput.value || '').trim();
			if (!name) return;
			domOut.style.display = '';
			domOut.textContent = 'проверяю ' + name + '…';
			domLog.textContent = '';
			callDiagDomain(name, domLive.checked ? 'live' : '').then(function(res) {
				if (!res || res.ok === false) {
					domOut.textContent = 'Ошибка: ' + ((res || {}).error || 'нет ответа');
					return;
				}
				domOut.textContent = (res.lines || []).join('\n');
				domLog.textContent = 'лог: ' + (res.log || '');
			}).catch(function(e) {
				domOut.textContent = 'Ошибка: ' + e;
			});
		}

		panels.diag.appendChild(E('div', { 'class': 'gs-card' }, [
			E('h3', {}, 'Диагностика'),
			E('div', { 'class': 'gs-hint' }, 'Проверки выполняются на роутере. У каждой свой лог, логи хранятся 7 дней.'),
			diagVerdict,
			E('div', { 'class': 'gs-row', style: 'margin:8px 0;gap:10px;align-items:center' }, [
				E('button', { 'class': 'gs-btn gs-btn-primary', 'click': function(ev) { diagRun('all', ev.target); } }, 'Проверить всё'),
				diagSummary
			]),
			diagRowsBox,
			diagLogBox
		]));
		panels.diag.appendChild(E('div', { 'class': 'gs-card' }, [
			E('h3', {}, 'Маршрут домена'),
			E('div', { 'class': 'gs-hint' }, 'Введите домен — покажу DNS, группу MagiTrickle, правило mihomo, узел и страну выхода. Без ИИ и без экспериментов.'),
			E('div', { 'class': 'gs-row', style: 'margin:8px 0;gap:8px;align-items:center;flex-wrap:wrap' }, [
				domInput,
				E('label', { 'class': 'gs-meta' }, [domLive, 'живая проверка (реальный запрос)']),
				E('button', { 'class': 'gs-btn gs-btn-primary', 'click': domRun }, 'Проверить маршрут')
			]),
			domOut,
			domLog
		]));
		diagRender();

		panels.bk.appendChild(E('div', { 'class': 'gs-card' }, [
			E('h3', {}, 'Резервные копии'),
			bkBox
		]));

		/* ===================== Automation (lists tab) ===================== */
		var selInt = E('select', { 'class': 'gs-input' });
		['1', '2', '3', '5', '10', '15', '30'].forEach(function(v) {
			var op = E('option', { value: v }, 'каждые ' + v + ' мин');
			if (stk.sync_interval && String(stk.sync_interval).replace('*/', '') === v) op.selected = true;
			selInt.appendChild(op);
		});
		var btnInt = E('button', { 'class': 'gs-btn', 'click': function() {
			btnInt.disabled = true;
			callSyncSet({ interval: selInt.value }).then(function(res) {
				btnInt.disabled = false;
				if (res && res.error) { ui.addNotification(null, E('p', {}, 'Ошибка: ' + res.error), 'error'); return; }
				ui.addNotification(null, E('p', {}, 'Cron-страховка синка: каждые ' + selInt.value + ' мин'), 'info');
				return view.reload();
			}).catch(function(e) { btnInt.disabled = false; ui.addNotification(null, E('p', {}, 'Ошибка: ' + e), 'error'); });
		} }, 'Применить');
		var btnSyncd = stk.syncd_enabled
			? E('button', { 'class': 'gs-btn gs-btn-warn', 'click': function() {
				btnSyncd.disabled = true;
				callSyncdSet({ state: 'off' }).then(function() { view.reload(); }).catch(function() { btnSyncd.disabled = false; });
			} }, 'Выключить сторож')
			: E('button', { 'class': 'gs-btn gs-btn-primary', 'click': function() {
				btnSyncd.disabled = true;
				callSyncdSet({ state: 'on' }).then(function() { view.reload(); }).catch(function() { btnSyncd.disabled = false; });
			} }, 'Включить сторож');
		panels.lists.appendChild(E('div', { 'class': 'gs-card' }, [
			E('h3', {}, 'Автоматизация применения списков'),
			E('div', { 'class': 'gs-row' }, [
				E('span', { 'class': 'gs-label' }, 'Событийный сторож (применяет за секунды)'),
				E('span', { 'class': 'gs-badge ' + (stk.syncd ? 'gs-ok' : 'gs-off') }, stk.syncd ? 'работает' : 'не запущен'),
				btnSyncd
			]),
			E('div', { 'class': 'gs-row' }, [
				E('span', { 'class': 'gs-label' }, 'Cron-страховка (полный прогон)'),
				E('span', { 'class': 'gs-val gs-mono' }, String(stk.sync_interval || '*/5')),
				E('span', {}, [selInt, btnInt])
			]),
			E('div', { 'class': 'gs-hint' }, 'Сторож реагирует на сохранение списков в MagiTrickle; cron — гарантия на случай сбоя события.')
		]));

		/* ===================== Lists template (lists tab, v1.13.0) ===================== */
		var btnSeed = E('button', { 'class': 'gs-btn gs-btn-warn', 'click': function() {
			if (!confirm('Восстановить списки MagiTrickle из шаблона geomask? Текущие группы будут перезаписаны (config.yaml сохранится в бэкап), MagiTrickle перезапустится на несколько секунд.')) return;
			btnSeed.disabled = true; btnSeed.textContent = 'Восстановление…';
			callListsSeed().then(function(res) {
				btnSeed.disabled = false; btnSeed.textContent = 'Восстановить из шаблона';
				if (res && res.ok) ui.addNotification(null, E('p', {}, 'Списки восстановлены из шаблона'), 'info');
				else ui.addNotification(null, E('p', {}, 'Ошибка восстановления: ' + ((res && res.result) || 'см. журнал')), 'error');
				return view.reload();
			}).catch(function(e) { btnSeed.disabled = false; btnSeed.textContent = 'Восстановить из шаблона'; ui.addNotification(null, E('p', {}, 'Ошибка: ' + e), 'error'); });
		} }, 'Восстановить из шаблона');
		panels.lists.appendChild(E('div', { 'class': 'gs-card' }, [
			E('h3', {}, 'Шаблоны списков (резерв восстановления)'),
			E('div', { 'class': 'gs-hint', 'style': 'margin:0 0 8px' },
				'geomask хранит снимок всех групп MagiTrickle (To VPN / To Not VPN / Google+AI / Geoblock / Youtube и др.) — без секретов. ' +
				'После переустановки или сброса (инцидент B-018) кнопка ниже вернет списки к этому снимку. Тонкая правка доменов — в панели MagiTrickle (:8080).'),
			E('div', { 'class': 'gs-row' }, [
				E('span', { 'class': 'gs-label' }, 'Шаблон'),
				E('span', { 'class': 'gs-badge ' + (lt.template_present ? 'gs-ok' : 'gs-off') }, lt.template_present ? ('v' + (lt.template_version || '?')) : 'отсутствует'),
				E('span', { 'class': 'gs-val gs-mono' }, 'групп ' + (lt.template_groups || 0) + ' · правил ' + (lt.template_rules || 0))
			]),
			E('div', { 'class': 'gs-row' }, [
				E('span', { 'class': 'gs-label' }, 'Сейчас в MagiTrickle'),
				E('span', { 'class': 'gs-val gs-mono' }, 'групп ' + (lt.current_groups || 0) + ' · правил ' + (lt.current_rules || 0)),
				E('span', { 'class': 'gs-badge ' + (lt.magitrickle_running ? 'gs-ok' : 'gs-off') }, lt.magitrickle_running ? 'работает' : 'остановлен')
			]),
			E('div', { 'class': 'gs-row' }, [ btnSeed ])
		]));

		/* ===================== YouTube edges watchdog (lists tab, v1.14.0) ===================== */
		var ytAuto = (yt.mode || 'auto') === 'auto';
		var setMode = function(m, btn) {
			btn.disabled = true;
			callYtMode({ mode: m }).then(function(res) {
				btn.disabled = false;
				if (res && res.ok) ui.addNotification(null, E('p', {}, m === 'auto' ? 'Режим авто: битые рёбра уходят в VPN, остальные через запрет (DPI)' : 'Режим blanket: весь googlevideo в VPN'), 'info');
				else ui.addNotification(null, E('p', {}, 'Не удалось переключить режим'), 'error');
				return view.reload();
			}).catch(function(e) { btn.disabled = false; ui.addNotification(null, E('p', {}, 'Ошибка: ' + e), 'error'); });
		};
		var btnYtAuto = E('button', { 'class': 'gs-btn' + (ytAuto ? '' : ' gs-btn-primary'), 'click': function() { setMode('auto', btnYtAuto); } }, 'Авто (битые — в VPN)');
		var btnYtAll = E('button', { 'class': 'gs-btn' + (ytAuto ? '' : ' gs-btn-primary'), 'click': function() { setMode('blanket', btnYtAll); } }, 'Весь googlevideo в VPN');
		var btnYtRel = E('button', { 'class': 'gs-btn gs-btn-warn', 'click': function() {
			if (!confirm('Отпустить все автотопинги? Рёбра вернутся на прямой путь через запрет (DPI).')) return;
			btnYtRel.disabled = true;
			callYtReset().then(function() { view.reload(); }).catch(function(e) { btnYtRel.disabled = false; ui.addNotification(null, E('p', {}, 'Ошибка: ' + e), 'error'); });
		} }, 'Отпустить пины сейчас');
		var ytPins = yt.pinned_hosts || [];
		panels.lists.appendChild(E('div', { 'class': 'gs-card' }, [
			E('h3', {}, 'YouTube: рёбра googlevideo'),
			E('div', { 'class': 'gs-hint', 'style': 'margin:0 0 8px' },
				'Google раздаёт адреса рёбер динамически и часть из них блокируется по IP, поэтому статический список всегда отстаёт. ' +
				'В режиме «Авто» рёбра идут напрямую через запрет (DPI), а watchdog каждые 5 минут читает ошибки соединений mihomo и ' +
				'кладёт реально падающие хосты в группу «GoogleVideo VPN» (видно в MagiTrickle :8080). Ночью пины отпускаются. ' +
				'Картинки (ytimg/ggpht) и сам youtube.com — всегда напрямую через запрет.'),
			E('div', { 'class': 'gs-row' }, [
				E('span', { 'class': 'gs-label' }, 'Режим'),
				E('span', { 'class': 'gs-badge ' + (ytAuto ? 'gs-ok' : 'gs-off') }, ytAuto ? 'авто' : 'весь в VPN'),
				btnYtAuto, btnYtAll
			]),
			E('div', { 'class': 'gs-row' }, [
				E('span', { 'class': 'gs-label' }, 'Сейчас в VPN'),
				E('span', { 'class': 'gs-val gs-mono' }, ytPins.length ? (ytPins.length + ' ребра') : '—'),
				E('span', { 'class': 'gs-hint' }, 'порог ' + (yt.threshold || 2) + ' ошибок за ' + (yt.window_min || 30) + ' мин, максимум ' + (yt.max_pins || 40))
			]),
			ytPins.length ? E('div', { 'class': 'gs-blocklist' }, ytPins.join('\n')) : null,
			(function(){
				var st = yt.stalled || {}, ks = Object.keys(st).sort();
				return E('div', { 'class': 'gs-row' }, [
					E('span', { 'class': 'gs-label' }, 'Зависшие рёбра'),
					E('span', { 'class': 'gs-badge ' + (ks.length ? 'gs-warn' : 'gs-ok') }, ks.length ? (ks.length + ' без данных') : 'нет'),
					E('span', { 'class': 'gs-hint' }, 'считается зависшим: живёт >' + (yt.stall_min_age || 25) + ' с и скачано <' +
						Math.round((yt.stall_max_bytes || 10000)/1024) + ' КБ — такое ребро пинится в VPN')
				]);
			})(),
			(function(){
				var st = yt.stalled || {}, ks = Object.keys(st).sort();
				return ks.length ? E('div', { 'class': 'gs-blocklist' }, ks.join('\n')) : null;
			})(),
			E('div', { 'class': 'gs-row' }, [ btnYtRel ])
		]));

		/* ===================== Torrents (lists tab, v1.15.0) ===================== */
		var torOn = !!tor.listener_present;
		var setListener = function(state, btn) {
			btn.disabled = true;
			callTorListener({ state: state }).then(function(res) {
				btn.disabled = false;
				if (res && res.ok) ui.addNotification(null, E('p', {}, state === 'on' ? 'Слушатель включён' : 'Слушатель выключен'), 'info');
				else ui.addNotification(null, E('p', {}, 'Не удалось изменить (см. /root/geo-sync.log)'), 'error');
				return view.reload();
			}).catch(function(e) { btn.disabled = false; ui.addNotification(null, E('p', {}, 'Ошибка: ' + e), 'error'); });
		};
		var btnTorOff = E('button', { 'class': 'gs-btn gs-btn-warn', 'click': function() {
			if (!confirm('Выключить слушатель? Клиенты, указавшие его как прокси, потеряют обход VPN-подписки и пойдут по обычным правилам.')) return;
			setListener('off', btnTorOff);
		} }, 'Выключить слушатель');
		var btnTorOn = E('button', { 'class': 'gs-btn' + (torOn ? '' : ' gs-btn-primary'), 'click': function() { setListener('on', btnTorOn); } }, 'Включить слушатель');
		var btnTorUpd = E('button', { 'class': 'gs-btn', 'click': function() {
			btnTorUpd.disabled = true; btnTorUpd.textContent = 'Обновляю…';
			callTorRefresh().then(function(res) {
				btnTorUpd.disabled = false; btnTorUpd.textContent = 'Обновить списки трекеров';
				ui.addNotification(null, E('p', {}, (res && res.changed) ? ('Список пересобран: доменов ' + res.domains + ', IP ' + res.raw_ips) : 'Изменений нет'), 'info');
				return view.reload();
			}).catch(function(e) { btnTorUpd.disabled = false; btnTorUpd.textContent = 'Обновить списки трекеров'; ui.addNotification(null, E('p', {}, 'Ошибка: ' + e), 'error'); });
		} }, 'Обновить списки трекеров');
		var inTorExtra = E('textarea', { 'class': 'gs-text', rows: 3, style: 'width:100%;font-family:monospace' });
		inTorExtra.value = (tor.extras || []).join('\n');
		var btnTorExtra = E('button', { 'class': 'gs-btn', 'click': function() {
			btnTorExtra.disabled = true;
			callTorExtra({ hosts: inTorExtra.value }).then(function(res) {
				btnTorExtra.disabled = false;
				if (res && res.ok) ui.addNotification(null, E('p', {}, 'Сохранено, правил: ' + res.domains), 'info');
				else ui.addNotification(null, E('p', {}, 'Отклонено: ' + ((res && res.rejected || []).join(', ') || 'ошибка')), 'error');
				return view.reload();
			}).catch(function(e) { btnTorExtra.disabled = false; ui.addNotification(null, E('p', {}, 'Ошибка: ' + e), 'error'); });
		} }, 'Сохранить и пересобрать');
		panels.lists.appendChild(E('div', { 'class': 'gs-card' }, [
			E('h3', {}, 'Торренты и трекеры'),
			E('div', { 'class': 'gs-hint', 'style': 'margin:0 0 8px' },
				'Ни одно торрент-соединение не должно идти через обычную VPN-подписку — провайдер это ' +
				'запрещает и 31.10 заблокировал подписку, увидев BitTorrent в туннеле. Клиент указывает ' +
				'прокси на локальный слушатель с жёсткой политикой: торренты уходят в AI-подписку ' +
				'(ограничений на P2P у неё нет), а AI-домены через тот же слушатель попадают в AI — ' +
				'утечка в DIRECT исключена по построению. Дополнительно: домены трекеров закреплены ' +
				'ruleset\'ом (DIRECT), а ответы входящим пирам — правилом SRC-PORT (AI), потому что ' +
				'1300 IP-CIDR хостинговых групп уводят пиров на OVH/Hetzner/DO в VPN.'),
			E('div', { 'class': 'gs-row' }, [
				E('span', { 'class': 'gs-label' }, 'Слушатель'),
				E('span', { 'class': 'gs-badge ' + (torOn ? 'gs-ok' : 'gs-off') },
					torOn ? ('работает :' + (tor.listener_port || 7894) + ' → ' + (tor.listener_policy || 'без политики')) : 'выключен'),
				E('span', { 'class': 'gs-val gs-mono' }, tor.proxy_hint || ''),
				(torOn ? btnTorOff : btnTorOn)
			]),
			E('div', { 'class': 'gs-row' }, [
				E('span', { 'class': 'gs-label' }, 'Список трекеров'),
				E('span', { 'class': 'gs-val gs-mono' }, 'доменов ' + (tor.domains || 0) + ' · IP ' + (tor.raw_ips || 0)),
				E('span', { 'class': 'gs-badge ' + (tor.provider_loaded ? 'gs-ok' : 'gs-off') }, 'в mihomo: ' + (tor.provider_loaded || 0)),
				btnTorUpd
			]),
			E('div', { 'class': 'gs-hint', 'style': 'margin:6px 0 4px' },
				'Свои домены (по строке). Пересобираются вместе с публичными списками: ' +
				((tor.sources || []).join(' , ') || '—')),
			inTorExtra,
			E('div', { 'class': 'gs-row' }, [ btnTorExtra ])
		]));

		/* Isolated per-path service-egress observations; no production apply. */
		panels.nodes.appendChild(E('div', { 'class': 'gs-card' }, [
			E('h3', {}, 'Раздельные фильтры выхода: VPN / VPN-noP2P / AI'),
			E('p', { 'class': 'gs-hint' }, 'Проверка не переключает рабочие узлы. Страна определяется по ответу сайта, не по адресу входного сервера. Сохранение — желаемая настройка, не применение фильтра. Старый ночной сканер ниже работает отдельно.'),
			pathAudit.error ? E('p', {}, 'Ошибка: ' + pathAudit.error) : E('span', {}, '')
		]));
		(pathAudit.paths || []).forEach(function(path) {
			// Keep an unsaved edit per path. Reloading the page (or the automatic reload after
			// a check) used to wipe whatever was typed in the other cards.
			var draftKey = 'gs-draft-' + path.id;
			var draft = '';
			try { draft = localStorage.getItem(draftKey) || ''; } catch (e) { draft = ''; }
			var savedValue = (path.countries_allow || []).join(',');
			var input = E('input', { 'class': 'gs-text', type: 'text', size: 22,
				value: draft || savedValue,
				'input': function() {
					try { localStorage.setItem(draftKey, this.value); } catch (e) { /* ignore */ }
				} });
			var draftHint = draft && draft !== savedValue
				? E('span', { 'class': 'gs-warn' }, 'есть несохранённый ввод')
				: E('span', {});
			var report = path.report || {};
			var status = E('div', { 'class': 'gs-hint' }, report.status ? ('Проверка: ' + report.status + (report.stale ? ' · УСТАРЕЛА' : '')) : 'Ещё не проверялся');
			var save = E('button', { 'class': 'gs-btn', 'click': function() {
				save.disabled = true;
				callNodesPathSave(path.id, input.value.trim().toUpperCase(), pathAudit.revision).then(function(res) {
					if (!res || !res.ok) throw new Error((res || {}).error || 'Сохранение не подтверждено');
					try { localStorage.removeItem(draftKey); } catch (e) { /* ignore */ }
					ui.addNotification(null, E('p', {}, res.changed ? 'Желаемый фильтр сохранён. В mihomo не применён.' : 'Настройки не изменились.'), 'info');
					view.reload();
				}).catch(function(err) { ui.addNotification(null, E('p', {}, String(err)), 'error'); }).finally(function() { save.disabled = false; });
			} }, 'Сохранить настройки');
			var run = E('button', { 'class': 'gs-btn gs-btn-primary', 'click': function() {
				run.disabled = true;
				callNodesPathCheck(path.id).then(function(res) {
					if (!res || !res.ok) throw new Error((res || {}).error || 'Запуск не подтверждён');
					var jobId = res.job_id;
					// Store jobId for polling and cancel
					run._jobId = jobId;
					status.textContent = 'Проверка запущена (job ' + jobId + '). Ожидание результатов...';
					run.style.display = 'none';
					cancel.style.display = 'inline-block';
					view.pollJob(jobId, path.id);
				}).catch(function(err) { ui.addNotification(null, E('p', {}, String(err)), 'error'); }).finally(function() { run.disabled = false; });
			} }, 'Проверить ' + path.label);
			var cancel = E('button', { 'class': 'gs-btn gs-btn-warn', style: 'display:none', 'click': function() {
				var jobId = run._jobId;
				if (jobId) {
					cancel.disabled = true;
					callNodesAuditCancel(jobId).then(function(res) {
						if (res && res.ok) {
							status.textContent = 'Отмена запрошена, ожидание завершения...';
							cancel.style.display = 'none';
							// polling will detect cancelled status and reload
						} else {
							ui.addNotification(null, E('p', {}, 'Отмена не удалась: ' + ((res || {}).error || 'unknown')), 'error');
						}
					}).catch(function(err) { ui.addNotification(null, E('p', {}, String(err)), 'error'); }).finally(function() { cancel.disabled = false; });
				}
			} }, 'Отменить');
			var rows = (report.nodes || []).map(function(node) {
				var facts = Object.keys(node.probes || {}).map(function(service) {
					var proof = node.probes[service];
					return service + ': ' + (proof.country || 'UNKNOWN') + (proof.ip ? ' / ' + proof.ip : '') + (proof.reason ? ' (' + proof.reason + ')' : '');
				}).join(' · ');
				return E('div', { 'class': 'gs-upd-meta' }, node.name + ' — ' + node.status + '\n' + facts);
			});
			panels.nodes.appendChild(E('div', { 'class': 'gs-card' }, [
				E('h3', {}, path.label),
				E('p', { 'class': 'gs-hint' }, 'Источник: ' + ((path.providers || []).join(', ') || 'не определён') + (path.configured ? ' · сохранено' : ' · предлагаемые страны, ещё не сохранены')),
				E('div', { 'class': 'gs-row' }, [E('span', {}, 'Разрешённые страны выхода (ISO-2)'), input, save, run,
					E('button', { 'class': 'gs-btn', 'click': function() { view.reload(); } }, 'Обновить результаты'), draftHint]),
				status,
				E('p', { 'class': 'gs-hint' }, report.error || report.limit || 'HTTP 200/204 Google не означает US. Проверки SOCKS5h не подтверждают путь клиента с локальным hosts.'),
				// What admission actually means for this path, in plain words.
				path.id === 'ai' ? E('p', { 'class': 'gs-hint' },
					'Допуск AI: ChatGPT и Cloudflare. Google не отдаёт страну в машинно-читаемом виде — '
					+ 'его страна показывается как «неизмерима» и в допуске не участвует.') : E('span', {}),
				// Positive state of the filter. Without this the card only ever showed a
				// warning when something was wrong, so "nothing applied" and "applied and
				// healthy" looked identical to the owner.
				(function() {
					var d = path.applied_detail || {};
					var state = d.status || 'never';
					var text, cls = 'gs-hint';
					if (state === 'applied') {
						text = 'Фильтр действует: ' + (d.node_count || 0) + ' проверенных узлов'
							+ (d.applied_at ? ', применён ' + new Date(d.applied_at).toLocaleString() : '')
							+ (d.provider_id ? ' · ' + d.provider_id : '');
					} else if (state === 'applying') {
						// An apply in flight, or one that died between commit and bookkeeping.
						// The group may already be pinned, so this must never read as "not applied".
						text = 'Применение не завершено: пул может быть уже подключён. Проверьте состав '
							+ 'и при необходимости отзовите фильтр.';
						cls = 'gs-warn';
					} else if (state === 'expired') {
						text = 'Фильтр действует, но доказательства просрочены: нужен новый Check и повторное применение';
						cls = 'gs-warn';
					} else if (state === 'revoked') {
						text = 'Фильтр отозван: путь снова использует исходный источник';
					} else {
						text = 'Фильтр не применялся: путь использует исходный источник без сужения';
					}
					// Desired and acting are different questions: say so when they diverge.
					var acting = (d.acting_countries || []).join(',');
					var desired = (path.countries_allow || []).join(',');
					var nodes = [E('p', { 'class': cls }, text)];
					if ((state === 'applied' || state === 'expired') && acting && acting !== desired) {						nodes.push(E('p', { 'class': 'gs-hint' },
							'Желаемые страны: ' + (desired || '—') + ' · действующие: ' + acting));
					}
					return E('div', {}, nodes);
				})(),
				E('div', {}, rows),
				// Revoke is always available while a filter is in force: an owner must be able
				// to undo an apply even when the report has gone stale, which is exactly the
				// state an apply itself creates.
				path.applied && (path.applied_detail || {}).status !== 'revoked' ? E('div', { 'class': 'gs-row', style: 'margin-top: 8px' }, [
					E('button', { 'class': 'gs-btn gs-btn-warn', 'click': function() {
						var btn = this; btn.disabled = true;
						callNodesPathRevoke(path.id).then(function(res) {
							btn.disabled = false;
							if (!res || !res.ok) throw new Error((res || {}).error || 'Отзыв не подтверждён');
							ui.addNotification(null, E('p', {}, res.revoked ? 'Фильтр отозван, исходный источник и фильтр группы восстановлены' : 'Активного фильтра нет'), 'info');
							view.reload();
						}).catch(function(err) { btn.disabled = false; ui.addNotification(null, E('p', {}, String(err)), 'error'); });
					} }, 'Отозвать фильтр')
				]) : E('span', {}),
				// Preview and Apply section
				report.status === 'complete' && !report.stale && report.counts && report.counts.eligible > 0 ? E('div', { 'class': 'gs-row', style: 'margin-top: 8px; padding-top: 8px; border-top: 1px solid var(--border-color,#30363d)' }, [
					E('button', { 'class': 'gs-btn', 'click': function() {
						var btn = this; btn.disabled = true;
						callNodesPathPreview(path.id, report.job_id, report.settings_revision, report.source_revision).then(function(res) {
							btn.disabled = false;
							if (!res || !res.ok) throw new Error((res || {}).error || 'Предпросмотр не подтверждён');
							// Show preview in notification
							var msg = 'Предпросмотр: ' + res.eligible_count + ' подходят, ' + res.excluded_count + ' исключены, ' + res.unknown_count + ' неизвестно.';
							if (res.to_add && res.to_add.length) msg += ' Добавится: ' + res.to_add.join(', ');
							if (res.to_remove && res.to_remove.length) msg += ' Удалится: ' + res.to_remove.join(', ');
							if (res.warnings && res.warnings.length) msg += ' ⚠ ' + res.warnings.join('; ');
							ui.addNotification(null, E('p', {}, msg), 'info');
							// Store preview_id for apply
							path._preview_id = res.preview_id;
						}).catch(function(err) {
							btn.disabled = false;
							// A check that finished after this page was drawn replaces the
							// report, so the card holds a dead job id. Refresh instead of
							// leaving the owner with an unexplained refusal.
							if (/Report ID mismatch|Report not found|Report not complete/i.test(String(err))) {
								ui.addNotification(null, E('p', {},
									'Отчёт обновился после загрузки страницы. Данные перечитаны — '
									+ 'нажмите «Предпросмотр» ещё раз.'), 'warning');
								view.reload();
								return;
							}
							ui.addNotification(null, E('p', {}, String(err)), 'error');
						});
					} }, 'Предпросмотр'),
					E('button', { 'class': 'gs-btn gs-btn-primary', 'click': function() {
						var btn = this; 
						var previewId = path._preview_id;
						if (!previewId) { ui.addNotification(null, E('p', {}, 'Сначала сделайте предпросмотр'), 'error'); return; }
						btn.disabled = true;
						callNodesPathApply(path.id, previewId, report.settings_revision).then(function(res) {
							btn.disabled = false;
							if (!res || !res.ok) throw new Error((res || {}).error || 'Применение не подтверждено');
							ui.addNotification(null, E('p', {}, 'Фильтр применён к трафику (' + (res.tx_id || '') + ')'), 'info');
							view.reload();
						}).catch(function(err) { btn.disabled = false; ui.addNotification(null, E('p', {}, String(err)), 'error'); });
					} }, 'Применить фильтр'),
				]) : E('span', {}),
				// History section
				E('div', { 'class': 'gs-row', style: 'margin-top: 8px; padding-top: 8px; border-top: 1px solid var(--border-color,#30363d)' }, [
					E('button', { 'class': 'gs-btn', 'click': function() {
						var btn = this; btn.disabled = true;
						callNodesAuditEventsGet(path.id, null, 10).then(function(res) {
							btn.disabled = false;
							if (!res || !res.ok) throw new Error((res || {}).error || 'История не загружена');
							var msg = 'История (' + res.events.length + '):\n' + res.events.map(function(e) {
								var ts = e.timestamp;
								var t;
								if (ts) {
									t = new Date(ts).toLocaleString();
								} else if (e.event_id) {
									// Fallback: extract ISO timestamp from event_id prefix (format: 2026-11-09T14:16:28.149342+03:00-xxx)
									var m = e.event_id.match(/^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)/);
									if (m) t = new Date(m[1]).toLocaleString();
								}
								t = t || 'Время неизвестно';
								return t + ' [' + e.action + '] ' + (e.result || '') + (e.revoked_nodes ? ' · отозвано: ' + e.revoked_nodes.join(', ') : '');
							}).join('\n');
							ui.addNotification(null, E('p', { 'style': 'white-space: pre-wrap; max-height: 300px; overflow: auto' }, msg), 'info');
						}).catch(function(err) { btn.disabled = false; ui.addNotification(null, E('p', {}, String(err)), 'error'); });
					} }, 'История')
				]),
				// Drift detection
				report.status === 'complete' && !report.stale && path.applied && path.applied_detail && path.applied_detail.runtime_verified === false ? E('div', { 'class': 'gs-row gs-warn', style: 'margin-top: 8px; padding: 8px; background: #d2992222; border: 1px solid #d29922; border-radius: 6px' }, [
					E('span', { 'class': 'gs-warn' }, '⚠ Применённый фильтр не подтверждён на работающем mihomo.'),
					E('button', { 'class': 'gs-btn', 'click': function() { view.reload(); } }, 'Перезагрузить')
				]) : E('span', {}),
				// Expired evidence: the filter is still narrowing the pool, it was not widened.
				path.applied_detail && path.applied_detail.status === 'expired' ? E('div', { 'class': 'gs-row', style: 'margin-top: 8px; padding: 8px; background: #d2992222; border: 1px solid #d29922; border-radius: 6px' }, [
					E('span', { 'class': 'gs-warn' }, '⏳ Срок давности проверки истёк. Фильтр продолжает действовать и путь не расширен: нужен новый Check и повторное применение.'),
					E('button', { 'class': 'gs-btn', 'click': function() {
						var btn = this; btn.disabled = true;
						callNodesPathCheck(path.id).then(function(res) {
							btn.disabled = false;
							if (!res || !res.ok) throw new Error((res || {}).error || 'Проверка не запущена');
							view.reload();
						}).catch(function(err) { btn.disabled = false; ui.addNotification(null, E('p', {}, String(err)), 'error'); });
					} }, 'Проверить сейчас')
				]) : E('span', {}),
				// Schedule section
				(function() {
					var sched = path.schedule || {};
					var inTime = E('input', { 'class': 'gs-text', type: 'text', value: sched.time || '03:30', size: 6 });
					var inMode = E('select', { 'class': 'gs-text' }, [
						E('option', { value: 'check-only' }, 'Только проверка'),
						E('option', { value: 'check-and-apply' }, 'Проверка и применение')
					]);
					inMode.value = sched.mode || 'check-only';
					var chkEn = E('input', { 'class': 'gs-check', type: 'checkbox' });
					chkEn.checked = sched.enabled === true;

					function persist() {
						callNodesAuditScheduleSet(path.id, chkEn.checked ? 'true' : 'false',
							inTime.value.trim(), inMode.value).then(function(res) {
							if (!res || !res.ok) {
								ui.addNotification(null, E('p', {}, 'Ошибка: ' + ((res || {}).error || 'unknown')), 'error');
								return;
							}
							ui.addNotification(null, E('p', {}, 'Расписание сохранено'), 'info');
							view.reload();
						}).catch(function(err) {
							ui.addNotification(null, E('p', {}, String(err)), 'error');
						});
					}

					return E('div', { 'class': 'gs-row', style: 'margin-top: 8px; padding-top: 8px; border-top: 1px solid var(--border-color,#30363d)' }, [
						E('span', { 'class': 'gs-label' }, 'Расписание проверки'),
						E('label', { 'class': 'gs-check' }, [chkEn, E('span', {}, 'включено')]),
						E('span', { 'class': 'gs-label' }, 'Время (ЧЧ:ММ)'), inTime,
						E('span', { 'class': 'gs-label' }, 'Режим'), inMode,
						E('button', { 'class': 'gs-btn', 'click': persist }, 'Сохранить расписание')
					]);
				})()
			]));
		});

		/* ===================== Legacy node audit (separate server-country policy) ===================== */
		var conf = nd.conf || {};
		var chkEn = E('input', { type: 'checkbox' }); chkEn.checked = (nd.enabled !== false);
		var inSched = E('input', { 'class': 'gs-text', type: 'text', value: nd.schedule || '03:30', size: 6 });
		var inCC = E('input', { 'class': 'gs-text', type: 'text', value: conf.bad_countries || '', size: 14 });
		var inAS = E('input', { 'class': 'gs-text', type: 'text', value: conf.bad_asns || '', size: 24 });
		var inBase = E('input', { 'class': 'gs-text', type: 'text', value: conf.base_filter || '', size: 40 });
		var inProv = E('input', { 'class': 'gs-text', type: 'text', value: conf.sub_provider || 'sub', size: 8 });
		var btnSave = E('button', { 'class': 'gs-btn gs-btn-primary', 'click': function() {
			btnSave.disabled = true; btnSave.textContent = 'Сохранение…';
			callNodesSet({
				enabled: chkEn.checked ? '1' : '0',
				schedule: inSched.value.trim(), countries: inCC.value.trim(), asns: inAS.value.trim(),
				base: inBase.value.trim(), provider: inProv.value.trim()
			}).then(function(res) {
				btnSave.disabled = false; btnSave.textContent = 'Сохранить';
				if (res && res.error) { ui.addNotification(null, E('p', {}, 'Ошибка: ' + res.error), 'error'); return; }
				ui.addNotification(null, E('p', {}, 'Настройки аудита сохранены'), 'info');
				return view.reload();
			}).catch(function(e) { btnSave.disabled = false; btnSave.textContent = 'Сохранить'; ui.addNotification(null, E('p', {}, 'Ошибка: ' + e), 'error'); });
		} }, 'Сохранить');
		var btnRun = E('button', { 'class': 'gs-btn', 'click': function() {
			btnRun.disabled = true; btnRun.textContent = 'Запущен…';
			callNodescan().then(function() {
				ui.addNotification(null, E('p', {}, 'Аудит запущен (до ~1 минуты), затем фильтр применится сам'), 'info');
				setTimeout(function() { view.reload(); }, 45000);
			}).catch(function(e) { btnRun.disabled = false; ui.addNotification(null, E('p', {}, 'Ошибка: ' + e), 'error'); });
		} }, 'Проверить сейчас');
		var nst = nd.state || {};
		var blocked = nd.blocked || [];
		panels.nodes.appendChild(E('div', { 'class': 'gs-card' }, [
			E('h3', {}, 'Ночной аудит узлов подписки'),
			E('div', { 'class': 'gs-hint', 'style': 'margin:0 0 8px' },
				'Сканер скачивает подписку, проверяет адрес каждого сервера по гео-реестрам (страна и владелец сети), ' +
				'и собирает exclude-filter группы VPN: узлы с «плохой» сетью блокируются точным именем, чистые — разблокируются. ' +
				'Трафик при этом не переключается; конфиг применяется только после успешной проверки mihomo -t.'),
			E('div', { 'class': 'gs-row' }, [
				E('span', { 'class': 'gs-label' }, 'Последний прогон'),
				E('span', { 'class': 'gs-val' }, nst.ts ? (nst.ts + ' · узлов ' + nst.total + ' · заблокировано ' + nst.blocked
					+ (nst.errors ? ' · сбоев проверки ' + nst.errors : '') + (nst.changed ? ' · фильтр обновлён' : ''))
					: 'ещё не запускался')
			]),
			E('div', { 'class': 'gs-row' }, [
				E('label', { 'class': 'gs-check' }, [chkEn, E('span', {}, 'Аудит по расписанию (ночью)')]),
				E('span', { 'class': 'gs-label' }, 'Время (ЧЧ:ММ)'), inSched, btnRun
			]),
			E('div', { 'class': 'gs-row' }, [
				E('span', { 'class': 'gs-label' }, 'Плохие страны'), inCC,
				E('span', { 'class': 'gs-label' }, 'Плохие сети (ASN)'), inAS,
				E('span', { 'class': 'gs-label' }, 'Провайдер'), inProv
			]),
			E('div', { 'class': 'gs-row' }, [
				E('span', { 'class': 'gs-label' }, 'Базовый фильтр имён'), inBase, btnSave
			]),
			E('div', { 'class': 'gs-hint' }, 'Страны: AA,BB (ISO-2). ASN: AS123456,AS234. Без кавычек и обратных слэшей.'),
			E('div', { 'class': 'gs-blocklist' }, blocked.length ? blocked.join('\n') : 'заблокированных узлов нет'),
			E('div', { 'class': 'gs-upd-meta' }, (nd.log || []).join('\n'))
		]));

		/* ===================== Settings tab ===================== */
		var btnPin = stk.pin
			? E('button', { 'class': 'gs-btn gs-btn-warn', 'click': function() {
				if (!confirm('Снять запрет рестарта mihomo? Панельные операции с --restart снова станут возможны.')) return;
				btnPin.disabled = true;
				callPinSet({ state: 'off' }).then(function() { view.reload(); }).catch(function() { btnPin.disabled = false; });
			} }, 'Снять пин')
			: E('button', { 'class': 'gs-btn', 'click': function() {
				btnPin.disabled = true;
				callPinSet({ state: 'on' }).then(function() { view.reload(); }).catch(function() { btnPin.disabled = false; });
			} }, 'Запретить рестарты');
		var K = stk.links || {};
		function linkRow(label, url) {
			return E('div', { 'class': 'gs-row' }, [
				E('span', { 'class': 'gs-label' }, label),
				url ? E('a', { 'href': url, 'target': '_blank', 'class': 'gs-mono' }, url) : E('span', {}, '—')
			]);
		}
		panels.set.appendChild(E('div', { 'class': 'gs-card' }, [
			E('h3', {}, 'Прочее'),
			E('div', { 'class': 'gs-row' }, [
				E('span', { 'class': 'gs-label' }, 'Пин «не рестартить mihomo»'),
				E('span', { 'class': 'gs-badge ' + (stk.pin ? 'gs-on' : 'gs-off') }, stk.pin ? 'установлен' : 'снят'),
				btnPin
			]),
			E('div', { 'class': 'gs-hint' }, 'С пином безопасные операции панели не трогают живой VPN-трафик рестартами. Снимайте только осознанно.'),
			linkRow('MagiTrickle (группы доменов)', K.magitrickle),
			linkRow('Zapret DPI (стратегии)', K.zapret),
			linkRow('Лендинг портов', K.landing),
			linkRow('LuCI', K.luci),
			E('div', { 'class': 'gs-row' }, [
				E('span', { 'class': 'gs-label' }, 'Версия панели'),
				E('span', { 'class': 'gs-val gs-mono' }, PANEL_VERSION)
			])
		]));

		return wrap;
	},

	act: function(rpcFn, params, okMsg) {
		var view = this;
		return rpcFn(params).then(function(res) {
			if (res && res.error) { ui.addNotification(null, E('p', {}, 'Ошибка: ' + res.error), 'error'); return; }
			if (okMsg) ui.addNotification(null, E('p', {}, okMsg), 'info');
			return view.reload();
		}).catch(function(e) {
			ui.addNotification(null, E('p', {}, 'Ошибка: ' + e), 'error');
		});
	},

	reload: function() {
		var view = this;
		var calls = [
			callStatus(), callLeases(), callLog(), callAiSubGet(), callSubsList(),
			callUpdCheck({ force: null }), callUpdStatus(), callBackups(), callStack(),
			callNodesGet(), callListsGet(), callYtGet(), callTorGet(), callNodesPathsGet(), callModList(), callDiagList()
		];
		return Promise.all(calls).then(function(all) {
			var newView = view.render(all);
			var old = document.querySelector('.gs-wrap');
			if (old && old.parentNode) old.parentNode.replaceChild(newView, old);
		});
	},

	// Poll job status until complete.
	//
	// One timer per path, kept outside the view instance: reload() builds a new view, so a
	// timer owned by the old one survived and kept polling -- several clicks, or a couple of
	// reloads, produced a swarm of overlapping requests and one "interrupted" notice each.
	// A transient timeout is retried a few times before it is reported at all.
	pollJob: function(jobId, pathId) {
		var view = this;
		if (POLL_TIMERS[pathId]) {
			clearInterval(POLL_TIMERS[pathId].timer);
			delete POLL_TIMERS[pathId];
		}
		var state = { jobId: jobId, failures: 0, timer: null };

		function stop() {
			if (state.timer !== null) {
				clearInterval(state.timer);
				state.timer = null;
			}
			if (POLL_TIMERS[pathId] === state) delete POLL_TIMERS[pathId];
		}

		state.timer = setInterval(function() {
			callNodesAuditJobGet(jobId).then(function(res) {
				if (!res || !res.ok) {
					var error = (res || {}).error || 'ошибка RPC';
					stop();
					// A newer check replaced this job file; the card already shows it.
					if (error !== 'job not found') {
						ui.addNotification(null, E('p', {}, 'Наблюдение прервано: ' + error), 'warning');
					}
					return;
				}
				state.failures = 0;
				var job = res.job || {};
				var st = job.status;
				if (st === 'complete' || st === 'error' || st === 'stale' || st === 'cancelled') {
					stop();
					var msg, kind = 'info';
					if (st === 'complete') {
						var counts = job.counts || {};
						msg = 'Проверка завершена: ' + (counts.eligible || 0) + ' допущено, '
							+ (counts.excluded || 0) + ' исключено, '
							+ (counts.unknown || 0) + ' неизвестно.';
					} else if (st === 'stale') {
						var staleCounts = job.counts || {};
						msg = 'Проверка завершена, но результат уже устарел (конфиг изменился): '
							+ (staleCounts.eligible || 0) + ' допущено, '
							+ (staleCounts.excluded || 0) + ' исключено, '
							+ (staleCounts.unknown || 0) + ' неизвестно. Нужна повторная проверка.';
						kind = 'warning';
					} else if (st === 'cancelled') {
						msg = 'Проверка отменена.';
						kind = 'warning';
					} else {
						msg = 'Проверка завершилась ошибкой: ' + (job.error || 'причина не указана');
						kind = 'error';
					}
					ui.addNotification(null, E('p', {}, msg), kind);
					view.reload();
				}
			}).catch(function(err) {
				state.failures += 1;
				if (state.failures < 3) return;
				stop();
				ui.addNotification(null, E('p', {}, 'Наблюдение прервано: ' + String(err)
					+ '. Нажмите «Обновить результаты».'), 'warning');
			});
		}, 2000);
		POLL_TIMERS[pathId] = state;
	}
});
