// Small helpers for the log pages. Everything still works without this file.
(function () {
  // "Update: March 09, 2026" -> "9 Mar 2026". The HTML stays as the now-post app writes it.
  document.querySelectorAll('.log > .date').forEach(function (el) {
    var raw = el.textContent.trim();
    var m = raw.match(/^Update:\s*(.+)$/i);
    if (!m) return;
    var iso = /^\d{4}-\d{2}-\d{2}$/.test(m[1]);
    var d = new Date(iso ? m[1] + 'T12:00:00' : m[1]);
    el.textContent = isNaN(d) ? m[1] : d.toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric' });
  });

  // Tap a photo to see it full size. Esc or another tap closes it.
  var imgs = document.querySelectorAll('.log img');
  if (!imgs.length || !window.HTMLDialogElement) return;
  var dlg = document.createElement('dialog');
  dlg.className = 'zoom';
  var big = document.createElement('img');
  big.alt = '';
  dlg.appendChild(big);
  document.body.appendChild(dlg);
  dlg.addEventListener('click', function () { dlg.close(); });
  imgs.forEach(function (img) {
    img.tabIndex = 0;
    function open() { big.src = img.src; big.alt = img.alt; dlg.showModal(); }
    img.addEventListener('click', open);
    img.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') { e.preventDefault(); open(); }
    });
  });
})();
