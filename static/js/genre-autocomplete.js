(function () {
  var genreHints = [];
  var genreMenu = null;
  var genreMenuInput = null;
  var genreMenuItems = [];
  var genreMenuIndex = -1;

  function loadGenreHintsFromDom() {
    try {
      var hintsEl = document.getElementById('genre-hints');
      if (hintsEl) {
        genreHints = JSON.parse(hintsEl.textContent || '[]');
      }
    } catch (err) {
      genreHints = [];
    }
  }

  loadGenreHintsFromDom();

  function closeGenreMenu() {
    if (genreMenu) genreMenu.remove();
    genreMenu = null;
    genreMenuInput = null;
    genreMenuItems = [];
    genreMenuIndex = -1;
  }

  function positionGenreMenu(input) {
    if (!genreMenu) return;
    var rect = input.getBoundingClientRect();
    var margin = 8;
    var width = Math.max(Math.min(rect.width, window.innerWidth - margin * 2), 220);
    width = Math.min(width, window.innerWidth - margin * 2);
    var left = Math.max(margin, Math.min(rect.left, window.innerWidth - width - margin));
    var top = rect.bottom + 2;
    var maxHeight = Math.min(240, window.innerHeight - top - margin);
    if (maxHeight < 120) {
      top = Math.max(margin, rect.top - Math.min(240, rect.top - margin));
      maxHeight = Math.min(240, rect.top - margin * 2);
    }
    genreMenu.style.left = left + 'px';
    genreMenu.style.top = top + 'px';
    genreMenu.style.width = width + 'px';
    genreMenu.style.maxHeight = Math.max(maxHeight, 120) + 'px';
  }

  function setGenreMenuIndex(index) {
    if (!genreMenu) return;
    var rows = genreMenu.querySelectorAll('.genre-autocomplete-item');
    rows.forEach(function (row, i) {
      row.classList.toggle('is-active', i === index);
    });
    genreMenuIndex = index;
    if (index >= 0 && rows[index]) {
      rows[index].scrollIntoView({ block: 'nearest' });
    }
  }

  function pickGenreHint(input, hint) {
    input.value = hint.name;
    closeGenreMenu();
    input.focus();
  }

  function matchingGenreHints(query) {
    var q = query.replace(/^\s+|\s+$/g, '').toLowerCase();
    var matches = genreHints.filter(function (hint) {
      if (!q) return true;
      return hint.name.toLowerCase().indexOf(q) !== -1;
    });
    return matches.slice(0, 20);
  }

  function openGenreMenu(input, matches) {
    closeGenreMenu();
    if (!matches.length) return;

    genreMenuInput = input;
    genreMenuItems = matches;
    genreMenu = document.createElement('ul');
    genreMenu.className = 'genre-autocomplete-menu';
    genreMenu.setAttribute('role', 'listbox');

    matches.forEach(function (hint, index) {
      var li = document.createElement('li');
      li.className = 'genre-autocomplete-item';
      li.setAttribute('role', 'option');
      li.dataset.index = String(index);

      var name = document.createElement('span');
      name.className = 'genre-autocomplete-name';
      name.textContent = hint.name;
      li.appendChild(name);

      if (hint.examples && hint.examples.length) {
        var examples = document.createElement('span');
        examples.className = 'genre-autocomplete-examples';
        examples.textContent = hint.examples.join(', ');
        li.appendChild(examples);
      }

      li.addEventListener('mousedown', function (e) {
        e.preventDefault();
        pickGenreHint(input, hint);
      });
      genreMenu.appendChild(li);
    });

    document.body.appendChild(genreMenu);
    positionGenreMenu(input);
    genreMenuIndex = -1;
  }

  function refreshGenreMenu(input) {
    openGenreMenu(input, matchingGenreHints(input.value));
  }

  document.addEventListener('input', function (e) {
    if (!e.target.classList || !e.target.classList.contains('genre-autocomplete')) return;
    refreshGenreMenu(e.target);
  });

  document.addEventListener('focusin', function (e) {
    if (!e.target.classList || !e.target.classList.contains('genre-autocomplete')) return;
    refreshGenreMenu(e.target);
  });

  document.addEventListener('focusout', function (e) {
    if (!e.target.classList || !e.target.classList.contains('genre-autocomplete')) return;
    setTimeout(closeGenreMenu, 120);
  });

  document.addEventListener('keydown', function (e) {
    if (!genreMenu || !genreMenuInput || e.target !== genreMenuInput) return;

    if (e.key === 'ArrowDown') {
      e.preventDefault();
      setGenreMenuIndex(Math.min(genreMenuIndex + 1, genreMenuItems.length - 1));
      return;
    }
    if (e.key === 'ArrowUp') {
      e.preventDefault();
      setGenreMenuIndex(Math.max(genreMenuIndex - 1, 0));
      return;
    }
    if (e.key === 'Escape') {
      e.preventDefault();
      closeGenreMenu();
      return;
    }
    if (e.key === 'Enter' && genreMenuIndex >= 0 && genreMenuItems[genreMenuIndex]) {
      e.preventDefault();
      e.stopPropagation();
      pickGenreHint(genreMenuInput, genreMenuItems[genreMenuIndex]);
    }
  }, true);

  window.addEventListener('resize', function () {
    if (genreMenuInput) positionGenreMenu(genreMenuInput);
  });

  window.addEventListener('scroll', function () {
    if (genreMenuInput) positionGenreMenu(genreMenuInput);
  }, true);

  document.body.addEventListener('htmx:oobAfterSwap', function (evt) {
    var target = evt.detail && evt.detail.target;
    if (target && target.id === 'genre-hints') {
      loadGenreHintsFromDom();
      closeGenreMenu();
    }
  });
})();
