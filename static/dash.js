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

/* Rows added without saving first, from the page's <template>s: steps on a maintenance
   task list, and steps and their fields on an operating procedure. In a template, __I__
   becomes the new row's number (from 0), __N__ its position (from 1) and __J__ a field's
   number; the hidden count beside them says how many rows the form sends. */
function fromTemplate(id, values) {
  "use strict";
  var html = document.getElementById(id).innerHTML;
  Object.keys(values).forEach(function (k) { html = html.split(k).join(values[k]); });
  var holder = document.createElement("template");
  holder.innerHTML = html.trim();
  return holder.content.firstElementChild;
}

(function () {
  "use strict";
  var count = document.querySelector("[data-op-count]");
  if (!count) return;
  var list = document.querySelector("[data-ops]"), add = document.querySelector("[data-add-op]");
  var max = +count.dataset.max;
  add.hidden = false;
  add.disabled = +count.value >= max;
  add.addEventListener("click", function () {
    var i = +count.value;
    if (i >= max) return;
    var row = fromTemplate("op-template", { __I__: i, __N__: i + 1 });
    list.appendChild(row);
    count.value = i + 1;
    add.disabled = i + 1 >= max;
    row.querySelector("input").focus();
  });
})();

(function () {
  "use strict";
  var count = document.querySelector("[data-step-count]");
  if (!count) return;
  var form = count.form, steps = form.querySelector("[data-steps]");
  var addStep = form.querySelector("[data-add-step]");
  var maxSteps = +count.dataset.maxSteps, maxFields = +count.dataset.maxFields;

  function refresh() {
    addStep.disabled = +count.value >= maxSteps;
    form.querySelectorAll("[data-step]").forEach(function (step) {
      var button = step.querySelector("[data-add-field]");
      button.hidden = false;
      button.disabled = +step.querySelector("[data-field-count]").value >= maxFields;
    });
  }

  addStep.hidden = false;
  addStep.addEventListener("click", function () {
    var i = +count.value;
    if (i >= maxSteps) return;
    var step = fromTemplate("proc-step-template", { __I__: i, __N__: i + 1 });
    steps.appendChild(step);
    count.value = i + 1;
    refresh();
    step.querySelector('[name="name-' + i + '"]').focus();
  });
  // Pictures chosen for a step: shown straight away, and made smaller (at most 1600 px on a
  // side, as JPEG) so that saving several phone photos stays well under the upload limit.
  // GIFs are sent as they are (they may move). Save waits until they're ready.
  var save = form.querySelector("button.btn-primary"), shrinking = 0;
  var MAX_SIDE = 1600, SMALL_ENOUGH = 1024 * 1024;
  function shrink(file) {
    return new Promise(function (resolve) {
      if (!/^image\/(jpeg|png|webp)$/.test(file.type)) { resolve(file); return; }
      var url = URL.createObjectURL(file), img = new Image();
      img.onload = function () {
        URL.revokeObjectURL(url);
        var scale = Math.min(1, MAX_SIDE / Math.max(img.naturalWidth, img.naturalHeight));
        if (scale === 1 && file.size <= SMALL_ENOUGH) { resolve(file); return; }
        var canvas = document.createElement("canvas");
        canvas.width = Math.round(img.naturalWidth * scale);
        canvas.height = Math.round(img.naturalHeight * scale);
        var ctx = canvas.getContext("2d");
        ctx.fillStyle = "#fff";  // a see-through PNG goes on white
        ctx.fillRect(0, 0, canvas.width, canvas.height);
        ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
        canvas.toBlob(function (blob) {
          resolve(blob && blob.size < file.size
            ? new File([blob], file.name.replace(/\.[^.]*$/, "") + ".jpg", { type: "image/jpeg" }) : file);
        }, "image/jpeg", 0.85);
      };
      img.onerror = function () { URL.revokeObjectURL(url); resolve(file); };
      img.src = url;
    });
  }
  form.addEventListener("change", function (e) {
    var input = e.target;
    if (!input.matches("[data-picture-input]")) return;
    var preview = input.parentNode.querySelector("[data-picture-preview]");
    preview.textContent = "";
    shrinking += 1;
    save.disabled = true;
    Promise.all(Array.prototype.map.call(input.files, shrink)).then(function (files) {
      if (window.DataTransfer) {
        var picked = new DataTransfer();
        files.forEach(function (f) { picked.items.add(f); });
        input.files = picked.files;
      }
      files.forEach(function (f) {
        var img = document.createElement("img");
        img.src = URL.createObjectURL(f);
        img.alt = f.name;
        preview.appendChild(img);
      });
    }).finally(function () {
      shrinking -= 1;
      save.disabled = shrinking > 0;
    });
  });

  form.addEventListener("click", function (e) {
    var button = e.target.closest("[data-add-field]");
    if (!button) return;
    var step = button.closest("[data-step]"), fields = step.querySelector("[data-field-count]");
    var j = +fields.value;
    if (j >= maxFields) return;
    var row = fromTemplate("proc-field-template", { __I__: step.dataset.step, __J__: j });
    step.querySelector("[data-fields]").appendChild(row);
    fields.value = j + 1;
    refresh();
    row.querySelector('[name$="-label"]').focus();
  });
  refresh();
})();

