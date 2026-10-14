// Общий компонент сканирования: поле в фокусе, зелёный/красный ответ, звук,
// последние 10 сканов, камера и честное сообщение при потере связи (SPEC §6, §11).
(function (global) {
	'use strict';

	var SCAN_TIMEOUT_MS = 5000;
	var HISTORY_LIMIT = 10;

	function beep(ok) {
		try {
			var Ctx = global.AudioContext || global.webkitAudioContext;
			if (!Ctx) return;
			var ctx = beep.ctx || (beep.ctx = new Ctx());
			if (ctx.state === 'suspended') ctx.resume();
			var osc = ctx.createOscillator();
			var gain = ctx.createGain();
			osc.type = ok ? 'sine' : 'square';
			osc.frequency.value = ok ? 1320 : 220;
			gain.gain.value = 0.12;
			osc.connect(gain);
			gain.connect(ctx.destination);
			osc.start();
			osc.stop(ctx.currentTime + (ok ? 0.09 : 0.32));
		} catch (e) {
			// звук не критичен
		}
	}

	function ScanScreen(options) {
		this.url = options.url;
		this.form = document.getElementById(options.formId || 'scan-form');
		this.input = document.getElementById(options.inputId || 'scan-input');
		this.result = document.getElementById(options.resultId || 'scan-result');
		this.history = document.getElementById(options.historyId || 'scan-history');
		this.csrf = options.csrf;
		this.extra = options.extra || function () { return {}; };
		this.onSuccess = options.onSuccess || function () {};
		this.busy = false;
		this.bind();
	}

	ScanScreen.prototype.bind = function () {
		var self = this;
		if (!this.form || !this.input) return;
		this.form.addEventListener('submit', function (event) {
			event.preventDefault();
			self.send(self.input.value);
		});
		// Поле всегда в фокусе: сканер-клавиатура печатает прямо в него
		document.addEventListener('click', function (event) {
			if (event.target.closest('button, a, select, input, textarea')) return;
			self.focus();
		});
		this.focus();
	};

	ScanScreen.prototype.focus = function () {
		if (this.input && !this.busy) {
			this.input.focus();
			this.input.select();
		}
	};

	ScanScreen.prototype.show = function (ok, message, detail) {
		if (!this.result) return;
		this.result.className = 'scan-result ' + (ok ? 'ok' : 'err');
		this.result.innerHTML = '';
		var head = document.createElement('div');
		head.textContent = message;
		this.result.appendChild(head);
		if (detail) {
			var sub = document.createElement('div');
			sub.className = 'sub';
			sub.textContent = detail;
			this.result.appendChild(sub);
		}
		beep(ok);
	};

	ScanScreen.prototype.remember = function (ok, code, message) {
		if (!this.history) return;
		var item = document.createElement('li');
		item.className = ok ? 'ok' : 'err';
		var time = new Date();
		var stamp = ('0' + time.getHours()).slice(-2) + ':' + ('0' + time.getMinutes()).slice(-2);
		item.textContent = stamp + ' · ' + (code || '—') + ' · ' + message;
		this.history.insertBefore(item, this.history.firstChild);
		while (this.history.children.length > HISTORY_LIMIT) {
			this.history.removeChild(this.history.lastChild);
		}
	};

	ScanScreen.prototype.send = function (rawCode) {
		var self = this;
		var code = (rawCode || '').trim();
		if (!code || this.busy) return;
		this.busy = true;

		var controller = new AbortController();
		var timer = setTimeout(function () { controller.abort(); }, SCAN_TIMEOUT_MS);
		var payload = { code: code, csrf_token: this.csrf };
		var extra = this.extra();
		for (var key in extra) { if (Object.prototype.hasOwnProperty.call(extra, key)) payload[key] = extra[key]; }

		fetch(this.url, {
			method: 'POST',
			headers: {
				'Content-Type': 'application/json',
				'X-CSRF-Token': this.csrf,
				'X-Requested-With': 'fetch'
			},
			body: JSON.stringify(payload),
			signal: controller.signal
		}).then(function (response) {
			return response.json().catch(function () {
				return { ok: false, message: 'Сервер ответил непонятно' };
			});
		}).then(function (data) {
			clearTimeout(timer);
			self.busy = false;
			var ok = !!data.ok;
			var message = data.message || data.error || (ok ? 'Готово' : 'Отказ');
			var detail = data.detail || '';
			if (ok && typeof data.stock === 'number') {
				detail = detail ? detail + ' · остаток ' + data.stock : 'остаток ' + data.stock;
			}
			self.show(ok, message, detail);
			self.remember(ok, data.number || code, message);
			self.input.value = '';
			self.focus();
			if (ok) self.onSuccess(data);
		}).catch(function () {
			clearTimeout(timer);
			self.busy = false;
			// Сканы не копятся на устройстве: честно говорим, что запись не прошла
			self.show(false, 'Нет связи, скан НЕ записан', 'Повторите скан, когда связь появится');
			self.remember(false, code, 'нет связи');
			self.input.value = '';
			self.focus();
		});
	};

	// --- Камера ---
	function CameraScan(options) {
		this.boxId = options.boxId || 'camera-box';
		this.onCode = options.onCode;
		this.scanner = null;
	}

	CameraScan.prototype.start = function () {
		var self = this;
		var box = document.getElementById(this.boxId);
		if (!box) return;
		box.hidden = false;
		if (!global.Html5Qrcode) {
			box.textContent = 'Библиотека камеры не загрузилась';
			return;
		}
		if (this.scanner) return;
		this.scanner = new global.Html5Qrcode(this.boxId);
		this.scanner.start(
			{ facingMode: 'environment' },
			{ fps: 10, qrbox: { width: 250, height: 250 } },
			function (text) {
				self.stop();
				self.onCode(text);
			},
			function () { /* кадр без кода — это нормально */ }
		).catch(function (error) {
			box.textContent = 'Камера недоступна: ' + error;
			self.scanner = null;
		});
	};

	CameraScan.prototype.stop = function () {
		var self = this;
		if (!this.scanner) return;
		this.scanner.stop().then(function () {
			self.scanner.clear();
			self.scanner = null;
			var box = document.getElementById(self.boxId);
			if (box) box.hidden = true;
		}).catch(function () {
			self.scanner = null;
		});
	};

	global.Sklad = global.Sklad || {};
	global.Sklad.ScanScreen = ScanScreen;
	global.Sklad.CameraScan = CameraScan;
	global.Sklad.beep = beep;
})(window);
