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
	function busy() {
		if (document.hidden) return true;
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
	var stamp = null, pending = false;
	function refresh() {
		if (busy()) { pending = true; return; }
		pending = false;
		fetch(window.location.href, { headers: { 'X-Requested-With': 'live' }, credentials: 'same-origin' })
			.then(function (response) { return response.ok ? response.text() : null; })
			.then(function (html) {
				if (!html) return;
				if (busy()) { pending = true; return; }
				var fresh = new DOMParser().parseFromString(html, 'text/html');
				Array.prototype.forEach.call(live, function (element) {
					var next = element.id && fresh.getElementById(element.id);
					if (next && next.innerHTML !== element.innerHTML) element.innerHTML = next.innerHTML;
				});
				document.dispatchEvent(new CustomEvent('live:update'));
			})
			.catch(function () { pending = true; });
	}
	setInterval(function () {
		if (document.hidden) return;
		fetch('/live/stamp', { credentials: 'same-origin', cache: 'no-store' })
			.then(function (response) { return response.ok ? response.text() : null; })
			.then(function (value) {
				if (value === null) return;
				if (stamp !== null && value !== stamp) pending = true;
				stamp = value;
				if (pending) refresh();
			})
			.catch(function () {});
	}, 3000);
})();