/* Maintenance calendar: click a day to add a task due then; drag a task's due date to
   another day to move it. Pointer events, so a mouse and a finger both work. */
(function () {
  "use strict";
  var cal = document.querySelector("[data-calendar]");
  if (!cal) return;
  var errorBox = document.querySelector(".cal-error");
  var drag = null, swallowClick = false;

  function dayAt(x, y) {
    var el = document.elementFromPoint(x, y);
    var day = el && el.closest ? el.closest(".cal-day") : null;
    return day && cal.contains(day) ? day : null;
  }
  function unmark() {
    cal.querySelectorAll(".cal-drop").forEach(function (d) { d.classList.remove("cal-drop"); });
  }
  function stop() {
    if (!drag) return;
    if (drag.ghost) drag.ghost.remove();
    drag.entry.classList.remove("cal-dragging");
    document.body.classList.remove("cal-is-dragging");
    unmark();
    drag = null;
  }
  function problem(message) {
    errorBox.textContent = "! " + message;
    errorBox.hidden = false;
  }

  cal.addEventListener("pointerdown", function (e) {
    var entry = e.target.closest(".cal-movable");
    if (!entry || e.button !== 0) return;
    drag = { entry: entry, x: e.clientX, y: e.clientY, pointer: e.pointerId, started: false };
  });
  document.addEventListener("pointermove", function (e) {
    if (!drag || e.pointerId !== drag.pointer) return;
    if (!drag.started) {
      if (Math.abs(e.clientX - drag.x) + Math.abs(e.clientY - drag.y) < 6) return;  // still a click
      drag.started = true;
      drag.ghost = drag.entry.cloneNode(true);
      drag.ghost.classList.add("cal-ghost");
      drag.ghost.style.width = drag.entry.getBoundingClientRect().width + "px";
      document.body.appendChild(drag.ghost);
      drag.entry.classList.add("cal-dragging");
      document.body.classList.add("cal-is-dragging");
    }
    e.preventDefault();
    drag.ghost.style.left = (e.clientX + 6) + "px";
    drag.ghost.style.top = (e.clientY + 6) + "px";
    unmark();
    var day = dayAt(e.clientX, e.clientY);
    if (day) day.classList.add("cal-drop");
  });
  document.addEventListener("pointerup", function (e) {
    if (!drag || e.pointerId !== drag.pointer) return;
    if (!drag.started) { drag = null; return; }  // a click: open the task
    var entry = drag.entry, day = dayAt(e.clientX, e.clientY);
    stop();
    swallowClick = true;  // the click that ends a drag doesn't open the task
    setTimeout(function () { swallowClick = false; }, 0);
    if (day && day.dataset.date !== entry.dataset.due) move(entry, day);
  });
  document.addEventListener("pointercancel", stop);
  document.addEventListener("keydown", function (e) { if (e.key === "Escape") stop(); });

  function move(entry, day) {
    var from = entry.parentNode, next = entry.nextSibling;
    day.appendChild(entry);
    entry.classList.add("cal-saving");
    function undo(message) {
      from.insertBefore(entry, next);
      entry.classList.remove("cal-saving");
      problem(message);
    }
    fetch(entry.dataset.moveUrl, {
      method: "POST", credentials: "same-origin", headers: { "Accept": "application/json" },
      body: new URLSearchParams({ from: entry.dataset.due, to: day.dataset.date })
    }).then(function (r) {
      var json = (r.headers.get("Content-Type") || "").indexOf("application/json") === 0;
      if (!json) return undo("You may have been signed out. Reload the page and try again.");
      return r.json().then(function (body) {
        if (r.ok && body.ok) window.location.reload();
        else undo(body.message || "Couldn't move it.");
      });
    }).catch(function () { undo("Couldn't reach the server. Check the connection and try again."); });
  }

  cal.addEventListener("click", function (e) {
    if (swallowClick) { e.preventDefault(); return; }
    if (e.target.closest("a")) return;  // a task, or a day's +
    var day = e.target.closest(".cal-day");
    if (day && day.dataset.newUrl) window.location.href = day.dataset.newUrl;
  });
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
