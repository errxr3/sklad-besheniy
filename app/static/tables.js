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
