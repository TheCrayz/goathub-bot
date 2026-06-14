// Backdoor-/Invite-Login. Extern (nicht inline), weil die globale CSP
// (script-src 'self') Inline-Scripts blockt — inline würde der Handler nie
// laufen und das Formular Credentials per nativem GET in die URL schreiben.
(function () {
  var f = document.getElementById("f"),
    btn = document.getElementById("btn"),
    err = document.getElementById("err");
  if (!f) return;
  f.addEventListener("submit", function (e) {
    e.preventDefault();
    err.textContent = "";
    btn.disabled = true;
    btn.textContent = "Signing in…";
    var email = document.getElementById("email").value.trim().toLowerCase();
    var password = document.getElementById("pw").value;
    fetch("/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
      body: JSON.stringify({ email: email, password: password }),
    })
      .then(function (r) {
        if (r.ok) {
          window.location.href = "/";
          return;
        }
        return r
          .json()
          .catch(function () {
            return {};
          })
          .then(function (j) {
            btn.disabled = false;
            btn.textContent = "Sign in →";
            err.textContent =
              j && j.detail
                ? j.detail
                : r.status === 429
                ? "Too many attempts — try again later."
                : "Wrong email or password.";
          });
      })
      .catch(function () {
        btn.disabled = false;
        btn.textContent = "Sign in →";
        err.textContent = "Network error — is the server reachable?";
      });
  });
})();
