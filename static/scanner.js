/* Scan screen: camera scanning + result display + container action buttons. */
(function () {
  "use strict";

  var readerEl = document.getElementById("reader");
  var resultEl = document.getElementById("result");
  var actionsEl = document.getElementById("actions");
  var startBtn = document.getElementById("start-btn");
  var manualForm = document.getElementById("manual-form");
  var manualInput = document.getElementById("manual-code");
  var employeeSel = document.getElementById("employee");

  var scanner = null;
  var busy = false;            // a scan is being processed / result showing
  var lastCode = null;
  var lastCodeAt = 0;
  var CLIENT_DEBOUNCE_MS = 4000;
  var RESUME_AFTER_MS = 2500;

  // Remember who is scanning on this device.
  employeeSel.value = localStorage.getItem("gdn_employee") || "";
  if (employeeSel.value === "" || !employeeSel.querySelector('option[value="' + employeeSel.value + '"]')) {
    employeeSel.value = "";
  }
  employeeSel.addEventListener("change", function () {
    localStorage.setItem("gdn_employee", employeeSel.value);
  });

  startBtn.addEventListener("click", startCamera);

  function startCamera() {
    if (typeof Html5Qrcode === "undefined") {
      showResult("err", "Scanner library missing — use the box below to type codes.");
      return;
    }
    startBtn.disabled = true;
    startBtn.textContent = "Starting camera…";
    scanner = new Html5Qrcode("reader");
    scanner
      .start(
        { facingMode: "environment" },
        { fps: 10, qrbox: qrbox, aspectRatio: 1.4 },
        onDecoded,
        function () {} // per-frame decode misses; ignore
      )
      .then(function () {
        startBtn.classList.add("hidden");
      })
      .catch(function (err) {
        startBtn.disabled = false;
        startBtn.textContent = "Start camera";
        showResult(
          "err",
          "Camera unavailable (" + err + "). Note: browsers require HTTPS or localhost " +
            "for camera access. You can still type codes below."
        );
      });
  }

  function qrbox(w, h) {
    var size = Math.floor(Math.min(w, h) * 0.75);
    return { width: size, height: size };
  }

  function onDecoded(text) {
    var nowMs = Date.now();
    if (busy) return;
    if (text === lastCode && nowMs - lastCodeAt < CLIENT_DEBOUNCE_MS) return;
    lastCode = text;
    lastCodeAt = nowMs;
    submitCode(text);
  }

  manualForm.addEventListener("submit", function (e) {
    e.preventDefault();
    var code = manualInput.value.trim();
    if (code && !busy) {
      manualInput.value = "";
      submitCode(code);
    }
  });

  function submitCode(code) {
    busy = true;
    hideActions();
    showResult("warn", "…");
    post("/api/scan", { code: code, employee_id: employeeId() })
      .then(function (res) {
        if (res.status === "needs_action") {
          showResult("warn", res.message);
          showActions(res);
        } else if (res.status === "ok") {
          showResult("ok", res.message);
          doneSoon();
        } else if (res.status === "duplicate") {
          showResult("warn", res.message);
          doneSoon();
        } else {
          showResult("err", res.message || "Something went wrong.");
          doneSoon();
        }
      })
      .catch(function () {
        showResult("err", "Could not reach the server. Try again.");
        doneSoon();
      });
  }

  function showActions(res) {
    actionsEl.innerHTML = "";
    res.actions.forEach(function (a) {
      var btn = document.createElement("button");
      btn.className = "btn act-" + a.action;
      btn.textContent = a.label;
      btn.addEventListener("click", function () {
        hideActions();
        showResult("warn", "…");
        post("/api/container-action", {
          container_code: res.container_code,
          action: a.action,
          employee_id: employeeId(),
        })
          .then(function (r) {
            showResult(r.status === "ok" ? "ok" : "err", r.message);
            doneSoon();
          })
          .catch(function () {
            showResult("err", "Could not reach the server. Try again.");
            doneSoon();
          });
      });
      actionsEl.appendChild(btn);
    });
    var cancel = document.createElement("button");
    cancel.className = "btn act-CANCEL";
    cancel.textContent = "Cancel";
    cancel.addEventListener("click", function () {
      hideActions();
      hideResult();
      busy = false;
    });
    actionsEl.appendChild(cancel);
    actionsEl.classList.remove("hidden");
  }

  function doneSoon() {
    setTimeout(function () {
      hideResult();
      busy = false;
    }, RESUME_AFTER_MS);
  }

  function employeeId() {
    return employeeSel.value ? parseInt(employeeSel.value, 10) : null;
  }

  function post(url, body) {
    return fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then(function (r) {
      return r.json();
    });
  }

  function showResult(cls, msg) {
    resultEl.className = "result " + cls;
    resultEl.textContent = msg;
  }
  function hideResult() {
    resultEl.className = "result hidden";
  }
  function hideActions() {
    actionsEl.classList.add("hidden");
    actionsEl.innerHTML = "";
  }
})();
