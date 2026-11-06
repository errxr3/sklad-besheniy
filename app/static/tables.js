/* Большие таблицы: кнопка «Во весь экран» и широкая страница (решение владельца 2026-11-06). */
(function () {
	var MIN_ROWS = 8;
	var exit = null;
	var full = null;

	function close() {
		if (!full) return;
		full.classList.remove('tbl-full');
		document.body.classList.remove('tbl-full-open');
		full = null;
	}

	function open(box) {
		close();
		full = box;
		box.classList.add('tbl-full');
		document.body.classList.add('tbl-full-open');
		if (!exit) {
			exit = document.createElement('button');
			exit.type = 'button';
			exit.className = 'tbl-exit';
			exit.textContent = 'Свернуть ✕';
			exit.addEventListener('click', close);
			document.body.appendChild(exit);
		}
	}

	// Где показать таблицу на весь экран: её прокрутка или карточка с ней
	function boxOf(table) {
		return table.closest('.table-wrap') || table.closest('.card') || table.parentElement;
	}

	function enhance() {
		Array.prototype.forEach.call(document.querySelectorAll('main table'), function (table) {
			if (table.rows.length < MIN_ROWS) return;
			var box = boxOf(table);
			if (!box || box === document.querySelector('main')) return;
			// Кнопка стоит перед прокручиваемым блоком, а в карточке — первой строкой
			var place = box.classList.contains('table-wrap') ? box : null;
			var exists = place ? place.previousElementSibling : box.firstElementChild;
			if (exists && exists.classList.contains('tbl-tools')) return;
			var tools = document.createElement('div');
			tools.className = 'tbl-tools';
			var button = document.createElement('button');
			button.type = 'button';
			button.textContent = '⛶ Во весь экран';
			button.addEventListener('click', function () { open(box); });
			tools.appendChild(button);
			if (place) place.parentNode.insertBefore(tools, place);
			else box.insertBefore(tools, box.firstChild);
		});
		if (document.querySelector('main .table-wrap')) document.body.classList.add('wide');
	}

	// Пока крутится — без подсветки строк под мышкой (см. .is-scrolling в app.css)
	var timers = new WeakMap();
	document.addEventListener('scroll', function (event) {
		var wrap = event.target;
		if (!wrap.classList || !wrap.classList.contains('table-wrap')) return;
		if (!wrap.classList.contains('is-scrolling')) wrap.classList.add('is-scrolling');
		clearTimeout(timers.get(wrap));
		timers.set(wrap, setTimeout(function () { wrap.classList.remove('is-scrolling'); }, 200));
	}, { capture: true, passive: true });

	document.addEventListener('keydown', function (event) {
		if (event.key === 'Escape') close();
	});
	// Живое обновление подменяет содержимое карточек — кнопку ставим заново
	document.addEventListener('live:update', enhance);
	enhance();
})();

/* Подсказки [data-tip] (кроме «!» в «Отгрузке», у него своя): мышью — при наведении,
   на телефоне — по нажатию. Плавающий блок поверх страницы, таблица его не обрезает (2026-11-06). */
(function () {
	var box = null, owner = null;
	function hide() { if (box) box.hidden = true; owner = null; }
	function show(el) {
		if (!box) {
			box = document.createElement('div');
			box.className = 'float-tip';
			box.setAttribute('role', 'tooltip');
			document.body.appendChild(box);
		}
		owner = el;
		box.textContent = el.getAttribute('data-tip');
		box.hidden = false;
		var r = el.getBoundingClientRect();
		var left = Math.min(Math.max(8, r.left), window.innerWidth - box.offsetWidth - 8);
		var top = r.bottom + 6;
		if (top + box.offsetHeight > window.innerHeight - 8) top = r.top - box.offsetHeight - 6;
		box.style.left = left + 'px';
		box.style.top = top + 'px';
	}
	function tipOf(target) {
		var el = target && target.closest ? target.closest('[data-tip]') : null;
		return el && !el.classList.contains('open-mark') ? el : null;
	}
	document.addEventListener('mouseover', function (e) { var el = tipOf(e.target); if (el) show(el); });
	document.addEventListener('mouseout', function (e) { if (owner && tipOf(e.target) === owner) hide(); });
	document.addEventListener('focusin', function (e) { var el = tipOf(e.target); if (el) show(el); });
	document.addEventListener('focusout', hide);
	document.addEventListener('click', function (e) {
		var el = tipOf(e.target);
		if (el) { if (owner === el && box && !box.hidden) hide(); else show(el); } else hide();
	});
	window.addEventListener('scroll', hide, true);
})();
