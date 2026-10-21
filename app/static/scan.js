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
		this.onResult = options.onResult || function () {};
		this.busy = false;
		this.bind();
	}

	// Режим ввода: «Сканер» — поле всегда в фокусе для сканера-клавиатуры;
	// «Камера» — поле не хватает фокус, экранная клавиатура не выскакивает.
	// Выбор запоминается на устройстве.
	var MODE_KEY = 'sklad-scan-mode';

	function getMode() {
		try { return global.localStorage.getItem(MODE_KEY) === 'camera' ? 'camera' : 'scanner'; }
		catch (e) { return 'scanner'; }
	}

	function setMode(mode) {
		try { global.localStorage.setItem(MODE_KEY, mode); } catch (e) { /* без памяти — только на эту страницу */ }
	}

	ScanScreen.prototype.addModeSwitch = function () {
		var self = this;
		var box = document.createElement('div');
		box.className = 'scan-mode';
		[['scanner', 'Сканер'], ['camera', 'Камера']].forEach(function (pair) {
			var button = document.createElement('button');
			button.type = 'button';
			button.dataset.mode = pair[0];
			button.textContent = pair[1];
			button.addEventListener('click', function () { self.applyMode(pair[0], true); });
			box.appendChild(button);
		});
		this.form.parentNode.insertBefore(box, this.form);
		this.modeBox = box;
		this.applyMode(getMode(), false);
	};

	ScanScreen.prototype.applyMode = function (mode, remember) {
		this.mode = mode;
		if (remember) setMode(mode);
		Array.prototype.forEach.call(this.modeBox.children, function (button) {
			button.classList.toggle('active', button.dataset.mode === mode);
		});
		var cameraButton = document.getElementById('camera-btn');
		if (cameraButton) cameraButton.classList.toggle('secondary', mode !== 'camera');
		if (mode === 'camera') {
			if (document.activeElement === this.input) this.input.blur();
		} else {
			this.focus();
		}
	};

	ScanScreen.prototype.bind = function () {
		var self = this;
		if (!this.form || !this.input) return;
		this.form.addEventListener('submit', function (event) {
			event.preventDefault();
			self.send(self.input.value);
		});
		this.addModeSwitch();
		// Поле всегда в фокусе: сканер-клавиатура печатает прямо в него
		document.addEventListener('click', function (event) {
			if (event.target.closest('button, a, select, input, textarea')) return;
			self.focus();
		});
		this.focus();
	};

	ScanScreen.prototype.focus = function () {
		if (this.mode === 'camera') return;
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
			self.onResult(data);
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
	// Кадр распознаётся в полном разрешении камеры: тонкие полосы Code 128 на этикетке 58 мм
	// пропадают, если кадр сначала ужать до размера окошка на экране.
	// Android Chrome — встроенный BarcodeDetector, остальные браузеры — ZXing (zxing-wasm).
	var SCAN_INTERVAL_MS = 120;
	var MAX_FRAME_WIDTH = 1920;
	var zxingReady = null;

	function prepareZxing() {
		if (zxingReady) return zxingReady;
		if (!global.ZXingWASM) return null;
		global.ZXingWASM.prepareZXingModule({
			overrides: {
				locateFile: function (path, prefix) {
					if (path.slice(-5) !== '.wasm') return prefix + path;
					var version = global.SKLAD_STATIC_VERSION ? '?v=' + global.SKLAD_STATIC_VERSION : '';
					return '/static/vendor/' + path + version;
				}
			},
			fireImmediately: true
		});
		zxingReady = Promise.resolve();
		return zxingReady;
	}

	function makeDetector() {
		if (!('BarcodeDetector' in global)) return Promise.resolve(null);
		return global.BarcodeDetector.getSupportedFormats().then(function (formats) {
			if (formats.indexOf('code_128') < 0) return null;
			var wanted = ['code_128'];
			if (formats.indexOf('qr_code') >= 0) wanted.push('qr_code');
			return new global.BarcodeDetector({ formats: wanted });
		}).catch(function () { return null; });
	}

	function CameraScan(options) {
		this.boxId = options.boxId || 'camera-box';
		this.onCode = options.onCode;
		this.stream = null;
		this.video = null;
		this.canvas = null;
		this.timer = null;
		this.detector = null;
		this.running = false;
	}

	CameraScan.prototype.start = function () {
		var self = this;
		var box = document.getElementById(this.boxId);
		if (!box) return;
		box.hidden = false;
		if (this.running) return;
		if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
			box.textContent = 'Камера недоступна: откройте склад по https';
			return;
		}
		this.running = true;
		box.innerHTML = '';
		var video = document.createElement('video');
		video.setAttribute('playsinline', '');
		video.setAttribute('autoplay', '');
		video.muted = true;
		var frame = document.createElement('div');
		frame.className = 'camera-frame';
		frame.appendChild(video);
		var aim = document.createElement('div');
		aim.className = 'camera-aim';
		frame.appendChild(aim);
		var hint = document.createElement('p');
		hint.className = 'hint';
		hint.textContent = 'Наведите на штрихкод — он должен занимать почти всю ширину рамки';
		box.appendChild(frame);
		box.appendChild(hint);
		this.video = video;
		this.canvas = document.createElement('canvas');

		navigator.mediaDevices.getUserMedia({
			audio: false,
			video: {
				facingMode: { ideal: 'environment' },
				width: { ideal: 1920 },
				height: { ideal: 1080 }
			}
		}).then(function (stream) {
			if (!self.running) {
				stream.getTracks().forEach(function (track) { track.stop(); });
				return null;
			}
			self.stream = stream;
			self.tuneTrack(stream);
			video.srcObject = stream;
			return Promise.all([video.play(), makeDetector()]);
		}).then(function (result) {
			if (!result || !self.running) return;
			self.detector = result[1];
			if (!self.detector && !prepareZxing()) {
				box.textContent = 'Библиотека камеры не загрузилась';
				self.stop();
				return;
			}
			self.loop();
		}).catch(function (error) {
			self.running = false;
			box.textContent = 'Камера недоступна: ' + (error && error.name === 'NotAllowedError'
				? 'разрешите доступ к камере в настройках браузера' : error);
		});
	};

	// Непрерывный автофокус там, где телефон это умеет
	CameraScan.prototype.tuneTrack = function (stream) {
		var track = stream.getVideoTracks()[0];
		if (!track || !track.getCapabilities) return;
		var caps = track.getCapabilities();
		if (caps.focusMode && caps.focusMode.indexOf('continuous') >= 0) {
			track.applyConstraints({ advanced: [{ focusMode: 'continuous' }] }).catch(function () {});
		}
	};

	CameraScan.prototype.loop = function () {
		var self = this;
		if (!this.running) return;
		this.decodeFrame().then(function (text) {
			if (!self.running) return;
			if (text) {
				self.stop();
				self.onCode(text);
				return;
			}
			self.timer = setTimeout(function () { self.loop(); }, SCAN_INTERVAL_MS);
		}).catch(function () {
			if (self.running) self.timer = setTimeout(function () { self.loop(); }, SCAN_INTERVAL_MS);
		});
	};

	// Средняя полоса кадра (где рамка-прицел) в полном разрешении
	CameraScan.prototype.grab = function () {
		var video = this.video;
		var width = video.videoWidth;
		var height = video.videoHeight;
		if (!width || !height) return null;
		var scale = Math.min(1, MAX_FRAME_WIDTH / width);
		var cropTop = Math.floor(height * 0.15);
		var cropHeight = height - cropTop * 2;
		var canvas = this.canvas;
		canvas.width = Math.round(width * scale);
		canvas.height = Math.round(cropHeight * scale);
		var ctx = canvas.getContext('2d', { willReadFrequently: true });
		ctx.drawImage(video, 0, cropTop, width, cropHeight, 0, 0, canvas.width, canvas.height);
		return ctx;
	};

	CameraScan.prototype.decodeFrame = function () {
		var ctx = this.grab();
		if (!ctx) return Promise.resolve(null);
		if (this.detector) {
			return this.detector.detect(this.canvas).then(function (codes) {
				return codes.length ? codes[0].rawValue : null;
			});
		}
		var image = ctx.getImageData(0, 0, this.canvas.width, this.canvas.height);
		return global.ZXingWASM.readBarcodes(image, {
			formats: ['Code128', 'QRCode'],
			tryHarder: true,
			tryRotate: false,
			tryInvert: false,
			tryDownscale: true,
			maxNumberOfSymbols: 1
		}).then(function (results) {
			for (var i = 0; i < results.length; i++) {
				if (results[i].isValid && results[i].text) return results[i].text;
			}
			return null;
		});
	};

	CameraScan.prototype.stop = function () {
		this.running = false;
		clearTimeout(this.timer);
		if (this.stream) {
			this.stream.getTracks().forEach(function (track) { track.stop(); });
			this.stream = null;
		}
		if (this.video) this.video.srcObject = null;
		var box = document.getElementById(this.boxId);
		if (box) {
			box.innerHTML = '';
			box.hidden = true;
		}
	};

	global.Sklad = global.Sklad || {};
	global.Sklad.ScanScreen = ScanScreen;
	global.Sklad.CameraScan = CameraScan;
	global.Sklad.beep = beep;
})(window);
