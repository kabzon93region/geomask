'use strict';
'require view';
'require rpc';
'require ui';

var callStatus = rpc.declare({ object: 'geo-sync', method: 'status', expect: {} });
var callLeases = rpc.declare({ object: 'geo-sync', method: 'leases', expect: {} });
var callLog    = rpc.declare({ object: 'geo-sync', method: 'log',    expect: {} });
var callLists  = rpc.declare({ object: 'geo-sync', method: 'sync_lists', expect: {} });
var callGoogle = rpc.declare({ object: 'geo-sync', method: 'google',  expect: {} });
/* params as OBJECT (not array): array form expects positional args and would
   wrap {mode:...} into {"mode":{...}} breaking rpcd json_get_var */
var callGlobal = rpc.declare({ object: 'geo-sync', method: 'global',  params: { mode: null }, expect: {} });
var callIpv6   = rpc.declare({ object: 'geo-sync', method: 'ipv6',    params: { state: null }, expect: {} });
var callDevAdd = rpc.declare({ object: 'geo-sync', method: 'dev_add', params: { ip: null, mode: null, mac: null, name: null }, expect: {} });
var callDevDel = rpc.declare({ object: 'geo-sync', method: 'dev_del', params: { ip: null }, expect: {} });

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
	'.gs-input{background:#0d1117;border:1px solid #30363d;color:#c9d1d9;border-radius:6px;padding:6px 9px;font-size:13px}',
	'.gs-hint{color:#8b949e;font-size:12px;margin-top:6px}',
	'.gs-out{background:#0d1117;color:#8b949e;font-family:monospace;font-size:12px;padding:9px 11px;border-radius:7px;margin-top:10px;white-space:pre-wrap;max-height:130px;overflow:auto}'
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
		return Promise.all([ callStatus(), callLeases(), callLog() ]);
	},

	render: function(all) {
		var st = all[0] || {}, leases = (all[1] || {}).leases || [], lg = (all[2] || {}).log || '';
		var view = this;
		var wrap = E('div', { 'class': 'gs-wrap' });

		/* ===================== HERO: dual subscription ===================== */
		var aiCc = st.ai_cc || '', vpnCc = st.vpn_cc || '';
		var aiSub = E('div', { 'class': 'gs-sub-card' }, [
			E('div', { 'class': 'gs-ent' }, 'ИИ · Google / OpenAI / Claude → Германия'),
			E('div', { 'class': 'gs-node' }, [ ccBadge(aiCc), cleanName(st.ai_node) || '—' ]),
			E('div', { 'class': 'gs-meta' }, 'Подписка de5 · выход ' + ((CCNAME[aiCc] || aiCc) ? (CCNAME[aiCc] || aiCc) : '—') +
				(st.google ? ' · Google видит: ' + st.google : ''))
		]);
		var vpnSub = E('div', { 'class': 'gs-sub-card' }, [
			E('div', { 'class': 'gs-ent' }, 'Общая VPN · всё магитрикле (не-ИИ)'),
			E('div', { 'class': 'gs-node' }, [ ccBadge(vpnCc), cleanName(st.vpn_node) || '—' ]),
			E('div', { 'class': 'gs-meta' }, 'Подписка vlessi · выход ' + ((CCNAME[vpnCc] || vpnCc) ? (CCNAME[vpnCc] || vpnCc) : '—'))
		]);

		var counters = E('div', { 'class': 'gs-counters' }, [
			E('div', { 'class': 'gs-counter' }, [ E('div', { 'class': 'gs-num' }, String(st.ai_rules != null ? st.ai_rules : '—')),
				E('div', { 'class': 'gs-cap' }, 'правил ИИ-пути (ai.yaml → Германия)') ]),
			E('div', { 'class': 'gs-counter' }, [ E('div', { 'class': 'gs-num' }, String(st.mt_rules != null ? st.mt_rules : '—')),
				E('div', { 'class': 'gs-cap' }, 'правил общего пути (mtrickle.yaml → VPN)') ]),
			E('div', { 'class': 'gs-counter' }, [ E('div', { 'class': 'gs-num' }, String(st.n_bypass != null ? st.n_bypass : (st.devices || []).length)),
				E('div', { 'class': 'gs-cap' }, 'мимо VPN (реальный IP)') ])
		]);

		var hero = E('div', { 'class': 'gs-hero' }, [
			E('h2', {}, 'Гео-маскировка и маршрутизация AI'),
			E('div', { 'class': 'gs-sub' }, 'Google и ИИ-сервисы идут через немецкую подписку (Google видит Германию), всё остальное — через общую VPN.'),
			E('div', { 'class': 'gs-duo' }, [ aiSub, vpnSub ]),
			counters
		]);
		wrap.appendChild(hero);

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
				hint: 'IP маскирован через mihomo, но часовой пояс самого ПК не трогается (GeoSync.ps1 на этом ПК выключен).' }
		};
		var modeSel = E('select', { 'class': 'gs-input', 'style': 'width:180px' }, [
			E('option', { 'value': 'bypass' }, 'Мимо VPN (реальный IP)'),
			E('option', { 'value': 'realtz' }, 'Через VPN, часовой пояс ПК свой')
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
				'По умолчанию все устройства маскируются: IP идёт через mihomo, а часовой пояс каждого ПК настраивает его ОС (скрипт GeoSync.ps1). ' +
				'Роутер часовой пояс ПК не меняет. Здесь задаются исключения по IP-маршрутизации (мимо VPN / через VPN).'),
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
		return Promise.all([ callStatus(), callLeases(), callLog() ]).then(function(all) {
			var newView = view.render(all);
			var old = document.querySelector('.gs-wrap');
			if (old && old.parentNode) old.parentNode.replaceChild(newView, old);
		});
	}
});
