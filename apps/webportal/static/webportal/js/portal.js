/* CDK Attendance portal — theming, sidebar drawer, DataTables, HTMX glue. */
(function () {
  'use strict';

  /* ---------------------------------------------------------- accent ------ */
  var ACCENTS = {
    purple: { brand:'#6D28D9', hover:'#5B21B6', light:'#8B5CF6', tint:'#F5F3FF',
      grad:'linear-gradient(135deg,#7C3AED 0%,#6D28D9 55%,#5B21B6 100%)' },
    indigo: { brand:'#4F46E5', hover:'#4338CA', light:'#6366F1', tint:'#EEF2FF',
      grad:'linear-gradient(135deg,#6366F1 0%,#4F46E5 55%,#4338CA 100%)' },
    teal:   { brand:'#0D9488', hover:'#0F766E', light:'#14B8A6', tint:'#F0FDFA',
      grad:'linear-gradient(135deg,#14B8A6 0%,#0D9488 55%,#0F766E 100%)' },
    rose:   { brand:'#E11D48', hover:'#BE123C', light:'#F43F5E', tint:'#FFF1F2',
      grad:'linear-gradient(135deg,#F43F5E 0%,#E11D48 55%,#BE123C 100%)' },
    slate:  { brand:'#475569', hover:'#334155', light:'#64748B', tint:'#F1F5F9',
      grad:'linear-gradient(135deg,#64748B 0%,#475569 55%,#334155 100%)' },
    amber:  { brand:'#D97706', hover:'#B45309', light:'#F59E0B', tint:'#FFF7ED',
      grad:'linear-gradient(135deg,#F59E0B 0%,#D97706 55%,#B45309 100%)' }
  };

  function applyAccent(key) {
    var a = ACCENTS[key] || ACCENTS.purple;
    var r = document.documentElement.style;
    r.setProperty('--brand', a.brand);
    r.setProperty('--brand-hover', a.hover);
    r.setProperty('--brand-light', a.light);
    r.setProperty('--brand-tint', a.tint);
    r.setProperty('--brand-grad', a.grad);
    try { localStorage.setItem('portalAccent', key); } catch (e) {}
    document.querySelectorAll('[data-accent]').forEach(function (b) {
      b.classList.toggle('active', b.dataset.accent === key);
    });
  }
  window.applyAccent = applyAccent;

  var saved = 'purple';
  try { saved = localStorage.getItem('portalAccent') || 'purple'; } catch (e) {}
  applyAccent(saved);  // set CSS vars immediately (avoid flash)

  /* --------------------------------------------- sidebar + surface -------- */
  // These are pure CSS-variable presets keyed off a data-* attribute on <html>;
  // JS only flips the attribute, persists it, and marks the active swatch.
  function markSwatch(attr, key) {
    document.querySelectorAll('#themePanel [data-' + attr + ']').forEach(function (b) {
      b.classList.toggle('active', b.getAttribute('data-' + attr) === key);
    });
  }
  function lsGet(k, dflt) { try { return localStorage.getItem(k) || dflt; } catch (e) { return dflt; } }
  function lsSet(k, v) { try { localStorage.setItem(k, v); } catch (e) {} }

  function applySidebar(key) {
    document.documentElement.setAttribute('data-sidebar', key);
    lsSet('portalSidebar', key); markSwatch('sidebar', key);
  }
  var DARK_SURFACES = ['dark', 'black', 'slate'];
  function applySurface(key) {
    document.documentElement.setAttribute('data-surface', key);
    document.documentElement.classList.toggle('surface-dark', DARK_SURFACES.indexOf(key) !== -1);
    lsSet('portalSurface', key); markSwatch('surface', key);
  }
  function applyCards(key) {
    document.documentElement.setAttribute('data-cards', key);
    lsSet('portalCards', key); markSwatch('cards', key);
  }
  window.applySidebar = applySidebar;
  window.applySurface = applySurface;
  window.applyCards = applyCards;

  var savedSidebar = lsGet('portalSidebar', 'midnight');
  var savedSurface = lsGet('portalSurface', 'clean');
  var savedCards = lsGet('portalCards', 'elevated');
  // Set attributes before first paint to avoid a flash of the default theme.
  document.documentElement.setAttribute('data-sidebar', savedSidebar);
  document.documentElement.setAttribute('data-surface', savedSurface);
  document.documentElement.setAttribute('data-cards', savedCards);
  document.documentElement.classList.toggle('surface-dark', DARK_SURFACES.indexOf(savedSurface) !== -1);

  /* ------------------------------------------- collapsed sidebar rail ----- */
  function applyCollapsed(on) {
    if (document.body) document.body.classList.toggle('sidebar-collapsed', on);
    lsSet('portalSidebarCollapsed', on ? '1' : '0');
    // The main column resized — let DataTables re-measure column widths.
    setTimeout(function () {
      try { window.dispatchEvent(new Event('resize')); } catch (e) {}
    }, 260);
  }
  var savedCollapsed = lsGet('portalSidebarCollapsed', '0') === '1';
  if (document.body) document.body.classList.toggle('sidebar-collapsed', savedCollapsed);

  /* ---------------------------------------------------------- DataTables -- */
  var DT_LAYOUT = { topStart: 'buttons', topEnd: 'search', bottomStart: 'info', bottomEnd: 'paging' };
  function dtButtons() {
    return [
      { extend: 'copyHtml5', text: 'Copy' },
      { extend: 'csvHtml5', text: 'CSV' },
      { extend: 'excelHtml5', text: 'Excel' },
      { extend: 'pdfHtml5', text: 'PDF', orientation: 'landscape', pageSize: 'A4' },
      { extend: 'print', text: 'Print' }
    ];
  }

  function initTables(root) {
    if (!window.jQuery || !$.fn.DataTable) return;
    $(root).find('table.js-dt').each(function () {
      if ($.fn.DataTable.isDataTable(this)) return;
      $(this).DataTable({
        responsive: true, pageLength: 10, order: $(this).data('dt-order') || [[0, 'desc']],
        layout: DT_LAYOUT, buttons: dtButtons(),
        language: { search: '', searchPlaceholder: 'Search…' }
      });
    });
    $(root).find('table.js-dt-server').each(function () {
      if ($.fn.DataTable.isDataTable(this)) return;
      $(this).DataTable({
        serverSide: true, processing: true, responsive: true, pageLength: 10,
        order: $(this).data('dt-order') || [[0, 'desc']],
        layout: DT_LAYOUT, buttons: dtButtons(),
        language: { search: '', searchPlaceholder: 'Search…' },
        ajax: {
          url: $(this).data('dt-url'),
          data: function (d) {
            var form = document.getElementById('dt-filters');
            if (form) new FormData(form).forEach(function (v, k) { d[k] = v; });
          }
        }
      });
    });
  }
  window.portalInitTables = initTables;

  function reloadServerTables() {
    if (!window.jQuery || !$.fn.DataTable) return;
    $('table.js-dt-server').each(function () {
      if ($.fn.DataTable.isDataTable(this)) $(this).DataTable().ajax.reload();
    });
  }

  /* ----------------------------------------------------------- weather ---- */
  // Open-Meteo (no key, CORS-enabled). Progressive enhancement on the holiday
  // calendar: fills the current-conditions box + annotates the next 16 days.
  function wmo(code) {
    if (code === 0) return { icon: 'brightness-high', label: 'Clear', cls: 'w-sun' };
    if (code === 1 || code === 2) return { icon: 'cloud-sun', label: 'Partly cloudy', cls: 'w-sun' };
    if (code === 3) return { icon: 'clouds', label: 'Overcast', cls: 'w-cloud' };
    if (code === 45 || code === 48) return { icon: 'cloud-fog2', label: 'Fog', cls: 'w-cloud' };
    if (code >= 51 && code <= 57) return { icon: 'cloud-drizzle', label: 'Drizzle', cls: 'w-rain' };
    if (code >= 61 && code <= 67) return { icon: 'cloud-rain', label: 'Rain', cls: 'w-rain' };
    if (code >= 71 && code <= 77) return { icon: 'cloud-snow', label: 'Snow', cls: 'w-snow' };
    if (code >= 80 && code <= 82) return { icon: 'cloud-rain-heavy', label: 'Showers', cls: 'w-rain' };
    if (code >= 85 && code <= 86) return { icon: 'cloud-snow', label: 'Snow showers', cls: 'w-snow' };
    if (code >= 95) return { icon: 'cloud-lightning-rain', label: 'Thunderstorm', cls: 'w-storm' };
    return { icon: 'cloud', label: '—', cls: 'w-cloud' };
  }

  function hour12(dt) {
    var h = dt.getHours(), ap = h >= 12 ? 'PM' : 'AM';
    return (h % 12 || 12) + ' ' + ap;
  }

  /* Rich widget for the dashboard: current + next hours + next days. */
  function renderDashWeather(data) {
    var el = document.getElementById('dash-weather');
    if (!el || !data.current || !data.daily) return;
    var w = wmo(data.current.weather_code), d = data.daily;

    var hours = '';
    if (data.hourly && data.hourly.time) {
      var now = Date.now(), start = 0;
      for (var i = 0; i < data.hourly.time.length; i++) {
        if (new Date(data.hourly.time[i]).getTime() >= now) { start = i; break; }
      }
      for (var k = start; k < Math.min(start + 8, data.hourly.time.length); k++) {
        var hw = wmo(data.hourly.weather_code[k]);
        hours += '<div class="wx-cell"><div class="wx-cell-top">' + hour12(new Date(data.hourly.time[k])) + '</div>' +
          '<i class="bi bi-' + hw.icon + ' ' + hw.cls + '"></i>' +
          '<div class="wx-cell-bot">' + Math.round(data.hourly.temperature_2m[k]) + '°</div></div>';
      }
    }

    var days = '';
    for (var j = 1; j < Math.min(6, d.time.length); j++) {
      var dw = wmo(d.weather_code[j]);
      var label = new Date(d.time[j] + 'T00:00').toLocaleDateString(undefined, { weekday: 'short' });
      days += '<div class="wx-cell"><div class="wx-cell-top">' + label + '</div>' +
        '<i class="bi bi-' + dw.icon + ' ' + dw.cls + '"></i>' +
        '<div class="wx-cell-bot">' + Math.round(d.temperature_2m_max[j]) + '°' +
        '<span class="wx-lo">' + Math.round(d.temperature_2m_min[j]) + '°</span></div></div>';
    }

    el.innerHTML =
      '<div class="wx-now">' +
        '<i class="bi bi-' + w.icon + ' wx-big ' + w.cls + '"></i>' +
        '<div class="wx-now-main">' +
          '<div class="wx-now-temp">' + Math.round(data.current.temperature_2m) + '°</div>' +
          '<div class="wx-now-cond">' + w.label + '</div></div>' +
        '<div class="wx-now-meta">H ' + Math.round(d.temperature_2m_max[0]) + '°<br>L ' + Math.round(d.temperature_2m_min[0]) + '°</div>' +
      '</div>' +
      (hours ? '<div class="wx-sec">Next hours</div><div class="wx-strip">' + hours + '</div>' : '') +
      (days ? '<div class="wx-sec">Next days</div><div class="wx-strip">' + days + '</div>' : '');
  }

  function weatherApply(data) {
    if (!data || !data.daily) return;
    var d = data.daily, map = {};
    for (var i = 0; i < d.time.length; i++) {
      map[d.time[i]] = { code: d.weather_code[i], hi: d.temperature_2m_max[i], lo: d.temperature_2m_min[i] };
    }
    var box = document.getElementById('hol-weather');
    if (box && data.current) {
      var w = wmo(data.current.weather_code);
      var todayInfo = map[d.time[0]];
      box.innerHTML =
        '<i class="bi bi-' + w.icon + ' w-icon ' + w.cls + '"></i>' +
        '<div class="w-temp">' + Math.round(data.current.temperature_2m) + '°</div>' +
        '<div class="w-label">' + w.label + '</div>' +
        (todayInfo ? '<div class="w-hilo">H ' + Math.round(todayInfo.hi) + '° · L ' + Math.round(todayInfo.lo) + '°</div>' : '');
    }
    document.querySelectorAll('.cal-day[data-date]').forEach(function (cell) {
      var old = cell.querySelector('.cal-wx'); if (old) old.remove();
      if (cell.classList.contains('out')) return;
      var info = map[cell.getAttribute('data-date')];
      if (!info) return;
      var cw = wmo(info.code);
      var el = document.createElement('span');
      el.className = 'cal-wx';
      el.title = cw.label + ' · H ' + Math.round(info.hi) + '° L ' + Math.round(info.lo) + '°';
      el.innerHTML = '<i class="bi bi-' + cw.icon + ' ' + cw.cls + '"></i><b>' + Math.round(info.hi) + '°</b>';
      cell.appendChild(el);
    });
    renderDashWeather(data);
  }

  function weatherLoad() {
    // Any element carrying coords hosts the forecast (#cal or #dash-weather).
    var host = document.querySelector('[data-lat][data-lon]');
    if (!host) return;
    var lat = host.getAttribute('data-lat'), lon = host.getAttribute('data-lon');
    var ckey = 'omWx1:' + lat + ',' + lon;
    try {
      var c = JSON.parse(sessionStorage.getItem(ckey));
      if (c && (Date.now() - c.t) < 3600000) { weatherApply(c.d); return; }
    } catch (e) {}
    var url = 'https://api.open-meteo.com/v1/forecast?latitude=' + lat + '&longitude=' + lon +
      '&daily=weather_code,temperature_2m_max,temperature_2m_min' +
      '&hourly=temperature_2m,weather_code' +
      '&current=temperature_2m,weather_code&forecast_days=16&timezone=auto';
    fetch(url).then(function (r) { return r.json(); }).then(function (data) {
      try { sessionStorage.setItem(ckey, JSON.stringify({ t: Date.now(), d: data })); } catch (e) {}
      weatherApply(data);
    }).catch(function () { /* offline / blocked — UI degrades gracefully */ });
  }
  window.portalWeather = weatherLoad;

  /* ------------------------------------------------------------ modal ----- */
  function getModal() {
    var el = document.getElementById('appModal');
    return (el && window.bootstrap) ? bootstrap.Modal.getOrCreateInstance(el) : null;
  }

  /* ------------------------------------------------------------ ready ----- */
  function ready(fn) {
    if (document.readyState !== 'loading') fn();
    else document.addEventListener('DOMContentLoaded', fn);
  }

  ready(function () {
    applyAccent(saved);              // set active state on the picker now the DOM exists
    markSwatch('sidebar', savedSidebar);
    markSwatch('surface', savedSurface);
    markSwatch('cards', savedCards);

    document.querySelectorAll('[data-accent]').forEach(function (b) {
      b.addEventListener('click', function () { applyAccent(b.dataset.accent); });
    });
    document.querySelectorAll('#themePanel [data-sidebar]').forEach(function (b) {
      b.addEventListener('click', function () { applySidebar(b.getAttribute('data-sidebar')); });
    });
    document.querySelectorAll('#themePanel [data-surface]').forEach(function (b) {
      b.addEventListener('click', function () { applySurface(b.getAttribute('data-surface')); });
    });
    document.querySelectorAll('#themePanel [data-cards]').forEach(function (b) {
      b.addEventListener('click', function () { applyCards(b.getAttribute('data-cards')); });
    });
    var reset = document.getElementById('tc-reset');
    if (reset) reset.addEventListener('click', function () {
      applyAccent('purple'); applySidebar('midnight'); applySurface('clean'); applyCards('elevated');
    });

    // Sidebar drawer (mobile).
    document.querySelectorAll('[data-nav-toggle]').forEach(function (b) {
      b.addEventListener('click', function () { document.body.classList.toggle('nav-open'); });
    });
    // Sidebar collapse to icon-only rail (desktop).
    document.querySelectorAll('[data-sidebar-collapse]').forEach(function (b) {
      b.addEventListener('click', function () {
        applyCollapsed(!document.body.classList.contains('sidebar-collapsed'));
      });
    });
    var scrim = document.querySelector('.scrim');
    if (scrim) scrim.addEventListener('click', function () { document.body.classList.remove('nav-open'); });

    // Move focus out of the modal before Bootstrap hides it (it sets aria-hidden
    // on the modal, which is invalid while a descendant keeps focus).
    var modalEl = document.getElementById('appModal');
    if (modalEl) modalEl.addEventListener('hide.bs.modal', function () {
      if (modalEl.contains(document.activeElement)) document.activeElement.blur();
    });

    initTables(document);
    weatherLoad();
  });

  // Server-side tables reload when the top filters change.
  document.addEventListener('change', function (e) {
    if (e.target.closest('#dt-filters')) reloadServerTables();
  });

  // DataTables inside a hidden tab can't measure column widths; fix on show.
  document.addEventListener('shown.bs.tab', function (e) {
    if (!window.jQuery || !$.fn.DataTable) return;
    var sel = e.target.getAttribute('data-bs-target');
    var pane = sel && document.querySelector(sel);
    if (!pane) return;
    $(pane).find('table.js-dt, table.js-dt-server').each(function () {
      if ($.fn.DataTable.isDataTable(this)) $(this).DataTable().columns.adjust();
    });
  });

  // Modal open the moment a modal-loader is clicked (independent of htmx timing).
  document.addEventListener('click', function (e) {
    if (e.target.closest('[hx-get][hx-target="#modalContent"]')) {
      var m = getModal(); if (m) m.show();
    }
  });

  // HTMX lifecycle: tear down / re-init DataTables around swaps; modal handling.
  document.body.addEventListener('htmx:beforeSwap', function (e) {
    if (window.jQuery && e.detail.target) {
      $(e.detail.target).find('table.js-dt, table.js-dt-server').each(function () {
        if ($.fn.DataTable.isDataTable(this)) $(this).DataTable().destroy();
      });
    }
  });
  document.body.addEventListener('htmx:afterSwap', function (e) {
    if (!e.detail.target) return;
    if (e.detail.target.id === 'modalContent') { var m = getModal(); if (m) m.show(); }
    if (e.detail.target.id === 'cal') weatherLoad();  // re-apply on month nav
    initTables(e.detail.target);
  });
  document.body.addEventListener('closeModal', function () { var m = getModal(); if (m) m.hide(); });
  document.body.addEventListener('htmx:targetError', function (e) {
    console.warn('[portal] htmx target not found:', e.detail ? e.detail.target : e);
  });
})();
