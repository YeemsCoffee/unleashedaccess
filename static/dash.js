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
