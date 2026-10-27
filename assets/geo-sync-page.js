'use strict';
'require view';
'require rpc';
'require ui';

var callStatus = rpc.declare({ object: 'geo-sync', method: 'status', expect: {} });
var callLeases = rpc.declare({ object: 'geo-sync', method: 'leases', expect: {} });
var callLog    = rpc.declare({ object: 'geo-sync', method: 'log',    expect: {} });
var callBackups = rpc.declare({ object: 'geo-sync', method: 'backups', expect: {} });
var callLists  = rpc.declare({ object: 'geo-sync', method: 'sync_lists', expect: {} });
var callGoogle = rpc.declare({ object: 'geo-sync', method: 'google',  expect: {} });
/* params as OBJECT (not array): array form expects positional args and would
   wrap {mode:...} into {"mode":{...}} breaking rpcd json_get_var */
var callGlobal = rpc.declare({ object: 'geo-sync', method: 'global',  params: { mode: null }, expect: {} });
var callIpv6   = rpc.declare({ object: 'geo-sync', method: 'ipv6',    params: { state: null }, expect: {} });
var callDevAdd = rpc.declare({ object: 'geo-sync', method: 'dev_add', params: { ip: null, mode: null, mac: null, name: null }, expect: {} });
var callDevDel = rpc.declare({ object: 'geo-sync', method: 'dev_del', params: { ip: null }, expect: {} });
var callAiSubGet = rpc.declare({ object: 'geo-sync', method: 'ai_sub_get', expect: {} });
var callAiSubSet = rpc.declare({ object: 'geo-sync', method: 'ai_sub_set', params: { url: null }, expect: {} });
var callSubsList = rpc.declare({ object: 'geo-sync', method: 'subs_list', expect: {} });
var callSubsSeed = rpc.declare({ object: 'geo-sync', method: 'subs_seed', expect: {} });
var callSubAdd   = rpc.declare({ object: 'geo-sync', method: 'sub_add',   params: { name: null, url: null }, expect: {} });
var callSubDel   = rpc.declare({ object: 'geo-sync', method: 'sub_del',   params: { id: null }, expect: {} });
var callAiSubUse   = rpc.declare({ object: 'geo-sync', method: 'ai_sub_use',   params: { id: null }, expect: {} });
var callVpnSubUse  = rpc.declare({ object: 'geo-sync', method: 'vpn_sub_use',  params: { id: null }, expect: {} });
var callUpdCheck   = rpc.declare({ object: 'geo-sync', method: 'upd_check',    params: { force: null }, expect: {} });
var callUpdStatus  = rpc.declare({ object: 'geo-sync', method: 'upd_status',   expect: {} });
var callUpdApply   = rpc.declare({ object: 'geo-sync', method: 'upd_apply',    expect: {} });
var PANEL_VERSION = '@@PANEL_VERSION@@';

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
	'.gs-subs td:first-child{width:110px;font-weight:600}',
	'.gs-subs td:nth-child(2){font-family:monospace;font-size:12px;color:#8b949e}',
	'.gs-subs td:nth-child(3){width:110px}',
	'.gs-subs td:last-child{width:200px;white-space:nowrap}',
	'.gs-subs .gs-btn-use{background:#21262d;border-color:#30363d;color:#c9d1d9;padding:5px 10px;margin-right:4px}',
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
	'.gs-bk{margin-top:10px;border-top:1px solid #30363d;padding-top:6px}',
	'.gs-bk h4{margin:2px 0 4px;font-size:13px;color:#e6edf3}'
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
function ccBadge(cc) {
	return E('span', { 'class': 'gs-cc' }, cc || '—');
}

var E = (function(raw) {
	return function() {
		var a = Array.prototype.slice.call(arguments), i = a.length - 1;
		if (i >= 1 && (typeof a[i] === 'string' || typeof a[i] === 'number')) a[i] = [ String(a[i]) ];
		return raw.apply(null, a);
	};
})(window.E);

