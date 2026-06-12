(function () {
  function editorRoot(el) {
    return el && el.closest ? el.closest('.genre-editor') : null;
  }

  function chipTags(chipsEl) {
    return Array.from(chipsEl.querySelectorAll('.chip'))
      .map(function (c) {
        return c.getAttribute('data-tag') || '';
      })
      .filter(Boolean);
  }

  function syncHidden(root) {
    var hidden = root.querySelector('.genre-hidden-input');
    var chips = root.querySelector('.chips');
    if (hidden && chips) {
      hidden.value = chipTags(chips).join(', ');
    }
  }

  function addChip(root, rawTag) {
    var tag = rawTag.replace(/^\s+|\s+$/g, '');
    if (!tag) return;

    var chips = root.querySelector('.chips');
    if (!chips) return;

    var lower = tag.toLowerCase();
    var dup = Array.from(chips.querySelectorAll('.chip')).some(function (c) {
      return (c.getAttribute('data-tag') || '').toLowerCase() === lower;
    });
    if (dup) return;

    var span = document.createElement('span');
    span.className = 'chip';
    span.setAttribute('data-tag', tag);
    span.appendChild(document.createTextNode(tag + ' '));

    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'chip-x';
    btn.setAttribute('aria-label', 'Remove ' + tag);
    btn.textContent = '\u00d7';
    span.appendChild(btn);

    chips.appendChild(span);
    syncHidden(root);
  }

  document.addEventListener('click', function (e) {
    var t = e.target;
    if (t.closest && t.closest('.chip-x')) {
      var chip = t.closest('.chip');
      var root = chip && editorRoot(chip);
      if (chip && root) {
        chip.remove();
        syncHidden(root);
      }
      return;
    }

    if (t.closest && t.closest('.genre-add-btn')) {
      var addRoot = editorRoot(t.closest('.genre-add-btn'));
      if (!addRoot) return;
      var inp = addRoot.querySelector('.genre-add-input');
      if (inp) {
        addChip(addRoot, inp.value);
        inp.value = '';
        inp.focus();
      }
    }
  });

  document.addEventListener('keydown', function (e) {
    if (e.key !== 'Enter') return;
    if (!e.target.classList || !e.target.classList.contains('genre-add-input')) return;

    var root = editorRoot(e.target);
    if (!root) return;

    if (e.metaKey || e.ctrlKey) {
      e.preventDefault();
      syncHidden(root);
      var form = root.querySelector('form.genre-form');
      if (form) form.requestSubmit();
      return;
    }

    e.preventDefault();
    addChip(root, e.target.value);
    e.target.value = '';
  });

  document.body.addEventListener('htmx:beforeRequest', function (evt) {
    var elt = evt.detail && evt.detail.elt;
    if (!elt || !elt.classList || !elt.classList.contains('genre-form')) return;
    var root = editorRoot(elt);
    if (root) syncHidden(root);
  });
})();
