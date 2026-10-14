// Экран входа: поиск сотрудника и экранная клавиатура для PIN.
(function () {
	var search = document.getElementById('search');
	var select = document.getElementById('user-select');
	var pin = document.getElementById('pin');
	var dots = document.getElementById('pin-dots');
	if (!select || !pin) return;

	function redrawDots() {
		dots.textContent = '•'.repeat(pin.value.length);
	}

	if (search) {
		search.addEventListener('input', function () {
			var query = search.value.trim().toLowerCase();
			Array.prototype.forEach.call(select.options, function (option) {
				var match = !query || option.dataset.name.indexOf(query) !== -1;
				option.hidden = !match;
			});
			var visible = Array.prototype.filter.call(select.options, function (o) { return !o.hidden; });
			if (visible.length === 1) visible[0].selected = true;
		});
	}

	Array.prototype.forEach.call(document.querySelectorAll('.pin-key'), function (key) {
		key.addEventListener('click', function () {
			if (pin.value.length < 6) pin.value += key.dataset.d;
			redrawDots();
		});
	});
	var clear = document.getElementById('pin-clear');
	if (clear) clear.addEventListener('click', function () { pin.value = ''; redrawDots(); });
	var back = document.getElementById('pin-back');
	if (back) back.addEventListener('click', function () { pin.value = pin.value.slice(0, -1); redrawDots(); });
	pin.addEventListener('input', redrawDots);
	redrawDots();
})();