return view.extend({
	load: function() {
		injectStyle();
		return Promise.all([ callStatus(), callLeases(), callLog(), callAiSubGet(), callSubsList(),
			callUpdCheck({ force: null }), callUpdStatus(), callBackups() ]);
	},

	render: function(all) {
		var st = all[0] || {}, leases = (all[1] || {}).leases || [], lg = (all[2] || {}).log || '';
		var aicfg = all[3] || {};
		var subs = (all[4] || {}).subs || [];
		var upd = all[5] || {};
		var upds = all[6] || {};
		var bks = all[7] || {};
		var view = this;
		if (view._updTimer) { clearInterval(view._updTimer); view._updTimer = null; }
		var wrap = E('div', { 'class': 'gs-wrap' });

		/* ===================== HERO: dual subscription ===================== */
		var aiCc = st.ai_cc || '', vpnCc = st.vpn_cc || '';
		var aiSub = E('div', { 'class': 'gs-sub-card' }, [
			E('div', { 'class': 'gs-ent' }, 'ИИ · Google / OpenAI / Claude → AI-подписка'),
			E('div', { 'class': 'gs-node' }, [ ccBadge(aiCc), cleanName(st.ai_node) || '—' ]),
			E('div', { 'class': 'gs-meta' }, 'Подписка de5 · выход ' + ((CCNAME[aiCc] || aiCc) ? (CCNAME[aiCc] || aiCc) : '—') +
				(st.google ? ' · Google видит: ' + st.google : '')),
			E('div', { 'class': 'gs-meta' }, 'DNS AI-устройств: ' + (
				st.aidns === 1 ? 'через AI-туннель (утечки нет)' :
				st.aidns === 2 ? 'ПРОБЛЕМА — не через туннель (проверьте шаг aidns)' : 'не настроено (утечка DNS!)'))
		]);
		var vpnSub = E('div', { 'class': 'gs-sub-card' }, [
			E('div', { 'class': 'gs-ent' }, 'Общая VPN · всё магитрикле (не-ИИ)'),
			E('div', { 'class': 'gs-node' }, [ ccBadge(vpnCc), cleanName(st.vpn_node) || '—' ]),
			E('div', { 'class': 'gs-meta' }, 'Подписка vlessi · выход ' + ((CCNAME[vpnCc] || vpnCc) ? (CCNAME[vpnCc] || vpnCc) : '—'))
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
			E('div', { 'class': 'gs-duo' }, [ aiSub, vpnSub ]),
			counters
		]);
		wrap.appendChild(hero);

		/* ===================== Subscriptions registry ===================== */
		function maskUrl(u) {
			if (!u) return '—';
			return String(u).replace(/\/[^/]*$/, '/...');
		}
		function subRow(s) {
			var isVpn = !!(st.vpn_sub && st.vpn_sub === s.id);
			var isAi = !!(st.ai_sub && st.ai_sub === s.id);
			var dest = '—';
			if (isVpn && isAi) dest = E('span', { 'class': 'gs-badge gs-on' }, 'VPN + AI');
			else if (isVpn) dest = E('span', { 'class': 'gs-badge gs-on' }, 'VPN');
			else if (isAi) dest = E('span', { 'class': 'gs-badge gs-ok' }, 'AI');
			var bV = { 'class': 'gs-btn gs-btn-use' + (isVpn ? ' gs-btn-vpn-on' : ''), 'title': isVpn ? 'Эта подписка используется для общего VPN' : 'Переключить общий VPN на эту подписку', 'click': function() { view.act(callVpnSubUse, { id: s.id }, 'Подписка применена для VPN'); } };
			var bA = { 'class': 'gs-btn gs-btn-use' + (isAi ? ' gs-btn-ai-on' : ''), 'title': isAi ? 'Эта подписка используется для AI-маршрутов' : 'Переключить AI-маршруты на эту подписку', 'click': function() { view.act(callAiSubUse, { id: s.id }, 'Подписка применена для AI'); } };
			var bD = { 'class': 'gs-btn gs-btn-warn', 'title': 'Убрать из реестра', 'click': function() { view.act(callSubDel, { id: s.id }, 'Подписка удалена'); } };
			return E('tr', {}, [
				E('td', {}, s.name || s.id),
				E('td', { 'class': 'gs-subs-url' }, maskUrl(s.url)),
				E('td', {}, dest),
				E('td', { 'class': 'gs-subs-actions' }, [
					E('button', bV, '→ VPN'),
					E('button', bA, '→ AI'),
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
				'Реестр подписок. В колонке «Назначение» показано, чем подписка занята СЕЙЧАС; кнопки «→ VPN»/«→ AI» переключают соответствующий маршрут на эту подписку (активная кнопка неактивна). Переключение — через API mihomo, без перезапуска и обрыва соединений.'),
			E('table', { 'class': 'gs-table gs-subs' },
				[ E('tr', {}, [ E('th', {}, 'Имя'), E('th', {}, 'Ссылка'), E('th', {}, 'Назначение'), E('th', {}, '') ]) ]
					.concat(subs.map(subRow))),
			E('div', { 'class': 'gs-row', 'style': 'margin-top:10px' }, [ subNameIn, subUrlIn, btnSubAdd ]),
			E('div', { 'class': 'gs-row' }, [ btnSubSeed ])
		]);
		wrap.appendChild(subsCard);

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

		var syncCard = E('div', { 'class': 'gs-card' }, [
			E('h3', {}, 'Синхронизация и проверка'),
			E('div', { 'class': 'gs-hint', 'style': 'margin:0 0 6px' },
				'Списки «Google+AI to DE» в магитрикле автоматически уходят в mihomo каждые 5 минут. Кнопка ниже применяет изменения сразу, не дожидаясь cron.'),
			E('div', { 'class': 'gs-actions' }, [ btnLists, btnGoogle ]),
			listsOut
		]);
		wrap.appendChild(syncCard);

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
		wrap.appendChild(routeCard);

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
		wrap.appendChild(v6Card);

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
		wrap.appendChild(devCard);

		/* ===================== Log ===================== */
		var logCard = E('div', { 'class': 'gs-card' }, [
			E('h3', {}, 'Журнал'),
			E('pre', { 'class': 'gs-log' }, lg || '(пусто)')
		]);
		wrap.appendChild(logCard);

		/* ===================== Updates (bottom - rarely used block) ===================== */
		var updStatus = E('div', { 'class': 'gs-upd-status' });
		var updLog = E('pre', { 'class': 'gs-console' });
		updLog.style.display = 'none';
		var lastLogKey = null;
		function updLineClass(l) {
			if (l.indexOf('[error]') >= 0) return 'c-err';
			if (l.indexOf('[done]') >= 0) return 'c-done';
			if (/\[(queued|backup|download|validate|apply)\]/.test(l)) return 'c-run';
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
			E('h3', {}, 'Обновления'),
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
			updChildren.push(verRow('geomask (ядро)', coreV,
				(latV && coreV !== 'none' && coreV !== latV && !upd.need) ? E('span', { 'class': 'gs-upd-meta' }, 'локальная новее релиза') : null));
			updChildren.push(verRow('Админка LuCI', loc.panel,
				(loc.panel && loc.backend && loc.panel !== loc.backend) ? E('span', { 'class': 'gs-badge gs-warn' }, 'панель≠бэкенд') : null));
			updChildren.push(verRow('geomask-guard', loc.guard));
			updChildren.push(E('div', { 'class': 'gs-row' }, [
				E('span', { 'class': 'gs-label' }, 'Доступно на GitHub'),
				latV ? E('span', { 'class': 'gs-val gs-upd-latest' }, latTag) : E('span', {}, '—'),
				upd.need === true
					? E('span', { 'class': 'gs-badge gs-warn' }, 'доступно обновление')
					: E('span', { 'class': 'gs-badge gs-ok' }, 'актуально')
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
		updChildren.push(bkBox);
		updChildren.push(updStatus);
		updChildren.push(updLog);
		var updCard = E('div', { 'class': 'gs-card' }, updChildren);
		wrap.appendChild(updCard);
		updRenderLines((upds || {}).lines);
		if (UPD_RUNNING.indexOf((upds || {}).phase) >= 0) {
			updStatus.style.color = '#d29922';
			updStatus.textContent = 'Идет обновление… подробности в консоли ниже.';
			updPoll();
		}

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
		return Promise.all([ callStatus(), callLeases(), callLog(), callAiSubGet(), callSubsList(),
			callUpdCheck({ force: null }), callUpdStatus(), callBackups() ]).then(function(all) {
			var newView = view.render(all);
			var old = document.querySelector('.gs-wrap');
			if (old && old.parentNode) old.parentNode.replaceChild(newView, old);
		});
	}
});
