/* Chart hover: crosshair + tooltip from each hit rect's data-tip. */
(function () {
  "use strict";
  document.querySelectorAll(".chart").forEach(function (chart) {
    var svg = chart.querySelector("svg");
    var tip = chart.querySelector(".tip");
    var cross = svg.querySelector(".crosshair");

    function show(hit) {
      var vb = svg.viewBox.baseVal;
      var scale = svg.getBoundingClientRect().width / vb.width;
      var x = parseFloat(hit.dataset.x), y = parseFloat(hit.dataset.y);
      tip.textContent = hit.dataset.tip;
      tip.hidden = false;
      var left = Math.max(80, Math.min(x * scale, chart.clientWidth - 80));
      tip.style.left = left + "px";
      tip.style.top = (y * scale) + "px";
      if (!hit.dataset.nocross) {
        cross.setAttribute("x1", x);
        cross.setAttribute("x2", x);
        cross.setAttribute("visibility", "visible");
      }
    }
    function hide() {
      tip.hidden = true;
      cross.setAttribute("visibility", "hidden");
    }
    svg.querySelectorAll(".hit").forEach(function (hit) {
      hit.addEventListener("mouseenter", function () { show(hit); });
      hit.addEventListener("touchstart", function () { show(hit); }, { passive: true });
    });
    svg.addEventListener("mouseleave", hide);
  });
})();

/* A form that changes something is sent once, so a double click can't, say, start two
   production orders. (Buttons that only look something up, formmethod="get", don't count.) */
(function () {
  "use strict";
  var forms = document.querySelectorAll('form[method="post"]');
  forms.forEach(function (form) {
    form.addEventListener("submit", function (e) {
      if (e.submitter && e.submitter.getAttribute("formmethod") === "get") return;
      if (form.dataset.sent) { e.preventDefault(); return; }
      form.dataset.sent = "1";
    });
  });
  window.addEventListener("pageshow", function () {  // back button: the form can be sent again
    forms.forEach(function (form) { delete form.dataset.sent; });
  });
})();

/* Yields as the quantity made is typed, on the page that completes a production order:
   each [data-yield] shows made x data-num / data-den; [data-flag] ones go red under 95%. */
(function () {
  "use strict";
  var made = document.getElementById("made");
  if (!made) return;
  var outs = document.querySelectorAll("[data-yield]");
  function show() {
    var m = parseFloat(made.value);
    outs.forEach(function (out) {
      var num = parseFloat(out.dataset.num), den = parseFloat(out.dataset.den);
      var v = m > 0 && num > 0 && den > 0 ? m * num / den : NaN;
      out.textContent = isFinite(v) ? (v * 100).toFixed(1) + "%" : "—";
      if (out.hasAttribute("data-flag")) out.classList.toggle("over", isFinite(v) && v < 0.95);
    });
  }
  made.addEventListener("input", show);
  show();
})();

/* Batches made, on the same page: more rows on request, and their total against the
   quantity made (red until they match). */
(function () {
  "use strict";
  var body = document.querySelector("[data-batches]");
  if (!body) return;
  var made = document.getElementById("made");
  var total = document.querySelector("[data-batch-total]");
  var add = document.querySelector("[data-add-batch]");
  function sum() {
    var t = 0, rows = 0, blank = 0;
    body.querySelectorAll("tr").forEach(function (row) {
      var number = row.querySelector('[name="batch_number"]').value.trim();
      var qty = row.querySelector('[name="batch_qty"]').value.trim();
      if (!number && !qty) return;
      rows += 1;
      if (qty) t += parseFloat(qty) || 0; else blank += 1;
    });
    var m = parseFloat(made.value);
    if (rows === 1 && blank === 1 && m > 0) t = m;  // a lone batch without a quantity is all of it
    total.textContent = rows ? String(Math.round(t * 10000) / 10000) : "—";
    total.classList.toggle("over", rows > 0 && !(Math.abs(t - m) <= 0.0051));
  }
  add.hidden = false;
  add.addEventListener("click", function () {
    var row = body.querySelector("tr:last-child").cloneNode(true);
    row.querySelectorAll("input").forEach(function (input) { input.value = ""; });
    body.appendChild(row);
    row.querySelector("input").focus();
  });
  body.addEventListener("input", sum);
  made.addEventListener("input", sum);
  sum();
})();

/* On narrow screens the tab rows scroll sideways: bring the open tab into view. */
(function () {
  "use strict";
  document.querySelectorAll(".dash-nav").forEach(function (nav) {
    var active = nav.querySelector("a.active");
    if (active && nav.scrollWidth > nav.clientWidth) {
      nav.scrollLeft = active.offsetLeft - (nav.clientWidth - active.offsetWidth) / 2;
    }
  });
})();
