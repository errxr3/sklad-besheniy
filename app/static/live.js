/* Фильтры применяются сразу, без «Показать», а таблицы со [data-live] обновляются после каждого скана (решение владельца 2026-11-05). */
(function () {
	// 1. Фильтры: выбрал — страница сразу показала. Текст — через 0,7 с после ввода.
	Array.prototype.forEach.call(document.querySelectorAll('form[method="get"]'), function (form) {
		if (form.hasAttribute('data-manual')) return;
		var timer = null;
		function go() { if (form.requestSubmit) form.requestSubmit(); else form.submit(); }
		form.addEventListener('change', function (event) {
			var field = event.target;
			if (!field.name || field.type === 'text' || field.type === 'search') return;
			// сменили подразделение — сотрудник из другого отдела больше не подходит
			var reset = field.getAttribute('data-reset');
			if (reset && form.elements[reset]) form.elements[reset].value = form.elements[reset].getAttribute('data-default') || '';
			go();
		});
		form.addEventListener('input', function (event) {
			var field = event.target;
			if (field.type !== 'text' && field.type !== 'search') return;
			clearTimeout(timer);
			timer = setTimeout(go, 700);
		});
		Array.prototype.forEach.call(form.querySelectorAll('button[type="submit"]:not([formaction])'), function (button) {
			if (!button.hasAttribute('data-keep')) button.style.display = 'none';
		});
	});

	// 2. Живые таблицы: обновляются сразу после скана или другого изменения на складе.
	// Не трогаем, пока человек что-то выделяет, печатает или отмечает галочки.
	var live = document.querySelectorAll('[data-live]');
	if (!live.length) return;
	// Пока человек крутит страницу или таблицу, не подменяем её: иначе прокрутка дёргается
	var scrolledAt = 0;
	window.addEventListener('scroll', function () { scrolledAt = Date.now(); }, { capture: true, passive: true });
	window.addEventListener('wheel', function () { scrolledAt = Date.now(); }, { passive: true });
	window.addEventListener('touchmove', function () { scrolledAt = Date.now(); }, { passive: true });
	function busy() {
		if (document.hidden) return true;
		if (Date.now() - scrolledAt < 1500) return true;
		if (window.getSelection && String(window.getSelection())) return true;
		var active = document.activeElement;
		for (var i = 0; i < live.length; i++) {
			if (active && active !== document.body && live[i].contains(active)) return true;
			if (live[i].querySelector('input[type="checkbox"]:checked')) return true;
			var texts = live[i].querySelectorAll('input[type="text"], input[type="number"], textarea');
			for (var j = 0; j < texts.length; j++) if (texts[j].value && texts[j].value !== texts[j].defaultValue) return true;
		}
		return false;
	}
	// Обновляем не по таймеру, а когда на складе что-то поменялось: скан в канал, отмена, план.
	// Сервер отдаёт короткую метку; поменялась — подтягиваем свежую страницу.
	// Тяжёлые страницы (Остатки) ставят data-live-every="10": не чаще раза в 10 с, даже в час пик.
	var every = 0;
	Array.prototype.forEach.call(live, function (element) {
		every = Math.max(every, (parseInt(element.getAttribute('data-live-every'), 10) || 0) * 1000);
	});
	var stamp = null, pending = 0, last = 0;
	function refresh() {
		if (busy() || Date.now() - last < every) return;
		last = Date.now();
		pending -= 1;
		fetch(window.location.href, { headers: { 'X-Requested-With': 'live' }, credentials: 'same-origin' })
			.then(function (response) { return response.ok ? response.text() : null; })
			.then(function (html) {
				if (!html) return;
				if (busy()) { pending = Math.max(pending, 1); return; }
				var fresh = new DOMParser().parseFromString(html, 'text/html');
				Array.prototype.forEach.call(live, function (element) {
					var next = element.id && fresh.getElementById(element.id);
					if (next && next.innerHTML !== element.innerHTML) element.innerHTML = next.innerHTML;
				});
				document.dispatchEvent(new CustomEvent('live:update'));
			})
			.catch(function () { pending = Math.max(pending, 1); });
	}
	setInterval(function () {
		if (document.hidden) return;
		fetch('/live/stamp', { credentials: 'same-origin', cache: 'no-store' })
			.then(function (response) { return response.ok ? response.text() : null; })
			.then(function (value) {
				if (value === null) return;
				// Два обновления: второе ловит скан, который в момент первого ещё не дописался в базу
				if (stamp !== null && value !== stamp) pending = 2;
				stamp = value;
				if (pending > 0) refresh();
			})
			.catch(function () {});
	}, 3000);
})();
