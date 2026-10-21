/* Выбор артикула: поле поиска + выпадающий список (задачи, печать ШК на возвратах).
 *
 * Разметка:
 * <div class="article-picker" data-article-picker data-site="ID площадки или пусто">
 *   <input type="search" class="ap-input" autocomplete="off">
 *   <input type="hidden" name="article_id" class="ap-value">
 *   <ul class="ap-list" hidden></ul>
 * </div>
 * С data-site показываются только артикулы, которые делают на этой площадке.
 */
(function (global) {
	'use strict';

	function ArticlePicker(root) {
		this.root = root;
		this.input = root.querySelector('.ap-input');
		this.value = root.querySelector('.ap-value');
		this.list = root.querySelector('.ap-list');
		this.site = root.dataset.site || '';
		this.items = [];
		this.active = -1;
		this.timer = null;
		this.request = 0;
		this.bind();
	}

	ArticlePicker.prototype.bind = function () {
		var self = this;
		this.input.addEventListener('input', function () {
			self.value.value = '';
			self.root.classList.remove('chosen');
			clearTimeout(self.timer);
			self.timer = setTimeout(function () { self.load(self.input.value); }, 150);
		});
		this.input.addEventListener('focus', function () { self.load(self.input.value); });
		this.input.addEventListener('keydown', function (event) {
			if (self.list.hidden) return;
			if (event.key === 'ArrowDown') { self.move(1); event.preventDefault(); }
			else if (event.key === 'ArrowUp') { self.move(-1); event.preventDefault(); }
			else if (event.key === 'Enter') {
				if (self.active >= 0) { self.choose(self.items[self.active]); event.preventDefault(); }
			} else if (event.key === 'Escape') { self.hide(); }
		});
		document.addEventListener('click', function (event) {
			if (!self.root.contains(event.target)) self.hide();
		});
		var form = this.root.closest('form');
		if (form) {
			form.addEventListener('submit', function (event) {
				if (!self.value.value) {
					event.preventDefault();
					self.root.classList.add('error');
					self.input.focus();
				}
			});
		}
	};

	ArticlePicker.prototype.load = function (query) {
		var self = this;
		var ticket = ++this.request;
		var url = '/api/articles?q=' + encodeURIComponent(query || '');
		if (this.site) url += '&site_id=' + encodeURIComponent(this.site);
		fetch(url, { headers: { 'X-Requested-With': 'fetch' } })
			.then(function (r) { return r.json(); })
			.then(function (data) {
				if (ticket !== self.request) return;
				self.render(data.articles || []);
			})
			.catch(function () {});
	};

	ArticlePicker.prototype.render = function (items) {
		var self = this;
		this.items = items;
		this.active = items.length ? 0 : -1;
		this.list.innerHTML = '';
		if (!items.length) {
			var empty = document.createElement('li');
			empty.className = 'ap-empty';
			empty.textContent = 'Ничего не найдено';
			this.list.appendChild(empty);
		}
		items.forEach(function (item, index) {
			var li = document.createElement('li');
			li.textContent = item.name;
			li.className = index === 0 ? 'active' : '';
			li.addEventListener('mousedown', function (event) {
				event.preventDefault();
				self.choose(item);
			});
			self.list.appendChild(li);
		});
		this.list.hidden = false;
	};

	ArticlePicker.prototype.move = function (step) {
		if (!this.items.length) return;
		this.active = (this.active + step + this.items.length) % this.items.length;
		Array.prototype.forEach.call(this.list.children, function (li, index) {
			li.classList.toggle('active', index === this.active);
		}, this);
		var current = this.list.children[this.active];
		if (current && current.scrollIntoView) current.scrollIntoView({ block: 'nearest' });
	};

	ArticlePicker.prototype.choose = function (item) {
		this.value.value = item.id;
		this.input.value = item.name;
		this.root.classList.add('chosen');
		this.root.classList.remove('error');
		this.hide();
	};

	ArticlePicker.prototype.hide = function () {
		this.list.hidden = true;
	};

	Array.prototype.forEach.call(document.querySelectorAll('[data-article-picker]'), function (root) {
		root.articlePicker = new ArticlePicker(root);
	});
	global.Sklad = global.Sklad || {};
	global.Sklad.ArticlePicker = ArticlePicker;
})(window);
